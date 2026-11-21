"""本机 OCR：真实跑 RapidOCR 识别一张合成截图，只断言关键字，不比对全文。

fixtures/ocr_resume.png 是用字体画出来的合成简历片段，不含真实个人信息。
切图和按行合并是纯函数，用构造的图片和坐标单独测，不依赖识别结果。
"""

import asyncio
import threading
from io import BytesIO
from pathlib import Path

import anyio.to_thread
import httpx
import pytest
from PIL import Image, ImageDraw

from jobpilot.api import ocr as ocr_api
from jobpilot.api.ocr import MAX_UPLOAD_BYTES
from jobpilot.services import parsing
from jobpilot.services.parsing import (
    MAX_OCR_IMAGES,
    NeedsOcrError,
    UnreadableFileError,
    UnsupportedFormatError,
    extract_text,
    group_lines,
    load_image,
    ocr_pages,
    split_tall_image,
)

SCREENSHOT = (Path(__file__).parent / "fixtures" / "ocr_resume.png").read_bytes()
KEYWORDS = ("课程排班面板", "FastAPI", "30%")


def as_scanned_pdf(png: bytes) -> bytes:
    """把截图存成只有图片、没有文字层的 PDF，相当于一份扫描件。"""
    buffer = BytesIO()
    Image.open(BytesIO(png)).convert("RGB").save(buffer, "PDF")
    return buffer.getvalue()


def image_bytes(image: Image.Image, image_format: str) -> bytes:
    buffer = BytesIO()
    image.save(buffer, image_format)
    return buffer.getvalue()


# ---------- 真实识别 ----------


def test_screenshot_keywords_are_recognised() -> None:
    [text] = ocr_pages("shot.png", SCREENSHOT)

    for keyword in KEYWORDS:
        assert keyword in text


def test_scanned_pdf_goes_to_ocr_and_is_recognised() -> None:
    """同一份扫描件：直接提取文字会被判为需要 OCR，OCR 后能读出内容。"""
    pdf = as_scanned_pdf(SCREENSHOT)

    with pytest.raises(NeedsOcrError):
        extract_text("scan.pdf", pdf)
    [text] = ocr_pages("scan.pdf", pdf)
    for keyword in KEYWORDS:
        assert keyword in text


# ---------- 图片检查：按内容判断格式，限制尺寸 ----------


def test_image_format_is_checked_by_content_not_extension() -> None:
    gif = image_bytes(Image.new("RGB", (10, 10)), "GIF")

    with pytest.raises(UnsupportedFormatError, match="GIF"):
        ocr_pages("renamed.png", gif)
    with pytest.raises(UnsupportedFormatError):
        ocr_pages("shot.png", b"not an image")
    with pytest.raises(UnsupportedFormatError, match="OCR 只支持"):
        ocr_pages("resume.docx", SCREENSHOT)


def test_oversized_image_is_rejected_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parsing, "MAX_IMAGE_PIXELS", 99)

    with pytest.raises(UnreadableFileError, match="尺寸"):
        load_image(image_bytes(Image.new("RGB", (10, 10)), "PNG"))


def test_exif_orientation_is_applied() -> None:
    """手机竖拍的照片像素是横的，EXIF 标记要旋转 90°；读入后应变成竖图。"""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation：顺时针旋转 90° 后显示
    data = BytesIO()
    Image.new("RGB", (40, 20)).save(data, "JPEG", exif=exif)

    assert load_image(data.getvalue()).size == (20, 40)


# ---------- 高图切分 ----------


def striped_image(width: int, height: int) -> Image.Image:
    """白底，每 50 像素一个周期，后 20 像素是黑块，模拟一行行文字。

    黑块放在周期末尾，第 2999 行正好是黑的：按上限 3000 硬切会切在文字上。
    """
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for top in range(0, height, 50):
        draw.rectangle((10, top + 30, width - 10, top + 49), fill="black")
    return image


def test_short_image_is_not_split() -> None:
    image = striped_image(1000, 2900)

    assert split_tall_image(image) == [image]


def test_tall_image_is_cut_on_blank_rows() -> None:
    """宽 1000 时每段最高 3000；切点落在两行文字之间，没有一块黑色被切开。"""
    image = striped_image(1000, 10000)
    assert image.getpixel((500, 2999)) == (0, 0, 0)  # 硬切会切开文字

    segments = split_tall_image(image)

    assert len(segments) > 1
    assert sum(segment.height for segment in segments) == image.height
    assert all(segment.height <= 3000 for segment in segments)
    for segment in segments[:-1]:
        bottom_row = segment.crop(
            (0, segment.height - 1, segment.width, segment.height)
        )
        assert bottom_row.convert("L").getextrema() == (255, 255)  # 整行都是白的


def test_tall_image_without_blank_rows_is_cut_anyway() -> None:
    """竖条纹让每一行都有黑有白，找不到空白行，只能按上限硬切。"""
    image = Image.new("RGB", (1000, 7000), "white")
    draw = ImageDraw.Draw(image)
    for left in range(0, 1000, 20):
        draw.rectangle((left, 0, left + 9, 6999), fill="black")

    assert [s.height for s in split_tall_image(image)] == [3000, 3000, 1000]


# ---------- 按行合并文字框 ----------


def box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def test_boxes_on_the_same_row_are_joined_left_to_right() -> None:
    boxes = [
        box(400, 12, 520, 38),  # 右边的日期略低一点，仍算同一行
        box(20, 10, 200, 40),
        box(20, 60, 300, 90),
    ]

    lines = group_lines(boxes, ["2024.07", "课程排班面板", "负责筛选功能"])

    assert lines == ["课程排班面板 2024.07", "负责筛选功能"]


def test_slightly_overlapping_rows_stay_separate() -> None:
    boxes = [box(20, 10, 200, 40), box(20, 35, 200, 65)]  # 只重叠 5 像素

    assert group_lines(boxes, ["第一行", "第二行"]) == ["第一行", "第二行"]


# ---------- 接口 ----------


@pytest.fixture
async def signed_in(async_client: httpx.AsyncClient) -> httpx.AsyncClient:
    credentials = {"email": "ocr@test.invalid", "password": "ocr-test-password"}
    assert (await async_client.post("/api/auth/register", json=credentials)).is_success
    assert (await async_client.post("/api/auth/login", json=credentials)).is_success
    return async_client


async def test_ocr_endpoint_returns_pages_in_upload_order(
    signed_in: httpx.AsyncClient,
) -> None:
    blank = image_bytes(Image.new("RGB", (200, 100), "white"), "PNG")
    files = [
        ("files", ("1.png", SCREENSHOT, "image/png")),
        ("files", ("2.png", blank, "image/png")),
    ]

    response = await signed_in.post("/api/ocr", files=files)

    assert response.status_code == 200
    body = response.json()
    assert len(body["pages"]) == 2
    assert "课程排班面板" in body["pages"][0]
    assert body["pages"][1] == ""
    assert body["text"] == body["pages"][0]  # 空白页不在全文里留空段


@pytest.mark.parametrize(
    ("files", "status"),
    [
        ([("files", ("a.doc", b"x", "application/msword"))], 415),
        ([("files", ("a.png", b"x" * (MAX_UPLOAD_BYTES + 1), "image/png"))], 413),
        ([("files", (f"{n}.png", b"x", "image/png")) for n in range(11)], 413),
        ([("files", ("scan.pdf", b"%PDF-1.4 broken", "application/pdf"))], 422),
    ],
    ids=["unsupported-type", "file-too-large", "too-many-files", "broken-pdf"],
)
async def test_ocr_endpoint_rejects_bad_uploads(
    signed_in: httpx.AsyncClient, files: list, status: int
) -> None:
    assert MAX_OCR_IMAGES == 10  # too-many-files 用例按 10 张上限构造
    response = await signed_in.post("/api/ocr", files=files)

    assert response.status_code == status, response.text


# ---------- 排队：在事件循环里等，不占线程；名额满了返回 503 ----------


class StalledOcr:
    """替换真实识别：开始后停住，直到测试放行；记录同时在识别的数量。"""

    def __init__(self) -> None:
        self.running = 0
        self.release = threading.Event()

    def __call__(self, filename: str, data: bytes) -> list[str]:
        self.running += 1
        self.release.wait(timeout=5)
        self.running -= 1
        return [filename]


@pytest.fixture
def stalled_ocr(monkeypatch: pytest.MonkeyPatch) -> StalledOcr:
    fake = StalledOcr()
    monkeypatch.setattr(ocr_api, "ocr_pages", fake)
    return fake


def upload(name: str) -> list:
    return [("files", (name, b"x", "image/png"))]


async def wait_until_admitted(count: int) -> None:
    for _ in range(500):
        if ocr_api.ocr_queue.requests == count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等了 5 秒仍没有 {count} 个请求进入队列")


async def test_waiting_requests_hold_no_thread(
    signed_in: httpx.AsyncClient, stalled_ocr: StalledOcr
) -> None:
    """3 个请求同时到达：只有 1 个在线程里识别。

    另 2 个在事件循环里等，不占共用线程池。
    """
    tasks = [
        asyncio.create_task(signed_in.post("/api/ocr", files=upload(f"{n}.png")))
        for n in range(3)
    ]
    await wait_until_admitted(3)

    assert stalled_ocr.running == 1
    assert ocr_api.ocr_queue.limiter.borrowed_tokens == 1
    assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0

    stalled_ocr.release.set()
    responses = await asyncio.gather(*tasks)
    assert [r.status_code for r in responses] == [200, 200, 200]


async def test_full_queue_returns_503_and_frees_the_slot(
    signed_in: httpx.AsyncClient,
    stalled_ocr: StalledOcr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr_api.ocr_queue, "max_requests", 1)
    first = asyncio.create_task(signed_in.post("/api/ocr", files=upload("1.png")))
    await wait_until_admitted(1)

    rejected = await signed_in.post("/api/ocr", files=upload("2.png"))
    stalled_ocr.release.set()

    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "10"
    assert (await first).status_code == 200
    assert ocr_api.ocr_queue.requests == 0  # 请求结束后名额归还
