"""完整简历的渲染与导出：HTML（预览和导出共用一个模板）、Markdown、DOCX（01 F5）。

两种模式：
- 预览：显示「待处理」「待检查」「请核对」标记，待处理的条目也显示出来，提醒用户处理；
- 导出：不带依据标记，待处理的条目整条略过（它的文字没有通过检查，不能进成品）。
三种格式都从同一份结构化内容生成，包含的内容一致。PDF 由浏览器打印 HTML 得到。
"""

import io
from enum import StrEnum
from uuid import UUID

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.shared import Mm, Pt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import CheckStatus
from jobpilot.services.resume_content import BOLD, Bullet, ResumeContent

TEMPLATES = REPO_ROOT / "jobpilot" / "templates"
_ENV = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),  # 简历文字是用户输入，一律转义
    trim_blocks=True,
    lstrip_blocks=True,
)


def rich(text: str) -> Markup:
    """先整体转义，再把 **加粗** 换成 <strong>：用户文字里的标签不会生效。"""
    return Markup(BOLD.sub(r"<strong>\1</strong>", str(escape(text))))


_ENV.filters["rich"] = rich


def _add_rich(paragraph, text: str) -> None:
    """按 **加粗** 拆成几段 run，加粗的那段设为粗体。"""
    position = 0
    for match in BOLD.finditer(text):
        if match.start() > position:
            paragraph.add_run(text[position : match.start()])
        paragraph.add_run(match.group(1)).bold = True
        position = match.end()
    if position < len(text):
        paragraph.add_run(text[position:])


class RenderMode(StrEnum):
    PREVIEW = "preview"
    EXPORT = "export"


def exported_bullets(bullets: list[Bullet]) -> list[Bullet]:
    """进成品的条目：待处理的不要。"""
    return [b for b in bullets if b.origin is not CheckStatus.PENDING]


def _visible(content: ResumeContent, mode: RenderMode) -> set[UUID]:
    return {
        bullet.bullet_id
        for section in content.sections
        for entry in section.entries
        for bullet in (
            entry.bullets
            if mode is RenderMode.PREVIEW
            else exported_bullets(entry.bullets)
        )
    }


def title_of(content: ResumeContent) -> str:
    return f"{content.header[0]} 的简历" if content.header else "简历"


def render_html(
    content: ResumeContent,
    *,
    mode: RenderMode,
    version: int,
    editor_script: str | None = None,
) -> str:
    """单个 HTML 文件：样式内嵌，不依赖网络；editor_script 给出时一并内嵌。"""
    return _ENV.get_template("resume.html").render(
        title=title_of(content),
        version=version,
        header=content.header,
        sections=content.sections,
        visible=_visible(content, mode),
        marks=mode is RenderMode.PREVIEW,
        editor_script=editor_script,
    )


def to_markdown(content: ResumeContent) -> str:
    """纯文字备份：页头、## 栏目、### 经历（名称 · 时间）、正文和条目。"""
    lines: list[str] = []
    if content.header:
        lines += [f"# {content.header[0]}", "", *content.header[1:], ""]
    for section in content.sections:
        if section.title:
            lines += [f"## {section.title}", ""]
        for entry in section.entries:
            heading = entry.title + (f" · {entry.time}" if entry.time else "")
            lines += [f"### {heading}", ""]
            lines += [*entry.lines]
            lines += [f"- {b.text}" for b in exported_bullets(entry.bullets)]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_docx(content: ResumeContent) -> bytes:
    """Word 文档：A4，名称靠左、时间用右对齐制表位放在同一行。"""
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Mm(16))
    right_edge = section.page_width - section.left_margin - section.right_margin

    if content.header:
        name = document.add_paragraph()
        run = name.add_run(content.header[0])
        run.bold, run.font.size = True, Pt(18)
        for line in content.header[1:]:
            _add_rich(document.add_paragraph(), line)
    for part in content.sections:
        if part.title:
            document.add_heading(part.title, level=2)
        for entry in part.entries:
            head = document.add_paragraph()
            head.paragraph_format.tab_stops.add_tab_stop(
                right_edge, WD_TAB_ALIGNMENT.RIGHT
            )
            head.add_run(entry.title).bold = True
            if entry.time:
                head.add_run(f"\t{entry.time}")
            for line in entry.lines:
                _add_rich(document.add_paragraph(), line)
            for bullet in exported_bullets(entry.bullets):
                _add_rich(document.add_paragraph(style="List Bullet"), bullet.text)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
