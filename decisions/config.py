"""Configuration: defaults < config file (TOML) < DECISIONS_* env vars < CLI flags."""
from __future__ import annotations

import hmac
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field, fields

DEFAULT_SPACE = "default"
SPACE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


@dataclass
class Space:
    """A hard boundary between contexts (work, private, ...). Every decision, webhook and event
    belongs to one; a space token only ever sees its own space."""
    name: str
    tokens: list[str] = field(default_factory=list)
    # None = the server-wide deadline_days.
    deadline_days: float | None = None


@dataclass(frozen=True)
class Scope:
    """What a caller may see: some spaces, or (spaces=None) all of them."""
    spaces: frozenset[str] | None = None
    # Where the caller's new decisions and webhooks go.
    default: str = DEFAULT_SPACE

    @property
    def everything(self) -> bool:
        return self.spaces is None

    def allows(self, space: str | None) -> bool:
        return self.spaces is None or space in self.spaces

    def where(self, column: str) -> tuple[str, list]:
        """SQL condition (with a leading AND) limiting `column` to this scope."""
        if self.spaces is None:
            return "", []
        return f" AND {column} IN ({','.join('?' * len(self.spaces))})", sorted(self.spaces)


ALL = Scope()


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    db: str = "~/.local/share/decisions/decisions.db"
    # Shared bearer token for the API. Empty = no auth (fine when bound to localhost).
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
    # From [spaces.<name>] tables. The default space always exists, even when not configured.
    spaces: dict[str, Space] = field(default_factory=dict)

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

    @property
    def space_names(self) -> list[str]:
        return sorted({DEFAULT_SPACE, *self.spaces})

    def deadline_days_for(self, space: str) -> float:
        sp = self.spaces.get(space)
        return self.deadline_days if sp is None or sp.deadline_days is None else sp.deadline_days

    def scope_for(self, token: str) -> Scope | None:
        """The admin `token` sees every space, a space's token only that space. Without an admin
        token auth is off: anyone sees everything, though a space token still routes to its space.
        None = not authorized."""
        for sp in self.spaces.values():
            if any(hmac.compare_digest(token, t) for t in sp.tokens):
                return Scope(frozenset({sp.name}), sp.name)
        if not self.token or hmac.compare_digest(token, self.token):
            return ALL
        return None


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


def _spaces(data: dict, admin_token: str) -> dict[str, Space]:
    out: dict[str, Space] = {}
    seen: set[str] = set()
    for name, spec in data.items():
        if not isinstance(spec, dict) or not SPACE_NAME.fullmatch(name):
            raise SystemExit(f"error: [spaces.{name}]: space names are lowercase letters, digits, - and _")
        tokens = spec.get("tokens", [])
        if isinstance(tokens, str):
            tokens = [tokens]
        if not all(isinstance(t, str) and t for t in tokens):
            raise SystemExit(f"error: [spaces.{name}] tokens must be non-empty strings")
        if seen & set(tokens) or admin_token in tokens:
            raise SystemExit(f"error: [spaces.{name}] reuses a token; each token belongs to one space")
        seen |= set(tokens)
        days = spec.get("deadline_days")
        out[name] = Space(name, list(tokens), None if days is None else float(days))
    return out


def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    cfg = Config()
    types = {f.name: f.type for f in fields(Config) if f.name != "spaces"}
    spaces: dict = {}

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
            spaces = data.get("spaces", {})
            break

    for name, kind in types.items():
        env = os.environ.get(f"DECISIONS_{name.upper()}")
        if env is not None:
            setattr(cfg, name, _coerce(kind, env))

    for k, v in (overrides or {}).items():
        if v is not None and k in types:
            setattr(cfg, k, _coerce(types[k], v))
    cfg.spaces = _spaces(spaces, cfg.token)
    return cfg
