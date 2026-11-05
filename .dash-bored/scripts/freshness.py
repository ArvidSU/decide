"""Is what's running/installed on this machine in step with this checkout?

Usage: python3 .dash-bored/scripts/freshness.py <server|cli|skill>

server  the process listening on the Decisions port: up, and started after the last change to
        the Python code it loaded (static/ and skill/ are read from disk per request, so they
        don't count).
cli     the `decide` on this machine: an editable install of this checkout (always current
        code) whose metadata is newer than pyproject.toml, or a copy identical to this checkout.
skill   every installed decisions/SKILL.md under the agents' skills dirs: a symlink into this
        checkout or an identical copy.

Prints status JSON. Read-only; stdlib only; runs on the macOS system python3 (3.9).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = os.path.join(ROOT, "decisions")
URL = os.environ.get("DECISIONS_URL", "http://127.0.0.1:8787")
PORT = urlparse(URL if "://" in URL else "http://" + URL).port or 8787
HOME = os.path.expanduser("~")
SKILL_DIRS = ["~/.claude/skills", "~/.agents/skills"]  # decisions/agents.py AGENTS + SHARED_SKILLS_DIR
SERVER_PLIST = "~/Library/LaunchAgents/com.decisions.server.plist"


def out(state, detail):
    print(json.dumps({"state": state, "detail": detail}))


def short(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def ago(ts):
    secs = int(time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s ago" % (secs // size, unit)
    return "just now"


def py_files(root):
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "static", "skill")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def server():
    try:
        pids = subprocess.run(["lsof", "-nP", "-t", "-iTCP:%d" % PORT, "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=5).stdout.split()
    except FileNotFoundError:
        sys.exit("lsof not found")
    managed = os.path.exists(os.path.expanduser(SERVER_PLIST))
    if not pids:
        out("unknown", "Nothing listening on :%d. Start it from Develop → Serve%s."
            % (PORT, " or launchd (service installed)" if managed else ""))
        return
    pid = pids[0]
    ps = subprocess.run(["ps", "-o", "lstart=,command=", "-p", pid], capture_output=True, text=True).stdout.strip()
    m = re.match(r"(\w{3} \w{3} +\d+ \d\d:\d\d:\d\d \d{4}) +(.*)", ps)
    if not m:
        out("warning", "pid %s on :%d, but its start time is unreadable: %s" % (pid, PORT, ps[:120]))
        return
    started = time.mktime(datetime.strptime(re.sub(" +", " ", m.group(1)), "%a %b %d %H:%M:%S %Y").timetuple())
    cmd = m.group(2)
    who = "pid %s%s, started %s" % (pid, " (launchd service)" if managed else "", ago(started))
    if "decide" not in cmd and "decisions" not in cmd:
        out("warning", "%s on :%d is not a decisions server: %s" % (who, PORT, cmd[:120]))
        return
    newer = sorted((p for p in py_files(PKG) if os.path.getmtime(p) > started), key=os.path.getmtime)
    if newer:
        out("warning", "Out of date: %s changed after it started (%s). Restart it."
            % (", ".join(os.path.relpath(p, PKG) for p in newer[-4:]) + (" +%d more" % (len(newer) - 4) if len(newer) > 4 else ""), who))
    else:
        out("healthy", "Running this checkout's code · %s" % who)


def find_decide():
    for d in os.environ.get("PATH", "").split(os.pathsep) + [os.path.join(HOME, ".local", "bin")]:
        p = os.path.join(d, "decide")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


PROBE = r"""
import json, os, importlib.metadata as md, decisions
d = md.distribution("decisions")
print(json.dumps({"file": decisions.__file__, "direct_url": d.read_text("direct_url.json"),
                  "dist_info": str(d._path) if hasattr(d, "_path") else None}))
"""


def cli():
    exe = find_decide()
    if not exe:
        out("error", "`decide` is not installed (not on PATH or in ~/.local/bin). Use Develop → Reinstall decide CLI.")
        return
    with open(exe) as fh:
        first = fh.readline().strip()
    interp = first[2:].strip() if first.startswith("#!") else "python3"
    try:
        r = subprocess.run(interp.split() + ["-c", PROBE], capture_output=True, text=True, timeout=10)
        info = json.loads(r.stdout)
    except Exception:
        out("error", "%s is broken (its Python can't import decisions). Reinstall it." % short(exe))
        return
    pkg = os.path.dirname(os.path.realpath(info["file"]))
    editable = '"editable": true' in (info.get("direct_url") or "")
    if pkg == os.path.realpath(PKG):
        dist_info = info.get("dist_info")
        if dist_info and os.path.getmtime(os.path.join(ROOT, "pyproject.toml")) > os.path.getmtime(dist_info):
            out("warning", "Editable install of this checkout, but pyproject.toml changed since it was installed. Reinstall.")
        else:
            out("healthy", "%s runs this checkout%s" % (short(exe), " (editable)" if editable else ""))
        return
    mine = {os.path.relpath(p, PKG): sha(p) for p in py_files(PKG)}
    theirs = {os.path.relpath(p, pkg): sha(p) for p in py_files(pkg)}
    diff = sorted(k for k in set(mine) | set(theirs) if mine.get(k) != theirs.get(k))
    if diff:
        out("warning", "Out of date: %s is a copy from %s; differs in %s. Reinstall."
            % (short(exe), short(pkg), ", ".join(diff[:4]) + (" +%d more" % (len(diff) - 4) if len(diff) > 4 else "")))
    else:
        out("healthy", "%s is a copy identical to this checkout" % short(exe))


def skill():
    source = os.path.join(PKG, "skill", "SKILL.md")
    want = sha(source)
    found, stale = [], []
    for d in SKILL_DIRS:
        target = os.path.join(os.path.expanduser(d), "decisions", "SKILL.md")
        if not os.path.lexists(target):
            continue
        label = short(target)
        if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
            found.append(label + " (symlink)")
        elif os.path.exists(target) and sha(target) == want:
            found.append(label)
        else:
            stale.append(label)
    if stale:
        out("warning", "Out of date: %s. Use Develop → Install skill.%s"
            % (", ".join(stale), " Current: " + ", ".join(found) if found else ""))
    elif found:
        out("healthy", "Current: " + ", ".join(found))
    else:
        out("error", "Not installed in %s. Use Develop → Install skill." % " or ".join(SKILL_DIRS))


if __name__ == "__main__":
    views = {"server": server, "cli": cli, "skill": skill}
    if len(sys.argv) != 2 or sys.argv[1] not in views:
        sys.exit("usage: freshness.py <%s>" % "|".join(views))
    views[sys.argv[1]]()
