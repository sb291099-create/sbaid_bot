"""Шифрование реквизитов продавцов (Fernet, ключ из ENCRYPTION_KEY)."""
from __future__ import annotations

from cryptography.fernet import Fernet


class Cipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError("ENCRYPTION_KEY не задан: реквизиты нельзя хранить открыто")
        self._f = Fernet(key.encode())

    def encrypt(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
