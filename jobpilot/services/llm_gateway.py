"""模型网关：业务代码调用模型的唯一入口。

request_completion 负责一次逻辑请求：有限重试上游临时错误、JSON 校验失败时修复一次。
其中每次实际发送都先扣减运行额度，再在 model_call 表登记一行；
登记和结算各用一个独立短事务，业务事务回滚也不会丢失调用记录。
同时提供不可信文本的数据区块包装，并在每次外发前统一遮蔽联系方式和证件号。
"""

import re
from asyncio import sleep
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from json import dumps
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, omit
from openai.resources.chat.completions import AsyncCompletions
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)
from pydantic import ValidationError
from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelSettings, ProtocolAdapter, settings
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.model_quota import daily_reserved_tokens, lock_daily_quota
from jobpilot.db.models import AiRun, CallOutcome, ModelCall
from jobpilot.db.runs import ACTIVE_RUN_STATUSES
from jobpilot.services.model_protocol import (
    completion_usage,
    context_overflow,
    protocol_parameters,
)
from jobpilot.services.prompts import Prompt, load_prompt

_DATA_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# 只匹配 untrusted_data 标签的开头，放过 List<String> 这类正常尖括号。
_DATA_TAG_PATTERN = re.compile(r"<(\s*/?\s*untrusted_data)\b", re.IGNORECASE)

_ID_CARD_PATTERN = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"\d{3}[\dXx](?![\dXx])"
)
# 不用 \w 做边界：Python 3 中 \w 会匹配中文，导致「邮箱a@b.com」漏匹配。
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9.-])"
)
_MOBILE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}(?!\d)"
)
# 座机必须带区号和连字符，避免误伤「2025-2026」这类年份区间。
_LANDLINE_PATTERN = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")

# 身份证号先于手机号替换，避免 18 位号码中的 11 位子串被误拆。
_PII_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_ID_CARD_PATTERN, "[ID_CARD]"),
    (_EMAIL_PATTERN, "[EMAIL]"),
    (_MOBILE_PATTERN, "[PHONE]"),
    (_LANDLINE_PATTERN, "[PHONE]"),
)


def wrap_untrusted_text(label: str, text: str) -> str:
    """把不可信文本包进数据区块，并转义正文中伪造的区块标签。

    label 只允许字母、数字、下划线和连字符，防止破坏属性引号。
    """
    if not _DATA_LABEL_PATTERN.fullmatch(label):
        raise ValueError(f"非法的不可信数据区块标签: {label!r}")
    escaped = _DATA_TAG_PATTERN.sub(r"&lt;\1", text)
    return f'<untrusted_data label="{label}">\n{escaped}\n</untrusted_data>'


def redact_pii(text: str) -> str:
    """用占位符遮蔽文本中的身份证号、邮箱、手机号和座机号。"""
    for pattern, placeholder in _PII_RULES:
        text = pattern.sub(placeholder, text)
    return text


def pii_spans(text: str) -> list[tuple[int, int]]:
    """返回原文字索引，合并重叠片段，切分时一次保留完整片段。"""
    ranges = sorted(
        match.span() for pattern, _ in _PII_RULES for match in pattern.finditer(text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start < merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def pii_at_boundary(
    spans: Sequence[tuple[int, int]], cut: int
) -> tuple[int, int] | None:
    return next(((start, end) for start, end in spans if start < cut < end), None)


def preview_text(text: str, limit: int) -> str:
    """在原文上限制预览长度，切点避开联系方式，再遮蔽整段。"""
    span = pii_at_boundary(pii_spans(text), limit) if len(text) > limit > 0 else None
    return redact_pii(text[: span[0] if span else limit])


class EmptyModelResponseError(RuntimeError):
    """上游返回成功，但没有候选消息可供解析。"""


class IncompleteModelStreamError(RuntimeError):
    """流已断开或完成标记与实际响应不一致。"""


class BudgetExhaustedError(RuntimeError):
    """剩余额度不足以发送下一次模型请求。"""


class DailyModelQuotaError(RuntimeError):
    """账号当天的预扣额度不足。"""

    def __init__(self) -> None:
        super().__init__(
            "今天的模型额度已用完，明天会恢复；可在 .env 调整 DAILY_TOKEN_LIMIT"
        )


class ContextOverflowError(APIStatusError):
    """服务商拒绝了超出其窗口的请求，不按普通临时错误原样重试。"""


class ModelRunInactiveError(RuntimeError):
    """运行已取消或结束，不能再发送模型请求。"""


@dataclass
class ModelBudget:
    """保存同一次业务运行共享的剩余调用次数和 token 额度。"""

    remaining_calls: int
    remaining_tokens: int

    def reserve(self, estimated_tokens: int) -> None:
        """检查两项额度，通过后一起扣减。"""
        if self.remaining_calls < 1:
            raise BudgetExhaustedError("模型调用次数已用完")
        if estimated_tokens > self.remaining_tokens:
            raise BudgetExhaustedError("模型 token 额度不足")

        self.remaining_calls -= 1
        self.remaining_tokens -= estimated_tokens


@dataclass(frozen=True)
class AgentModelBudget:
    """业务调用可以共用一轮对话的持久化预算，包括改写子图的并发调用。"""

    run_id: UUID


@dataclass(frozen=True)
class CompactModelBudget:
    """独立压缩运行的额度，不从对话 Agent 扣减。"""

    run_id: UUID


ModelQuota = ModelBudget | AgentModelBudget | CompactModelBudget


async def request_completion(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    purpose: str,
    prompt_version: str,
    messages: Sequence[ChatCompletionMessageParam],
    json_output: bool,
    budget: ModelQuota,
    validate: Callable[[ChatCompletion], None] | None = None,
    tools: Sequence[ChatCompletionToolUnionParam] = (),
    on_text: Callable[[str], None] | None = None,
    max_output_tokens: int | None = None,
    max_input_tokens: int | None = None,
) -> ChatCompletion:
    """完成一次逻辑请求，返回通过 validate 的响应。

    失败时按错误种类处理（规则见 02 第 7 节「重试」「记录」）：
    - 超时、连接错误、5xx：等待后原样重发，总发送次数不超过 model_max_attempts。
    - JSON 输出未通过 Pydantic 校验：附上错误类型重发一次，不占上面的次数。
    - 4xx（含 429）、额度不足、其他异常：直接抛给调用方。
    每次重发都重新扣额度、另记一行 model_call。
    prompt_version 写入每行调用记录；修复重发时追加修复提示词的版本。
    json_output 与提示词和 validate 绑定，由调用方声明，不允许被环境变量改写。
    调用前需已提交所属 ai_run。
    """
    request_messages = list(messages)
    call_prompt_version = prompt_version
    upstream_attempts = 1
    repair_used = False
    text_emitted = False

    def emit_text(fragment: str) -> None:
        nonlocal text_emitted
        text_emitted = True
        if on_text is not None:
            on_text(fragment)

    while True:
        try:
            return await _send_once(
                session_factory,
                user_id,
                run_id,
                client=client,
                purpose=purpose,
                prompt_version=call_prompt_version,
                messages=request_messages,
                json_output=json_output,
                budget=budget,
                validate=validate,
                tools=tools,
                on_text=emit_text if on_text is not None else None,
                max_output_tokens=max_output_tokens,
                max_input_tokens=max_input_tokens,
            )

        except ValidationError as error:
            if not json_output or repair_used or text_emitted:
                raise

            repair_used = True
            repair_prompt = load_prompt("repair_json")
            request_messages.append(_repair_message(error, repair_prompt))
            call_prompt_version = f"{prompt_version}+{repair_prompt.label}"

        except (APIStatusError, APIConnectionError) as error:
            if text_emitted or not _is_retryable(error):
                raise
            if upstream_attempts >= settings.model_max_attempts:
                raise
            upstream_attempts += 1
            await sleep(settings.model_retry_delay_seconds)


def _is_retryable(error: APIStatusError | APIConnectionError) -> bool:
    """超时、连接错误和 5xx 可能是上游临时故障，重发有机会成功。

    APITimeoutError 是 APIConnectionError 的子类，这里一并处理。
    4xx 说明请求本身有问题，重发也不会变好。
    """
    if isinstance(error, APIConnectionError):
        return True
    return 500 <= error.status_code < 600


def _repair_message(
    error: ValidationError, repair_prompt: Prompt
) -> ChatCompletionMessageParam:
    """生成修复请求：只告诉模型错误类型，不回传原始输入和校验详情。"""
    issues = error.errors(include_input=False, include_context=False, include_url=False)
    error_types = ",".join(sorted({issue["type"] for issue in issues}))
    content = repair_prompt.text.format(error_types=error_types)
    return {"role": "user", "content": content}


async def _send_once(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    purpose: str,
    prompt_version: str,
    messages: Sequence[ChatCompletionMessageParam],
    json_output: bool,
    budget: ModelQuota,
    validate: Callable[[ChatCompletion], None] | None,
    tools: Sequence[ChatCompletionToolUnionParam],
    on_text: Callable[[str], None] | None,
    max_output_tokens: int | None,
    max_input_tokens: int | None,
) -> ChatCompletion:
    """实际发送一次：查配置并脱敏 → 扣额度 → 登记 unknown → 发送并校验 → 结算。

    这里是所有用途、首次发送与重发通往上游的唯一入口，脱敏放在这里不会漏。
    用途未知或额度不足时在登记前抛出，不会留下调用记录。
    上游错误和校验错误在结算后原样抛出，由 request_completion 决定是否重发。
    """
    model_settings = settings.model_for_purpose(purpose)
    profile = settings.model_profile(purpose)
    output = min(model_settings.max_tokens, profile.max_output_tokens)
    if max_output_tokens is not None:
        output = min(output, max_output_tokens)
    model_settings = model_settings.model_copy(update={"max_tokens": output})
    sanitized_messages = request_messages_for_model(messages, adapter=profile.adapter)
    estimated = _estimate_tokens(sanitized_messages, model_settings.max_tokens)
    if tools:
        estimated += len(dumps(list(tools), ensure_ascii=False).encode("utf-8"))
    if max_input_tokens is not None and estimated - output > max_input_tokens:
        raise BudgetExhaustedError("本次模型输入超过额度")
    call_id = await _register_call(
        session_factory,
        user_id,
        run_id,
        purpose,
        prompt_version,
        model_settings.model_name,
        budget,
        estimated,
    )

    started = monotonic()
    # 超时、连接错误时 outcome 保持 unknown：请求可能已被上游处理并计费，
    # 但我们拿不到用量，不能记成 0。
    outcome = CallOutcome.UNKNOWN
    usage: CompletionUsage | None = None
    try:
        if tools or on_text is not None:
            response = await _tool_completion(
                client,
                model_settings,
                sanitized_messages,
                json_output,
                tools,
                on_text,
                profile.adapter,
            )
        else:
            response = await _create_completion(
                client, model_settings, sanitized_messages, json_output, profile.adapter
            )
        usage = completion_usage(profile.adapter, response)
        # 先记为 invalid_output：validate 抛出任何异常都按校验失败结算。
        outcome = CallOutcome.INVALID_OUTPUT
        if not response.choices:
            raise EmptyModelResponseError("模型回包缺少候选消息")
        if validate is not None:
            validate(response)
        outcome = CallOutcome.SUCCESS
        return response
    except APIStatusError as error:
        outcome = CallOutcome.HTTP_ERROR
        if context_overflow(profile.adapter, error):
            raise ContextOverflowError(
                "模型上下文超长", response=error.response, body=None
            ) from None
        raise
    finally:
        # 进程被杀时 finally 可能不执行，登记行仍保留 unknown 和空用量。
        latency_ms = int((monotonic() - started) * 1000)
        await _settle_call(session_factory, call_id, outcome, latency_ms, usage)


def request_messages_for_model(
    messages: Sequence[ChatCompletionMessageParam],
    *,
    adapter: ProtocolAdapter = ProtocolAdapter.DEEPSEEK,
) -> list[ChatCompletionMessageParam]:
    """返回脱敏后的消息副本，不改动调用方传入的列表和字典。"""
    return [_sanitize_message(message, adapter=adapter) for message in messages]


def _sanitize_message(
    message: ChatCompletionMessageParam,
    *,
    adapter: ProtocolAdapter,
) -> ChatCompletionMessageParam:
    """复制单条消息并遮蔽其中的文本，兼容字符串和分段 content 两种形式。"""
    copied: dict[str, Any] = dict(message)
    content = copied.get("content")
    if isinstance(content, str):
        if copied["role"] == "tool":
            content = wrap_untrusted_text("tool_result", content)
        copied["content"] = redact_pii(content)
    elif isinstance(content, list):
        copied["content"] = [_sanitize_part(part) for part in content]
    if adapter == ProtocolAdapter.OPENAI:
        copied.pop("reasoning_content", None)
    if isinstance(reasoning := copied.get("reasoning_content"), str):
        copied["reasoning_content"] = redact_pii(reasoning)
    if copied.get("tool_calls"):
        copied["tool_calls"] = [
            _sanitize_tool_call(call) for call in copied["tool_calls"]
        ]
    return cast(ChatCompletionMessageParam, copied)


def _sanitize_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    if call["type"] != "function":
        return call
    function = call["function"]
    return {
        **call,
        "function": {**function, "arguments": redact_pii(function["arguments"])},
    }


def _sanitize_part(part: Any) -> Any:
    """分段 content 中只有 type 为 text 的段落有文本，其余（如图片）原样复制。"""
    if not isinstance(part, dict):
        return part
    copied = dict(part)
    if copied.get("type") == "text" and isinstance(copied.get("text"), str):
        copied["text"] = redact_pii(copied["text"])
    return copied


def _estimate_tokens(
    messages: Sequence[ChatCompletionMessageParam], max_output_tokens: int
) -> int:
    """估算一次发送最多消耗的 token，用于发送前扣额度。

    手头没有 DeepSeek 的分词器，输入部分用序列化后的 UTF-8 字节数代替：
    一个 token 至少对应一个字节，所以字节数只会多估、不会少估。
    输出部分按 max_tokens 上限计算。
    """
    serialized = dumps(list(messages), ensure_ascii=False)
    return len(serialized.encode("utf-8")) + max_output_tokens


async def _create_completion(
    client: AsyncOpenAI,
    model_settings: ModelSettings,
    messages: Sequence[ChatCompletionMessageParam],
    json_output: bool,
    adapter: ProtocolAdapter,
) -> ChatCompletion:
    """按用途配置组装参数并发送；关闭 SDK 自带重试，重试只由网关负责。"""
    return await client.with_options(max_retries=0).chat.completions.create(
        **_completion_args(model_settings, messages, json_output, adapter)
    )


def _completion_args(
    model_settings: ModelSettings,
    messages: Sequence[ChatCompletionMessageParam],
    json_output: bool,
    adapter: ProtocolAdapter,
) -> dict[str, Any]:
    return dict(
        model=model_settings.model_name,
        messages=messages,
        max_tokens=model_settings.max_tokens,
        response_format={"type": "json_object"} if json_output else omit,
        **protocol_parameters(adapter, model_settings),
    )


async def _tool_completion(
    client: AsyncOpenAI,
    model_settings: ModelSettings,
    messages: Sequence[ChatCompletionMessageParam],
    json_output: bool,
    tools: Sequence[ChatCompletionToolUnionParam],
    on_text: Callable[[str], None] | None,
    adapter: ProtocolAdapter,
) -> ChatCompletion:
    completions = client.with_options(max_retries=0).chat.completions
    arguments = _completion_args(model_settings, messages, json_output, adapter)
    arguments["tools"] = tools or omit
    if on_text is None:
        return await completions.create(**arguments)
    return await _read_completion_stream(completions, arguments, on_text)


async def _read_completion_stream(
    completions: AsyncCompletions,
    arguments: dict[str, Any],
    on_text: Callable[[str], None],
) -> ChatCompletion:
    """先确认首选响应确实结束，再让 SDK 组装最终回复。"""
    finish_reason = None
    async with completions.stream(
        **arguments, stream_options={"include_usage": True}
    ) as stream:
        async for event in stream:
            if event.type == "chunk":
                for choice in event.chunk.choices:
                    if choice.index == 0 and choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
            if event.type == "content.delta":
                on_text(event.delta)
        if finish_reason not in ("stop", "tool_calls"):
            raise IncompleteModelStreamError("未收到完整的模型响应")
        completion = await stream.get_final_completion()
    response = completion.choices[0].message
    if finish_reason == "tool_calls" and response.tool_calls:
        return completion
    if (
        finish_reason == "stop"
        and (response.content or "").strip()
        and not response.tool_calls
    ):
        return completion
    raise IncompleteModelStreamError("模型完成标记与响应内容不一致")


async def _register_call(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    purpose: str,
    prompt_version: str,
    model_name: str,
    budget: ModelQuota,
    estimated_tokens: int,
) -> UUID:
    """发送前用独立事务登记一行 unknown，返回供结算使用的 call_id。"""
    budget_run_id = budget.run_id if not isinstance(budget, ModelBudget) else None
    if isinstance(budget, ModelBudget):
        budget.reserve(estimated_tokens)
    async with session_factory.begin() as session:
        if not await lock_active_account(session, user_id):
            raise ModelRunInactiveError("运行已取消或结束")
        await lock_daily_quota(session, user_id)
        now = datetime.now().astimezone()
        used = await daily_reserved_tokens(session, user_id, now)
        if used + estimated_tokens > settings.daily_token_limit:
            raise DailyModelQuotaError()
        if budget_run_id is not None:
            await _check_persisted_budget(session, user_id, budget, estimated_tokens)
        return await _insert_call(
            session,
            user_id,
            run_id,
            purpose,
            prompt_version,
            model_name,
            budget_run_id=budget_run_id,
            estimated_tokens=estimated_tokens,
            now=now,
        )


async def _insert_call(
    session: AsyncSession,
    user_id: UUID,
    run_id: UUID,
    purpose: str,
    prompt_version: str,
    model_name: str,
    *,
    budget_run_id: UUID | None,
    estimated_tokens: int,
    now: datetime,
) -> UUID:
    call_id = uuid4()
    # 共享行锁让登记和取消有明确的先后。模型请求在短事务提交后才发出。
    eligible_run = (
        select(
            literal(call_id),
            AiRun.user_id,
            AiRun.run_id,
            literal(purpose),
            literal(prompt_version),
            literal(model_name),
            literal(CallOutcome.UNKNOWN),
            literal(budget_run_id, type_=AiRun.run_id.type),
            literal(estimated_tokens),
            literal(now, type_=ModelCall.created_at.type),
        )
        .where(
            AiRun.user_id == user_id,
            AiRun.run_id == run_id,
            AiRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .with_for_update(read=True, of=AiRun)
    )
    registered = await session.scalar(
        insert(ModelCall)
        .from_select(
            [
                "call_id",
                "user_id",
                "run_id",
                "purpose",
                "prompt_version",
                "model",
                "outcome",
                "budget_run_id",
                "reserved_tokens",
                "created_at",
            ],
            eligible_run,
        )
        .returning(ModelCall.call_id)
    )
    if registered is None:
        raise ModelRunInactiveError("运行已取消或结束")
    return registered


async def _check_persisted_budget(
    session: AsyncSession,
    user_id: UUID,
    budget: AgentModelBudget | CompactModelBudget,
    estimated_tokens: int,
) -> None:
    """锁住父运行，确保并行子调用不能共享同一份剩余额度。"""
    run_id = budget.run_id
    compact = isinstance(budget, CompactModelBudget)
    kind = "compact" if compact else "agent"
    active = await session.scalar(
        select(AiRun.run_id)
        .where(
            AiRun.user_id == user_id,
            AiRun.run_id == run_id,
            AiRun.kind == kind,
            AiRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .with_for_update(key_share=True)
    )
    if active is None:
        raise ModelRunInactiveError("运行已取消或结束")
    limits = settings.agent_limits
    call_limit = limits.compact_model_calls if compact else limits.model_calls
    output = min(
        settings.compact.max_tokens, settings.model_profile("compact").max_output_tokens
    )
    compact_tokens = (limits.compact_input_tokens + output) * call_limit
    token_limit = compact_tokens if compact else limits.model_tokens
    # 旧记录没有预扣值。缺少实际用量时保守地用本轮 token 上限。
    reserved = func.coalesce(
        ModelCall.reserved_tokens,
        ModelCall.prompt_tokens + ModelCall.completion_tokens,
        token_limit,
    )
    calls, tokens = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(reserved), 0)).where(
                (ModelCall.budget_run_id == run_id)
                | ((ModelCall.run_id == run_id) & (ModelCall.user_id == user_id)),
            )
        )
    ).one()
    ModelBudget(call_limit - calls, token_limit - tokens).reserve(estimated_tokens)


async def _settle_call(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    outcome: CallOutcome,
    latency_ms: int,
    usage: CompletionUsage | None,
) -> None:
    """用另一个独立事务回填结果、耗时和用量；没收到响应时用量留空。"""
    async with session_factory.begin() as session:
        await session.execute(
            update(ModelCall)
            .where(ModelCall.call_id == call_id)
            .values(
                outcome=outcome,
                latency_ms=latency_ms,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
            )
        )
