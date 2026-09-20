"""豆瓣 RSS 解析服务。"""
import re
import xml.dom.minidom
from typing import Iterator

from app.utils.dom import DomUtils

from ...core.subscribe import MediaCandidate


class DoubanService:
    DOUBAN_ID = re.compile(r"/(\d+)/")
    YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

    @classmethod
    def parse(cls, text: str, source: str) -> Iterator[MediaCandidate]:
        root = xml.dom.minidom.parseString(text).documentElement
        for item in root.getElementsByTagName("item"):
            title = str(DomUtils.tag_value(item, "title", default="") or "").strip()
            link = str(DomUtils.tag_value(item, "link", default="") or "").strip()
            if not title:
                continue
            found_id = cls.DOUBAN_ID.findall(link)
            description = str(DomUtils.tag_value(item, "description", default="") or "")
            year = str(DomUtils.tag_value(item, "year", default="") or "").strip()
            if not year:
                found_year = cls.YEAR.findall(description)
                year = found_year[0] if found_year else ""
            raw_type = str(DomUtils.tag_value(item, "type", default="") or "").lower()
            media_type = "movie" if raw_type == "movie" else "tv" if raw_type else None
            yield MediaCandidate(
                title=title,
                year=year or None,
                media_type=media_type,
                douban_id=found_id[0] if found_id else None,
                source="douban",
                source_meta={"url": link, "rss": source},
                unique_seed=f"{found_id[0] if found_id else title}:{year}",
            )
