"""按归属更新业务运行状态，由调用方与相关业务写入一同提交。"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import AiRun, RunStatus


async def set_run_status(
    session: AsyncSession, user_id: UUID, run_id: UUID, status: RunStatus
) -> bool:
    """原子更新未结束的运行；已结束或不属于该用户时返回 False。"""
    active = (RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION)
    changed = await session.scalar(
        update(AiRun)
        .where(
            AiRun.user_id == user_id, AiRun.run_id == run_id, AiRun.status.in_(active)
        )
        .values(status=status, finished_at=None if status in active else func.now())
        .returning(AiRun.run_id)
    )
    return changed is not None


class RunAlreadyFinishedError(RuntimeError):
    def __init__(self, status: str | None):
        super().__init__("运行已结束或不可用")
        self.status = status


async def finish_run(
    session: AsyncSession, user_id: UUID, run_id: UUID, status: RunStatus
) -> None:
    """在产物的事务里取得终态；发生冲突时调用方必须回滚该事务。"""
    if status in (RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION):
        raise ValueError("收尾需要终态")
    if await set_run_status(session, user_id, run_id, status):
        return
    current = await session.scalar(
        select(AiRun.status).where(AiRun.user_id == user_id, AiRun.run_id == run_id)
    )
    raise RunAlreadyFinishedError(current)
