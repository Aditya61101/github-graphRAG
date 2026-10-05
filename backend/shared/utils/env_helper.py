import os

from dotenv import load_dotenv
load_dotenv()

def require_env(name: str, default=None) -> str:
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value