"""截图与扫描件识别接口：识别结果只返回给前端核对，图片和文字都不保存。

简历和 JD 共用这个接口。用户在文本框里核对、修改后，再把文字提交给各自的上传接口。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import anyio
import anyio.to_thread
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from jobpilot.api.deps import CurrentUser
from jobpilot.services.parsing import (
    MAX_OCR_IMAGES,
    ParseError,
    UnsupportedFormatError,
    ocr_pages,
)

router = APIRouter(prefix="/api", tags=["ocr"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class OcrQueue:
    """OCR 请求在事件循环里排队，轮到了才借一个线程去识别。

    等待中的请求只是暂停的协程，不占线程；共用线程池（默认 40 个）不会被排队的请求占满。
    识别一次就占满全部 CPU 核心，并行没有好处，所以同时只识别一个文件。
    """

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests  # 正在识别的加上排队的
        self.requests = 0
        self.limiter = anyio.CapacityLimiter(1)

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        """名额已满时直接返回 503，不让请求无限等下去。

        检查和加一之间没有 await，单线程的事件循环不会在中间切到别的请求，计数不用加锁。
        """
        if self.requests >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="识别繁忙，请稍后再试",
                headers={"Retry-After": "10"},
            )
        self.requests += 1
        try:
            yield
        finally:
            self.requests -= 1

    async def run(self, filename: str, data: bytes) -> list[str]:
        return await anyio.to_thread.run_sync(
            ocr_pages, filename, data, limiter=self.limiter
        )


# 1 个正在识别，最多再排 3 个；单张截图约 1 秒，排在最后的请求等几秒。
ocr_queue = OcrQueue(max_requests=4)


class OcrOut(BaseModel):
    """text 是按上传顺序拼接的全文；pages 是每张图片或每页 PDF 各自的识别结果。"""

    text: str
    pages: list[str]


@router.post("/ocr")
async def recognize(
    user: CurrentUser,
    files: Annotated[list[UploadFile], File(description="图片或一个扫描版 PDF")],
) -> OcrOut:
    """按上传顺序逐个识别。多张截图的自动排序（order_ocr）是 P1，暂未实现。

    user 不参与识别，声明它是为了要求登录：OCR 占 CPU，不能匿名调用。
    """
    if len(files) > MAX_OCR_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"一次最多识别 {MAX_OCR_IMAGES} 个文件",
        )

    pages: list[str] = []
    async with ocr_queue.admit():
        for upload in files:
            data = await read_limited(upload)
            try:
                pages += await ocr_queue.run(upload.filename or "", data)
            except UnsupportedFormatError as error:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"{upload.filename}：{error}",
                ) from None
            except ParseError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{upload.filename}：{error}",
                ) from None
    return OcrOut(text="\n\n".join(page for page in pages if page), pages=pages)


async def read_limited(upload: UploadFile) -> bytes:
    """最多读 MAX_UPLOAD_BYTES + 1 字节，超出就拒绝，不把整个大文件读进内存。"""
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{upload.filename}：单个文件不能超过 10 MB",
        )
    return data
