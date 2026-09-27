from fastapi import APIRouter, HTTPException

from ..core import security
from ..core.security import create_token
from ..db.users import UserRepository

router = APIRouter()


@router.post("/auth/login")
def login(email: str, password: str, db=None):
    """Sign a user in and return a session token."""
    repo = UserRepository(db)
    user = repo.find_by_email(email)
    if user is None or not security.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token(user["id"])}


@router.post("/auth/register")
def register(email: str, password: str, db=None):
    """Create a new account."""
    repo = UserRepository(db)
    return repo.create(email, security.hash_password(password))
