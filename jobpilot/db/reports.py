"""建立匹配报告并校验运行输入，事务由调用方统一提交。"""

from typing import Any

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
