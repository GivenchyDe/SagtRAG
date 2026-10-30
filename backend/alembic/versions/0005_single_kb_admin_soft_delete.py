"""单一总知识库标记与管理员软删除。

Revision ID: 0005_single_kb_admin_soft_delete
Revises: 0004_ingestion_runtime_access

本次迁移只追加结构，不搬移或删除既有文档、索引和 S3 原件：

1. ``knowledge_bases.is_default`` 标记租户唯一的总知识库，并建立租户内唯一索引；
   历史数据按“文档最多的启用知识库”选为总知识库并改名为“总知识库”，
   没有文档的其余启用知识库归档为 ``archived``。
2. ``admin_users.deleted_at`` 支持后台软删除；用户名唯一约束改为只约束未删除账号，
   从而允许删除后用同一用户名创建全新账号。
3. 为文档列表追加按租户、状态和上传时间的索引。
"""

from alembic import op

revision = "0005_single_kb_admin_soft_delete"
down_revision = "0004_ingestion_runtime_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加总知识库标记、管理员软删除列及配套索引，并回填历史知识库。"""
    op.execute("ALTER TABLE knowledge_bases ADD COLUMN is_default boolean NOT NULL DEFAULT false")
    op.execute(
        "CREATE UNIQUE INDEX uq_kbs_tenant_default ON knowledge_bases (tenant_id) WHERE is_default"
    )

    # 历史知识库回填需要跨租户读取文档计数。两张表启用了 FORCE RLS，
    # 迁移期间临时关闭行级安全，结束后恢复启用与强制标记。
    op.execute("ALTER TABLE knowledge_bases DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
    try:
        # 每个租户选一份启用的知识库作为总知识库：优先文档最多的，其次最早创建的。
        op.execute(
            """
            WITH stats AS (
                SELECT kb.tenant_id,
                       kb.id,
                       kb.created_at,
                       (
                           SELECT count(*)
                           FROM documents d
                           WHERE d.tenant_id = kb.tenant_id
                             AND d.kb_id = kb.id
                             AND d.status = 'active'
                       ) AS document_count
                FROM knowledge_bases kb
                WHERE kb.status = 'active'
                  AND kb.is_default = false
            ), ranked AS (
                SELECT tenant_id, id, row_number() OVER (
                    PARTITION BY tenant_id ORDER BY document_count DESC, created_at ASC
                ) AS position
                FROM stats
            )
            UPDATE knowledge_bases kb
            SET is_default = true,
                name = '总知识库'
            FROM ranked r
            WHERE kb.id = r.id
              AND r.position = 1
            """
        )
        # 空的历史测试知识库归档，不搬移其文档（本来就没有文档），也不删除任何记录。
        op.execute(
            """
            UPDATE knowledge_bases kb
            SET status = 'archived'
            WHERE kb.status = 'active'
              AND kb.is_default = false
              AND NOT EXISTS (
                  SELECT 1 FROM documents d
                  WHERE d.tenant_id = kb.tenant_id AND d.kb_id = kb.id
              )
            """
        )
    finally:
        op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE knowledge_bases FORCE ROW LEVEL SECURITY")

    op.execute("ALTER TABLE admin_users ADD COLUMN deleted_at timestamptz")
    op.execute("ALTER TABLE admin_users DROP CONSTRAINT IF EXISTS uq_admin_users_tenant_username")
    op.execute(
        "CREATE UNIQUE INDEX uq_admin_users_tenant_username_active "
        "ON admin_users (tenant_id, username) WHERE deleted_at IS NULL"
    )

    op.execute(
        "CREATE INDEX ix_documents_tenant_status_created "
        "ON documents (tenant_id, status, created_at DESC)"
    )


def downgrade() -> None:
    """还原管理员唯一约束并移除新增列与索引；历史改名与归档不回滚。"""
    op.execute("DROP INDEX IF EXISTS ix_documents_tenant_status_created")

    op.execute("DROP INDEX IF EXISTS uq_admin_users_tenant_username_active")
    op.execute("ALTER TABLE admin_users DROP COLUMN IF EXISTS deleted_at")
    op.execute(
        "ALTER TABLE admin_users "
        "ADD CONSTRAINT uq_admin_users_tenant_username UNIQUE (tenant_id, username)"
    )

    op.execute("UPDATE knowledge_bases SET is_default = false")
    op.execute("DROP INDEX IF EXISTS uq_kbs_tenant_default")
    op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS is_default")
