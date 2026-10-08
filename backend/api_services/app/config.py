from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv()
from ai_services.retrievers.settings import RetrievalSettings

RETRIEVAL_SETTINGS = RetrievalSettings.from_env()

JWT_SECRET = os.getenv("JWT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Webhook & Storage Configuration
GITHUB_APP_WEBHOOK_SECRET = os.getenv("GITHUB_APP_WEBHOOK_SECRET")
REPOS_STORAGE_DIR = os.getenv(
    "REPOS_STORAGE_DIR",
    str(Path(__file__).resolve().parents[2] / "data" / "repos"),
)
ADRS_STORAGE_DIR = os.getenv(
    "ADRS_STORAGE_DIR",
    str(Path(__file__).resolve().parents[2] / "data" / "adrs"),
)
MAX_ADR_FILE_SIZE_BYTES = int(
    os.getenv("MAX_ADR_FILE_SIZE_BYTES", str(10 * 1024 * 1024))  # 10 MB default
)
MAX_ADR_FILES_PER_UPLOAD = int(os.getenv("MAX_ADR_FILES_PER_UPLOAD", "20"))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(Path(__file__).resolve().parents[2] / 'data' / 'decisionguard.db').as_posix()}",
)
