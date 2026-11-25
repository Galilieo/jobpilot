"""组装账号的全量归档，并为每份定制简历版本渲染离线 HTML。"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.account_export import export_business_records, export_identity
from jobpilot.services.resume_content import ResumeContent
from jobpilot.services.resume_export import RenderMode, render_html


async def build_account_export(
    session: AsyncSession, user_id: UUID
) -> dict[str, Any] | None:
    async with session.begin():
        # 各表在同一快照读取，此时保存新简历也不会让版本指针悬空。
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        account = await export_identity(session, user_id)
        if account is None:
            return None
        records = await export_business_records(session, user_id)

    for version in records["resume_versions"]:
        content = ResumeContent.model_validate(version["content_json"])
        version["html"] = render_html(
            content, mode=RenderMode.EXPORT, version=version["version"]
        )
    return {"format_version": 1, "account": account, **records}
