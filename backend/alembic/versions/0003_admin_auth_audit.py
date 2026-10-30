"""管理员登录审计与运行账号访问权限。

Revision ID: 0003_admin_auth_audit
Revises: 0002_phase2_content_pipeline
"""

from alembic import op

revision = "0003_admin_auth_audit"
down_revision = "0002_phase2_content_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建独立管理员审计表并授权运行账号执行登录流程。"""
    op.execute("""CREATE TABLE admin_audit_events (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        admin_user_id uuid, action varchar(80) NOT NULL,
        details json NOT NULL DEFAULT '{}'::json, occurred_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_admin_audit_tenant_admin FOREIGN KEY (tenant_id, admin_user_id)
            REFERENCES admin_users(tenant_id, id) ON DELETE RESTRICT
    )""")
    op.execute("CREATE INDEX ix_admin_audit_tenant_time ON admin_audit_events(tenant_id, occurred_at)")
    op.execute("ALTER TABLE admin_audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE admin_audit_events FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON admin_audit_events
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE admin_users TO sagtrag_app")
    op.execute("GRANT SELECT, INSERT ON TABLE admin_audit_events TO sagtrag_app")


def downgrade() -> None:
    """移除管理员审计表及相应运行账号权限。"""
    op.execute("REVOKE SELECT, INSERT ON TABLE admin_audit_events FROM sagtrag_app")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE admin_users FROM sagtrag_app")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON admin_audit_events")
    op.execute("ALTER TABLE admin_audit_events DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE admin_audit_events")
