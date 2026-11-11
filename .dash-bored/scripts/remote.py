"""Deploy this checkout to the machine that hosts the Decisions server, and check what it serves.

Usage: python3 .dash-bored/scripts/remote.py status
       python3 .dash-bored/scripts/remote.py deploy [--no-clients]

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
# Left alone on the remote: its secrets, its backups, and dash-bored's own checkouts.
KEEP = ["/.env", "/backups/", "/.dash-bored/.env", "/.dash-bored/components/external/",
        "/.dash-bored/themes/external/"]
KEEP_BACKUPS = 10
CLIENT_CONFIG = os.path.expanduser("~/.config/decisions/client.toml")
# Noninteractive ssh gets a bare PATH: add Docker Desktop's CLI and uv's tools.
REMOTE_PATH = ('export PATH="/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:'
               '$HOME/.local/bin:/opt/homebrew/bin:$PATH"')
EXIT_MARK = "__remote_exit__"
LOCAL_PATH =os.pathsep.join([os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
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


def ship(remote, head):
    """HEAD, as the remote's REMOTE_DIR. Returns the fingerprint of the code shipped."""
    tmp = tempfile.mkdtemp(prefix="decisions-deploy-")
    try:
        archive = subprocess.run(["git", "-C", ROOT, "archive", "HEAD"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        with open(os.path.join(tmp, ".deployed"), "w") as fh:
            fh.write("%s %s\n" % (head, time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        shipped = fingerprint(tmp)
        cmd = ["rsync", "-az", "--delete", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10"]
        cmd += ["--exclude=" + k for k in KEEP]
        subprocess.run(cmd + [tmp + "/", "%s:%s/" % (remote.host, remote.path)], check=True)
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


def deploy(args):
    remote = Remote(setting("REMOTE_SSH"), setting("REMOTE_DIR", "decisions"))
    if dirty():
        sys.exit("Uncommitted changes to what goes into the image; commit them first:\n" + dirty())
    head = git("rev-parse", "--short", "HEAD").strip()
    step("shipping %s to %s:%s" % (head, remote.host, remote.path))
    expected = ship(remote, head)
    url = remote.env("DECISIONS_BASE_URL")
    step("building the image")
    remote.run("docker compose build")
    name = "%s-before-%s" % (time.strftime("%Y%m%d-%H%M%S"), head)
    if snapshot(remote, name):
        step("backed up the database to %s/backups/%s" % (remote.path, name))
        step("upgrading a copy of it with the new image")
        failure = trial_upgrade(remote, name)
        if failure:
            sys.exit("The server's database can't be upgraded to the schema of %s.\nNothing was restarted: "
                     "the server still runs the previous code, on an untouched database. See above; fix the migration "
                     "(MIGRATIONS in decisions/store.py) and deploy again." % head)
    step("starting the server")
    remote.run("docker compose up -d")
    wait_for(url, expected)
    step("%s serves %s (%s)" % (url, head, expected))
    prune(remote)
    if not args.no_clients:
        install_clients(remote, url)
    print("done: %s runs %s. Backup: %s:%s/backups/%s" % (url, head, remote.host, remote.path, name))


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="remote.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("deploy")
    d.add_argument("--no-clients", action="store_true", help="don't reinstall decide on the machines")
    a = p.parse_args()
    status() if a.cmd == "status" else deploy(a)
