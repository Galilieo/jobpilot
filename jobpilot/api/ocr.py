"""截图与扫描件识别接口：识别结果只返回给前端核对，图片和文字都不保存。

简历和 JD 共用这个接口。用户在文本框里核对、修改后，再把文字提交给各自的上传接口。
"""

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
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
    for upload in files:
        data = await _read_limited(upload)
        try:
            # 识别要几秒且占满 CPU，放到线程池里，事件循环继续处理其他请求。
            pages += await run_in_threadpool(ocr_pages, upload.filename or "", data)
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


async def _read_limited(upload: UploadFile) -> bytes:
    """最多读 MAX_UPLOAD_BYTES + 1 字节，超出就拒绝，不把整个大文件读进内存。"""
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{upload.filename}：单个文件不能超过 10 MB",
        )
    return data
