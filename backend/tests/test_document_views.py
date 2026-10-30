"""文档列表投影与统一访问条件的回归测试。"""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.api.documents import serialize_document
from app.models import Document
from app.permissions import Principal, document_access_predicate


def test_serialize_document_defaults_to_queued_without_job() -> None:
    """刚创建、尚未产生任务快照的文档仍应展示为排队中。"""
    document = SimpleNamespace(
        id=uuid4(),
        title="虚构制度",
        clearance_level=2,
        created_at="2026-10-29T00:00:00+08:00",
        updated_at="2026-10-29T00:00:00+08:00",
    )
    view = serialize_document(document, None, None)
    assert view["ingestion_status"] == "queued"
    assert view["filename"] is None
    assert view["byte_size"] is None
    assert view["error_summary"] is None


def test_serialize_document_hides_summary_until_failure() -> None:
    """错误摘要只对失败任务展示，处理中的任务不暴露半成品错误。"""
    document = SimpleNamespace(
        id=uuid4(),
        title="虚构制度",
        clearance_level=0,
        created_at="2026-10-29T00:00:00+08:00",
        updated_at="2026-10-29T00:00:00+08:00",
    )
    revision = SimpleNamespace(filename="policy.md", byte_size=193)
    processing = SimpleNamespace(status="parsing", error_summary=None, id=uuid4())
    failed = SimpleNamespace(status="failed", error_summary="解析失败，请检查文件格式", id=uuid4())

    assert serialize_document(document, revision, processing)["error_summary"] is None
    failed_view = serialize_document(document, revision, failed)
    assert failed_view["error_summary"] == "解析失败，请检查文件格式"
    assert failed_view["ingestion_status"] == "failed"


def test_document_access_predicate_requires_active_wecom_binding_and_clearance() -> None:
    """访问条件必须在 SQL 中限定租户、启用账号、企微绑定与密级。"""
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        clearance_level=2,
        permission_version=1,
        tenant_role=None,
    )
    expression = document_access_predicate(Document, principal)
    sql = str(expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))
    assert "documents.tenant_id" in sql
    assert "wecom_userid IS NOT NULL" in sql
    assert "users.status" in sql
    assert "documents.clearance_level <=" in sql
    assert "users.clearance_level" in sql
