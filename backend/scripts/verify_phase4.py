"""Phase 4 定向联调：合成语音、真实 ASR/RAG、隔离数据库和管理员诊断 API。"""

import argparse
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from dotenv import dotenv_values
from fastapi import FastAPI
import httpx
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.admin_qa import router as qa_router
from app.api.admin_management import router as management_router
from app.auth.tokens import create_access_token
from app.config import get_settings
from app.db import SessionFactory, engine, set_tenant_rls_context
from app.models import AdminAuditEvent, AdminUser, Tenant, User, WeComInboundMessage as Message
from app.rag_worker import claim_message, now, process_message, purge_message_content
from scripts import verify_phase3 as fixtures


async def verify(audio_path: Path, output: Path):
    settings = get_settings()
    original_tenant = settings.we_com_tenant_id
    local = dotenv_values("../TEST/.env")
    runtime = make_url(settings.database_url)
    assert runtime.host in {"127.0.0.1", "localhost"} and runtime.database == local["POSTGRES_DB"]
    owner = create_async_engine(runtime.set(username=local["POSTGRES_USER"], password=local["POSTGRES_PASSWORD"]), hide_parameters=True)
    fixtures.OwnerSession = async_sessionmaker(owner, expire_on_commit=False)
    tenant, foreign = uuid4(), uuid4()
    report = {"checks": [], "real_wecom_delivery": False}

    def passed(name):
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        _, users = await fixtures.seed(tenant, [{"key":"expense", "title":"Fictional Aurora expense policy", "level":0,
            "content":"Fictional Aurora Company expense policy: Employees must submit expense claims within 30 days of incurring an expense. Transport reimbursement is limited to 200 yuan per claim."}])
        admin_id, foreign_user_id, foreign_message = uuid4(), uuid4(), uuid4()
        async with fixtures.OwnerSession() as session, session.begin():
            session.add(AdminUser(id=admin_id, tenant_id=tenant, username="phase4_fixture_admin",
                                  password_hash="fixture-not-used", must_change_password=False))
            session.add(Tenant(id=foreign, name="Phase 4 foreign fixture", wecom_corp_id=f"fixture-{foreign}"))
            await session.flush()
            session.add(User(id=foreign_user_id, tenant_id=foreign, external_id="foreign", display_name="Other fixture employee"))
            await session.flush()
            session.add(Message(id=foreign_message, tenant_id=foreign, user_id=foreign_user_id,
                wecom_userid="foreign-fixture", wecom_msg_id=str(uuid4()), msg_type="text", content="CROSS_TENANT_FIXTURE", state="completed"))
        settings.we_com_tenant_id = tenant
        message_id = uuid4()
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session, tenant_id=tenant)
            session.add(Message(id=message_id, tenant_id=tenant, user_id=users[0].id,
                wecom_userid=users[0].wecom_userid, wecom_msg_id=str(uuid4()), msg_type="voice", content="",
                media_id="fixture-audio", state="queued"))
        audio = audio_path.read_bytes()
        download_calls = 0

        async def fixture_download(delivery, media_id):
            nonlocal download_calls
            assert media_id == "fixture-audio"
            download_calls += 1
            return audio

        delivery = fixtures.CaptureDelivery()
        delivery.fail_once = True
        with patch("app.voice.download_voice", fixture_download):
            assert await claim_message("phase4-voice") == message_id
            await process_message(message_id, "phase4-voice", delivery)
            row = await fixtures.message_row(message_id)
            assert row.state == "queued" and row.send_state == "retry"
            assert row.answer["outcome"] == "answered", row.answer["outcome"]
            assert "30" in row.answer["text"]
            assert row.content and row.media_id is None
            assert row.diagnostics["voice"]["state"] == "transcribed"
            assert row.diagnostics["candidates"] and row.diagnostics["rerank_scores"]
            assert row.diagnostics["cited_chunk_ids"]
            report["transcript"] = row.content
            passed("real_audio_codec_asr_rag_citations_and_diagnostics")
            async with SessionFactory() as session, session.begin():
                await set_tenant_rls_context(session, tenant_id=tenant)
                await session.execute(update(Message).where(Message.id == message_id).values(next_attempt_at=now()-timedelta(seconds=1)))
            assert await claim_message("phase4-retry") == message_id
            await process_message(message_id, "phase4-retry", delivery)
            assert download_calls == 1 and len(delivery.sent) == 1
            assert (await fixtures.message_row(message_id)).send_state == "sent"
            passed("voice_checkpoint_reused_on_delivery_retry_no_real_send")

        app = FastAPI()
        app.include_router(qa_router)
        app.include_router(management_router)
        token = create_access_token(user_id=admin_id, tenant_id=tenant, permission_version=1, token_type="admin")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            assert (await client.get("/api/v1/admin/qa-messages")).status_code == 401
            employee_token = create_access_token(user_id=users[0].id, tenant_id=tenant, permission_version=1)
            assert (await client.get("/api/v1/admin/qa-messages", headers={"Authorization":f"Bearer {employee_token}"})).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            listing = await client.get("/api/v1/admin/qa-messages", params={"msg_type":"voice", "send_state":"sent", "q":"Aurora", "limit":1})
            assert listing.status_code == 200, listing.status_code
            data = listing.json()
            assert data["total"] == 1 and len(data["items"]) == 1 and data["stats"]["sent"] == 1
            short_id = message_id.hex[:12]
            assert (await client.get("/api/v1/admin/qa-messages", params={"q":short_id})).json()["total"] == 1
            assert (await client.get("/api/v1/admin/qa-messages", params={"offset":1})).json()["items"] == []
            assert (await client.get(f"/api/v1/admin/qa-messages/{foreign_message}")).status_code == 404
            detail = await client.get(f"/api/v1/admin/qa-messages/{message_id}")
            assert detail.status_code == 200
            details = detail.json()
            assert details["question"] and details["answer"]["sources"]
            assert details["candidates"][0]["title"] == "Fictional Aurora expense policy"
            assert any(item["cited"] and item["selected"] for item in details["candidates"])
            assert "content" not in details["candidates"][0]
            passed("admin_only_scoped_search_filter_pagination_candidate_diagnostics")
            events = await client.get("/api/v1/admin/audit-events", params={"category":"qa"})
            assert events.status_code == 200 and events.json()["total"] == 1
            passed("detail_read_audited_in_existing_operations_log")

            async with SessionFactory() as session, session.begin():
                await set_tenant_rls_context(session, tenant_id=tenant)
                await session.execute(update(Message).where(Message.id == message_id).values(received_at=now()-timedelta(days=8)))
            expired = (await client.get(f"/api/v1/admin/qa-messages/{message_id}")).json()
            assert expired["content_expired"] and expired["question"] is None and expired["answer"] is None
            assert expired["candidates"] == [] and expired["diagnostics"] == {}
            hidden_search = (await client.get("/api/v1/admin/qa-messages", params={"q":"Aurora"})).json()
            assert hidden_search["total"] == 0
            await purge_message_content()
            expired_row = await fixtures.message_row(message_id)
            assert expired_row.content == "" and expired_row.diagnostics == {} and expired_row.answer is None
            async with SessionFactory() as session, session.begin():
                await set_tenant_rls_context(session, tenant_id=tenant)
                await session.execute(update(Message).where(Message.id == message_id).values(diagnostics={"outcome":"answered"}))
            await purge_message_content()
            assert (await fixtures.message_row(message_id)).diagnostics == {}
            passed("expired_question_answer_diagnostics_hidden_and_purged")
        report["passed"] = True
        # 恢复已判定不可重试的转写检查点，不再访问媒体或模型。
        failed_voice = uuid4()
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session, tenant_id=tenant)
            session.add(Message(id=failed_voice, tenant_id=tenant, user_id=users[0].id,
                wecom_userid=users[0].wecom_userid, wecom_msg_id=str(uuid4()), msg_type="voice", content="", state="queued",
                diagnostics={"voice":{"state":"failed", "error_code":"voice_no_speech", "retryable":False}}))
        async def unexpected_asr(*args, **kwargs):
            raise AssertionError("已保存的转写失败不应重复调用 ASR")
        with patch("app.rag_worker.transcribe_voice", unexpected_asr):
            assert await claim_message("phase4-no-speech") == failed_voice
            await process_message(failed_voice, "phase4-no-speech", delivery)
        assert (await fixtures.message_row(failed_voice)).answer["outcome"] == "voice_no_speech"
        passed("fatal_voice_checkpoint_recovers_without_media_or_model_call")

        revoked = uuid4()
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session, tenant_id=tenant)
            session.add(Message(id=revoked, tenant_id=tenant, user_id=users[3].id,
                wecom_userid=users[3].wecom_userid, wecom_msg_id=str(uuid4()), msg_type="voice", content="", state="queued", media_id="fixture-audio"))
            await session.execute(update(User).where(User.id == users[3].id).values(wecom_userid=None))
        count = len(delivery.sent)
        with patch("app.rag_worker.transcribe_voice", unexpected_asr):
            assert await claim_message("phase4-revoked") == revoked
            await process_message(revoked, "phase4-revoked", delivery)
        cancelled = await fixtures.message_row(revoked)
        assert cancelled.send_state == "cancelled" and len(delivery.sent) == count
        assert cancelled.diagnostics["outcome"] == "identity_revoked"
        passed("revoked_voice_identity_cannot_download_transcribe_or_send")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        try:
            async with fixtures.OwnerSession() as session, session.begin():
                await session.execute(AdminAuditEvent.__table__.delete().where(AdminAuditEvent.tenant_id == tenant))
                await session.execute(AdminUser.__table__.delete().where(AdminUser.tenant_id == tenant))
            await fixtures.cleanup(tenant)
            await fixtures.cleanup(foreign)
        finally:
            settings.we_com_tenant_id = original_tenant
            await owner.dispose()
            await engine.dispose()
            # 只删除本次明确传入的合成测试音频；不持久化或清理任何用户录音。
            audio_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, default=Path("../TEST/phase4/acceptance.json"))
    args = parser.parse_args()
    asyncio.run(verify(args.audio, args.output))
