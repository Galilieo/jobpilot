"""完整简历的渲染与导出：HTML（预览和导出共用一个模板）、Markdown、DOCX（01 F5）。

两种模式：
- 预览：显示「待处理」「待检查」「请核对」标记，待处理的条目也显示出来，提醒用户处理；
- 导出：不带依据标记，待处理的条目整条略过（它的文字没有通过检查，不能进成品）。
两种 HTML 都内嵌编辑器和一份数据：预览页嵌完整内容（保存时要发回服务器），
导出文件只嵌看得见的文字和版式（OfflineContent），发出去的成品不带依据。
三种格式都从同一份结构化内容生成，包含的内容一致。PDF 由浏览器打印 HTML 得到。
"""

import io
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.shared import Mm, Pt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from pydantic import BaseModel, Field

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import CheckStatus
from jobpilot.services.resume_content import BOLD, Bullet, ResumeContent, ResumeStyle
from jobpilot.services.resume_fonts import default_fonts

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


class OfflineBullet(BaseModel):
    """导出文件里带上的一条条目：只有 ID 和文字。"""

    bullet_id: UUID  # 和页面上 li[data-bullet-id] 对应，编辑器收回文字时用
    text: str


class OfflineEntry(BaseModel):
    """导出文件里带上的一段经历：标题行、没改写的正文和进成品的条目。"""

    entry_id: str
    title: str
    time: str | None = None
    lines: list[str] = Field(default_factory=list)
    bullets: list[OfflineBullet] = Field(default_factory=list)


class OfflineSection(BaseModel):
    entries: list[OfflineEntry]


class OfflineContent(BaseModel):
    """导出文件里内嵌给编辑器的数据：只放看得见的文字和版式。

    应用里的预览要把整份内容发回服务器保存，所以嵌完整内容。导出的文件是要发出去的
    成品，不连服务器，编辑器只用到把页面文字收回成结构化内容时要对照的部分
    （resume_editor.js 的 collect）。引用的事实、检查结论、待处理的条目都不嵌。
    """

    header: list[str]
    sections: list[OfflineSection]
    style: ResumeStyle

    @classmethod
    def of(cls, content: ResumeContent) -> Self:
        """取出看得见、用得到的部分；待处理的条目不进成品，也不进这里。"""
        return cls(
            header=content.header,
            style=content.style,
            sections=[
                OfflineSection(
                    entries=[
                        OfflineEntry(
                            entry_id=entry.entry_id,
                            title=entry.title,
                            time=entry.time,
                            lines=entry.lines,
                            bullets=[
                                OfflineBullet(bullet_id=b.bullet_id, text=b.text)
                                for b in exported_bullets(entry.bullets)
                            ],
                        )
                        for entry in section.entries
                    ]
                )
                for section in content.sections
            ],
        )


@dataclass(frozen=True)
class EditorContext:
    """内嵌编辑器需要的信息。save_url 为空表示离线文件，修改保存在浏览器本地。"""

    version_id: UUID
    storage_key: str  # 本地保存和草稿用的键，区分不同简历和版本
    save_url: str | None = None
    export_url: str | None = None
    reset_url: str | None = None  # 回到系统生成的版本：服务器把第一版复制成新版本
    use_facts_url: str | None = None  # 含 {bullet_id} 占位，改用服务端记录的事实原文

    @property
    def online(self) -> bool:
        """在应用里打开：保存要发回服务器。编辑器也是按 save_url 判断的。"""
        return self.save_url is not None


EDITOR_SCRIPT = (TEMPLATES / "resume_editor.js").read_text(encoding="utf-8")


def _editor_json(content: ResumeContent, version: int, editor: EditorContext) -> Markup:
    """嵌在 <script type="application/json"> 里的数据。

    预览页（应用里）嵌整份内容；导出的离线文件只嵌看得见的文字和版式。
    「<」写成 \\u003c：用户文字里的「</script>」不会提前结束脚本标签。
    """
    embedded = content if editor.online else OfflineContent.of(content)
    data = {
        "content": embedded.model_dump(mode="json"),
        "version": version,
        "version_id": str(editor.version_id),
        "storage_key": editor.storage_key,
        "save_url": editor.save_url,
        "export_url": editor.export_url,
        "reset_url": editor.reset_url,
        "use_facts_url": editor.use_facts_url,
        "fonts": default_fonts().editor_options(),
    }
    return Markup(json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))


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
    editor: EditorContext | None = None,
    csp_nonce: str | None = None,
) -> str:
    """单个 HTML 文件：样式内嵌，不依赖网络；给出 editor 时内嵌编辑器和数据。"""
    return _ENV.get_template("resume.html").render(
        title=title_of(content),
        version=version,
        header=content.header,
        sections=content.sections,
        style=content.style,
        font_css=default_fonts().css_of(content.style.font),
        visible=_visible(content, mode),
        marks=mode is RenderMode.PREVIEW,
        editor_json=_editor_json(content, version, editor) if editor else None,
        editor_script=Markup(EDITOR_SCRIPT) if editor else None,
        csp_nonce=csp_nonce,
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
