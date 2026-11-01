"""意图路由与闲聊通道的可空元数据。

Revision ID: 0010_intent_routing
Revises: 0009_qa_diagnostics
"""
from alembic import op

revision = "0010_intent_routing"
down_revision = "0009_qa_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD COLUMN predicted_intent varchar(24),
        ADD COLUMN intent varchar(24),
        ADD COLUMN intent_confidence double precision,
        ADD COLUMN intent_source varchar(16),
        ADD COLUMN intent_model varchar(80),
        ADD COLUMN intent_rule_id varchar(64),
        ADD COLUMN intent_error varchar(80),
        ADD COLUMN route_ms integer,
        ADD COLUMN policy_version varchar(160)""")
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD CONSTRAINT ck_wecom_intent
        CHECK (intent IS NULL OR intent IN ('chitchat','knowledge_query','out_of_scope'))""")
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD CONSTRAINT ck_wecom_predicted_intent
        CHECK (predicted_intent IS NULL OR predicted_intent IN ('chitchat','knowledge_query','out_of_scope'))""")
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD CONSTRAINT ck_wecom_intent_confidence
        CHECK (intent_confidence IS NULL OR (intent_confidence >= 0 AND intent_confidence <= 1))""")
    op.execute("""ALTER TABLE wecom_inbound_messages
        ADD CONSTRAINT ck_wecom_route_ms
        CHECK (route_ms IS NULL OR route_ms >= 0)""")
    op.execute("CREATE INDEX ix_wecom_intent_received ON wecom_inbound_messages (tenant_id, intent, received_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_wecom_intent_received")
    for constraint in ("ck_wecom_route_ms", "ck_wecom_intent_confidence",
                       "ck_wecom_predicted_intent", "ck_wecom_intent"):
        op.execute(f"ALTER TABLE wecom_inbound_messages DROP CONSTRAINT {constraint}")
    for column in ("policy_version", "route_ms", "intent_error", "intent_rule_id",
                   "intent_model", "intent_source", "intent_confidence", "intent", "predicted_intent"):
        op.execute(f"ALTER TABLE wecom_inbound_messages DROP COLUMN {column}")
