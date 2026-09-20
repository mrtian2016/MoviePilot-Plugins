"""HDHive 动态验证码识别与安全页处理。"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from app.log import logger

from .action import ServerActionProtocol, ServerActionResponse
from .parser import decode_embedded_text, response_body, response_text

BASE_URL = "https://re0.me"
ALPHABET = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CHALLENGE_PATH = "/security-check"
TEXT_TOP = 20
TEXT_BOTTOM = 112
TEXT_LEFT = 40
TEXT_RIGHT = 280
SLOT_WIDTH = 48
DIRECTION_LAG = 8
MASK_LAG = 1
SEGMENT_FRAME_COUNT = 36
NOISE_QUANTILE = 0.975
BOUNDARY_SEARCH_RADIUS = 8
MAX_VERIFY_ATTEMPTS = 3
MOVEMENT_DIRECTIONS = tuple(
    (dx, dy)
    for dy in (-1, 0, 1)
    for dx in (-1, 0, 1)
    if (dx, dy) != (0, 0)
)
SLOT_BOUNDARIES = list(range(TEXT_LEFT, TEXT_RIGHT + 1, SLOT_WIDTH))
ROUTER_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(no-layout)%22%2C%7B%22children%22"
    "%3A%5B%22security-check%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B"
    "%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull"
    "%2Cnull%2Ctrue%5D"
)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
GIF_RE = re.compile(r"data:image/gif;base64,([A-Za-z0-9+/=]+)", re.I)
NEXT_REDIRECT_RE = re.compile(r"NEXT_REDIRECT;[^;]+;([^;]+);\d+;", re.I)
PAGE_CHUNK_RE = re.compile(
    r"static/chunks/app/\(no-layout\)/security-check/page-[A-Za-z0-9]+\.js"
)
ACTION_RE = re.compile(
    r'createServerReference\)\("([0-9a-f]{40,64})",'
    r'(?:(?!createServerReference).){0,300}?"([A-Za-z]+AbuseChallenge)"',
    re.S,
)


class HDHiveCaptchaError(RuntimeError):
    """验证码识别、刷新或提交失败。"""

    def __init__(self, message: str, code: str = "captcha_failed"):
        super().__init__(message)
        self.code = str(code or "captcha_failed")


@dataclass(frozen=True)
class CaptchaChallenge:
    challenge_id: str
    url: str
    return_to: str
    page: bytes


@dataclass(frozen=True)
class ActionResult:
    success: bool
    code: str = ""
    message: str = ""
    remaining_attempts: Optional[int] = None
    clearance_seconds: int = 0


def _dependencies():
    try:
        import numpy as np
        from PIL import Image, ImageSequence
    except ImportError as error:
        raise HDHiveCaptchaError(
            "HDHive 验证码依赖缺失，请安装 Pillow 和 numpy",
            code="captcha_dependency_missing",
        ) from error
    return np, Image, ImageSequence


def _text_area(np, shape: tuple[int, int], margin: int):
    area = np.zeros(shape, dtype=bool)
    area[
        TEXT_TOP - margin:TEXT_BOTTOM - margin,
        TEXT_LEFT - margin:TEXT_RIGHT - margin,
    ] = True
    return area


def _box_mean(np, array):
    padded = np.pad(array, 1, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (3, 3))
    return windows.mean(axis=(-2, -1), dtype=np.float32)


def _connected_components(np, mask):
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    components = []
    for start_y, start_x in zip(*np.where(mask)):
        if seen[start_y, start_x]:
            continue
        seen[start_y, start_x] = True
        stack = [(int(start_y), int(start_x))]
        component = []
        while stack:
            y, x = stack.pop()
            component.append((y, x))
            for offset_y in (-1, 0, 1):
                for offset_x in (-1, 0, 1):
                    next_y = y + offset_y
                    next_x = x + offset_x
                    if not (0 <= next_y < height and 0 <= next_x < width):
                        continue
                    if mask[next_y, next_x] and not seen[next_y, next_x]:
                        seen[next_y, next_x] = True
                        stack.append((next_y, next_x))
        rows, columns = zip(*component)
        components.append((np.asarray(rows), np.asarray(columns)))
    return components


def _remove_small_components(np, mask, minimum: int):
    cleaned = np.zeros_like(mask, dtype=bool)
    for rows, columns in _connected_components(np, mask):
        if len(rows) >= minimum:
            cleaned[rows, columns] = True
    return cleaned


def _extract_score_mask(np, Image, score, threshold: float, minimum: int, margin: int):
    smoothed = _box_mean(np, score)
    valid_area = _text_area(np, smoothed.shape, margin)
    edge_values = smoothed[~valid_area]
    effective_threshold = max(
        threshold,
        float(np.quantile(edge_values, NOISE_QUANTILE)),
    )
    mask = smoothed > effective_threshold
    mask &= valid_area
    mask = _remove_small_components(np, mask, minimum)
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    from PIL import ImageFilter
    image = image.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    mask = _remove_small_components(np, np.asarray(image) > 0, minimum)

    confidence = np.zeros_like(smoothed, dtype=np.float32)
    if mask.any():
        signal_q95 = float(np.quantile(smoothed[mask], 0.95))
        signal_span = max(signal_q95 - effective_threshold, 1e-6)
        confidence = np.clip(
            (smoothed - effective_threshold) / signal_span,
            0,
            1,
        )
        confidence *= mask
    return mask, confidence


def _mask_quality(np, mask, margin: int) -> float:
    inner = mask[
        TEXT_TOP - margin:TEXT_BOTTOM - margin,
        TEXT_LEFT - margin:TEXT_RIGHT - margin,
    ]
    slots = np.asarray([
        mask[
            TEXT_TOP - margin:TEXT_BOTTOM - margin,
            left - margin:right - margin,
        ].mean()
        for left, right in zip(SLOT_BOUNDARIES, SLOT_BOUNDARIES[1:])
    ])
    area = float(inner.mean())
    balance = float(slots.min() / (slots.mean() + 1e-6))
    foreground = int(mask.sum())
    neighbor_pairs = sum(
        int(np.logical_and(mask, np.roll(mask, (dy, dx), axis=(0, 1))).sum())
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
    )
    neighbor_ratio = neighbor_pairs / max(4 * foreground, 1)
    sizes = sorted(
        (len(rows) for rows, _ in _connected_components(np, mask)),
        reverse=True,
    )
    concentration = sum(sizes[:5]) / max(sum(sizes), 1)
    area_penalty = max(0.0, 0.06 - area) * 6 + max(0.0, area - 0.40) * 4
    fragmentation_penalty = max(0, len(sizes) - 20) * 0.012
    return (
            0.6 * balance
            + 1.5 * area
            + 1.5 * neighbor_ratio
            + 0.8 * concentration
            - area_penalty
            - fragmentation_penalty
    )


def _motion_maps(np, frames, lag: int):
    base = frames[:-lag, lag:-lag, lag:-lag]
    maps = []
    for dx, dy in MOVEMENT_DIRECTIONS:
        shifted = np.roll(
            frames[lag:],
            shift=(-dy * lag, -dx * lag),
            axis=(1, 2),
        )[:, lag:-lag, lag:-lag]
        maps.append((base == shifted).mean(axis=0, dtype=np.float32))
    return np.stack(maps)


def _select_motion(np, Image, maps, threshold: float, minimum: int, margin: int):
    edge = ~_text_area(np, maps.shape[1:], margin)
    background = int(np.argmax(maps[:, edge].mean(axis=1)))
    candidates = []
    scoring_threshold = max(threshold + 0.01, 0.035)
    for character in range(len(MOVEMENT_DIRECTIONS)):
        if character == background:
            continue
        score = maps[character] - maps[background]
        mask, _ = _extract_score_mask(
            np, Image, score, scoring_threshold, minimum, margin
        )
        candidates.append((_mask_quality(np, mask, margin), character))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return background, candidates[0][1]


def _recover_segment(np, Image, frames, threshold: float, minimum: int):
    background, character = _select_motion(
        np,
        Image,
        _motion_maps(np, frames, DIRECTION_LAG),
        threshold,
        minimum,
        DIRECTION_LAG,
    )
    fine_maps = _motion_maps(np, frames, MASK_LAG)
    mask_inner, confidence_inner = _extract_score_mask(
        np,
        Image,
        fine_maps[character] - fine_maps[background],
        threshold,
        minimum,
        MASK_LAG,
    )
    word = np.zeros(frames.shape[1:], dtype=bool)
    word[MASK_LAG:-MASK_LAG, MASK_LAG:-MASK_LAG] = mask_inner
    confidence = np.zeros(frames.shape[1:], dtype=np.uint8)
    confidence[MASK_LAG:-MASK_LAG, MASK_LAG:-MASK_LAG] = (
            confidence_inner * 255
    ).astype(np.uint8)
    return word, confidence


def _recover_mask(np, Image, frames, threshold: float, minimum: int):
    recovered = []
    for start in range(0, len(frames), SEGMENT_FRAME_COUNT):
        segment = frames[start:start + SEGMENT_FRAME_COUNT]
        if len(segment) > DIRECTION_LAG:
            recovered.append(
                _recover_segment(np, Image, segment, threshold, minimum)
            )
    if not recovered:
        raise HDHiveCaptchaError("验证码动画帧数不足", code="captcha_invalid_gif")
    words = np.stack([item[0] for item in recovered])
    required_votes = max(1, (len(recovered) + 1) // 2)
    word = words.sum(axis=0) >= required_votes
    confidence = np.max(np.stack([item[1] for item in recovered]), axis=0)
    confidence[~word] = 0
    return word, confidence


def _find_boundaries(np, mask):
    projection = mask[TEXT_TOP:TEXT_BOTTOM].sum(axis=0)
    boundaries = [TEXT_LEFT]
    for expected in SLOT_BOUNDARIES[1:-1]:
        boundary = min(
            range(
                expected - BOUNDARY_SEARCH_RADIUS,
                expected + BOUNDARY_SEARCH_RADIUS + 1,
            ),
            key=lambda column: (
                int(projection[column - 1:column + 1].sum()),
                abs(column - expected),
            ),
        )
        boundaries.append(boundary)
    boundaries.append(TEXT_RIGHT)
    return boundaries


def _split_glyph_masks(np, mask, boundaries):
    centers = np.asarray([
        TEXT_LEFT + SLOT_WIDTH // 2 + index * SLOT_WIDTH
        for index in range(5)
    ])
    glyphs = [np.zeros_like(mask, dtype=bool) for _ in centers]
    for rows, columns in _connected_components(np, mask):
        nearest = int(np.argmin(np.abs(centers - float(columns.mean()))))
        width = int(columns.max() - columns.min() + 1)
        if width <= SLOT_WIDTH + 4:
            glyphs[nearest][rows, columns] = True
            continue
        owners = np.searchsorted(boundaries[1:-1], columns, side="right")
        for index in range(len(glyphs)):
            selected = owners == index
            glyphs[index][rows[selected], columns[selected]] = True
    return glyphs


def _hog_feature(np, image):
    gradient_y, gradient_x = np.gradient(image)
    magnitude = np.hypot(gradient_x, gradient_y)
    angles = (np.arctan2(gradient_y, gradient_x) % np.pi) * (9 / np.pi)
    histograms = np.zeros((7, 4, 9), dtype=np.float32)
    for cell_y in range(7):
        for cell_x in range(4):
            cell_angles = angles[
                cell_y * 8:(cell_y + 1) * 8,
                cell_x * 8:(cell_x + 1) * 8,
            ].ravel()
            cell_magnitudes = magnitude[
                cell_y * 8:(cell_y + 1) * 8,
                cell_x * 8:(cell_x + 1) * 8,
            ].ravel()
            bins = np.floor(cell_angles).astype(int) % 9
            histograms[cell_y, cell_x] = np.bincount(
                bins,
                weights=cell_magnitudes,
                minlength=9,
            )
    blocks = []
    for block_y in range(6):
        for block_x in range(3):
            block = histograms[
                block_y:block_y + 2,
                block_x:block_x + 2,
            ].ravel()
            blocks.append(block / (np.linalg.norm(block) + 1e-6))
    return np.concatenate(blocks)


class HDHiveCaptchaRecognizer:
    """延迟加载本地模型并识别五字符动态验证码。"""

    def __init__(self, model_path: Optional[Path] = None):
        self._model_path = model_path or Path(__file__).with_name("captcha.bin")
        self._bundle = None
        self._lock = threading.RLock()

    def _model(self):
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            if not self._model_path.is_file():
                raise HDHiveCaptchaError(
                    f"HDHive 验证码模型不存在：{self._model_path}",
                    code="captcha_model_missing",
                )
            logger.debug(f"HDHive 验证码模型加载：{self._model_path.name}")
            np, _, _ = _dependencies()
            try:
                with np.load(self._model_path, allow_pickle=False) as model:
                    if int(model["format_version"].item()) != 1:
                        raise ValueError("不支持的模型格式")
                    self._bundle = {
                        "threshold": float(model["threshold"].item()),
                        "min_component": int(model["min_component"].item()),
                        "mean": model["mean"].astype(np.float32),
                        "scale": model["scale"].astype(np.float32),
                        "coef": model["coef"].astype(np.float32),
                        "intercept": model["intercept"].astype(np.float32),
                        "classes": model["classes"].astype("<U1"),
                    }
                logger.info(
                    "HDHive 验证码模型加载完成："
                    f"大小 {self._model_path.stat().st_size}，"
                    f"类别 {len(self._bundle['classes'])}，"
                    f"阈值 {self._bundle['threshold']:.4f}"
                )
            except Exception as error:
                raise HDHiveCaptchaError(
                    f"HDHive 验证码模型加载失败：{error}",
                    code="captcha_model_invalid",
                ) from error
            return self._bundle

    def ensure_ready(self) -> None:
        """在发起验证码后续请求前完成模型存在性与格式检查。"""
        self._model()

    def recognize(self, gif: bytes) -> tuple[str, float]:
        bundle = self._model()
        np, Image, ImageSequence = _dependencies()
        try:
            with Image.open(BytesIO(gif)) as image:
                frames = np.stack([
                    np.asarray(frame.convert("L"), dtype=np.uint8) > 128
                    for frame in ImageSequence.Iterator(image)
                ])
        except Exception as error:
            raise HDHiveCaptchaError(
                f"HDHive 验证码 GIF 解析失败：{error}",
                code="captcha_invalid_gif",
            ) from error
        if frames.shape[1:] != (132, 320):
            raise HDHiveCaptchaError(
                f"HDHive 验证码尺寸无效：{frames.shape[1:]}",
                code="captcha_invalid_gif",
            )
        logger.debug(
            f"HDHive 验证码 GIF 解析：帧数 {frames.shape[0]}，尺寸 {frames.shape[1]}x{frames.shape[2]}"
        )
        word, confidence = _recover_mask(
            np,
            Image,
            frames,
            float(bundle.get("threshold") or 0.025),
            int(bundle.get("min_component") or 18),
        )
        boundaries = _find_boundaries(np, word)
        glyph_masks = _split_glyph_masks(np, word, boundaries)
        images = []
        for index, glyph_mask in enumerate(glyph_masks):
            center = TEXT_LEFT + SLOT_WIDTH // 2 + index * SLOT_WIDTH
            filtered = np.where(glyph_mask, confidence, 0)
            crop = Image.fromarray(
                filtered[10:122, center - 32:center + 32]
            ).resize((32, 56), Image.Resampling.BILINEAR)
            images.append(np.asarray(crop, dtype=np.float32) / 255)
        features = np.stack([_hog_feature(np, image) for image in images])
        normalized = (features - bundle["mean"]) / bundle["scale"]
        decisions = normalized @ bundle["coef"].T + bundle["intercept"]
        prediction = bundle["classes"][np.argmax(decisions, axis=1)]
        ordered = np.sort(decisions, axis=1)
        answer = "".join(prediction)
        if len(answer) != 5 or any(character not in ALPHABET for character in answer):
            raise HDHiveCaptchaError(
                "HDHive 验证码模型输出无效",
                code="captcha_prediction_invalid",
            )
        confidence = float((ordered[:, -1] - ordered[:, -2]).min())
        logger.info(
            f"HDHive 验证码识别完成：字符数 {len(answer)}，最小决策间隔 {confidence:.4f}"
        )
        return answer, confidence


class HDHiveCaptchaSolver:
    """处理 security-check 重定向、识别、刷新与提交。"""

    def __init__(
            self,
            request: Callable[..., Any],
            server_actions: ServerActionProtocol,
            recognizer: Optional[HDHiveCaptchaRecognizer] = None,
    ):
        self._request = request
        self._server_actions = server_actions
        self._recognizer = recognizer or HDHiveCaptchaRecognizer()
        self._action_cache: dict[str, dict[str, str]] = {}

    @classmethod
    def challenge_url(cls, response) -> str:
        response_url = str(getattr(response, "url", "") or "")
        candidate = cls._valid_challenge_url(response_url)
        if candidate:
            return candidate
        if int(getattr(response, "status_code", 0) or 0) == 428:
            try:
                payload = response.json()
            except (AttributeError, ValueError):
                payload = {}
            data = payload.get("data") if isinstance(payload, dict) else None
            error = payload.get("error") if isinstance(payload, dict) else None
            error_data = error.get("data") if isinstance(error, dict) else None
            challenge_id = str(
                (data or {}).get("challenge_id")
                if isinstance(data, dict)
                else ""
            ).strip() or str(
                (error_data or {}).get("challenge_id")
                if isinstance(error_data, dict)
                else ""
            ).strip()
            error_code = str(
                payload.get("code")
                or payload.get("error_code")
                or (error or {}).get("code")
                if isinstance(payload, dict)
                else ""
            ).strip()
            if (
                    error_code == "ABUSE_CHALLENGE_REQUIRED"
                    and UUID_RE.fullmatch(challenge_id)
            ):
                return f"{BASE_URL}{CHALLENGE_PATH}?challenge={challenge_id}"
        normalized = decode_embedded_text(response_text(response))
        redirect = NEXT_REDIRECT_RE.search(normalized)
        if redirect:
            candidate = cls._valid_challenge_url(
                urljoin(f"{BASE_URL}/", redirect.group(1).lstrip("/"))
            )
            if candidate:
                return candidate
        for matched in re.findall(
                r"/security-check\?challenge=[0-9a-fA-F-]{36}[^\"'<> ]*",
                normalized,
        ):
            candidate = cls._valid_challenge_url(urljoin(BASE_URL, matched))
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _valid_challenge_url(url: str) -> str:
        parsed = urlsplit(str(url or ""))
        challenge_id = parse_qs(parsed.query).get("challenge", [""])[0]
        if (
                parsed.scheme == "https"
                and parsed.hostname == "re0.me"
                and parsed.path == CHALLENGE_PATH
                and UUID_RE.fullmatch(challenge_id)
        ):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        return ""

    @classmethod
    def is_challenge_response(cls, response) -> bool:
        return bool(cls.challenge_url(response))

    @staticmethod
    def _safe_path(value: str, fallback: str = "/") -> str:
        resolved = urlsplit(urljoin(f"{BASE_URL}/", str(value or "").lstrip("/")))
        if resolved.scheme != "https" or resolved.hostname != "re0.me":
            return fallback
        path = resolved.path or "/"
        return f"{path}?{resolved.query}" if resolved.query else path

    @staticmethod
    def _relative_url(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    @staticmethod
    def _extract_gif(payload: bytes) -> bytes:
        normalized = decode_embedded_text(
            payload.decode("utf-8", errors="replace")
        )
        match = GIF_RE.search(normalized)
        if not match:
            raise HDHiveCaptchaError(
                "HDHive 安全页未返回动态验证码",
                code="captcha_image_missing",
            )
        import base64
        encoded = match.group(1)
        encoded += "=" * (-len(encoded) % 4)
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise HDHiveCaptchaError(
                "HDHive 动态验证码 Base64 无效",
                code="captcha_invalid_gif",
            ) from error
        if not image.startswith((b"GIF87a", b"GIF89a")):
            raise HDHiveCaptchaError(
                "HDHive 动态验证码格式无效",
                code="captcha_invalid_gif",
            )
        return image

    def _load_challenge(
            self,
            challenge_url: str,
            requested_path: str,
            response=None,
    ) -> CaptchaChallenge:
        payload = response_body(response) if response is not None else b""
        page_text = decode_embedded_text(
            payload.decode("utf-8", errors="ignore")
        ) if payload else ""
        if not payload or not GIF_RE.search(page_text):
            response = self._request(
                "GET",
                self._relative_url(challenge_url),
                headers={"accept": "text/html,application/xhtml+xml"},
            )
            payload = response_body(response)
            challenge_url = self.challenge_url(response) or challenge_url
        parsed = urlsplit(challenge_url)
        challenge_id = parse_qs(parsed.query).get("challenge", [""])[0]
        if not UUID_RE.fullmatch(challenge_id):
            raise HDHiveCaptchaError(
                "HDHive security challenge 缺少有效 ID",
                code="captcha_schema_changed",
            )
        requested = self._safe_path(requested_path, fallback="/")
        return_value = parse_qs(parsed.query).get("return", [requested])[0]
        return_to = requested if requested != "/" else self._safe_path(return_value)
        self._extract_gif(payload)
        logger.debug(
            f"HDHive 验证码挑战页加载：请求路径 {urlsplit(requested).path or '/'}，"
            f"页面字节数 {len(payload)}"
        )
        return CaptchaChallenge(challenge_id, challenge_url, return_to, payload)

    def _actions(self, challenge: CaptchaChallenge) -> dict[str, str]:
        text = decode_embedded_text(
            challenge.page.decode("utf-8", errors="replace")
        )
        chunk_match = PAGE_CHUNK_RE.search(text)
        if not chunk_match:
            raise HDHiveCaptchaError(
                "HDHive 安全页客户端模块未找到",
                code="captcha_schema_changed",
            )
        chunk = chunk_match.group(0)
        if chunk in self._action_cache:
            return self._action_cache[chunk]
        response = self._request("GET", f"/_next/{chunk}")
        javascript = response_text(response)
        actions = {name: action_id for action_id, name in ACTION_RE.findall(javascript)}
        required = {"refreshAbuseChallenge", "verifyAbuseChallenge"}
        if not required.issubset(actions):
            raise HDHiveCaptchaError(
                "HDHive 验证码 Server Action 未找到",
                code="captcha_schema_changed",
            )
        logger.debug("HDHive 验证码 Server Action 已解析")
        self._action_cache[chunk] = actions
        return actions

    @staticmethod
    def _result(response: ServerActionResponse) -> ActionResult:
        if response.payload:
            data = response.data
            remaining = data.get("remaining_attempts")
            return ActionResult(
                success=response.success,
                code=response.code,
                message=response.message,
                remaining_attempts=(
                    int(remaining) if isinstance(remaining, (int, float)) else None
                ),
                clearance_seconds=int(data.get("clearance_seconds") or 0),
            )
        text = response.text
        code_match = re.search(r'"code"\s*:\s*"([A-Z0-9_]+)"', text)
        if re.search(r'"success"\s*:\s*true', text, re.I):
            return ActionResult(success=True)
        return ActionResult(
            success=False,
            code=code_match.group(1) if code_match else "",
            message="HDHive 验证码响应格式无法识别",
        )

    def _post_action(
            self,
            challenge: CaptchaChallenge,
            action: str,
            arguments: list[str],
    ) -> tuple[ActionResult, bytes]:
        response = self._server_actions.post(
            self._request,
            self._relative_url(challenge.url),
            action,
            arguments,
            referer=challenge.url,
            router_state=ROUTER_STATE,
            next_url=challenge.return_to,
        )
        if response.status_code >= 400:
            raise HDHiveCaptchaError(
                f"HDHive 验证码提交失败（HTTP {response.status_code}）",
                code="captcha_request_failed",
            )
        return self._result(response), response.body

    def _refresh(self, challenge: CaptchaChallenge) -> CaptchaChallenge:
        actions = self._actions(challenge)
        result, payload = self._post_action(
            challenge,
            actions["refreshAbuseChallenge"],
            [challenge.challenge_id],
        )
        logger.debug(
            f"HDHive 验证码刷新结果：success={result.success}，code={result.code or '-'}"
        )
        if result.code == "ABUSE_CHALLENGE_EXPIRED":
            raise HDHiveCaptchaError("HDHive 验证码已过期", code=result.code)
        if not result.success:
            raise HDHiveCaptchaError(
                result.message or "HDHive 验证码刷新失败",
                code=result.code or "captcha_refresh_failed",
            )
        image = self._extract_gif(payload)
        if hashlib.sha256(image).digest() == hashlib.sha256(
                self._extract_gif(challenge.page)
        ).digest():
            raise HDHiveCaptchaError(
                "HDHive 验证码刷新后未变化",
                code="captcha_refresh_unchanged",
            )
        return CaptchaChallenge(
            challenge.challenge_id,
            challenge.url,
            challenge.return_to,
            payload,
        )

    def _trigger(self, return_to: str) -> Optional[CaptchaChallenge]:
        response = self._request(
            "GET",
            return_to,
            headers={
                "accept": "text/x-component",
                "rsc": "1",
                "next-url": urlsplit(return_to).path or "/",
            },
        )
        challenge_url = self.challenge_url(response)
        if not challenge_url:
            return None
        return self._load_challenge(challenge_url, return_to, response)

    def solve(self, response, requested_path: str) -> int:
        challenge_url = self.challenge_url(response)
        if not challenge_url:
            raise HDHiveCaptchaError(
                "响应不是 HDHive security challenge",
                code="captcha_not_found",
            )
        requested = self._safe_path(requested_path)
        logger.info(
            f"HDHive 检测到验证码挑战：请求路径 {urlsplit(requested).path or '/'}"
        )
        self._recognizer.ensure_ready()
        challenge = self._load_challenge(challenge_url, requested_path, response)
        for attempt in range(MAX_VERIFY_ATTEMPTS):
            image = self._extract_gif(challenge.page)
            answer, _ = self._recognizer.recognize(image)
            logger.debug(
                f"HDHive 验证码提交尝试：第 {attempt + 1}/{MAX_VERIFY_ATTEMPTS} 次"
            )
            actions = self._actions(challenge)
            result, _ = self._post_action(
                challenge,
                actions["verifyAbuseChallenge"],
                [challenge.challenge_id, answer],
            )
            logger.debug(
                f"HDHive 验证码校验结果：第 {attempt + 1} 次，"
                f"success={result.success}，code={result.code or '-'}，"
                f"剩余次数={result.remaining_attempts if result.remaining_attempts is not None else '-'}"
            )
            if result.success:
                return result.clearance_seconds
            if result.code == "ABUSE_CHALLENGE_EXPIRED":
                replacement = self._trigger(challenge.return_to)
                if replacement is None:
                    return 0
                challenge = replacement
                continue
            if result.remaining_attempts == 0:
                raise HDHiveCaptchaError(
                    result.message or "HDHive 验证码尝试次数已用尽",
                    code=result.code or "captcha_attempts_exhausted",
                )
            if attempt + 1 >= MAX_VERIFY_ATTEMPTS:
                break
            try:
                challenge = self._refresh(challenge)
            except HDHiveCaptchaError as error:
                if error.code != "ABUSE_CHALLENGE_EXPIRED":
                    raise
                replacement = self._trigger(challenge.return_to)
                if replacement is None:
                    return 0
                challenge = replacement
        raise HDHiveCaptchaError(
            "HDHive 验证码连续识别失败",
            code="captcha_verify_failed",
        )
