"""Auth helpers: password hashing (bcrypt directly), api-key + session tokens."""
from __future__ import annotations

import secrets

import bcrypt

# bcrypt has a 72-byte password limit; truncate to avoid ValueError.
_BCRYPT_MAX = 72


def hash_password(pw: str) -> str:
    pw_b = pw.encode("utf-8")[:_BCRYPT_MAX]
    return bcrypt.hashpw(pw_b, bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8")[:_BCRYPT_MAX], pw_hash.encode("utf-8"))
    except Exception:
        return False


def generate_api_key() -> str:
    """32-byte URL-safe token, prefixed for readability."""
    return "xben_" + secrets.token_urlsafe(32)
