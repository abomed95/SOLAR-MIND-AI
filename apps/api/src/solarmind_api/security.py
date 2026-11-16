"""Password hashing, opaque tokens, MFA (TOTP) and secret encryption."""

import base64
import hashlib
import secrets

import pyotp
from argon2 import PasswordHasher
from argon2 import Type as Argon2Type
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

# Argon2id with the library's RFC 9106 "low memory" defaults.
_hasher = PasswordHasher(type=Argon2Type.ID)
# Verified against when the account does not exist, so that response time does
# not reveal whether an e-mail address is registered.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    """High-entropy opaque token (256 bits). Only its hash is ever stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # A fast hash is appropriate here: the input is random, not a human password.
    return hashlib.sha256(token.encode()).hexdigest()


class SecretBox:
    """Symmetric encryption for small secrets stored in the database."""

    def __init__(self, secret_key: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str | None:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="SOLAR MIND AI")


def verify_totp(secret: str, code: str) -> bool:
    # valid_window=1 tolerates one 30 s step of clock drift.
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
