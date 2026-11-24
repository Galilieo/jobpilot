"""从 Cookie 识别当前请求的用户；业务接口只从这里取得 user_id。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api.idempotency import complete_reserved_product
from jobpilot.config import settings
from jobpilot.db.accounts import LoginSession, find_login_session
from jobpilot.db.models import RunStatus, User
from jobpilot.db.runs import RunAlreadyFinishedError, finish_run
from jobpilot.db.session import get_session
from jobpilot.services.auth import hash_token

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def current_session(request: Request, db: DbSession) -> LoginSession:
    """登录有效即通过，不看账号状态；登出和以后的恢复账号使用。"""
    token = request.cookies.get(settings.cookie_name)
    login = await find_login_session(db, hash_token(token)) if token else None
    if login is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期"
        )
    return login


async def current_user(
    login: Annotated[LoginSession, Depends(current_session)],
) -> User:
    """在登录有效的基础上拒绝待删除账号；业务接口都用它取得当前用户。"""
    if login.user.deletion_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号待删除，只能恢复或登出"
        )
    return login.user


CurrentUser = Annotated[User, Depends(current_user)]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """模型网关要用独立的短事务记账，需要 session 工厂而不是请求的 session。"""
    return request.app.state.db_session_factory


def get_model_client(request: Request) -> AsyncOpenAI:
    """应用启动时创建的模型客户端；测试替换成假上游。"""
    return request.app.state.model_client


SessionFactory = Annotated[
    async_sessionmaker[AsyncSession], Depends(get_session_factory)
]
ModelClient = Annotated[AsyncOpenAI, Depends(get_model_client)]


async def finish_product(db: AsyncSession, user_id: UUID, run_id: UUID) -> None:
    try:
        await finish_run(db, user_id, run_id, RunStatus.SUCCEEDED)
    except RunAlreadyFinishedError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"status": error.status}) from error
    await complete_reserved_product(db, run_id)
