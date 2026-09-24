"""Passwords and role-bearing sessions for named users.

The env-configured admin (ui_username/ui_password) keeps working through the
original stateless token — deleting every AppUser can never lock the operator
out. AppUsers get their own signed cookie carrying email+role; passwords are
PBKDF2-SHA256 with per-user salt (hashlib only, no new dependency)."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.settings import get_settings

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = str(stored or "").split("$")
        if scheme != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def _signing_key() -> bytes:
    settings = get_settings()
    material = f"user-session\0{settings.ui_password}\0{settings.settings_fernet_key or 'dev'}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def user_session_token(email: str, role: str) -> str:
    payload = f"{email}|{role}"
    signature = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def parse_user_session_token(token: str) -> tuple[str, str] | None:
    text = str(token or "")
    payload, _, signature = text.rpartition(".")
    if not payload or not signature:
        return None
    expected = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    email, _, role = payload.partition("|")
    if not email or role not in {"admin", "sales"}:
        return None
    return email, role
