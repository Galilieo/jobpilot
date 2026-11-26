"""Agent 节点之间可持久化的数据与一次后台执行的依赖。"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Annotated, Any
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing_extensions import TypedDict

from jobpilot.services.llm_gateway import AgentModelBudget

Payload = dict[str, Any]
SendEvent = Callable[[str, Payload], None]


class ToolCategory(StrEnum):
    READ = "read"
    GENERATE = "generate"
    WRITE = "write"
    ASK = "ask"


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Decision(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"


class JobChoice(StrEnum):
    NEW = "new"
    SWITCH = "switch"
    KEEP = "keep"


AnswerText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Decision
    answers: dict[str, AnswerText] | None = Field(default=None, max_length=10)
    job_choice: JobChoice | None = None


class ToolCall(TypedDict):
    call_id: str
    name: str
    arguments: str
    key: str


class AgentState(TypedDict, total=False):
    run_id: str
    messages: list[ChatCompletionMessageParam]
    calls: list[ToolCall]
    tools_left: int
    seconds_left: float
    next_tool: str
    action_id: str
    reply: str
    budget_exhausted: bool
    asked: bool
    message_used_as_jd: bool
    clarification: str | None
    switch_job_id: str | None
    generation_results: dict[str, dict[str, Payload]]


@dataclass(frozen=True)
class AgentDeps:
    sessions: async_sessionmaker[AsyncSession]
    client: AsyncOpenAI
    user_id: UUID
    conversation_id: UUID
    run_id: UUID
    emit: SendEvent
    started: float
    seconds: float

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.seconds - (monotonic() - self.started))


@dataclass(frozen=True)
class ToolContext:
    agent: AgentDeps
    call: ToolCall
    message_used_as_jd: bool = False

    @property
    def budget(self) -> AgentModelBudget:
        return AgentModelBudget(self.agent.run_id)


class ToolError(ValueError):
    """可回给模型或前端的业务错误，只包含固定业务提示。"""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status
