"""读取 jobpilot/data/resume_fonts.yaml：字体代码、显示名与 CSS 字体串映射。"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from jobpilot.config import REPO_ROOT

DEFAULT_FONTS_PATH = REPO_ROOT / "jobpilot" / "data" / "resume_fonts.yaml"


class ResumeFontsError(ValueError):
    """字体配置格式不对或存在重复代码。"""


@dataclass(frozen=True)
class FontOption:
    code: str
    label: str
    css: str
    legacy_css: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeFonts:
    default_code: str
    options: tuple[FontOption, ...]
    by_code: dict[str, FontOption]
    legacy_to_code: dict[str, str]

    def css_of(self, code: str) -> str:
        option = self.by_code.get(code)
        if option is None:
            raise ValueError(f"不支持的字体代码：{code}")
        return option.css

    def code_from_stored(self, raw: object) -> str:
        """读取历史版本里存的值：已是代码直接保留，旧字体串映射回代码，认不出用默认。"""
        if not isinstance(raw, str):
            return self.default_code
        text = raw.strip()
        if not text:
            return self.default_code
        if text in self.by_code:
            return text
        return self.legacy_to_code.get(text, self.default_code)

    def editor_options(self) -> list[dict[str, str]]:
        return [
            {"code": option.code, "label": option.label, "css": option.css}
            for option in self.options
        ]


def load_resume_fonts(path: Path) -> ResumeFonts:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    options: list[FontOption] = []
    by_code: dict[str, FontOption] = {}
    legacy_to_code: dict[str, str] = {}

    for entry in data.get("fonts") or []:
        code = str(entry["code"]).strip()
        if not code or code in by_code:
            raise ResumeFontsError(f"{path.name}：字体代码无效或重复：{code!r}")
        css = str(entry.get("css") or "").strip()
        legacy = tuple(str(x).strip() for x in (entry.get("legacy_css") or ()) if x)
        option = FontOption(
            code=code,
            label=str(entry["label"]).strip(),
            css=css,
            legacy_css=legacy,
        )
        options.append(option)
        by_code[code] = option
        if css:
            legacy_to_code[css] = code
        for old in legacy:
            legacy_to_code[old] = code

    default_code = str(data.get("default_code") or "").strip()
    if default_code not in by_code:
        raise ResumeFontsError(f"{path.name}：default_code 不在字体列表中")

    return ResumeFonts(
        default_code=default_code,
        options=tuple(options),
        by_code=by_code,
        legacy_to_code=legacy_to_code,
    )


@cache
def default_fonts() -> ResumeFonts:
    return load_resume_fonts(DEFAULT_FONTS_PATH)
