"""Authentication utilities — Argon2id, sessions, CSRF.

Security properties
-------------------
* Passwords are hashed with Argon2id (memory-hard, side-channel resistant).
* Sessions are signed cookies via ``itsdangerous.TimestampSigner``.
* CSRF tokens are per-session, validated on every mutating request.
* Login is rate-limited and accounts are locked after threshold failures.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, hash_: str) -> bool:
    """Verify a password against an Argon2id hash.

    Returns ``True`` on match, ``False`` on mismatch or invalid hash.
    """
    try:
        return _hasher.verify(hash_, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(hash_: str) -> bool:
    """Check whether the hash parameters are outdated."""
    return _hasher.check_needs_rehash(hash_)


# ---------------------------------------------------------------------------
# Session signing
# ---------------------------------------------------------------------------


class SessionManager:
    """Cookie-based session manager using ``itsdangerous``.

    Parameters
    ----------
    secret_key
        Server-side signing key (from Docker secrets).
    max_age_seconds
        Session lifetime.
    cookie_name
        Name of the session cookie.
    """

    def __init__(
        self,
        secret_key: str,
        *,
        max_age_seconds: int = 86400,
        cookie_name: str = "mediacat_session",
        cookie_secure: bool = False,
    ) -> None:
        self._signer = TimestampSigner(secret_key)
        self._max_age = max_age_seconds
        self.cookie_name = cookie_name
        self.cookie_secure = cookie_secure

    def create_session(self, user_id: str, role: str) -> str:
        """Create a signed session token."""
        payload = f"{user_id}|{role}|{secrets.token_hex(8)}"
        return self._signer.sign(payload).decode("utf-8")

    def validate_session(self, token: str) -> dict[str, str] | None:
        """Validate and decode a session token.

        Returns a dict with ``user_id`` and ``role``, or ``None`` if
        the token is invalid or expired.
        """
        try:
            payload = self._signer.unsign(token, max_age=self._max_age).decode("utf-8")
            parts = payload.split("|")
            if len(parts) < 3:
                return None
            return {"user_id": parts[0], "role": parts[1]}
        except (BadSignature, SignatureExpired):
            return None


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------


class CsrfProtection:
    """Per-session CSRF token generation and validation.

    Tokens are HMAC-SHA256 of the session ID + a server secret, so they
    are tied to the session and cannot be forged without the secret.
    """

    def __init__(self, secret_key: str) -> None:
        self._secret = secret_key.encode("utf-8")

    def generate_token(self, session_id: str) -> str:
        """Generate a CSRF token for the given session."""
        return hmac.new(self._secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def validate_token(self, token: str, session_id: str) -> bool:
        """Validate a CSRF token."""
        expected = self.generate_token(session_id)
        return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------


class LoginRateLimiter:
    """In-memory sliding-window rate limiter for login attempts.

    Used as a fallback when Redis is unavailable.  State is lost on
    process restart and is not shared across workers.

    Parameters
    ----------
    max_attempts
        Maximum failed attempts before lockout.
    window_seconds
        Lockout window duration in seconds.
    """

    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: int = 900,
    ) -> None:
        self._max = max_attempts
        self._window = timedelta(seconds=window_seconds)
        self._attempts: dict[str, list[datetime]] = {}

    async def record_failure(self, key: str) -> None:
        """Record a failed login attempt."""
        now = datetime.now(UTC)
        attempts = self._attempts.setdefault(key, [])
        attempts.append(now)
        cutoff = now - self._window
        self._attempts[key] = [a for a in attempts if a > cutoff]

    async def is_locked(self, key: str) -> bool:
        """Return True if the key (username or IP) is currently locked out."""
        now = datetime.now(UTC)
        cutoff = now - self._window
        recent = [a for a in self._attempts.get(key, []) if a > cutoff]
        return len(recent) >= self._max

    async def clear(self, key: str) -> None:
        """Clear attempts for a key (call on successful login)."""
        self._attempts.pop(key, None)


class RedisLoginRateLimiter:
    """Redis-backed sliding-window rate limiter for login attempts.

    Uses one sorted set per key, scored by Unix timestamp.  Each member
    is unique (timestamp + random hex) so concurrent failures within the
    same millisecond are all recorded.

    Parameters
    ----------
    redis
        An ``redis.asyncio.Redis`` instance.
    max_attempts
        Maximum failed attempts before lockout.
    window_seconds
        Lockout window duration in seconds.
    """

    _KEY_PREFIX = "mediacat:rl:login:"

    def __init__(
        self,
        redis: Any,
        max_attempts: int = 10,
        window_seconds: int = 900,
    ) -> None:
        self._redis = redis
        self._max = max_attempts
        self._window = window_seconds

    async def record_failure(self, key: str) -> None:
        """Record a failed login attempt."""
        now = datetime.now(UTC).timestamp()
        rkey = self._KEY_PREFIX + key
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zadd(rkey, {member: now})
            pipe.zremrangebyscore(rkey, 0, now - self._window)
            pipe.expire(rkey, self._window + 60)
            await pipe.execute()

    async def is_locked(self, key: str) -> bool:
        """Return True if the key (username or IP) is currently locked out."""
        now = datetime.now(UTC).timestamp()
        cutoff = now - self._window
        count = await self._redis.zcount(self._KEY_PREFIX + key, cutoff, "+inf")
        return int(count) >= self._max

    async def clear(self, key: str) -> None:
        """Clear attempts for a key (call on successful login)."""
        await self._redis.delete(self._KEY_PREFIX + key)
