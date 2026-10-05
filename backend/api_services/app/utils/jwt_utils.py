import jwt
from datetime import datetime, timedelta, timezone
from api_services.app.config import JWT_SECRET

def create_access_token(data: dict):
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(days=7)

    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    return token