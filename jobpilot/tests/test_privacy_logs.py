"""经 HTTP 触发真实数据库约束错误，验证响应与应用错误日志。"""

from typing import Annotated

import httpx
import pytest
from fastapi import Body
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jobpilot.api.deps import DbSession
from jobpilot.main import app

_RESUME_MARK = "合成简历隐私测试正文"
_PHONE = "13800001234"


async def test_constraint_error_does_not_escape_to_server_logs(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """用 PostgreSQL 生成带有失败行的 DETAIL，再检查应用的响应和日志。"""

    async def write_resume(
        sample: str, body: Annotated[str, Body(embed=True)], db: DbSession
    ) -> None:
        await db.execute(
            text(
                "CREATE TEMP TABLE privacy_sample (body text CHECK (body = '')) "
                "ON COMMIT DROP"
            )
        )
        try:
            await db.execute(
                text("INSERT INTO privacy_sample VALUES (:body)"),
                {"body": body},
            )
        except IntegrityError as error:
            # 确保泄露风险确实来自服务端 DETAIL，即使语句参数已隐藏。
            assert all(
                part in error.orig.diag.message_detail
                for part in (_RESUME_MARK, _PHONE)
            )
            raise

    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    app.add_api_route("/api/privacy-sample/{sample}", write_resume, methods=["POST"])

    # 保留 ASGITransport 默认的异常传播，和 uvicorn 一样能看到逃逸的异常。
    response = await async_client.post(
        f"/api/privacy-sample/{_PHONE}",
        params={"contact": _PHONE},
        json={"body": f"{_RESUME_MARK} {_PHONE}"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "服务器内部错误"}
    assert _RESUME_MARK not in caplog.text
    assert _PHONE not in caplog.text
    assert "POST /api/privacy-sample/{sample}" in caplog.text
    assert "IntegrityError" in caplog.text
    assert "write_resume" in caplog.text
