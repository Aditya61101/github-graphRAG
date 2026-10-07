from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from ai_services.ingestion.adr.parser import ADRParseError, parse_adr_content
from ai_services.ingestion.adr.service import (
    ADRService,
    DuplicateADRError,
    FileSizeExceededError,
    InvalidFileTypeError,
    RepositoryAccessDeniedError,
    RepositoryNotFoundError,
    sanitize_filename,
)
from ai_services.ingestion.persistence.models import (
    ADRModel,
    Base,
    GitHubConnectionModel,
    RepositoryModel,
    UserModel,
)
from ai_services.ingestion.persistence.sqlite_store import SqliteApplicationStore
from api_services.app.routers.repositories import router as repositories_router
from api_services.app.utils.jwt_utils import create_access_token


def _create_minimal_pdf_bytes(title: str = "ADR 003: Architecture Decision") -> bytes:
    """Generate minimal valid PDF binary with an embedded text stream."""
    stream_content = f"BT\n/F1 12 Tf\n100 700 Td\n({title}) Tj\nET\n".encode("latin-1")
    stream_len = len(stream_content)
    header = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    )
    obj5_header = f"5 0 obj << /Length {stream_len} >> stream\n".encode("latin-1")
    footer = (
        b"endstream\nendobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000244 00000 n \n"
        b"0000000325 00000 n \n"
        b"trailer << /Size 6 /Root 1 0 R >>\n"
        b"startxref\n431\n%%EOF\n"
    )
    return header + obj5_header + stream_content + footer


def _create_minimal_docx_bytes(title: str = "ADR 004: Event Bus Selection") -> bytes:
    """Generate minimal valid DOCX binary archive with document.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
            "  <w:body>\n"
            f"    <w:p><w:r><w:t>{title}</w:t></w:r></w:p>\n"
            "    <w:p><w:r><w:t>Context: High-throughput async messaging.</w:t></w:r></w:p>\n"
            "    <w:p><w:r><w:t>Decision: Use Apache Kafka.</w:t></w:r></w:p>\n"
            "  </w:body>\n"
            "</w:document>"
        )
        zf.writestr("word/document.xml", xml_content)
    return buf.getvalue()


@pytest.fixture
def test_env():
    """Create isolated SQLite database, storage directory, test users and repositories."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        storage_path = temp_path / "adrs"
        storage_path.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)

        store = SqliteApplicationStore(session_factory=session_factory)

        # Seed Test Users
        with session_factory() as session:
            alice = UserModel(id="usr_alice", username="alice", email="alice@example.com")
            bob = UserModel(id="usr_bob", username="bob", email="bob@example.com")
            session.add_all([alice, bob])

            conn_alice = GitHubConnectionModel(
                id="conn_alice",
                user_id="usr_alice",
                github_user_id="1111",
                access_token="ghp_alice_token",
            )
            conn_bob = GitHubConnectionModel(
                id="conn_bob",
                user_id="usr_bob",
                github_user_id="2222",
                access_token="ghp_bob_token",
            )
            session.add_all([conn_alice, conn_bob])

            # Seed Test Repositories
            repo_alice = RepositoryModel(
                id="repo_1001",
                github_repository_id="1001",
                owner="alice",
                name="project-a",
                full_name="alice/project-a",
                repository_url="https://github.com/alice/project-a",
                default_branch="main",
                tracked_branch="main",
                user_id="usr_alice",
                github_connection_id="conn_alice",
                status="COMPLETED",
            )
            repo_bob = RepositoryModel(
                id="repo_2001",
                github_repository_id="2001",
                owner="bob",
                name="project-b",
                full_name="bob/project-b",
                repository_url="https://github.com/bob/project-b",
                default_branch="main",
                tracked_branch="main",
                user_id="usr_bob",
                github_connection_id="conn_bob",
                status="COMPLETED",
            )
            session.add_all([repo_alice, repo_bob])
            session.commit()

        adr_service = ADRService(
            sqlite_store=store,
            storage_dir=storage_path,
            max_file_size_bytes=1024 * 1024,  # 1 MB
        )

        app = FastAPI()
        app.include_router(repositories_router, prefix="/repositories")
        app.state.sqlite_store = store
        app.state.adr_service = adr_service

        client = TestClient(app)

        alice_jwt = create_access_token({"sub": "usr_alice"})
        bob_jwt = create_access_token({"sub": "usr_bob"})

        try:
            yield {
                "client": client,
                "store": store,
                "adr_service": adr_service,
                "storage_path": storage_path,
                "alice_jwt": alice_jwt,
                "bob_jwt": bob_jwt,
                "repo_alice_id": "repo_1001",
                "repo_bob_id": "repo_2001",
            }
        finally:
            client.close()
            engine.dispose()


# =========================================================================
# 1. No JWT -> Rejected (401)
# =========================================================================
def test_upload_adr_without_jwt_rejected(test_env):
    client = test_env["client"]
    files = {"file": ("001-test.md", b"# Test ADR", "text/markdown")}
    resp = client.post(f"/repositories/{test_env['repo_alice_id']}/adrs", files=files)
    assert resp.status_code == 401


# =========================================================================
# 2. Invalid JWT -> Rejected (401)
# =========================================================================
def test_upload_adr_invalid_jwt_rejected(test_env):
    client = test_env["client"]
    files = {"file": ("001-test.md", b"# Test ADR", "text/markdown")}
    headers = {"Authorization": "Bearer invalid.jwt.token"}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 401


# =========================================================================
# 3. Repository Does Not Exist -> 404
# =========================================================================
def test_upload_adr_repo_not_found(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    files = {"file": ("001-test.md", b"# Test ADR", "text/markdown")}
    resp = client.post("/repositories/repo_nonexistent/adrs", files=files, headers=headers)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# =========================================================================
# 4. Repository Belongs to Another User -> 403
# =========================================================================
def test_upload_adr_forbidden_different_user(test_env):
    client = test_env["client"]
    # Alice attempts to upload to Bob's repository
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    files = {"file": ("001-test.md", b"# Test ADR", "text/markdown")}
    resp = client.post(
        f"/repositories/{test_env['repo_bob_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 403
    assert "access denied" in resp.json()["detail"].lower()


# =========================================================================
# 5. Unsupported File Extension -> Rejected (400)
# =========================================================================
def test_upload_adr_unsupported_file_extension(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    files = {"file": ("malicious.exe", b"binary content", "application/octet-stream")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 400
    assert "unsupported file type" in resp.json()["detail"].lower()


# =========================================================================
# 6. File Exceeds Configured Size -> Rejected (413)
# =========================================================================
def test_upload_adr_file_exceeds_size(test_env):
    client = test_env["client"]
    adr_service = test_env["adr_service"]
    # Temporarily set max file size to 50 bytes
    original_limit = adr_service.max_file_size_bytes
    adr_service.max_file_size_bytes = 50
    try:
        headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
        large_content = b"A" * 100
        files = {"file": ("large.md", large_content, "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 413
        assert "exceeds" in resp.json()["detail"].lower()
    finally:
        adr_service.max_file_size_bytes = original_limit


# =========================================================================
# 7. Valid Markdown ADR -> 201, Saved, Parsed
# =========================================================================
def test_upload_adr_valid_markdown(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    md_content = b"# ADR 001: Use SQLite for Prototype\n\n## Context\nRapid local iteration.\n\n## Decision\nWe choose SQLite."
    files = {"file": ("001-sqlite.md", md_content, "text/markdown")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert data["title"] == "ADR 001: Use SQLite for Prototype"
    assert data["source_type"] == "MANUAL_UPLOAD"
    assert data["source_name"] == "001-sqlite.md"
    assert data["file_extension"] == ".md"
    assert "file_path" not in data  # file_path is internal metadata, not exposed

    # Verify DB record and file saved on local disk under storage path
    store = test_env["store"]
    db_adr = store.get_adr(data["id"])
    assert db_adr is not None
    assert db_adr.status == "COMPLETED"
    assert db_adr.title == "ADR 001: Use SQLite for Prototype"

    saved_file = Path(db_adr.file_path)
    assert saved_file.exists()
    assert saved_file.read_bytes() == md_content


# =========================================================================
# 8. Valid TXT ADR -> 201, Parsed Successfully
# =========================================================================
def test_upload_adr_valid_txt(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    txt_content = b"ADR 002: Service Communication\n\nContext: Distributed system.\nDecision: Use gRPC."
    files = {"file": ("decision-grpc.txt", txt_content, "text/plain")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert data["title"] == "ADR 002: Service Communication"
    assert data["file_extension"] == ".txt"


# =========================================================================
# 9. Valid PDF ADR -> 201, Parsed Successfully
# =========================================================================
def test_upload_adr_valid_pdf(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    pdf_bytes = _create_minimal_pdf_bytes("ADR 003: Architecture Decision")
    files = {"file": ("adr-003.pdf", pdf_bytes, "application/pdf")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert "ADR 003: Architecture Decision" in data["title"]
    assert data["mime_type"] == "application/pdf"


# =========================================================================
# 10. Valid DOCX ADR -> 201, Parsed Successfully
# =========================================================================
def test_upload_adr_valid_docx(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    docx_bytes = _create_minimal_docx_bytes("ADR 004: Event Bus Selection")
    files = {
        "file": (
            "adr-004.docx",
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert data["title"] == "ADR 004: Event Bus Selection"
    assert data["file_extension"] == ".docx"


# =========================================================================
# 11. Content Hash is Generated Correctly (SHA-256)
# =========================================================================
def test_upload_adr_content_hash_verified(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    content = b"# ADR Hash Test\nSpecific content to verify hash generation."
    expected_hash = hashlib.sha256(content).hexdigest()

    files = {"file": ("hash-test.md", content, "text/markdown")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content_hash"] == expected_hash


# =========================================================================
# 12. Duplicate Upload for Same Repository -> Deterministic Duplicate (409)
# =========================================================================
def test_upload_adr_duplicate_detection(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    content = b"# ADR 005: Cache Layer\nUse Redis for low-latency session caching."

    files1 = {"file": ("adr-005.md", content, "text/markdown")}
    resp1 = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files1,
        headers=headers,
    )
    assert resp1.status_code == 201

    # Second upload with identical content to the same repository
    files2 = {"file": ("adr-005-copy.md", content, "text/markdown")}
    resp2 = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files2,
        headers=headers,
    )
    assert resp2.status_code == 409
    assert "identical adr already exists" in resp2.json()["detail"].lower()

    # But uploading the SAME file content to a DIFFERENT repository (by Bob) succeeds
    headers_bob = {"Authorization": f"Bearer {test_env['bob_jwt']}"}
    files_bob = {"file": ("adr-005.md", content, "text/markdown")}
    resp_bob = client.post(
        f"/repositories/{test_env['repo_bob_id']}/adrs",
        files=files_bob,
        headers=headers_bob,
    )
    assert resp_bob.status_code == 201


# =========================================================================
# 13. Path Traversal Filename -> Safely Stored
# =========================================================================
def test_upload_adr_path_traversal_prevention(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    content = b"# Dangerous Filename ADR\nCheck storage safety."

    # Malicious filenames attempting directory traversal
    for traversal_name in [
        "../../../../etc/passwd.md",
        "..\\..\\windows\\system32\\cmd.md",
        "/absolute/path/override.md",
    ]:
        files = {"file": (traversal_name, content + traversal_name.encode(), "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "file_path" not in data

        db_adr = test_env["store"].get_adr(data["id"])
        assert db_adr is not None
        saved_file = Path(db_adr.file_path).resolve()
        storage_root = test_env["storage_path"].resolve()

        # Must be strictly within storage root
        assert storage_root in saved_file.parents
        # Traversal sequence should not be in the final stored path or metadata
        assert ".." not in str(saved_file)


# =========================================================================
# 14. Parsing Failure -> ADR Marked FAILED (422)
# =========================================================================
def test_upload_adr_parsing_failure_handling(test_env):
    client = test_env["client"]
    store = test_env["store"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}

    # Simulate parser raising ADRParseError
    with patch(
        "ai_services.ingestion.adr.service.parse_adr_content",
        side_effect=ADRParseError("Malformed document syntax"),
    ):
        files = {"file": ("bad-doc.md", b"# Malformed Document", "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"].lower()
        assert "parse" in detail and "failed" in detail

    # Verify ADR was recorded in DB and marked FAILED
    adrs = store.list_adrs(test_env["repo_alice_id"])
    failed_adr = next((a for a in adrs if a.source_name == "bad-doc.md"), None)
    assert failed_adr is not None
    assert failed_adr.status == "FAILED"
    assert "Malformed document syntax" in failed_adr.error

    # Verify file is retained on disk for debugging
    assert Path(failed_adr.file_path).exists()


# =========================================================================
# 15. Repository Isolation -> Strict User Access Verification
# =========================================================================
def test_upload_adr_repository_isolation(test_env):
    client = test_env["client"]
    headers_alice = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    headers_bob = {"Authorization": f"Bearer {test_env['bob_jwt']}"}

    # Alice uploads ADR to Alice's repo
    resp_alice = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files={"file": ("alice-secret.md", b"# Alice Secret ADR", "text/markdown")},
        headers=headers_alice,
    )
    assert resp_alice.status_code == 201

    # Bob cannot upload to Alice's repo
    resp_bob_upload = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files={"file": ("bob-intruder.md", b"# Bob Intrusion", "text/markdown")},
        headers=headers_bob,
    )
    assert resp_bob_upload.status_code == 403

    # Bob cannot list ADRs from Alice's repo
    resp_bob_list = client.get(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        headers=headers_bob,
    )
    assert resp_bob_list.status_code == 403

    # Alice can list ADRs from Alice's repo
    resp_alice_list = client.get(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        headers=headers_alice,
    )
    assert resp_alice_list.status_code == 200
    assert len(resp_alice_list.json()) >= 1
    assert resp_alice_list.json()[0]["title"] == "Alice Secret ADR"


# =========================================================================
# 16. File Cleanup on Disk Write Failure
# =========================================================================
def test_upload_adr_file_cleanup_on_write_failure(test_env):
    adr_service = test_env["adr_service"]
    store = test_env["store"]

    # Mock open() to simulate an I/O error during disk writing
    with patch("ai_services.ingestion.adr.service.open", side_effect=IOError("Disk write simulated failure")):
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                adr_service.process_adr_upload(
                    repository_id=test_env["repo_alice_id"],
                    user_id="usr_alice",
                    filename="disk-fail.md",
                    file_bytes=b"# Test disk fail",
                )
            )
        assert "disk" in str(exc_info.value).lower()

    # Verify ADR record in DB is marked FAILED
    adrs = store.list_adrs(test_env["repo_alice_id"])
    failed_adr = next((a for a in adrs if a.source_name == "disk-fail.md"), None)
    assert failed_adr is not None
    assert failed_adr.status == "FAILED"
    assert "filesystem write error" in failed_adr.error.lower()


# =========================================================================
# 17. Failed Upload is Retryable (Not Blocked as Duplicate)
# =========================================================================
def test_upload_adr_retry_failed_upload(test_env):
    """If an upload previously failed, uploading the same content again should succeed rather than conflict."""
    client = test_env["client"]
    store = test_env["store"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    content = b"# Retried ADR\n\n## Context\nTest retry on failure.\n\n## Decision\nIt works."

    # 1. First attempt fails due to temporary parser error
    with patch(
        "ai_services.ingestion.adr.service.parse_adr_content",
        side_effect=ADRParseError("Temporary parsing failure"),
    ):
        resp1 = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files={"file": ("retried-adr.md", content, "text/markdown")},
            headers=headers,
        )
        assert resp1.status_code == 422

    # Verify status in database is FAILED
    content_hash = hashlib.sha256(content).hexdigest()
    failed_adr = store.get_adr_by_hash(test_env["repo_alice_id"], content_hash)
    assert failed_adr is not None
    assert failed_adr.status == "FAILED"
    first_adr_id = failed_adr.id

    # 2. Second attempt with exact same content should NOT return 409 Duplicate
    # It should reclaim/retry the record and transition to COMPLETED
    resp2 = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files={"file": ("retried-adr.md", content, "text/markdown")},
        headers=headers,
    )
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["status"] == "COMPLETED"
    assert data2["title"] == "Retried ADR"
    assert data2["id"] == first_adr_id  # Reclaimed existing record

    # Verify updated DB state
    updated_adr = store.get_adr(first_adr_id)
    assert updated_adr is not None
    assert updated_adr.status == "COMPLETED"
    assert updated_adr.error is None

