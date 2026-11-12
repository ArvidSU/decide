"""`decide server update`: upgrade a server installed with `decide server install` to upstream's code, keeping
its data.

One round: fetch upstream (a git repository) into a mirror in the state directory, and pick the commit to run:
its newest release tag, or the head of its main branch (`follow`). If that's ahead of the code installed here:
unpack it, have that code build its own wheel, back the database up, upgrade a copy of the backup with the new
code's `decide migrate`, and only if that works install the wheel and restart the server, which then upgrades
the real database the same way. If the copy can't be upgraded, nothing is installed or restarted. A database is
never reset or deleted.

It only moves forward: code that upstream's commit doesn't contain is left alone. A commit that can't be run
here (its migration fails on the copy, or the server doesn't come up with it) is not tried again; the next one
is. Any other failure is tried again after RETRY_AFTER. What happened is in STATUS, which the server shows in
Settings → Version.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import re
import shutil
import site
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import dist
from .config import STATE_DIR, Config

FOLLOW = ("tags", "main")
# A release: v1.2.0 or 1.2.0. Anything else (v2.0.0-rc1, nightly) isn't followed.
RELEASE = re.compile(r"^v?\d+(\.\d+)*$")
KEEP_BACKUPS = 10
RETRY_AFTER = 1800  # seconds before an update that failed is tried again: each try takes a backup
MIRROR = os.path.join(STATE_DIR, "upstream.git")
STATUS = os.path.join(STATE_DIR, "server-update.json")
SERVER_LOG = os.path.join(STATE_DIR, "server.log")


class Stuck(Exception):
    """This commit can't be run here. It's not tried again."""


class Failed(Exception):
    """Something else went wrong. It's tried again later."""


def checkout() -> str | None:
    """The source tree this code runs from, when it does (a checkout, or an editable install of one): that's its
    developer's to update. None for an installed package."""
    root = dist.PACKAGE.parent
    return str(root) if (root / ".git").exists() or (root / "pyproject.toml").is_file() else None


# ---------------------------------------------------------------------------------------- upstream

def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-C", MIRROR, *args], capture_output=True, text=text, timeout=300,
                              env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})  # never wait for a password
    except FileNotFoundError:
        raise Failed("git isn't installed (or not on PATH)")
    except subprocess.TimeoutExpired:
        raise Failed(f"git {args[0]} took more than 5 minutes")
    if proc.returncode:
        err = proc.stderr if text else proc.stderr.decode(errors="replace")
        raise Failed(f"git {args[0]}: {err.strip() or f'exit {proc.returncode}'}")
    return proc


def fetch(cfg: Config):
    """Bring the mirror up to date with what is followed: upstream's tags, or its main branch."""
    if not os.path.isdir(MIRROR):
        os.makedirs(MIRROR)
        _git("init", "--quiet", "--bare")
    refs = "+refs/tags/*:refs/tags/*" if cfg.follow == "tags" else "+refs/heads/main:refs/heads/main"
    _git("fetch", "--quiet", "--prune", "--no-tags", cfg.upstream, refs)


def target(cfg: Config) -> dict | None:
    """The commit to run: {"hash", "date", "subject", "label"}. None when upstream has no release to follow."""
    if cfg.follow == "main":
        label, ref = "main", "refs/heads/main"
    else:
        tags = [t for t in _git("tag", "--list", "--sort=-v:refname").stdout.split() if RELEASE.match(t)]
        if not tags:
            return None
        label, ref = tags[0], "refs/tags/" + tags[0]
    sha, date, subject = _git("log", "-1", "--format=%H%x00%cI%x00%s", ref).stdout.rstrip("\n").split("\0", 2)
    return {"hash": sha, "date": date, "subject": subject, "label": label}


def _name(t: dict) -> str:
    return f"{t['label']} ({t['hash'][:7]})"


def _of(cfg: Config) -> str:
    return f"{'the newest release' if cfg.follow == 'tags' else 'the head of main'} of {cfg.upstream}"


def look(cfg: Config) -> tuple[str, str, dict | None]:
    """Fetch upstream and compare it with the code installed here. Returns (state, what that means, the commit
    to run): `current` = this is it, `behind` = it's ahead of this code, `waiting` = there's nothing to move to."""
    fetch(cfg)
    t = target(cfg)
    if t is None:
        return "waiting", (f"{cfg.upstream} has no releases yet (tags like v1.2.0), so there's nothing to follow "
                           "and the server keeps the code it has. To follow its main branch instead, set "
                           "`follow = \"main\"` in the server's config, or pass --follow main."), None
    mine = dist.commit()
    if mine and mine["hash"] == t["hash"]:
        return "current", f"The server's code is {_name(t)}, {_of(cfg)}.", t
    if mine and subprocess.run(["git", "-C", MIRROR, "merge-base", "--is-ancestor", mine["hash"], t["hash"]],
                               capture_output=True).returncode:
        return "waiting", (f"The server's code is {mine['hash'][:7]}, which {_name(t)}, {_of(cfg)}, doesn't "
                           "contain: it's ahead of it, or from somewhere else. Leaving it until upstream has it."), t
    return "behind", (f"{_name(t)} is {_of(cfg)}: {t['subject']}. The server's code is "
                      f"{mine['hash'][:7] if mine else 'a commit nobody recorded'}."), t


# ---------------------------------------------------------------------------------------- the new code

def _env(staging: str) -> dict:
    return os.environ | {"PYTHONPATH": staging, "PYTHONDONTWRITEBYTECODE": "1"}


def stage(t: dict, into: str) -> dict:
    """Unpack the commit into `into`, with the commit it is in decisions/.commit (there's no git history there,
    see dist.commit()), and have that code build its own wheel. Returns {"wheel": its path, "build"}."""
    archive = _git("archive", "--format=tar", t["hash"], text=False).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    if not os.path.isfile(os.path.join(into, "decisions", "dist.py")):
        raise Stuck(f"{_name(t)} isn't the Decisions app: it has no decisions/dist.py.")
    with open(os.path.join(into, "decisions", dist.COMMIT.name), "w") as fh:
        json.dump({"hash": t["hash"], "date": t["date"], "subject": t["subject"]}, fh)
    build = ("import json; from decisions import dist; name, data = dist.wheel(); open(name, 'wb').write(data); "
             "print(json.dumps({'wheel': name, 'build': dist.fingerprint()}))")
    proc = subprocess.run([sys.executable, "-c", build], cwd=into, env=_env(into), capture_output=True, text=True)
    if proc.returncode:
        raise Stuck(f"The code of {_name(t)} can't build its wheel: {proc.stderr.strip()}")
    built = json.loads(proc.stdout)
    return {"wheel": os.path.join(into, built["wheel"]), "build": built["build"]}


def backup(db_path: str, short: str) -> str | None:
    """A consistent copy of the database (it may be in use) in backups/ next to it. None when there's no
    database yet."""
    if not os.path.exists(db_path):
        return None
    dest = Path(db_path).parent / "backups" / f"{time.strftime('%Y%m%d-%H%M%S')}-before-{short}.db"
    dest.parent.mkdir(exist_ok=True)
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return str(dest)


def trial(staging: str, saved: str) -> str | None:
    """Upgrade a copy of the backup with the new code. Returns what `decide migrate` said when it can't be
    done, else None."""
    copy = saved.removesuffix(".db") + ".trial"
    shutil.copyfile(saved, copy)
    try:
        proc = subprocess.run([sys.executable, "-m", "decisions", "migrate", "--db", copy], cwd=staging,
                              env=_env(staging), capture_output=True, text=True)
        said = (proc.stdout + proc.stderr).strip()
        print(said, flush=True)
        return (said or f"decide migrate exited {proc.returncode}") if proc.returncode else None
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(copy + suffix)


def prune(db_path: str):
    """Keep the last KEEP_BACKUPS backups this made."""
    for old in sorted((Path(db_path).parent / "backups").glob("*-before-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()


def schema_version(db_path: str) -> int | None:
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------- installing

def installer(wheel: str) -> list[str]:
    """The command that replaces the code installed here with `wheel`, using what installed it: uv
    (`uv tool install`), pipx, or pip in this environment."""
    def tool(name):
        path = shutil.which(name)
        if not path:
            raise Failed(f"this decide was installed with {name}, which isn't on PATH now ({os.environ.get('PATH')})")
        return path
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        return [tool("uv"), "tool", "install", "--force", "--python", ">=3.11", wheel]
    if (prefix / "pipx_metadata.json").is_file():
        return [tool("pipx"), "install", "--force", wheel]
    if importlib.util.find_spec("pip") is None:
        if shutil.which("uv"):
            return [shutil.which("uv"), "pip", "install", "--python", sys.executable, "--reinstall", "--no-deps", wheel]
        raise Failed(f"don't know how to install into {sys.prefix}: it has no pip, and it isn't a uv or pipx tool")
    user = sys.prefix == sys.base_prefix and site.ENABLE_USER_SITE and dist.PACKAGE.is_relative_to(
        site.getusersitepackages())
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", "--no-deps",
            *(["--user"] if user else []), wheel]


def install(wheel: str, build: str):
    """Install the wheel over the code that's here, and check that the code here is then `build`."""
    cmd = installer(wheel)
    done = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT)
    if done.returncode:
        raise Failed(f"`{' '.join(cmd)}` failed (exit {done.returncode}).")
    if dist.fingerprint() != build:
        raise Failed(f"`{' '.join(cmd)}` ran, but the code in {dist.PACKAGE} is still build {dist.fingerprint()}, "
                     f"not {build}: is decide installed somewhere that installer doesn't reach?")


def local_url(cfg: Config) -> str:
    """Where the server is reached from its own machine."""
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    return f"http://{f'[{host}]' if ':' in host else host}:{cfg.port}"


def served(url: str, timeout: float = 3) -> str:
    """The build of the code the server at `url` runs, from the wheel its installer installs."""
    with urllib.request.urlopen(url + "/install.sh", timeout=timeout) as resp:
        m = re.search(r"/install/decisions-[^+]+\+([0-9a-f]+)-py3", resp.read().decode())
    if not m:
        raise RuntimeError(f"no wheel in {url}/install.sh")
    return m.group(1)


def comes_up(url: str, build: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if served(url) == build:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def roll_out(cfg: Config, t: dict, restart) -> str:
    """Make the server run commit `t`, keeping its data. Returns what was done, in words."""
    step = lambda msg: print("==> " + msg, flush=True)
    running = dist.fingerprint()
    with tempfile.TemporaryDirectory(prefix="decisions-update-") as tmp:
        step(f"unpacking {_name(t)}")
        staging = os.path.join(tmp, "new")
        staged = stage(t, staging)
        if staged["build"] == running:
            # Nothing to install: the commit doesn't change the code. Note that the code here is that commit too.
            with contextlib.suppress(OSError):
                shutil.copyfile(os.path.join(staging, "decisions", dist.COMMIT.name), dist.COMMIT)
            return (f"The server's code is {_name(t)}, {_of(cfg)}. It's the same code as before (build {running}), "
                    "so nothing was restarted.")
        saved = backup(cfg.db_path, t["hash"][:7])
        if saved:
            prune(cfg.db_path)  # now, not at the end: an update that keeps failing takes a backup each try
            step(f"backed up the database to {saved}")
            step("upgrading a copy of it with the new code")
            failure = trial(staging, saved)
            if failure:
                raise Stuck(f"The server's database can't be upgraded to the schema of {_name(t)}: {failure}\n"
                            "Nothing was installed or restarted: the server still runs the previous code, on an "
                            "untouched database.")
        # The code that runs now, as a wheel, to put back if the new one doesn't work out.
        name, data = dist.wheel()
        os.makedirs(os.path.join(tmp, "previous"))
        previous = os.path.join(tmp, "previous", name)
        with open(previous, "wb") as fh:
            fh.write(data)
        schema = schema_version(cfg.db_path)
        step(f"installing {os.path.basename(staged['wheel'])}")
        try:
            install(staged["wheel"], staged["build"])
        except Failed:
            if dist.fingerprint() != running:  # half done: the server isn't restarted, so it should find its code
                with contextlib.suppress(Failed):
                    install(previous, running)
            raise
        step("restarting the server")
        restart()
        url = local_url(cfg)
        if not comes_up(url, staged["build"], cfg.update_timeout):
            if schema_version(cfg.db_path) != schema:
                raise Failed(f"The server didn't come up with {_name(t)} at {url} after it upgraded the database, "
                             "which the previous code can't read, so the new code is left in place. See its log: "
                             f"{SERVER_LOG}." + (f" The database from before is in {saved}." if saved else ""))
            step("the server didn't come up: putting the previous code back")
            install(previous, running)
            restart()
            back = comes_up(url, running, cfg.update_timeout)
            raise Stuck(f"The server didn't come up with {_name(t)} at {url} (see its log: {SERVER_LOG}). The "
                        f"previous code (build {running}) was put back"
                        + ("" if back else ", but the server hasn't come up with it either") + ". The database "
                        "wasn't changed.")
        return (f"The server runs {_name(t)}, {_of(cfg)} (build {staged['build']})."
                + (f" The database from before is in {saved}." if saved else ""))


# ---------------------------------------------------------------------------------------- one round

def read_status() -> dict:
    try:
        with open(STATUS) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _status(cfg: Config, state: str, detail: str, t: dict | None) -> dict:
    """Record how the round went. `at` is since when it's been like this, `checked` when it was last looked at."""
    was = read_status()
    now = {"state": state, "detail": detail, "target": t and t["hash"], "follow": cfg.follow, "upstream": cfg.upstream}
    same = now == {k: was.get(k) for k in now}
    now |= {"at": was["at"] if same else time.time(), "checked": time.time()}
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATUS + ".tmp", "w") as fh:
        json.dump(now, fh)
    os.replace(STATUS + ".tmp", STATUS)
    return now


def summary() -> dict | None:
    """What the updater last did on this machine, for the GUI. None when it never ran here."""
    s = read_status()
    if not s:
        return None
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None
    return {"follow": s.get("follow"), "upstream": s.get("upstream"), "state": s.get("state"),
            "detail": s.get("detail"), "at": iso(s.get("at")), "checked": iso(s.get("checked"))}


def update(cfg: Config, restart, retry: bool = False) -> dict:
    """One round. `restart()` restarts the server's service. Returns the status: `state` is healthy, waiting
    (nothing to move to), stuck (this commit can't be run here) or error (tried again later)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "server-update.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("error: another `decide server update` is running on this machine")
        try:
            state, detail, t = look(cfg)
        except Failed as exc:
            return _status(cfg, "error", f"Can't look at {cfg.upstream}: {exc}", None)
        was = read_status()
        failed_before = was.get("target") == (t and t["hash"]) and (
            was.get("state") == "stuck" or was.get("state") == "error" and time.time() - was.get("at", 0) < RETRY_AFTER)
        if state == "waiting":
            return _status(cfg, "waiting", detail, t)
        if state == "current":
            # The code is in place. Is it what answers? Not after an install whose restart didn't work out.
            url = local_url(cfg)
            try:
                answers = served(url)
            except Exception as exc:
                answers = None
                problem = f"the server at {url} doesn't answer ({exc})"
            if answers == dist.fingerprint():
                return _status(cfg, "healthy", detail, t)
            if answers:
                problem = f"the server at {url} runs build {answers}, not that code ({dist.fingerprint()})"
            return _status(cfg, "error", f"{detail} But {problem}. Restart it; its log is {SERVER_LOG}.", t)
        if failed_before and not retry:
            return _status(cfg, was["state"], was["detail"], t)
        _status(cfg, "updating", f"Updating to {_name(t)}.", t)
        try:
            detail = roll_out(cfg, t, restart)
        except Stuck as exc:
            return _status(cfg, "stuck", f"{exc} Not trying {_name(t)} again; the next one is tried "
                           "(`decide server update --retry` tries it anyway).", t)
        except (Failed, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            return _status(cfg, "error", f"Updating to {_name(t)} failed: {exc} Trying again in "
                           f"{RETRY_AFTER // 60} min (`decide server update --retry`: now).", t)
        return _status(cfg, "healthy", detail, t)
