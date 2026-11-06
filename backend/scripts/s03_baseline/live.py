"""本机随机 schema 中执行真实检索/模型；不读取业务数据、不发送企微消息。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.s03_baseline.evaluate import (
    fact_precheck, live_summary, requests, stable_id, write_json,
)


@contextmanager
def counted_http(stats, limit):
    """只累计次数、模型和供应商用量，不记录凭据、请求或响应原文。"""
    send = httpx.AsyncClient.send

    async def counted(client, request, **kwargs):
        if stats["http_requests"] >= limit:
            raise RuntimeError("s03_http_budget_exhausted")
        stats["http_requests"] += 1
        payload = json.loads(request.content)
        model = payload.get("model", "unknown")
        stats["by_model"][model] = stats["by_model"].get(model, 0) + 1
        response = await send(client, request, **kwargs)

        def usage(data):
            value = data.get("usage")
            if isinstance(value, dict):
                stats["provider_usage_records"].append({"model": model, "usage": value})

        if kwargs.get("stream"):
            lines = response.aiter_lines

            async def tracked_lines():
                async for line in lines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        try:
                            usage(json.loads(line[5:]))
                        except (ValueError, TypeError):
                            pass
                    yield line
            response.aiter_lines = tracked_lines
        else:
            try:
                usage(response.json())
            except ValueError:
                pass
        return response

    with patch.object(httpx.AsyncClient, "send", counted):
        yield


def profile(settings, args):
    names = ("generation_model", "embedding_model", "rerank_model", "rag_recall_limit", "rag_context_limit",
             "rag_min_relevance", "intent_model", "intent_timeout_seconds", "intent_retry",
             "intent_confidence_threshold", "intent_rules_version", "intent_prompt_version",
             "chitchat_model", "chitchat_max_tokens", "chitchat_max_bytes")
    # 只暴露白名单，不输出 Settings、数据库 URL 或密钥。
    values = {key: getattr(settings, key) for key in names}
    values["intent_routing_enabled"] = True if args.routing == "on" else settings.intent_routing_enabled
    for name in ("embedding_base_url", "generation_base_url", "rerank_url", "intent_base_url", "chitchat_base_url"):
        value = getattr(settings, name)
        values[name + "_sha256"] = hashlib.sha256(str(value).encode()).hexdigest()
    return {"settings": values, "routing_source": args.routing,
            "local_routing_enabled": settings.intent_routing_enabled, "concurrency": args.concurrency,
            "chunk_size": 1200, "chunk_overlap": 150,
            "database_scope": "local_random_schema_owner_role_application_predicates_only",
            "rls_policy_verification": "not_exercised_by_this_harness",
            "conversation_mode": "current_single_turn_no_history_injection",
            "external_data": "synthetic_fixture_only", "wecom_delivery": False,
            "latency_scope": "local_harness_not_production_end_to_end"}


async def seed(factory, data, parsed):
    from app.config import get_settings
    from app.embeddings import embed_texts
    from app.models import Document, DocumentRevision, IndexVersion, KnowledgeBase, Tenant, User
    from app.permissions import Principal

    vectors = await embed_texts([chunk["content"] for row in parsed for chunk in row["chunks"]])
    vector_iter = iter(vectors)
    tenant = stable_id("main-tenant")
    user = stable_id("main-user")
    async with factory() as session, session.begin():
        for label in ("main", "other"):
            tid = stable_id(label + "-tenant")
            session.add(Tenant(id=tid, name="S03虚构租户", wecom_corp_id=str(tid)))
        await session.flush()
        for label in ("main", "other"):
            tid, uid, kb = stable_id(label + "-tenant"), stable_id(label + "-user"), stable_id(label + "-kb")
            session.add(User(id=uid, tenant_id=tid, external_id="s03-fixture", display_name="虚构员工",
                             wecom_userid="s03-fictional", clearance_level=0))
            session.add(KnowledgeBase(id=kb, tenant_id=tid, name="S03虚构语料", is_default=True))
        await session.flush()
        documents = [(item, row["chunks"], "main", 0) for item, row in zip(data["knowledge"], parsed, strict=True)]
        for label, level in (("secret", 3), ("foreign", 0)):
            documents.append(({"id": label, "title": "虚构权限哨兵", "content": "虚构权限哨兵，不得泄漏。"},
                              [{"ordinal": 0, "content": "虚构权限哨兵，不得泄漏。", "chunk_key": label}],
                              "other" if label == "foreign" else "main", level))
        for item, chunks, label, level in documents:
            tid, kb = stable_id(label + "-tenant"), stable_id(label + "-kb")
            doc, rev, idx = (stable_id(item["id"] + suffix) for suffix in (":doc", ":revision", ":index"))
            session.add(Document(id=doc, tenant_id=tid, kb_id=kb, title=item["title"], clearance_level=level))
            await session.flush()
            session.add(DocumentRevision(id=rev, tenant_id=tid, document_id=doc, revision_number=1,
                filename=item["id"] + ".md", media_type="text/markdown", byte_size=len(item["content"].encode()),
                sha256=hashlib.sha256(item["content"].encode()).hexdigest(), object_key="s03-fictional/" + str(rev),
                parser_version="sagtrag-parser-1", clearance_level=level))
            await session.flush()
            session.add(IndexVersion(id=idx, tenant_id=tid, document_id=doc, revision_id=rev, version_number=1,
                status="active", embedding_model=get_settings().embedding_model, embedding_dimension=1024,
                chunk_count=len(chunks), parser_version="sagtrag-parser-1", chunking_config={"size":1200,"overlap":150}))
            await session.flush()
            await session.execute(update(Document).where(Document.id == doc).values(active_revision_id=rev))
            for chunk in chunks:
                vector = next(vector_iter) if item["id"] not in {"secret", "foreign"} else vectors[0]
                await session.execute(text("""INSERT INTO chunks
                    (id,tenant_id,kb_id,document_id,index_version_id,chunk_key,ordinal,content,clearance_level,embedding,search_vector,metadata_json)
                    VALUES (:id,:tenant,:kb,:doc,:idx,:key,:ordinal,:content,:level,CAST(:vector AS vector),
                        to_tsvector('simple',sagtrag_search_text(:content)),'{}'::json)"""),
                    {"id":stable_id(item["id"] + ":chunk:" + str(chunk["ordinal"])), "tenant":tid,"kb":kb,
                     "doc":doc,"idx":idx,"key":chunk["chunk_key"],"ordinal":chunk["ordinal"],
                     "content":chunk["content"],"level":level,"vector":"["+",".join(map(str,vector))+"]"})
    return Principal(user_id=user, tenant_id=tenant, clearance_level=0, permission_version=1, tenant_role=None)


async def acl_checks(factory, principal):
    """真实 SQL 下测试应用谓词；不冒称 owner 连接验证了 RLS。"""
    from app.config import get_settings
    from app.models import Document, IndexVersion, KnowledgeBase, User
    from app.retrieval import visible_chunks
    checks = []
    target = stable_id("expense_deadline:doc")
    user = stable_id("main-user")
    scenarios = [
        ("ordinary_visible", None, target, True),
        ("cross_tenant_denied", None, stable_id("foreign:doc"), False),
        ("clearance_denied", None, stable_id("secret:doc"), False),
        ("clearance_granted", update(User).where(User.id == user).values(clearance_level=3), stable_id("secret:doc"), True),
        ("disabled_user", update(User).where(User.id == user).values(status="disabled"), target, False),
        ("unbound_user", update(User).where(User.id == user).values(wecom_userid=None), target, False),
        ("deleted_document", update(Document).where(Document.id == target).values(status="deleted"), target, False),
        ("inactive_index", update(IndexVersion).where(IndexVersion.document_id == target).values(status="retired"), target, False),
        ("wrong_embedding", update(IndexVersion).where(IndexVersion.document_id == target).values(embedding_model="s03-incompatible"), target, False),
        ("inactive_kb", update(KnowledgeBase).where(KnowledgeBase.id == stable_id("main-kb")).values(status="disabled"), target, False),
    ]
    for name, mutation, doc, expected in scenarios:
        async with factory() as session:
            if mutation is not None:
                await session.execute(mutation)
            rows = (await session.execute(visible_chunks(principal))).all()
            actual = doc in {row.document_id for row in rows}
            checks.append({"id":name,"expected_visible":expected,"actual_visible":actual,"passed":actual == expected})
            await session.rollback()
    return checks


async def run_live(args, data, frozen, report):
    from app.config import get_settings
    from app.models import Base

    settings = get_settings()
    runtime = make_url(settings.database_url)
    local = dotenv_values(Path(__file__).resolve().parents[3] / "TEST/.env")
    if runtime.host not in {"127.0.0.1", "localhost"} or runtime.database != local.get("POSTGRES_DB"):
        raise ValueError("仅允许本项目本机测试数据库")
    if not 1 <= args.limit <= frozen["budgets"]["max_pipeline_requests"]:
        raise ValueError("问答数量超过冻结预算")
    context = profile(settings, args)
    previous = None
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        if (previous["dataset"] != report["dataset"] or
            previous["implementation"]["source_sha256"] != report["implementation"]["source_sha256"] or
            previous.get("live", {}).get("profile") != context or
            previous.get("harness_sha256") != harness_hash()):
            raise ValueError("数据、业务代码、评测代码或配置变化，禁止混用旧结果")
    report["harness_sha256"] = harness_hash()
    live = report["live"] = {"profile":context,"complete":False,"rows":[],"boundaries":[],"acl":[],
        "budget": {"http_requests":0,"by_model":{},"provider_usage_records":[],"actual_cost_cny":None,
                   "cost_note":"流式接口可能不返回用量；不以请求数伪造token或费用。实际费用须以供应商账单核对。"}}
    if previous:
        for key in ("rows", "boundaries", "budget"):
            live[key] = previous["live"][key]
    schema = "s03_eval_" + uuid4().hex
    live["schema"] = schema
    owner = runtime.set(username=local["POSTGRES_USER"], password=local["POSTGRES_PASSWORD"])
    engine = create_async_engine(owner, hide_parameters=True, pool_size=args.concurrency+1,
        connect_args={"timeout":5,"server_settings":{"search_path":f'"{schema}",public',"statement_timeout":"15000"}},
        execution_options={"schema_translate_map":{None:schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    created = False
    original_routing = settings.intent_routing_enabled
    settings.intent_routing_enabled = context["settings"]["intent_routing_enabled"]
    try:
        print("S03 connecting local PostgreSQL", flush=True)
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("S03 local isolated schema ready", flush=True)
        with counted_http(live["budget"], frozen["budgets"]["max_http_requests"]):
            principal = await seed(factory, data, report["engineering"]["parsed_corpus"])
            live["acl"] = await acl_checks(factory, principal)
            if not all(row["passed"] for row in live["acl"]):
                raise RuntimeError("s03_acl_fixture_failed")
            semaphore = asyncio.Semaphore(args.concurrency)
            known = {item["id"]:item for item in data["knowledge"]}
            completed = {row["id"] for row in live["rows"]}
            pending = [row for row in requests(data, frozen) if row["id"] not in completed][:args.limit]

            async def execute(case):
                from app.chitchat import answer_chitchat, answer_out_of_scope
                from app.intent_router import detect_intent
                from app.rag import answer_question
                async with semaphore:
                    start = time.perf_counter()
                    diagnostics = {}
                    try:
                        async with asyncio.timeout(frozen["budgets"]["per_request_seconds"]):
                            route = await detect_intent(case["question"])
                            diagnostics.update({"intent":route.intent,"intent_rule_id":route.rule_id,
                                                "intent_source":route.source,"route_ms":route.elapsed_ms})
                            if route.intent == "knowledge_query":
                                async with factory() as session:
                                    answer = await answer_question(session, principal, case["question"], diagnostics=diagnostics)
                            elif route.intent == "chitchat":
                                answer = await answer_chitchat(case["question"], route)
                            else:
                                answer = answer_out_of_scope(route, settings)
                        item = known.get(case.get("knowledge_id"))
                        row = {**case,"outcome":answer.outcome,"response":answer.display_text(developer=False),
                               "sources":answer.sources,"contexts":answer.contexts,"diagnostics":diagnostics,
                               "fact_precheck":fact_precheck(answer.display_text(developer=False), item) if item else None,
                               "expected_evidence_in_context":any(item["evidence"] in c for c in answer.contexts) if item else None,
                               "human_review":None}
                    except Exception as exc:
                        row = {**case,"outcome":"harness_timeout" if isinstance(exc, TimeoutError) else "harness_error",
                               "error_type":type(exc).__name__,"diagnostics":diagnostics,"human_review":None}
                    row["harness_ms"] = round((time.perf_counter()-start)*1000)
                    live["rows"].append(row)
                    live["summary"] = live_summary(live["rows"], data)
                    write_json(args.output, report)
                    print(f"S03 {len(live['rows'])}/191 {case['id']} {row['outcome']} {row['harness_ms']}ms", flush=True)

            # 同一对话按轮次执行；不同对话及独立问题受控并发。
            independent = [case for case in pending if case["suite"] != "conversations"]
            await asyncio.gather(*(execute(case) for case in independent))
            for conversation in data["conversations"]:
                for case in pending:
                    if case.get("conversation_id") == conversation["id"]:
                        await execute(case)
            from app.intent_router import detect_intent
            done_boundaries = {row["id"] for row in live["boundaries"]}
            for pair, q1, e1, q2, e2 in data["boundaries"]:
                for number, question, expected in ((1,q1,e1),(2,q2,e2)):
                    key = f"{pair}-{number}"
                    if key in done_boundaries:
                        continue
                    start = time.perf_counter()
                    try:
                        async with asyncio.timeout(30):
                            actual = await detect_intent(question)
                        live["boundaries"].append({"id":key,"question":question,"expected":expected,
                            "actual":actual.intent,"source":actual.source,"rule":actual.rule_id,
                            "passed":actual.intent == expected,"elapsed_ms":round((time.perf_counter()-start)*1000)})
                    except Exception as exc:
                        live["boundaries"].append({"id":key,"expected":expected,"actual":"harness_error",
                                                   "error_type":type(exc).__name__,"passed":False})
                    write_json(args.output, report)
        live["summary"] = live_summary(live["rows"], data)
        live["complete"] = (len(live["rows"]) == len(requests(data,frozen)) and
                            len(live["boundaries"]) == len(data["boundaries"])*2 and len(live["acl"]) == 10)
    except Exception as exc:
        live["stopped"] = {"error_type":type(exc).__name__,"code":"setup_or_budget_error"}
        print(f"S03 stopped: {type(exc).__name__}; sanitized details in report", flush=True)
    finally:
        settings.intent_routing_enabled = original_routing
        try:
            if created:
                # 名称仅由本进程生成；清理范围始终是该随机 schema。
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.drop_all)
                    await conn.execute(text(f'DROP SCHEMA "{schema}"'))
                live["schema_cleaned"] = True
        except Exception as exc:
            live["schema_cleaned"] = False
            live["cleanup_error_type"] = type(exc).__name__
        finally:
            await engine.dispose()
            write_json(args.output, report)


def harness_hash():
    root = Path(__file__).parent
    return {path.name:hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*.py"))}
