"""Identity and roles, no Chainlit imports (unit-testable).

Dev mode: the start page (`/start`) issues a signed cookie `ra_session` with
{email, role, department}. The cookie is HMAC-signed with CHAINLIT_AUTH_SECRET, so a client
cannot forge a role. Chainlit's header_auth_callback reads and verifies it.

Production mode: env CHAT_TRUSTED_HEADER=X-User-Email. Identity comes ONLY from that header.
The reverse proxy MUST set it itself and strip it from client requests, otherwise anybody can
impersonate anybody. Cookie and start page are disabled in this mode. Experts are listed
in env CHAT_EXPERT_EMAILS (comma separated); everybody else is an employee.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

COOKIE_NAME = "ra_session"
COOKIE_TTL = 12 * 3600
DEFAULT_EXPERT_PASSWORD = "demo-expert"  # dev only; set CHAT_EXPERT_PASSWORD in any shared stand
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


@dataclass(frozen=True)
class Identity:
    email: str
    role: str  # "employee" | "expert"
    department: str | None = None  # preset chosen on the start page (demo)


def demo_email() -> str:
    return f"demo-{secrets.token_hex(4)}@demo.local"


def expert_password() -> str:
    return os.environ.get("CHAT_EXPERT_PASSWORD") or DEFAULT_EXPERT_PASSWORD


def check_expert_password(candidate: str, env: Mapping[str, str] | None = None) -> bool:
    expected = (env or os.environ).get("CHAT_EXPERT_PASSWORD") or DEFAULT_EXPERT_PASSWORD
    return hmac.compare_digest(candidate.encode(), expected.encode())


def trusted_header(env: Mapping[str, str] | None = None) -> str | None:
    return (env or os.environ).get("CHAT_TRUSTED_HEADER") or None


def identity_from_trusted_header(headers: Mapping[str, str], env: Mapping[str, str] | None = None) -> Identity | None:
    name = trusted_header(env)
    if not name:
        return None
    value = (headers.get(name) or headers.get(name.lower()) or "").strip().lower()
    if not _EMAIL_RE.match(value):
        return None
    experts = {e.strip().lower() for e in (env or os.environ).get("CHAT_EXPERT_EMAILS", "").split(",") if e.strip()}
    return Identity(value, "expert" if value in experts else "employee")


# --- signed cookie ---------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_identity(identity: Identity, secret: str, now: float | None = None) -> str:
    payload = json.dumps(
        {"e": identity.email, "r": identity.role, "d": identity.department, "x": int((now or time.time()) + COOKIE_TTL)},
        ensure_ascii=False,
    ).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_identity(token: str, secret: str, now: float | None = None) -> Identity | None:
    try:
        p, s = token.split(".", 1)
        payload = _unb64(p)
        if not hmac.compare_digest(_unb64(s), hmac.new(secret.encode(), payload, hashlib.sha256).digest()):
            return None
        data = json.loads(payload)
        if data["x"] < (now or time.time()) or data["r"] not in ("employee", "expert"):
            return None
        return Identity(data["e"], data["r"], data.get("d"))
    except Exception:
        return None


def cookie_from_headers(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("cookie") or headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return v
    return None


def identity_from_headers(headers: Mapping[str, str], secret: str, env: Mapping[str, str] | None = None) -> Identity | None:
    """The single entry point used by Chainlit's header_auth_callback."""
    if trusted_header(env):
        return identity_from_trusted_header(headers, env)
    token = cookie_from_headers(headers)
    return verify_identity(token, secret) if token else None
