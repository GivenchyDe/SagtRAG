"""首版租户身份、企业微信绑定和授权数据结构。

Revision ID: 0001_identity_permissions
Revises:
"""

from alembic import op

revision = "0001_identity_permissions"
down_revision = None
branch_labels = None
depends_on = None


TENANT_POLICY = "tenant_isolation"
TENANT_TABLES = (
    "tenants",
    "users",
    "groups",
    "group_members",
    "knowledge_bases",
    "kb_members",
    "wecom_oauth_states",
    "wecom_inbound_messages",
    "audit_events",
)


def upgrade() -> None:
    """创建身份与授权表，并安装租户级 RLS 策略。"""
    op.execute(
        """CREATE TABLE tenants (
            id uuid PRIMARY KEY,
            name varchar(200) NOT NULL,
            wecom_corp_id varchar(128) NOT NULL UNIQUE,
            status varchar(24) NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE users (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            external_id varchar(200) NOT NULL,
            wecom_userid varchar(128),
            display_name varchar(200) NOT NULL,
            clearance_level integer NOT NULL DEFAULT 0,
            tenant_role varchar(32),
            status varchar(24) NOT NULL DEFAULT 'active',
            permission_version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz,
            CONSTRAINT uq_users_tenant_external UNIQUE (tenant_id, external_id),
            CONSTRAINT uq_users_tenant_wecom_user UNIQUE (tenant_id, wecom_userid),
            CONSTRAINT uq_users_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_users_clearance_range CHECK (clearance_level BETWEEN 0 AND 3),
            CONSTRAINT ck_users_tenant_role CHECK (tenant_role IS NULL OR tenant_role = 'tenant_admin'),
            CONSTRAINT ck_users_status CHECK (status IN ('active', 'disabled')),
            CONSTRAINT ck_users_permission_version CHECK (permission_version > 0)
        )"""
    )
    op.execute("CREATE INDEX ix_users_tenant_id ON users(tenant_id)")
    op.execute(
        """CREATE TABLE groups (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            name varchar(160) NOT NULL,
            clearance_level integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_groups_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_groups_tenant_name UNIQUE (tenant_id, name),
            CONSTRAINT ck_groups_clearance_range CHECK (clearance_level BETWEEN 0 AND 3)
        )"""
    )
    op.execute("CREATE INDEX ix_groups_tenant_id ON groups(tenant_id)")
    op.execute(
        """CREATE TABLE group_members (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            group_id uuid NOT NULL,
            user_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_group_members_tenant_user FOREIGN KEY (tenant_id, user_id)
                REFERENCES users(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_group_members_tenant_group FOREIGN KEY (tenant_id, group_id)
                REFERENCES groups(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_group_membership UNIQUE (tenant_id, group_id, user_id)
        )"""
    )
    op.execute("CREATE INDEX ix_group_members_tenant_id ON group_members(tenant_id)")
    op.execute(
        """CREATE TABLE knowledge_bases (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            name varchar(200) NOT NULL,
            status varchar(24) NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_kbs_tenant_id UNIQUE (tenant_id, id)
        )"""
    )
    op.execute("CREATE INDEX ix_knowledge_bases_tenant_id ON knowledge_bases(tenant_id)")
    op.execute(
        """CREATE TABLE kb_members (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            kb_id uuid NOT NULL,
            user_id uuid,
            group_id uuid,
            role varchar(32) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_kb_members_tenant_kb FOREIGN KEY (tenant_id, kb_id)
                REFERENCES knowledge_bases(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_kb_members_tenant_user FOREIGN KEY (tenant_id, user_id)
                REFERENCES users(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_kb_members_tenant_group FOREIGN KEY (tenant_id, group_id)
                REFERENCES groups(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_kb_member_one_subject CHECK ((user_id IS NOT NULL)::int + (group_id IS NOT NULL)::int = 1),
            CONSTRAINT ck_kb_member_role CHECK (role IN ('kb_admin', 'editor', 'viewer', 'auditor')),
            CONSTRAINT uq_kb_user_member UNIQUE (tenant_id, kb_id, user_id),
            CONSTRAINT uq_kb_group_member UNIQUE (tenant_id, kb_id, group_id)
        )"""
    )
    op.execute("CREATE INDEX ix_kb_members_user ON kb_members(tenant_id, user_id) WHERE user_id IS NOT NULL")
    op.execute("CREATE INDEX ix_kb_members_group ON kb_members(tenant_id, group_id) WHERE group_id IS NOT NULL")
    op.execute(
        """CREATE TABLE wecom_oauth_states (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            state_hash varchar(64) NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_wecom_oauth_states_expiry ON wecom_oauth_states(expires_at)")
    op.execute(
        """CREATE TABLE wecom_inbound_messages (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            user_id uuid NOT NULL,
            wecom_userid varchar(128) NOT NULL,
            wecom_msg_id varchar(128) NOT NULL,
            msg_type varchar(16) NOT NULL,
            content text NOT NULL DEFAULT '',
            media_id varchar(256),
            state varchar(24) NOT NULL DEFAULT 'received',
            received_at timestamptz NOT NULL DEFAULT now(),
            published_at timestamptz,
            CONSTRAINT fk_wecom_inbound_tenant_user FOREIGN KEY (tenant_id, user_id)
                REFERENCES users(tenant_id, id),
            CONSTRAINT uq_wecom_inbound_msg UNIQUE (tenant_id, wecom_msg_id),
            CONSTRAINT ck_wecom_inbound_msg_type CHECK (msg_type IN ('text', 'voice')),
            CONSTRAINT ck_wecom_inbound_state CHECK (state IN ('received', 'queued', 'processing', 'completed', 'failed')),
            CONSTRAINT ck_wecom_inbound_content_size CHECK (length(content) <= 10000)
        )"""
    )
    op.execute("CREATE INDEX ix_wecom_inbound_state ON wecom_inbound_messages(tenant_id, state, received_at)")
    op.execute(
        """CREATE TABLE audit_events (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            actor_id uuid NOT NULL,
            action varchar(80) NOT NULL,
            resource_type varchar(80) NOT NULL,
            resource_id varchar(160) NOT NULL,
            details json NOT NULL DEFAULT '{}'::json,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_audit_events_tenant_actor FOREIGN KEY (tenant_id, actor_id)
                REFERENCES users(tenant_id, id)
        )"""
    )
    op.execute("CREATE INDEX ix_audit_events_tenant_id ON audit_events(tenant_id)")

    # OAuth 查询前已知当前配置的租户 ID。租户路由表也必须受范围约束，避免运行账号枚举其他企业的信息。
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        tenant_column = "id" if table == "tenants" else "tenant_id"
        op.execute(
            f"""CREATE POLICY {TENANT_POLICY} ON {table}
                USING ({tenant_column} = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK ({tenant_column} = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
        )


def downgrade() -> None:
    """按依赖关系的逆序删除首版身份数据结构及其 RLS 策略。"""
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {TENANT_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS audit_events")
    op.execute("DROP TABLE IF EXISTS wecom_inbound_messages")
    op.execute("DROP TABLE IF EXISTS wecom_oauth_states")
    op.execute("DROP TABLE IF EXISTS kb_members")
    op.execute("DROP TABLE IF EXISTS knowledge_bases")
    op.execute("DROP TABLE IF EXISTS group_members")
    op.execute("DROP TABLE IF EXISTS groups")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TABLE IF EXISTS tenants")
