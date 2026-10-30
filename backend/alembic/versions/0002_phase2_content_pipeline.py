"""Phase 2 管理账号、文档修订、摄取任务与版本化索引结构。

Revision ID: 0002_phase2_content_pipeline
Revises: 0001_identity_permissions
"""

from alembic import op

revision = "0002_phase2_content_pipeline"
down_revision = "0001_identity_permissions"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "admin_users",
    "documents",
    "document_revisions",
    "ingestion_jobs",
    "index_versions",
    "chunks",
)


def upgrade() -> None:
    """创建 Phase 2 内容处理表、索引及租户隔离策略。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""CREATE TABLE admin_users (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        username varchar(128) NOT NULL, password_hash varchar(256) NOT NULL,
        role varchar(32) NOT NULL DEFAULT 'tenant_admin', status varchar(24) NOT NULL DEFAULT 'active',
        must_change_password boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
        last_login_at timestamptz, CONSTRAINT uq_admin_users_tenant_username UNIQUE (tenant_id, username),
        CONSTRAINT uq_admin_users_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_admin_users_role CHECK (role IN ('tenant_admin', 'document_manager', 'auditor')),
        CONSTRAINT ck_admin_users_status CHECK (status IN ('active', 'disabled'))
    )""")
    op.execute("""CREATE TABLE documents (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kb_id uuid NOT NULL, title varchar(500) NOT NULL, status varchar(24) NOT NULL DEFAULT 'active',
        clearance_level integer NOT NULL DEFAULT 0, active_revision_id uuid, created_by_admin_id uuid,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_documents_tenant_kb FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_documents_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_documents_clearance_range CHECK (clearance_level BETWEEN 0 AND 3),
        CONSTRAINT ck_documents_status CHECK (status IN ('active', 'deleted'))
    )""")
    op.execute("""CREATE TABLE document_revisions (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL, revision_number integer NOT NULL,
        filename varchar(512) NOT NULL, media_type varchar(160) NOT NULL, byte_size integer NOT NULL,
        sha256 varchar(64) NOT NULL, object_key varchar(1024) NOT NULL UNIQUE, parser_version varchar(64) NOT NULL,
        chunking_config json NOT NULL DEFAULT '{}'::json, clearance_level integer NOT NULL, created_by_admin_id uuid,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_document_revisions_tenant_document FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_document_revisions_number UNIQUE (tenant_id, document_id, revision_number),
        CONSTRAINT uq_document_revisions_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_document_revisions_values CHECK (revision_number > 0 AND byte_size >= 0 AND clearance_level BETWEEN 0 AND 3)
    )""")
    op.execute("ALTER TABLE documents ADD CONSTRAINT fk_documents_active_revision FOREIGN KEY (tenant_id, active_revision_id) REFERENCES document_revisions(tenant_id, id) DEFERRABLE INITIALLY DEFERRED")
    op.execute("""CREATE TABLE ingestion_jobs (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, document_id uuid NOT NULL,
        revision_id uuid NOT NULL, idempotency_key varchar(256) NOT NULL, status varchar(24) NOT NULL DEFAULT 'queued',
        attempt_count integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5, worker_id varchar(160),
        lease_expires_at timestamptz, heartbeat_at timestamptz, checkpoint json NOT NULL DEFAULT '{}'::json,
        error_code varchar(80), error_summary varchar(500), created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
        CONSTRAINT fk_ingestion_jobs_tenant_document FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT fk_ingestion_jobs_tenant_revision FOREIGN KEY (tenant_id, revision_id) REFERENCES document_revisions(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_ingestion_jobs_idempotency UNIQUE (tenant_id, idempotency_key),
        CONSTRAINT ck_ingestion_jobs_status CHECK (status IN ('queued','validating','parsing','chunking','embedding','indexing','completed','failed')),
        CONSTRAINT ck_ingestion_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts > 0)
    )""")
    op.execute("""CREATE TABLE index_versions (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, document_id uuid NOT NULL, revision_id uuid NOT NULL,
        version_number integer NOT NULL, status varchar(24) NOT NULL DEFAULT 'building',
        embedding_model varchar(160) NOT NULL DEFAULT 'text-embedding-v4', embedding_dimension integer NOT NULL DEFAULT 1024,
        chunk_count integer NOT NULL DEFAULT 0, parser_version varchar(64) NOT NULL,
        chunking_config json NOT NULL DEFAULT '{}'::json, created_at timestamptz NOT NULL DEFAULT now(), activated_at timestamptz,
        CONSTRAINT fk_index_versions_tenant_document FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT fk_index_versions_tenant_revision FOREIGN KEY (tenant_id, revision_id) REFERENCES document_revisions(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_index_versions_number UNIQUE (tenant_id, document_id, version_number),
        CONSTRAINT uq_index_versions_tenant_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_index_versions_status CHECK (status IN ('building','ready','active','failed','retired')),
        CONSTRAINT ck_index_versions_counts CHECK (embedding_dimension > 0 AND chunk_count >= 0)
    )""")
    op.execute("""CREATE TABLE chunks (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, kb_id uuid NOT NULL, document_id uuid NOT NULL,
        index_version_id uuid NOT NULL, chunk_key varchar(128) NOT NULL, ordinal integer NOT NULL,
        content text NOT NULL, clearance_level integer NOT NULL, search_vector tsvector,
        embedding vector(1024), metadata_json json NOT NULL DEFAULT '{}'::json, created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT fk_chunks_tenant_index_version FOREIGN KEY (tenant_id, index_version_id) REFERENCES index_versions(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT fk_chunks_tenant_document FOREIGN KEY (tenant_id, document_id) REFERENCES documents(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT fk_chunks_tenant_kb FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases(tenant_id, id) ON DELETE CASCADE,
        CONSTRAINT uq_chunks_version_key UNIQUE (tenant_id, index_version_id, chunk_key),
        CONSTRAINT ck_chunks_security_order CHECK (clearance_level BETWEEN 0 AND 3 AND ordinal >= 0)
    )""")
    op.execute("CREATE INDEX ix_documents_tenant_kb_status ON documents(tenant_id, kb_id, status)")
    op.execute("CREATE INDEX ix_ingestion_jobs_claim ON ingestion_jobs(status, lease_expires_at, created_at)")
    op.execute("CREATE INDEX ix_ingestion_jobs_tenant_status ON ingestion_jobs(tenant_id, status, created_at)")
    op.execute("CREATE INDEX ix_chunks_filter ON chunks(tenant_id, kb_id, clearance_level, index_version_id)")
    op.execute("CREATE INDEX ix_chunks_search_vector ON chunks USING gin(search_vector)")
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING "
            "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        )
    op.drop_table("wecom_oauth_states")


def downgrade() -> None:
    """按依赖逆序移除 Phase 2 表与租户策略。"""
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_active_revision")
    for table in ("chunks", "index_versions", "ingestion_jobs", "document_revisions", "documents", "admin_users"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
