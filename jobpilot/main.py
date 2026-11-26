"""提供 JobPilot HTTP 入口，管理数据库生命周期并检查连通性。"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from openai import AsyncOpenAI
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.api.account import router as account_router
from jobpilot.api.agent import router as agent_router
from jobpilot.api.auth import router as auth_router
from jobpilot.api.conversations import router as conversation_router
from jobpilot.api.error_boundary import RequestErrorBoundary
from jobpilot.api.facts import router as facts_router
from jobpilot.api.idempotency import IdempotentReplay, replay_response
from jobpilot.api.jobs import router as jobs_router
from jobpilot.api.match_idempotency import include_matching_routes
from jobpilot.api.memories import router as memories_router
from jobpilot.api.ocr import router as ocr_router
from jobpilot.api.overview import router as overview_router
from jobpilot.api.resumes import router as resumes_router
from jobpilot.api.tailored_resume import router as tailored_resume_router
from jobpilot.api.tailoring import RunLauncher
from jobpilot.api.tailoring import router as tailoring_router
from jobpilot.config import settings
from jobpilot.db.idempotency import fail_interrupted_keys
from jobpilot.db.runs import fail_abandoned_runs
from jobpilot.db.session import check_database, create_database_engine, get_session
from jobpilot.diagnostics import describe_error, error_locations
from jobpilot.services.accounts import purge_daily, purge_with_logging
from jobpilot.services.agent.runtime import AgentRuntime

logger = logging.getLogger("uvicorn.error").getChild("jobpilot")


async def _start_resources(app: FastAPI, resources: AsyncExitStack) -> None:
    """初始化服务，资源就绪后立即登记退出时的释放动作。"""
    app.state.run_launcher = RunLauncher()
    app.state.agent_runtime = AgentRuntime()
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    resources.push_async_callback(engine.dispose)
    app.state.db_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with app.state.db_session_factory.begin() as session:
        await fail_abandoned_runs(session)
        await fail_interrupted_keys(session)
    app.state.model_client = AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value() or "not-configured",
        base_url=settings.deepseek_base_url,
    )
    resources.push_async_callback(app.state.model_client.close)
    await purge_with_logging(app.state.db_session_factory)
    await app.state.agent_runtime.recover_decided_runs(
        app.state.db_session_factory,
        app.state.model_client,
        app.state.run_launcher.launch,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """回收遗留运行，管理定时清理和连接资源，对服务器报告启动或退出失败。"""
    try:
        async with AsyncExitStack() as resources:
            await _start_resources(app, resources)
            account_cleanup = asyncio.create_task(
                purge_daily(app.state.db_session_factory)
            )
            try:
                yield
            finally:
                account_cleanup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await account_cleanup
    except Exception as error:
        logger.error(
            "应用生命周期出错（%s），调用栈：%s",
            describe_error(error),
            error_locations(error),
        )
        raise RuntimeError("应用生命周期失败") from None


app = FastAPI(title="JobPilot", lifespan=lifespan)
app.add_middleware(RequestErrorBoundary)
# 改写的后台任务。不依赖事件循环；测试客户端不运行 lifespan，也要有它。
app.state.run_launcher = RunLauncher()
app.state.agent_runtime = AgentRuntime()
app.add_exception_handler(IdempotentReplay, replay_response)
app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(agent_router)
app.include_router(account_router)
app.include_router(memories_router)
app.include_router(ocr_router)
app.include_router(resumes_router)
app.include_router(facts_router)
app.include_router(jobs_router)
app.include_router(tailoring_router)
app.include_router(tailored_resume_router)
app.include_router(overview_router)
include_matching_routes(app)


@app.get("/api/health")
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """通过请求专属的 session 执行查询，检查 PostgreSQL 连通性。"""
    try:
        await check_database(session)
    except SQLAlchemyError:
        # 不把连接信息、驱动异常或数据库内部细节返回给浏览器。
        raise HTTPException(status_code=503, detail="数据库暂不可用") from None
    return {"status": "ok", "database": "ok"}
