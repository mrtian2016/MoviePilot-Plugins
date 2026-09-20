"""外部资源站点的媒体标题匹配工具。"""

import html
import re
from typing import Callable, Iterable, List, Optional, Tuple

import unicodedata

_TITLE_SEPARATOR_RE = re.compile(
    r"[\s\u3000\-_:：~～·•丨｜|¦.,，。!！?？'\"“”‘’()（）\[\]【】/\\]+"
)
_ROMAN_SEASON_PATTERN = re.compile(
    r"(?i)(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"
    r"[\s\u3000._-]*(VIII|VII|VI|IV|III|II|IX|X|V|I)"
    r"(?=$|[\s\u3000._:：~～-])"
)
_SEASON_PATTERNS = (
    re.compile(r"(?i)\bS(?:eason)?[ ._-]*0*(\d{1,3})\b"),
    re.compile(r"(?i)\bSeason[ ._-]*0*(\d{1,3})\b"),
    _ROMAN_SEASON_PATTERN,
    re.compile(r"第\s*([零〇一二两三四五六七八九十百\d]{1,6})\s*季"),
)
_ROMAN_NUMBERS = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
}


def unique_texts(
        values: Iterable[object],
        normalizer: Optional[Callable[[str], str]] = None,
) -> List[str]:
    """清理、按原顺序去重一组文本，并可统一规范化。"""
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if normalizer:
            text = normalizer(text)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def media_identifier_queries(
        tmdb_id: object = None,
        douban_id: object = None,
        imdb_id: object = None,
) -> List[Tuple[str, str, str]]:
    """按统一优先级返回可用的媒体 ID 查询：TMDB、豆瓣、IMDb。"""
    result = []
    seen = set()
    for label, value, response_field in (
            ("TMDB", tmdb_id, "tmdb_id"),
            ("豆瓣", douban_id, "douban_id"),
            ("IMDb", imdb_id, "imdb_id"),
    ):
        query = str(value or "").strip()
        normalized = query.casefold()
        if not query or normalized in seen:
            continue
        seen.add(normalized)
        result.append((label, query, response_field))
    return result


def positive_ints(values: Iterable[object]) -> set:
    """返回有效正整数集合，忽略外部接口中的空值和非法值。"""
    result = set()
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.add(number)
    return result


def normalize_title(value: object) -> str:
    """生成忽略空白、标点和大小写的标题指纹。"""
    text = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return _TITLE_SEPARATOR_RE.sub("", text)


def _chinese_number(value: str) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100}
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            return None
    return total + current


def extract_season(value: object) -> Optional[int]:
    """从中英文发布标题提取季号。"""
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    for index, pattern in enumerate(_SEASON_PATTERNS):
        matched = pattern.search(text)
        if not matched:
            continue
        if index < 2:
            return int(matched.group(1))
        if pattern is _ROMAN_SEASON_PATTERN:
            return _ROMAN_NUMBERS.get(matched.group(1).upper())
        return _chinese_number(matched.group(1))
    return None


def title_without_season(value: object) -> str:
    """移除季号后生成标题指纹，用于剧集作品级匹配。"""
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    for pattern in _SEASON_PATTERNS:
        text = pattern.sub(" ", text)
    return normalize_title(text)


def title_matches(candidate: object, expected_titles: Iterable[object]) -> bool:
    """要求候选标题与至少一个期望标题在作品级精确一致。"""
    candidate_normalized = title_without_season(candidate)
    if not candidate_normalized:
        return False
    return any(
        candidate_normalized == title_without_season(expected)
        for expected in expected_titles
        if str(expected or "").strip()
    )


def extract_year(value: object) -> str:
    matched = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
    return matched.group(1) if matched else ""
