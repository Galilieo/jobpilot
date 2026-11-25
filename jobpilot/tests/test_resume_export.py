"""渲染与导出：预览带标记、导出不带；待处理的条目不进成品；三种格式内容一致；转义。

导出的 HTML 带离线编辑器，内嵌一份数据：发出去的成品里只能有看得见的文字和版式。
"""

import io
import json
import re
from uuid import UUID, uuid4

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


def export_file(built: ResumeContent) -> str:
    """和下载接口给出的文件一样：导出模式，带离线编辑器。"""
    return render_html(
        built,
        mode=RenderMode.EXPORT,
        version=3,
        editor=EditorContext(version_id=uuid4(), storage_key="r:v3"),
    )


def embedded(html: str) -> dict:
    """取出文件里内嵌给编辑器的数据。"""
    data = html.split('<script type="application/json" id="jobpilot-resume">', 1)[1]
    return json.loads(data.split("</script>", 1)[0])


PENDING_MARK = "ZZ_PENDING_MARK"
ISSUE_MARK = "ZZ_ISSUE_MARK"


def content_with_private_marks() -> tuple[ResumeContent, UUID]:
    """工作经历下有三条：通过的、待处理的（原文和检查问题里各有一个标记）、改过的。"""
    built = content()
    fact_id = uuid4()
    issue = GroundingIssue(
        kind=IssueKind.ROLE, message=f"{ISSUE_MARK}：「主导」高于职责", fragment="主导"
    )
    built.sections[0].entries[0].bullets[1] = bullet_of(
        uuid4(), f"{PENDING_MARK} 主导重构", [fact_id], CheckStatus.PENDING, [issue]
    )
    built.style = built.style.model_copy(
        update={"font": "songti", "size_pt": 9.5, "leading": 1.35}
    )
    return built, fact_id


def test_export_file_embeds_only_visible_text_and_style():
    """内嵌的数据只放看得见的文字、版式，和编辑器对回页面文字要用的 ID。"""
    built, _ = content_with_private_marks()
    data = embedded(export_file(built))
    content = data["content"]

    assert set(data) == {
        "content",
        "version",
        "version_id",
        "storage_key",
        "save_url",
        "export_url",
        "reset_url",
        "use_facts_url",
        "fonts",
    }
    assert data["version"] == 3  # 编辑器按版本号导出：导出的是它看到的这一版
    assert set(content) == {"header", "sections", "style"}
    assert content["header"] == built.header
    assert content["style"] == {
        "font": "songti",
        "size_pt": 9.5,
        "leading": 1.35,
    }
    entries = {e["entry_id"]: e for s in content["sections"] for e in s["entries"]}
    # 改写过的经历：标题行、时间，待处理的那条不在，进成品的两条只有 ID 和文字
    work = entries["e1"]
    assert set(work) == {"entry_id", "title", "time", "lines", "bullets"}
    assert (work["title"], work["time"], work["lines"]) == (
        "砚桥商服软件  /  Java 开发工程师",
        "2024.07 至今",
        [],
    )
    assert [b["text"] for b in work["bullets"]] == ["负责月结清单接口", INJECTED]
    assert all(set(b) == {"bullet_id", "text"} for b in work["bullets"])
    # 没改写的经历：底稿原文的每一行都在
    voucher = built.sections[1].entries[1]
    assert entries["e3"]["lines"] == voucher.lines
    assert entries["e3"]["lines"][0].startswith("- 参与对接财务方")


def test_export_file_has_no_pending_text_anywhere():
    """打开源码也看不到：待处理条目的原文、检查问题的说明、引用的事实，整份文件里都没有。"""
    built, fact_id = content_with_private_marks()
    html = export_file(built)
    for private in (PENDING_MARK, ISSUE_MARK, str(fact_id)):
        assert private not in html
    assert "「主导」高于职责" not in html


def test_preview_embeds_the_full_content_for_saving():
    """应用里的预览不变：保存时要把没改的条目连同引用原样发回，所以嵌完整内容。"""
    built, _ = content_with_private_marks()
    html = render_html(
        built,
        mode=RenderMode.PREVIEW,
        version=3,
        editor=EditorContext(
            version_id=uuid4(), storage_key="r", save_url="/api/tailor-runs/x/resume"
        ),
    )
    content = embedded(html)["content"]
    bullets = [
        b for s in content["sections"] for e in s["entries"] for b in e["bullets"]
    ]
    pending = next(b for b in bullets if PENDING_MARK in b["text"])
    assert pending["origin"] == "pending" and pending["fact_ids"]
    assert pending["check"]["issues"][0]["message"].startswith(ISSUE_MARK)


def test_export_html_has_no_evidence_marks():
    """模板本身（不带编辑器）在导出模式下不渲染依据标记。"""
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
        update={"font": "songti", "size_pt": 9.5, "leading": 1.35}
    )
    html = render_html(built, mode=RenderMode.EXPORT, version=1)
    assert "--size: 9.5pt; --leading: 1.35;" in html
    assert 'data-font-code="songti"' in html
    # 服务端把字体代码映射成固定 CSS 字体串，不信任任意用户输入
    assert "--font: &#34;Songti SC&#34;, SimSun, serif;" in html


def test_font_validation_legacy_mapping_and_csp_nonce():
    from pydantic import ValidationError

    from jobpilot.services.resume_fonts import default_fonts

    fonts = default_fonts()
    assert [f.code for f in fonts.options] == [
        "default",
        "pingfang",
        "yahei",
        "source_han",
        "songti",
        "kaiti",
    ]
    with pytest.raises(ValidationError):
        ResumeContent.model_validate(
            {"header": [], "sections": [], "style": {"font": '"Comic Sans"; } body{}'}}
        )

    legacy = ResumeContent.from_stored(
        {
            "header": [],
            "sections": [],
            "style": {"font": '"Songti SC", serif', "size_pt": 10.5, "leading": 1.5},
        }
    )
    assert legacy.style.font == "songti"
    unknown = ResumeContent.from_stored(
        {"header": [], "sections": [], "style": {"font": "MyCustomFont"}}
    )
    assert unknown.style.font == "default"

    html = render_html(
        content(),
        mode=RenderMode.PREVIEW,
        version=1,
        editor=EditorContext(
            version_id=uuid4(), storage_key="r", save_url="/api/tailor-runs/x/resume"
        ),
        csp_nonce="test-nonce-123",
    )
    assert html.count('<script nonce="test-nonce-123">') == 2
    assert 'nonce="test-nonce-123"' not in export_file(content())


def test_editor_data_cannot_close_its_script_tag():
    built = content()
    built.header[0] = "</script><script>alert(1)</script>"
    editor = EditorContext(version_id=uuid4(), storage_key="r:v1")
    html = render_html(built, mode=RenderMode.EXPORT, version=1, editor=editor)
    data = html.split('<script type="application/json" id="jobpilot-resume">', 1)[1]
    data = data.split("</script>", 1)[0]
    assert "<" not in data and "\\u003c/script>" in data
    assert '"save_url": null' in data  # 离线文件存本地，不连服务器


@pytest.mark.parametrize("online", [True, False], ids=["preview", "export"])
def test_editor_reads_only_keys_that_are_embedded(online: bool):
    """编辑器脚本读的字段，两种文件的内嵌数据都要给，缺了就是 undefined。"""
    editor = EditorContext(
        version_id=uuid4(),
        storage_key="r",
        save_url="/api/tailor-runs/x/resume" if online else None,
    )
    html = render_html(
        content(),
        mode=RenderMode.PREVIEW if online else RenderMode.EXPORT,
        version=1,
        editor=editor,
    )
    script = (
        html.rsplit("<script>", 1)[1]
        if "<script>" in html
        else html.rsplit("</script>", 2)[1].split(">", 1)[1]
    )
    read = set(re.findall(r"\bdata\.([a-z_]+)", script))
    assert read and read <= set(embedded(html)), read - set(embedded(html))


def test_editor_toolbar_theme_vars_and_offline_vs_online_buttons():
    """工具栏带主题变量、分组样式，且仅离线导出分支挂保存 HTML 与打印按钮。"""
    preview_html = render_html(
        content(),
        mode=RenderMode.PREVIEW,
        version=1,
        editor=EditorContext(
            version_id=uuid4(), storage_key="r", save_url="/api/tailor-runs/x/resume"
        ),
    )
    offline_html = export_file(content())
    for html in (preview_html, offline_html):
        assert "--tb-border: #E6E6E2;" in html
        assert "--tb-active-bg: #E4EAF0;" in html
        assert "toolbar-group" in html
        assert "if (!online)" in html
        assert '"保存 HTML"' in html and '"打印 / 导出 PDF"' in html
    assert '"导出 HTML"' not in preview_html and '"导出 HTML"' not in offline_html
    assert embedded(preview_html)["save_url"] == "/api/tailor-runs/x/resume"
    assert embedded(offline_html)["save_url"] is None
