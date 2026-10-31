"""Worker-only access to short-lived encrypted technical credentials."""

from api.secrets import decrypt_secret


def decrypt_access(ciphertext: str) -> str:
    return decrypt_secret(ciphertext)
