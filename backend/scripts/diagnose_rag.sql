-- R0：意图与 RAG 阶段诊断（只读，不修改数据）。
-- 用法：
--   psql -U sagtrag_owner -d sagtrag -f backend/scripts/diagnose_rag.sql
-- 先对同一问题的不同问法各发一次，再运行本脚本比较结果。

\echo '=== 最近问答的阶段诊断 ==='
SELECT received_at,
       left(content, 40)                 AS question,
       answer->>'outcome'                AS outcome,
       diagnostics->>'intent'            AS intent,
       diagnostics->>'intent_source'     AS intent_source,
       diagnostics->>'dense_count'       AS dense,
       diagnostics->>'fts_count'         AS fts,
       diagnostics->'rerank_scores'      AS rerank_scores,
       diagnostics->'selected_chunk_ids' AS selected,
       diagnostics->>'intent_error'      AS intent_error,
       diagnostics->>'error_code'        AS error_code,
       diagnostics->>'rag_total_ms'      AS total_ms
FROM wecom_inbound_messages
WHERE received_at > now() - interval '7 days'
ORDER BY received_at DESC
LIMIT 50;

\echo '=== 文档与索引版本（iv_status 必须是 active）==='
SELECT d.id, d.status, d.active_revision_id,
       iv.id AS index_version, iv.status AS iv_status,
       iv.embedding_model, iv.embedding_dimension, iv.chunk_count
FROM documents d
LEFT JOIN index_versions iv ON iv.document_id = d.id
ORDER BY d.created_at DESC;

\echo '=== 分块与向量/全文索引 ==='
SELECT count(*) AS chunks,
       count(embedding) AS with_embedding,
       count(search_vector) AS with_fts
FROM chunks;
