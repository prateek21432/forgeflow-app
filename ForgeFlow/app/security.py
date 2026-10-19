"""Password hashing and redaction; credentials are never agent inputs."""

import hashlib
import hmac
import re
import secrets

from .config import settings


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${value}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def redact(value: str) -> str:
    for secret in [
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_backup_api_key.get_secret_value(),
        settings.llm_api_key.get_secret_value(),
        settings.llm_backup_api_key.get_secret_value(),
        settings.gpt_api_key.get_secret_value(),
        settings.secret_key.get_secret_value(),
        settings.github_token.get_secret_value(),
    ]:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return re.sub(r"sk-or-v1-[A-Za-z0-9]+", "[REDACTED]", value)
