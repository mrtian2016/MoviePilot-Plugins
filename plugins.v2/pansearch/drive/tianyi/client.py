"""天翼云盘客户端"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import random
import re
import time
import uuid
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from app.utils.string import StringUtils

from ..common import DriveRateLimiter


class TianyiApiError(RuntimeError):
    pass


class TianyiClient:
    WEB_URL = "https://cloud.189.cn"
    AUTH_URL = "https://open.e.189.cn"
    API_URL = "https://api.cloud.189.cn"
    APP_ID = "8025431004"
    CLIENT_TYPE = "10020"
    RETURN_URL = "https://m.cloud.189.cn/zhuanti/2020/loginErrorPc/index.html"

    def __init__(
            self, cookie: str = "", timeout: int = 60,
            access_token: str = "", refresh_token: str = "",
            session_key: str = "", on_token_refresh=None,
    ):
        self.timeout = max(10, int(timeout or 60))
        self.access_token = str(access_token or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.session_key = str(session_key or "").strip()
        self.on_token_refresh = on_token_refresh
        self.rate_limiter = DriveRateLimiter.shared(
            "tianyi", self.session_key or self.access_token, min_interval=0.5
        )
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json;charset=UTF-8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://cloud.189.cn/",
            "Sign-Type": "1",
            "User-Agent": "Mozilla/5.0",
        })
        if cookie:
            self.session.headers["Cookie"] = cookie

    def close(self):
        self.session.close()

    @staticmethod
    def _response_data(response: requests.Response) -> dict:
        http_error = None
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            http_error = error
        try:
            data = response.json()
        except ValueError:
            try:
                root = ElementTree.fromstring(response.text)
            except ElementTree.ParseError as error:
                raise TianyiApiError("天翼云盘返回了无法识别的响应") from error

            def xml_value(element):
                children = list(element)
                if not children:
                    return str(element.text or "").strip()
                result = {}
                for child in children:
                    key = child.tag.rsplit("}", 1)[-1]
                    value = xml_value(child)
                    if key not in result:
                        result[key] = value
                    elif isinstance(result[key], list):
                        result[key].append(value)
                    else:
                        result[key] = [result[key], value]
                return result

            data = xml_value(root)
        if not isinstance(data, dict):
            raise TianyiApiError("天翼云盘返回了无效响应")
        error_code = data.get("errorCode")
        result_code = data.get("res_code")
        has_error = error_code not in (None, "", 0, "0", "SUCCESS")
        if result_code not in (None, "", 0, "0", "SUCCESS"):
            has_error = True
        if http_error or has_error:
            code = str(data.get("code") or error_code or result_code or "")
            known_messages = {
                "ShareAuditNotPass": "天翼分享未通过审核或已被屏蔽",
                "ShareNotFound": "天翼分享不存在或已失效",
                "ShareNotFoundFlatDir": "天翼分享目录不存在",
            }
            raise TianyiApiError(
                known_messages.get(code)
                or data.get("message")
                or data.get("errorMsg")
                or data.get("res_message")
                or str(data)
            )
        return data

    def _raw_request(self, method: str, url: str, **kwargs) -> dict:
        response = self.rate_limiter.call(
            self.session.request,
            method,
            url,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
            **kwargs,
        )
        return self._response_data(response)

    def public_request(self, method: str, url: str, **kwargs) -> dict:
        """请求无需登录的公开分享接口。"""
        headers = {"Accept": "application/json;charset=UTF-8"}
        headers.update(dict(kwargs.pop("headers", {}) or {}))
        return self._raw_request(method, url, headers=headers, **kwargs)

    def request(self, method: str, url: str, **kwargs):
        if url.startswith(self.WEB_URL):
            self.ensure_session()
            params = dict(kwargs.pop("params", {}) or {})
            params.setdefault("noCache", random.random())
            params.setdefault("sessionKey", self.session_key)
            kwargs["params"] = params
        return self._raw_request(method, url, **kwargs)

    def _get_login_form(self) -> dict:
        response = self.rate_limiter.call(
            self.session.get,
            f"{self.WEB_URL}/api/portal/unifyLoginForPC.action",
            params={
                "appId": self.APP_ID,
                "clientType": self.CLIENT_TYPE,
                "returnURL": self.RETURN_URL,
                "timeStamp": int(time.time() * 1000),
            },
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        response.raise_for_status()
        html = response.text

        def extract(pattern: str, name: str) -> str:
            match = re.search(pattern, html)
            if not match:
                raise TianyiApiError(f"天翼扫码登录页缺少 {name}")
            value = match.group(1).strip()
            if not value.isascii() or "\r" in value or "\n" in value:
                raise TianyiApiError(f"天翼扫码登录页的 {name} 参数无效")
            return value

        return {
            "lt": extract(r'\blt\s*=\s*["\']([^"\']+)["\']', "lt"),
            "param_id": extract(
                r'\bparamId\s*=\s*["\']([^"\']+)["\']', "paramId"
            ),
            "req_id": extract(
                r'\breqId\s*=\s*["\']([^"\']+)["\']', "reqId"
            ),
        }

    def create_qrcode_login(self, client_type: str = "") -> dict:
        login_form = self._get_login_form()
        data = self._raw_request(
            "POST", f"{self.AUTH_URL}/api/logbox/oauth2/getUUID.do",
            headers={"Referer": self.AUTH_URL},
            data={"appId": self.APP_ID},
        )
        qr_uuid = str(data.get("uuid") or "").strip()
        encrypted_uuid = str(data.get("encryuuid") or "").strip()
        if not qr_uuid or not encrypted_uuid:
            raise TianyiApiError("天翼云盘未返回完整二维码参数")
        return {
            "qr_url": qr_uuid,
            "uuid": qr_uuid,
            "encryuuid": encrypted_uuid,
            **login_form,
            "expires_in": 120,
            "interval": 3,
        }

    def _get_session_for_pc(
            self, *, redirect_url: str = "", access_token: str = ""
    ) -> dict:
        params = {
            "appId": self.APP_ID,
            "clientType": self.CLIENT_TYPE,
            "version": "6.2",
            "model": "TELEPC",
            "osFamily": "windows",
            "osVersion": "10",
            "clientSn": "web_cloud.189.cn",
        }
        if redirect_url:
            params["redirectURL"] = redirect_url
        if access_token:
            params["accessToken"] = access_token
        return self._raw_request(
            "POST", f"{self.API_URL}/getSessionForPC.action", params=params
        )

    def _apply_token_session(self, data: dict) -> None:
        self.access_token = str(data.get("accessToken") or self.access_token).strip()
        self.refresh_token = str(data.get("refreshToken") or self.refresh_token).strip()
        self.session_key = str(data.get("sessionKey") or self.session_key).strip()
        if not self.session_key:
            raise TianyiApiError("天翼登录响应缺少 SessionKey")
        if self.on_token_refresh:
            self.on_token_refresh(
                self.access_token, self.refresh_token, self.session_key
            )

    def check_qrcode_login(self, **kwargs) -> dict:
        qr_data = {
            "uuid": str(kwargs.get("uuid") or "").strip(),
            "encryuuid": str(kwargs.get("encryuuid") or "").strip(),
            "req_id": str(kwargs.get("req_id") or "").strip(),
            "lt": str(kwargs.get("lt") or "").strip(),
            "param_id": str(kwargs.get("param_id") or "").strip(),
        }
        if not all(qr_data.values()):
            raise ValueError("缺少天翼云盘扫码会话参数")
        for name in ("req_id", "lt"):
            value = qr_data[name]
            if not value.isascii() or "\r" in value or "\n" in value:
                raise ValueError(f"天翼云盘扫码会话参数 {name} 无效")
        now = time.localtime()
        milliseconds = int(time.time() * 1000) % 1000
        date = time.strftime("%Y-%m-%d%H:%M:%S", now) + f".{milliseconds:03d}"
        data = self._raw_request(
            "POST", f"{self.AUTH_URL}/api/logbox/oauth2/qrcodeLoginState.do",
            headers={
                "Referer": self.AUTH_URL,
                "Reqid": qr_data["req_id"],
                "lt": qr_data["lt"],
                "Accept": "application/json;charset=UTF-8",
            },
            data={
                "appId": self.APP_ID,
                "clientType": self.CLIENT_TYPE,
                "returnUrl": self.RETURN_URL,
                "paramId": qr_data["param_id"],
                "uuid": qr_data["uuid"],
                "encryuuid": qr_data["encryuuid"],
                "date": date,
                "timeStamp": int(time.time() * 1000),
                "cb_SaveName": "0",
                "isOauth2": "true",
                "state": "",
            },
        )
        status = int(data.get("status") or 0)
        if status == -106:
            return {"status": "waiting", "message": "等待扫码"}
        if status == -11002:
            return {"status": "scanned", "message": "已扫码，请在手机上确认"}
        if status == -11001:
            return {"status": "expired", "message": "二维码已失效"}
        if status != 0:
            message = str(data.get("msg") or data.get("message") or "").strip()
            raise TianyiApiError(message or f"天翼扫码登录状态异常：{status}")
        redirect_url = str(data.get("redirectUrl") or "").strip()
        if not redirect_url:
            raise TianyiApiError("天翼扫码成功但未返回登录地址")
        session_data = self._get_session_for_pc(redirect_url=redirect_url)
        self._apply_token_session(session_data)
        return {
            "status": "success",
            "message": "登录成功",
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "session_key": self.session_key,
        }

    def refresh(self) -> None:
        if not self.refresh_token:
            raise TianyiApiError("未配置天翼云盘 Refresh Token")
        data = self._raw_request(
            "POST", f"{self.AUTH_URL}/api/oauth2/refreshToken.do",
            data={
                "clientId": self.APP_ID,
                "refreshToken": self.refresh_token,
                "grantType": "refresh_token",
                "format": "json",
            },
        )
        access_token = str(data.get("accessToken") or "").strip()
        if not access_token:
            raise TianyiApiError("天翼云盘 Token 刷新失败")
        self._apply_token_session(
            self._get_session_for_pc(access_token=access_token)
        )

    def ensure_session(self) -> None:
        if self.session_key:
            return
        if self.access_token:
            self._apply_token_session(
                self._get_session_for_pc(access_token=self.access_token)
            )
            return
        if self.refresh_token:
            self.refresh()
            return
        if not self.check_login():
            raise TianyiApiError("天翼登录凭据已失效")

    def check_login(self) -> bool:
        try:
            if self.session_key:
                return True
            data = self._raw_request(
                "GET", f"{self.WEB_URL}/v2/getUserBriefInfo.action"
            )
            self.session_key = str(data.get("sessionKey") or "")
            return bool(self.session_key)
        except Exception:
            return False

    def get_account_info(self) -> dict:
        try:
            self.ensure_session()
            user = self.request(
                "GET", f"{self.WEB_URL}/v2/getUserBriefInfo.action"
            )
            size = self.request(
                "GET", f"{self.WEB_URL}/api/portal/getUserSizeInfo.action"
            )
            capacity = size.get("cloudCapacityInfo") or {}
            total = int(capacity.get("totalSize") or 0)
            used = int(capacity.get("usedSize") or 0)
            remaining = int(capacity.get("freeSize") or max(0, total - used))
            return {
                "connected": True,
                "user": {
                    "name": str(
                        user.get("nickname") or user.get("loginName")
                        or user.get("userName") or "天翼云盘用户"
                    ),
                    "avatar": str(user.get("iconUrl") or user.get("avatar") or ""),
                    "membership_supported": False,
                    "is_vip": False,
                    "is_forever_vip": False,
                    "vip_expire_date": "",
                },
                "storage": {
                    "total": StringUtils.str_filesize(total),
                    "used": StringUtils.str_filesize(used),
                    "remaining": StringUtils.str_filesize(remaining),
                },
            }
        except Exception as error:
            return {"connected": False, "error": str(error)}

    def _rsa_key(self) -> tuple[str, str]:
        data = self.request("GET", "https://cloud.189.cn/api/security/generateRsaKey.action")
        return str(data.get("pubKey") or ""), str(data.get("pkId") or "")

    def upload_request(self, uri: str, params: dict) -> dict:
        if not self.session_key and not self.check_login():
            raise TianyiApiError("天翼登录 Cookie 已失效")
        query = urlencode(params)
        secret = (uuid.uuid4().hex + os.urandom(8).hex())[:24]
        key = secret[:16].encode()
        padding = 16 - len(query.encode()) % 16
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(query.encode() + bytes([padding]) * padding).hex()
        timestamp = str(int(time.time() * 1000))
        sign_text = f"SessionKey={self.session_key}&Operate=GET&RequestURI={uri}&Date={timestamp}&params={encrypted}"
        signature = hmac.new(secret.encode(), sign_text.encode(), hashlib.sha1).hexdigest()
        pub_key, pk_id = self._rsa_key()
        rsa_key = RSA.import_key(f"-----BEGIN PUBLIC KEY-----\n{pub_key}\n-----END PUBLIC KEY-----")
        encryption_text = base64.b64encode(PKCS1_v1_5.new(rsa_key).encrypt(secret.encode())).decode()
        headers = {"SessionKey": self.session_key, "Signature": signature,
                   "X-Request-Date": timestamp, "X-Request-ID": str(uuid.uuid4()),
                   "EncryptionText": encryption_text, "PkId": pk_id,
                   "Accept": "application/json;charset=UTF-8"}
        data = self.request("GET", "https://upload.cloud.189.cn" + uri,
                            params={"params": encrypted}, headers=headers)
        if data.get("code") != "SUCCESS":
            raise TianyiApiError(data.get("msg") or str(data))
        return data
