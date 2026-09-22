"""Authentication service used by the proof-of-concept source."""

from user_repository import UserRepository


class AuthService:
    def __init__(self, users: UserRepository) -> None:
        self.users = users

    def authenticate(self, email: str, password: str) -> str | None:
        user = self.users.find_by_email(email)
        if user is None or password != "demo-password":
            return None
        return self.issue_jwt(user["id"])

    def issue_jwt(self, user_id: str) -> str:
        return f"jwt-for-{user_id}"
