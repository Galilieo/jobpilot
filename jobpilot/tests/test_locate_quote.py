"""原文定位：模型给出片段文字，程序在原文里找它的位置。

比较时忽略空白（换行、空格、制表符、全角空格），其他字符一个都不能差。
返回的位置满足：截出来的原文去掉空白后等于片段去掉空白后的内容，且首尾不是空白。
"""

import pytest

from jobpilot.services.facts import locate_quote

RESUME = (
    "张三  13800000000\n"
    "【实习经历】\n"
    "甲公司 后端开发实习生 2024.07-2024.09\n"
    "负责后端接口开发\n"
    "使用 FastAPI 编写 12 个接口\n"
    "乙公司 后端开发实习生 2025.01-2025.03\n"
    "负责后端接口开发\n"
    "优化慢查询，响应时间\n降低 30%\n"
)


def compact(text: str) -> str:
    return "".join(char for char in text if not char.isspace())


def test_exact_quote_is_found() -> None:
    [(start, end)] = locate_quote(RESUME, "使用 FastAPI 编写 12 个接口")

    assert RESUME[start:end] == "使用 FastAPI 编写 12 个接口"


def test_line_break_inside_the_original_is_ignored() -> None:
    """模型抄写时漏了原文里的换行，仍算同一段；截出的原文保留换行。"""
    [(start, end)] = locate_quote(RESUME, "优化慢查询，响应时间降低 30%")

    assert RESUME[start:end] == "优化慢查询，响应时间\n降低 30%"


def test_extra_whitespace_in_the_quote_is_ignored() -> None:
    [(start, end)] = locate_quote(RESUME, "  使用FastAPI编写12个接口\n")

    assert RESUME[start:end] == "使用 FastAPI 编写 12 个接口"


def test_full_width_space_counts_as_whitespace() -> None:
    [(start, end)] = locate_quote("技能：Python　FastAPI", "Python FastAPI")

    assert (start, end) == (3, 17)


def test_every_occurrence_is_returned_in_order() -> None:
    spans = locate_quote(RESUME, "负责后端接口开发")

    assert len(spans) == 2
    assert spans[0][0] < spans[1][0]
    assert all(RESUME[start:end] == "负责后端接口开发" for start, end in spans)


def test_search_can_be_limited_to_one_section() -> None:
    """只在乙公司这一段里找，得到第二处，而不是甲公司的第一处。"""
    section_start = RESUME.index("乙公司")

    [(start, end)] = locate_quote(RESUME, "负责后端接口开发", start=section_start)

    assert start > section_start
    assert (start, end) == locate_quote(RESUME, "负责后端接口开发")[1]


def test_match_crossing_the_section_end_is_not_found() -> None:
    end = RESUME.index("2024.09") + 4  # 范围截到「2024.09」的「2024」为止

    assert locate_quote(RESUME, "2024.07-2024.09", end=end) == []


@pytest.mark.parametrize(
    "quote",
    ["响应时间降低 20%", "降低 30％", "负责後端接口开发", "完全不存在的句子"],
    ids=["changed-number", "full-width-percent", "traditional-char", "absent"],
)
def test_any_visible_difference_fails(quote: str) -> None:
    """只忽略空白，不做语义判断：数字、全角半角、繁简不同都算对不上。"""
    assert locate_quote(RESUME, quote) == []


@pytest.mark.parametrize("quote", ["", "   ", "\n\t"])
def test_empty_quote_matches_nothing(quote: str) -> None:
    """空片段在任何位置都「出现」，必须明确返回找不到，不能被当成校验通过。"""
    assert locate_quote(RESUME, quote) == []


def test_span_never_starts_or_ends_with_whitespace() -> None:
    for start, end in locate_quote(RESUME, "\n负责后端接口开发\n"):
        assert not RESUME[start].isspace()
        assert not RESUME[end - 1].isspace()
        assert compact(RESUME[start:end]) == "负责后端接口开发"
