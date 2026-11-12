#!/bin/sh
# Install the Decisions server on this machine (macOS or Linux): the `decide` CLI, the server as a background
# service, and a job that keeps it on upstream's code.
#   curl -fsSL https://raw.githubusercontent.com/ArvidSU/decide/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/ArvidSU/decide/main/install.sh | sh -s -- --network
# Run it again to upgrade: it keeps the config and the data. See usage() for the flags.
set -eu

DEFAULT_REPO="https://github.com/ArvidSU/decide"

usage() {
  cat <<EOF
Install the Decisions server on this machine, as a background service that updates itself.

  --network            accept connections from other machines (host 0.0.0.0) and require a token, which is
                       generated. Without it the server only listens on this machine (127.0.0.1), without one.
  --host ADDR          listen on this address instead (--network is --host 0.0.0.0)
  --port N             listen on this port (default 8787)
  --base-url URL       the URL other machines and links use, if not http://<hostname>:<port>
  --follow tags|main   what to install and keep up with: the newest release (a tag like v1.2.0; the default), or
                       every commit on the main branch. While there are no releases, the head of main is installed.
  --repo URL           the git repository to install from and follow (default $DEFAULT_REPO)
  --no-auto-update     don't install the job that updates the server every hour (\`decide server update\` does it by hand)
  --no-uv              don't download uv (https://docs.astral.sh/uv/) when there's no uv, pipx or python3 >= 3.11 with pip

On a machine that has a server already (its config file exists), this upgrades it: the config and the database are
kept, and the database is backed up first. Change host, port, base_url, follow or upstream in the config file.
EOF
}

step() { echo "==> $*"; }
die() { echo "error: $*" >&2; exit 1; }

main() {
  REPO="" FOLLOW="" HOST="" PORT="" BASE_URL="" AUTO_UPDATE=1 FETCH_UV=1 RELEASED=1
  ON_PATH="$(command -v decide || true)"  # the decide a new shell finds, before this adds anything to PATH
  while [ $# -gt 0 ]; do
    case "$1" in
      --network) HOST="0.0.0.0"; shift ;;
      --host|--port|--base-url|--follow|--repo)
        [ $# -ge 2 ] || { echo "error: $1 needs a value" >&2; exit 2; }
        case "$1" in
          --host) HOST="$2" ;; --port) PORT="$2" ;; --base-url) BASE_URL="$2" ;;
          --follow) FOLLOW="$2" ;; --repo) REPO="$2" ;;
        esac
        shift 2 ;;
      --host=*) HOST="${1#*=}"; shift ;;
      --port=*) PORT="${1#*=}"; shift ;;
      --base-url=*) BASE_URL="${1#*=}"; shift ;;
      --follow=*) FOLLOW="${1#*=}"; shift ;;
      --repo=*) REPO="${1#*=}"; shift ;;
      --no-auto-update) AUTO_UPDATE=0; shift ;;
      --no-uv) FETCH_UV=0; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "error: unknown option: $1 (--help lists them)" >&2; exit 2 ;;
    esac
  done
  case "$FOLLOW" in ""|tags|main) ;; *) echo "error: --follow is tags or main, not $FOLLOW" >&2; exit 2 ;; esac

  # ---- what this machine can do, before anything is changed
  case "$(uname -s)" in
    Darwin) ;;
    Linux)
      systemctl --user show-environment >/dev/null 2>&1 || die "this installs the server as a systemd user service, \
and \`systemctl --user\` doesn't work here (a container, or a shell without a login session?). Run it with Docker \
instead ($DEFAULT_REPO#running-it-on-another-machine), or install decide (\`uv tool install git+$DEFAULT_REPO\`) and \
keep \`decide serve\` running yourself." ;;
    *) die "this installs a launchd (macOS) or systemd (Linux) service; on $(uname -s), run it with Docker instead: \
$DEFAULT_REPO#running-it-on-another-machine" ;;
  esac
  command -v git >/dev/null 2>&1 || die "git is needed, to fetch the code now and its updates later. Install it \
(macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git) and run this again."

  # ---- a server that's here already keeps its config: that's where what it listens on and follows is set
  CONFIG="${DECISIONS_CONFIG:-$HOME/.config/decisions/config.toml}"
  EXISTING=0
  if [ -f "$CONFIG" ]; then
    EXISTING=1
    kept() {  # kept KEY FLAG GIVEN DEFAULT: the config's value for KEY, which a flag can't change
      value="$(sed -n -e '/^[[:space:]]*\[/q' \
        -e "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*\$/\1/p" "$CONFIG" \
        | sed 's/[[:space:]]*$//' | head -n 1)"
      [ -n "$value" ] || value="$4"
      if [ -n "$3" ] && [ "$3" != "$value" ]; then
        die "a server is already set up here, with $1 = $value in $CONFIG. To change it to $3, edit that file \
and run this again without $2."
      fi
      echo "$value"
    }
    kept host "--host / --network" "$HOST" "127.0.0.1" >/dev/null
    kept port --port "$PORT" 8787 >/dev/null
    kept base_url --base-url "$BASE_URL" "" >/dev/null
    FOLLOW="$(kept follow --follow "$FOLLOW" tags)"
    REPO="$(kept upstream --repo "$REPO" "$DEFAULT_REPO")"
    step "a server is already set up here: keeping $CONFIG and its data"
  fi
  FOLLOW_GIVEN="$FOLLOW"
  FOLLOW="${FOLLOW:-tags}"
  REPO="${REPO:-$DEFAULT_REPO}"

  TMP="$(mktemp -d "${TMPDIR:-/tmp}/decisions-install.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  trap 'exit 1' INT TERM
  export GIT_TERMINAL_PROMPT=0

  # ---- the commit to install
  step "looking at $REPO"
  REF="refs/heads/main"
  LABEL="main"
  if [ "$FOLLOW" = tags ]; then
    TAGS="$(git ls-remote --tags --refs --sort=-v:refname "$REPO")" || die "can't reach $REPO"
    TAG="$(printf '%s\n' "$TAGS" | sed 's|.*refs/tags/||' | grep -E '^v?[0-9]+(\.[0-9]+)*$' | head -n 1 || true)"
    if [ -n "$TAG" ]; then
      REF="refs/tags/$TAG"
      LABEL="$TAG"
    else
      RELEASED=0
      echo "    it has no releases yet (tags like v1.2.0): installing the head of its main branch. The server moves"
      echo "    to the first release when there is one. (--follow main keeps it on main instead.)"
    fi
  fi
  git init --quiet --bare "$TMP/src.git"
  git -C "$TMP/src.git" fetch --quiet --depth 1 "$REPO" "$REF" || die "can't fetch $REF from $REPO"
  COMMIT_HASH="$(git -C "$TMP/src.git" rev-parse 'FETCH_HEAD^{commit}')"
  COMMIT_DATE="$(git -C "$TMP/src.git" log -1 --format=%cI "$COMMIT_HASH")"
  COMMIT_SUBJECT="$(git -C "$TMP/src.git" log -1 --format=%s "$COMMIT_HASH")"
  SHORT="$(printf %.7s "$COMMIT_HASH")"
  mkdir "$TMP/src" "$TMP/out"
  git -C "$TMP/src.git" archive --format=tar "$COMMIT_HASH" | tar -x -C "$TMP/src"
  [ -f "$TMP/src/decisions/dist.py" ] || die "$LABEL ($SHORT) of $REPO isn't the Decisions app: it has no decisions/dist.py"

  # ---- what installs it: uv, else pipx, else pip, else a uv that's downloaded
  PY=""
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      PY="$(command -v "$candidate")"
      break
    fi
  done
  pip_works() {  # pip is there, and the system doesn't forbid `pip install --user` (PEP 668)
    [ -n "$PY" ] && "$PY" -m pip --version >/dev/null 2>&1 && "$PY" -c 'import os, sys, sysconfig
sys.exit(os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")))'
  }
  if command -v uv >/dev/null 2>&1; then
    WITH=uv
  elif command -v pipx >/dev/null 2>&1 && [ -n "$PY" ]; then
    WITH=pipx
  elif pip_works; then
    WITH=pip
  elif [ "$FETCH_UV" = 1 ]; then
    step "there's no uv, pipx or python3 >= 3.11 with pip here: installing uv (https://docs.astral.sh/uv/), which brings its own Python"
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh
    else
      die "need curl or wget to download uv; or install uv yourself (https://docs.astral.sh/uv/) and run this again"
    fi
    PATH="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv was installed, but isn't in ${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}; \
open a new shell and run this again"
    WITH=uv
  else
    die "need uv (https://docs.astral.sh/uv/), pipx, or python3 >= 3.11 with pip"
  fi
  if [ "$WITH" = uv ] && grep -q 'editable *=' "$(uv tool dir)/decisions/uv-receipt.toml" 2>/dev/null; then
    die "the decide on this machine runs from a checkout (an editable install), which is its developer's to update: \
pull there and restart the server. Nothing was changed."
  fi

  # ---- that code builds its own wheel, and proves it can run an existing database before anything is replaced
  cat > "$TMP/prepare.py" <<'PY'
import json
import os
import sys

from decisions import dist, update
from decisions.config import load_config

env = os.environ
# The code has no git history here, so it's told which commit it is (dist.commit()): Settings → Version names
# it, and `decide server update` knows what it would be updating from.
with open(dist.COMMIT, "w") as fh:
    json.dump({"hash": env["COMMIT_HASH"], "date": env["COMMIT_DATE"], "subject": env["COMMIT_SUBJECT"]}, fh)
name, data = dist.wheel()
with open(os.path.join(env["OUT"], name), "wb") as fh:
    fh.write(data)
if os.path.exists(env["CONFIG"]):
    cfg = load_config(env["CONFIG"])
    saved = update.backup(cfg.db_path, env["COMMIT_HASH"][:7])
    if saved:
        update.prune(cfg.db_path)
        print(f"==> backed up the database to {saved}", flush=True)
        print("==> upgrading a copy of it with the new code", flush=True)
        failure = update.trial(os.getcwd(), saved)
        if failure:
            sys.exit(f"error: the database ({cfg.db_path}) can't be upgraded to the schema of this code: {failure}\n"
                     "Nothing was installed or restarted: the server still runs the code it had, on an untouched "
                     "database.")
PY
  step "preparing $LABEL ($SHORT): $COMMIT_SUBJECT"
  run_python() {
    if [ -n "$PY" ]; then "$PY" "$@"; else uv run --no-project --python '>=3.11' python "$@"; fi
  }
  (
    cd "$TMP/src" || exit 1
    export PYTHONPATH="$TMP/src" PYTHONDONTWRITEBYTECODE=1 OUT="$TMP/out" CONFIG COMMIT_HASH COMMIT_DATE COMMIT_SUBJECT
    run_python "$TMP/prepare.py"
  ) || exit 1
  WHEEL="$(ls "$TMP/out"/decisions-*.whl)"

  step "installing decide with $WITH"
  case "$WITH" in
    uv)
      uv tool install --force --python '>=3.11' "$WHEEL"
      BIN="$(uv tool dir --bin)" ;;
    pipx)
      pipx install --force --python "$PY" "$WHEEL"
      BIN="$(pipx environment --value PIPX_BIN_DIR)" ;;
    pip)
      "$PY" -m pip install --user --disable-pip-version-check --force-reinstall --no-deps "$WHEEL"
      BIN="$("$PY" -m site --user-base)/bin" ;;
  esac
  DECIDE="$BIN/decide"
  [ -x "$DECIDE" ] || die "decide was installed, but it isn't in $BIN"
  PATH="$BIN:$PATH"

  # ---- the service, and the job that updates it
  step "installing the server as a background service"
  set -- server install --wait 60
  if [ "$EXISTING" = 0 ]; then
    [ -z "$HOST" ] || set -- "$@" --host "$HOST"
    [ -z "$PORT" ] || set -- "$@" --port "$PORT"
    [ -z "$BASE_URL" ] || set -- "$@" --base-url "$BASE_URL"
    [ -z "$FOLLOW_GIVEN" ] || set -- "$@" --follow "$FOLLOW_GIVEN"
    [ "$REPO" = "$DEFAULT_REPO" ] || set -- "$@" --upstream "$REPO"
  fi
  [ "$AUTO_UPDATE" = 0 ] || set -- "$@" --auto-update
  "$DECIDE" "$@"

  echo
  echo "Decisions is running. Next:"
  echo "  1. Open the GUI link above."
  echo "  2. Connect your coding agents: in the GUI, Settings → Machines → Connect a machine, and run the command"
  echo "     it shows on each machine whose agents should ask you there (this one too)."
  if [ "$EXISTING" = 0 ] && { [ -z "$HOST" ] || [ "$HOST" = 127.0.0.1 ] || [ "$HOST" = localhost ]; }; then
    echo "  It only listens on this machine. For agents on other machines, set host = \"0.0.0.0\" and a token in"
    echo "  $CONFIG and run this again (a new install does both with --network)."
  fi
  if [ "$AUTO_UPDATE" = 1 ] && [ "$RELEASED" = 0 ]; then
    echo "  It looks for a release every hour and moves to the first one; until then it stays on this code."
    echo "  Settings → Version says what it runs. To follow the main branch instead: follow = \"main\" in $CONFIG"
  elif [ "$AUTO_UPDATE" = 1 ]; then
    echo "  It updates itself every hour; Settings → Version says what it runs. By hand: decide server update"
  else
    echo "  To upgrade it: decide server update (or run this again)."
  fi
  if [ "$ON_PATH" != "$DECIDE" ]; then
    case "$ON_PATH" in
      "") echo "  decide is in $BIN, which isn't on your PATH: add it (uv: \`uv tool update-shell\`) and open a new shell." ;;
      *) echo "  note: another decide comes first on your PATH ($ON_PATH); the server uses $DECIDE." ;;
    esac
  fi
}

main "$@"
