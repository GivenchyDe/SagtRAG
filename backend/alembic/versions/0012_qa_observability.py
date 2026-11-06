"""S04 可观测性：首次领取时间。

Revision ID: 0012_qa_observability
Revises: 0011_employee_developer_mode

新增列可空，历史记录读取为 NULL（未知），不回填、不改写既有问答内容。
回退仅删除该观测列；其他诊断仍在 diagnostics JSON 中保留。
"""
from alembic import op

revision = "0012_qa_observability"
down_revision = "0011_employee_developer_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE wecom_inbound_messages ADD COLUMN first_claimed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE wecom_inbound_messages DROP COLUMN first_claimed_at")
