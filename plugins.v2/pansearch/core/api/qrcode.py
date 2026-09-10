"""网盘扫码登录 API。"""

from base64 import b64encode
from io import BytesIO
from typing import Any, Dict

import qrcode.image.svg
from app.log import logger

import qrcode
from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ...drive.p115 import P115ClientManager


class QRCodeService(OwnerDelegator):
    """按 Provider 能力创建二维码并写回登录凭证。"""

    @staticmethod
    def _svg_qrcode(value: str) -> str:
        image = qrcode.make(
            value,
            image_factory=qrcode.image.svg.SvgPathImage,
            box_size=8,
            border=2,
        )
        output = BytesIO()
        image.save(output)
        return f"data:image/svg+xml;base64,{b64encode(output.getvalue()).decode('ascii')}"

    def _qrcode_service(self, provider: str):
        key = str(provider or "115").strip().lower()
        if not self._cloud_drive_registry:
            raise RuntimeError("网盘提供方尚未初始化")
        drive = self._cloud_drive_registry.get(key)
        return key, drive.require(CloudDriveCapability.QRCODE_AUTH)

    def api_vue_get_qrcode(
            self, provider: str = "115", client_type: str = "alipaymini"
    ) -> dict:
        try:
            key, service = self._qrcode_service(provider)
            data = dict(service.create_qrcode_login(client_type) or {})
            qr_value = str(
                data.get("qr_url")
                or data.get("verification_uri_complete")
                or data.get("verification_uri")
                or ""
            ).strip()
            if not data.get("qrcode") and qr_value:
                data["qrcode"] = self._svg_qrcode(qr_value)
            if not data.get("qrcode"):
                raise RuntimeError("网盘接口未返回可用二维码")
            data["provider"] = key
            return {"success": True, "data": data}
        except Exception as error:
            logger.error(f"获取网盘登录二维码失败：{provider} - {error}")
            return {"success": False, "message": str(error)}

    def api_vue_check_qrcode(
            self,
            provider: str = "115",
            uid: str = "",
            time: str = "",
            sign: str = "",
            client_type: str = "alipaymini",
            qr_token: str = "",
            uni_id: str = "",
            device_code: str = "",
            device_id: str = "",
            client_id: str = "",
            t: str = "",
            ck: str = "",
            uuid: str = "",
            encryuuid: str = "",
            req_id: str = "",
            lt: str = "",
            param_id: str = "",
    ) -> dict:
        try:
            key, service = self._qrcode_service(provider)
            params: Dict[str, Any]
            if key == "115":
                params = {
                    "uid": uid,
                    "qrcode_time": time,
                    "sign": sign,
                    "client_type": client_type,
                }
            elif key == "quark":
                params = {"qr_token": qr_token}
            elif key == "123":
                params = {"uni_id": uni_id}
            elif key == "guangya":
                params = {
                    "device_code": device_code,
                    "device_id": device_id,
                    "client_id": client_id,
                }
            elif key == "alipan":
                params = {"t": t, "ck": ck}
            elif key == "tianyi":
                params = {
                    "uuid": uuid,
                    "encryuuid": encryuuid,
                    "req_id": req_id,
                    "lt": lt,
                    "param_id": param_id,
                }
            else:
                raise ValueError(f"不支持扫码登录的网盘提供方：{key}")

            result = dict(service.check_qrcode_login(**params) or {})
            if result.get("status") != "success":
                return {"success": True, "provider": key, **result}

            credentials = self._apply_qrcode_credentials(key, result)
            self._persist_config_values(**credentials)
            self._init_handlers()
            from .account import clear_account_cache
            from .page import clear_ui_options_cache
            clear_account_cache(f"drive:{key}")
            clear_ui_options_cache()
            logger.info(f"{key} 扫码登录成功")
            return {
                "success": True,
                "provider": key,
                "status": "success",
                "message": result.get("message") or "登录成功",
                "credentials": credentials,
            }
        except Exception as error:
            logger.error(f"检查网盘扫码登录状态失败：{provider} - {error}")
            return {"success": False, "message": str(error)}

    def api_vue_check_qrcode_post(self, payload: Dict[str, Any]) -> dict:
        """POST 版本的扫码状态检查，避免把临时凭据放进 URL。"""
        data = payload or {}
        fields = (
            "provider", "uid", "time", "sign", "client_type", "qr_token",
            "uni_id", "device_code", "device_id", "client_id", "t", "ck",
            "uuid", "encryuuid", "req_id", "lt", "param_id",
        )
        return self.api_vue_check_qrcode(**{
            field: data.get(field, "") for field in fields if field in data
        })

    def _apply_qrcode_credentials(
            self, provider: str, result: Dict[str, Any]
    ) -> Dict[str, str]:
        if provider == "115":
            cookies = str(result.get("cookie") or "").strip()
            if not cookies:
                raise RuntimeError("扫码成功但未获得 115 Cookie")
            manager = P115ClientManager(
                cookies=cookies,
                share_cache_ttl_minutes=self._search_cache_ttl_minutes,
                **self._p115_timeout_kwargs(),
            )
            if not manager.check_login():
                raise RuntimeError("扫码成功，但115登录状态校验失败")
            if self._p115_manager:
                self._p115_manager.close()
            self._p115_cookies = cookies
            self._p115_manager = manager
            self._register_p115_provider()
            return {"cookies": cookies}

        if provider == "quark":
            cookie = str(result.get("cookie") or "").strip()
            if not cookie:
                raise RuntimeError("扫码成功但未获得夸克 Cookie")
            self._quark_cookie = cookie
            self._register_quark_provider()
            return {"quark_cookie": cookie}

        if provider == "123":
            token = str(result.get("token") or "").strip()
            if not token:
                raise RuntimeError("扫码成功但未获得 123 网盘 Token")
            self._p123_token = token
            self._register_p123_provider()
            return {"p123_token": token}

        if provider == "guangya":
            access_token = str(result.get("access_token") or "").strip()
            refresh_token = str(result.get("refresh_token") or "").strip()
            if not access_token:
                raise RuntimeError("扫码成功但未获得光鸭 Access Token")
            self._guangya_access_token = access_token
            self._guangya_refresh_token = refresh_token
            self._guangya_client_id = str(result.get("client_id") or "").strip()
            self._guangya_device_id = str(result.get("device_id") or "").strip()
            self._register_guangya_provider()
            return {
                "guangya_access_token": access_token,
                "guangya_refresh_token": refresh_token,
                "guangya_client_id": self._guangya_client_id,
                "guangya_device_id": self._guangya_device_id,
            }

        if provider == "alipan":
            access_token = str(result.get("access_token") or "").strip()
            refresh_token = str(result.get("refresh_token") or "").strip()
            if not access_token and not refresh_token:
                raise RuntimeError("扫码成功但未获得阿里云盘 Token")
            self._alipan_access_token = access_token
            self._alipan_refresh_token = refresh_token
            self._register_alipan_provider()
            return {
                "alipan_access_token": access_token,
                "alipan_refresh_token": refresh_token,
            }

        if provider == "tianyi":
            access_token = str(result.get("access_token") or "").strip()
            refresh_token = str(result.get("refresh_token") or "").strip()
            session_key = str(result.get("session_key") or "").strip()
            if not session_key:
                raise RuntimeError("扫码成功但未获得天翼云盘 SessionKey")
            self._tianyi_access_token = access_token
            self._tianyi_refresh_token = refresh_token
            self._tianyi_session_key = session_key
            self._register_tianyi_provider()
            return {
                "tianyi_access_token": access_token,
                "tianyi_refresh_token": refresh_token,
                "tianyi_session_key": session_key,
            }

        raise ValueError(f"不支持扫码登录的网盘提供方：{provider}")
