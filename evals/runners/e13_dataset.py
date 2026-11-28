"""校验 E13 合成对话和消息标注，给检索评测与 AB 提供共同的起始状态。"""

from datetime import UTC, datetime
from hashlib import sha256
from json import loads
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import AiRun, Conversation, Message, MessageRole, RunStatus

E13_DIR = REPO_ROOT / "evals" / "datasets" / "e13"
DATASET_VERSION = "e13-v2"
Split = Literal["dev", "test"]
QueryType = Literal["exact", "paraphrase", "gist", "no_answer"]


class _DatasetRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Turn(_DatasetRow):
    user: str = Field(min_length=4)
    assistant: str = Field(min_length=4)


class ConversationSample(_DatasetRow):
    id: str = Field(pattern=r"^c\d{2}$")
    split: Split
    topic: str
    title: str = Field(min_length=1, max_length=200)
    turns: list[Turn] = Field(min_length=6, max_length=20)

    @property
    def message_labels(self) -> set[str]:
        return {
            f"{self.id}:{role}{index:02}"
            for index in range(1, len(self.turns) + 1)
            for role in ("u", "a")
        }


class Query(_DatasetRow):
    id: str
    query: str = Field(min_length=1, max_length=120)
    type: QueryType
    relevant: list[str]


def _validated_queries(split: Split, valid_refs: set[str]) -> list[Query]:
    path = E13_DIR / f"queries-{split}.jsonl"
    queries = [
        Query.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    if {query.id for query in queries} != {
        f"{split}-{number:02}" for number in range(1, 31)
    }:
        raise ValueError(f"E13 {split} 查询 ID 不完整")
    if len(queries) != 30 or len({query.query for query in queries}) != 30:
        raise ValueError(f"E13 {split} 查询有重复")
    for query in queries:
        refs = set(query.relevant)
        if (
            not refs <= valid_refs
            or len(refs) != len(query.relevant)
            or bool(refs) != (query.type != "no_answer")
        ):
            raise ValueError(f"E13 查询 {query.id} 的来源标注无效")
    for query_type, count in {
        "exact": 8,
        "paraphrase": 8,
        "gist": 8,
        "no_answer": 6,
    }.items():
        if sum(query.type == query_type for query in queries) != count:
            raise ValueError(f"E13 {split} 查询类型 {query_type} 数量无效")
    if not any(
        ref.split(":")[1].startswith("a") for query in queries for ref in query.relevant
    ):
        raise ValueError(f"E13 {split} 查询需要覆盖 Agent 回复")
    return queries


def _validate_no_cross_conversation_repeats(samples: list[ConversationSample]) -> None:
    """忽略空白，识别被不同对话复用的整条消息。"""
    seen_in: dict[str, str] = {}
    for sample in samples:
        for turn in sample.turns:
            for message in (turn.user, turn.assistant):
                normalized = "".join(message.split())
                if not normalized:
                    raise ValueError(f"E13 对话 {sample.id} 存在空消息")
                previous = seen_in.setdefault(normalized, sample.id)
                if previous != sample.id:
                    raise ValueError(f"E13 对话 {previous} 和 {sample.id} 存在重复消息")


def load_e13(split: Split) -> tuple[list[ConversationSample], list[Query]]:
    """检查切分和标注归属，保留可给 AB 按 ID 重用的对话。"""
    samples = [
        ConversationSample.model_validate_json(line)
        for line in (E13_DIR / "conversations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    if len(samples) != 30 or len({sample.id for sample in samples}) != 30:
        raise ValueError("E13 必须包含 30 段 ID 唯一的对话")
    _validate_no_cross_conversation_repeats(samples)
    selected = [sample for sample in samples if sample.split == split]
    if len(selected) != 15:
        raise ValueError(f"E13 {split} 必须包含 15 段对话")
    valid_refs = set().union(*(sample.message_labels for sample in selected))
    queries = _validated_queries(split, valid_refs)
    if split == "test":
        seal = loads((E13_DIR / "test-seal.json").read_text(encoding="utf-8"))
        if seal != {"version": DATASET_VERSION, "sha256": _digest(selected, queries)}:
            raise ValueError("E13 正式测试集与封存指纹不一致")
    return selected, queries


def _digest(samples: list[ConversationSample], queries: list[Query]) -> str:
    sha = sha256()
    for row in [*samples, *queries]:
        sha.update(row.model_dump_json().encode() + b"\n")
    return sha.hexdigest()


def dataset_digest(split: Split) -> str:
    """对最终分割的语料与标注签名，用于核对评测记录。"""
    return _digest(*load_e13(split))


async def _seed_conversation(
    session: AsyncSession, user_id: UUID, sample: ConversationSample
) -> dict[UUID, str]:
    conversation = Conversation(user_id=user_id, title=sample.title)
    session.add(conversation)
    await session.flush()
    labels: dict[UUID, str] = {}
    for number, turn in enumerate(sample.turns, 1):
        run = AiRun(
            user_id=user_id,
            conversation_id=conversation.conversation_id,
            kind="agent",
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        for role, event, short in (
            (MessageRole.USER, "input", "u"),
            (MessageRole.ASSISTANT, "final", "a"),
        ):
            message = Message(
                run_id=run.run_id,
                user_id=user_id,
                conversation_id=conversation.conversation_id,
                role=role,
                event_key=event,
                content=getattr(turn, role.value),
            )
            session.add(message)
            await session.flush()
            labels[message.message_id] = f"{sample.id}:{short}{number:02}"
    return labels


async def seed_conversations(
    session: AsyncSession, user_id: UUID, samples: list[ConversationSample]
) -> dict[int, str]:
    """调用方提交事务，返回数据库序号到数据集标签的映射。"""
    labels: dict[UUID, str] = {}
    for sample in samples:
        labels.update(await _seed_conversation(session, user_id, sample))
    rows = await session.execute(
        select(Message.message_id, Message.seq).where(Message.user_id == user_id)
    )
    return {seq: labels[message_id] for message_id, seq in rows if message_id in labels}
