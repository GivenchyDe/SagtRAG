"""Phase 3 问答租约、发送状态与中文全文索引。

Revision ID: 0007_rag_messages
Revises: 0006_user_soft_delete
"""

from alembic import op

revision = "0007_rag_messages"
down_revision = "0006_user_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展现有消息表，历史固定回复记录保持 completed，不回放。"""
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD COLUMN attempt_count integer NOT NULL DEFAULT 0,
        ADD COLUMN send_attempt_count integer NOT NULL DEFAULT 0,
        ADD COLUMN worker_id varchar(160),
        ADD COLUMN lease_expires_at timestamptz,
        ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
        ADD COLUMN answer json,
        ADD COLUMN permission_version integer,
        ADD COLUMN send_state varchar(24) NOT NULL DEFAULT 'pending',
        ADD COLUMN send_msg_id varchar(128),
        ADD COLUMN error_code varchar(80),
        ADD COLUMN completed_at timestamptz,
        ADD CONSTRAINT ck_wecom_send_state CHECK
            (send_state IN ('pending','sending','retry','sent','unknown','cancelled','failed'))""")
    op.execute("""UPDATE wecom_inbound_messages SET send_state='sent', completed_at=received_at
        WHERE state='completed'""")
    op.execute("""CREATE INDEX ix_wecom_rag_claim ON wecom_inbound_messages
        (tenant_id, state, next_attempt_at, lease_expires_at)""")
    # simple 配置不切中文词；用相邻双字作为中文召回项，同时保留英文和数字词。
    op.execute("""CREATE FUNCTION sagtrag_search_text(source text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE part text[]; token text; tokens text[] := ARRAY[]::text[]; i integer;
        BEGIN
            FOR part IN SELECT regexp_matches(lower(source), '[一-鿿]+|[[:alnum:]_]+', 'g') LOOP
                token := part[1];
                IF token ~ '^[一-鿿]+$' AND char_length(token) > 1 THEN
                    FOR i IN 1..char_length(token)-1 LOOP
                        tokens := array_append(tokens, substr(token, i, 2));
                    END LOOP;
                ELSE
                    tokens := array_append(tokens, token);
                END IF;
            END LOOP;
            RETURN array_to_string(tokens, ' ');
        END $$""")
    # owner 执行数据迁移，明确恢复 FORCE RLS；运行账号权限保持不变。
    op.execute("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY")
    op.execute("UPDATE chunks SET search_vector=to_tsvector('simple', sagtrag_search_text(content))")
    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """仅移除 Phase 3 增量结构。"""
    op.execute("DROP INDEX ix_wecom_rag_claim")
    op.execute("ALTER TABLE wecom_inbound_messages DROP CONSTRAINT ck_wecom_send_state")
    for column in ("attempt_count", "send_attempt_count", "worker_id", "lease_expires_at",
                   "next_attempt_at", "answer", "permission_version", "send_state", "send_msg_id",
                   "error_code", "completed_at"):
        op.execute(f"ALTER TABLE wecom_inbound_messages DROP COLUMN {column}")
    op.execute("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY")
    op.execute("UPDATE chunks SET search_vector=to_tsvector('simple', content)")
    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION sagtrag_search_text(text)")
