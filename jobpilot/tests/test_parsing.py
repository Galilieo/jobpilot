"""文字提取是纯函数：输入文件名和字节，输出纯文本或说明原因的异常。

DOCX 和 PDF 在测试里现场生成，不往仓库放二进制样例，也不会混进真实简历。
"""

from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter

from jobpilot.services.parsing import (
    MAX_PDF_PAGES,
    NeedsOcrError,
    UnreadableFileError,
    UnsupportedFormatError,
    extract_text,
    html_to_text,
    normalize_text,
)

# ---------- 规范化：粘贴文本和所有格式共用 ----------


def test_normalize_unifies_newlines_and_removes_invisible_characters() -> None:
    raw = "﻿项目：课程排班\r\n负责​筛选功能  \r\n\r\n\r\n\r\n技能\x00：Python\t\n"

    assert normalize_text(raw) == "项目：课程排班\n负责筛选功能\n\n技能：Python"


def test_normalize_keeps_visible_characters_unchanged() -> None:
    """全角数字和符号保持原样：用户核对时看到什么，保存的就是什么。"""
    assert normalize_text("提升３０％，ＡＰＩ") == "提升３０％，ＡＰＩ"


# ---------- TXT / Markdown ----------


@pytest.mark.parametrize("filename", ["resume.txt", "简历.MD", "notes.markdown"])
def test_plain_text_formats_are_read_as_utf8(filename: str) -> None:
    data = "﻿# 项目经历\n- 实现按周次筛选".encode()

    assert extract_text(filename, data) == "# 项目经历\n- 实现按周次筛选"


def test_text_falls_back_to_gb18030() -> None:
    """中文 Windows 记事本的「ANSI」实际是 GBK，UTF-8 解码会失败。"""
    data = "课程排班面板".encode("gb18030")

    assert extract_text("resume.txt", data) == "课程排班面板"


# ---------- HTML ----------


def test_html_drops_scripts_styles_and_head() -> None:
    """本项目导出的 HTML 简历内嵌编辑脚本，脚本里的字符串不能混进正文。"""
    html = """<!doctype html><html>
      <head><title>简历</title><style>p { color: red; }</style></head>
      <body>
        <h1>张三</h1>
        <p>负责 <b>课程排班</b>
           面板的筛选功能</p>
        <script>const tip = "</p>这不是正文";</script>
      </body></html>"""

    assert html_to_text(html) == "张三\n负责 课程排班 面板的筛选功能"


def test_html_decodes_entities_and_separates_blocks() -> None:
    html = "<ul><li>A &amp; B</li><li>1 &lt; 2</li></ul>第一行<br/>第二行<br>第三行"

    assert html_to_text(html) == "A & B\n1 < 2\n第一行\n第二行\n第三行"


def test_html_table_cells_are_tab_separated() -> None:
    html = (
        "<table><tr><th>时间</th><th>经历</th></tr>"
        "<tr><td>2025</td><td>实习</td></tr></table>"
    )

    assert html_to_text(html) == "时间\t经历\n2025\t实习"


def test_html_file_goes_through_normalize() -> None:
    data = "<p>项目​经历</p>".encode()

    assert extract_text("resume.html", data) == "项目经历"


# ---------- DOCX ----------


def make_docx() -> bytes:
    document = Document()
    document.add_paragraph("项目经历")
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "时间"
    table.cell(0, 1).merge(table.cell(0, 2)).text = "经历"
    table.cell(1, 0).text = "2025"
    table.cell(1, 1).text = "课程排班"
    table.cell(1, 2).text = "负责"
    document.add_paragraph("技能：Python")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_keeps_paragraph_and_table_order() -> None:
    """表格在两个段落之间，提取结果也在中间；合并单元格只出现一次。"""
    assert extract_text("resume.docx", make_docx()) == (
        "项目经历\n时间\t经历\n2025\t课程排班\t负责\n技能：Python"
    )


def test_corrupted_docx_is_unreadable() -> None:
    with pytest.raises(UnreadableFileError):
        extract_text("resume.docx", b"PK\x03\x04 not really a zip")


# ---------- PDF ----------


def make_text_pdf(*lines: str) -> bytes:
    """手写一个最小的单页 PDF：Helvetica 字体只支持 ASCII，够测提取流程。"""
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    ops += [f"({line}) Tj T*" for line in lines]
    ops.append("ET")
    content = "\n".join(ops).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(pdf)


def make_blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_text_pdf_is_extracted() -> None:
    data = make_text_pdf(
        "Course Scheduler Dashboard", "Built weekly filters with FastAPI"
    )

    text = extract_text("resume.pdf", data)

    assert "Course Scheduler Dashboard" in text
    assert "Built weekly filters with FastAPI" in text


def test_pdf_without_text_needs_ocr() -> None:
    with pytest.raises(NeedsOcrError):
        extract_text("scan.pdf", make_blank_pdf(1))


def test_pdf_with_only_a_page_number_still_needs_ocr() -> None:
    """扫描件常带页码或水印：只有零星几个字也按扫描件处理。"""
    with pytest.raises(NeedsOcrError):
        extract_text("scan.pdf", make_text_pdf("Page 1"))


def test_pdf_over_page_limit_is_rejected() -> None:
    with pytest.raises(UnreadableFileError, match="页"):
        extract_text("long.pdf", make_blank_pdf(MAX_PDF_PAGES + 1))


def test_broken_pdf_is_unreadable() -> None:
    with pytest.raises(UnreadableFileError):
        extract_text("resume.pdf", b"%PDF-1.4\nthis is not a pdf body")


# ---------- 分流：图片转 OCR，其他格式拒绝 ----------


@pytest.mark.parametrize("filename", ["shot.png", "shot.JPG", "shot.jpeg", "a.webp"])
def test_images_need_ocr(filename: str) -> None:
    with pytest.raises(NeedsOcrError):
        extract_text(filename, b"\x89PNG")


@pytest.mark.parametrize("filename", ["resume.doc", "resume.pages", "resume"])
def test_unsupported_formats_are_rejected(filename: str) -> None:
    with pytest.raises(UnsupportedFormatError):
        extract_text(filename, b"anything")


@pytest.mark.parametrize(
    ("filename", "data"),
    [("resume.pdf", b"PK\x03\x04..."), ("resume.docx", b"%PDF-1.4...")],
    ids=["zip-renamed-to-pdf", "pdf-renamed-to-docx"],
)
def test_content_must_match_extension(filename: str, data: bytes) -> None:
    with pytest.raises(UnsupportedFormatError, match="文件内容不是"):
        extract_text(filename, data)
