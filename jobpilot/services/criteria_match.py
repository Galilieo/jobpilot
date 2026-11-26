"""求职条件与教育的程序比对（纯函数，不访问数据库、不调用模型）。

把 JD 要求先解析成结构化要求，再与用户已确认的求职条件和教育经历独立比对，
不依赖模型是否引用条件；复合、矛盾、读不出数字或基本字段与逐条要求冲突时，
标为待核对交回模型并在报告中说明。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import (
    CriterionField,
    DegreeLevel,
    EducationStatus,
    FactSetCriterion,
    FactSetEducation,
)
from jobpilot.services.jobs import BasicFields, Requirement, RequirementType

DEFAULT_CRITERIA_RULES = REPO_ROOT / "jobpilot" / "data" / "criteria_rules.yaml"

_CLAUSE_SPLIT = re.compile(r"[，,;；。\n]+|并且|且|以及")
_DAYS_RANGE_RE = re.compile(
    r"(?P<low>\d+|[一二两三四五六七])\s*(?P<sep>[～~—\-至到])\s*"
    r"(?P<high>\d+|[一二两三四五六七])\s*(?:天|日)"
)
_DAYS_SINGLE_RE = re.compile(
    r"(?:每周(?:可?到岗|出勤)?|到岗|出勤|至少|不少于|不低于)?\s*"
    r"(?P<val>\d+|[一二两三四五六七])\s*(?:天|日)(?:\s*[/／]\s*周|以上|及以上|起|\+)?"
)
_MONTHS_RANGE_RE = re.compile(
    r"(?P<low>\d+|[一二两三四五六七八九十]+)\s*(?P<sep>[～~—\-至到])\s*"
    r"(?P<high>\d+|[一二两三四五六七八九十]+)\s*个月"
)
_MONTHS_SINGLE_RE = re.compile(
    r"(?:(?P<val>\d+|十一|十二|[一二两三四五六七八九十])\s*个月|(?P<alias>半年|一年))"
)


class CompareOp(StrEnum):
    GTE = "gte"
    RANGE = "range"


@dataclass(frozen=True)
class CriteriaRules:
    degree_ranks: dict[DegreeLevel, int]
    degree_labels: dict[DegreeLevel, str]
    degree_words: dict[str, DegreeLevel]
    status_words: dict[str, EducationStatus]
    preferred_markers: tuple[str, ...]
    gte_markers: tuple[str, ...]
    onsite_cues: tuple[str, ...]
    months_cues: tuple[str, ...]
    education_cues: tuple[str, ...]
    chinese_numbers: dict[str, int]
    filler_clauses: frozenset[str]
    education_modifiers: tuple[str, ...]
    schedule_modifiers: tuple[str, ...]

    @property
    def degree_pattern(self) -> re.Pattern[str]:
        return _term_pattern(tuple(self.degree_words))


def load_criteria_rules(path: Path) -> CriteriaRules:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    degree_ranks: dict[DegreeLevel, int] = {}
    degree_labels: dict[DegreeLevel, str] = {}
    degree_words: dict[str, DegreeLevel] = {}
    for entry in data.get("degree_levels") or ():
        code = DegreeLevel(entry["code"])
        degree_ranks[code] = int(entry["rank"])
        degree_labels[code] = str(entry["label"])
        for word in entry["words"]:
            degree_words[str(word)] = code

    status_words: dict[str, EducationStatus] = {}
    for status_key, words in (data.get("education_status_words") or {}).items():
        status = EducationStatus(status_key)
        for word in words:
            status_words[str(word)] = status

    return CriteriaRules(
        degree_ranks=degree_ranks,
        degree_labels=degree_labels,
        degree_words=degree_words,
        status_words=status_words,
        preferred_markers=tuple(data.get("preferred_markers") or ()),
        gte_markers=tuple(data.get("gte_markers") or ()),
        onsite_cues=tuple(data.get("onsite_cues") or ()),
        months_cues=tuple(data.get("months_cues") or ()),
        education_cues=tuple(data.get("education_cues") or ()),
        chinese_numbers={
            str(k): int(v) for k, v in (data.get("chinese_numbers") or {}).items()
        },
        filler_clauses=frozenset(data.get("filler_clauses") or ()),
        education_modifiers=tuple(data.get("education_modifiers") or ()),
        schedule_modifiers=tuple(data.get("schedule_modifiers") or ()),
    )


@cache
def default_criteria_rules() -> CriteriaRules:
    return load_criteria_rules(DEFAULT_CRITERIA_RULES)


# ---------- 候选输入结构 ----------


@dataclass(frozen=True)
class CriterionCandidate:
    """参与匹配的一条已发布求职条件。"""

    ref_id: str
    field: CriterionField
    int_value: int | None
    text_value: str | None
    text: str
    label: str = "求职条件"

    @classmethod
    def from_row(cls, row: FactSetCriterion) -> "CriterionCandidate":
        field = CriterionField(row.field)
        if field is CriterionField.ONSITE_DAYS_PER_WEEK:
            display = f"每周可到岗 {row.int_value} 天"
        elif field is CriterionField.INTERNSHIP_MONTHS:
            display = f"可实习 {row.int_value} 个月"
        else:
            display = f"最早 {row.text_value} 到岗"
        return cls(
            ref_id=f"criteria.{field.value}",
            field=field,
            int_value=row.int_value,
            text_value=row.text_value,
            text=display,
        )


@dataclass(frozen=True)
class EducationCandidate:
    """参与匹配的一段已发布教育经历。"""

    ref_id: str
    education_id: UUID
    school: str
    major: str | None
    degree: DegreeLevel
    status: EducationStatus
    start_text: str | None
    end_text: str | None
    text: str
    label: str = "教育"

    @classmethod
    def from_row(cls, row: FactSetEducation) -> "EducationCandidate":
        rules = default_criteria_rules()
        degree = DegreeLevel(row.degree)
        status = EducationStatus(row.status)
        status_label = "在读" if status is EducationStatus.ENROLLED else "已取得"
        parts = [row.school]
        if row.major:
            parts.append(row.major)
        parts.append(f"{rules.degree_labels[degree]}（{status_label}）")
        period = "–".join(p for p in (row.start_text, row.end_text) if p)
        display = " · ".join(parts) + (f" {period}" if period else "")
        return cls(
            ref_id=f"education.{row.education_id}",
            education_id=row.education_id,
            school=row.school,
            major=row.major,
            degree=degree,
            status=status,
            start_text=row.start_text,
            end_text=row.end_text,
            text=display,
        )


# ---------- JD 要求解析 ----------


@dataclass(frozen=True)
class ParsedClause:
    dimension: Literal["onsite_days", "internship_months", "education"]
    op: CompareOp
    min_value: int
    max_value: int | None
    required_status: EducationStatus | None
    hardness: RequirementType
    source_text: str


@dataclass(frozen=True)
class ParsedRequirement:
    clauses: tuple[ParsedClause, ...]
    needs_review: bool = False
    review_note: str | None = None


@dataclass(frozen=True)
class ProgramDecision:
    verdict: Literal["covered", "partial", "missing"]
    criteria_refs: tuple[str, ...]
    reason: str
    suggestion: str
    hardness: RequirementType


def _to_int(token: str, rules: CriteriaRules) -> int | None:
    if token.isdigit():
        return int(token)
    return rules.chinese_numbers.get(token)


def _is_preferred_clause(
    clause: str, base_type: RequirementType, rules: CriteriaRules
) -> RequirementType:
    if base_type is RequirementType.PREFERRED:
        return RequirementType.PREFERRED
    if any(marker in clause for marker in rules.preferred_markers):
        return RequirementType.PREFERRED
    return RequirementType.HARD


@cache
def _term_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(map(re.escape, sorted(terms, key=len, reverse=True))))


def _known_text(text: str, terms: tuple[str, ...]) -> bool:
    """每段剩余文本都必须能由已知词解释。"""
    return not _term_pattern(terms).sub("", text).strip()


def _known_schedule_text(
    clause: str, match: re.Match[str], rules: CriteriaRules, cues: tuple[str, ...]
) -> bool:
    residue = clause[: match.start()] + " " + clause[match.end() :]
    return _known_text(
        residue,
        cues
        + rules.schedule_modifiers
        + rules.gte_markers
        + rules.preferred_markers
        + tuple(sorted(rules.filler_clauses)),
    )


def _parse_days_clause(
    clause: str, base_type: RequirementType, rules: CriteriaRules
) -> ParsedClause | None:
    hardness = _is_preferred_clause(clause, base_type, rules)
    m_range = _DAYS_RANGE_RE.search(clause)
    if m_range:
        if not _known_schedule_text(clause, m_range, rules, rules.onsite_cues):
            return None
        low = _to_int(m_range.group("low"), rules)
        high = _to_int(m_range.group("high"), rules)
        if low is not None and high is not None and 1 <= low <= high <= 7:
            return ParsedClause(
                "onsite_days", CompareOp.RANGE, low, high, None, hardness, clause
            )
    if not any(cue in clause for cue in (*rules.onsite_cues, "天", "日")):
        return None
    matches = list(_DAYS_SINGLE_RE.finditer(clause))
    if len(matches) != 1 or not _known_schedule_text(
        clause, matches[0], rules, rules.onsite_cues
    ):
        return None
    val = _to_int(matches[0].group("val"), rules)
    if val is None or not (1 <= val <= 7):
        return None
    return ParsedClause("onsite_days", CompareOp.GTE, val, None, None, hardness, clause)


def _parse_months_clause(
    clause: str, base_type: RequirementType, rules: CriteriaRules
) -> ParsedClause | None:
    hardness = _is_preferred_clause(clause, base_type, rules)
    m_range = _MONTHS_RANGE_RE.search(clause)
    if m_range:
        if not _known_schedule_text(clause, m_range, rules, rules.months_cues):
            return None
        low = _to_int(m_range.group("low"), rules)
        high = _to_int(m_range.group("high"), rules)
        if low is not None and high is not None and 1 <= low <= high <= 24:
            return ParsedClause(
                "internship_months", CompareOp.RANGE, low, high, None, hardness, clause
            )
    if not any(cue in clause for cue in (*rules.months_cues, "月", "半年", "一年")):
        return None
    # 避免把「工作 3 年以上」误当成实习月数。
    if "年" in clause and "一年" not in clause and "月" not in clause:
        return None
    matches = list(_MONTHS_SINGLE_RE.finditer(clause))
    if len(matches) != 1 or not _known_schedule_text(
        clause, matches[0], rules, rules.months_cues
    ):
        return None
    match = matches[0]
    val = _to_int(match.group("alias") or match.group("val"), rules)
    if val is None or not (1 <= val <= 24):
        return None
    return ParsedClause(
        "internship_months", CompareOp.GTE, val, None, None, hardness, clause
    )


def _parse_education_clause(
    clause: str, base_type: RequirementType, rules: CriteriaRules
) -> ParsedClause | None:
    matches = list(rules.degree_pattern.finditer(clause))
    if not matches:
        return None
    statuses = {status for word, status in rules.status_words.items() if word in clause}
    if len(statuses) > 1 or not _is_simple_degree_alternative(
        clause, matches, statuses
    ):
        return None
    leftover = rules.degree_pattern.sub("", clause)
    recognized = (
        tuple(rules.status_words)
        + rules.education_modifiers
        + rules.gte_markers
        + rules.preferred_markers
        + tuple(sorted(rules.filler_clauses))
        + ("或", "／", "/")
    )
    if not _known_text(leftover, recognized):
        return None
    degrees = {rules.degree_words[match.group()] for match in matches}
    # 学历明确为「本科或硕士」时，沿用较低等级作为下限。
    min_degree = min(degrees, key=lambda d: rules.degree_ranks[d])
    status = next(iter(statuses), None)
    hardness = _is_preferred_clause(clause, base_type, rules)
    return ParsedClause(
        "education",
        CompareOp.GTE,
        rules.degree_ranks[min_degree],
        None,
        status,
        hardness,
        clause,
    )


def _is_simple_degree_alternative(
    clause: str, matches: list[re.Match[str]], statuses: set[EducationStatus]
) -> bool:
    if len(matches) == 1:
        return True
    # 多个等级加上就读状态时，无法确定状态约束适用于哪个等级。
    return not statuses and all(
        clause[left.end() : right.start()].strip() in {"或", "／", "/"}
        for left, right in zip(matches, matches[1:], strict=False)
    )


def _has_criteria_cue(text: str, rules: CriteriaRules) -> bool:
    return any(
        cue in text
        for cue in (*rules.onsite_cues, *rules.months_cues, *rules.education_cues)
    )


def _is_filler(clause: str, rules: CriteriaRules) -> bool:
    stripped = clause.strip()
    if not stripped or stripped in rules.filler_clauses:
        return True
    return all(
        token in rules.filler_clauses or token in rules.preferred_markers
        for token in re.split(r"[、/／和及与\s]+", stripped)
        if token
    )


def _clause_conflicts(clauses: Sequence[ParsedClause]) -> bool:
    """同一条要求内出现同维度且互相矛盾的硬性约束时返回 True。"""
    for dim in ("onsite_days", "internship_months"):
        hard = [
            c
            for c in clauses
            if c.dimension == dim and c.hardness is RequirementType.HARD
        ]
        if len(hard) > 1 and len({(c.op, c.min_value, c.max_value) for c in hard}) > 1:
            return True
    return False


def _basic_field_conflict(
    parsed: Sequence[ParsedClause],
    basic_fields: BasicFields | None,
    rules: CriteriaRules,
) -> str | None:
    if basic_fields is None:
        return None
    checks: list[tuple[str, str | None, str]] = [
        ("onsite_days", basic_fields.onsite_days_per_week, "到岗天数"),
        ("internship_months", basic_fields.internship_duration, "实习时长"),
        ("education", basic_fields.education, "学历"),
    ]
    for dim, raw_basic, label in checks:
        if not raw_basic:
            continue
        basic_clauses = _parse_text_clauses(raw_basic, RequirementType.HARD, rules)
        basic_dim = [c for c in basic_clauses if c.dimension == dim]
        req_dim = [
            c
            for c in parsed
            if c.dimension == dim and c.hardness is RequirementType.HARD
        ]
        if not basic_dim or not req_dim:
            continue
        b = basic_dim[0]
        r = req_dim[0]
        if (b.min_value, b.max_value, b.required_status) != (
            r.min_value,
            r.max_value,
            r.required_status,
        ):
            return (
                f"岗位基本字段的{label}（{raw_basic}）"
                f"与逐条要求（{r.source_text}）不一致"
            )
    return None


_SENTENCE_END = "。！？"


def _join_sentences(parts: Sequence[str]) -> str:
    """逐句接起来：每句以句号结尾，不用「；」再连（各子句的说明本身就是完整的句子）。"""
    return "".join(
        part if part.endswith(tuple(_SENTENCE_END)) else f"{part}。"
        for part in (p.strip() for p in parts)
        if part
    )


def _parse_text_clauses(
    text: str, base_type: RequirementType, rules: CriteriaRules
) -> list[ParsedClause]:
    clauses: list[ParsedClause] = []
    for raw_part in _CLAUSE_SPLIT.split(text):
        part = raw_part.strip()
        if not part:
            continue
        parsed_any = False
        for parser in (
            _parse_days_clause,
            _parse_months_clause,
            _parse_education_clause,
        ):
            clause = parser(part, base_type, rules)
            if clause is not None:
                clauses.append(clause)
                parsed_any = True
        if not parsed_any and _has_criteria_cue(part, rules):
            continue
    return clauses


def parse_requirement(
    req: Requirement, basic_fields: BasicFields | None = None
) -> ParsedRequirement:
    """把一条 JD 要求解析成可由程序比对的子句；不确定时标 needs_review。"""
    rules = default_criteria_rules()
    if not _has_criteria_cue(req.text, rules):
        return ParsedRequirement(clauses=())

    parts = [p.strip() for p in _CLAUSE_SPLIT.split(req.text) if p.strip()]
    clauses: list[ParsedClause] = []
    unparsed_parts: list[str] = []
    for part in parts:
        found: list[ParsedClause] = []
        for parser in (
            _parse_days_clause,
            _parse_months_clause,
            _parse_education_clause,
        ):
            c = parser(part, req.type, rules)
            if c is not None:
                found.append(c)
        if found:
            clauses.extend(found)
        elif not _is_filler(part, rules):
            unparsed_parts.append(part)

    if not clauses:
        return ParsedRequirement(
            clauses=(),
            needs_review=True,
            review_note="要求涉及求职条件或学历，但未读出明确数字或等级，需结合上下文核对",
        )
    if unparsed_parts:
        return ParsedRequirement(
            clauses=(),
            needs_review=True,
            review_note=f"复合要求包含其他内容（{'、'.join(unparsed_parts)}），不由程序单独判定",
        )
    if _clause_conflicts(clauses):
        return ParsedRequirement(
            clauses=(),
            needs_review=True,
            review_note="同一条要求里包含多个不一致的数字条件，需人工核对",
        )
    conflict = _basic_field_conflict(clauses, basic_fields, rules)
    if conflict is not None:
        return ParsedRequirement(clauses=(), needs_review=True, review_note=conflict)

    return ParsedRequirement(clauses=tuple(clauses))


# ---------- 独立比对 ----------


def _match_int_clause(
    clause: ParsedClause,
    value: int | None,
    ref_id: str | None,
    unit: str,
    missing_hint: str,
) -> tuple[Literal["covered", "partial", "missing"], str | None, str, str]:
    if value is None or ref_id is None:
        return (
            "missing",
            None,
            f"「求职条件」中尚未填写{missing_hint}。",
            f"请先在「求职条件」里补上{missing_hint}，不要直接写进简历。",
        )
    if value < clause.min_value:
        return (
            "partial",
            ref_id,
            f"已确认{missing_hint}为 {value} {unit}，"
            f"低于岗位要求的 {clause.min_value} {unit}。",
            f"确认能否调整至 {clause.min_value} {unit}以上；"
            "如可调整，请先在「求职条件」更新。",
        )
    if clause.max_value is not None and value > clause.max_value:
        return (
            "partial",
            ref_id,
            f"已确认{missing_hint}为 {value} {unit}，"
            f"不在岗位期望的 {clause.min_value}～{clause.max_value} {unit}范围内。",
            "确认与岗位预期时长是否匹配；如需调整，请在「求职条件」更新。",
        )
    return (
        "covered",
        ref_id,
        f"已确认{missing_hint}为 {value} {unit}，满足岗位要求。",
        "求职条件已满足该要求，无需额外改写进简历正文。",
    )


def _match_education_clause(
    clause: ParsedClause,
    educations: Sequence[EducationCandidate],
    rules: CriteriaRules,
) -> tuple[Literal["covered", "partial", "missing"], list[str], str, str]:
    if not educations:
        return (
            "missing",
            [],
            "「教育」中尚未填写已确认的教育经历。",
            "请先在「教育」里补上教育经历，不要直接写进简历。",
        )

    req_label = next(
        label
        for code, label in rules.degree_labels.items()
        if rules.degree_ranks[code] == clause.min_value
    )
    status_suffix = (
        "在读"
        if clause.required_status is EducationStatus.ENROLLED
        else ("已毕业" if clause.required_status is EducationStatus.COMPLETED else "")
    )
    target_desc = f"{req_label}{status_suffix}"

    matched = [
        edu
        for edu in educations
        if rules.degree_ranks[edu.degree] >= clause.min_value
        and (clause.required_status is None or edu.status is clause.required_status)
    ]
    if matched:
        return (
            "covered",
            [edu.ref_id for edu in matched],
            f"教育经历「{matched[0].text}」满足{target_desc}要求。",
            "教育背景已满足该要求，保留底稿中的教育栏即可。",
        )

    degree_ok = [
        edu for edu in educations if rules.degree_ranks[edu.degree] >= clause.min_value
    ]
    if degree_ok and clause.required_status is not None:
        return (
            "partial",
            [edu.ref_id for edu in degree_ok],
            f"教育经历学历达到{req_label}，但不符合{status_suffix}状态要求。",
            "请核对就读状态或岗位投递条件是否可接受当前状态。",
        )

    highest = max(educations, key=lambda edu: rules.degree_ranks[edu.degree])
    return (
        "partial",
        [highest.ref_id],
        f"已确认最高学历为{rules.degree_labels[highest.degree]}，低于岗位要求的{target_desc}。",
        "该项为硬性条件差距，建议核对岗位是否放宽学历要求。",
    )


def evaluate_requirement(
    req: Requirement,
    criteria: Sequence[CriterionCandidate],
    education: Sequence[EducationCandidate],
    *,
    basic_fields: BasicFields | None = None,
) -> tuple[ProgramDecision | None, str | None]:
    """独立比对一条 JD 要求。返回 (程序结论, 待核对说明)。"""
    parsed = parse_requirement(req, basic_fields=basic_fields)
    if parsed.needs_review:
        return None, parsed.review_note
    if not parsed.clauses:
        return None, None

    rules = default_criteria_rules()
    crit_by_field = {c.field: c for c in criteria}

    hard_clauses = [c for c in parsed.clauses if c.hardness is RequirementType.HARD]
    pref_clauses = [
        c for c in parsed.clauses if c.hardness is RequirementType.PREFERRED
    ]
    primary_clauses = hard_clauses or pref_clauses

    outcomes: list[
        tuple[Literal["covered", "partial", "missing"], list[str], str, str]
    ] = []
    for clause in primary_clauses:
        if clause.dimension == "onsite_days":
            cand = crit_by_field.get(CriterionField.ONSITE_DAYS_PER_WEEK)
            v, ref, reason, suggestion = _match_int_clause(
                clause,
                cand.int_value if cand else None,
                cand.ref_id if cand else None,
                "天/周",
                "每周可到岗天数",
            )
            outcomes.append((v, [ref] if ref else [], reason, suggestion))
        elif clause.dimension == "internship_months":
            cand = crit_by_field.get(CriterionField.INTERNSHIP_MONTHS)
            v, ref, reason, suggestion = _match_int_clause(
                clause,
                cand.int_value if cand else None,
                cand.ref_id if cand else None,
                "个月",
                "可实习月数",
            )
            outcomes.append((v, [ref] if ref else [], reason, suggestion))
        else:
            outcomes.append(_match_education_clause(clause, education, rules))

    verdicts = [o[0] for o in outcomes]
    refs = list(dict.fromkeys(r for o in outcomes for r in o[1]))
    reasons = [o[2] for o in outcomes]
    suggestions = [o[3] for o in outcomes]

    if "missing" in verdicts and all(v == "missing" for v in verdicts):
        final_verdict: Literal["covered", "partial", "missing"] = "missing"
    elif "partial" in verdicts or "missing" in verdicts:
        final_verdict = "partial"
    else:
        final_verdict = "covered"

    # 若硬性条件已满足，单独再检验并报告附加的「优先」条件
    # （例如「本科及以上，硕士优先」）。
    if final_verdict == "covered" and hard_clauses and pref_clauses:
        pref_notes: list[str] = []
        for pref in pref_clauses:
            if pref.dimension == "education":
                pv, _, preason, _ = _match_education_clause(pref, education, rules)
                if pv != "covered":
                    pref_notes.append(
                        f"优先条件未满足（{pref.source_text}：{preason.rstrip(_SENTENCE_END)}）"
                    )
            elif pref.dimension == "onsite_days":
                cand = crit_by_field.get(CriterionField.ONSITE_DAYS_PER_WEEK)
                pv, _, preason, _ = _match_int_clause(
                    pref,
                    cand.int_value if cand else None,
                    cand.ref_id if cand else None,
                    "天/周",
                    "每周可到岗天数",
                )
                if pv != "covered":
                    pref_notes.append(
                        f"优先条件未满足（{preason.rstrip(_SENTENCE_END)}）"
                    )
            elif pref.dimension == "internship_months":
                cand = crit_by_field.get(CriterionField.INTERNSHIP_MONTHS)
                pv, _, preason, _ = _match_int_clause(
                    pref,
                    cand.int_value if cand else None,
                    cand.ref_id if cand else None,
                    "个月",
                    "可实习月数",
                )
                if pv != "covered":
                    pref_notes.append(
                        f"优先条件未满足（{preason.rstrip(_SENTENCE_END)}）"
                    )
        if pref_notes:
            reasons.extend(pref_notes)

    hardness = RequirementType.HARD if hard_clauses else RequirementType.PREFERRED
    return (
        ProgramDecision(
            verdict=final_verdict,
            criteria_refs=tuple(refs),
            reason=_join_sentences(reasons),
            suggestion=_join_sentences(list(dict.fromkeys(suggestions))),
            hardness=hardness,
        ),
        None,
    )
