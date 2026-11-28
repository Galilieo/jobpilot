"""岗位方向归类、城市归一与关键词搜索纯函数测试。"""

from pathlib import Path

import pytest

from jobpilot.services.job_facets import (
    DEFAULT_CITIES_PATH,
    DEFAULT_DIRECTIONS_PATH,
    JobFacetsError,
    job_cities,
    job_directions,
    load_city_table,
    load_direction_rules,
    matches_job_query,
)
from jobpilot.services.jobs import BasicFields, ParsedJob


def test_project_facet_configs_are_valid() -> None:
    assert load_direction_rules(DEFAULT_DIRECTIONS_PATH)
    assert load_city_table(DEFAULT_CITIES_PATH)


def test_direction_rules_reject_unknown_or_alias_keywords(tmp_path: Path) -> None:
    bad_unknown = tmp_path / "unknown.yaml"
    bad_unknown.write_text(
        "- name: 后端\n  title_words: [后端]\n  keywords: [不存在框架]\n",
        encoding="utf-8",
    )
    with pytest.raises(JobFacetsError, match="标准名"):
        load_direction_rules(bad_unknown)

    # 写了别名「大模型」而不是标准名「LLM」也必须报错。
    bad_alias = tmp_path / "alias.yaml"
    bad_alias.write_text(
        "- name: AI 应用\n  title_words: [AI]\n  keywords: [大模型]\n",
        encoding="utf-8",
    )
    with pytest.raises(JobFacetsError, match="标准名"):
        load_direction_rules(bad_alias)


def test_job_directions_title_hits_and_priority_over_keywords() -> None:
    # 标题命中 AI 应用时，即使关键词里有后端技术也不再同时归进后端。
    assert job_directions(
        "AI 应用开发实习生", ["Python", "FastAPI", "MySQL", "Redis"]
    ) == ["AI 应用"]

    # 标题同时命中多个方向时都保留。
    assert job_directions("Agent 全栈开发实习生", ["React"]) == ["AI 应用", "全栈"]
    assert job_directions("大模型应用后端研发实习生（Go方向）", []) == [
        "AI 应用",
        "后端",
    ]


def test_job_directions_ascii_word_boundaries_avoid_false_matches() -> None:
    assert job_directions("JavaScript 前端工程师", []) == ["前端"]
    assert job_directions("Algorithm 实习生", []) == ["其他"]
    assert job_directions("Retail Data Engineer", ["SQL"]) == ["数据"]


def test_job_directions_fallback_to_keywords_and_other() -> None:
    # 标题未命中任何方向时，回退到技术关键词命中最多的一项。
    assert job_directions("研发实习生", ["FastAPI", "MySQL", "Redis", "React"]) == [
        "后端"
    ]
    assert job_directions("暑期实习生（未知方向）", ["PyTorch", "NLP"]) == ["算法"]
    assert job_directions(None, ["未知自研工具"]) == ["其他"]
    assert job_directions("", []) == ["其他"]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("上海·徐汇", ["上海"]),
        ("上海-浦东", ["上海"]),
        ("北京/上海", ["北京", "上海"]),
        ("北京、上海、深圳", ["北京", "上海", "深圳"]),
        ("上海，北京", ["上海", "北京"]),
        ("上海或杭州", ["上海", "杭州"]),
        ("上海市浦东新区", ["上海"]),
        ("浙江省杭州市余杭区", ["杭州"]),
        ("上海（张江）/上海·徐汇", ["上海"]),
        ("多地", ["多地"]),
        ("全国", ["多地"]),
        ("远程", ["多地"]),
        ("Remote", ["多地"]),
        ("多地（北京/上海）", ["多地", "北京", "上海"]),
        ("Hong Kong", ["香港"]),
        ("", ["其他"]),
        ("   ", ["其他"]),
        (None, ["其他"]),
        ("火星基地", ["其他"]),
    ],
)
def test_job_cities_normalization(location: str | None, expected: list[str]) -> None:
    assert job_cities(location) == expected


def test_city_table_rejects_unknown_alias_target(tmp_path: Path) -> None:
    bad = tmp_path / "cities.yaml"
    bad.write_text(
        "cities: [北京]\naliases:\n  Atlantis: 亚特兰蒂斯\n",
        encoding="utf-8",
    )
    with pytest.raises(JobFacetsError, match="未知城市"):
        load_city_table(bad)


def test_matches_job_query_checks_all_fields_and_normalizes_text() -> None:
    parsed = ParsedJob(
        basic_fields=BasicFields(
            title="AI 应用实习生",
            company="青梧数智",
            location="上海·徐汇",
        ),
        requirements=[],
        duties=[],
        keywords=["FastAPI", "RAG"],
    )
    raw = "职责：搭建知识库问答服务，熟悉 Python。"

    assert matches_job_query("ai 应用", parsed=parsed, raw_text=raw)
    assert matches_job_query("青梧", parsed=parsed, raw_text=raw)
    assert matches_job_query("徐汇", parsed=parsed, raw_text=raw)
    assert matches_job_query("ＦＡＳＴＡＰＩ", parsed=parsed, raw_text=raw)
    assert matches_job_query("知识库问答", parsed=parsed, raw_text=raw)
    assert not matches_job_query("Spring Boot", parsed=parsed, raw_text=raw)
