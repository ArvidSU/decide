"""Install assets served by the server, so other machines can install `decide` and the skill from it.

The wheel is built in memory from the running package. Its version carries a content hash, so
re-running the installer after the server is updated always picks up the new code.
"""
from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from functools import lru_cache
from importlib.metadata import distribution
from pathlib import Path

from . import __version__

PACKAGE = Path(__file__).parent
SKILL = PACKAGE / "skill" / "SKILL.md"


def _package_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("."))


def _digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


@lru_cache(maxsize=4)
def _build(fingerprint: str, license_text: str) -> tuple[str, bytes]:
    version = f"{__version__}+{fingerprint[:10]}"
    dist_info = f"decisions-{version}.dist-info"
    files = {f"decisions/{p.relative_to(PACKAGE).as_posix()}": p.read_bytes() for p in _package_files()}
    files[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: decisions\nVersion: {version}\nRequires-Python: >=3.11\n"
        "License-Expression: MIT\nLicense-File: LICENSE\n"
        "Summary: General-purpose decision log (installed from a decisions server)\n").encode()
    files[f"{dist_info}/licenses/LICENSE"] = license_text.encode()
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: decisions\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\ndecide = decisions.cli:main\n"
    record = "".join(f"{name},sha256={_digest(data)},{len(data)}\n" for name, data in files.items())
    files[f"{dist_info}/RECORD"] = (record + f"{dist_info}/RECORD,,\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return f"decisions-{version}-py3-none-any.whl", buf.getvalue()


def wheel() -> tuple[str, bytes]:
    """(filename, bytes) of a wheel of the code this server is running."""
    source_license = PACKAGE.parent / "LICENSE"
    license_text = (source_license.read_text() if source_license.is_file()
                    else distribution("decisions").read_text("licenses/LICENSE"))
    if not license_text:
        raise RuntimeError("package is missing its LICENSE")
    h = hashlib.sha256(license_text.encode())
    for p in _package_files():
        h.update(p.relative_to(PACKAGE).as_posix().encode() + b"\0" + p.read_bytes())
    return _build(h.hexdigest(), license_text)


INSTALL_SH = r'''#!/bin/sh
# Install the `decide` CLI, the agent skill and the resumer from a decisions server.
#   curl -fsSL __URL__/install.sh | sh -s -- --token <token> [--no-resumer] [--no-skill]
set -eu
SERVER="__URL__"
WHEEL="$SERVER/install/__WHEEL__"
TOKEN="${DECISIONS_TOKEN:-}"
RESUMER=1
SKILL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2 ;;
    --token=*) TOKEN="${1#--token=}"; shift ;;
    --no-resumer) RESUMER=0; shift ;;
    --no-skill) SKILL=0; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "==> installing decide from $SERVER"
if command -v uv >/dev/null 2>&1; then
  uv tool install --force --python '>=3.11' "decisions @ $WHEEL"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$WHEEL"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  python3 -m pip install --user --upgrade "$WHEEL"
else
  echo "error: need uv (https://docs.astral.sh/uv/), pipx, or python3 >= 3.11 with pip" >&2
  exit 1
fi

DECIDE="$(command -v decide || true)"
for d in "$HOME/.local/bin" "$HOME/Library/Python"/*/bin; do
  [ -z "$DECIDE" ] && [ -x "$d/decide" ] && DECIDE="$d/decide"
done
if [ -z "$DECIDE" ]; then
  echo "error: decide was installed but isn't on PATH; add ~/.local/bin to PATH and re-run" >&2
  exit 1
fi

echo "==> connecting to $SERVER"
if [ -n "$TOKEN" ]; then "$DECIDE" connect "$SERVER" --token "$TOKEN"; else "$DECIDE" connect "$SERVER"; fi

if [ "$SKILL" = 1 ]; then
  echo "==> installing the agent skill"
  "$DECIDE" skill install
fi

if [ "$RESUMER" = 1 ]; then
  if command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1; then
    echo "==> installing the resumer (resumes agent sessions on this machine when you decide)"
    "$DECIDE" resumer install || echo "warning: resumer not installed; run 'decide resumer run' yourself" >&2
  else
    echo "==> skipping the resumer: no supported agent (claude, codex) on PATH; for others see 'decide resumer install --help'"
  fi
fi

echo "done. Try: decide list"
'''


def install_script(server_url: str) -> str:
    name, _ = wheel()
    return INSTALL_SH.replace("__URL__", server_url.rstrip("/")).replace("__WHEEL__", name)
