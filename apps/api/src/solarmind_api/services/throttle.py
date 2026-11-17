"""Counters in Redis: request rate limits, per-account lockout, TOTP replay.

Identifiers (e-mail addresses, keys) are hashed before they become Redis
keys: an operator browsing Redis learns nothing about who tried to log in.
"""

import hashlib
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from solarmind_api.config import Settings
from solarmind_api.errors import RateLimited, ServiceUnavailable


def digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]


async def hit(redis: Redis, key: str, window_s: int) -> int:
    """Increment ``key`` and return its value. INCR and EXPIRE travel in one
    MULTI/EXEC, so a counter can never be left without a time-to-live."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_s, nx=True)
        count, _ = await pipe.execute()
    return int(count)


class AccountGuard:
    """Brute-force protection keyed on the *account*, not the client address.

    The per-IP limit stops one noisy client; it does nothing against a slow
    attack spread over many addresses (botnet, mobile carrier NAT). These
    counters cap failed passwords and failed MFA codes per account over a
    window, whatever their origin. Redis failures fail closed: an
    authentication without brute-force protection is refused.

    Trade-off: an attacker who knows an e-mail address can lock that account
    for ``account_lock_window_s``. That is the accepted price (OWASP ASVS
    2.2.1); the lock is temporary and successful logins are not affected
    once it expires.
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis, self.settings = redis, settings

    def _login_key(self, email: str) -> str:
        return f"lock:login:{digest(email)}"

    def _mfa_key(self, user_id: uuid.UUID) -> str:
        return f"lock:mfa:{user_id}"

    async def _get(self, key: str) -> int:
        try:
            return int(await self.redis.get(key) or 0)
        except RedisError as exc:
            raise ServiceUnavailable("Authentication temporarily unavailable") from exc

    async def _hit(self, key: str) -> None:
        try:
            await hit(self.redis, key, self.settings.account_lock_window_s)
        except RedisError as exc:
            raise ServiceUnavailable("Authentication temporarily unavailable") from exc

    def _locked(self) -> RateLimited:
        minutes = max(1, self.settings.account_lock_window_s // 60)
        return RateLimited(
            f"Too many failed attempts for this account; retry in {minutes} minutes",
            code="account_locked",
        )

    async def check_login(self, email: str) -> None:
        if await self._get(self._login_key(email)) >= self.settings.login_max_failures:
            raise self._locked()

    async def login_failed(self, email: str) -> None:
        await self._hit(self._login_key(email))

    async def check_mfa(self, user_id: uuid.UUID) -> None:
        if await self._get(self._mfa_key(user_id)) >= self.settings.mfa_max_failures:
            raise self._locked()

    async def mfa_failed(self, user_id: uuid.UUID) -> None:
        await self._hit(self._mfa_key(user_id))

    async def succeeded(self, email: str, user_id: uuid.UUID) -> None:
        try:
            await self.redis.delete(self._login_key(email), self._mfa_key(user_id))
        except RedisError:
            pass  # counters expire on their own

    async def claim_totp(self, user_id: uuid.UUID, code: str) -> bool:
        """True the first time ``code`` is used by ``user_id`` within its
        validity (90 s with one step of drift). A code seen by a shoulder
        surfer or a phishing proxy cannot be replayed."""
        try:
            claimed = await self.redis.set(f"totp:{user_id}:{code.strip()}", 1, nx=True, ex=120)
        except RedisError as exc:
            raise ServiceUnavailable("Authentication temporarily unavailable") from exc
        return bool(claimed)
