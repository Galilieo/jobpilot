"""投递接口共用账号权限、幂等键和只读简历预览策略。"""

import secrets
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from jobpilot.api.deps import CurrentUser, DbSession, SessionFactory
from jobpilot.api.idempotency import RequiredKey, idempotent_request, save_snapshot
from jobpilot.api.tailored_resume import preview_csp
from jobpilot.db.idempotency import Attempt, KeyScope, Operation
from jobpilot.services.applications import (
    ApplicationChanges,
    ApplicationDetail,
    ApplicationError,
    ApplicationInput,
    ApplicationOut,
    RevisionInput,
    application_detail,
    applications_for_job,
    delete_application,
    record_application,
    require_application,
    update_application,
)

router = APIRouter(prefix="/api/applications", tags=["applications"])


async def application_error(_request: Request, error: ApplicationError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=error.status)


async def _reserve(
    body: ApplicationInput,
    user: CurrentUser,
    db: DbSession,
    factory: SessionFactory,
    key: RequiredKey,
) -> AsyncIterator[tuple[ApplicationInput, Attempt]]:
    async with idempotent_request(
        db,
        factory,
        KeyScope(user.user_id, Operation.RECORD_APPLICATION, key),
        body.model_dump_json(),
    ) as attempt:
        yield body, attempt


Submission = Annotated[
    tuple[ApplicationInput, Attempt], Depends(_reserve, scope="function")
]


@router.post("", status_code=201)
async def create_application(submission: Submission, db: DbSession) -> ApplicationOut:
    body, attempt = submission
    row = await record_application(attempt.scope.user_id, db, body)
    output = ApplicationOut.model_validate(row)
    await save_snapshot(db, attempt, row.application_id, output, 201)
    await db.commit()
    return output


@router.get("")
async def list_applications(
    user: CurrentUser, db: DbSession, job_id: UUID | None = None
) -> list[ApplicationOut]:
    return [
        ApplicationOut.model_validate(row)
        for row in await applications_for_job(user.user_id, db, job_id)
    ]


@router.get("/{application_id}")
async def get_application(
    application_id: UUID, user: CurrentUser, db: DbSession
) -> ApplicationDetail:
    return await application_detail(user.user_id, db, application_id)


@router.get("/{application_id}/snapshot.html", response_class=HTMLResponse)
async def get_snapshot(
    application_id: UUID, user: CurrentUser, db: DbSession
) -> HTMLResponse:
    row = await require_application(user.user_id, db, application_id)
    if row.snapshot_html is None:
        raise HTTPException(404, "这条投递没有简历快照")
    return HTMLResponse(
        row.snapshot_html,
        headers={
            "Content-Security-Policy": preview_csp(secrets.token_urlsafe(18)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/{application_id}")
async def edit_application(
    application_id: UUID, body: ApplicationChanges, user: CurrentUser, db: DbSession
) -> ApplicationOut:
    row = await update_application(user.user_id, db, application_id, body)
    output = ApplicationOut.model_validate(row)
    await db.commit()
    return output


@router.delete("/{application_id}", status_code=204)
async def remove_application(
    application_id: UUID, body: RevisionInput, user: CurrentUser, db: DbSession
) -> Response:
    await delete_application(user.user_id, db, application_id, body.expected_revision)
    await db.commit()
    return Response(status_code=204)
