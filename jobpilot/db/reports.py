"""建立匹配报告并校验运行输入，事务由调用方统一提交。"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import AiRun, MatchReport


def add_match_report(
    db: AsyncSession, run: AiRun, *, items: list[dict[str, Any]], main_gaps: list[str]
) -> MatchReport:
    if run.kind != "match" or run.fact_set_id is None or run.job_snapshot_json is None:
        raise ValueError("报告需要带有事实版本和岗位快照的匹配运行")
    report = MatchReport(
        user_id=run.user_id, run_id=run.run_id, items_json=items, main_gaps=main_gaps
    )
    db.add(report)
    return report


async def latest_match_reports_by_job(
    db: AsyncSession, user_id: UUID
) -> dict[UUID, tuple[dict[str, Any] | None, list[dict[str, Any]]]]:
    """一次查询当前用户每个岗位的最新匹配报告（快照与结果），按自增序号 seq 倒序。"""
    rows = (
        await db.execute(
            select(AiRun.user_job_id, AiRun.job_snapshot_json, MatchReport.items_json)
            .join(
                MatchReport,
                (MatchReport.run_id == AiRun.run_id)
                & (MatchReport.user_id == AiRun.user_id),
            )
            .where(MatchReport.user_id == user_id, AiRun.user_job_id.is_not(None))
            .ext(distinct_on(AiRun.user_job_id))
            .order_by(AiRun.user_job_id, MatchReport.seq.desc())
        )
    ).all()
    return {
        job_id: (snapshot_json, items_json)
        for job_id, snapshot_json, items_json in rows
        if job_id is not None
    }
