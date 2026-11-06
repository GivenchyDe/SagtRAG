"""S08 有限会话状态。

Revision ID: 0013_conversation_state
Revises: 0012_qa_observability

只保存理解问题所需的主题/槽位/待补字段和消息版本，不保存 AI 回答、文档正文
或检索证据。表受租户 RLS 约束；回退只删除该表。
"""
from alembic import op

revision = "0013_conversation_state"
down_revision = "0012_qa_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE conversation_states (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL,
        conversation_key varchar(64) NOT NULL DEFAULT 'wecom:direct',
        topic varchar(200),
        confirmed_info json NOT NULL DEFAULT '{}'::json,
        pending_fields json NOT NULL DEFAULT '[]'::json,
        pending_prompt varchar(500),
        last_query text,
        last_message_id uuid,
        last_message_at timestamptz,
        expires_at timestamptz,
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_conversation_state_tenant_user
            FOREIGN KEY (tenant_id, user_id) REFERENCES users(tenant_id, id),
        CONSTRAINT uq_conversation_state_scope
            UNIQUE (tenant_id, user_id, conversation_key),
        CONSTRAINT ck_conversation_state_version CHECK (version >= 1)
    )""")
    op.execute("""CREATE INDEX ix_conversation_state_updated
        ON conversation_states (tenant_id, user_id, updated_at)""")
    op.execute("ALTER TABLE conversation_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_states FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON conversation_states
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversation_states TO sagtrag_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE conversation_states FROM sagtrag_app")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_states")
    op.execute("DROP TABLE IF EXISTS conversation_states")
