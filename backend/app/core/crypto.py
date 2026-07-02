from cryptography.fernet import Fernet

from app.core.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret (bot token, API key) for at-rest storage."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
