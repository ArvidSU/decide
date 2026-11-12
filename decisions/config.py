"""Configuration: defaults < config file (TOML) < DECISIONS_* env vars < CLI flags."""
from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, fields


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    db: str = "~/.local/share/decisions/decisions.db"
    # Admin token: sees and manages every space. Spaces and their machine tokens live in the
    # database (Settings, or `decide token`). Empty = no auth (fine when bound to localhost).
    token: str = ""
    # Public URL used in webhook payloads and reference links. Defaults to http://host:port.
    base_url: str = ""
    # Actor name recorded in the log for actions taken in the GUI.
    owner: str = "me"
    # How the server reaches its own machine, for webhooks to agents there (`decide watch` against
    # localhost). Empty = 127.0.0.1. The Docker image sets host.docker.internal.
    callback_host: str = ""
    # Open decisions expire (are closed as `expired`) this many days after they're created unless
    # they set their own due_at. 0 = decisions without a due_at never expire.
    deadline_days: float = 4
    # Where "Report upstream" on a piece of feedback opens a prefilled issue, for the app's developers.
    # Empty = no such button.
    feedback_issues_url: str = "https://github.com/ArvidSU/decide/issues/new"
    webhook_timeout: float = 10
    # Give up after this many failed attempts (0 = retry forever).
    webhook_max_attempts: int = 25
    # Exponential backoff: base * 2^(attempt-1), capped at max.
    webhook_backoff_base: float = 10
    webhook_backoff_max: float = 3600
    # When true, a 2xx is not enough: the delivery re-fires every refire_interval
    # seconds until the receiver calls its ack_url.
    webhook_require_ack: bool = False
    webhook_refire_interval: float = 300

    @property
    def db_path(self) -> str:
        return os.path.expanduser(self.db)

    @property
    def public_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        host = self.host
        if host in ("0.0.0.0", "::", ""):  # listening everywhere: links need a name others can resolve
            host = socket.gethostname()
        return f"http://{host}:{self.port}"


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _coerce(kind, value):
    if kind is bool or kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind is int or kind == "int":
        return int(value)
    if kind is float or kind == "float":
        return float(value)
    return str(value)


def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    cfg = Config()
    types = {f.name: f.type for f in fields(Config)}

    candidates = [path] if path else [
        os.environ.get("DECISIONS_CONFIG"),
        "decisions.toml",
        "~/.config/decisions/config.toml",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(os.path.expanduser(candidate)):
            with open(os.path.expanduser(candidate), "rb") as fh:
                data = tomllib.load(fh)
            flat = {k: v for k, v in data.items() if not isinstance(v, dict)}
            for k, v in data.get("webhook", {}).items():
                flat[f"webhook_{k}"] = v
            for k, v in flat.items():
                if k in types:
                    setattr(cfg, k, _coerce(types[k], v))
            break

    for name, kind in types.items():
        env = os.environ.get(f"DECISIONS_{name.upper()}")
        if env is not None:
            setattr(cfg, name, _coerce(kind, env))

    for k, v in (overrides or {}).items():
        if v is not None and k in types:
            setattr(cfg, k, _coerce(types[k], v))
    return cfg
