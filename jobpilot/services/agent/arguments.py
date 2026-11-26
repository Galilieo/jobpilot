"""模型可传的工具参数。身份、当前简历版本和审批状态由服务端取得。"""

from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints

from jobpilot.db.models import SectionKind
from jobpilot.services.agent.types import ToolArgs

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class JobArgs(ToolArgs):
    job_id: UUID | None = Field(default=None, description="不填时使用当前会话的岗位")


class SetJobArgs(ToolArgs):
    job_id: UUID | None = Field(description="岗位 ID，null 表示解除绑定")


class FactsArgs(ToolArgs):
    fact_set_id: UUID | None = Field(default=None, description="不填时读取最新发布版本")


class MatchReportArgs(ToolArgs):
    report_id: UUID


class ResumeArgs(ToolArgs):
    run_id: UUID
    version: int | None = Field(default=None, ge=1)


class EditResumeArgs(ToolArgs):
    run_id: UUID
    bullet_id: UUID
    text: ShortText


class ParseJobArgs(ToolArgs):
    raw_text: str = Field(min_length=1, max_length=50000)


class TailorArgs(JobArgs):
    base_doc_id: UUID | None = None
    sections: dict[str, SectionKind] = Field(default_factory=dict, max_length=30)


class AskArgs(ToolArgs):
    questions: list[ShortText] = Field(min_length=1, max_length=10)
