"""验证文档输入校验、提取与稳定分块规则。"""

import pytest

from app.ingestion import DocumentInputError, extract_paragraphs, split_into_chunks, validate_document_bytes


def test_markdown_validation_and_chunk_identity_are_deterministic() -> None:
    """相同修订与文本应得到相同的稳定 chunk identity。"""
    content = b"A heading\n\nA short body."
    assert validate_document_bytes("guide.md", "text/markdown", content, 1024) == ".md"
    paragraphs = extract_paragraphs("guide.md", content)
    first = split_into_chunks(paragraphs, revision_id="revision-1")
    second = split_into_chunks(paragraphs, revision_id="revision-1")
    assert first == second
    assert first[0]["ordinal"] == 0


def test_rejects_mismatched_type_and_invalid_utf8() -> None:
    """上传时拒绝与扩展名冲突的 MIME 和无效 UTF-8。"""
    with pytest.raises(DocumentInputError):
        validate_document_bytes("guide.md", "application/pdf", b"body", 1024)
    with pytest.raises(DocumentInputError):
        validate_document_bytes("guide.txt", "text/plain", b"\xff", 1024)


def test_oversized_paragraph_is_split_with_overlap() -> None:
    """超长段落按配置窗口重叠切分。"""
    chunks = split_into_chunks(["x" * 20], revision_id="r", size=10, overlap=2)
    assert [len(str(chunk["content"])) for chunk in chunks] == [10, 10, 4]
    assert chunks[0]["content"][-2:] == chunks[1]["content"][:2]
