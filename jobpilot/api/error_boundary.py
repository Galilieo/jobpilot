"""在 ASGI 服务器接手前处理逃出 FastAPI 路由的请求异常。"""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobpilot.diagnostics import error_locations

logger = logging.getLogger("uvicorn.error").getChild("jobpilot")


class RequestErrorBoundary:
    """保留框架的业务响应，为未处理异常提供固定的 500。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def track_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, track_response)
        except Exception as error:
            # Starlette 的全局 Exception handler 响应后仍会重新抛出原异常。
            route_template = getattr(scope.get("route"), "path", "<unknown>")
            logger.error(
                "%s %s 出错（%s），调用栈：%s",
                scope["method"],
                route_template,
                type(error).__name__,
                error_locations(error),
            )
            if response_started:
                # 响应已发送，交给服务器结束连接。断开原异常的输出链。
                raise RuntimeError("响应发送失败") from None
            response = JSONResponse({"detail": "服务器内部错误"}, status_code=500)
            await response(scope, receive, send)
