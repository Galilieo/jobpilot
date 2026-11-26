"""核对条目编辑请求中的原话及指向的简历条目。"""

import re
from collections.abc import Iterator
from uuid import UUID

from jobpilot.db.models import Message
from jobpilot.services.agent.arguments import EditResumeArgs
from jobpilot.services.resume_content import Bullet, ResumeContent

_ORDINAL = re.compile(r"第\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*条")
_ASSIGNMENT = re.compile(r"(?:改成|改为|替换成|替换为)[：:]?\s*")
_REPLACE_TARGET = re.compile(r"这条|该条|整条|这一条|唯一一条")
_NEGATIVE_EDIT = re.compile(r"不要|不用|别|不想|无需|不必")
_POLISH_REQUEST = re.compile(r"润色|写得|改得|更偏|改写|突出|简化|精简|优化")
_CHINESE_DIGITS = {char: digit for digit, char in enumerate("零一二三四五六七八九")}


def _ordinal(text: str) -> int:
    if text.isdecimal():
        return int(text)
    if "十" in text:
        before, _, after = text.partition("十")
        return _CHINESE_DIGITS.get(before, 1) * 10 + _CHINESE_DIGITS.get(after, 0)
    return _CHINESE_DIGITS.get(text, 0)


def _bullets(resume: ResumeContent) -> Iterator[Bullet]:
    for section in resume.sections:
        for entry in section.entries:
            yield from entry.bullets


def _target_is_unique(content: ResumeContent, body: str, bullet_id: UUID) -> bool:
    bullets = list(_bullets(content))
    matches = list(_ORDINAL.finditer(body))
    if not matches:
        return len(bullets) == 1 and bullets[0].bullet_id == bullet_id
    if len(matches) != 1:
        return False
    position = _ordinal(matches[0][1])
    # 条目顺序按当前简历的栏、经历、条目顺序。带局部栏目指示的序号需用户明确。
    before = body[: matches[0].start()]
    names = [section.title for section in content.sections]
    names.extend(
        entry.heading for section in content.sections for entry in section.entries
    )
    if len(bullets) > 1 and any(name and name in before for name in names):
        return False
    return (
        1 <= position <= len(bullets) and bullets[position - 1].bullet_id == bullet_id
    )


def authorized_edit(
    message: Message | None, resume: ResumeContent, args: EditResumeArgs
) -> bool:
    if message is None or message.content.count(args.quote) != 1:
        return False
    if any(args.quote in item["text"] for item in message.attachments_json):
        return False
    if args.mode == "polish":
        return (
            bool(_POLISH_REQUEST.search(args.quote))
            and _target_is_unique(resume, message.content, args.bullet_id)
            and not _NEGATIVE_EDIT.search(message.content)
        )
    before, _, after = message.content.partition(args.quote)
    assignments = list(_ASSIGNMENT.finditer(before))
    return (
        len(assignments) == 1
        and bool(_ORDINAL.search(before) or _REPLACE_TARGET.search(before))
        and not _NEGATIVE_EDIT.search(before)
        and not before[assignments[0].end() :].lstrip('「『"“')
        and after.strip(' \t\r\n」』"”。！!') == ""
        and _target_is_unique(resume, before, args.bullet_id)
        and not _ASSIGNMENT.search(args.quote)
        and not _ORDINAL.search(args.quote)
    )
