"""账号删除、恢复与全量数据导出接口。"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from jobpilot.api.auth import clear_session_cookie
from jobpilot.api.deps import AuthenticatedSession, DbSession, current_user
from jobpilot.api.tailoring import Launcher
from jobpilot.db.accounts import RestoreResult, restore_account
from jobpilot.services.account_export import build_account_export
from jobpilot.services.accounts import request_account_deletion

router = APIRouter(prefix="/api/account", tags=["account"])

_RESTORE_PROBLEMS = {
    RestoreResult.EXPIRED: "账号已过 7 天恢复期",
    RestoreResult.STALE: "账号的删除申请已更新，或登录已失效，请刷新后再试",
}


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(current_user)],
)
async def request_deletion(
    response: Response, login: AuthenticatedSession, db: DbSession, launcher: Launcher
) -> None:
    """记录删除申请并清除已撤销的登录 Cookie。"""
    cancelled = await request_account_deletion(db, login)
    launcher.cancel_runs(cancelled)
    clear_session_cookie(response)


@router.post("/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(login: AuthenticatedSession, db: DbSession) -> None:
    """撤销当前的删除申请，只在 7 天窗口内；同一次申请重复恢复也算成功。"""
    result = await restore_account(db, login)
    if result is not RestoreResult.RESTORED:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=_RESTORE_PROBLEMS[result])
    await db.commit()


@router.get("/export", response_class=JSONResponse)
async def export(login: AuthenticatedSession, db: DbSession) -> JSONResponse:
    """下载个人归档，待删除期间也可使用。"""
    user_id = login.user.user_id
    # 结束鉴权的读事务，再用同一请求 session 开启导出快照。
    await db.rollback()
    archive = await build_account_export(db, user_id)
    if archive is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="账号已删除")
    return JSONResponse(
        content=jsonable_encoder(archive),
        headers={
            "Content-Disposition": 'attachment; filename="jobpilot-account.json"',
            "Cache-Control": "no-store",
        },
    )
