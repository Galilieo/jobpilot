"""定义投递阶段和 ORM、迁移共用的字段约束。"""

from enum import StrEnum


class ApplicationStage(StrEnum):
    """投递当前所处的阶段。"""

    APPLIED = "applied"
    WRITTEN_TEST = "written_test"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"


def stage_check(column: str) -> str:
    """生成投递及阶段历史共用的检查表达式。"""
    values = ", ".join(repr(stage.value) for stage in ApplicationStage)
    return f"{column} IN ({values})"


CHANNEL_LENGTH = 120
SNAPSHOT_CHECK = (
    "backfilled OR (snapshot_json IS NOT NULL AND snapshot_html IS NOT NULL "
    "AND fact_set_id IS NOT NULL)"
)
