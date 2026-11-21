"""把上传的简历和 JD 文件变成纯文本；只做格式转换，不连数据库、不调用模型。

extract_text 处理 TXT、Markdown、HTML、DOCX 和文字版 PDF。图片和提取不到文字的 PDF
抛出 NeedsOcrError，改走 ocr_pages：在本机用 RapidOCR 识别，识别结果经用户核对后再提交。
所有结果都经过 normalize_text，粘贴的文本也走同一个函数，保证引用位置按同一份文本计算。
"""

import re
import threading
from functools import cache
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePath

import numpy as np
import pypdfium2 as pdfium
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from rapidocr import RapidOCR

TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
HTML_SUFFIXES = frozenset({".html", ".htm"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})

MAX_PDF_PAGES = 20
# 平均每页可见字符少于这个数，就当作扫描件：扫描件常带页码或水印，不能只判断是否为空。
_MIN_CHARS_PER_PAGE = 30

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"  # DOCX 本质是 zip 包

# NUL 不能存进 PostgreSQL 的 text；零宽字符看不见，却会让原文片段对不上。
_INVISIBLE = dict.fromkeys(map(ord, "\x00\u200b\u200c\u200d\u2060\ufeff"))
_TRAILING_SPACES = re.compile(r"[ \t\u3000]+\n")
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(ValueError):
    """文件无法直接转成文本；子类说明原因，接口层据此返回不同提示。"""


class UnsupportedFormatError(ParseError):
    """扩展名不在支持范围内，或文件内容与扩展名不符。"""


class UnreadableFileError(ParseError):
    """格式支持，但文件损坏、加密或页数超限。"""


class NeedsOcrError(ParseError):
    """图片或扫描版 PDF，需要先 OCR 并由用户核对。"""


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名选择解析方式，返回规范化后的纯文本。"""
    suffix = PurePath(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return normalize_text(_decode(data))
    if suffix in HTML_SUFFIXES:
        return normalize_text(html_to_text(_decode(data)))
    if suffix == ".docx":
        _require_magic(data, _ZIP_MAGIC, "DOCX")
        return normalize_text(_docx_to_text(data))
    if suffix == ".pdf":
        _require_magic(data, _PDF_MAGIC, "PDF")
        return normalize_text(_pdf_to_text(data))
    if suffix in IMAGE_SUFFIXES:
        raise NeedsOcrError("图片需要先识别文字")
    raise UnsupportedFormatError(f"不支持的文件类型：{suffix or '无扩展名'}")


def normalize_text(text: str) -> str:
    """统一换行、去掉不可见字符和行尾空白，连续空行最多保留一行。

    不改动可见字符（不做全角转半角），保证用户核对时看到的就是最终保存的原文。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE)
    text = _TRAILING_SPACES.sub("\n", text + "\n")
    return _EXTRA_BLANK_LINES.sub("\n\n", text).strip()


def _decode(data: bytes) -> str:
    """优先按 UTF-8（可带 BOM）解码；失败时按 GB18030，兼容中文 Windows 保存的文件。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError as error:
        raise UnreadableFileError("文本文件编码无法识别") from error


def _require_magic(data: bytes, magic: bytes, label: str) -> None:
    """检查文件头，防止改了扩展名的文件被送进不匹配的解析器。"""
    if not data.startswith(magic):
        raise UnsupportedFormatError(f"文件内容不是 {label}")


# ---------- HTML ----------

# 这些标签里的内容不是给人看的正文，整体跳过。
_SKIPPED_TAGS = frozenset({"script", "style", "head", "template", "noscript"})
# 块级标签前后换行，避免相邻段落的文字粘在一起。
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul",
    }
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})
_WHITESPACE_RUN = re.compile(r"[ \t\n]+")


class _TextCollector(HTMLParser):
    """逐个接收标签和文字：跳过脚本与样式，块级标签处换行，表格单元格用制表符分隔。

    convert_charrefs=True 让 &amp;、&lt; 等实体先还原成字符再交给 handle_data。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._break_line()
        elif tag in _CELL_TAGS and self._current:
            self._current.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._break_line()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            # HTML 里源码的换行和缩进只是排版，浏览器显示时会合并成一个空格。
            self._current.append(_WHITESPACE_RUN.sub(" ", data))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 这种自闭合写法默认会同时触发开始和结束，这里只按开始处理一次。
        self.handle_starttag(tag, attrs)

    def _break_line(self) -> None:
        line = "".join(self._current).strip(" ")
        if line:
            self.lines.append(line)
        self._current = []

    def text(self) -> str:
        self._break_line()
        return "\n".join(self.lines)


def html_to_text(html: str) -> str:
    """去掉标签取正文；用标准库的 HTML 解析器，而不是正则删 <...>。"""
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return collector.text()


# ---------- DOCX ----------


def _docx_to_text(data: bytes) -> str:
    """按文档顺序取段落和表格；表格每行一行，单元格之间用制表符分隔。"""
    try:
        document = Document(BytesIO(data))
    except Exception as error:  # python-docx 对损坏文件会抛多种底层异常
        raise UnreadableFileError("DOCX 文件无法打开") from error

    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            lines.append(block.text)
        elif isinstance(block, Table):
            lines.extend(_table_rows(block))
    return "\n".join(lines)


def _table_rows(table: Table) -> list[str]:
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        previous = None
        for cell in row.cells:
            # 合并单元格在 python-docx 里会重复返回同一个单元格，只取一次。
            if cell._tc is not previous:
                cells.append(cell.text)
            previous = cell._tc
        rows.append("\t".join(cells))
    return rows


# ---------- PDF ----------


def _pdf_to_text(data: bytes) -> str:
    """逐页提取文字；几乎没有文字时判定为扫描件，抛出 NeedsOcrError。"""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnreadableFileError("PDF 已加密，请先解除密码")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise UnreadableFileError(f"PDF 超过 {MAX_PDF_PAGES} 页")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ParseError:
        raise
    except Exception as error:  # 畸形 PDF 除了 PyPdfError 还可能抛 KeyError 等
        raise UnreadableFileError("PDF 文件无法解析") from error

    visible_chars = sum(len("".join(page.split())) for page in pages)
    if visible_chars < _MIN_CHARS_PER_PAGE * max(page_count, 1):
        raise NeedsOcrError("PDF 中几乎没有可提取的文字，可能是扫描件")
    return "\n\n".join(pages)


# ---------- OCR：图片与扫描版 PDF ----------

MAX_OCR_IMAGES = 10
# 手机长截图约 1170×20000，约 2300 万像素；再大的图多半是异常文件或解压炸弹。
MAX_IMAGE_PIXELS = 40_000_000
_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
_PDF_RENDER_SCALE = 2  # 72 dpi × 2，A4 页渲染成约 1190×1684 像素

# RapidOCR 会把最长边缩到 2000 像素再检测文字。实测宽 1170 的截图高 6000 仍能全部识别，
# 高 12000 时一行都认不出，所以高图先切成高度不超过宽度 3 倍的几段。
_MAX_SEGMENT_RATIO = 3
_MIN_SEGMENT_HEIGHT = 2000
# 切点优先选在颜色一致的行（行内最亮与最暗相差小于这个值），避免把一行字切成两半。
_BLANK_ROW_TOLERANCE = 10

# onnxruntime 每次识别会占满全部 CPU 核心；pdfium 不是线程安全的。
# 所以同一时间只处理一个文件，多个请求排队。
_OCR_LOCK = threading.Lock()


def ocr_pages(filename: str, data: bytes) -> list[str]:
    """识别一个图片或扫描版 PDF 文件，每张图片或每页 PDF 返回一段文本。

    识别慢且占 CPU，接口层应放到线程里调用，不要直接在事件循环里执行。
    """
    suffix = PurePath(filename).suffix.lower()
    if suffix not in IMAGE_SUFFIXES and suffix != ".pdf":
        raise UnsupportedFormatError(f"OCR 只支持图片和 PDF：{suffix or '无扩展名'}")
    with _OCR_LOCK:
        if suffix == ".pdf":
            _require_magic(data, _PDF_MAGIC, "PDF")
            images = _render_pdf(data)
        else:
            images = [load_image(data)]
        return [_ocr_image(image) for image in images]


def load_image(data: bytes) -> Image.Image:
    """按文件内容（不是扩展名）确认图片格式，检查尺寸，按 EXIF 摆正方向。"""
    try:
        image = Image.open(BytesIO(data))
    except UnidentifiedImageError as error:
        raise UnsupportedFormatError("文件内容不是 PNG、JPEG 或 WebP 图片") from error
    if image.format not in _IMAGE_FORMATS:
        raise UnsupportedFormatError(f"不支持的图片格式：{image.format}")
    # Image.open 只读文件头，这里在解码像素之前拦下超大图片。
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise UnreadableFileError("图片尺寸过大")
    try:
        # 手机照片常把方向写在 EXIF 里，不摆正的话文字是横着的。
        return ImageOps.exif_transpose(image).convert("RGB")
    except OSError as error:  # 文件头正常但像素数据损坏
        raise UnreadableFileError("图片已损坏") from error


def _ocr_image(image: Image.Image) -> str:
    """识别一张图片；高图分段识别后按顺序拼接。调用方需持有 _OCR_LOCK。"""
    lines: list[str] = []
    for segment in split_tall_image(image):
        result = _engine()(np.asarray(segment))
        if result.txts:
            lines.extend(group_lines(result.boxes.tolist(), list(result.txts)))
    return normalize_text("\n".join(lines))


def split_tall_image(image: Image.Image) -> list[Image.Image]:
    """把过高的图片按空白行切成几段；不高的图片原样返回。"""
    limit = max(image.width * _MAX_SEGMENT_RATIO, _MIN_SEGMENT_HEIGHT)
    if image.height <= limit:
        return [image]

    gray = np.asarray(image.convert("L"), dtype=np.int16)
    blank_rows = (gray.max(axis=1) - gray.min(axis=1)) < _BLANK_ROW_TOLERANCE
    segments: list[Image.Image] = []
    top = 0
    while image.height - top > limit:
        cut = _blank_row_near(blank_rows, low=top + limit // 2, high=top + limit)
        segments.append(image.crop((0, top, image.width, cut)))
        top = cut
    segments.append(image.crop((0, top, image.width, image.height)))
    return segments


def _blank_row_near(blank_rows: np.ndarray, low: int, high: int) -> int:
    """在 [low, high) 内从下往上找最靠下的空白行；找不到就在 high 处硬切。"""
    candidates = np.flatnonzero(blank_rows[low:high])
    return low + int(candidates[-1]) if candidates.size else high


_Box = tuple[float, float, float, str]  # (上边 y, 下边 y, 左边 x, 文字)


def group_lines(boxes: list[list[list[float]]], texts: list[str]) -> list[str]:
    """把 RapidOCR 的文字框按行合并。

    同一行左右两块文字（如「公司名」和「2024.07」）是两个框，竖直方向重叠超过一半就算
    同一行，从左到右用空格连接。每个框是四个角的坐标：左上、右上、右下、左下。
    """
    items: list[_Box] = []
    for box, text in zip(boxes, texts, strict=True):
        ys = [y for _, y in box]
        items.append((min(ys), max(ys), box[0][0], text))
    items.sort(key=lambda item: (item[0], item[2]))  # 先按上边，再按左边

    rows: list[list[_Box]] = []
    for item in items:
        if rows and _same_row(rows[-1], item):
            rows[-1].append(item)
        else:
            rows.append([item])
    return [
        " ".join(text for *_, text in sorted(row, key=lambda item: item[2]))
        for row in rows
    ]


def _same_row(row: list[_Box], item: _Box) -> bool:
    row_top = min(top for top, *_ in row)
    row_bottom = max(bottom for _, bottom, *_ in row)
    top, bottom, *_ = item
    overlap = min(row_bottom, bottom) - max(row_top, top)
    return overlap > 0.5 * min(row_bottom - row_top, bottom - top)


def _render_pdf(data: bytes) -> list[Image.Image]:
    """把扫描版 PDF 的每一页渲染成图片。"""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise UnreadableFileError("PDF 文件无法解析或已加密") from error
    try:
        if len(document) > MAX_PDF_PAGES:
            raise UnreadableFileError(f"PDF 超过 {MAX_PDF_PAGES} 页")
        return [
            page.render(scale=_PDF_RENDER_SCALE).to_pil().convert("RGB")
            for page in document
        ]
    finally:
        document.close()


@cache
def _engine() -> RapidOCR:
    """第一次识别时才加载模型，之后复用同一个引擎；模型文件随 rapidocr 安装包自带。"""
    return RapidOCR(params={"Global.log_level": "warning"})
