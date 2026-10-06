from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv()

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
JWT_SECRET = os.getenv("JWT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Webhook & Storage Configuration
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
REPOS_STORAGE_DIR = os.getenv(
    "REPOS_STORAGE_DIR",
    str(Path(__file__).resolve().parents[2] / "data" / "repos"),
)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(Path(__file__).resolve().parents[2] / 'data' / 'decisionguard.db').as_posix()}",
)