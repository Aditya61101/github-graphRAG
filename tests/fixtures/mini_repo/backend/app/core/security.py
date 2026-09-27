"""Password hashing and token helpers."""
import hashlib
import hmac

SECRET = "change-me"


def hash_password(plain: str) -> str:
    """Hash a password with a static salt (demo only)."""
    return hashlib.sha256(("salt:" + plain).encode()).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    """Compare a login password with the stored hash."""
    return hmac.compare_digest(hash_password(plain), hashed)


def create_token(user_id: str) -> str:
    """Issue a signed session token after a successful login."""
    payload = f"user:{user_id}"
    return payload + "." + _sign(payload)


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), "sha256").hexdigest()
