"""Phase 3 定向联调：隔离虚构语料、真实数据库/模型、捕获企微发送，不触达真实员工。"""

import argparse
import asyncio
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from dotenv import dotenv_values
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.wecom_callback import router
from app.auth.wecom_crypto import encrypt_message
from app.config import get_settings
from app.db import SessionFactory, engine, set_tenant_rls_context
from app.embeddings import embed_texts
from app.models import AuditEvent, Chunk, Document, DocumentRevision, IndexVersion, KnowledgeBase, Tenant, User, WeComInboundMessage
from app.permissions import load_principal
from app.rag import RagAnswer, answer_question
from app.rag_worker import claim_message, now, process_message, purge_message_content
from app.retrieval import retrieve, visible_chunks
from app.wecom_delivery import DeliveryError

FIXTURE = Path(__file__).with_name("phase3_cases.json")
owner_engine = None
OwnerSession = None


class CaptureDelivery:
    """捕获最终发送，禁止测试中的虚构账号进入真实企微 API。"""

    def __init__(self):
        self.sent = []
        self.fail_once = False

    async def send_text(self, recipient, content):
        if self.fail_once:
            self.fail_once = False
            raise DeliveryError("wecom_send_45009", retryable=True)
        self.sent.append((recipient, content))
        return f"fixture-send-{len(self.sent)}"


class CapturePublisher:
    def __init__(self):
        self.calls = 0

    async def publish(self, **kwargs):
        self.calls += 1


async def seed(tenant_id, documents, *, other=False):
    """只建立本次随机租户的虚构语料，使用真实 Embedding。"""
    settings = get_settings()
    vectors = await embed_texts([item["content"] for item in documents])
    doc_ids = {}
    async with OwnerSession() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=tenant_id)
        session.add(Tenant(id=tenant_id, name="Phase 3 临时虚构联调", wecom_corp_id=f"fixture-{tenant_id}"))
        await session.flush()
        kb = KnowledgeBase(id=uuid4(), tenant_id=tenant_id, name="总知识库", is_default=True)
        session.add(kb)
        await session.flush()
        for item, vector in zip(documents, vectors, strict=True):
            doc_id, revision_id, index_id = uuid4(), uuid4(), uuid4()
            doc_ids[item["key"]] = doc_id
            session.add(Document(id=doc_id, tenant_id=tenant_id, kb_id=kb.id, title=item["title"], clearance_level=item["level"]))
            await session.flush()
            session.add(DocumentRevision(id=revision_id, tenant_id=tenant_id, document_id=doc_id,
                revision_number=1, filename=item["key"] + ".md", media_type="text/markdown",
                byte_size=len(item["content"].encode()), sha256=hashlib.sha256(item["content"].encode()).hexdigest(),
                object_key=f"phase3-fixture/{tenant_id}/{revision_id}", parser_version="fixture-1", clearance_level=item["level"]))
            await session.flush()
            session.add(IndexVersion(id=index_id, tenant_id=tenant_id, document_id=doc_id, revision_id=revision_id,
                version_number=1, status="active", embedding_model=settings.embedding_model,
                embedding_dimension=1024, chunk_count=1, parser_version="fixture-1", chunking_config={}))
            await session.flush()
            await session.execute(update(Document).where(Document.id == doc_id).values(active_revision_id=revision_id))
            await session.execute(text("""INSERT INTO chunks
                (id,tenant_id,kb_id,document_id,index_version_id,chunk_key,ordinal,content,clearance_level,embedding,search_vector,metadata_json)
                VALUES (:id,:tenant,:kb,:doc,:idx,:key,0,:content,:level,CAST(:vector AS vector),
                    to_tsvector('simple',sagtrag_search_text(:content)),'{}'::json)"""),
                {"id":uuid4(),"tenant":tenant_id,"kb":kb.id,"doc":doc_id,"idx":index_id,"key":item["key"],
                 "content":item["content"],"level":item["level"],"vector":"["+",".join(map(str,vector))+"]"})
        users = {}
        for level in (0, 3):
            user = User(id=uuid4(), tenant_id=tenant_id, external_id=f"fixture-{level}",
                        wecom_userid=f"fixture-{tenant_id}-{level}", display_name=f"虚构员工{level}", clearance_level=level)
            session.add(user)
            users[level] = user
        await session.flush()
    return doc_ids, users


async def cleanup(tenant_id):
    """只清理本次创建的随机租户，保留用户现有演示数据。"""
    async with OwnerSession() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=tenant_id)
        await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
        await session.execute(delete(WeComInboundMessage).where(WeComInboundMessage.tenant_id == tenant_id))
        await session.execute(update(Document).where(Document.tenant_id == tenant_id).values(active_revision_id=None))
        for model in (Chunk, IndexVersion, DocumentRevision, Document, User, KnowledgeBase):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def enqueue(tenant, user, *, answer=None, **kwargs):
    message_id = uuid4()
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=tenant)
        session.add(WeComInboundMessage(id=message_id, tenant_id=tenant, user_id=user.id,
            wecom_userid=user.wecom_userid, wecom_msg_id=str(uuid4()), msg_type="text", content="虚构联调问题",
            state="queued", answer=answer, permission_version=user.permission_version, **kwargs))
    return message_id


async def message_row(message_id):
    async with SessionFactory() as session:
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        return await session.get(WeComInboundMessage, message_id)


async def verify(output: Path, *, quality: bool):
    global owner_engine, OwnerSession
    settings = get_settings()
    local = dotenv_values("../TEST/.env")
    runtime = make_url(settings.database_url)
    assert runtime.host in {"127.0.0.1", "localhost"} and runtime.database == local["POSTGRES_DB"]
    owner_url = runtime.set(username=local["POSTGRES_USER"], password=local["POSTGRES_PASSWORD"])
    owner_engine = create_async_engine(owner_url, hide_parameters=True)
    OwnerSession = async_sessionmaker(owner_engine, expire_on_commit=False)
    original_tenant = settings.we_com_tenant_id
    tenant, other = uuid4(), uuid4()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report = {"gate1": [], "cases": [], "real_wecom_delivery": False}

    def passed(name):
        report["gate1"].append(name)
        print("PASS " + name, flush=True)

    try:
        doc_ids, users = await seed(tenant, fixture["documents"])
        await seed(other, [{"key":"foreign","title":"其他租户虚构资料","level":0,"content":"星河公司报销期限为9999天。CROSS_TENANT_SENTINEL"}])
        settings.we_com_tenant_id = tenant
        async with SessionFactory() as session:
            await set_tenant_rls_context(session, tenant_id=tenant)
            principal = await load_principal(session, users[0])
            rows = (await session.execute(visible_chunks(principal))).all()
            assert {row.document_id for row in rows} == {doc_ids["expense"], doc_ids["injection"]}
            assert not (await session.execute(select(Chunk.id).where(Chunk.tenant_id == other))).all()
            chinese_hits = await session.scalar(text("""SELECT count(*) FROM chunks WHERE tenant_id=:tenant
                AND search_vector @@ plainto_tsquery('simple', sagtrag_search_text('交通费'))"""), {"tenant":tenant})
            assert chinese_hits == 1, "中文数字混排的全文索引应可召回"
            candidates = await retrieve(session, principal, "星河公司的报销申请期限")
            assert candidates and all(item.document_id in {doc_ids["expense"], doc_ids["injection"]} for item in candidates)
        passed("tenant_clearance_filter_dense_fts_rrf_and_chinese_fts")

        publisher, delivery = CapturePublisher(), CaptureDelivery()
        app = FastAPI()
        app.include_router(router)
        app.state.inbound_publisher = publisher
        app_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        async with app_client as client:
            plain = ET.Element("xml")
            for key,value in {"ToUserName":settings.we_com_corp_id,"FromUserName":users[0].wecom_userid,
                "AgentID":settings.we_com_agent_id,"MsgType":"text","MsgId":str(uuid4()),
                "Content":fixture["questions"][0]["question"]}.items():
                ET.SubElement(plain,key).text=value
            encrypted = encrypt_message(ET.tostring(plain,encoding="utf-8"))
            timestamp, nonce = str(int(time.time())), "phase3-fixture"
            signature = hashlib.sha1("".join(sorted((settings.we_com_token.get_secret_value(),timestamp,nonce,encrypted))).encode()).hexdigest()
            params = {"msg_signature":signature,"timestamp":timestamp,"nonce":nonce}
            envelope = ET.Element("xml")
            ET.SubElement(envelope,"Encrypt").text=encrypted
            start=time.monotonic()
            first = await client.post("/api/v1/wecom/callback", params=params, content=ET.tostring(envelope))
            elapsed=time.monotonic()-start
            duplicate = await client.post("/api/v1/wecom/callback", params=params, content=ET.tostring(envelope))
            assert first.status_code == duplicate.status_code == 200 and first.text == "success"
            assert elapsed < 5 and publisher.calls == 1
        passed("encrypted_callback_fast_ack_duplicate_published_once")
        claimed = await claim_message("phase3-verify")
        assert claimed is not None
        await process_message(claimed, "phase3-verify", delivery)
        completed = await message_row(claimed)
        assert completed.state == "completed" and completed.send_state == "sent"
        assert completed.answer["outcome"] == "answered", completed.answer["outcome"]
        assert len(delivery.sent)==1 and "30" in delivery.sent[0][1]
        await process_message(claimed, "phase3-verify", delivery)
        assert len(delivery.sent)==1
        saved_answer=completed.answer
        passed("real_embedding_rerank_generation_citations_worker_checkpoint_delivery_once")

        # 崩溃恢复和发送重试复用已生成答案，不反复调用模型。
        expired=await enqueue(tenant,users[0],answer=saved_answer)
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(WeComInboundMessage).where(WeComInboundMessage.id==expired).values(
                state="processing",worker_id="crashed",lease_expires_at=now()-timedelta(seconds=1)))
        assert await claim_message("recovered")==expired
        delivery.fail_once=True
        await process_message(expired,"recovered",delivery)
        retry=await message_row(expired)
        assert retry.state=="queued" and retry.send_state=="retry" and retry.answer==saved_answer
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(WeComInboundMessage).where(WeComInboundMessage.id==expired).values(next_attempt_at=now()-timedelta(seconds=1)))
        assert await claim_message("retry")==expired
        await process_message(expired,"retry",delivery)
        assert (await message_row(expired)).send_state=="sent" and len(delivery.sent)==2
        passed("expired_lease_recovery_retry_reuses_answer")

        unknown=await enqueue(tenant,users[0],answer=saved_answer)
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(WeComInboundMessage).where(WeComInboundMessage.id==unknown).values(
                state="processing",send_state="sending",worker_id="crashed",lease_expires_at=now()-timedelta(seconds=1)))
        assert await claim_message("recover-unknown") is None
        assert (await message_row(unknown)).send_state=="unknown" and len(delivery.sent)==2
        passed("interrupted_send_is_diagnosable_without_duplicate")

        for case in fixture["questions"]:
            async with SessionFactory() as session:
                await set_tenant_rls_context(session,tenant_id=tenant)
                principal=await load_principal(session,users[case["level"]])
                result=await answer_question(session,principal,case["question"])
                assert (result.outcome=="answered") == (case["expected"]=="answered"), f"{case['id']}: {result.outcome}"
                assert "CROSS_TENANT_SENTINEL" not in result.text and "LEAK_SENTINEL" not in result.text
                if case["level"]==0:
                    assert "7391" not in result.text and "680" not in result.text
                if case.get("contains"):
                    assert case["contains"] in result.text, case["id"]
                    assert any(source["document_id"]==str(doc_ids[case["doc"]]) for source in result.sources)
                contexts=result.contexts
            report["cases"].append({**case,"outcome":result.outcome,"response":result.text,"contexts":contexts})
            passed("case_"+case["id"])

        # 已保存的答案在文档删除或身份解除后不得继续送出旧资料。
        revoked=await enqueue(tenant,users[0],answer=saved_answer)
        assert await claim_message("revocation")==revoked
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(Document).where(Document.id==doc_ids["expense"]).values(status="deleted"))
        await process_message(revoked,"revocation",delivery)
        assert (await message_row(revoked)).answer["outcome"]=="permission_changed"
        assert "30" not in delivery.sent[-1][1]
        cancelled=await enqueue(tenant,users[0],answer=saved_answer)
        assert await claim_message("unbind")==cancelled
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(User).where(User.id==users[0].id).values(wecom_userid=None,permission_version=2))
        count=len(delivery.sent)
        await process_message(cancelled,"unbind",delivery)
        assert (await message_row(cancelled)).send_state=="cancelled" and len(delivery.sent)==count
        passed("delete_document_and_unbind_user_rechecked_before_sending")

        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session,tenant_id=tenant)
            await session.execute(update(WeComInboundMessage).where(WeComInboundMessage.id==claimed).values(
                received_at=now()-timedelta(days=settings.rag_message_retention_days+1)))
        await purge_message_content()
        purged=await message_row(claimed)
        assert purged.content=="" and purged.answer is None and purged.wecom_msg_id
        passed("retention_clears_text_preserves_idempotency_tombstone")
        report["gate1_passed"]=True
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        if quality:
            from scripts.evaluate_phase3 import evaluate
            report["gate2"]=await evaluate(report["cases"])
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            if not report["gate2"]["passed"]:
                raise RuntimeError("Gate 2 未达到 Demo 基线，结果已保存")
        print("Phase 3 定向联调完成；真实企微客户端送达仍需公网配置。",flush=True)
    finally:
        for fixture_tenant in (tenant,other):
            await cleanup(fixture_tenant)
        settings.we_com_tenant_id=original_tenant
        await engine.dispose()
        await owner_engine.dispose()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=Path("../TEST/phase3/acceptance.json"))
    parser.add_argument("--quality",action="store_true")
    args=parser.parse_args()
    asyncio.run(verify(args.output,quality=args.quality))
