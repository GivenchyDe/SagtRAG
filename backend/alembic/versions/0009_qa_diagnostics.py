"""Phase 4 问答诊断与记录查询索引。

Revision ID: 0009_qa_diagnostics
Revises: 0008_search_token_boundaries
"""
from alembic import op

revision = "0009_qa_diagnostics"
down_revision = "0008_search_token_boundaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE wecom_inbound_messages ADD COLUMN diagnostics json NOT NULL DEFAULT '{}'::json")
    op.execute("CREATE INDEX ix_wecom_qa_received ON wecom_inbound_messages(tenant_id, received_at DESC, id)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_wecom_qa_received")
    op.execute("ALTER TABLE wecom_inbound_messages DROP COLUMN diagnostics")
