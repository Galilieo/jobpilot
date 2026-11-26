"""工具注册表在服务端决定权限和输入模型。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from openai.types.chat import ChatCompletionToolUnionParam
from pydantic import ValidationError

from jobpilot.services.agent import actions, generation, read_tools
from jobpilot.services.agent.arguments import (
    AskArgs,
    EditResumeArgs,
    FactsArgs,
    JobArgs,
    MatchReportArgs,
    ParseJobArgs,
    ResumeArgs,
    SetJobArgs,
    TailorArgs,
)
from jobpilot.services.agent.memory_actions import (
    ForgetArgs,
    RememberArgs,
    prepare_memory,
)
from jobpilot.services.agent.types import (
    Payload,
    ToolArgs,
    ToolCategory,
    ToolContext,
    ToolError,
)


@dataclass(frozen=True)
class Tool:
    name: str
    category: ToolCategory
    description: str
    arguments: type[ToolArgs]
    execute: Callable[[ToolContext, ToolArgs], Awaitable[Payload]]

    def parse_arguments(self, raw_arguments: str) -> ToolArgs:
        try:
            return self.arguments.model_validate_json(raw_arguments)
        except ValidationError:
            raise ToolError("工具参数格式错误") from None

    async def invoke(self, context: ToolContext) -> Payload:
        return await self.execute(
            context, self.parse_arguments(context.call["arguments"])
        )

    @property
    def definition(self) -> ChatCompletionToolUnionParam:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments.model_json_schema(),
            },
        }


def _register[Args: ToolArgs](
    name: str,
    category: ToolCategory,
    description: str,
    arguments: type[Args],
    run: Callable[[ToolContext, Args], Awaitable[Payload]],
) -> Tool:
    async def execute(context: ToolContext, args: ToolArgs) -> Payload:
        return await run(context, cast(Args, args))

    return Tool(name, category, description, arguments, execute)


async def _ask(_context: ToolContext, args: AskArgs) -> Payload:
    return args.model_dump(mode="json")


TOOLS = {
    tool.name: tool
    for tool in (
        _register(
            "list_jobs",
            ToolCategory.READ,
            "列出本人的岗位和基本字段",
            ToolArgs,
            read_tools.list_jobs,
        ),
        _register(
            "read_job",
            ToolCategory.READ,
            "读取本人的岗位详情和 JD",
            JobArgs,
            read_tools.read_job,
        ),
        _register(
            "read_facts",
            ToolCategory.READ,
            "读取本人的已发布经历事实、求职条件和教育",
            FactsArgs,
            read_tools.read_facts,
        ),
        _register(
            "read_match_report",
            ToolCategory.READ,
            "按报告 ID 读取匹配报告",
            MatchReportArgs,
            read_tools.read_match_report,
        ),
        _register(
            "read_resume",
            ToolCategory.READ,
            "按改写运行 ID 读取定制简历及版本",
            ResumeArgs,
            read_tools.read_resume,
        ),
        _register(
            "parse_jd",
            ToolCategory.GENERATE,
            "引用本轮用户的完整 JD 正文或附件，解析后存入岗位库",
            ParseJobArgs,
            generation.parse_jd,
        ),
        _register(
            "match_job",
            ToolCategory.GENERATE,
            "用已发布事实库和岗位要求生成匹配报告",
            JobArgs,
            generation.match,
        ),
        _register(
            "tailor_resume",
            ToolCategory.GENERATE,
            "生成经过依据检查的定制简历",
            TailorArgs,
            generation.tailor,
        ),
        _register(
            "set_current_job",
            ToolCategory.WRITE,
            "提议切换或解除当前岗位，须用户确认",
            SetJobArgs,
            actions.prepare_job,
        ),
        _register(
            "edit_resume_item",
            ToolCategory.WRITE,
            "引用本轮替换正文或润色要求，提议修改一条定制简历条目，须用户确认",
            EditResumeArgs,
            actions.prepare_resume,
        ),
        _register(
            "remember",
            ToolCategory.WRITE,
            "引用本轮正文提议长期偏好，须用户确认",
            RememberArgs,
            prepare_memory,
        ),
        _register(
            "forget",
            ToolCategory.WRITE,
            "引用本轮正文提议删除偏好，须用户确认",
            ForgetArgs,
            prepare_memory,
        ),
        _register(
            "ask_user", ToolCategory.ASK, "合并本轮需要用户回答的问题", AskArgs, _ask
        ),
    )
}

TOOL_DEFINITIONS = [tool.definition for tool in TOOLS.values()]
