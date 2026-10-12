"""Security utilities: password hashing and verification using bcrypt."""

import bcrypt


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        # Check standard bcrypt hash
        if hashed_password.startswith(("$2b$", "$2a$")):
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
        # Fallback for plain-text or unexpected seeds
        return plain_password == hashed_password
    except (ValueError, TypeError):
        return False
