"""账号、登录 session 与两阶段删除的数据访问。

密码和 token 的哈希在 services/auth.py 完成，这里只存取哈希后的值。
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, exists, func, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from jobpilot.db.models import User, UserSession, normalize_email

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


async def lock_active_account(session: AsyncSession, user_id: UUID) -> bool:
    """在提交本次运行前与删号互斥，避免认证后才开始的运行逃过取消。"""
    account_id = await session.scalar(
        select(User.user_id)
        .where(User.user_id == user_id, User.deletion_requested_at.is_(None))
        .with_for_update(read=True)
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
    valid_session = exists().where(
        UserSession.session_id == login.session_id,
        UserSession.expires_at > func.clock_timestamp(),
    )
    changed = await session.scalar(
        update(User)
        .where(
            User.user_id == user_id, User.deletion_requested_at.is_(None), valid_session
        )
        .values(deletion_requested_at=func.clock_timestamp())
        .returning(User.user_id)
    )
    return changed is not None


async def revoke_account_sessions(session: AsyncSession, user_id: UUID) -> None:
    """注销该账号的全部会话，和取消运行、记录申请一起提交。"""
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))


async def restore_account(session: AsyncSession, user_id: UUID) -> bool:
    """仅恢复尚未到期的账号；重复恢复已成功的账号也返回成功。"""
    restored = await session.scalar(
        update(User)
        .where(
            User.user_id == user_id,
            or_(
                User.deletion_requested_at.is_(None),
                User.deletion_requested_at
                > func.clock_timestamp() - _DELETION_INTERVAL,
            ),
        )
        .values(deletion_requested_at=None)
        .returning(User.user_id)
    )
    return restored is not None


def _deletion_due() -> ColumnElement[bool]:
    return User.deletion_requested_at <= func.clock_timestamp() - _DELETION_INTERVAL


async def due_account_ids(session: AsyncSession) -> list[UUID]:
    """只拿到期账号 ID，清理时再逐个核对。"""
    return list(await session.scalars(select(User.user_id).where(_deletion_due())))


async def delete_due_account(session: AsyncSession, user_id: UUID) -> bool:
    """在删除事务里重新核对到期状态，防止删掉已恢复的账号。"""
    deleted = await session.scalar(
        delete(User)
        .where(User.user_id == user_id, _deletion_due())
        .returning(User.user_id)
    )
    return deleted is not None
