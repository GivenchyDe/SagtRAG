"""Phase 2 文档格式验证、文本提取与稳定分块。"""

import hashlib
import io
import re
import zipfile
from pathlib import PurePath

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}
PARSER_VERSION = "sagtrag-parser-2"
CHUNKER_VERSION = "structured-sections-1"


class DocumentInputError(ValueError):
    """表示上传文件格式、签名或内容不可处理。"""


def validate_document_bytes(filename: str, media_type: str, content: bytes, max_bytes: int) -> str:
    """校验扩展名、允许 MIME、大小和文件容器特征并返回规范化格式。"""
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise DocumentInputError("不支持此文件格式")
    if not content or len(content) > max_bytes:
        raise DocumentInputError("文件为空或超过上传大小限制")
    expected = {
        ".pdf": {"application/pdf", "application/octet-stream"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/octet-stream"},
        ".txt": {"text/plain", "application/octet-stream"},
        ".md": {"text/markdown", "text/plain", "application/octet-stream"},
        ".markdown": {"text/markdown", "text/plain", "application/octet-stream"},
    }[suffix]
    if media_type.lower().split(";", 1)[0].strip() not in expected:
        raise DocumentInputError("文件类型与扩展名不匹配")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise DocumentInputError("PDF 文件签名无效")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                total_size = sum(item.file_size for item in archive.infolist())
                if len(names) > 500 or total_size > max_bytes * 40 or "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise DocumentInputError("DOCX 容器结构无效或超出处理限制")
        except (zipfile.BadZipFile, OSError) as exc:
            raise DocumentInputError("DOCX 文件容器损坏") from exc
    if suffix in {".txt", ".md", ".markdown"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentInputError("文本文件必须使用 UTF-8 编码") from exc
    return suffix


def _heading_level(style_name: str) -> int:
    match = re.match(r"(?:Heading|标题)\s*([1-6])", style_name or "", re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _docx_blocks(content: bytes) -> list[dict[str, object]]:
    """按 body XML 顺序交错输出段落和表格，避免先全部段落再表格。"""
    document = DocxDocument(io.BytesIO(content))
    blocks: list[dict[str, object]] = []
    section: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            level = _heading_level(getattr(paragraph.style, "name", "") or "")
            if level:
                section = section[: level - 1] + [text]
            blocks.append({"type": "heading" if level else "paragraph", "text": text,
                           "level": level, "section_path": list(section) if level else list(section),
                           "page": None, "table_header": None})
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows = []
            for row in table.rows:
                value = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if value:
                    rows.append(value)
            if rows:
                blocks.append({"type": "table", "text": "\n".join(rows), "level": 0,
                               "section_path": list(section), "page": None,
                               "table_header": rows[0]})
    if not blocks:
        raise DocumentInputError("DOCX 文件中没有可提取文本")
    return blocks


def extract_structured_blocks(filename: str, content: bytes) -> list[dict[str, object]]:
    """提取带类型/页码/章节路径/表头的结构块，正文与表格保持原顺序。"""
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix == ".pdf":
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentInputError("加密 PDF 暂不支持")
            blocks = []
            for page_number, page in enumerate(reader.pages, 1):
                for part in (page.extract_text() or "").splitlines():
                    value = part.strip()
                    if value:
                        blocks.append({"type": "paragraph", "text": value, "level": 0,
                                       "section_path": [], "page": page_number,
                                       "table_header": None})
            if not blocks:
                raise DocumentInputError("PDF 未提取到可搜索文本，扫描件暂不支持 OCR")
            return blocks
        if suffix == ".docx":
            return _docx_blocks(content)
        text = content.decode("utf-8-sig")
        blocks = []
        section: list[str] = []
        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue
            match = re.match(r"^(#{1,6})\s+(.+)$", value)
            if match:
                level = len(match.group(1))
                section = section[: level - 1] + [match.group(2).strip()]
                blocks.append({"type": "heading", "text": match.group(2).strip(), "level": level,
                               "section_path": list(section), "page": None, "table_header": None})
            else:
                blocks.append({"type": "paragraph", "text": value, "level": 0,
                               "section_path": list(section), "page": None, "table_header": None})
        if not blocks:
            raise DocumentInputError("文本文件中没有内容")
        return blocks
    except DocumentInputError:
        raise
    except Exception as exc:
        raise DocumentInputError("文件无法解析或结构损坏") from exc


def extract_paragraphs(filename: str, content: bytes) -> list[str]:
    """兼容旧调用：结构化块扁平为文本段落。"""
    return [str(block["text"]) for block in extract_structured_blocks(filename, content)]


def preview_blocks(blocks: list[dict[str, object]], *, limit: int = 50) -> list[dict[str, object]]:
    """管理端解析预览只返回结构元数据和有限正文，不写日志。"""
    return [dict(block) for block in blocks[:max(1, min(limit, 200))]]


def _split_long_text(text: str, *, size: int, overlap: int) -> list[str]:
    """优先在句末切分；单句超长才退回窗口，避免拆散条件和例外。"""
    sentences = [item for item in re.split(r"(?<=[。！？；])", text) if item]
    if len(sentences) <= 1:
        return [text[start:start + size] for start in range(0, len(text), max(1, size - overlap))]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > size:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_split_long_text(sentence, size=size, overlap=overlap))
        elif current and len(current) + len(sentence) > size:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts


def split_structured_chunks(
    blocks: list[dict[str, object]], *, revision_id: str, size: int = 1200, overlap: int = 150,
) -> list[dict[str, object]]:
    """按章节/段落/表格完整块切分，表格表头随表进入同一块。"""
    chunks: list[str] = []
    metadata: list[dict[str, object]] = []
    current = ""
    current_meta: dict[str, object] = {"section_path": [], "block_types": []}
    for block in blocks:
        text = str(block["text"])
        path = list(block.get("section_path") or [])
        if block.get("type") == "heading":
            # 标题进入正文，后续chunk仍带章节路径，引用可定位。
            text = f"{text}"
        for part in _split_long_text(text, size=size, overlap=overlap) if len(text) > size else [text]:
            candidate = f"{current}\n\n{part}" if current else part
            if current and len(candidate) > size:
                chunks.append(current)
                metadata.append(current_meta)
                current = part
                current_meta = {"section_path": path, "block_types": [block.get("type")]}
            else:
                current = candidate
                if not current_meta["section_path"]:
                    current_meta["section_path"] = path
                if block.get("type") not in current_meta["block_types"]:
                    current_meta["block_types"].append(block.get("type"))
    if current:
        chunks.append(current)
        metadata.append(current_meta)
    return [
        {
            "ordinal": ordinal,
            "content": value,
            "chunk_key": hashlib.sha256(f"{revision_id}:{ordinal}:{value}".encode("utf-8")).hexdigest(),
            "section_path": metadata[ordinal]["section_path"],
            "block_types": metadata[ordinal]["block_types"],
        }
        for ordinal, value in enumerate(chunks)
    ]


def split_into_chunks(paragraphs: list[str], *, revision_id: str, size: int = 1200, overlap: int = 150) -> list[dict[str, object]]:
    """优先按段落边界合并，超长段落再按字符窗口重叠切分。"""
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(paragraph):
                end = min(start + size, len(paragraph))
                chunks.append(paragraph[start:end])
                if end == len(paragraph):
                    break
                start = end - overlap
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > size and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [
        {
            "ordinal": ordinal,
            "content": value,
            "chunk_key": hashlib.sha256(f"{revision_id}:{ordinal}:{value}".encode("utf-8")).hexdigest(),
        }
        for ordinal, value in enumerate(chunks)
    ]
