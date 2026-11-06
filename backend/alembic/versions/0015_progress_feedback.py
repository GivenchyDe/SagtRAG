"""S11 有界进度消息与 unknown 可诊断状态。

Revision ID: 0015_progress_feedback
Revises: 0014_fair_claim

进度消息与最终答案分开保存；最多一次，不把进度当作最终送达。
"""
from alembic import op

revision = "0015_progress_feedback"
down_revision = "0014_fair_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD COLUMN progress_state varchar(24) NOT NULL DEFAULT 'none',
        ADD COLUMN progress_attempt_count integer NOT NULL DEFAULT 0,
        ADD COLUMN progress_msg_id varchar(128),
        ADD COLUMN progress_sent_at timestamptz,
        ADD COLUMN progress_error varchar(80)""")
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD CONSTRAINT ck_wecom_progress_state CHECK
        (progress_state IN ('none','sending','sent','failed','unknown'))""")


def downgrade() -> None:
    op.execute("ALTER TABLE wecom_inbound_messages DROP CONSTRAINT ck_wecom_progress_state")
    for column in ("progress_state", "progress_attempt_count", "progress_msg_id",
                   "progress_sent_at", "progress_error"):
        op.execute(f"ALTER TABLE wecom_inbound_messages DROP COLUMN {column}")
