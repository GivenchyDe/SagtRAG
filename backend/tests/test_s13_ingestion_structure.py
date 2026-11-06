"""S13 结构解析顺序、表头章节路径与预览测试（虚构数据）。"""

import io
import zipfile

from docx import Document as DocxDocument

from app.ingestion import (DocumentInputError, extract_paragraphs,
                           extract_structured_blocks, preview_blocks,
                           split_structured_chunks, validate_document_bytes)


def docx_bytes() -> bytes:
    document = DocxDocument()
    document.add_heading("差旅制度", level=1)
    document.add_paragraph("适用对象为正式员工。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "费用项"
    table.cell(0, 1).text = "上限"
    table.cell(1, 0).text = "交通费"
    table.cell(1, 1).text = "180元"
    document.add_paragraph("交通费超过上限时须提前审批。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_blocks_keep_body_and_table_order():
    content = docx_bytes()
    validate_document_bytes(
        "travel.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content, 1_000_000,
    )
    blocks = extract_structured_blocks("travel.docx", content)
    assert [block["type"] for block in blocks] == ["heading", "paragraph", "table", "paragraph"]
    assert blocks[2]["table_header"] == "费用项 | 上限"
    assert "交通费 | 180元" in blocks[2]["text"]
    assert blocks[3]["section_path"] == ["差旅制度"]
    # 兼容旧调用仍按同一顺序扁平化。
    assert extract_paragraphs("travel.docx", content)[2:3] == ["费用项 | 上限\n交通费 | 180元"]


def test_structured_chunks_keep_table_header_and_conditions():
    blocks = extract_structured_blocks("travel.docx", docx_bytes())
    chunks = split_structured_chunks(blocks, revision_id="r1", size=1200, overlap=150)
    content = "\n".join(chunk["content"] for chunk in chunks)
    assert "费用项 | 上限" in content and "交通费 | 180元" in content
    assert "交通费超过上限时须提前审批。" in content
    assert any("table" in chunk["block_types"] for chunk in chunks)
    assert chunks[0]["section_path"] == ["差旅制度"]


def test_preview_is_bounded_and_does_not_mutate_blocks():
    blocks = extract_structured_blocks("travel.docx", docx_bytes())
    preview = preview_blocks(blocks, limit=2)
    assert len(preview) == 2
    preview[0]["text"] = "changed"
    assert blocks[0]["text"] == "差旅制度"


def test_scanned_pdf_without_text_is_explicitly_rejected():
    # 空内容不是有效 PDF 签名，验证不会把无法解析的扫描件当作成功。
    try:
        extract_structured_blocks("scan.pdf", b"%PDF-1.4\n%empty")
    except DocumentInputError as exc:
        assert "扫描件" in str(exc) or "解析" in str(exc)
    else:
        raise AssertionError("empty PDF should not parse")
