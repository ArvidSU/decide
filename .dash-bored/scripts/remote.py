"""Deploy this checkout to the machine that hosts the Decisions server, and check what it serves.

Usage: python3 .dash-bored/scripts/remote.py status
       python3 .dash-bored/scripts/remote.py deploy [--no-clients]
       python3 .dash-bored/scripts/remote.py follow-setup [--off] [--every SECONDS]
       python3 .dash-bored/scripts/remote.py follow --dir DIR [--install | --uninstall]   (on the server's machine)

The server runs from compose.yaml in REMOTE_DIR (relative to the remote home, default: decisions)
on REMOTE_SSH (user@host), with DECISIONS_TOKEN and DECISIONS_BASE_URL in REMOTE_DIR/.env. Set
REMOTE_SSH and REMOTE_DIR in .dash-bored/.env.

status  status JSON: does the server serve this checkout's code? Compares the fingerprint in the
        wheel name it serves with this checkout's, computed the way decisions/dist.py does. The
        server is REMOTE_URL, else the url in ~/.config/decisions/client.toml. Read-only.
deploy  ships HEAD (refuses uncommitted changes to what goes into the image), builds the image,
        snapshots the database into REMOTE_DIR/backups/ (keeps the last 10), upgrades a copy of
        the snapshot with the new image (`decide migrate`), and restarts the server, which then
        upgrades the real database the same way, keeping its data. If the copy can't be upgraded
        it stops before the restart: the server keeps running the previous code on an untouched
        database. Then it reinstalls decide, the skill and the resumer with the server's
        installer, on the remote machine and on this one, wherever they're connected to this
        server (--no-clients skips that).
follow-setup  makes the server deploy itself whenever origin's main moves: over ssh, clones this repository's
        origin next to REMOTE_DIR (REMOTE_DIR-src) and installs a launchd job there that runs `follow` every
        --every seconds (default 120). --off removes the job. The machine needs read access to origin.
follow  one round, on the server's machine, from that clone: pulls main, and when it has commits the server
        doesn't run, deploys them into DIR the way `deploy` does: backup, trial upgrade of a copy, restart.
        It only moves forward: a server that runs a commit origin's main doesn't have (a `deploy` of work that
        isn't pushed yet) is left alone until main contains it. A commit whose migration fails on the copy is
        not tried again; the next commit is. Any other failure is tried again after half an hour. Status in DIR/.follow/status.json, log in DIR/.follow/follow.log.
        The machines' decide, skill and resumer follow the server by themselves (`decide update`, which
        every resumer does), so nothing is installed on them from here.

Stdlib only; runs on the macOS system python3 (3.9).
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# What the image is built from: uncommitted changes here would make HEAD differ from the checkout.
SHIPPED = ["decisions", "pyproject.toml", "README.md", "LICENSE", "Dockerfile", "compose.yaml", ".dockerignore"]
# Left alone on the remote: its secrets, which commit it runs, its backups, what `follow` keeps, and
# dash-bored's own checkouts.
KEEP = ["/.env", "/.deployed", "/backups/", "/.follow/", "/.dash-bored/.env", "/.dash-bored/components/external/",
        "/.dash-bored/themes/external/"]
KEEP_BACKUPS = 10
FOLLOW_SERVICE = "com.decisions.follow"
RETRY_AFTER = 1800  # seconds before `follow` tries a deploy that failed again: each try takes a backup
CLIENT_CONFIG = os.path.expanduser("~/.config/decisions/client.toml")
# Noninteractive ssh gets a bare PATH: add Docker Desktop's CLI and uv's tools.
REMOTE_PATH = ('export PATH="/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:'
               '$HOME/.local/bin:/opt/homebrew/bin:$PATH"')
EXIT_MARK = "__remote_exit__"
LOCAL_PATH = os.pathsep.join([os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                              os.environ.get("PATH", "")])


def out(state, detail):
    print(json.dumps({"state": state, "detail": detail}))


def step(msg):
    print("==> " + msg, flush=True)


def setting(name, default=None):
    value = os.environ.get(name) or default
    if not value:
        sys.exit("Set %s in .dash-bored/.env." % name)
    return value


def git(*args):
    return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True, check=True).stdout


def fingerprint(root):
    """The 10 hex digits in the version of the wheel a server running `root`'s code serves
    (decisions/dist.py wheel())."""
    pkg = Path(root) / "decisions"
    h = hashlib.sha256((Path(root) / "LICENSE").read_text().encode())
    for p in sorted(p for p in pkg.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")):
        h.update(p.relative_to(pkg).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:10]


def served(url, timeout=5):
    """The fingerprint of the code the server at `url` runs, from the wheel its installer installs."""
    with urllib.request.urlopen(url.rstrip("/") + "/install.sh", timeout=timeout) as resp:
        m = re.search(r"/install/decisions-[^+]+\+([0-9a-f]+)-py3", resp.read().decode())
    if not m:
        raise RuntimeError("no wheel in %s/install.sh" % url)
    return m.group(1)


def client_config(text):
    """(url, token) from a client.toml written by `decide connect`."""
    url = re.search(r'^url = "([^"]*)"', text, re.M)
    token = re.search(r'^token = "([^"]*)"', text, re.M)
    return (url.group(1) if url else None), (token.group(1) if token else None)


def local_client():
    try:
        with open(CLIENT_CONFIG) as fh:
            return client_config(fh.read())
    except FileNotFoundError:
        return None, None


def dirty():
    return git("status", "--porcelain", "--", *SHIPPED).strip()


# ---------------------------------------------------------------------------------------- status

def status():
    url = os.environ.get("REMOTE_URL") or local_client()[0]
    if not url:
        out("unknown", "No server: set REMOTE_URL in .dash-bored/.env, or `decide connect` this machine.")
        return
    mine = fingerprint(ROOT)
    head = git("rev-parse", "--short", "HEAD").strip()
    try:
        theirs = served(url)
    except Exception as exc:
        out("error", "%s is unreachable: %s" % (url, exc))
        return
    if theirs == mine:
        out("healthy", "%s serves this checkout (%s, %s)." % (url, head, mine))
    elif dirty():
        out("warning", "%s serves %s; this checkout has uncommitted changes (%s). Commit, then Deploy."
            % (url, theirs, mine))
    else:
        out("warning", "%s serves %s, not this checkout (%s, %s). Deploy to update it." % (url, theirs, head, mine))


# ---------------------------------------------------------------------------------------- deploy

class Remote:
    def __init__(self, host, path):
        self.host, self.path = host, path

    def run(self, script, capture=False, stdin=None, check=True):
        """Run `script` in REMOTE_DIR. Output streams unless captured. The exit status comes back
        as a marker line: ssh to this machine reports 0 whatever the command exits with."""
        full = '%s; cd %s && {\n%s\n}; echo "%s $?"' % (REMOTE_PATH, shlex.quote(self.path), script, EXIT_MARK)
        proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, full],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if capture else subprocess.STDOUT, text=True)
        if capture:
            stdout, stderr = proc.communicate(stdin)
            lines = stdout.splitlines(True)
        else:
            proc.stdin.write(stdin or "")
            proc.stdin.close()
            lines, stderr = [], ""
            for line in proc.stdout:
                if line.startswith(EXIT_MARK):
                    lines.append(line)
                else:
                    print(line, end="", flush=True)
            proc.wait()
        marks = [l for l in lines if l.startswith(EXIT_MARK)]
        code = int(marks[-1].split()[1]) if marks else (proc.returncode or 255)
        if check and code:
            sys.exit("Failed on %s (exit %d): %s%s" % (self.host, code, script,
                                                       "\n" + stderr.strip() if stderr.strip() else ""))
        return "".join(l for l in lines if not l.startswith(EXIT_MARK)) if capture else code

    def env(self, name):
        return self.run("sed -n 's/^%s=//p' .env" % name, capture=True).strip().strip('"\'')

    def url(self):
        """Where the server is reached from the machine this runs on."""
        return self.env("DECISIONS_BASE_URL")


class Local(Remote):
    """The server's directory on this machine: what `follow` deploys into."""
    def __init__(self, path):
        self.host, self.path = None, path

    def run(self, script, capture=False, stdin=None, check=True):
        proc = subprocess.run("%s; cd %s && {\n%s\n}" % (REMOTE_PATH, shlex.quote(self.path), script), shell=True,
                              input=stdin, text=True, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else subprocess.STDOUT)
        if check and proc.returncode:
            sys.exit("Failed (exit %d): %s%s" % (proc.returncode, script,
                                                 "\n" + proc.stderr.strip() if capture and proc.stderr.strip() else ""))
        return proc.stdout if capture else proc.returncode

    def url(self):
        return "http://127.0.0.1:" + self.run("docker compose port decisions 8787", capture=True).strip().rsplit(":", 1)[1]


def ship(remote):
    """HEAD, as the server's directory, with the commit it is in decisions/.commit (decisions/dist.py commit()):
    the image has no git history to ask. Returns the fingerprint of the code shipped."""
    tmp = tempfile.mkdtemp(prefix="decisions-deploy-")
    try:
        archive = subprocess.run(["git", "-C", ROOT, "archive", "HEAD"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        shipped = fingerprint(tmp)
        sha, date, subject = git("log", "-1", "--format=%H%x00%cI%x00%s").rstrip("\n").split("\0", 2)
        with open(os.path.join(tmp, "decisions", ".commit"), "w") as fh:
            json.dump({"hash": sha, "date": date, "subject": subject}, fh)
        cmd = ["rsync", "-az", "--delete", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10"]
        cmd += ["--exclude=" + k for k in KEEP]
        dest = "%s:%s/" % (remote.host, remote.path) if remote.host else remote.path + "/"
        subprocess.run(cmd + [tmp + "/", dest], check=True)
        return shipped
    finally:
        shutil.rmtree(tmp)


def snapshot(remote, name):
    """Copy the container's /data to backups/<name>. False when there's no container yet."""
    container = remote.run("docker compose ps -a -q decisions", capture=True).strip()
    if not container:
        return False
    # docker cp, unlike docker compose cp, also copies out of a stopped container.
    remote.run("rm -rf backups/%s && mkdir -p backups && docker cp %s:/data backups/%s"
               % (name, container, name))
    return True


def trial_upgrade(remote, name):
    """Upgrade a copy of the snapshot with the image just built. Returns what `decide migrate` said
    when it can't be done, else None."""
    trial = "backups/%s.trial" % name
    remote.run("rm -rf %s && cp -R backups/%s %s" % (trial, name, trial))
    # As root: the image's own user can't write to a directory mounted from the host.
    code = remote.run('docker compose run --rm --no-deps -T -u 0 -v "$PWD/%s:/trial" '
                      "-e DECISIONS_DB=/trial/decisions.db decisions decide migrate > %s.log 2>&1"
                      % (trial, trial), check=False)
    said = remote.run("cat %s.log; rm -rf %s %s.log" % (trial, trial, trial), capture=True).strip()
    print(said)
    return said if code else None


def prune(remote):
    names = sorted(n for n in remote.run("ls -1 backups", capture=True).split() if n)
    for n in names[:-KEEP_BACKUPS]:
        remote.run("rm -rf backups/%s" % shlex.quote(n))


def wait_for(url, expected, seconds=90):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = served(url, timeout=3)
            if last == expected:
                return
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    sys.exit("%s didn't come up with %s within %ds (last: %s). See `docker compose logs` on the server."
             % (url, expected, seconds, last))


def install_clients(remote, url):
    """Rerun the server's installer wherever a client is connected to it, with its token.
    The token goes in on stdin / in the environment, never on a command line."""
    installer = "curl -fsSL %s/install.sh | sh" % shlex.quote(url)
    remote_url, remote_token = client_config(remote.run("cat ~/.config/decisions/client.toml 2>/dev/null || true",
                                                        capture=True))
    targets = [("remote " + remote.host, remote_url, remote_token,
                lambda token: remote.run("read -r DECISIONS_TOKEN && export DECISIONS_TOKEN && " + installer,
                                         stdin=token + "\n"))]
    local_url, local_token = local_client()
    targets.append(("this machine", local_url, local_token,
                    lambda token: subprocess.run(installer, shell=True, check=True,
                                                 env=dict(os.environ, PATH=LOCAL_PATH, DECISIONS_TOKEN=token))))
    for label, client_url, token, install in targets:
        if (client_url or "").rstrip("/") != url.rstrip("/"):
            print("==> skipping %s: its decide isn't connected to %s" % (label, url))
        elif not token:
            print("==> skipping %s: its decide has no token" % label)
        else:
            step("updating decide, the skill and the resumer on " + label)
            install(token)


class CantUpgrade(Exception):
    """The new code's migrations fail on a copy of the server's database."""


def roll_out(remote, head):
    """Ship HEAD into the server's directory and make the server run it, keeping its data: build, back the
    database up, upgrade a copy of the backup, and only then restart. .deployed names the commit the server
    runs, so it's written last. Returns the backup's name."""
    step("shipping %s to %s" % (head, "%s:%s" % (remote.host, remote.path) if remote.host else remote.path))
    expected = ship(remote)
    step("building the image")
    remote.run("docker compose build")
    name = "%s-before-%s" % (time.strftime("%Y%m%d-%H%M%S"), head)
    if snapshot(remote, name):
        step("backed up the database to %s/backups/%s" % (remote.path, name))
        step("upgrading a copy of it with the new image")
        failure = trial_upgrade(remote, name)
        if failure:
            raise CantUpgrade("The server's database can't be upgraded to the schema of %s.\nNothing was restarted: "
                              "the server still runs the previous code, on an untouched database. See above; fix the "
                              "migration (MIGRATIONS in decisions/store.py) and deploy again." % head)
    step("starting the server")
    remote.run("docker compose up -d")
    url = remote.url()
    wait_for(url, expected)
    remote.run("echo %s > .deployed" % shlex.quote("%s %s" % (head, time.strftime("%Y-%m-%dT%H:%M:%S%z"))))
    step("%s serves %s (%s)" % (url, head, expected))
    prune(remote)
    return name


def deploy(args):
    remote = Remote(setting("REMOTE_SSH"), setting("REMOTE_DIR", "decisions"))
    if dirty():
        sys.exit("Uncommitted changes to what goes into the image; commit them first:\n" + dirty())
    head = git("rev-parse", "--short", "HEAD").strip()
    try:
        name = roll_out(remote, head)
    except CantUpgrade as exc:
        sys.exit(str(exc))
    url = remote.url()
    if not args.no_clients:
        install_clients(remote, url)
    print("done: %s runs %s. Backup: %s:%s/backups/%s" % (url, head, remote.host, remote.path, name))


# ---------------------------------------------------------------------------------------- follow

def commit(rev):
    """The full hash of `rev` in this clone, or None when it doesn't have it."""
    proc = subprocess.run(["git", "-C", ROOT, "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                          capture_output=True, text=True)
    return proc.stdout.strip() or None


def follow_status(path, state=None, detail=None, head=None):
    """Read DIR/.follow/status.json, or write it (and say so, once per change: this runs every few minutes)."""
    file = os.path.join(path, ".follow", "status.json")
    try:
        with open(file) as fh:
            was = json.load(fh)
    except (OSError, ValueError):
        was = {}
    if state is None:
        return was
    now = {"state": state, "detail": detail, "head": head}
    if now != {k: was.get(k) for k in now}:
        print("%s %s: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), state, detail), flush=True)
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "w") as fh:
            json.dump(dict(now, at=time.time()), fh)
    return now


def follow(args):
    """One round: pull, and deploy HEAD if the server runs an ancestor of it."""
    path = os.path.abspath(os.path.expanduser(args.dir))
    before = commit("HEAD")
    try:
        git("fetch", "--quiet", "origin", args.branch)
        git("merge", "--quiet", "--ff-only", "FETCH_HEAD")
    except subprocess.CalledProcessError as exc:
        return follow_status(path, "error", "Can't pull %s into %s: %s" % (args.branch, ROOT, exc.stderr.strip()), before)
    head = commit("HEAD")
    if head != before and not os.environ.get("FOLLOW_PULLED"):
        # This script may be among what changed: deploy with the new one.
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                  dict(os.environ, FOLLOW_PULLED="1"))
    short = git("rev-parse", "--short", "HEAD").strip()
    try:
        with open(os.path.join(path, ".deployed")) as fh:
            deployed = fh.read().split()[0]
    except (OSError, IndexError):
        deployed = None
    was = follow_status(path)
    if deployed and commit(deployed) == head:
        return follow_status(path, "healthy", "The server runs %s, the head of %s." % (short, args.branch), head)
    if deployed and subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", commit(deployed) or deployed,
                                    head], capture_output=True).returncode:
        return follow_status(path, "waiting", "The server runs %s, which %s (%s) doesn't contain: someone deployed "
                             "work that isn't pushed. Leaving it until it is." % (deployed, args.branch, short), head)
    if was.get("head") == head and (was.get("state") == "stuck" or
                                    was.get("state") == "error" and time.time() - was.get("at", 0) < RETRY_AFTER):
        return was
    follow_status(path, "deploying", "Deploying %s (the server runs %s)." % (short, deployed or "nothing yet"), head)
    try:
        roll_out(Local(path), short)
    except CantUpgrade as exc:
        return follow_status(path, "stuck", "%s Not trying %s again; the next commit is tried." % (exc, short), head)
    except (SystemExit, subprocess.CalledProcessError) as exc:  # e.g. Docker isn't running: tried again later
        return follow_status(path, "error", "Deploying %s failed: %s" % (short, exc), head)
    return follow_status(path, "healthy", "The server runs %s, the head of %s." % (short, args.branch), head)


def follow_service(args):
    """Install (or remove) the launchd job that runs `follow` on this machine."""
    import plistlib
    plist = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % FOLLOW_SERVICE)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", "%s/%s" % (domain, FOLLOW_SERVICE)], capture_output=True)
    if args.uninstall:
        if os.path.exists(plist):
            os.remove(plist)
        print("removed %s: the server no longer follows %s" % (FOLLOW_SERVICE, args.branch))
        return
    path = os.path.abspath(os.path.expanduser(args.dir))
    log = os.path.join(path, ".follow", "follow.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    with open(plist, "wb") as fh:
        plistlib.dump({"Label": FOLLOW_SERVICE, "RunAtLoad": True, "StartInterval": args.every,
                       "ProgramArguments": [sys.executable, os.path.abspath(__file__), "follow", "--dir", path,
                                            "--branch", args.branch],
                       "StandardOutPath": log, "StandardErrorPath": log}, fh)
    deadline = time.time() + 10  # bootout returns while the job is still being torn down
    while subprocess.run(["launchctl", "bootstrap", domain, plist], capture_output=True).returncode:
        if time.time() > deadline:
            sys.exit("launchctl bootstrap %s %s failed" % (domain, plist))
        time.sleep(0.5)
    print("installed %s: every %ds, %s deploys origin's %s into %s. Log: %s"
          % (plist, args.every, ROOT, args.branch, path, log))


def follow_setup(args):
    """From the developer's machine: set the server's machine up to follow origin, or stop it."""
    remote = Remote(setting("REMOTE_SSH"), setting("REMOTE_DIR", "decisions"))
    script = '/usr/bin/python3 "$PWD-src/.dash-bored/scripts/remote.py" follow --dir "$PWD" --branch %s ' \
             % shlex.quote(args.branch)
    if args.off:
        remote.run('[ ! -d "$PWD-src" ] || ' + script + "--uninstall")
        return
    origin = git("remote", "get-url", "origin").strip()
    step("cloning %s on %s, next to %s" % (origin, remote.host, remote.path))
    remote.run('[ -d "$PWD-src/.git" ] || git clone --quiet --branch %s %s "$PWD-src"'
               % (shlex.quote(args.branch), shlex.quote(origin)))
    remote.run(script + "--install --every %d" % args.every)
    ahead = git("rev-list", "--count", "origin/%s..HEAD" % args.branch).strip()
    if ahead != "0":
        print("note: this checkout is %s commit(s) ahead of origin/%s. The server gets them when they're pushed."
              % (ahead, args.branch))


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="remote.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("deploy")
    d.add_argument("--no-clients", action="store_true", help="don't reinstall decide on the machines")
    f = sub.add_parser("follow")
    f.add_argument("--dir", required=True, help="the server's directory on this machine (compose.yaml, .env)")
    f.add_argument("--install", action="store_true", help="install the launchd job that runs this regularly")
    f.add_argument("--uninstall", action="store_true", help="remove that job")
    u = sub.add_parser("follow-setup")
    u.add_argument("--off", action="store_true", help="stop following: remove the job from the server's machine")
    for x in (f, u):
        x.add_argument("--branch", default="main")
        x.add_argument("--every", type=int, default=120, help="seconds between rounds")
    a = p.parse_args()
    if a.cmd == "follow":
        follow_service(a) if a.install or a.uninstall else follow(a)
    else:
        {"status": status, "deploy": lambda: deploy(a), "follow-setup": lambda: follow_setup(a)}[a.cmd]()
