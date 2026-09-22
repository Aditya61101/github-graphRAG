"""Repository used by the authentication service."""

from database import Database


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_by_email(self, email: str) -> dict | None:
        return self.database.fetch_user_by_email(email)
