"""S10 问答并发与公平调度增量。

Revision ID: 0014_fair_claim
Revises: 0013_conversation_state

只增加领取时间与公平排序索引；会话顺序仍由 PostgreSQL 领取事务、租约和
同员工处理互斥保证，不引入进程内锁作为唯一协调。
"""
from alembic import op

revision = "0014_fair_claim"
down_revision = "0013_conversation_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD COLUMN last_claimed_at timestamptz""")
    op.execute("""CREATE INDEX ix_wecom_fair_claim
        ON wecom_inbound_messages (tenant_id, user_id, last_claimed_at)""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_wecom_fair_claim")
    op.execute("ALTER TABLE wecom_inbound_messages DROP COLUMN last_claimed_at")
