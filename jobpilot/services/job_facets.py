"""岗位库筛选维度的纯函数规则：方向归类、城市归一与关键词搜索匹配。

所有规则放在 jobpilot/data/job_directions.yaml 与 cities.yaml，本模块只负责加载校验、
按请求现算方向/城市以及匹配搜索词；不写入数据库，也不调用模型。
"""

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from jobpilot.config import REPO_ROOT
from jobpilot.services.jobs import ParsedJob
from jobpilot.services.keywords import default_table, normalize_key, normalize_keywords

OTHER_LABEL = "其他"
MULTI_CITY_LABEL = "多地"

DEFAULT_DIRECTIONS_PATH = REPO_ROOT / "jobpilot" / "data" / "job_directions.yaml"
DEFAULT_CITIES_PATH = REPO_ROOT / "jobpilot" / "data" / "cities.yaml"

_CITY_SPLIT_RE = re.compile(r"[/、,;；|()]|或")
_CITY_SEGMENT_HEAD_RE = re.compile(r"[·・•\-—–市(]")


class JobFacetsError(ValueError):
    """方向或城市规则表格式不对，或引用了未知的技术关键词/城市。"""


@dataclass(frozen=True)
class DirectionRule:
    """单个岗位方向的标题匹配正则与关联标准技术关键词集合。"""

    name: str
    title_pattern: re.Pattern[str] | None
    keywords: frozenset[str]


@dataclass(frozen=True)
class CityTable:
    """规范化城市查找表与「多地」类别名集合。"""

    city_map: dict[str, str]
    multi_keys: frozenset[str]


def _word_boundary_pattern(word: str) -> str:
    """为 ASCII 单词加字母数字边界，避免 JavaScript 命中 Java 或 Domain 命中 AI。"""
    escaped = re.escape(word)
    prefix = r"(?<![a-z0-9])" if word[0].isascii() and word[0].isalnum() else ""
    if word.endswith("+"):
        suffix = r"(?![a-z0-9+])"
    elif word[-1].isascii() and word[-1].isalnum():
        suffix = r"(?![a-z0-9])"
    else:
        suffix = ""
    return f"{prefix}{escaped}{suffix}"


def _build_title_pattern(title_words: list[str]) -> re.Pattern[str] | None:
    normalized = [key for word in title_words if (key := normalize_key(str(word)))]
    if not normalized:
        return None
    # 按长度降序排列，优先匹配更具体的词组。
    ordered = sorted(dict.fromkeys(normalized), key=len, reverse=True)
    return re.compile("|".join(_word_boundary_pattern(word) for word in ordered))


def load_direction_rules(
    path: Path, keyword_table: dict[str, str] | None = None
) -> tuple[DirectionRule, ...]:
    """加载并校验方向规则表；keywords 必须使用技术同义词表中的标准名。"""
    if not path.exists():
        raise JobFacetsError(f"方向规则表不存在：{path}")
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(entries, list) or not entries:
        raise JobFacetsError(f"{path.name}：方向规则不能为空列表")

    table = default_table() if keyword_table is None else keyword_table
    canonical_names = set(table.values())

    rules: list[DirectionRule] = []
    seen_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise JobFacetsError(f"{path.name}：每项都要有字符串 name：{entry!r}")
        name = entry["name"].strip()
        if not name or name == OTHER_LABEL:
            raise JobFacetsError(f"{path.name}：方向名非法：{name!r}")
        if name in seen_names:
            raise JobFacetsError(f"{path.name}：方向名重复：{name}")
        seen_names.add(name)

        title_words = entry.get("title_words") or []
        keywords_raw = entry.get("keywords") or []
        if not isinstance(title_words, list) or not isinstance(keywords_raw, list):
            raise JobFacetsError(
                f"{path.name}：「{name}」的 title_words 和 keywords 必须是列表"
            )

        keywords: list[str] = []
        for kw in keywords_raw:
            kw_str = str(kw).strip()
            if kw_str not in canonical_names:
                raise JobFacetsError(
                    f"{path.name}：「{name}」引用了不在技术同义词表标准名中的关键词：{kw_str!r}"
                )
            keywords.append(kw_str)

        title_pattern = _build_title_pattern(title_words)
        if title_pattern is None and not keywords:
            raise JobFacetsError(
                f"{path.name}：「{name}」的 title_words 与 keywords 不能同时为空"
            )

        rules.append(
            DirectionRule(
                name=name,
                title_pattern=title_pattern,
                keywords=frozenset(keywords),
            )
        )
    return tuple(rules)


@cache
def default_direction_rules() -> tuple[DirectionRule, ...]:
    return load_direction_rules(DEFAULT_DIRECTIONS_PATH)


def job_directions(
    title: str | None,
    keywords: list[str] | tuple[str, ...],
    rules: tuple[DirectionRule, ...] | None = None,
    keyword_table: dict[str, str] | None = None,
) -> list[str]:
    """按标题优先、技术关键词兜底的规则归类岗位方向。"""
    active_rules = default_direction_rules() if rules is None else rules
    normalized_title = normalize_key(title or "")
    if normalized_title:
        title_hits = [
            rule.name
            for rule in active_rules
            if rule.title_pattern is not None
            and rule.title_pattern.search(normalized_title)
        ]
        if title_hits:
            return title_hits

    # 标题未命中时才看技术关键词，避免 AI 应用岗因含 Python/FastAPI 被同时归入后端。
    normalized_kws = set(normalize_keywords(list(keywords), keyword_table))
    if normalized_kws:
        best_name: str | None = None
        best_hits = 0
        for rule in active_rules:
            hits = len(rule.keywords & normalized_kws)
            if hits > best_hits:
                best_hits = hits
                best_name = rule.name
        if best_name is not None:
            return [best_name]

    return [OTHER_LABEL]


def load_city_table(path: Path) -> CityTable:
    """加载并校验城市归一名表。"""
    if not path.exists():
        raise JobFacetsError(f"城市表不存在：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise JobFacetsError(f"{path.name}：顶层必须是映射")

    cities_raw = data.get("cities") or []
    if not isinstance(cities_raw, list) or not cities_raw:
        raise JobFacetsError(f"{path.name}：cities 不能为空")

    city_map: dict[str, str] = {}
    canonical_cities: set[str] = set()
    for item in cities_raw:
        city = str(item).strip()
        if not city or city in {OTHER_LABEL, MULTI_CITY_LABEL}:
            raise JobFacetsError(f"{path.name}：城市名非法：{city!r}")
        key = normalize_key(city)
        if key in city_map:
            raise JobFacetsError(f"{path.name}：城市名重复：{city}")
        city_map[key] = city
        canonical_cities.add(city)

    multi_keys: set[str] = set()
    for alias in data.get("multi_aliases") or []:
        key = normalize_key(str(alias))
        if not key:
            continue
        if key in city_map:
            raise JobFacetsError(f"{path.name}：multi_aliases 与城市名冲突：{alias!r}")
        multi_keys.add(key)

    aliases_raw = data.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise JobFacetsError(f"{path.name}：aliases 必须是映射")
    for alias, target in aliases_raw.items():
        target_name = str(target).strip()
        if target_name not in canonical_cities:
            raise JobFacetsError(
                f"{path.name}：别名「{alias}」指向未知城市：{target_name!r}"
            )
        key = normalize_key(str(alias))
        if not key:
            continue
        if key in multi_keys or city_map.get(key, target_name) != target_name:
            raise JobFacetsError(f"{path.name}：城市别名冲突：{alias!r}")
        city_map[key] = target_name

    return CityTable(city_map=city_map, multi_keys=frozenset(multi_keys))


@cache
def default_city_table() -> CityTable:
    return load_city_table(DEFAULT_CITIES_PATH)


def _match_city_segment(segment: str, table: CityTable) -> str | None:
    cleaned = segment.strip().strip(")")
    if not cleaned:
        return None
    if cleaned in table.multi_keys:
        return MULTI_CITY_LABEL
    if cleaned in table.city_map:
        return table.city_map[cleaned]

    head = _CITY_SEGMENT_HEAD_RE.split(cleaned, maxsplit=1)[0].strip()
    if "省" in head:
        head = head.split("省", maxsplit=1)[1].strip()
    if not head and "(" in cleaned:
        return None

    candidates = [head]
    if " " in head:
        candidates.append(head.split(" ", maxsplit=1)[0])
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in table.multi_keys:
            return MULTI_CITY_LABEL
        if candidate in table.city_map:
            return table.city_map[candidate]
    return None


def job_cities(location: str | None, table: CityTable | None = None) -> list[str]:
    """把地点字符串拆分并归一为标准城市名列表；无法识别或为空返回「其他」。"""
    normalized = normalize_key(location or "")
    if not normalized:
        return [OTHER_LABEL]

    active_table = default_city_table() if table is None else table
    result: list[str] = []
    seen: set[str] = set()
    for segment in _CITY_SPLIT_RE.split(normalized):
        matched = _match_city_segment(segment, active_table)
        if matched and matched not in seen:
            seen.add(matched)
            result.append(matched)
    return result or [OTHER_LABEL]


def matches_job_query(query: str, *, parsed: ParsedJob, raw_text: str) -> bool:
    """按忽略大小写和全半角的子串匹配标题、公司、地点、关键词或 JD 原文。"""
    needle = normalize_key(query)
    if not needle:
        return True
    fields = parsed.basic_fields
    candidates = (
        fields.title,
        fields.company,
        fields.location,
        *parsed.keywords,
        raw_text,
    )
    return any(needle in normalize_key(text) for text in candidates if text)
