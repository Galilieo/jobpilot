"""完整简历的内容结构与组装（01 F5，06 T2.5）。纯函数，不调用模型。

底稿是用户核对过的纯文字简历。事实拆分时已记下每段经历标题在原文的位置（sections_json），
这里按这些位置切段（与 OpenResume 按标题切段的做法相同）：
- 第一段之前是页头（姓名、联系方式、求职意向、到岗信息），原样保留；
- 每段经历的第一行是标题行，按规则拆出名称和时间，拆不出时间就整行当名称；
- 段末形如「教育经历」「技术栈」的短行是下一组的栏目名；只有后面确实还有一段时才拿，
  最后一段的末行一定是这一段自己的正文；
- 改写过的经历用改写条目替换正文；没改写的（教育、获奖等）正文原样保留。
所以成品里改写以外的文字都出自底稿，不会多出底稿没有的内容。

依据检查的结果跟着它检查的那段文字保存（BulletCheck.text）：文字改了、结果没跟上时，
显示「待检查」而不是沿用旧结论（评审 2026-11-24）。
"""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from jobpilot.db.models import CheckStatus, SectionKind
from jobpilot.services.grounding import GroundingIssue

SKILLS_TITLE = "专业技能"  # 底稿没有技能栏、却有技能概述时新加的栏目名
# 编辑器里的加粗按 Markdown 的写法存在文字里：「负责**运单查询**接口」。
BOLD = re.compile(r"\*\*(.+?)\*\*")


def plain(text: str) -> str:
    """去掉加粗标记。依据检查和「改没改过」只看文字本身，加粗不算修改。"""
    return BOLD.sub(r"\1", text)


class CheckLayers(StrEnum):
    FULL = "full"  # 生成时三层都查过
    PROGRAM = "program"  # 手动修改后只查了第一、二层


class BulletCheck(BaseModel):
    """一次依据检查的结论，绑定它检查的文字。"""

    text: str
    layers: CheckLayers
    issues: list[GroundingIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


class Bullet(BaseModel):
    bullet_id: UUID  # tailored_bullet 的 ID，用来找回引用的事实
    text: str
    fact_ids: list[UUID]
    origin: CheckStatus  # 生成时的结果：通过、回退成事实原文、待处理
    edited: bool = False  # 用户手动改过
    check: BulletCheck

    @property
    def check_current(self) -> bool:
        """检查结论是不是针对现在这段文字（加粗与否不影响）。"""
        return plain(self.check.text) == plain(self.text)


class Entry(BaseModel):
    """一段经历：标题行加正文。改写过的经历用 bullets，没改写的用 lines（底稿原文）。"""

    entry_id: str
    experience: str | None  # 对应事实库里的经历名；技能概述和未识别的段为空
    heading: str  # 底稿里的标题行；新补入的经历为经历名
    title: str
    time: str | None = None
    lines: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class ResumeSection(BaseModel):
    title: str | None  # 底稿里的栏目名，例如「实习经历」；没有栏目名时为空
    kind: SectionKind
    entries: list[Entry]


class ResumeStyle(BaseModel):
    """编辑器工具栏调的版式：字体、字号、行距。随版本保存，导出文件也带着。"""

    font: str = Field(default="", max_length=60)  # 空表示默认的系统中文字体
    size_pt: float = Field(default=10.5, ge=7, le=14)
    leading: float = Field(default=1.5, ge=1.1, le=2.0)


class ResumeContent(BaseModel):
    header: list[str]  # 页头各行，第一行一般是姓名
    sections: list[ResumeSection]
    style: ResumeStyle = Field(default_factory=ResumeStyle)


class BaseSection(BaseModel):
    """底稿里一段经历的位置，即 resume_document.sections_json 的一项。"""

    name: str
    section: SectionKind
    start: int
    end: int


class BlockBullets(BaseModel):
    """组装用的一块：改写计划里的一段经历（或技能概述）和它定稿的条目。"""

    experience: str | None  # 技能概述为空
    section: SectionKind
    bullets: list[Bullet]


# ---------- 标题行 ----------

_DATE = r"(?:19|20)\d{2}(?:\s*[./年-]\s*\d{1,2}\s*月?)?"
_TIME = re.compile(
    rf"[（(]?\s*{_DATE}\s*(?:[–—~～至到-]+\s*(?:{_DATE}|至今|今)|至今)?"
    r"\s*(?:[（(]预计[）)])?\s*[）)]?"
)
_SEPARATORS = " \t|｜/／,，、·-–—"


def split_heading(line: str) -> tuple[str, str | None, str]:
    """标题行拆成 (名称, 时间, 同一行里标题之后的正文)。

    「同川数服 / Java 实习生 / 2023.07–2023.10」→ 名称「同川数服 / Java 实习生」，
    时间「2023.07–2023.10」。有的简历把标题和正文写在一段里
    （「学校东门便利店，周末店员（2024.09–2025.06）。负责补货……」），
    句号之后算正文。拆不出时间时整行（句号前）都是名称。
    """
    head, _, rest = line.partition("。")
    matches = list(_TIME.finditer(head))
    if not matches:
        return head.strip(_SEPARATORS), None, rest.strip()
    time = matches[-1]
    title = (head[: time.start()] + head[time.end() :]).strip(_SEPARATORS)
    stamp = time.group().strip(" （()）")
    if stamp.endswith("预计"):
        stamp += "）"  # 「2027.06（预计」被 strip 去掉了右括号
    return title or head.strip(_SEPARATORS), stamp, rest.strip()


# ---------- 栏目名 ----------

_SECTION_WORDS = re.compile(
    r"教育|学历|实习|工作|项目|技能|技术|工具|获奖|荣誉|证书|校园|社团|兼职|志愿|"
    r"其他|经历|经验|背景|评价|总结|自我"
)
_SENTENCE = re.compile(r"[，。；：,.;:!?！？0-9]")


def is_section_title(line: str) -> bool:
    """「教育经历」「技术与工具」这类单独成行的短栏目名。"""
    text = line.strip()
    return (
        0 < len(text) <= 10
        and _SECTION_WORDS.search(text) is not None
        and _SENTENCE.search(text) is None
    )


def _split_tail_title(
    lines: list[str], *, followed: bool
) -> tuple[list[str], str | None]:
    """去掉末尾的空行；最后一行是栏目名时拿出来，它属于下一组。

    只有 followed（后面确实还有一段）时才拿：最后一段的末行后面没有栏目承接，
    拿走就丢了。单独一行「教育技术学」很短、含「教育」、没有标点，
    和栏目名没有区别（评审 2026-11-25）。
    """
    body = list(lines)
    while body and not body[-1].strip():
        body.pop()
    if followed and body and is_section_title(body[-1]):
        title = body.pop().strip()
        while body and not body[-1].strip():
            body.pop()
        return body, title
    return body, None


# ---------- 组装 ----------


def _base_entries(
    raw_text: str, sections: Sequence[BaseSection]
) -> tuple[list[str], list[tuple[BaseSection, str | None, list[str]]]]:
    """切出页头和每段经历：(位置, 这段之前的栏目名, 这段的原文各行)。"""
    ordered = sorted(sections, key=lambda s: s.start)
    if not ordered:
        header, _ = _split_tail_title(raw_text.splitlines(), followed=False)
        return header, []
    header, title = _split_tail_title(
        raw_text[: ordered[0].start].splitlines(), followed=True
    )
    pieces = []
    for index, section in enumerate(ordered):
        lines, next_title = _split_tail_title(
            raw_text[section.start : section.end].splitlines(),
            followed=index < len(ordered) - 1,  # 最后一段后面没有栏目承接
        )
        pieces.append((section, title, lines))
        title = next_title
    return [line for line in header if line.strip()], pieces


def _entry(entry_id: str, experience: str, lines: list[str]) -> Entry:
    heading = next((line for line in lines if line.strip()), "")
    title, time, rest = split_heading(heading)
    body = [rest] if rest else []
    body += [line for line in lines[lines.index(heading) + 1 :] if line.strip()]
    return Entry(
        entry_id=entry_id,
        experience=experience,
        heading=heading,
        title=title,
        time=time,
        lines=body,
    )


def assemble(
    raw_text: str,
    base_sections: Sequence[BaseSection],
    blocks: Sequence[BlockBullets],
) -> ResumeContent:
    """用底稿和改写结果组装完整简历。

    - 栏目和经历沿用底稿的顺序；同一栏里改写过的经历按改写计划的顺序排（只在它们原来的
      位置之间互换，没改写的经历不动）。
    - 改写过的经历换成改写条目；技能概述替换底稿技能栏的正文，底稿没有技能栏就加在最后。
    - 计划里有、底稿里没有的经历（事实来自另一份简历），补在同类栏目的最后。
    - 待处理的条目也放进来，由界面标出、导出时略过；成品里不会放回底稿原句。
    """
    header, pieces = _base_entries(raw_text, base_sections)
    by_experience = {b.experience: b for b in blocks if b.experience is not None}
    plan_rank = {b.experience: i for i, b in enumerate(blocks)}
    skills = next((b for b in blocks if b.experience is None), None)

    result: list[ResumeSection] = []
    placed: set[str] = set()
    for index, (base, title, lines) in enumerate(pieces):
        entry = _entry(f"e{index + 1}", base.name, lines)
        block = by_experience.get(base.name)
        if block is not None:
            entry = entry.model_copy(update={"lines": [], "bullets": block.bullets})
            placed.add(base.name)
        elif base.section is SectionKind.SKILL and skills is not None:
            entry = entry.model_copy(update={"lines": [], "bullets": skills.bullets})
            skills = None  # 技能概述只放一次
        if title is not None or not result or result[-1].kind is not base.section:
            result.append(ResumeSection(title=title, kind=base.section, entries=[]))
        result[-1].entries.append(entry)

    for section in result:
        _order_rewritten(section, plan_rank)

    extra = [
        b for b in blocks if b.experience is not None and b.experience not in placed
    ]
    for number, block in enumerate(extra, start=len(pieces) + 1):
        entry = Entry(
            entry_id=f"e{number}",
            experience=block.experience,
            heading=block.experience or "",
            title=block.experience or "",
            bullets=block.bullets,
        )
        target = next((s for s in reversed(result) if s.kind is block.section), None)
        if target is None:
            target = ResumeSection(title=None, kind=block.section, entries=[])
            result.append(target)
        target.entries.append(entry)

    if skills is not None and skills.bullets:
        entry = Entry(
            entry_id=f"e{len(pieces) + len(extra) + 1}",
            experience=None,
            heading=SKILLS_TITLE,
            title=SKILLS_TITLE,
            bullets=skills.bullets,
        )
        result.append(
            ResumeSection(title=SKILLS_TITLE, kind=SectionKind.SKILL, entries=[entry])
        )
    return ResumeContent(header=header, sections=result)


def _order_rewritten(
    section: ResumeSection, plan_rank: Mapping[str | None, int]
) -> None:
    """改写过的经历按计划顺序排，只在它们原来占的位置之间互换。"""
    slots = [
        i
        for i, e in enumerate(section.entries)
        if e.bullets and e.experience in plan_rank
    ]
    ordered = sorted(
        (section.entries[i] for i in slots), key=lambda e: plan_rank[e.experience]
    )
    for slot, entry in zip(slots, ordered, strict=True):
        section.entries[slot] = entry


def bullet_of(
    bullet_id: UUID,
    text: str,
    fact_ids: Sequence[UUID],
    status: CheckStatus,
    issues: Sequence[GroundingIssue],
) -> Bullet:
    """生成时定稿的一条，检查结论绑定生成时的文字。

    issues 是生成时最后一次检查的问题。回退的条目里，它说的是被弃用的那句，
    不是现在的文字：现在的文字照抄事实原文，第一、二层必然通过，所以结论记为无问题。
    """
    if status is CheckStatus.FALLBACK:
        check = BulletCheck(text=text, layers=CheckLayers.PROGRAM)
    else:
        check = BulletCheck(text=text, layers=CheckLayers.FULL, issues=list(issues))
    return Bullet(
        bullet_id=bullet_id,
        text=text,
        fact_ids=list(fact_ids),
        origin=status,
        check=check,
    )
