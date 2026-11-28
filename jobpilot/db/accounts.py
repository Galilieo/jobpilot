"""账号、登录 session 与两阶段删除的数据访问。

密码和 token 的哈希在 services/auth.py 完成，这里只存取哈希后的值。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import delete, exists, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from jobpilot.db.checkpoints import delete_checkpoint_threads
from jobpilot.db.models import AiRun, User, UserSession, normalize_email

DELETION_GRACE_PERIOD = timedelta(days=7)
# 使用小时单位，保证数据库与前端显示的截止时间都是申请后 168 小时。
_GRACE_HOURS = DELETION_GRACE_PERIOD // timedelta(hours=1)
_DELETION_INTERVAL = literal_column(f"INTERVAL '{_GRACE_HOURS} hours'")


@dataclass(frozen=True)
class LoginSession:
    """一条有效的登录记录及其所属账号，供依赖函数和登出使用。"""

    session_id: UUID
    user: User


async def find_account_by_email(session: AsyncSession, email: str) -> User | None:
    """按登录标识查账号，账号状态和密码由调用方校验。"""
    return await session.scalar(
        select(User).where(func.lower(User.email) == normalize_email(email))
    )


async def create_account(
    session: AsyncSession, email: str, hashed_password: str
) -> User:
    """写入新账号并 flush；邮箱大小写变体已存在时由唯一索引抛出 IntegrityError。"""
    user = User(email=email, hashed_password=hashed_password)
    session.add(user)
    await session.flush()
    return user


async def create_login_session(
    session: AsyncSession, user_id: UUID, token_hash: str, max_age_seconds: int
) -> None:
    """登记一次登录；到期时间用数据库时钟计算，与查找时的比较保持同一时钟。"""
    session.add(
        UserSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=func.now() + timedelta(seconds=max_age_seconds),
        )
    )
    await session.flush()


async def lock_account_for_session(session: AsyncSession, user_id: UUID) -> bool:
    """与注销全部 session 的事务互斥，并识别验证密码期间已被清理的账号。"""
    account_id = await session.scalar(
        select(User.user_id).where(User.user_id == user_id).with_for_update(read=True)
    )
    return account_id is not None


async def lock_active_account(
    session: AsyncSession, user_id: UUID, *, exclusive: bool = False
) -> bool:
    """在提交本次运行前与删号互斥，避免认证后才开始的运行逃过取消。"""
    account_id = await session.scalar(
        select(User.user_id)
        .where(User.user_id == user_id, User.deletion_requested_at.is_(None))
        .with_for_update(read=not exclusive)
    )
    return account_id is not None


async def find_login_session(
    session: AsyncSession, token_hash: str
) -> LoginSession | None:
    """按 token 哈希查找未过期的登录及其账号；不检查账号是否待删除。"""
    row = (
        await session.execute(
            select(UserSession.session_id, User)
            .join(User, User.user_id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.expires_at > func.now(),
            )
        )
    ).first()
    if row is None:
        return None
    return LoginSession(session_id=row.session_id, user=row.User)


async def delete_login_session(session: AsyncSession, session_id: UUID) -> None:
    """删除一条登录记录，使对应 token 立即失效。"""
    await session.execute(
        delete(UserSession).where(UserSession.session_id == session_id)
    )


async def mark_deletion_requested(session: AsyncSession, login: LoginSession) -> bool:
    """首次有效申请才记时间；等待期间已被注销的旧 session 不能再次申请。"""
    user_id = login.user.user_id
    # NO KEY UPDATE 与业务表外键的 KEY SHARE 兼容，运行收尾仍能先提交。
    locked = await session.scalar(
        select(User.user_id)
        .where(User.user_id == user_id)
        .with_for_update(key_share=True)
    )
    if locked is None:
        return False
    changed = await session.scalar(
        update(User)
        .where(
            User.user_id == user_id,
            User.deletion_requested_at.is_(None),
            _live_session(login),
        )
        .values(deletion_requested_at=func.clock_timestamp())
        .returning(User.user_id)
    )
    return changed is not None


def _live_session(login: LoginSession) -> ColumnElement[bool]:
    """这个登录现在是否仍然有效：进锁之前，它可能已被注销全部登录的事务撤销。"""
    return exists().where(
        UserSession.session_id == login.session_id,
        UserSession.user_id == login.user.user_id,
        UserSession.expires_at > func.clock_timestamp(),
    )


async def revoke_account_sessions(session: AsyncSession, user_id: UUID) -> None:
    """注销该账号的全部会话，和取消运行、记录申请一起提交。"""
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))


class RestoreResult(StrEnum):
    RESTORED = "restored"  # 撤销了这次申请；账号已经不在待删除时也是它
    EXPIRED = "expired"  # 这次申请已经过了 7 天恢复期
    STALE = "stale"  # 认证之后账号有了新的删除申请，或者这个登录已被注销


async def restore_account(session: AsyncSession, login: LoginSession) -> RestoreResult:
    """撤销认证时看到的那一次删除申请；同一次申请重复恢复也返回成功。

    只按 user_id 更新的话，同一个恢复发了两次、第一个完成后用户又申请了删除，
    第二个请求这时才执行，会把新的申请也撤销。所以在账号行锁内重新核对登录，
    并且要求账号上的申请时间就是认证时看到的那个。
    """
    user_id = login.user.user_id
    seen = login.user.deletion_requested_at
    # NO KEY UPDATE 与业务表外键的 KEY SHARE 兼容，运行收尾仍能先提交。
    locked = await session.execute(
        select(User.user_id, User.deletion_requested_at)
        .where(User.user_id == user_id)
        .with_for_update(key_share=True)
    )
    row = locked.one_or_none()
    if row is None:
        return RestoreResult.EXPIRED  # 账号已经被清理
    if row.deletion_requested_at is None:
        return RestoreResult.RESTORED  # 没有待删除的申请：已经恢复过，或本来就没申请
    if row.deletion_requested_at != seen:
        return RestoreResult.STALE  # 换成了一次新的申请，这是旧请求，不能动它
    return await _cancel_deletion_request(session, login, seen)


async def _cancel_deletion_request(
    session: AsyncSession, login: LoginSession, seen: datetime
) -> RestoreResult:
    """撤销这一次还没到期的申请；调用方已经锁住账号行。

    没更新成时再分清是哪一种：过了恢复期，还是这个登录在进锁之前就被注销了。
    """
    user_id = login.user.user_id
    restored = await session.scalar(
        update(User)
        .where(
            User.user_id == user_id,
            User.deletion_requested_at == seen,
            User.deletion_requested_at > func.clock_timestamp() - _DELETION_INTERVAL,
            _live_session(login),
        )
        .values(deletion_requested_at=None)
        .returning(User.user_id)
    )
    if restored is not None:
        return RestoreResult.RESTORED
    live = await session.scalar(select(_live_session(login)))
    return RestoreResult.EXPIRED if live else RestoreResult.STALE


def _deletion_due() -> ColumnElement[bool]:
    return User.deletion_requested_at <= func.clock_timestamp() - _DELETION_INTERVAL


async def due_account_ids(session: AsyncSession) -> list[UUID]:
    """只拿到期账号 ID，清理时再逐个核对。"""
    return list(await session.scalars(select(User.user_id).where(_deletion_due())))


async def delete_due_account(session: AsyncSession, user_id: UUID) -> bool:
    """在删除事务里重新核对到期状态，防止删掉已恢复的账号。"""
    locked = await session.scalar(
        select(User.user_id)
        .where(User.user_id == user_id, _deletion_due())
        .with_for_update()
    )
    if locked is None:
        return False
    threads = await session.scalars(
        select(AiRun.thread_id)
        .where(AiRun.user_id == user_id, AiRun.thread_id.is_not(None))
        .distinct()
    )
    await delete_checkpoint_threads(session, [thread for thread in threads if thread])
    await session.execute(delete(User).where(User.user_id == user_id))
    return True
