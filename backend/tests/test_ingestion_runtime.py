"""Regression coverage for runtime-only bootstrap and index serialization failures."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_settings(monkeypatch):
    """Allow regression tests to run without any local credentials or services."""
    from app.config import get_settings

    values = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1/test",
        "JWT_SECRET_KEY": "test-only-key-at-least-32-bytes-long",
        "JWT_ISSUER": "https://test.invalid",
        "ADMIN_BOOTSTRAP_PASSWORD": "test-only-bootstrap",
        "DASHSCOPE_API_KEY": "test-only-key",
        "WE_COM_CORP_ID": "test-corp",
        "WE_COM_AGENT_ID": "100001",
        "WE_COM_SECRET": "test-only-secret",
        "WE_COM_TOKEN": "test-only-token",
        "WE_COM_ENCODING_AES_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "WE_COM_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "RABBITMQ_URL": "amqp://test:test@127.0.0.1/",
        "REDIS_PASSWORD": "test-only-password",
        "S3_ACCESS_KEY": "test-only-access",
        "S3_SECRET_KEY": "test-only-secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSession:
    """Provide transactional sessions while preserving real UUID model values."""

    def __init__(self, *, gets=(), scalars=()):
        """Keep deterministic database results and captured index insert values."""
        self.gets = iter(gets)
        self.scalar_results = iter(scalars)
        self.execute = AsyncMock()
        self.added = []
        self.scalar_queries = []

    async def __aenter__(self):
        """Enter a session or transaction context."""
        return self

    async def __aexit__(self, *args):
        """Leave a session or transaction context."""

    def begin(self):
        """Return a transaction context."""
        return self

    async def get(self, *args):
        """Return preconfigured source records."""
        return next(self.gets)

    async def scalar(self, *args):
        """Return preconfigured locked records."""
        self.scalar_queries.append(args[0])
        return next(self.scalar_results)

    async def scalars(self, *args):
        """Expose no existing index versions."""
        return SimpleNamespace(all=lambda: [])

    def add(self, item):
        """Capture created index records."""
        self.added.append(item)

    async def flush(self):
        """Simulate successful index version insertion."""

    async def commit(self):
        """Finish the source read transaction."""


@pytest.mark.asyncio
async def test_process_job_serializes_uuid_revision_and_completes(monkeypatch):
    """A real UUID revision must reach the chunk INSERT and completed state."""
    from app import worker

    tenant, job_id, revision_id, document_id = uuid4(), uuid4(), uuid4(), uuid4()
    job = SimpleNamespace(worker_id="test-worker", revision_id=revision_id, document_id=document_id)
    revision = SimpleNamespace(document_id=document_id, object_key="test/source.md", filename="test.md",
                               media_type="text/markdown", clearance_level=0)
    document = SimpleNamespace(id=document_id, kb_id=uuid4(), clearance_level=0)
    source = FakeSession(gets=[job, revision, document])
    target = FakeSession(scalars=[job, document])
    sessions = iter([source, target])
    monkeypatch.setattr(worker, "SessionFactory", lambda: next(sessions))
    monkeypatch.setattr(worker, "set_tenant_rls_context", AsyncMock())
    monkeypatch.setattr(worker, "_set_stage", AsyncMock())
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(max_upload_bytes=4096, max_document_chunks=100,
                                                                     embedding_model="test-embedding"))

    @asynccontextmanager
    async def store(settings):
        """Supply a fictional source without contacting an external service."""
        yield SimpleNamespace(get=AsyncMock(return_value=(b"# Test\n\nFictional example.", "text/markdown")))

    monkeypatch.setattr(worker, "connected_object_store", store)
    monkeypatch.setattr(worker, "embed_texts", AsyncMock(return_value=[[0.25] * 1024]))
    await worker.process_job(str(job_id), str(tenant), "test-worker")
    rows = target.execute.call_args.args[1]
    assert json.loads(rows[0]["metadata_json"])["revision_id"] == str(revision_id)
    assert job.status == "completed"
    assert target.added[0].status == "active"
    assert document.active_revision_id == revision_id


@pytest.mark.asyncio
async def test_admin_bootstrap_only_reads_tenant_after_advisory_lock(monkeypatch):
    """Bootstrap works with SELECT-only tenant permissions and retains serialization."""
    from app.api import admin_auth

    session = FakeSession(scalars=[SimpleNamespace(id=uuid4()), uuid4()])
    monkeypatch.setattr(admin_auth, "set_tenant_rls_context", AsyncMock())
    await admin_auth.initialize_first_admin(lambda: session)
    assert "pg_advisory_xact_lock" in str(session.execute.call_args.args[0])
    assert "FOR UPDATE" not in str(session.scalar_queries[0])
