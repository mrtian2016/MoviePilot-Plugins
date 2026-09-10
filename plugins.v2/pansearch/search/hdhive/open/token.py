"""HDHive OpenAPI Token 文件兼容读写。"""
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class HDHiveTokenStoreError(Exception):
    """Token 文件无法读取或写回。"""


class HDHiveTokenStore:
    ACCESS_KEYS = ("access_token", "accessToken", "token")
    REFRESH_KEYS = ("refresh_token", "refreshToken")
    EXPIRES_AT_KEYS = ("token_expires_at", "expires_at", "expiresAt")
    EXPIRES_IN_KEYS = ("expires_in", "expiresIn")

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self._format = ""
        self._data: Any = None
        self._paths: Dict[str, Tuple[Any, ...]] = {}
        self._text_lines = []
        self._single_kind = "refresh_token"
        self._newline = "\n"
        self._trailing_newline = True
        self._json_indent: Any = 2

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            raise HDHiveTokenStoreError(f"Token 文件不存在: {self.path}")
        original = self.path.read_bytes().decode("utf-8-sig")
        self._newline = "\r\n" if "\r\n" in original else "\n"
        self._trailing_newline = original.endswith(("\r", "\n"))
        raw = original.strip()
        if not raw:
            raise HDHiveTokenStoreError(f"Token 文件为空: {self.path}")

        try:
            self._data = json.loads(raw)
            if isinstance(self._data, dict):
                self._format = "json_dict"
                indent_match = re.search(r"\r?\n([ \t]+)\S", original)
                self._json_indent = indent_match.group(1) if indent_match else None
                return self._load_json_dict()
            if isinstance(self._data, str):
                self._format = "json_string"
                self._single_kind = self._guess_single_kind(self._data)
                return {self._single_kind: self._data.strip()}
        except json.JSONDecodeError:
            pass

        parsed = self._load_key_value_text(raw)
        if parsed:
            self._format = "key_value"
            return parsed

        self._format = "plain"
        self._data = raw
        self._single_kind = self._guess_single_kind(raw)
        return {self._single_kind: raw}

    def write(self, tokens: Dict[str, Any]) -> None:
        if not self._format:
            self.load()
        content = self._render(tokens)
        mode = self.path.stat().st_mode if self.path.exists() else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temp_path, mode)
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _load_json_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        mappings = {
            "access_token": self.ACCESS_KEYS,
            "refresh_token": self.REFRESH_KEYS,
            "token_expires_at": self.EXPIRES_AT_KEYS,
            "expires_in": self.EXPIRES_IN_KEYS,
            "client_id": ("client_id", "clientId"),
            "redirect_uri": ("redirect_uri", "redirectUri"),
            "state": ("state", "oauth_state", "oauthState"),
        }
        for canonical, aliases in mappings.items():
            found = self._find_key(self._data, aliases)
            if found:
                path, value = found
                self._paths[canonical] = path
                result[canonical] = value
        if not result.get("token_expires_at") and result.get("expires_in"):
            updated = self._find_key(self._data, ("updated_at", "config_updated_at", "saved_at"))
            if updated:
                try:
                    result["token_expires_at"] = float(updated[1]) + float(result["expires_in"])
                except (TypeError, ValueError):
                    pass
        return result

    def _load_key_value_text(self, raw: str) -> Dict[str, Any]:
        self._text_lines = raw.splitlines()
        result: Dict[str, Any] = {}
        aliases = {
            **{key.lower(): "access_token" for key in self.ACCESS_KEYS},
            **{key.lower(): "refresh_token" for key in self.REFRESH_KEYS},
            **{key.lower(): "token_expires_at" for key in self.EXPIRES_AT_KEYS},
            **{key.lower(): "expires_in" for key in self.EXPIRES_IN_KEYS},
        }
        for index, line in enumerate(self._text_lines):
            match = re.match(r"^(\s*)([A-Za-z_][\w.-]*)(\s*[=:]\s*)(.*?)(\s*)$", line)
            if not match:
                continue
            canonical = aliases.get(match.group(2).lower())
            if canonical:
                result[canonical] = match.group(4).strip().strip('"\'')
                self._paths[canonical] = (index, match.groups())
        return result

    def _render(self, tokens: Dict[str, Any]) -> str:
        if self._format == "json_dict":
            self._update_json_dict(tokens)
            rendered = json.dumps(self._data, ensure_ascii=False, indent=self._json_indent)
            if self._newline != "\n":
                rendered = rendered.replace("\n", self._newline)
            return rendered + (self._newline if self._trailing_newline else "")
        if self._format == "json_string":
            rendered = json.dumps(str(tokens.get(self._single_kind) or self._data), ensure_ascii=False)
            return rendered + (self._newline if self._trailing_newline else "")
        if self._format == "key_value":
            lines = list(self._text_lines)
            for canonical, path_info in self._paths.items():
                if canonical not in tokens:
                    continue
                index, groups = path_info
                leading, key, separator, _old, trailing = groups
                lines[index] = f"{leading}{key}{separator}{tokens[canonical]}{trailing}"
            separator = next((info[1][2] for info in self._paths.values()), "=")
            for canonical in ("access_token", "refresh_token"):
                if canonical not in self._paths and tokens.get(canonical):
                    lines.append(f"{canonical}{separator}{tokens[canonical]}")
            rendered = self._newline.join(lines)
            return rendered + (self._newline if self._trailing_newline else "")
        rendered = str(tokens.get(self._single_kind) or self._data).strip()
        return rendered + (self._newline if self._trailing_newline else "")

    def _update_json_dict(self, tokens: Dict[str, Any]) -> None:
        for canonical in ("access_token", "refresh_token"):
            value = tokens.get(canonical)
            if value:
                self._set_json_value(canonical, value)

        expires_at = tokens.get("token_expires_at")
        expires_in = tokens.get("expires_in")
        if expires_at:
            self._set_json_value("token_expires_at", int(float(expires_at)))
        if expires_in is not None:
            self._set_json_value("expires_in", int(expires_in or 0))

        for key in ("refresh_expires_in", "scope", "scopes", "token_type"):
            if key in tokens:
                found = self._find_key(self._data, (key,))
                if found:
                    self._set_path_value(found[0], tokens[key])
        now = int(time.time())
        for key in ("updated_at", "config_updated_at"):
            found = self._find_key(self._data, (key,))
            if found:
                self._set_path_value(found[0], now)

    def _set_json_value(self, canonical: str, value: Any) -> None:
        path = self._paths.get(canonical)
        if not path:
            preferred = {
                "access_token": "access_token",
                "refresh_token": "refresh_token",
                "token_expires_at": "token_expires_at",
                "expires_in": "expires_in",
            }[canonical]
            anchor = self._token_container_path()
            container = self._get_path_value(anchor)
            container[preferred] = value
            self._paths[canonical] = anchor + (preferred,)
            return
        self._set_path_value(path, value)

    def _token_container_path(self) -> Tuple[Any, ...]:
        for key in ("access_token", "refresh_token", "expires_in", "token_expires_at"):
            path = self._paths.get(key)
            if path:
                return path[:-1]
        return ()

    def _get_path_value(self, path: Tuple[Any, ...]) -> Any:
        current = self._data
        for part in path:
            current = current[part]
        return current

    def _set_path_value(self, path: Tuple[Any, ...], value: Any) -> None:
        current = self._get_path_value(path[:-1])
        current[path[-1]] = value

    @classmethod
    def _find_key(cls, value: Any, aliases: Tuple[str, ...], path: Tuple[Any, ...] = ()) -> Optional[
        Tuple[Tuple[Any, ...], Any]]:
        if isinstance(value, dict):
            for alias in aliases:
                if alias in value and value[alias] not in (None, ""):
                    return path + (alias,), value[alias]
            for key, child in value.items():
                found = cls._find_key(child, aliases, path + (key,))
                if found:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found = cls._find_key(child, aliases, path + (index,))
                if found:
                    return found
        return None

    @staticmethod
    def _guess_single_kind(token: str) -> str:
        return "access_token" if token.strip().count(".") == 2 else "refresh_token"
