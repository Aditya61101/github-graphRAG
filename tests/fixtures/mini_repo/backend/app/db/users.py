from .base import BaseRepository


class UserRepository(BaseRepository):
    """Reads and writes users."""

    def find_by_email(self, email: str):
        return self.session.query("users").filter(email=email).first()

    def create(self, email: str, password_hash: str):
        user = {"email": email, "password_hash": password_hash}
        return self.save(user)
