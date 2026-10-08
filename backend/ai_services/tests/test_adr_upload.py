from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def post_adr_batch(env, uploads, **kwargs):
    return env['client'].post(
        f"/repositories/{env['repo_alice_id']}/adrs",
        files=[('files', upload) for upload in uploads],
        headers={'Authorization': f"Bearer {env['alice_jwt']}"},
        **kwargs,
    )


def test_batch_upload_mixed_formats_infers_titles_without_metadata(test_env):
    response = post_adr_batch(test_env, [
        ('one.md', b'# One\nChoose SQLite.', 'text/markdown'),
        ('two.txt', b'Two\nChoose Kafka.', 'text/plain'),
        ('three.pdf', _create_minimal_pdf_bytes(), 'application/pdf'),
        ('four.docx', _create_minimal_docx_bytes(), 'application/octet-stream'),
    ])
    assert response.status_code == 201
    data = response.json()
    assert (data['total'], data['completed'], data['duplicates'], data['failed']) == (4, 4, 0, 0)
    assert [item['index'] for item in data['results']] == [0, 1, 2, 3]
    records = [item['adr'] for item in data['results']]
    assert [record['title'] for record in records] == ['One', 'Two', 'ADR 003: Architecture Decision', 'ADR 004: Event Bus Selection']
    assert all(record['description'] is None for record in records)
    assert len({record['id'] for record in records}) == 4
    for record in records:
        assert record['repository_id'] == test_env['repo_alice_id']
        assert 'file_path' not in record
        assert Path(test_env['store'].get_adr(record['id']).file_path).exists()


def test_batch_duplicate_and_invalid_item_do_not_stop_later_files(test_env):
    content = b'# ADR\nChoose SQLite.'
    response = post_adr_batch(test_env, [
        ('one.md', content, 'text/markdown'),
        ('copy.md', content, 'text/markdown'),
        ('bad.exe', b'unsupported', 'application/octet-stream'),
        ('last.txt', b'Another decision: use Kafka.', 'text/plain'),
    ])
    assert response.status_code == 207
    data = response.json()
    assert (data['completed'], data['duplicates'], data['failed']) == (2, 1, 1)
    assert [item['status_code'] for item in data['results']] == [201, 409, 400, 201]
    assert data['results'][0]['adr']['id'] == data['results'][1]['adr']['id']
    assert len(test_env['store'].list_adrs(test_env['repo_alice_id'])) == 2


def test_batch_size_parse_and_empty_errors_are_per_file(test_env):
    test_env['adr_service'].max_file_size_bytes = 100
    response = post_adr_batch(test_env, [
        ('large.md', b'x' * 101, 'text/markdown'),
        ('broken.pdf', b'not a PDF', 'application/pdf'),
        ('empty.txt', b'', 'text/plain'),
        ('good.md', b'# Good\nChoose SQLite.', 'text/markdown'),
    ])
    assert response.status_code == 207
    assert [item['status_code'] for item in response.json()['results']] == [413, 422, 400, 201]
    assert response.json()['completed'] == 1


def test_batch_count_limit_is_rejected_before_ingestion(test_env):
    test_env['adr_service'].max_files_per_upload = 1
    response = post_adr_batch(test_env, [
        ('one.md', b'# One', 'text/markdown'), ('two.md', b'# Two', 'text/markdown'),
    ])
    assert response.status_code == 400
    assert test_env['store'].list_adrs(test_env['repo_alice_id']) == []


@pytest.mark.parametrize('repo,token,code', [
    ('repo_1001', None, 401), ('repo_missing', 'alice_jwt', 404),
    ('repo_2001', 'alice_jwt', 403),
])
def test_batch_repository_authorization(test_env, repo, token, code):
    headers = {'Authorization': f"Bearer {test_env[token]}"} if token else {}
    response = test_env['client'].post(
        f'/repositories/{repo}/adrs',
        files=[('files', ('one.md', b'# One', 'text/markdown')),
               ('files', ('two.md', b'# Two', 'text/markdown'))], headers=headers,
    )
    assert response.status_code == code
    assert test_env['store'].list_adrs(test_env['repo_alice_id']) == []
    assert test_env['store'].list_adrs(test_env['repo_bob_id']) == []


def test_single_file_uses_files_field_and_batch_response(test_env):
    response = post_adr_batch(test_env, [('one.md', b'# One', 'text/markdown')])
    assert response.status_code == 201
    assert response.json()['total'] == response.json()['completed'] == 1
    assert response.json()['results'][0]['adr']['title'] == 'One'


def test_legacy_file_field_is_not_supported(test_env):
    response = test_env['client'].post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files={'file': ('legacy.md', b'# Legacy', 'text/markdown')},
        headers={'Authorization': f"Bearer {test_env['alice_jwt']}"},
    )
    assert response.status_code == 422
    assert test_env['store'].list_adrs(test_env['repo_alice_id']) == []


def test_removed_metadata_cannot_override_inferred_title_or_description(test_env):
    # FastAPI ignores extra form fields; none are forwarded into ingestion.
    response = post_adr_batch(test_env, [('inferred.md', b'# Inferred title', 'text/markdown')],
                             data={'title': 'Override', 'description': 'Not stored',
                                   'titles': ['Override'], 'descriptions': ['Not stored']})
    assert response.status_code == 201
    record = response.json()['results'][0]['adr']
    assert record['title'] == 'Inferred title' and record['description'] is None
    stored = test_env['store'].get_adr(record['id'])
    assert stored.title == 'Inferred title' and stored.description is None


def test_upload_service_has_no_title_or_description_override_parameters():
    from inspect import signature
    assert set(signature(ADRService.process_adr_uploads).parameters) == {
        'self', 'repository_id', 'user_id', 'files',
    }
    assert set(signature(ADRService.process_adr_upload).parameters) == {
        'self', 'repository_id', 'user_id', 'filename', 'file_bytes',
    }
    assert 'explicit_title' not in signature(parse_adr_content).parameters


def test_parser_preserves_automatic_filename_title_fallback():
    # Unstyled DOCX documents with a long first paragraph have no inferred title.
    document = parse_adr_content(
        file_bytes=_create_minimal_docx_bytes('x' * 160), file_extension='.docx',
        source_name='003-database-choice.docx', adr_id='adr_test', repository_id='repo_test',
        file_path='unused.docx', content_hash='test',
    )
    assert document.title == '003 Database Choice'


def test_batch_api_schema_documents_file_array_and_multi_status(test_env):
    schema = test_env['client'].get('/openapi.json').json()
    operation = schema['paths']['/repositories/{repo_id}/adrs']['post']
    assert '207' in operation['responses']
    body_ref = operation['requestBody']['content']['multipart/form-data']['schema']['$ref']
    properties = schema['components']['schemas'][body_ref.rsplit('/', 1)[1]]['properties']
    assert set(properties) == {'files'}
    assert properties['files']['type'] == 'array'
    assert schema['components']['schemas'][body_ref.rsplit('/', 1)[1]]['required'] == ['files']


def test_batch_processor_failure_is_sanitized_and_later_item_completes(test_env):
    from unittest.mock import AsyncMock
    processor = AsyncMock()
    processor.process_adr.side_effect = [RuntimeError('private-secret-detail'), None]
    test_env['adr_service'].processor = processor
    response = post_adr_batch(test_env, [
        ('failed.md', b'# Failed\nChoose SQLite.', 'text/markdown'),
        ('success.md', b'# Success\nChoose Kafka.', 'text/markdown'),
    ])
    assert response.status_code == 207
    assert [item['status_code'] for item in response.json()['results']] == [500, 201]
    assert 'private-secret-detail' not in response.text
    assert processor.process_adr.await_count == 2
    assert sorted(record.status for record in test_env['store'].list_adrs(test_env['repo_alice_id'])) == ['COMPLETED', 'FAILED']


@pytest.mark.asyncio
async def test_batch_reads_bounded_chunks_and_closes_uploads(test_env):
    from starlette.datastructures import UploadFile
    from unittest.mock import AsyncMock
    uploads = [UploadFile(io.BytesIO(b'# One'), filename='one.md'),
               UploadFile(io.BytesIO(b'# Two'), filename='two.md')]
    for upload in uploads:
        upload.read = AsyncMock(wraps=upload.read)
        upload.close = AsyncMock(wraps=upload.close)
    results = await test_env['adr_service'].process_adr_uploads(
        test_env['repo_alice_id'], 'usr_alice', uploads,
    )
    assert [item.status for item in results] == ['completed', 'completed']
    for upload in uploads:
        assert all(call.args == (64 * 1024,) for call in upload.read.call_args_list)
        upload.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_read_failure_is_isolated_and_file_is_closed(test_env):
    from starlette.datastructures import UploadFile
    from unittest.mock import AsyncMock
    broken = UploadFile(io.BytesIO(b'# Broken'), filename='broken.md')
    broken.read = AsyncMock(side_effect=OSError('private read error'))
    broken.close = AsyncMock(wraps=broken.close)
    good = UploadFile(io.BytesIO(b'# Good'), filename='good.md')
    results = await test_env['adr_service'].process_adr_uploads(
        test_env['repo_alice_id'], 'usr_alice', [broken, good],
    )
    assert [item.status_code for item in results] == [500, 201]
    assert 'private read error' not in results[0].error
    broken.close.assert_awaited_once()


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
        # This suite isolates ADR processing; live App authorization is covered separately.
        app.state.github_app = SimpleNamespace(authorize_tracked=AsyncMock(
            side_effect=lambda user_id, identifier: store.get_repository(identifier)))

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
    files = {"files": ("001-test.md", b"# Test ADR", "text/markdown")}
    resp = client.post(f"/repositories/{test_env['repo_alice_id']}/adrs", files=files)
    assert resp.status_code == 401


# =========================================================================
# 2. Invalid JWT -> Rejected (401)
# =========================================================================
def test_upload_adr_invalid_jwt_rejected(test_env):
    client = test_env["client"]
    files = {"files": ("001-test.md", b"# Test ADR", "text/markdown")}
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
    files = {"files": ("001-test.md", b"# Test ADR", "text/markdown")}
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
    files = {"files": ("001-test.md", b"# Test ADR", "text/markdown")}
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
    files = {"files": ("malicious.exe", b"binary content", "application/octet-stream")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 207
    assert resp.json()["results"][0]["status_code"] == 400
    assert "unsupported file type" in resp.json()["results"][0]["error"].lower()


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
        files = {"files": ("large.md", large_content, "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 207
        assert resp.json()["results"][0]["status_code"] == 413
        assert "exceeds" in resp.json()["results"][0]["error"].lower()
    finally:
        adr_service.max_file_size_bytes = original_limit


# =========================================================================
# 7. Valid Markdown ADR -> 201, Saved, Parsed
# =========================================================================
def test_upload_adr_valid_markdown(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    md_content = b"# ADR 001: Use SQLite for Prototype\n\n## Context\nRapid local iteration.\n\n## Decision\nWe choose SQLite."
    files = {"files": ("001-sqlite.md", md_content, "text/markdown")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()["results"][0]["adr"]
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
    files = {"files": ("decision-grpc.txt", txt_content, "text/plain")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()["results"][0]["adr"]
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
    files = {"files": ("adr-003.pdf", pdf_bytes, "application/pdf")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()["results"][0]["adr"]
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
        "files": (
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
    data = resp.json()["results"][0]["adr"]
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

    files = {"files": ("hash-test.md", content, "text/markdown")}
    resp = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()["results"][0]["adr"]
    assert data["content_hash"] == expected_hash


# =========================================================================
# 12. Duplicate Upload for Same Repository -> Deterministic Duplicate (409)
# =========================================================================
def test_upload_adr_duplicate_detection(test_env):
    client = test_env["client"]
    headers = {"Authorization": f"Bearer {test_env['alice_jwt']}"}
    content = b"# ADR 005: Cache Layer\nUse Redis for low-latency session caching."

    files1 = {"files": ("adr-005.md", content, "text/markdown")}
    resp1 = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files1,
        headers=headers,
    )
    assert resp1.status_code == 201

    # Second upload with identical content to the same repository
    files2 = {"files": ("adr-005-copy.md", content, "text/markdown")}
    resp2 = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files=files2,
        headers=headers,
    )
    assert resp2.status_code == 207
    assert resp2.json()["results"][0]["status_code"] == 409
    assert "identical adr already exists" in resp2.json()["results"][0]["error"].lower()

    # But uploading the SAME file content to a DIFFERENT repository (by Bob) succeeds
    headers_bob = {"Authorization": f"Bearer {test_env['bob_jwt']}"}
    files_bob = {"files": ("adr-005.md", content, "text/markdown")}
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
        files = {"files": (traversal_name, content + traversal_name.encode(), "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()["results"][0]["adr"]
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
        files = {"files": ("bad-doc.md", b"# Malformed Document", "text/markdown")}
        resp = client.post(
            f"/repositories/{test_env['repo_alice_id']}/adrs",
            files=files,
            headers=headers,
        )
        assert resp.status_code == 207
        assert resp.json()["results"][0]["status_code"] == 422
        detail = resp.json()["results"][0]["error"].lower()
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
        files={"files": ("alice-secret.md", b"# Alice Secret ADR", "text/markdown")},
        headers=headers_alice,
    )
    assert resp_alice.status_code == 201

    # Bob cannot upload to Alice's repo
    resp_bob_upload = client.post(
        f"/repositories/{test_env['repo_alice_id']}/adrs",
        files={"files": ("bob-intruder.md", b"# Bob Intrusion", "text/markdown")},
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
            files={"files": ("retried-adr.md", content, "text/markdown")},
            headers=headers,
        )
        assert resp1.status_code == 207
        assert resp1.json()["results"][0]["status_code"] == 422

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
        files={"files": ("retried-adr.md", content, "text/markdown")},
        headers=headers,
    )
    assert resp2.status_code == 201
    data2 = resp2.json()["results"][0]["adr"]
    assert data2["status"] == "COMPLETED"
    assert data2["title"] == "Retried ADR"
    assert data2["id"] == first_adr_id  # Reclaimed existing record

    # Verify updated DB state
    updated_adr = store.get_adr(first_adr_id)
    assert updated_adr is not None
    assert updated_adr.status == "COMPLETED"
    assert updated_adr.error is None
