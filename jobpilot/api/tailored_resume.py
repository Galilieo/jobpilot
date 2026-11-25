"""完整简历接口：读取（当前或指定版本）、保存编辑（02 第 6 节，06 T2.5）。

只有成功的改写运行才有完整简历；没有就 404（不区分不存在、不是本人、没成功）。
读取时带上每条改写引用的事实，供面板的「依据追溯」显示。
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from jobpilot.api.deps import CurrentUser, DbSession
from jobpilot.db.models import AiRun, FactSource
from jobpilot.services import drafts
from jobpilot.services.grounding import GroundingFact, organizations_of
from jobpilot.services.resume_content import ResumeContent
from jobpilot.services.resume_export import (
    EditorContext,
    RenderMode,
    render_html,
    title_of,
    to_docx,
    to_markdown,
)
from jobpilot.services.resume_versions import (
    LoadedResume,
    ResumeNotFoundError,
    UnknownPartError,
    VersionConflictError,
    load_resume,
    save_version,
)

router = APIRouter(prefix="/api", tags=["tailored-resume"])


class VersionSummary(BaseModel):
    version_id: UUID
    version: int
    created_at: datetime


class CitedFact(BaseModel):
    experience: str
    text: str
    source: FactSource
    source_quote: str | None


class ResumeOut(BaseModel):
    resume_id: UUID
    run_id: UUID
    version_id: UUID
    version: int
    current_version_id: UUID
    versions: list[VersionSummary]
    content: ResumeContent
    facts: dict[UUID, CitedFact]  # 条目引用的事实，按事实 ID


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: UUID  # 开始编辑时的版本；已不是当前版本时 409
    content: ResumeContent


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")


@router.get("/tailor-runs/{run_id}/resume")
async def get_resume(
    run_id: UUID, user: CurrentUser, db: DbSession, version: int | None = None
) -> ResumeOut:
    try:
        loaded = await load_resume(db, user.user_id, run_id, version)
    except ResumeNotFoundError:
        raise _not_found() from None
    return await _resume_out(db, user.user_id, run_id, loaded)


@router.put("/tailor-runs/{run_id}/resume")
async def save_resume(
    run_id: UUID, body: ResumeIn, user: CurrentUser, db: DbSession
) -> ResumeOut:
    """保存编辑，新增一个版本。文字改过的条目重新检查第一、二层，只提示不阻止。"""
    user_id = user.user_id
    facts = await _run_facts(db, user_id, run_id)
    try:
        await save_version(
            db,
            user_id,
            run_id,
            base_version_id=body.base_version_id,
            submitted=body.content,
            available=facts,
            organizations=organizations_of(facts.values()),
        )
    except ResumeNotFoundError:
        raise _not_found() from None
    except VersionConflictError as error:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "简历已在别处保存过，请在最新版本上修改",
                "current_version_id": str(error.current_version_id),
            },
        ) from None
    except UnknownPartError as error:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=list(error.problems),
        ) from None
    await db.commit()
    return await _resume_out(
        db, user_id, run_id, await load_resume(db, user_id, run_id)
    )


async def _run_facts(
    db: DbSession, user_id: UUID, run_id: UUID
) -> dict[UUID, GroundingFact]:
    """这次改写运行固定的事实库版本；依据检查只认这一版。"""
    fact_set_id = await db.scalar(
        select(AiRun.fact_set_id).where(
            AiRun.run_id == run_id, AiRun.user_id == user_id, AiRun.kind == "tailor"
        )
    )
    if fact_set_id is None:
        raise _not_found()
    facts = await drafts.facts_in(db, user_id, fact_set_id)
    return {fact.fact_id: GroundingFact.of(fact) for fact in facts}


async def _resume_out(
    db: DbSession, user_id: UUID, run_id: UUID, loaded: LoadedResume
) -> ResumeOut:
    content = loaded.content
    cited = {
        fact_id
        for section in content.sections
        for entry in section.entries
        for bullet in entry.bullets
        for fact_id in bullet.fact_ids
    }
    fact_set_id = await db.scalar(
        select(AiRun.fact_set_id).where(
            AiRun.run_id == run_id, AiRun.user_id == user_id
        )
    )
    facts = await drafts.facts_in(db, user_id, fact_set_id) if fact_set_id else []
    return ResumeOut(
        resume_id=loaded.resume.resume_id,
        run_id=run_id,
        version_id=loaded.version.resume_version_id,
        version=loaded.version.version,
        current_version_id=loaded.resume.current_version_id,
        versions=[
            VersionSummary(
                version_id=row.resume_version_id,
                version=row.version,
                created_at=row.created_at,
            )
            for row in loaded.history
        ],
        content=content,
        facts={
            fact.fact_id: CitedFact(
                experience=fact.experience,
                text=fact.text,
                source=FactSource(fact.source),
                source_quote=fact.source_quote,
            )
            for fact in facts
            if fact.fact_id in cited
        },
    )


# ---------- 预览与导出 ----------


class ExportFormat(StrEnum):
    HTML = "html"
    MARKDOWN = "md"
    DOCX = "docx"


_MEDIA_TYPES = {
    ExportFormat.HTML: "text/html; charset=utf-8",
    ExportFormat.MARKDOWN: "text/markdown; charset=utf-8",
    ExportFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}


async def _load(db: DbSession, user_id: UUID, run_id: UUID, version: int | None):
    try:
        return await load_resume(db, user_id, run_id, version)
    except ResumeNotFoundError:
        raise _not_found() from None


@router.get("/tailor-runs/{run_id}/preview", response_class=HTMLResponse)
async def preview_resume(
    run_id: UUID, user: CurrentUser, db: DbSession, version: int | None = None
) -> HTMLResponse:
    """应用内预览：带「待处理」「待检查」标记。当前版本可以直接编辑、自动保存；
    旧版本只读（在旧版本上保存必然冲突）。"""
    loaded = await _load(db, user.user_id, run_id, version)
    current = loaded.version.resume_version_id == loaded.resume.current_version_id
    editor = (
        EditorContext(
            version_id=loaded.version.resume_version_id,
            storage_key=f"{loaded.resume.resume_id}",
            save_url=f"/api/tailor-runs/{run_id}/resume",
            export_url=f"/api/tailor-runs/{run_id}/export?format=html",
        )
        if current
        else None
    )
    return HTMLResponse(
        render_html(
            loaded.content,
            mode=RenderMode.PREVIEW,
            version=loaded.version.version,
            editor=editor,
        )
    )


@router.get("/tailor-runs/{run_id}/export")
async def export_resume(
    run_id: UUID,
    user: CurrentUser,
    db: DbSession,
    kind: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.HTML,
    version: int | None = None,
) -> Response:
    """导出文件：不带依据标记，待处理的条目略过。PDF 用浏览器打印 HTML。"""
    loaded = await _load(db, user.user_id, run_id, version)
    content, number = loaded.content, loaded.version.version
    if kind is ExportFormat.HTML:
        # 导出文件离线打开也能编辑，修改存在浏览器本地，按简历和版本区分。
        editor = EditorContext(
            version_id=loaded.version.resume_version_id,
            storage_key=f"{loaded.resume.resume_id}:v{number}",
        )
        body: str | bytes = render_html(
            content, mode=RenderMode.EXPORT, version=number, editor=editor
        )
    elif kind is ExportFormat.MARKDOWN:
        body = to_markdown(content)
    else:
        body = to_docx(content)
    filename = f"{title_of(content)}-v{number}.{kind.value}"
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[kind],
        # RFC 6266：中文文件名用 filename* 按 UTF-8 编码
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )
