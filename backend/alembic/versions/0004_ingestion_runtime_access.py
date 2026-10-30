"""授予受 RLS 限制的摄取运行时表访问权限。

Revision ID: 0004_ingestion_runtime_access
Revises: 0003_admin_auth_audit
"""

from alembic import op

revision = "0004_ingestion_runtime_access"
down_revision = "0003_admin_auth_audit"
branch_labels = None
depends_on = None

TABLES = ("documents", "document_revisions", "ingestion_jobs", "index_versions", "chunks")


def upgrade() -> None:
    """授权摄取 API 与 Worker 操作内容表；各表继续由 RLS 限定租户范围。"""
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO sagtrag_app")


def downgrade() -> None:
    """撤销摄取运行时表访问权限。"""
    for table in TABLES:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE {table} FROM sagtrag_app")
