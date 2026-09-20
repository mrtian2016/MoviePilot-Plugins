"""媒体伴随字幕的匹配、转存与本地落盘。"""

import hashlib
import html
import re
import shutil
import tempfile
import time
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.log import logger

from ...core import CloudDriveCapability, CloudFile, OwnerDelegator
from ...utils import MediaFileParser


class SubtitleService(OwnerDelegator):
    """让字幕跟随已匹配的视频完成网盘和 STRM 后处理。"""

    _TEXT_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".smi", ".sub"}
    _SIMPLIFIED_HINTS = set("这为国发后里们个来时过还对从会与体门开见说车书画风云龙")
    _TRADITIONAL_HINTS = set("這為國發後裡們個來時過還對從會與體門開見說車書畫風雲龍")

    @classmethod
    def _platform_subtitle_name(cls, subtitle_name: str, target_video_name: str) -> str:
        """调用 MoviePilot 的字幕命名逻辑，确保网盘和 STRM 使用同一名称。"""
        subtitle_path = Path(subtitle_name)
        target_path = Path(target_video_name)
        try:
            # 该方法是平台整理链路实际使用的规则入口；懒加载可保持插件源码检查环境可用。
            from app.modules.filemanager.transhandler import TransHandler
            from app.schemas.file import FileItem

            source_name = subtitle_path.name
            subtitle_item = FileItem(
                storage="local",
                type="file",
                path=f"/source/{source_name}",
                name=source_name,
                extension=subtitle_path.suffix.lstrip("."),
            )
            rename = getattr(TransHandler, "_TransHandler__rename_subtitles")
            return str(rename(subtitle_item, target_path).name)
        except Exception as error:
            # 运行时平台包缺失时仍保留稳定的兼容命名，不影响字幕后处理重试。
            logger.debug(f"调用 MoviePilot 字幕命名逻辑失败，使用兼容规则：{error}")
            return cls._fallback_subtitle_name(subtitle_path, target_path)

    @staticmethod
    def _fallback_subtitle_name(subtitle_path: Path, target_path: Path) -> str:
        stem = subtitle_path.stem
        lowered = stem.casefold()

        def has_token(*values: str) -> bool:
            return any(
                re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", lowered)
                for value in values
            )

        if any(value in stem for value in ("繁体", "繁中", "繁體")) or has_token(
                "cht", "tc", "zh-tw", "zh_tw", "zh-hant", "big5", "jptc", "tc_jp"
        ):
            language = ".zh-tw"
        elif any(value in stem for value in ("简体", "简中", "簡體", "簡中")) or has_token(
                "chs", "chi", "sc", "zh-cn", "zh_cn", "zh-hans", "cn", "sg", "zho"
        ):
            language = ".chi.zh-cn"
        elif any(value in stem for value in ("日语", "日語")) or has_token(
                "jap", "jpn", "ja", "ja-jp"
        ):
            language = ".ja"
        elif has_token("eng", "en"):
            language = ".eng"
        else:
            language = ""
        return f"{target_path.stem}{language}{subtitle_path.suffix.lower()}"

    @classmethod
    def _subtitle_name_for_language(
            cls, language: Optional[str], subtitle_name: str, target_video_name: str
    ) -> str:
        language_tokens = {
            "zh-cn": "chs",
            "zh-tw": "cht",
            "ja": "jpn",
            "eng": "eng",
        }
        token = language_tokens.get(str(language or "").lower())
        if not token:
            return cls._platform_subtitle_name(subtitle_name, target_video_name)
        suffix = Path(subtitle_name).suffix.lower()
        return cls._platform_subtitle_name(
            f"subtitle.{token}{suffix}", target_video_name
        )

    @staticmethod
    def _decode_subtitle_text(path: Path) -> str:
        if path.suffix.lower() not in SubtitleService._TEXT_SUBTITLE_EXTENSIONS:
            return ""
        raw = path.read_bytes()[:8 * 1024 * 1024]
        if not raw:
            return ""
        encodings = []
        if raw.startswith(b"\xef\xbb\xbf"):
            encodings.append("utf-8-sig")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encodings.append("utf-16")
        encodings.append("utf-8-sig")
        try:
            import chardet

            detected = chardet.detect(raw).get("encoding")
            if detected:
                encodings.append(str(detected))
        except Exception:
            pass
        encodings.extend(("gb18030", "big5", "utf-16"))
        seen = set()
        for encoding in encodings:
            normalized = encoding.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                text = raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
            control_count = sum(ord(char) < 9 for char in text)
            if control_count <= max(4, len(text) // 100):
                return text
        return ""

    @staticmethod
    def _extract_subtitle_dialogue(text: str, suffix: str) -> str:
        if suffix in {".ass", ".ssa"}:
            dialogue = []
            for line in text.splitlines():
                if not line.lstrip().casefold().startswith("dialogue:"):
                    continue
                fields = line.split(",", 9)
                dialogue.append(fields[-1] if fields else line)
            text = "\n".join(dialogue)
        text = html.unescape(text.replace(r"\N", "\n").replace(r"\n", "\n"))
        text = re.sub(r"\{\\[^}]*}", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(
            r"\b\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3}\s*--?>\s*"
            r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3}\b",
            " ",
            text,
        )
        text = re.sub(r"(?m)^\s*\d+\s*$", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _detect_subtitle_language(cls, path: Path) -> Optional[str]:
        text = cls._decode_subtitle_text(path)
        if not text:
            return None
        dialogue = cls._extract_subtitle_dialogue(text, path.suffix.lower())
        if not dialogue:
            return None
        kana_count = len(re.findall(r"[\u3040-\u30ff]", dialogue))
        han_count = len(re.findall(r"[\u3400-\u9fff]", dialogue))
        latin_count = len(re.findall(r"[A-Za-z]", dialogue))
        if kana_count >= 5 and kana_count >= han_count // 10:
            return "ja"
        if han_count >= 5:
            try:
                from app.utils.zhconv import convert as zhconv_convert

                simplified = zhconv_convert(dialogue, "zh-hans")
                traditional = zhconv_convert(dialogue, "zh-hant")
                simplified_changes = sum(
                    left != right for left, right in zip_longest(dialogue, simplified)
                )
                traditional_changes = sum(
                    left != right for left, right in zip_longest(dialogue, traditional)
                )
            except Exception:
                simplified_changes = sum(
                    char in cls._TRADITIONAL_HINTS for char in dialogue
                )
                traditional_changes = sum(
                    char in cls._SIMPLIFIED_HINTS for char in dialogue
                )
            if simplified_changes < traditional_changes:
                return "zh-cn"
            if traditional_changes < simplified_changes:
                return "zh-tw"
            return None
        if latin_count >= 20:
            return "eng"
        return None

    @classmethod
    def _subtitle_target_name(
            cls, source_video_name: str, target_video_name: str,
            subtitle_name: str,
    ) -> str:
        # 保留 source_video_name 参数以兼容现有调用方；平台规则只依据字幕文件名和目标视频名。
        return cls._platform_subtitle_name(subtitle_name, target_video_name)

    @staticmethod
    def _subtitle_file_matches(
            candidate: CloudFile, subtitle: Dict[str, Any]
    ) -> bool:
        expected_sha1 = str(subtitle.get("source_sha1") or "").strip().upper()
        actual_sha1 = str(candidate.sha1 or "").strip().upper()
        if expected_sha1 and actual_sha1 and expected_sha1 != actual_sha1:
            return False
        expected_size = int(subtitle.get("file_size") or 0)
        actual_size = int(candidate.size or 0)
        if expected_size > 0 and actual_size > 0 and expected_size != actual_size:
            return False
        return True

    @staticmethod
    def _local_file_matches(
            path: Path, expected_size: int, expected_sha1: str = ""
    ) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        if expected_size > 0 and path.stat().st_size != expected_size:
            return False
        if expected_sha1:
            digest = hashlib.sha1()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest().upper() == expected_sha1.upper()
        return True

    @staticmethod
    def _sha1_file(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    @staticmethod
    def _subtitle_matches_video(
            video_name: str,
            subtitle_name: str,
            season: Optional[int],
            episode: Optional[int],
            single_video: bool,
    ) -> bool:
        video_stem = Path(video_name).stem.casefold()
        subtitle_stem = Path(subtitle_name).stem.casefold()
        if subtitle_stem == video_stem:
            return True
        if subtitle_stem.startswith(video_stem):
            remainder = subtitle_stem[len(video_stem):]
            if not remainder or remainder[0] in ". _-[":
                return True
        if season is not None and episode is not None:
            parsed = MediaFileParser.extract_season_episode(subtitle_name)
            return parsed == (int(season), int(episode))
        return single_video

    def _companion_subtitle_files(
            self,
            files: List[Dict[str, Any]],
            video_file: Dict[str, Any],
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """关联同名字幕；单电影资源允许其唯一视频接收全部字幕。"""
        flattened = list(MediaFileParser.iter_files(files))
        video_count = sum(
            MediaFileParser.is_video(str(item.get("name") or ""))
            for item in flattened
        )
        video_name = str(video_file.get("name") or "")
        video_cloud_path = str(video_file.get("cloud_path") or "").rstrip("/")
        matched = []
        seen = set()
        for item in flattened:
            subtitle_name = str(item.get("name") or "")
            if not MediaFileParser.is_subtitle(subtitle_name):
                continue
            if (
                    video_cloud_path
                    and str(item.get("cloud_path") or "").rstrip("/")
                    != video_cloud_path
            ):
                continue
            if not self._subtitle_matches_video(
                    video_name,
                    subtitle_name,
                    season,
                    episode,
                    single_video=video_count == 1,
            ):
                continue
            identity = str(item.get("id") or "") or subtitle_name.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            matched.append(item)
        return matched

    def _transfer_companion_subtitles(
            self,
            share_url: str,
            files: List[Dict[str, Any]],
            video_file: Dict[str, Any],
            target_video_name: str,
            media_type: str,
            season: Optional[int] = None,
            episode: Optional[int] = None,
            transferred_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """转存视频的伴随字幕，并返回可持久化的后处理描述。"""
        records = []
        used_names = set()
        transferred_ids = transferred_ids if transferred_ids is not None else set()
        for subtitle in self._companion_subtitle_files(
                files, video_file, season=season, episode=episode
        ):
            subtitle_id = str(subtitle.get("id") or "")
            if subtitle_id and subtitle_id in transferred_ids:
                continue
            source_name = str(subtitle.get("name") or "")
            target_name = self._subtitle_target_name(
                str(video_file.get("name") or ""), target_video_name, source_name
            )
            original_target = target_name
            duplicate_index = 2
            while target_name.casefold() in used_names:
                path = Path(original_target)
                target_name = f"{path.stem}.{duplicate_index}{path.suffix}"
                duplicate_index += 1
            used_names.add(target_name.casefold())
            item_url = str(subtitle.get("url") or share_url).strip()
            record = {
                "id": subtitle_id,
                "share_url": item_url,
                "source_name": source_name,
                "staging_name": str(subtitle.get("staging_name") or source_name),
                "file_name": target_name,
                "source_sha1": str(subtitle.get("sha1") or ""),
                "source_md5": str(subtitle.get("md5") or ""),
                "file_size": int(subtitle.get("size") or 0),
                "transferred": False,
            }
            direct_cloud_resource = (
                    self._is_cloud_resource_url(item_url)
                    and self._is_direct_cloud_resource_url(item_url)
            )
            if direct_cloud_resource:
                record["transferred"] = True
            else:
                try:
                    record["transferred"] = bool(self._timed_sync_call(
                        "share_transfer",
                        self._transfer_file,
                        item_url,
                        subtitle,
                        self._cloud_transfer_path,
                        target_name,
                        subtitle.get("sha1") or "",
                        media_type=media_type,
                    ))
                    if subtitle.get("staging_name"):
                        record["staging_name"] = str(subtitle["staging_name"])
                except Exception as error:
                    logger.warning(f"字幕转存暂未完成，将由后处理重试：{source_name}，{error}")
            if record["transferred"]:
                logger.debug(
                    f"字幕已登记网盘内整理，待最终命名：{source_name} -> {target_name}"
                    if direct_cloud_resource
                    else f"字幕已进入转存暂存目录，待最终命名：{source_name} -> {target_name}"
                )
            if subtitle_id:
                transferred_ids.add(subtitle_id)
            records.append(record)
        return records

    def _finalize_subtitle_files(
            self,
            item: Dict[str, Any],
            directory_snapshot: Callable[[str], tuple[bool, Dict[str, CloudFile]]],
            strm_path: Optional[Path] = None,
    ) -> bool:
        """先读取字幕内容识别语言，再完成远程和本地的统一命名。"""
        subtitles = item.get("subtitles") or []
        if not subtitles:
            return True
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        staging_dir = str(
            item.get("staging_dir") or final_dir
        ).rstrip("/") or "/"
        if not self._organize_after_transfer:
            # "转存后整理"关闭：字幕与视频一并留在转存目录。
            final_dir = staging_dir
            item["cloud_dir"] = final_dir
        final_valid, final_index = directory_snapshot(final_dir)
        if not final_valid:
            return False
        staging_index = final_index
        if staging_dir != final_dir:
            staging_valid, staging_index = directory_snapshot(staging_dir)
            if not staging_valid:
                return False

        used_target_names = set()
        for subtitle in subtitles:
            provisional_name = str(subtitle.get("file_name") or "")
            source_name = str(subtitle.get("source_name") or "")
            subtitle_url = str(
                subtitle.get("share_url") or item.get("share_url") or ""
            )
            target_file = final_index.get(provisional_name)
            if target_file and not self._subtitle_file_matches(target_file, subtitle):
                target_file = None
            if not target_file and not subtitle.get("transferred"):
                retry_item = {
                    "id": subtitle.get("id"),
                    "name": source_name,
                    "size": subtitle.get("file_size") or 0,
                    "sha1": subtitle.get("source_sha1") or "",
                    "md5": subtitle.get("source_md5") or "",
                }
                try:
                    subtitle["transferred"] = bool(self._transfer_file(
                        subtitle_url,
                        retry_item,
                        self._cloud_transfer_path,
                        provisional_name,
                        subtitle.get("source_sha1") or "",
                        media_type=str(
                            getattr(
                                self._deserialize_mediainfo(item.get("mediainfo") or {}),
                                "type",
                                "",
                            )
                        ),
                    ))
                    if retry_item.get("staging_name"):
                        subtitle["staging_name"] = retry_item["staging_name"]
                except Exception as error:
                    logger.warning(f"字幕后处理重试转存失败：{source_name}，{error}")
                if not subtitle.get("transferred"):
                    return False
                _, staging_index = directory_snapshot(staging_dir)

            if not target_file:
                candidates = []
                for candidate_name in (
                        provisional_name,
                        str(subtitle.get("staging_name") or ""),
                        source_name,
                ):
                    if not candidate_name:
                        continue
                    candidate = staging_index.get(candidate_name) or final_index.get(candidate_name)
                    if candidate and candidate not in candidates:
                        candidates.append(candidate)
                source_sha1 = str(subtitle.get("source_sha1") or "").upper()
                if source_sha1:
                    candidates.extend(
                        candidate for candidate in list(staging_index.values()) + list(final_index.values())
                        if candidate not in candidates
                        and str(candidate.sha1 or "").upper() == source_sha1
                    )
                target_file = next(
                    (candidate for candidate in candidates
                     if self._subtitle_file_matches(candidate, subtitle)),
                    None,
                )
                if not target_file:
                    return False
            if not self._cloud_drive.supports(CloudDriveCapability.FILE_DOWNLOAD):
                logger.error(f"目标网盘不支持读取字幕内容：{source_name}")
                return False

            # 远程字幕必须先下载到系统临时目录，内容识别完成后才能决定最终名称。
            with tempfile.TemporaryDirectory(prefix="pansearch-subtitle-") as temp_dir:
                temp_path = Path(temp_dir) / Path(source_name or provisional_name).name
                try:
                    self._cloud_drive.require(
                        CloudDriveCapability.FILE_DOWNLOAD
                    ).download_file(target_file, str(temp_path))
                except Exception as error:
                    logger.warning(f"字幕下载到临时目录失败：{source_name}，{error}")
                    return False
                if not temp_path.is_file() or temp_path.stat().st_size <= 0:
                    return False
                expected_size = int(target_file.size or subtitle.get("file_size") or 0)
                if expected_size > 0 and temp_path.stat().st_size != expected_size:
                    logger.warning(f"字幕下载大小校验失败：{source_name}")
                    return False
                content_sha1 = str(
                    target_file.sha1 or subtitle.get("source_sha1") or ""
                ).strip().upper() or self._sha1_file(temp_path)

                detected_language = self._detect_subtitle_language(temp_path)
                target_name = self._subtitle_name_for_language(
                    detected_language,
                    source_name or provisional_name,
                    str(item.get("file_name") or provisional_name),
                )
                if not detected_language:
                    logger.debug(f"字幕内容未能确定语言，沿用平台文件名规则：{source_name}")
                subtitle["language"] = detected_language or subtitle.get("language") or ""

                original_target_name = target_name
                duplicate_index = 2
                while target_name.casefold() in used_target_names:
                    path = Path(original_target_name)
                    target_name = f"{path.stem}.{duplicate_index}{path.suffix}"
                    duplicate_index += 1
                while True:
                    existing_target = (
                            final_index.get(target_name) or staging_index.get(target_name)
                    )
                    if not existing_target or str(existing_target.id) == str(target_file.id):
                        break
                    if self._subtitle_file_matches(existing_target, subtitle):
                        # 重试时目标名可能已由上一次运行创建，复用校验一致的远程文件。
                        target_file = existing_target
                        break
                    path = Path(original_target_name)
                    target_name = f"{path.stem}.{duplicate_index}{path.suffix}"
                    duplicate_index += 1
                used_target_names.add(target_name.casefold())
                subtitle["file_name"] = target_name

                final_ids = {str(candidate.id) for candidate in final_index.values()}
                current_dir = (
                    final_dir if str(target_file.id) in final_ids else staging_dir
                )
                if self._cloud_entry_name(target_file) != target_name:
                    if not self._cloud_mutations.rename_file(
                            current_dir, target_file, target_name
                    ):
                        return False
                    target_file = self._cloud_query.get_cached_file(
                        current_dir, target_name
                    )
                    if not target_file:
                        return False
                if current_dir != final_dir and self._organize_after_transfer:
                    target_file = self._cloud_mutations.move_file(
                        target_file, final_dir, target_name
                    )
                    if not target_file:
                        return False
                subtitle["moved_at"] = time.time()
                final_index[target_name] = target_file

                if not strm_path:
                    continue
                local_path = strm_path.parent / target_name
                if (
                        self._local_file_matches(local_path, expected_size, content_sha1)
                ):
                    if provisional_name != target_name:
                        old_local_path = strm_path.parent / provisional_name
                        if (
                                old_local_path != local_path
                                and self._local_file_matches(
                            old_local_path, expected_size, content_sha1
                        )
                        ):
                            old_local_path.unlink(missing_ok=True)
                    continue
                local_temp = None
                try:
                    with tempfile.NamedTemporaryFile(
                            prefix=f".{target_name}.", suffix=".tmp",
                            dir=str(strm_path.parent), delete=False
                    ) as handle:
                        local_temp = Path(handle.name)
                    shutil.copyfile(temp_path, local_temp)
                    local_temp.replace(local_path)
                except Exception as error:
                    logger.warning(f"字幕写入 STRM 目录失败：{target_name}，{error}")
                    return False
                finally:
                    if local_temp and local_temp.exists():
                        local_temp.unlink(missing_ok=True)
                if not local_path.is_file() or local_path.stat().st_size <= 0:
                    return False
                if provisional_name != target_name:
                    old_local_path = strm_path.parent / provisional_name
                    if (
                            old_local_path != local_path
                            and self._local_file_matches(
                        old_local_path, expected_size, content_sha1
                    )
                    ):
                        old_local_path.unlink(missing_ok=True)
                logger.debug(f"字幕已完成内容识别、远程重命名并下载到 STRM 目录：{local_path}")
        return True
