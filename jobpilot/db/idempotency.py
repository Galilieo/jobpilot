"""领取、关联运行并完成付费请求的幂等登记。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Update, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.unitofwork import UOWTransaction

from jobpilot.db.models import AiRun, IdempotencyKey, ResumeDocument, RunStatus
from jobpilot.db.runs import set_run_status


class Operation(StrEnum):
    PARSE_RESUME = "parse_resume"
    PARSE_JD = "parse_jd"
    MATCH = "match"
    TAILOR = "tailor"  # 成功指运行已创建；后台运行之后失败，同一键仍返回这个运行


class ClaimConflict(StrEnum):
    PROCESSING = "processing"
    HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: str


@dataclass(frozen=True)
class KeyScope:
    user_id: UUID
    operation: Operation
    key: str

    def columns(self) -> dict[str, str | UUID]:
        return {
            "user_id": self.user_id,
            "operation": self.operation,
            "key": self.key,
        }


@dataclass(frozen=True)
class Attempt:
    scope: KeyScope
    attempt_id: UUID
    resume_doc_id: UUID | None = None

    def _update(self, **values: Any) -> Update:
        return (
            update(IdempotencyKey)
            .filter_by(**self.scope.columns(), attempt_id=self.attempt_id)
            .where(IdempotencyKey.status == "processing")
            .values(**values)
        )

    def bind_run(self, session: Session, _flush_context: UOWTransaction) -> None:
        """运行与幂等键在首次提交时一起登记。flush 后可以读到生成的主键。"""
        runs = [
            row
            for row in session.new
            if isinstance(row, AiRun)
            and row.kind == self.scope.operation
            and row.user_id == self.scope.user_id
        ]
        if not runs:
            return
        (run,) = runs
        statement = self._update(run_id=run.run_id)
        if self.scope.operation is Operation.PARSE_RESUME:
            doc_id = self.resume_doc_id
            if doc_id is None:
                (doc,) = (
                    row
                    for row in session.new
                    if isinstance(row, ResumeDocument)
                    and row.user_id == self.scope.user_id
                )
                doc_id = doc.doc_id
            statement = statement.values(result_id=doc_id)
        if session.connection().execute(statement).rowcount != 1:
            raise RuntimeError("付费请求的幂等尝试已失效")

    async def succeed(
        self, db: AsyncSession, result_id: UUID, response: StoredResponse
    ) -> None:
        changed = await db.scalar(
            self._update(
                status="succeeded",
                result_id=result_id,
                response_status=response.status_code,
                response_body=response.body,
            ).returning(IdempotencyKey.attempt_id)
        )
        if changed is None:
            raise RuntimeError("付费请求的幂等尝试已失效")

    async def fail(self, db: AsyncSession) -> None:
        run_id = await db.scalar(
            self._update(status="failed").returning(IdempotencyKey.run_id)
        )
        if run_id is not None:
            await set_run_status(db, self.scope.user_id, run_id, RunStatus.FAILED)


async def claim(
    db: AsyncSession, scope: KeyScope, request_hash: str
) -> Attempt | StoredResponse | ClaimConflict:
    """在调用方的短事务里领取键、判断请求冲突或读出成功快照。"""
    attempt_id = uuid4()
    created = await db.scalar(
        insert(IdempotencyKey)
        .values(
            **scope.columns(),
            request_hash=request_hash,
            attempt_id=attempt_id,
            status="processing",
        )
        .on_conflict_do_nothing()
        .returning(IdempotencyKey.attempt_id)
    )
    if created is not None:
        return Attempt(scope, attempt_id)

    existing = (
        await db.scalars(
            select(IdempotencyKey).filter_by(**scope.columns()).with_for_update()
        )
    ).one()
    if existing.request_hash != request_hash:
        return ClaimConflict.HASH_MISMATCH
    if existing.status == "processing":
        return ClaimConflict.PROCESSING
    if existing.status == "succeeded":
        assert existing.response_status is not None
        assert existing.response_body is not None
        return StoredResponse(existing.response_status, existing.response_body)

    existing.status = "processing"
    existing.attempt_id = attempt_id
    existing.run_id = None
    return Attempt(scope, attempt_id, existing.result_id)


async def fail_interrupted_keys(db: AsyncSession) -> None:
    """单进程服务启动时回收未完成的键；原运行和调用记录仍可追查。"""
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.status == "processing")
        .values(status="failed")
    )


async def delete_result_snapshots(
    db: AsyncSession, user_id: UUID, operation: Operation, result_id: UUID
) -> None:
    """删除已被移除的业务结果所对应的所有成功快照。"""
    await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.operation == operation,
            IdempotencyKey.result_id == result_id,
            IdempotencyKey.status == "succeeded",
        )
    )
