"""ADR original storage shared by uploads and read-only evaluation authority."""

import os
from pathlib import Path
from urllib.parse import quote


class OriginalStorage:
    def __init__(self, directory, *, bucket=None, session=None):
        self.directory = Path(directory).resolve()
        self.bucket = bucket
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
            )
            self._session = AuthorizedSession(credentials)
        return self._session

    def location(self, repository_id, adr_id, extension):
        for value in (repository_id, adr_id):
            if not value or any(c in value for c in ("/", "\\", ".")):
                raise ValueError("Invalid original identity")
        key = f"{repository_id}/{adr_id}/original{extension}"
        return f"gs://{self.bucket}/{key}" if self.bucket else str(self.directory / key)

    def _key(self, location, repository_id, adr_id):
        prefix = f"gs://{self.bucket}/{repository_id}/{adr_id}/original"
        if not self.bucket or not location.startswith(prefix):
            raise ValueError("Untrusted original location")
        suffix = location[len(prefix):]
        if suffix not in {".md", ".markdown", ".txt", ".pdf", ".docx"}:
            raise ValueError("Untrusted original extension")
        return location[len(f"gs://{self.bucket}/"):]

    def write(self, location, data, mime_type, repository_id, adr_id):
        if location.startswith("gs://"):
            key = self._key(location, repository_id, adr_id)
            response = self.session.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{quote(self.bucket, safe='')}/o",
                params={"uploadType": "media", "name": key, "ifGenerationMatch": "0"},
                headers={"Content-Type": mime_type}, data=data, timeout=60,
            )
            if response.status_code == 412:
                # Retry of an interrupted upload must preserve the immutable original.
                if self.read(location, repository_id, adr_id, len(data)) != data:
                    raise ValueError("Original already exists with different content")
            else:
                response.raise_for_status()
            return
        path = self._local(location, repository_id, adr_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "wb") as stream:
                stream.write(data)
        except OSError:
            path.unlink(missing_ok=True)
            raise

    def _local(self, location, repository_id, adr_id):
        path = Path(location).resolve()
        if not path.is_relative_to(self.directory / repository_id / adr_id):
            raise ValueError("Untrusted original path")
        return path

    def read(self, location, repository_id, adr_id, limit):
        if location.startswith("gs://"):
            key = self._key(location, repository_id, adr_id)
            try:
                with self.session.get(
                    f"https://storage.googleapis.com/storage/v1/b/{quote(self.bucket, safe='')}/o/{quote(key, safe='')}",
                    params={"alt": "media"}, stream=True, timeout=60,
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk[:max(0, limit + 1 - len(data))])
                        if len(data) > limit:
                            break
                    return bytes(data)
            except Exception:
                raise OSError("ADR original unavailable") from None
        with self._local(location, repository_id, adr_id).open("rb") as stream:
            return stream.read(limit + 1)

    def delete(self, location, repository_id, adr_id):
        """Remove only this immutable original; retries of missing objects succeed."""
        if location.startswith('gs://'):
            key = self._key(location, repository_id, adr_id)
            response = self.session.delete(
                f"https://storage.googleapis.com/storage/v1/b/{quote(self.bucket, safe='')}/o/{quote(key, safe='')}",
                timeout=60,
            )
            if response.status_code != 404:
                response.raise_for_status()
        else:
            self._local(location, repository_id, adr_id).unlink(missing_ok=True)


def original_storage(directory):
    return OriginalStorage(directory, bucket=os.getenv("ADR_GCS_BUCKET") or None)
