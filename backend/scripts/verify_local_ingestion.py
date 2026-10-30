"""Verify real local HTTP upload, Worker indexing, PostgreSQL and S3 without logging secrets."""

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionFactory, engine, set_tenant_rls_context
from app.integrations import connected_object_store


def log(message: str) -> None:
    """Print only explicitly selected, non-secret acceptance evidence."""
    stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


async def verify(output: Path) -> None:
    """Exercise the running API and independently inspect persisted results."""
    settings = get_settings()
    local = dotenv_values(".env")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30, trust_env=False) as client:
        response = await client.get("/health/live")
        assert response.status_code == 200, "API health failed"
        log("PASS API health HTTP 200")
        password = settings.admin_bootstrap_password.get_secret_value()
        response = await client.post("/api/v1/auth/admin/login", json={"username": "admin", "password": password})
        if response.status_code == 401:
            password = local["ADMIN_E2E_PASSWORD"]
            response = await client.post("/api/v1/auth/admin/login", json={"username": "admin", "password": password})
        assert response.status_code == 200, f"Admin login HTTP {response.status_code}"
        login = response.json()
        client.headers["Authorization"] = f"Bearer {login['access_token']}"
        log(f"PASS admin login HTTP 200; token withheld; first_login={login['user']['is_first_login']}")
        if login["user"]["is_first_login"]:
            blocked = await client.get("/api/v1/admin/dashboard")
            assert blocked.status_code == 403, "First-login gate failed"
            log("PASS first-login management gate HTTP 403")
            response = await client.post("/api/v1/auth/admin/password", json={"current_password": password, "new_password": local["ADMIN_E2E_PASSWORD"]})
            assert response.status_code == 204, f"Change password HTTP {response.status_code}"
            response = await client.post("/api/v1/auth/admin/login", json={"username": "admin", "password": local["ADMIN_E2E_PASSWORD"]})
            assert response.status_code == 200, "Login after password change failed"
            assert not response.json()["user"]["is_first_login"]
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
            log("PASS password change HTTP 204; fresh login HTTP 200")
        response = await client.get("/api/v1/admin/dashboard")
        assert response.status_code == 200, "Dashboard aggregate failed"
        log(f"PASS dashboard aggregate HTTP 200; documents={response.json()['document_total']}")
        content = (f"# Fictional local acceptance\n\nRun: {uuid4()}\n\n"
                   "The fictional Aurora demo team stores sample onboarding guides here. "
                   "All names and procedures are invented for testing.\n").encode()
        output.mkdir(parents=True, exist_ok=True)
        (output / "test.md").write_bytes(content)
        response = await client.post("/api/v1/admin/documents", files={"file": ("test.md", content, "text/markdown")})
        assert response.status_code == 202, f"Upload HTTP {response.status_code}"
        uploaded = response.json()
        log(f"PASS upload HTTP 202; document_id={uploaded['document_id']}; job_id={uploaded['job_id']}; status={uploaded['status']}; duplicate={uploaded['duplicate']}")
        (output / "last-upload.json").write_text(json.dumps(uploaded, indent=2), encoding="utf-8")
        last = None
        for _ in range(180):
            async with SessionFactory() as session:
                await set_tenant_rls_context(session, tenant_id=settings.we_com_tenant_id)
                job = (await session.execute(text("SELECT status, attempt_count, error_code FROM ingestion_jobs WHERE id=:id"), {"id": UUID(uploaded["job_id"])})).mappings().one()
                if job["status"] != last:
                    log(f"DB ingestion_jobs status={job['status']}; attempts={job['attempt_count']}; error_code={job['error_code']}")
                    last = job["status"]
                assert job["status"] != "failed", f"Worker failed: {job['error_code']}"
                if job["status"] == "completed":
                    break
            await asyncio.sleep(1)
        else:
            raise AssertionError("Worker completion timed out")
        async with SessionFactory() as session:
            await set_tenant_rls_context(session, tenant_id=settings.we_com_tenant_id)
            params = {"id": UUID(uploaded["document_id"])}
            row = (await session.execute(text("SELECT d.status, d.active_revision_id, r.object_key, r.sha256 FROM documents d JOIN document_revisions r ON r.document_id=d.id AND r.id=d.active_revision_id WHERE d.id=:id"), params)).mappings().one()
            counts = dict((await session.execute(text("SELECT count(*) AS chunks, count(embedding) AS vectors, min(vector_dims(embedding)) AS min_dimensions, max(vector_dims(embedding)) AS max_dimensions, count(*) FILTER (WHERE search_vector IS NOT NULL AND search_vector != ''::tsvector) AS fts_rows, count(*) FILTER (WHERE vector_norm(embedding)>0) AS nonzero_vectors FROM chunks WHERE document_id=:id"), params)).mappings().one())
            assert row["status"] == "active" and row["active_revision_id"]
            assert counts["chunks"] > 0 and counts["chunks"] == counts["vectors"] == counts["fts_rows"] == counts["nonzero_vectors"]
            assert counts["min_dimensions"] == counts["max_dimensions"] == 1024
            version = (await session.execute(text("SELECT status, embedding_model, embedding_dimension, chunk_count FROM index_versions WHERE document_id=:id AND status='active'"), params)).mappings().one()
            log("PASS DB documents row exists; active revision set")
            log("PASS DB chunks " + json.dumps(counts))
            log("PASS DB index_versions " + json.dumps(dict(version)))
            async with connected_object_store(settings) as store:
                stored, _ = await store.get(row["object_key"])
                assert stored == content and hashlib.sha256(stored).hexdigest() == row["sha256"]
                log(f"PASS S3 source GET; bytes={len(stored)}; SHA256 matches uploaded file and DB revision")
        response = await client.get("/api/v1/admin/documents?limit=50")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(item["id"] == uploaded["document_id"] and item["ingestion_status"] == "completed" for item in items)
        log("PASS management document list reports completed")
        log("SUCCESS real HTTP -> S3 -> PostgreSQL job -> Worker -> Model Studio embedding -> pgvector/FTS; WeCom skipped")


async def main() -> None:
    """Run the check and always dispose database connections."""
    try:
        await verify(Path(sys.argv[1]))
    except Exception as exc:
        log(f"FAIL {type(exc).__name__}: {str(exc) if isinstance(exc, AssertionError) else 'details withheld; inspect sanitized diagnostics'}")
        raise SystemExit(1) from None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
