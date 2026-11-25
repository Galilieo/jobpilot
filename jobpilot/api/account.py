"""账号删除、恢复与全量数据导出接口。"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from jobpilot.api.auth import clear_session_cookie
from jobpilot.api.deps import (
    AuthenticatedSession,
    DbSession,
    SessionFactory,
    current_user,
)
from jobpilot.db.accounts import restore_account
from jobpilot.services.account_export import build_account_export
from jobpilot.services.accounts import request_account_deletion

router = APIRouter(prefix="/api/account", tags=["account"])


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(current_user)],
)
async def request_deletion(
    response: Response, login: AuthenticatedSession, db: DbSession
) -> None:
    """记录删除申请并清除已撤销的登录 Cookie。"""
    await request_account_deletion(db, login)
    clear_session_cookie(response)


@router.post("/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(login: AuthenticatedSession, db: DbSession) -> None:
    """只在 7 天窗口内恢复账号。"""
    if not await restore_account(db, login.user.user_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="账号已过 7 天恢复期")
    await db.commit()


@router.get("/export", response_class=JSONResponse)
async def export(login: AuthenticatedSession, factory: SessionFactory) -> JSONResponse:
    """下载个人归档，待删除期间也可使用。"""
    archive = await build_account_export(factory, login.user.user_id)
    if archive is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="账号已删除")
    return JSONResponse(
        content=jsonable_encoder(archive),
        headers={
            "Content-Disposition": 'attachment; filename="jobpilot-account.json"',
            "Cache-Control": "no-store",
        },
    )
