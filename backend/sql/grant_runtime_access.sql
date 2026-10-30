-- Run as the DBA after the Alembic migration. Create the role separately with a
-- password from a secret manager; never store a production password in this file.
-- This role must not own application tables and must not have BYPASSRLS/SUPERUSER.

ALTER ROLE sagtrag_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT USAGE ON SCHEMA public TO sagtrag_app;

-- The CorpID registry is still row filtered by app.tenant_id. Runtime code sets
-- that context from the authenticated request and configured tenant context.
GRANT SELECT ON TABLE tenants TO sagtrag_app;

GRANT SELECT, INSERT, UPDATE ON TABLE users, groups, knowledge_bases TO sagtrag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE group_members, kb_members TO sagtrag_app;
GRANT SELECT, INSERT, UPDATE ON TABLE wecom_inbound_messages TO sagtrag_app;

-- Audit is append-only for the runtime application.
GRANT SELECT, INSERT ON TABLE audit_events TO sagtrag_app;

-- Phase 2 admin authentication and audit records.
GRANT SELECT, INSERT, UPDATE ON TABLE admin_users TO sagtrag_app;
GRANT SELECT, INSERT ON TABLE admin_audit_events TO sagtrag_app;

-- Phase 2 document intake and indexing worker. Row access remains tenant-scoped
-- by forced RLS policies created by Alembic migrations.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    documents, document_revisions, ingestion_jobs, index_versions, chunks
    TO sagtrag_app;

