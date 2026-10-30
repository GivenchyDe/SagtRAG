"""员工软删除。

Revision ID: 0006_user_soft_delete
Revises: 0005_single_kb_admin_soft_delete

员工列表中的“删除”不再只表示解除企微绑定：删除后员工从列表移除、企微身份立即失效，
但用户行与历史审计保留，便于追溯。解除绑定仍通过独立的绑定接口完成。
"""

from alembic import op

revision = "0006_user_soft_delete"
down_revision = "0005_single_kb_admin_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增员工软删除标记与租户内查询索引。"""
    op.execute("ALTER TABLE users ADD COLUMN deleted_at timestamptz")
    op.execute("CREATE INDEX ix_users_tenant_deleted ON users (tenant_id, deleted_at)")


def downgrade() -> None:
    """移除员工软删除标记。"""
    op.execute("DROP INDEX IF EXISTS ix_users_tenant_deleted")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
