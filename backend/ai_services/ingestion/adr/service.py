from __future__ import annotations

import hashlib
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
from typing import Any, BinaryIO, Protocol, Sequence
import uuid

from ai_services.ingestion.adr.parser import ADRParseError, parse_adr_content
from ai_services.ingestion.persistence.models import ADRModel
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from ai_services.models.adr_document import ADRDocument

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}


class ADRUploadFile(Protocol):
    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ADRUploadResult:
    index: int
    filename: str
    status: str
    status_code: int
    adr: ADRModel | None = None
    error: str | None = None


class ADRServiceError(Exception):
    """Base exception for ADR service errors."""
    pass


class RepositoryNotFoundError(ADRServiceError):
    """Raised when the specified repository does not exist."""
    pass


class RepositoryAccessDeniedError(ADRServiceError):
    """Raised when the user does not have permission to access the repository."""
    pass


class InvalidFileTypeError(ADRServiceError):
    """Raised when the uploaded file type is not supported."""
    pass


class FileSizeExceededError(ADRServiceError):
    """Raised when the uploaded file exceeds the configured maximum size."""
    pass


class DuplicateADRError(ADRServiceError):
    """Raised when an ADR with identical content already exists for the repository."""

    def __init__(self, message: str, existing_adr: ADRModel) -> None:
        super().__init__(message)
        self.existing_adr = existing_adr


class ADRParsingError(ADRServiceError):
    """Raised when the uploaded ADR cannot be parsed into normalized text."""
    pass


def sanitize_filename(filename: str) -> str:
    """Sanitize original filename to prevent path traversal and shell injection."""
    # Strip any directory path components
    basename = Path(filename).name
    # Remove control characters and non-printable characters
    cleaned = re.sub(r"[\x00-\x1f\x7f/\\]", "", basename).strip()
    return cleaned or "document.txt"


def detect_mime_type(ext: str) -> str:
    """Return standard MIME type for supported document extensions."""
    mapping = {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return mapping.get(ext.lower(), "application/octet-stream")


class ADRService:
    """Coordinates ADR validation, storage, DB persistence, and document parsing."""

    def __init__(
        self,
        sqlite_store: SqliteApplicationStore,
        storage_dir: Path | str,
        max_file_size_bytes: int = 10 * 1024 * 1024,
        processor: Any | None = None,
        max_files_per_upload: int = 20,
    ) -> None:
        self.sqlite_store = sqlite_store
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_size_bytes = max_file_size_bytes
        self.processor = processor
        if max_files_per_upload < 1:
            raise ValueError("max_files_per_upload must be positive")
        self.max_files_per_upload = max_files_per_upload

    async def process_adr_uploads(
        self,
        repository_id: str,
        user_id: str,
        files: Sequence[ADRUploadFile],
    ) -> list[ADRUploadResult]:
        """Process a bounded batch independently, preserving each ADR's provenance.

        Repository authorization and batch shape are checked before any writes.
        Only one file's bytes are held at a time; ingestion uses the existing
        per-document pipeline. Successful items are not rolled back on failure.
        """
        self.validate_repository_access(repository_id, user_id)
        if not files or len(files) > self.max_files_per_upload:
            raise ValueError(f"Upload between 1 and {self.max_files_per_upload} ADR files.")

        results: list[ADRUploadResult] = []
        for index, upload in enumerate(files):
            filename = upload.filename or "uploaded_adr.txt"
            try:
                parts: list[bytes] = []
                size = 0
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > self.max_file_size_bytes:
                        raise FileSizeExceededError(
                            f"File size exceeds maximum allowed limit of {self.max_file_size_bytes} bytes."
                        )
                    parts.append(chunk)
                payload = b"".join(parts)
                del parts
                record, parsed_doc = await self.process_adr_upload(
                    repository_id=repository_id, user_id=user_id,
                    filename=filename, file_bytes=payload,
                )
                del parsed_doc
                results.append(ADRUploadResult(index, filename, "completed", 201, adr=record))
            except DuplicateADRError as exc:
                results.append(ADRUploadResult(index, filename, "duplicate", 409,
                                               adr=exc.existing_adr, error=str(exc)))
            except (InvalidFileTypeError, FileSizeExceededError) as exc:
                code = 413 if isinstance(exc, FileSizeExceededError) else 400
                results.append(ADRUploadResult(index, filename, "failed", code, error=str(exc)))
            except ADRParsingError:
                results.append(ADRUploadResult(index, filename, "failed", 422,
                    error="Failed to parse uploaded ADR document. Please verify the document format and content."))
            except Exception:
                logger.exception("ADR batch item failed repository=%s index=%s", repository_id, index)
                results.append(ADRUploadResult(index, filename, "failed", 500,
                    error="An error occurred while storing or processing the ADR."))
            finally:
                try:
                    await upload.close()
                except Exception:
                    logger.warning("Could not close ADR upload repository=%s index=%s", repository_id, index,
                                   exc_info=True)
                # Do not retain a previous document's bytes while reading the next.
                parts = []
                payload = b""
        logger.info("ADR batch completed repository=%s total=%s completed=%s", repository_id,
                    len(results), sum(item.status == "completed" for item in results))
        return results

    def validate_repository_access(self, repository_id: str, user_id: str) -> Any:
        """Verify repository exists and belongs to the authenticated user.

        Raises:
            RepositoryNotFoundError: if repository does not exist (404)
            RepositoryAccessDeniedError: if repository belongs to another user (403)
        """
        repo = self.sqlite_store.get_repository(repository_id)
        if not repo:
            logger.warning(f"ADR upload rejected: repository '{repository_id}' not found.")
            raise RepositoryNotFoundError(f"Repository '{repository_id}' was not found.")

        is_owner = (repo.user_id == user_id)
        has_connection = bool(
            repo.github_connection and repo.github_connection.user_id == user_id
        )
        if not (is_owner or has_connection):
            logger.warning(
                f"ADR upload forbidden: user '{user_id}' denied access to repository '{repository_id}' (owned by '{repo.user_id}')."
            )
            raise RepositoryAccessDeniedError(
                f"Access denied: You do not have permission to upload ADRs to repository '{repository_id}'."
            )

        return repo

    async def process_adr_upload(
        self,
        repository_id: str,
        user_id: str,
        filename: str,
        file_bytes: bytes,
    ) -> tuple[ADRModel, ADRDocument]:
        """Validate, store, persist, parse, and optionally ingest ADR into Neo4j graph."""
        logger.info(
            f"ADR upload initiated for repo '{repository_id}' by user '{user_id}' (file: '{filename}', size: {len(file_bytes)} bytes)"
        )

        # 1. Authorize repository access
        repo = self.validate_repository_access(repository_id, user_id)
        canonical_repo_id = repo.id

        # 2. Validate file name and extension
        if not filename or not filename.strip():
            raise InvalidFileTypeError("No filename provided.")

        clean_name = sanitize_filename(filename)
        ext = Path(clean_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            logger.warning(f"ADR upload rejected: unsupported extension '{ext}'. Allowed: {ALLOWED_EXTENSIONS}")
            raise InvalidFileTypeError(
                f"Unsupported file type '{ext}'. Supported formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # 3. Validate file size
        if len(file_bytes) > self.max_file_size_bytes:
            max_mb = self.max_file_size_bytes // (1024 * 1024)
            logger.warning(
                f"ADR upload rejected: file size ({len(file_bytes)} bytes) exceeds limit ({self.max_file_size_bytes} bytes)"
            )
            raise FileSizeExceededError(
                f"File size exceeds maximum allowed limit of {max_mb} MB."
            )

        if len(file_bytes) == 0:
            raise InvalidFileTypeError("Uploaded file is empty.")

        # 4. Compute content hash (SHA-256)
        content_hash = hashlib.sha256(file_bytes).hexdigest()

        # 5. Generate server-controlled collision-resistant storage path candidate
        provisional_adr_id = f"adr_{uuid.uuid4().hex[:12]}"
        repo_adr_dir = self.storage_dir / canonical_repo_id / provisional_adr_id
        repo_adr_dir.mkdir(parents=True, exist_ok=True)
        dest_file_path = repo_adr_dir / f"original{ext}"
        mime_type = detect_mime_type(ext)
        provisional_title = Path(clean_name).stem

        # 6. Atomically claim or create ADR record (protects against concurrent race condition and enables retry of FAILED uploads)
        adr_record, is_duplicate = self.sqlite_store.claim_or_create_adr(
            repository_id=canonical_repo_id,
            content_hash=content_hash,
            title=provisional_title,
            source_type="MANUAL_UPLOAD",
            source_name=clean_name,
            file_path=str(dest_file_path.as_posix()),
            file_size=len(file_bytes),
            mime_type=mime_type,
            file_extension=ext,
            adr_id=provisional_adr_id,
        )

        if is_duplicate:
            try:
                repo_adr_dir.rmdir()
            except Exception:
                pass
            logger.info(
                f"ADR upload duplicate detected in repo '{canonical_repo_id}' with hash '{content_hash[:8]}' (existing ID: '{adr_record.id}')"
            )
            raise DuplicateADRError(
                f"An identical ADR already exists for this repository (id='{adr_record.id}', title='{adr_record.title}').",
                existing_adr=adr_record,
            )

        adr_id = adr_record.id
        dest_file_path = Path(adr_record.file_path)
        dest_file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"ADR metadata record claimed/created: id='{adr_id}', status='PENDING'")

        # 8. Save file to disk safely
        try:
            with open(dest_file_path, "wb") as fp:
                fp.write(file_bytes)
            logger.info(f"ADR original file safely stored on disk: '{dest_file_path}'")
        except Exception as exc:
            logger.exception(f"Failed to write ADR file to disk for '{adr_id}': {exc}")
            # Clean up partial file if created
            if dest_file_path.exists():
                try:
                    dest_file_path.unlink()
                except Exception:
                    pass
            self.sqlite_store.update_adr_status(adr_id, status="FAILED", error=f"Filesystem write error: {exc}")
            raise ADRServiceError(f"Failed to store file on disk: {exc}") from exc

        # 9. Parse file into normalized text
        logger.info(f"Parsing ADR content for id='{adr_id}', format='{ext}'")
        try:
            parsed_doc = parse_adr_content(
                file_bytes=file_bytes,
                file_extension=ext,
                source_name=clean_name,
                adr_id=adr_id,
                repository_id=canonical_repo_id,
                file_path=str(dest_file_path.as_posix()),
                content_hash=content_hash,
                source_type="MANUAL_UPLOAD",
                mime_type=mime_type,
            )
        except Exception as exc:
            logger.warning(f"ADR parsing failed for '{adr_id}': {exc}")
            # Retain file on disk for diagnosis, but mark status as FAILED in DB
            self.sqlite_store.update_adr_status(
                adr_id=adr_id,
                status="FAILED",
                error=str(exc),
            )
            raise ADRParsingError(f"Failed to parse uploaded ADR document: {exc}") from exc

        # 10. Phase 2: Knowledge Graph Ingestion (if processor is configured)
        if self.processor:
            logger.info(f"Starting Phase 2 Neo4j knowledge graph ingestion for ADR '{adr_id}'")
            self.sqlite_store.update_adr_status(
                adr_id=adr_id,
                status="PROCESSING",
                title=parsed_doc.title,
                error=None,
            )
            try:
                await self.processor.process_adr(parsed_doc)
            except Exception as exc:
                logger.exception(f"Phase 2 ADR processing failed for '{adr_id}': {exc}")
                self.sqlite_store.update_adr_status(
                    adr_id=adr_id,
                    status="FAILED",
                    error=str(exc),
                )
                raise ADRServiceError(f"Knowledge graph ingestion failed: {exc}") from exc

        # 11. Mark COMPLETED with final title
        updated_record = self.sqlite_store.update_adr_status(
            adr_id=adr_id,
            status="COMPLETED",
            title=parsed_doc.title,
            error=None,
        )
        logger.info(
            f"ADR '{adr_id}' successfully completed all phases and marked COMPLETED (title='{parsed_doc.title}')"
        )
        return updated_record or adr_record, parsed_doc
