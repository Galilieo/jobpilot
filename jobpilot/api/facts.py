"""经历事实库接口：查看、确认、修改、补充、删除、批量操作、发布。

改动都先进草稿，发布时生成新版本。一条事实用 lineage_id 指代，
草稿和最新版本里都找不到时返回 404，包括别人的事实（不暴露是否存在）。
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.api.deps import CurrentUser, DbSession
from jobpilot.db.models import Fact, FactKind, FactSet, Responsibility
from jobpilot.services import drafts
from jobpilot.services.drafts import (
    CannotConfirmError,
    DraftItem,
    DraftRevisionConflictError,
    FactChanges,
    ItemNotFoundError,
    NothingToPublishError,
    VersionConflictError,
)

router = APIRouter(prefix="/api", tags=["facts"])
ExpectedRevision = Annotated[int, Field(ge=0, strict=True)]


class FactSetOut(BaseModel):
    fact_set_id: UUID
    version: int

    @classmethod
    def of(cls, fact_set: FactSet) -> "FactSetOut":
        return cls(fact_set_id=fact_set.fact_set_id, version=fact_set.version)


class FactOut(BaseModel):
    """已发布的一条事实。片段位置按 Python 字符计数，前端高亮要换算成 UTF-16。"""

    fact_id: UUID
    lineage_id: UUID
    experience: str
    kind: FactKind
    responsibility: Responsibility
    text: str
    has_number: bool
    source: str
    source_doc_id: UUID | None
    source_quote: str | None
    span_start: int | None
    span_end: int | None

    @classmethod
    def of(cls, fact: Fact) -> "FactOut":
        return cls.model_validate(fact, from_attributes=True)


class DraftOut(BaseModel):
    base_fact_set_id: UUID | None
    items: list[DraftItem]
    revision: int


class LibraryOut(BaseModel):
    fact_set: FactSetOut | None  # 还没发布过时为空
    facts: list[FactOut]
    draft: DraftOut


class FactDetailOut(BaseModel):
    published: FactOut | None  # 最新版本里的这条事实
    draft: DraftItem | None  # 草稿里对它的改动
    revision: int


class DraftItemOut(DraftItem):
    revision: int


class NewFactIn(BaseModel):
    """用户补充的事实，没有原文出处。"""

    model_config = ConfigDict(extra="forbid")

    experience: str = Field(min_length=1, max_length=200)
    kind: FactKind
    responsibility: Responsibility
    text: str = Field(min_length=1, max_length=500)


class FactChangesIn(FactChanges):
    model_config = ConfigDict(extra="forbid")

    expected_revision: ExpectedRevision | None = None

    @model_validator(mode="after")
    def require_revision_for_review(self) -> "FactChangesIn":
        if self.expected_revision is None and (
            self.confirmed is not None or self.responsibility is not None
        ):
            raise ValueError("设置确认状态或职责时必须提供 expected_revision")
        return self


class ConfirmCleanIn(BaseModel):
    """确认全部没有核对原因的待确认条目。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm_clean"]
    expected_revision: ExpectedRevision


class SetResponsibilityIn(BaseModel):
    """把某段经历里职责未写明的条目设为用户给的职责。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["set_responsibility"]
    experience: str = Field(min_length=1, max_length=200)
    responsibility: Literal[
        Responsibility.LEAD, Responsibility.OWN, Responsibility.PARTICIPATE
    ]
    expected_revision: ExpectedRevision


BatchIn = Annotated[ConfirmCleanIn | SetResponsibilityIn, Field(discriminator="action")]


class BatchOut(BaseModel):
    changed: int
    revision: int


class CommitIn(BaseModel):
    """base_fact_set_id 是开始编辑时看到的最新版本；第一次发布时为空。"""

    model_config = ConfigDict(extra="forbid")

    base_fact_set_id: UUID | None
    expected_revision: ExpectedRevision


class PublishOut(FactSetOut):
    revision: int


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事实不存在")


def _revision_conflict(error: DraftRevisionConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "草稿已在别处更新，请刷新后再操作",
            "revision": error.revision,
        },
    )


async def _commit_draft(db: AsyncSession, user_id: UUID) -> int:
    revision = await drafts.read_revision(db, user_id)
    await db.commit()
    return revision


@router.get("/facts")
async def get_library(user: CurrentUser, db: DbSession) -> LibraryOut:
    draft, latest = await drafts.read_snapshot(db, user.user_id)
    return LibraryOut(
        fact_set=FactSetOut.of(latest) if latest else None,
        facts=[FactOut.of(fact) for fact in await drafts.facts_of(db, latest)],
        draft=DraftOut(
            base_fact_set_id=draft.base_fact_set_id if draft else None,
            items=drafts.items_of(draft),
            revision=drafts.revision_of(draft),
        ),
    )


@router.get("/facts/{lineage_id}")
async def get_fact(lineage_id: UUID, user: CurrentUser, db: DbSession) -> FactDetailOut:
    draft, latest = await drafts.read_snapshot(db, user.user_id)
    published = next(
        (f for f in await drafts.facts_of(db, latest) if f.lineage_id == lineage_id),
        None,
    )
    draft_item = next(
        (item for item in drafts.items_of(draft) if item.lineage_id == lineage_id), None
    )
    if published is None and draft_item is None:
        raise _not_found()
    return FactDetailOut(
        published=FactOut.of(published) if published else None,
        draft=draft_item,
        revision=drafts.revision_of(draft),
    )


@router.post("/facts", status_code=status.HTTP_201_CREATED)
async def add_fact(body: NewFactIn, user: CurrentUser, db: DbSession) -> DraftItemOut:
    item = await drafts.add_user_fact(
        db,
        user.user_id,
        experience=body.experience,
        kind=body.kind,
        responsibility=body.responsibility,
        text=body.text,
    )
    revision = await _commit_draft(db, user.user_id)
    return DraftItemOut(**item.model_dump(), revision=revision)


@router.put("/facts/{lineage_id}")
async def edit_fact(
    lineage_id: UUID, body: FactChangesIn, user: CurrentUser, db: DbSession
) -> DraftItemOut:
    try:
        item = await drafts.edit_fact(
            db, user.user_id, lineage_id, body, expected_revision=body.expected_revision
        )
    except ItemNotFoundError:
        raise _not_found() from None
    except DraftRevisionConflictError as error:
        raise _revision_conflict(error) from None
    except CannotConfirmError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="原文校验没有通过，请修改文字或删除这条事实",
        ) from None
    revision = await _commit_draft(db, user.user_id)
    return DraftItemOut(**item.model_dump(), revision=revision)


@router.delete("/facts/{lineage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fact(lineage_id: UUID, user: CurrentUser, db: DbSession) -> Response:
    try:
        await drafts.delete_fact(db, user.user_id, lineage_id)
    except ItemNotFoundError:
        raise _not_found() from None
    revision = await _commit_draft(db, user.user_id)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Draft-Revision": str(revision)},
    )


@router.post("/facts/batch")
async def batch(
    body: Annotated[BatchIn, Body()], user: CurrentUser, db: DbSession
) -> BatchOut:
    try:
        if isinstance(body, ConfirmCleanIn):
            changed = await drafts.confirm_clean(
                db, user.user_id, expected_revision=body.expected_revision
            )
        else:
            changed = await drafts.set_responsibility(
                db,
                user.user_id,
                experience=body.experience,
                responsibility=body.responsibility,
                expected_revision=body.expected_revision,
            )
    except DraftRevisionConflictError as error:
        raise _revision_conflict(error) from None
    return BatchOut(changed=changed, revision=await _commit_draft(db, user.user_id))


@router.post("/facts/commit")
async def commit(body: CommitIn, user: CurrentUser, db: DbSession) -> PublishOut:
    try:
        fact_set = await drafts.publish_draft(
            db,
            user.user_id,
            body.base_fact_set_id,
            expected_revision=body.expected_revision,
        )
    except DraftRevisionConflictError as error:
        raise _revision_conflict(error) from None
    except VersionConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="事实库已在别处更新，请刷新后再发布",
        ) from None
    except NothingToPublishError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="没有已确认或删除的改动"
        ) from None
    revision = await _commit_draft(db, user.user_id)
    return PublishOut(
        fact_set_id=fact_set.fact_set_id, version=fact_set.version, revision=revision
    )
