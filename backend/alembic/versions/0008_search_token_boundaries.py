"""分开中文与数字英文，避免数字后的中文被吞为整词。

Revision ID: 0008_search_token_boundaries
Revises: 0007_rag_messages
"""

from alembic import op

revision = "0008_search_token_boundaries"
down_revision = "0007_rag_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """POSIX alnum 在 UTF-8 locale 下也包含中文，英文分支需显式限定字符。"""
    op.execute("""CREATE OR REPLACE FUNCTION sagtrag_search_text(source text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE part text[]; token text; tokens text[] := ARRAY[]::text[]; i integer;
        BEGIN
            FOR part IN SELECT regexp_matches(lower(source), '[一-鿿]+|[a-z0-9_]+', 'g') LOOP
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
    op.execute("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY")
    op.execute("UPDATE chunks SET search_vector=to_tsvector('simple', sagtrag_search_text(content))")
    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """兼容前一版本接口，保留修正后的分词结果。"""
    pass
