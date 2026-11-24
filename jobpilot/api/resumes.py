"""简历上传：先提取文字给用户核对，再保存原文并拆分事实。

分两步是因为事实的原文位置按保存的文字计算。PDF 硬换行、双栏交错、乱码这类提取错误，
必须在拆分之前由用户改掉，与 OCR 的做法一致（见 02 第 6 节 2026-11-23 的调整）。
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from jobpilot.api.deps import CurrentUser, DbSession, ModelClient, SessionFactory
from jobpilot.api.ocr import read_limited
from jobpilot.db.models import AiRun, ResumeDocument, RunStatus
from jobpilot.db.runs import set_run_status
from jobpilot.services import drafts
from jobpilot.services.drafts import DraftItem
from jobpilot.services.facts import Section, TruncatedOutputError, extract_facts
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    EmptyModelResponseError,
    ModelBudget,
)
from jobpilot.services.parsing import (
    NeedsOcrError,
    ParseError,
    UnsupportedFormatError,
    extract_text,
    normalize_text,
)

router = APIRouter(prefix="/api", tags=["resumes"])

# 一份简历约 2～3 千字；上限控制单次拆分的成本和输出长度。
MAX_RESUME_CHARS = 20_000
# 允许一次 JSON 修复和一次上游重试；token 按网关的估算方式留足。
_EXTRACT_BUDGET = {"remaining_calls": 3, "remaining_tokens": 60_000}
_MODEL_ERRORS = (
    APIConnectionError,
    APIStatusError,
    BudgetExhaustedError,
    TruncatedOutputError,
    EmptyModelResponseError,
    ValidationError,
)


class ExtractOut(BaseModel):
    text: str


class ResumeIn(BaseModel):
    """用户核对后的简历文字；文件名只用于展示。"""

    model_config = ConfigDict(extra="forbid")

    filename: str | None = Field(default=None, max_length=255)
    raw_text: str = Field(min_length=1, max_length=MAX_RESUME_CHARS)


class ResumeOut(BaseModel):
    doc_id: UUID
    run_id: UUID
    items: list[DraftItem]  # 这次拆出的事实，都在草稿里等确认


class ResumeDocumentOut(BaseModel):
    doc_id: UUID
    filename: str | None
    raw_text: str
    # 片段位置按 Python 字符计数，前端高亮时要换算成 UTF-16（表情符号占两个单位）。
    sections: list[Section]
    created_at: datetime


@router.post("/resumes/extract")
async def extract(
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="TXT、Markdown、HTML、DOCX 或 PDF")],
) -> ExtractOut:
    """只提取文字，不保存；图片和扫描版 PDF 请改用 /api/ocr。"""
    data = await read_limited(file)
    filename = file.filename or ""
    try:
        text = await run_in_threadpool(extract_text, filename, data)
    except NeedsOcrError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{filename}：需要文字识别，请改用 /api/ocr",
        ) from None
    except UnsupportedFormatError as error:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{filename}：{error}",
        ) from None
    except ParseError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{filename}：{error}",
        ) from None
    return ExtractOut(text=text)


@router.post("/resumes", status_code=status.HTTP_201_CREATED)
async def upload(
    body: ResumeIn,
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
) -> ResumeOut:
    """保存原文并拆分事实，拆出的事实放进草稿。

    原文和运行先提交，网关才能登记调用；拆分失败时运行记为失败，原文保留。
    """
    raw_text = normalize_text(body.raw_text)
    if not raw_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="简历内容为空"
        )

    doc = ResumeDocument(
        user_id=user.user_id, filename=body.filename, raw_text=raw_text
    )
    run = AiRun(user_id=user.user_id, kind="parse_resume")
    db.add_all([doc, run])
    await db.commit()

    try:
        result = await extract_facts(
            session_factory,
            user.user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(**_EXTRACT_BUDGET),
            doc_id=doc.doc_id,
            raw_text=raw_text,
        )
    except _MODEL_ERRORS:
        await set_run_status(db, user.user_id, run.run_id, RunStatus.FAILED)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="事实拆分失败，请稍后重试"
        ) from None

    doc.sections_json = [section.model_dump(mode="json") for section in result.sections]
    items = await drafts.add_extracted(db, user.user_id, result.facts)
    await set_run_status(db, user.user_id, run.run_id, RunStatus.SUCCEEDED)
    await db.commit()
    return ResumeOut(doc_id=doc.doc_id, run_id=run.run_id, items=items)


@router.get("/resumes/{doc_id}")
async def get_resume(
    doc_id: UUID, user: CurrentUser, db: DbSession
) -> ResumeDocumentOut:
    """原文与分段，供依据追溯高亮。不是本人的简历一律 404。"""
    doc = await db.scalar(
        select(ResumeDocument).where(
            ResumeDocument.doc_id == doc_id, ResumeDocument.user_id == user.user_id
        )
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")
    return ResumeDocumentOut(
        doc_id=doc.doc_id,
        filename=doc.filename,
        raw_text=doc.raw_text,
        sections=[Section.model_validate(raw) for raw in doc.sections_json],
        created_at=doc.created_at,
    )
