"""HDHive 网页安全握手与请求签名协议。"""

import base64
import binascii
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from contextlib import suppress
from functools import lru_cache
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Server Action 短期令牌 Cookie，仅在一次动作内有效，不随会话持久化。
ACTION_TOKEN_COOKIE = "hdh_sa_token"
_BIND_SECRET_RE = re.compile(r'[\\"]bindSecret[\\"]\s*:\s*[\\"]([^\\"]+)')
# Next.js Flight 数据常用的转义序列，令牌提取前需还原。
_EMBEDDED_ESCAPE_RE = re.compile(
    r"\\+(u0022|u005c|u003a|u002f|u0026|u003d|u003f|u002b|/|[\"'])", re.I
)
_EMBEDDED_ESCAPE_VALUES = {
    "u0022": '"', "u005c": "\\",
    "u003a": ":", "u002f": "/", "u0026": "&", "u003d": "=",
    "u003f": "?", "u002b": "+", "/": "/", '"': '"', "'": "'",
}
# 站点前端 action-proof 使用的置换表即 AES S 盒。
_ACTION_PROOF_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)


def payload_error_message(payload: Any) -> str:
    """从 JSON 响应体提取服务端错误消息（兼容 error.message 与 message）。"""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(payload.get("message") or "")


def is_risk_message(message: str) -> bool:
    """判断错误消息是否属于触发风控冷却的人机验证类提示。"""
    return any(marker in message for marker in (
        "高频", "人机验证", "安全验证", "访问频繁", "操作频繁",
    ))


def needs_action_proof(response) -> bool:
    """识别服务端要求或续期 Action Proof 的错误响应。"""
    if int(getattr(response, "status_code", 0) or 0) < 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("code") or payload.get("error_code") or "")
    description = " ".join(str(payload.get(key) or "") for key in (
        "description", "message",
    ))
    return (
            code in {
        "action_proof_required", "action_proof_expired",
        "action_proof_challenge_required",
    }
            or "expired_challenge" in description
            or "proof" in code.lower()
    )


def is_persistent_cookie(name: str) -> bool:
    """判断 Cookie 是否应随会话持久化（排除短期 Action 令牌）。"""
    return str(name or "") != ACTION_TOKEN_COOKIE


def _decoded_page_text(text: str) -> str:
    """还原 Flight 数据中被转义的冒号、斜杠与引号。"""
    decoded = str(text or "")
    for _ in range(4):
        current = html.unescape(unquote(decoded))
        current = _EMBEDDED_ESCAPE_RE.sub(
            lambda match: _EMBEDDED_ESCAPE_VALUES[match.group(1).lower()],
            current,
        )
        if current == decoded:
            break
        decoded = current
    return decoded


def extract_bind_secret(text: str) -> str:
    """从登录响应文本提取安全绑定密钥；缺失时返回空。"""
    match = _BIND_SECRET_RE.search(_decoded_page_text(text))
    return match.group(1).strip() if match else ""


def _rotl8(value: int, shift: int) -> int:
    rotation = shift % 8
    return ((value << rotation) | (value >> (8 - rotation))) & 0xFF


def _sha256_bytes(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


class ActionProofTables:
    """由 table_version/profile/profile_salt 派生的证明密钥表 """

    def __init__(self, table_version: str, profile: str, profile_salt: str):
        self.table_version = str(table_version or "").strip()
        self.profile = str(profile or "").strip()
        self.profile_salt = str(profile_salt or "").strip()
        identity = f"{self.table_version}:{self.profile}:{self.profile_salt}"
        self.state = self._prefix(identity, "state")
        self.iv = self._prefix(identity, "iv")
        self.seed_mask = self._prefix(identity, "seed")
        self.final_in = self._prefix(identity, "final-in")
        self.final_out = self._prefix(identity, "final-out")
        self.round_keys = [
            self._prefix(identity, f"round-key:{index:02d}")
            for index in range(12)
        ]
        self.permutations = [
            self._permutation(identity, index) for index in range(12)
        ]
        self.word_tables = [
            [self._word(identity, table, entry) for entry in range(256)]
            for table in range(4)
        ]

    @staticmethod
    def _prefix(identity: str, suffix: str) -> bytes:
        return _sha256_bytes(f"{identity}:{suffix}")[:16]

    @staticmethod
    def _word(identity: str, table: int, entry: int) -> int:
        digest = _sha256_bytes(f"{identity}:word:{table}:{entry}")
        return (
                (digest[0] << 24) | (digest[1] << 16)
                | (digest[2] << 8) | digest[3]
        )

    @classmethod
    def _permutation(cls, identity: str, index: int) -> bytes:
        order = bytearray(range(16))
        seed = cls._prefix(identity, f"perm:{index:02d}")
        for position in range(16):
            swap_at = seed[position] % 16
            order[position], order[swap_at] = order[swap_at], order[position]
        return bytes(order)


class ActionProofManifestError(ValueError):
    """动态 Action Proof 表清单无效。"""


@lru_cache(maxsize=16)
def validate_action_proof_manifest(
        manifest_json: str,
        expected_table_version: str = "",
) -> Dict[str, Any]:
    """校验网页端 manifest，返回不可变输入对应的清单对象 """

    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as error:
        raise ActionProofManifestError("Action Proof manifest JSON 无效") from error
    if not isinstance(manifest, dict):
        raise ActionProofManifestError("Action Proof manifest 必须是对象")
    table_version = str(manifest.get("table_version") or "").strip()
    algorithm_version = str(manifest.get("algorithm_version") or "").strip()
    if not table_version or not algorithm_version:
        raise ActionProofManifestError("Action Proof manifest 缺少版本字段")
    if expected_table_version and table_version != str(expected_table_version).strip():
        raise ActionProofManifestError(
            "Action Proof manifest table_version 与 challenge 不一致"
        )
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise ActionProofManifestError("Action Proof manifest 缺少 shards")
    required = {"ctx398", "seed_params", "round_schema", "round_group", "final_affine"}
    kinds = set()
    total_size = 0
    normalized = []
    for shard in shards:
        if not isinstance(shard, dict):
            raise ActionProofManifestError("Action Proof manifest shard 无效")
        name = str(shard.get("name") or "").strip()
        path = str(shard.get("path") or "").strip()
        digest = str(shard.get("hash") or "").strip().lower()
        kind = str(shard.get("kind") or "").strip()
        size = shard.get("size")
        if (
                not name or not path or path.startswith("/")
                or ".." in path or "\\" in path
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(size, (int, float))
                or not float(size).is_integer() or int(size) <= 0
        ):
            raise ActionProofManifestError("Action Proof manifest shard 参数无效")
        total_size += int(size)
        kinds.add(kind)
        normalized.append({"name": name, "path": path, "hash": digest, "kind": kind, "size": int(size)})
    if total_size > 1_500_000:
        raise ActionProofManifestError("Action Proof manifest 超过大小限制")
    missing = required - kinds
    if missing:
        raise ActionProofManifestError(
            f"Action Proof manifest 缺少分片类型：{','.join(sorted(missing))}"
        )
    return {
        "table_version": table_version,
        "algorithm_version": algorithm_version,
        "profile": str(manifest.get("profile") or "").strip(),
        "shards": normalized,
    }


def action_proof_canonical(
        challenge: Dict[str, Any],
        *,
        request_nonce: str,
        session_hint: str,
        client_caps: str,
        body_hash: str,
) -> str:
    """构造证明明文（与站点前端逐字段一致，超过 1024 字节视为异常）。"""
    fields = {
        "v": str(challenge.get("algorithm_version") or "").strip(),
        "cid": str(challenge.get("challenge_id") or "").strip(),
        "sn": str(challenge.get("server_nonce") or "").strip(),
        "uid": str(int(challenge.get("user_id") or 0)),
        "act": str(challenge.get("action") or "").strip(),
        "m": str(challenge.get("method") or "").strip().upper(),
        "p": str(challenge.get("path_template") or "").strip(),
        "slug": str(challenge.get("resource_slug") or "").strip(),
        "bh": str(body_hash or ""),
        "rh": str(request_nonce or "").strip(),
        "sh": str(session_hint or ""),
        "cc": str(client_caps or ""),
        "iat": str(int(challenge.get("issued_at") or 0)),
        "exp": str(int(challenge.get("expires_at") or 0)),
        "tv": str(challenge.get("table_version") or "").strip(),
        "pf": str(challenge.get("profile") or "").strip(),
        "ps": str(challenge.get("profile_salt") or "").strip(),
    }
    canonical = "&".join(f"{key}={value}" for key, value in fields.items())
    if len(canonical.encode("utf-8")) > 1024:
        raise ValueError("action proof canonical plain exceeds 1024 bytes")
    return canonical


def compute_action_proof(
        challenge: Dict[str, Any],
        *,
        request_nonce: str,
        session_hint: str = "",
        client_caps: str = "",
        body_hash: str = "",
) -> str:
    """计算 X-Action-Proof 头的十六进制证明值。"""
    plain = action_proof_canonical(
        challenge,
        request_nonce=request_nonce,
        session_hint=session_hint,
        client_caps=client_caps,
        body_hash=body_hash,
    )
    tables = ActionProofTables(
        challenge.get("table_version"),
        challenge.get("profile"),
        challenge.get("profile_salt"),
    )
    data = plain.encode("utf-8")
    padding = 16 - len(data) % 16
    padded = data + bytes([padding]) * padding
    transformed = bytearray(len(padded))
    state = tables.state
    for offset in range(0, len(padded), 4):
        word = (
                tables.word_tables[0][padded[offset]]
                ^ tables.word_tables[1][padded[offset + 1]]
                ^ tables.word_tables[2][padded[offset + 2]]
                ^ tables.word_tables[3][padded[offset + 3]]
        )
        transformed[offset] = (word >> 24) & 0xFF ^ state[offset % len(state)]
        transformed[offset + 1] = (
                (word >> 16) & 0xFF ^ state[(offset + 1) % len(state)]
        )
        transformed[offset + 2] = (
                (word >> 8) & 0xFF ^ state[(offset + 2) % len(state)]
        )
        transformed[offset + 3] = (
                word & 0xFF ^ state[(offset + 3) % len(state)]
        )

    sbox = _ACTION_PROOF_SBOX
    proof = bytearray()
    previous = tables.iv
    for start in range(0, len(transformed), 16):
        block = transformed[start:start + 16]
        current = bytearray(16)
        for position in range(16):
            mixed = (
                    block[position] ^ previous[position]
                    ^ tables.seed_mask[position]
            )
            current[position] = sbox[(mixed ^ 17 * position) & 0xFF]
        chained = 0
        for round_index in range(12):
            round_key = tables.round_keys[round_index]
            permutation = tables.permutations[round_index]
            nxt = bytearray(16)
            for position in range(16):
                chained = (chained ^ current[position]) & 0xFF
                mixed = (
                                current[permutation[position]]
                                ^ round_key[position]
                                ^ chained
                                ^ ((13 * round_index + position) & 0xFF)
                        ) & 0xFF
                nxt[position] = (
                                        sbox[mixed]
                                        ^ _rotl8(mixed, (round_index + position) % 7 + 1)
                                ) & 0xFF
            current = nxt
        output = bytearray(16)
        for position in range(16):
            first = sbox[_rotl8(
                current[position] ^ tables.final_in[position],
                tables.final_in[position] % 7 + 1,
            )]
            output[position] = _rotl8(
                first ^ tables.final_out[position],
                tables.final_out[position] % 5 + 1,
            )
        proof.extend(output)
        previous = output
    return proof.hex()


class HDHiveSecurityProtocol:
    """等价实现站点 hdh/v1 安全模块，不依赖 WASM 运行时。"""

    KID = "1"
    INFO = b"hdh/v1"
    POW_DIFFICULTY_BITS = 16
    POW_MAX_ITERATIONS = 0x7FFFFFFF
    SESSION_RETRY_CODES = frozenset({
        "invalid_session",
        "missing_signature",
        "signature_invalid",
        "session_user_mismatch",
    })
    SIGNED_RESPONSE_PATHS = frozenset({
        "/api/customer/user/current",
        "/api/customer/points-logs",
    })

    def __init__(self):
        self._private_key: Optional[X25519PrivateKey] = None
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._encryption_key = b""
        self._expires_at = 0.0
        self._clock_offset_ms = 0

    @property
    def cid(self) -> str:
        return self._cid

    def ready(self, margin_seconds: int = 60) -> bool:
        return bool(
            self._cid
            and self._request_key
            and self._expires_at - max(0, int(margin_seconds or 0)) > time.time()
        )

    def invalidate(self) -> None:
        self._private_key = None
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._encryption_key = b""
        self._expires_at = 0.0

    def begin_handshake(self) -> bytes:
        self._private_key = X25519PrivateKey.generate()
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._encryption_key = b""
        self._expires_at = 0.0
        return self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )

    def handshake_body(
            self,
            user_agent: str,
            languages: str,
            bind_token: str,
    ) -> bytes:
        public_key = self.begin_handshake()
        fingerprint = hashlib.sha256(
            f"{user_agent}|{languages}".encode("utf-8")
        ).hexdigest()
        client_pub = base64.b64encode(public_key).decode("ascii")
        timestamp = self.timestamp_ms()
        return json.dumps({
            "client_pub": client_pub,
            "ua_fingerprint": fingerprint,
            "ts": timestamp,
            "bind_token": str(bind_token or ""),
            "pow_nonce": self.solve_pow_nonce(client_pub, timestamp),
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _base36(value: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        if value == 0:
            return "0"
        result = []
        while value:
            value, remainder = divmod(value, 36)
            result.append(digits[remainder])
        return "".join(reversed(result))

    @classmethod
    def _pow_meets_difficulty(cls, digest: str, bits: int) -> bool:
        remaining = bits
        position = 0
        while remaining >= 4:
            if digest[position] != "0":
                return False
            position += 1
            remaining -= 4
        # 难度不是 4 的倍数时，仅要求下一个十六进制位的高位比特为 0。
        if remaining > 0:
            return int(digest[position], 16) >> (4 - remaining) == 0
        return True

    @classmethod
    def solve_pow_nonce(cls, client_pub: str, timestamp_ms: int) -> str:
        prefix = f"{client_pub}:{timestamp_ms}:".encode("ascii")
        for counter in range(cls.POW_MAX_ITERATIONS):
            nonce = cls._base36(counter)
            digest = hashlib.sha256(prefix + nonce.encode("ascii")).hexdigest()
            if cls._pow_meets_difficulty(digest, cls.POW_DIFFICULTY_BITS):
                return nonce
        raise ValueError("HDHive 安全握手工作量证明求解失败")

    def finalize_handshake(self, cid: str, server_public_key: bytes) -> None:
        if not self._private_key:
            raise ValueError("HDHive 安全握手尚未开始")
        normalized_cid = str(cid or "").strip()
        if not normalized_cid:
            raise ValueError("HDHive 安全握手缺少 cid")
        if len(server_public_key) != 32:
            raise ValueError("HDHive 服务端公钥长度无效")
        shared_secret = self._private_key.exchange(
            X25519PublicKey.from_public_bytes(server_public_key)
        )
        key_material = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=normalized_cid.encode("utf-8"),
            info=self.INFO,
        ).derive(shared_secret)
        self._cid = normalized_cid
        self._request_key = key_material[32:]
        self._response_key = self._request_key
        self._encryption_key = key_material[:32]

    def accept_handshake(self, data: Dict[str, Any]) -> None:
        server_public_key = base64.b64decode(str(data.get("server_pub") or ""))
        self.finalize_handshake(str(data.get("cid") or ""), server_public_key)
        self._expires_at = float(data.get("expires_at") or 0)

    def sync_time(self, server_time_ms: Any) -> None:
        self._clock_offset_ms = int(server_time_ms) - int(time.time() * 1000)

    def timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    def request_headers(
            self,
            method: str,
            path: str,
            body: bytes,
            user_id: str,
    ) -> Dict[str, str]:
        timestamp = str(self.timestamp_ms())
        nonce = self.nonce()
        return {
            "X-HDH-Cid": self.cid,
            "X-HDH-TS": timestamp,
            "X-HDH-Nonce": nonce,
            "X-HDH-Sig": self.sign_request(
                method, path, timestamp, nonce, body, user_id
            ),
            "X-HDH-Kid": self.KID,
        }

    @classmethod
    def is_unlock_path(cls, path: str) -> bool:
        normalized = urlsplit(path).path
        return bool(re.fullmatch(
            r"/api/customer/(?:resources|music_resources)/[^/]+/unlock",
            normalized,
        ) or re.fullmatch(
            r"/api/customer/tv-follow/packs/[^/]+/unlock", normalized
        ))

    @classmethod
    def requires_signed_response(cls, path: str) -> bool:
        normalized = urlsplit(path).path
        return (
                normalized in cls.SIGNED_RESPONSE_PATHS
                or cls.is_unlock_path(normalized)
        )

    @staticmethod
    def response_error_code(response: Any) -> str:
        if int(getattr(response, "status_code", 0) or 0) != 401:
            return ""
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("code") or payload.get("error_code") or "")

    @classmethod
    def retry_action(cls, error_code: str) -> str:
        if error_code in cls.SESSION_RETRY_CODES:
            return "handshake"
        if error_code == "stale_ts":
            return "clock"
        if error_code == "replay":
            return "retry"
        return ""

    def sign_request(
            self,
            method: str,
            path: str,
            timestamp: str,
            nonce: str,
            body: bytes,
            user_id: str,
    ) -> str:
        if not self._request_key or not self._cid:
            raise ValueError("HDHive 安全会话未就绪")
        canonical = "\n".join((
            str(method or "GET").upper(),
            str(path or "/"),
            str(timestamp or ""),
            str(nonce or ""),
            hashlib.sha256(body or b"").hexdigest(),
            self._cid,
            str(user_id or "0"),
            self.KID,
        )).encode("utf-8")
        return hmac.new(self._request_key, canonical, hashlib.sha256).hexdigest()

    def verify_response(
            self,
            path: str,
            status_code: int,
            response_timestamp: str,
            body: bytes,
            signature: str,
    ) -> bool:
        if not self._response_key:
            return False
        canonical = "|".join((
            str(path or "/"),
            str(int(status_code or 0)),
            str(response_timestamp or ""),
            hashlib.sha256(body or b"").hexdigest(),
        )).encode("utf-8")
        expected = hmac.new(
            self._response_key, canonical, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, str(signature or ""))

    def decrypt_response(self, payload: bytes) -> bytes:
        """解密 X-HDH-Enc 响应体：base64(nonce12 ‖ 密文 ‖ GCM tag16)。"""
        if not self._encryption_key:
            raise ValueError("HDHive 安全会话未就绪")
        try:
            encrypted = base64.b64decode(
                bytes(payload or b"").strip(), validate=False
            )
        except (binascii.Error, ValueError) as error:
            raise ValueError("加密响应体 base64 解析失败") from error
        # 12 字节 nonce 加至少 16 字节 GCM tag。
        if len(encrypted) < 28:
            raise ValueError("加密响应体长度非法")
        try:
            return AESGCM(self._encryption_key).decrypt(
                encrypted[:12], encrypted[12:], None
            )
        except InvalidTag as error:
            raise ValueError("加密响应体 GCM 校验失败") from error

    @staticmethod
    def nonce() -> str:
        return secrets.token_hex(16)


class HDHiveSecuritySession:
    """安全会话生命周期管理：握手、时钟同步、响应解密与签名校验。"""

    TIME_PATH = "/api/public/security/time"
    HANDSHAKE_PATH = "/api/public/security/session/handshake"
    ACTION_PROOF_PATH = "/api/customer/action-proof/challenge"
    ACTION_PROOF_TABLES_PATH = "/action-proof-tables"

    def __init__(
            self,
            protocol: HDHiveSecurityProtocol,
            transport: Callable[..., Any],
            error_factory: Callable[..., Exception],
            profile: Callable[[], Tuple[str, str, str]],
            body_reader: Callable[[Any], bytes],
            base_url: str,
            signed_transport: Optional[Callable[..., Any]] = None,
    ):
        self._protocol = protocol
        self._transport = transport
        self._error = error_factory
        self._profile = profile
        self._body_reader = body_reader
        self._base_url = str(base_url or "").rstrip("/")
        self._signed_transport = signed_transport
        self._action_proof_manifests: Dict[str, Dict[str, Any]] = {}

    def ensure(self, force: bool = False) -> None:
        """确保安全会话可用；过期或强制时重新握手。"""
        if not force and self._protocol.ready():
            return
        user_agent, languages, bind_secret = self._profile()
        body = self._protocol.handshake_body(
            user_agent, languages, bind_secret
        )
        response = self._transport(
            "POST",
            self.HANDSHAKE_PATH,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": self._base_url,
                "referer": f"{self._base_url}/",
            },
            data=body,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise self._error(
                "HDHive 安全握手响应格式异常", code="handshake_invalid"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code >= 400 or not isinstance(data, dict):
            message = payload_error_message(payload)
            raise self._error(
                f"HDHive 安全握手失败："
                f"{message or f'HTTP {response.status_code}'}",
                code="handshake_failed",
                status_code=response.status_code,
            )
        try:
            self._protocol.accept_handshake(data)
        except (TypeError, ValueError) as error:
            raise self._error(
                f"HDHive 安全握手参数无效：{error}", code="handshake_invalid"
            ) from error

    def sync_time(self) -> None:
        """按服务端时间校准本地时钟偏移。"""
        response = self._transport("GET", self.TIME_PATH)
        try:
            server_time = int(
                (response.json().get("data") or {}).get("server_time_ms")
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise self._error(
                "HDHive 服务端时间响应无效", code="clock_sync_failed"
            ) from error
        self._protocol.sync_time(server_time)

    def prepare_retry(self, error_code: str) -> bool:
        """按安全层错误码执行恢复动作；返回是否值得重试请求。"""
        action = self._protocol.retry_action(error_code)
        if action == "handshake":
            self.ensure(force=True)
            return True
        if action == "clock":
            self.sync_time()
            return True
        return action == "retry"

    def decode_response(self, response):
        """按 X-HDH-Enc 约定解密响应体；服务端对明文签名，需先解密再验签。"""
        if not str(response.headers.get("X-HDH-Enc") or ""):
            return response
        try:
            decrypted = self._protocol.decrypt_response(
                self._body_reader(response)
            )
        except ValueError as error:
            raise self._error(
                f"HDHive 加密响应解密失败：{error}",
                code="response_decrypt_failed",
                status_code=response.status_code,
            ) from error
        try:
            # curl_cffi 的 content 是普通属性，覆盖响应体时同步内部缓存。
            response.content = decrypted
        except AttributeError:
            response._content = decrypted
            response._content_consumed = True
        # text 可能在解密前被缓存，作废后按新内容重新解码。
        if getattr(response, "_text", None) is not None:
            response._text = None
        with suppress(Exception):
            del response.headers["X-HDH-Enc"]
            response.headers["Content-Length"] = str(len(decrypted))
        return response

    def verify_response(self, response, signed_path: str) -> None:
        """校验响应签名；受保护接口缺失签名时按错误响应处理。"""
        response_signature = str(response.headers.get("X-HDH-RSig") or "")
        if response_signature:
            if not self._protocol.verify_response(
                    signed_path,
                    response.status_code,
                    str(response.headers.get("X-HDH-RTS") or ""),
                    self._body_reader(response),
                    response_signature,
            ):
                raise self._error(
                    "HDHive 响应签名校验失败",
                    code="response_signature_invalid",
                )
            return
        if (
                response.status_code == 401
                or not self._protocol.requires_signed_response(signed_path)
        ):
            return
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = payload_error_message(payload).strip()
            raise self._error(
                f"HDHive 受保护接口请求失败："
                f"{message or f'HTTP {response.status_code}'}",
                code=(
                    "rate_limited"
                    if response.status_code == 429 or is_risk_message(message)
                    else "request_failed"
                ),
                status_code=response.status_code,
            )
        raise self._error(
            "HDHive 受保护接口未返回响应签名",
            code="response_signature_required",
            status_code=response.status_code,
        )

    def action_proof_headers(
            self,
            action: str,
            method: str,
            path_template: str,
            resource_slug: str,
            body: bytes,
    ) -> Dict[str, str]:
        """获取 Action Proof 挑战并构造证明请求头。"""

        if not self._signed_transport:
            raise self._error(
                "HDHive Action Proof 签名传输未初始化",
                code="action_proof_unavailable",
            )
        # 当前远程 signedFetch 对非空请求体使用 SHA-256；空体保持空串。
        body_hash = hashlib.sha256(body).hexdigest() if body else ""
        challenge_body = json.dumps({
            "action": str(action or ""),
            "method": str(method or "POST").upper(),
            "path": str(path_template or ""),
            "resource_slug": str(resource_slug or ""),
            "body_hash": body_hash,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._signed_transport(
            "POST",
            self.ACTION_PROOF_PATH,
            body=challenge_body,
            headers={
                "content-type": "application/json",
                "origin": self._base_url,
            },
            canonical_path=self.ACTION_PROOF_PATH,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except ValueError as error:
            raise self._error(
                "HDHive Action Proof 挑战响应格式异常",
                code="action_proof_challenge_failed",
                status_code=status_code,
            ) from error
        data = (
            payload.get("data") if isinstance(payload, dict) else None
        )
        if (
                status_code >= 400
                or not isinstance(payload, dict)
                or payload.get("success") is not True
                or not isinstance(data, dict)
        ):
            description = (
                str(payload.get("description") or "").strip()
                if isinstance(payload, dict) else ""
            )
            message = description or payload_error_message(payload).strip()
            raise self._error(
                f"HDHive Action Proof 挑战失败："
                f"{message or f'HTTP {status_code}'}",
                code="action_proof_challenge_failed",
                status_code=status_code,
            )
        proof_data = self._resolve_action_proof_manifest(data)
        request_nonce = secrets.token_hex(16)
        proof = compute_action_proof(
            proof_data,
            request_nonce=request_nonce,
            session_hint=self._protocol.cid,
            client_caps="",
            body_hash=body_hash,
        )
        return {
            "X-Action-Proof": proof,
            "X-Action-Challenge": str(data.get("challenge_id") or ""),
            "X-Action-Nonce": request_nonce,
            "X-Action-Algorithm": str(
                data.get("algorithm_version") or "ap-v1"
            ),
        }

    def _resolve_action_proof_manifest(
            self, challenge: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按网页流程动态读取表清单；缺失时使用 challenge 参数派生。"""
        table_version = str(challenge.get("table_version") or "").strip()
        if not table_version:
            return dict(challenge)
        manifest = self._action_proof_manifests.get(table_version)
        if manifest is None:
            path = (
                f"{self.ACTION_PROOF_TABLES_PATH}/"
                f"{quote(table_version, safe='')}/manifest.json"
            )
            try:
                response = self._transport(
                    "GET", path, headers={"cache-control": "no-cache"}
                )
                if int(getattr(response, "status_code", 0) or 0) >= 400:
                    raise ActionProofManifestError(
                        f"Action Proof manifest HTTP {response.status_code}"
                    )
                manifest = validate_action_proof_manifest(
                    self._body_reader(response).decode("utf-8", "replace"),
                    table_version,
                )
                self._action_proof_manifests[table_version] = manifest
                if len(self._action_proof_manifests) > 16:
                    self._action_proof_manifests.pop(
                        next(iter(self._action_proof_manifests))
                    )
            except Exception:
                return dict(challenge)
        resolved = dict(challenge)
        resolved["table_version"] = manifest["table_version"]
        if not str(resolved.get("profile") or "").strip():
            resolved["profile"] = str(manifest.get("profile") or "")
        return resolved
