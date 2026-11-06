"""员工开发者模式展示开关。

Revision ID: 0011_employee_developer_mode
Revises: 0010_intent_routing
"""
from alembic import op

revision = "0011_employee_developer_mode"
down_revision = "0010_intent_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN developer_mode boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN developer_mode")
