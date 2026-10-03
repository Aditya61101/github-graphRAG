import os
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


load_dotenv()


class DriveClient:
    """Client for interacting with the Google Drive API."""

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

    AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
    TOKEN_URI = "https://oauth2.googleapis.com/token"
    REDIRECT_URI = "http://localhost"

    BASE_DIR = Path(__file__).resolve().parent
    TOKEN_FILE = BASE_DIR / "token.json"

    def __init__(self):
        self.service = build(
            "drive",
            "v3",
            credentials=self._get_credentials(),
        )

    def _get_credentials(self):
        """Load, refresh, or create Google OAuth credentials."""

        credentials = self._load_saved_credentials()

        if credentials and credentials.valid:
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            credentials = self._authenticate()

        self._save_credentials(credentials)

        return credentials

    def _load_saved_credentials(self):
        """Load previously saved OAuth credentials, if available."""

        if not self.TOKEN_FILE.exists():
            return None

        return Credentials.from_authorized_user_file(
            self.TOKEN_FILE,
            self.SCOPES,
        )

    def _authenticate(self):
        """Run the OAuth authorization flow."""

        flow = InstalledAppFlow.from_client_config(
            self._get_client_config(),
            self.SCOPES,
        )

        return flow.run_local_server(port=0)

    @staticmethod
    def _get_client_config():
        """Build the Google OAuth client configuration from environment variables."""

        required_variables = {
            "GOOGLE_CLIENT_ID": os.getenv("GOOGLE_CLIENT_ID"),
            "GOOGLE_CLIENT_SECRET": os.getenv("GOOGLE_CLIENT_SECRET"),
            "GOOGLE_AUTH_URI": os.getenv("GOOGLE_AUTH_URI"),
            "GOOGLE_TOKEN_URI": os.getenv("GOOGLE_TOKEN_URI"),
            "GOOGLE_REDIRECT_URI": os.getenv("GOOGLE_REDIRECT_URI"),
        }

        missing_variables = [
            name
            for name, value in required_variables.items()
            if not value
        ]

        if missing_variables:
            raise ValueError(
                "Missing Google OAuth configuration: " + ", ".join(missing_variables)
            )

        return {
            "installed": {
                "client_id": required_variables["GOOGLE_CLIENT_ID"],
                "client_secret": required_variables["GOOGLE_CLIENT_SECRET"],
                "auth_uri": required_variables["GOOGLE_AUTH_URI"],
                "token_uri": required_variables["GOOGLE_TOKEN_URI"],
                "redirect_uris": [required_variables["GOOGLE_REDIRECT_URI"]],
            }
        }

    def _save_credentials(self, credentials):
        """Persist OAuth credentials for subsequent runs."""

        self.TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    def list_files(self, query=None):
        """Return all files matching the optional Drive query."""

        files = []
        page_token = None

        while True:
            response = (
                self.service.files()
                .list(
                    q=query,
                    pageSize=100,
                    pageToken=page_token,
                    fields=(
                        "nextPageToken,"
                        "files(id,name,mimeType,size,parents,webViewLink)"
                    ),
                )
                .execute()
            )

            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")

            if not page_token:
                break

        return files

    def list_folders(self):
        """Return all folders accessible to the authenticated user."""

        return self.list_files(
            query=f"mimeType = '{self.FOLDER_MIME_TYPE}'"
        )

    def list_folder_contents(self, folder_id):
        """Return all files and folders directly inside a folder."""

        return self.list_files(
            query=f"'{folder_id}' in parents"
        )

    def download_file(self, file_id):
        """Download a Drive file and return its contents as bytes."""

        request = self.service.files().get_media(
            fileId=file_id,
        )

        buffer = BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)

        done = False

        while not done:
            _, done = downloader.next_chunk()

        return buffer.getvalue()