"""Very small database boundary used by the proof-of-concept source."""


class Database:
    def fetch_user_by_email(self, email: str) -> dict | None:
        """Return a user record for an email address."""
        return {"id": "u-123", "email": email}
