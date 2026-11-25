"""按归属更新业务运行状态，由调用方与相关业务写入一同提交。"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import AiRun, RunStatus, StopReason

_ACTIVE_STATUSES = (RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION)


async def set_run_status(
    session: AsyncSession, user_id: UUID, run_id: UUID, status: RunStatus
) -> bool:
    """原子更新未结束的运行；已结束或不属于该用户时返回 False。"""
    changed = await session.scalar(
        update(AiRun)
        .where(
            AiRun.user_id == user_id,
            AiRun.run_id == run_id,
            AiRun.status.in_(_ACTIVE_STATUSES),
        )
        .values(
            status=status,
            finished_at=None if status in _ACTIVE_STATUSES else func.now(),
        )
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
    if status in _ACTIVE_STATUSES:
        raise ValueError("收尾需要终态")
    if await set_run_status(session, user_id, run_id, status):
        return
    current = await session.scalar(
        select(AiRun.status).where(AiRun.user_id == user_id, AiRun.run_id == run_id)
    )
    raise RunAlreadyFinishedError(current)


async def fail_abandoned_runs(session: AsyncSession) -> int:
    """启动时把上次进程留下的 running 标为失败，原因「服务重启」，返回条数。

    V1 只有一个后端进程，启动时不会有别的进程正在执行运行；多进程部署前要改成按进程
    或租约回收（02 第 3.3 节）。waiting_confirmation 继续等待用户确认，不动。
    已经提交的模型调用和产物保留。
    """
    result = await session.execute(
        update(AiRun)
        .where(AiRun.status == RunStatus.RUNNING)
        .values(
            status=RunStatus.FAILED,
            finished_at=func.now(),
            stop_reason=StopReason.SERVICE_RESTARTED,
        )
    )
    return result.rowcount


async def cancel_account_runs(session: AsyncSession, user_id: UUID) -> list[UUID]:
    """收尾与取消通过同一运行行串行化，避免迟到的任务覆盖取消状态。"""
    cancelled = await session.scalars(
        update(AiRun)
        .where(
            AiRun.user_id == user_id,
            AiRun.status.in_(_ACTIVE_STATUSES),
        )
        .values(status=RunStatus.CANCELLED, finished_at=func.now())
        .returning(AiRun.run_id)
    )
    return list(cancelled)
