"""Mikan 季度页和详情页解析。"""
import re
from typing import Iterator

from bs4 import BeautifulSoup

from ...core.subscribe import MediaCandidate


class MikanService:
    BANGUMI_ID = re.compile(r"b(?:gm|angumi)\.tv/subject/(\d+)")
    YEAR = re.compile(r"(19\d{2}|20\d{2}|2100)")

    @staticmethod
    def season_entries(html: str, base: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        result, seen = [], set()
        for group in soup.select("div.sk-bangumi"):
            for item in group.select("li"):
                span = item.select_one("span[data-bangumiid]")
                anchor = item.select_one("a.an-text")
                if not span or not anchor:
                    continue
                mikan_id = str(span.get("data-bangumiid") or "").strip()
                title = str(anchor.get("title") or anchor.get_text(strip=True) or "").strip()
                if not mikan_id or not title or mikan_id in seen:
                    continue
                seen.add(mikan_id)
                cover = str(span.get("data-src") or "").strip()
                if cover.startswith("/"):
                    cover = f"{base}{cover}"
                result.append({"mikan_id": mikan_id, "title": title, "cover": cover})
        return result

    @classmethod
    def detail(cls, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        nodes = soup.select("p.bangumi-info") or soup.select(".bangumi-info")
        text = "\n".join(node.get_text(" ", strip=True) for node in nodes) or html
        bangumi = cls.BANGUMI_ID.search(text)
        year = cls.YEAR.search(text)
        return {
            "bangumi_id": int(bangumi.group(1)) if bangumi else None,
            "year": year.group(1) if year else None,
        }

    @staticmethod
    def candidate(entry: dict, year: str, detail: dict) -> MediaCandidate:
        return MediaCandidate(
            title=entry["title"],
            year=detail.get("year") or year,
            media_type="tv",
            season=1,
            bangumi_id=detail.get("bangumi_id"),
            source="mikan",
            source_meta={"mikan_id": entry["mikan_id"], "cover": entry.get("cover")},
            unique_seed=f"{detail.get('bangumi_id') or entry['mikan_id']}:{year}",
        )

    def candidates(
            self, entries: list[dict], year: str, detail_loader=None
    ) -> Iterator[MediaCandidate]:
        for entry in entries:
            detail = detail_loader(entry) if detail_loader else {}
            yield self.candidate(entry, year, detail or {})
