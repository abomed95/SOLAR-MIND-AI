"""HTTP edge helpers and deployed-settings validation (no service needed)."""

from ipaddress import ip_network

import pytest
from pydantic import SecretStr, ValidationError

from solarmind_api.config import Settings
from solarmind_api.middleware import resolve_client_ip

PROXIES = [ip_network("10.0.0.0/8")]


def test_forwarded_for_is_ignored_from_an_untrusted_peer() -> None:
    # Anyone can send the header: only a trusted proxy's copy counts.
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", PROXIES) == "203.0.113.9"
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", []) == "10.0.0.5"


def test_rightmost_untrusted_hop_is_the_client() -> None:
    assert resolve_client_ip("10.0.0.5", "198.51.100.7", PROXIES) == "198.51.100.7"
    # Two proxies in the chain.
    assert resolve_client_ip("10.0.0.5", "198.51.100.7, 10.1.1.1", PROXIES) == "198.51.100.7"
    # A client forging a left-most entry cannot choose its address.
    assert resolve_client_ip("10.0.0.5", "6.6.6.6, 198.51.100.7", PROXIES) == "198.51.100.7"


def test_malformed_or_fully_trusted_chain_falls_back_to_the_peer() -> None:
    assert resolve_client_ip("10.0.0.5", "not-an-ip", PROXIES) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", "10.2.2.2", PROXIES) == "10.0.0.5"
    assert resolve_client_ip(None, "1.2.3.4", PROXIES) == "unknown"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "database_url": SecretStr("postgresql+asyncpg://u:p@h/d"),
        "redis_url": SecretStr("redis://h"),
        "secret_key": SecretStr("x" * 64),
        "cookie_secure": True,
        "web_base_url": "https://app.example.com",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_settings_are_accepted_when_safe() -> None:
    assert _settings().is_deployed


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"secret_key": SecretStr("short")}, "SOLARMIND_SECRET_KEY"),
        ({"cookie_secure": False}, "SOLARMIND_COOKIE_SECURE"),
        ({"web_base_url": "http://app.example.com"}, "SOLARMIND_WEB_BASE_URL"),
    ],
)
def test_unsafe_production_settings_stop_startup(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _settings(**override)


def test_development_keeps_local_defaults() -> None:
    assert not _settings(environment="development", cookie_secure=False,
                         web_base_url="http://localhost:3000").is_deployed  # fmt: skip
