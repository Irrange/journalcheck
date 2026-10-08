from __future__ import annotations
import hashlib
import hmac
import secrets
from pathlib import Path
from cryptography.fernet import Fernet


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        _, rounds, salt, expected = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def private_write(path: Path, data: bytes) -> None:
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    path.chmod(0o600)


class Secrets:
    def __init__(self, root: Path):
        directory = root / 'keys'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        key = directory / 'fernet.key'
        if not key.exists():
            # Never replace a missing key if a database already exists.
            if (root / 'journalcheck.sqlite').exists():
                raise RuntimeError('加密密钥缺失；请恢复原密钥，不能生成替代密钥。')
            private_write(key, Fernet.generate_key())
        key.chmod(0o600)
        self.fernet = Fernet(key.read_bytes())

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode()
