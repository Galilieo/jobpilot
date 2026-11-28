"""校验 E13 合成对话和消息标注，给检索评测与 AB 提供共同的起始状态。"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from json import loads
from typing import Literal
from unicodedata import normalize
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import AiRun, Conversation, Message, MessageRole, RunStatus
from jobpilot.services.message_index import CHUNK_MAX_CHARS, chunk_spans

E13_DIR = REPO_ROOT / "evals" / "datasets" / "e13"
DATASET_VERSION = "e13-v3"
Split = Literal["dev", "test"]
QueryType = Literal["exact", "paraphrase", "gist", "no_answer"]
NoAnswerKind = Literal["missing_number", "unseen_topic"]


class _DatasetRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Turn(_DatasetRow):
    user: str = Field(min_length=4)
    assistant: str = Field(min_length=4)
    jd_span: tuple[int, int] | None = None


class ConversationSample(_DatasetRow):
    id: str = Field(pattern=r"^c\d{2}$")
    split: Split
    topic: str
    title: str = Field(min_length=1, max_length=200)
    turns: list[Turn] = Field(min_length=6, max_length=20)

    def labeled_messages(self) -> Iterator[tuple[str, str]]:
        for index, turn in enumerate(self.turns, 1):
            yield f"{self.id}:u{index:02}", turn.user
            yield f"{self.id}:a{index:02}", turn.assistant

    @property
    def message_labels(self) -> set[str]:
        return {label for label, _ in self.labeled_messages()}


class Query(_DatasetRow):
    id: str
    query: str = Field(min_length=1, max_length=120)
    type: QueryType
    relevant: list[str]
    no_answer_kind: NoAnswerKind | None = None


def chunk_reference(message_label: str, start: int) -> str:
    return f"{message_label}@{start}"


def matching_refs(chunk_label: str, relevant: set[str]) -> set[str]:
    """整条消息接受任一块，指定字符起点的标注只接受该块。"""
    return relevant.intersection((chunk_label.partition("@")[0], chunk_label))


def corpus_chunks(samples: list[ConversationSample]) -> dict[str, str]:
    """复用产品的字符跨度，保留实际能进入索引的分块。"""
    return {
        chunk_reference(label, span.start): content[span.start : span.end]
        for sample in samples
        for label, content in sample.labeled_messages()
        for span in chunk_spans(content)
        if content[span.start : span.end].strip()
    }


@dataclass(frozen=True)
class LiteralOverlap:
    rank: int | None
    top_fraction: float


def _character_bigrams(text: str) -> set[str]:
    normalized = "".join(
        char for char in normalize("NFKC", text).casefold() if char.isalnum()
    )
    return {first + second for first, second in pairwise(normalized)}


def literal_overlap(
    queries: list[Query], chunks: dict[str, str]
) -> dict[str, LiteralOverlap]:
    """给并列项相同名次；相关块与查询没有共有二元组时不给名次。"""
    by_chunk = {label: _character_bigrams(text) for label, text in chunks.items()}
    rankings: dict[str, LiteralOverlap] = {}
    for query in queries:
        bigrams = _character_bigrams(query.query)
        relevant = set(query.relevant)
        scores = {label: len(terms & bigrams) for label, terms in by_chunk.items()}
        best = max(
            (
                score
                for label, score in scores.items()
                if matching_refs(label, relevant)
            ),
            default=0,
        )
        rank = 1 + sum(score > best for score in scores.values()) if best else None
        top_fraction = max(scores.values(), default=0) / (len(bigrams) or 1)
        rankings[query.id] = LiteralOverlap(rank, top_fraction)
    return rankings


def message_literal_overlap(
    queries: list[Query], messages: dict[str, str]
) -> dict[str, LiteralOverlap]:
    """把段落标注还原为消息标注，检查换说法是否依赖整条原文的字面匹配。"""
    whole_message_queries = [
        query.model_copy(
            update={
                "relevant": sorted({ref.partition("@")[0] for ref in query.relevant})
            }
        )
        for query in queries
    ]
    return literal_overlap(whole_message_queries, messages)


def _validated_queries(
    split: Split, messages: dict[str, str], chunks: dict[str, str]
) -> list[Query]:
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
    valid_refs = messages.keys() | chunks.keys()
    for query in queries:
        refs = set(query.relevant)
        if (
            not refs <= valid_refs
            or len(refs) != len(query.relevant)
            or bool(refs) != (query.type != "no_answer")
            or bool(query.no_answer_kind) != (query.type == "no_answer")
            or any(ref.partition("@")[0] in refs for ref in refs if "@" in ref)
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
    no_answer_kinds = Counter(
        query.no_answer_kind for query in queries if query.type == "no_answer"
    )
    if no_answer_kinds != {"missing_number": 3, "unseen_topic": 3}:
        raise ValueError(f"E13 {split} 两种无答案查询各需 3 条")
    _validate_paraphrases(queries, messages, chunks)
    return queries


def _validate_paraphrases(
    queries: list[Query], messages: dict[str, str], chunks: dict[str, str]
) -> None:
    paraphrases = [query for query in queries if query.type == "paraphrase"]
    for candidate_type, ranks in (
        ("原消息", message_literal_overlap(paraphrases, messages)),
        ("实际分块", literal_overlap(paraphrases, chunks)),
    ):
        for query_id, overlap in ranks.items():
            if overlap.rank == 1:
                raise ValueError(
                    f"E13 换说法查询 {query_id} 的{candidate_type}字面排名第 1"
                )


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


def _validate_length_profile(split: Split, samples: list[ConversationSample]) -> None:
    reply_lengths = [len(turn.assistant) for sample in samples for turn in sample.turns]
    if 2 * sum(100 <= size <= 400 for size in reply_lengths) <= len(reply_lengths):
        raise ValueError(f"E13 {split} 的 100～400 字 Agent 回复未过半")
    conversations_with_jd = 0
    for sample in samples:
        if not any(
            len(turn.assistant) > CHUNK_MAX_CHARS and "\n\n" in turn.assistant
            for turn in sample.turns
        ):
            raise ValueError(f"E13 对话 {sample.id} 缺少超过 500 字的多段回复")
        has_jd = False
        for turn in sample.turns:
            if turn.jd_span is None:
                continue
            start, end = turn.jd_span
            if not 0 <= start < end <= len(turn.user) or not 200 <= end - start <= 800:
                raise ValueError(f"E13 对话 {sample.id} 的 JD 正文跨度无效")
            has_jd = True
        conversations_with_jd += has_jd
    if conversations_with_jd != 5:
        raise ValueError(f"E13 {split} 应有 5 段含 JD 正文的对话")


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
    _validate_length_profile(split, selected)
    chunks = corpus_chunks(selected)
    messages = dict(pair for sample in selected for pair in sample.labeled_messages())
    queries = _validated_queries(split, messages, chunks)
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
