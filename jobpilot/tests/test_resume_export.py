"""渲染与导出：预览带标记、导出不带；待处理的条目不进成品；三种格式内容一致；转义。"""

import io
from uuid import uuid4

import pytest
from docx import Document

from jobpilot.db.models import CheckStatus
from jobpilot.services.grounding import GroundingIssue, IssueKind
from jobpilot.services.resume_content import ResumeContent, assemble, bullet_of
from jobpilot.services.resume_export import (
    EditorContext,
    RenderMode,
    render_html,
    rich,
    to_docx,
    to_markdown,
)
from jobpilot.tests.test_resume_content import RAW, base_sections, full_plan

PENDING = "主导月结清单重构，性能提升 10 倍"
INJECTED = "<script>alert(1)</script>负责导出"


def content() -> ResumeContent:
    built = assemble(RAW, base_sections(), full_plan())
    work = built.sections[0].entries[0]
    issue = GroundingIssue(
        kind=IssueKind.ROLE, message="「主导」高于职责", fragment="主导"
    )
    work.bullets += [
        bullet_of(uuid4(), PENDING, [uuid4()], CheckStatus.PENDING, [issue]),
        bullet_of(uuid4(), INJECTED, [uuid4()], CheckStatus.PASSED, []),
    ]
    return built


def docx_text(data: bytes) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def exports(built: ResumeContent) -> dict[str, str]:
    return {
        "html": render_html(built, mode=RenderMode.EXPORT, version=1),
        "md": to_markdown(built),
        "docx": docx_text(to_docx(built)),
    }


@pytest.mark.parametrize("kind", ["html", "md", "docx"])
def test_exports_skip_pending_bullets_and_keep_the_rest(kind):
    text = exports(content())[kind]
    assert PENDING not in text
    for expected in (
        "唐思齐",
        "工作经历",
        "负责月结清单接口",
        "2024.07 至今",
        "宁沅工学院",
    ):
        assert expected in text


def content_with_marks() -> ResumeContent:
    """再加两种会带标记的条目：改过没检查的、检查提示了问题的。"""
    built = content()
    stale = built.sections[0].entries[0].bullets[0]
    stale.text = "改过但还没检查"
    flagged = built.sections[1].entries[0].bullets[0]  # 手改后第二层提示了问题
    flagged.check = flagged.check.model_copy(
        update={
            "issues": [GroundingIssue(kind=IssueKind.NUMBER, message="数字「3」找不到")]
        }
    )
    return built


def test_preview_marks_pending_stale_and_flagged_bullets():
    html = render_html(content_with_marks(), mode=RenderMode.PREVIEW, version=3)
    assert PENDING in html and "待处理" in html
    assert "待检查" in html
    assert "请核对" in html and "数字「3」找不到" in html
    assert 'data-resume-version="3"' in html


def test_export_html_has_no_evidence_marks():
    html = render_html(content_with_marks(), mode=RenderMode.EXPORT, version=3)
    body = html.split("</style>", 1)[1]
    for mark in ("待处理", "待检查", "请核对", "mark-"):
        assert mark not in body


def test_user_text_is_escaped():
    html = render_html(content(), mode=RenderMode.EXPORT, version=1)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;负责导出" in html


def test_markdown_structure():
    md = to_markdown(content())
    assert md.startswith("# 唐思齐\n")
    assert "\n## 工作经历\n" in md
    assert "\n### 砚桥商服软件  /  Java 开发工程师 · 2024.07 至今\n" in md
    assert "\n- 负责月结清单接口\n" in md


def test_export_html_is_a_single_self_contained_file():
    html = exports(content())["html"]
    assert "<style>" in html
    for external in ('src="http', 'href="http', "<link"):
        assert external not in html


def test_bold_markers_render_as_strong_after_escaping():
    html = str(rich("负责**运单<查询>**接口"))
    assert html == "负责<strong>运单&lt;查询&gt;</strong>接口"


def test_docx_turns_bold_markers_into_bold_runs():
    built = content()
    built.sections[0].entries[0].bullets[0].text = "负责**月结清单**接口"
    document = Document(io.BytesIO(to_docx(built)))
    runs = [
        (run.text, bool(run.bold))
        for paragraph in document.paragraphs
        if paragraph.text == "负责月结清单接口"
        for run in paragraph.runs
    ]
    assert runs == [("负责", False), ("月结清单", True), ("接口", False)]


def test_saved_style_is_applied_to_the_page():
    built = content()
    built.style = built.style.model_copy(
        update={"font": '"Songti SC", serif', "size_pt": 9.5, "leading": 1.35}
    )
    html = render_html(built, mode=RenderMode.EXPORT, version=1)
    assert "--size: 9.5pt; --leading: 1.35;" in html
    # 字体名也是用户可改的文字，引号要转义，不能闭合 style 属性
    assert "--font: &#34;Songti SC&#34;, serif;" in html


def test_editor_data_cannot_close_its_script_tag():
    built = content()
    built.header[0] = "</script><script>alert(1)</script>"
    editor = EditorContext(version_id=uuid4(), storage_key="r:v1")
    html = render_html(built, mode=RenderMode.EXPORT, version=1, editor=editor)
    data = html.split('<script type="application/json" id="jobpilot-resume">', 1)[1]
    data = data.split("</script>", 1)[0]
    assert "<" not in data and "\\u003c/script>" in data
    assert '"save_url": null' in data  # 离线文件存本地，不连服务器
