"""Native Pinglian disk-link search client."""
from typing import Any, Dict, List, Optional
import requests

class PinglianClient:
    def __init__(self, base_url: str, session: Optional[requests.Session] = None, timeout: int = 15):
        self.base_url = (base_url or "").rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def search(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.base_url or not keyword or not keyword.strip():
            return []
        try:
            response = self.session.get(f"{self.base_url}/api/search", params={"q": keyword.strip(), "limit": min(max(int(limit), 1), 50)}, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError, OverflowError):
            return []
        items = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = items.get("items", items.get("results", []))
        if not isinstance(items, list):
            return []
        return [item for item in (self._normalize(value) for value in items) if item]

    @staticmethod
    def _normalize(item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        url = item.get("url") or item.get("share_url") or item.get("link")
        if not url:
            return None
        return {"url": url, "title": item.get("title") or item.get("name") or "", "password": item.get("password") or item.get("access_code") or "", "update_time": item.get("update_time") or item.get("updated_at") or "", "source": "pinglian"}
