"""
工具函数模块
包含不涉及业务逻辑的通用工具函数
"""
import base64
import datetime
import json
import os
import platform
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple

from app.core.config import settings
from app.log import logger


def _parse_proxy_url(proxy) -> Optional[Dict[str, str]]:
    """
    解析代理URL，支持 http://user:password@ip:port 格式
    
    :param proxy: 代理配置，可以是字符串或字典
    :return: Playwright 格式的代理配置 {"server": "...", "username": "...", "password": "..."}
    """
    if not proxy:
        return None
    
    # 如果是字典格式，取 http 或 https
    if isinstance(proxy, dict):
        proxy_url = proxy.get("http") or proxy.get("https")
    else:
        proxy_url = str(proxy)
    
    if not proxy_url:
        return None
    
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        
        # 构建不带认证的服务器地址
        if parsed.port:
            server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        else:
            server = f"{parsed.scheme}://{parsed.hostname}"
        
        result = {"server": server}
        
        # 如果有用户名和密码
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        
        return result
    except Exception as e:
        logger.debug(f"解析代理URL失败: {e}，将直接使用原始URL")
        return {"server": proxy_url}


def get_hdhive_extension_filename() -> Optional[str]:
    """
    根据当前平台获取 hdhive 扩展模块的文件名

    :return: 文件名，如果平台不支持则返回 None
    """
    machine = platform.machine().lower()
    system = platform.system().lower()

    # 映射架构名称
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    arch = arch_map.get(machine, machine)

    if system == "windows":
        # Windows: .pyd 文件 (Nuitka 编译产物)
        return f"hdhive.cp312-win_{arch}.pyd"
    elif system == "darwin":
        # macOS: 文件名不含架构前缀
        return "hdhive.cpython-312-darwin.so"
    elif system == "linux":
        # Linux: 包含完整架构信息
        return f"hdhive.cpython-312-{arch}-linux-gnu.so"
    else:
        return None


def download_so_file(lib_dir: Path):
    """
    检查并确保 hdhive 扩展模块可用

    优先使用本地预编译的文件，如果不存在则尝试从 GitHub 下载

    :param lib_dir: 库文件目录路径
    """
    lib_dir.mkdir(parents=True, exist_ok=True)

    system = platform.system().lower()
    machine = platform.machine().lower()

    # 获取当前平台对应的文件名
    ext_filename = get_hdhive_extension_filename()
    if not ext_filename:
        logger.warning(f"不支持的平台: {system}/{machine}，HDHive 功能无法使用")
        return

    target_path = lib_dir / ext_filename

    # 本地文件已存在，直接返回
    if target_path.exists():
        logger.debug(f"hdhive 扩展模块已存在: {target_path}")
        return

    # 本地不存在，尝试从 GitHub 下载
    base_url = "https://raw.githubusercontent.com/mrtian2016/hdhive_resource/main"
    download_url = f"{base_url}/{ext_filename}"

    logger.info(f"本地未找到 hdhive 扩展模块，尝试下载: {download_url}")

    try:
        # 设置代理
        proxy = settings.PROXY
        if proxy:
            # 兼容字符串和字典格式
            if isinstance(proxy, dict):
                proxy_handler = urllib.request.ProxyHandler(proxy)
            else:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(proxy_handler)
            logger.info(f"下载 hdhive 使用代理: {proxy}")
            response = opener.open(download_url, timeout=120)
        else:
            response = urllib.request.urlopen(download_url, timeout=120)

        with response:
            content = response.read()

        with open(target_path, "wb") as f:
            f.write(content)

        if system != "windows":
            os.chmod(target_path, 0o755)

        logger.info(f"hdhive 扩展模块下载成功: {target_path}")

    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.warning(f"hdhive 扩展模块暂不支持当前平台 ({system}/{machine})，HDHive 功能无法使用")
        else:
            logger.error(f"下载 hdhive 扩展模块失败 (HTTP {e.code}): {e}")
    except urllib.error.URLError as e:
        logger.error(f"下载 hdhive 扩展模块失败（网络错误）: {e}")
    except Exception as e:
        logger.error(f"下载 hdhive 扩展模块失败: {e}")


def extract_token_from_hdhive_cookie(cookie: str) -> Optional[str]:
    """
    从 HDHive Cookie 字符串中提取 token

    :param cookie: Cookie 字符串，格式如 "token=xxx; csrf_access_token=xxx"
    :return: token 值，如果未找到返回 None
    """
    if not cookie:
        return None

    for part in cookie.split(';'):
        part = part.strip()
        if part.startswith('token='):
            return part.split('=', 1)[1]
    return None


def decode_jwt_payload(token: str) -> Optional[dict]:
    """
    解码 JWT token 的 payload 部分（不验证签名）

    :param token: JWT token 字符串
    :return: payload 字典，解码失败返回 None
    """
    if not token:
        return None

    try:
        # JWT 格式: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return None

        # 解码 payload（第二部分）
        payload = parts[1]
        # 补齐 base64 padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        logger.debug(f"解码 JWT 失败: {e}")
        return None


def get_hdhive_token_info(cookie: str) -> Optional[dict]:
    """
    获取 HDHive Cookie 中 token 的信息（过期时间等）

    :param cookie: Cookie 字符串
    :return: token 信息字典，包含 exp, exp_time, time_left, is_expired, user_id
    """
    token = extract_token_from_hdhive_cookie(cookie)
    if not token:
        return None

    decoded = decode_jwt_payload(token)
    if not decoded:
        return None

    exp = decoded.get('exp')
    if not exp:
        return None

    exp_time = datetime.datetime.fromtimestamp(exp)
    now = datetime.datetime.now()
    time_left = (exp_time - now).total_seconds()

    return {
        'exp': exp,
        'exp_time': exp_time,
        'time_left': time_left,
        'is_expired': time_left <= 0,
        'user_id': decoded.get('sub')
    }


def check_hdhive_cookie_valid(cookie: str, refresh_before: int = 86400) -> Tuple[bool, str]:
    """
    检查 HDHive Cookie 是否有效

    :param cookie: Cookie 字符串
    :param refresh_before: 在过期前多少秒视为需要刷新
    :return: (是否有效, 原因说明)
    """
    if not cookie:
        return False, "Cookie 为空"

    token_info = get_hdhive_token_info(cookie)
    if not token_info:
        return False, "无法解析 Cookie 中的 token"

    if token_info['is_expired']:
        return False, "Cookie 已过期"

    if token_info['time_left'] < refresh_before:
        hours_left = token_info['time_left'] / 3600
        return False, f"Cookie 将在 {hours_left:.1f} 小时后过期，需要刷新"

    return True, f"Cookie 有效，还有 {token_info['time_left'] / 3600:.1f} 小时"


def refresh_hdhive_cookie_with_playwright(
    username: str,
    password: str,
    base_url: str = "https://hdhive.com"
) -> Optional[str]:
    """
    使用 Playwright 登录 HDHive 获取新 Cookie

    :param username: HDHive 用户名
    :param password: HDHive 密码
    :param base_url: HDHive 站点地址
    :return: 新的 Cookie 字符串，失败返回 None
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright 未安装，无法自动刷新 HDHive Cookie")
        logger.info("请运行: pip install playwright && playwright install firefox")
        return None

    try:
        login_url = f"{base_url}/login"
        proxy = settings.PROXY

        with sync_playwright() as pw:
            # 配置浏览器启动选项
            launch_options = {"headless": True}

            # 配置代理（支持 http://user:password@ip:port 格式）
            context_options = {}
            if proxy:
                proxy_config = _parse_proxy_url(proxy)
                if proxy_config:
                    context_options["proxy"] = proxy_config
                    # 日志中隐藏密码
                    safe_server = proxy_config.get("server", "")
                    if proxy_config.get("username"):
                        logger.info(f"HDHive Cookie 刷新使用代理: {safe_server} (带认证)")
                    else:
                        logger.info(f"HDHive Cookie 刷新使用代理: {safe_server}")

            # 使用 Firefox（更容易绕过 Cloudflare）
            browser = pw.chromium.launch(**launch_options)
            context = browser.new_context(**context_options)
            page = context.new_page()

            logger.info("HDHive: 访问登录页...")
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # 填写用户名
            username_selectors = [
                '#username',
                'input[name="username"]',
                'input[name="email"]',
                'input[type="email"]',
            ]

            username_filled = False
            for sel in username_selectors:
                try:
                    if page.query_selector(sel):
                        page.fill(sel, username)
                        logger.info("HDHive: 填写用户名成功")
                        username_filled = True
                        break
                except Exception:
                    continue

            if not username_filled:
                logger.error("HDHive: 未找到用户名输入框")
                context.close()
                browser.close()
                return None

            # 填写密码
            password_selectors = [
                '#password',
                'input[name="password"]',
                'input[type="password"]',
            ]

            password_filled = False
            for sel in password_selectors:
                try:
                    if page.query_selector(sel):
                        page.fill(sel, password)
                        logger.info("HDHive: 填写密码成功")
                        password_filled = True
                        break
                except Exception:
                    continue

            if not password_filled:
                logger.error("HDHive: 未找到密码输入框")
                context.close()
                browser.close()
                return None

            # 提交登录
            page.wait_for_timeout(500)
            try:
                btn = page.query_selector('button[type="submit"]')
                if btn:
                    btn.click()
                    logger.info("HDHive: 点击登录按钮成功")
                else:
                    page.keyboard.press("Enter")
                    logger.info("HDHive: 按 Enter 键提交成功")
            except Exception:
                page.keyboard.press("Enter")

            # 等待登录完成
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            # 增加等待时间，确保登录完成和 Cookie 设置
            page.wait_for_timeout(3000)

            # 检查当前 URL，判断是否登录成功
            current_url = page.url
            logger.info(f"HDHive: 登录后 URL: {current_url}")

            # 如果还在登录页面，可能登录失败
            if "/login" in current_url:
                # 尝试检查是否有错误提示
                error_selectors = [
                    '.error-message',
                    '.alert-error',
                    '.text-red-500',
                    '[class*="error"]',
                    '[class*="Error"]',
                ]
                error_msg = None
                for sel in error_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            error_msg = el.text_content()
                            break
                    except Exception:
                        continue

                if error_msg:
                    logger.error(f"HDHive: 登录失败，错误信息: {error_msg}")
                else:
                    logger.warning("HDHive: 登录后仍在登录页面，可能需要验证或密码错误")

                # 再等待一下，可能是跳转慢
                page.wait_for_timeout(3000)

            # 获取 Cookie
            cookies = context.cookies()
            token = None
            csrf = None

            # 打印所有 cookies 用于调试
            cookie_names = [c.get('name') for c in cookies]
            logger.info(f"HDHive: 获取到的 Cookie 名称: {cookie_names}")

            for c in cookies:
                if c.get('name') == 'token':
                    token = c.get('value')
                elif c.get('name') == 'csrf_access_token':
                    csrf = c.get('value')

            context.close()
            browser.close()

            if token:
                cookie_parts = [f"token={token}"]
                if csrf:
                    cookie_parts.append(f"csrf_access_token={csrf}")
                cookie_str = "; ".join(cookie_parts)
                logger.info(f"HDHive: 登录成功！Cookie 长度: {len(cookie_str)}")
                return cookie_str
            else:
                logger.error(f"HDHive: 登录失败：未获取到 token，当前 URL: {current_url}")
                return None

    except Exception as e:
        logger.error(f"HDHive: Playwright 登录异常: {e}")
        return None


def convert_nullbr_to_pansou_format(nullbr_resources: List[Dict]) -> List[Dict]:
    """
    将 Nullbr 资源格式转换为统一的资源格式

    Nullbr 格式: {"title": "...", "share_link": "...", "size": "...", "resolution": "...", "season_list": [...]}
    统一格式: {"url": "...", "title": "...", "update_time": ""}

    :param nullbr_resources: Nullbr 返回的资源列表
    :return: 统一格式的资源列表
    """
    converted = []
    for resource in nullbr_resources:
        converted.append({
            "url": resource.get("share_link", ""),
            "title": resource.get("title", ""),
            "update_time": ""  # Nullbr 没有更新时间字段
        })
    return converted


def convert_hdhive_to_pansou_format(hdhive_resources: List[Any]) -> List[Dict]:
    """
    将 HDHive 资源格式转换为统一的资源格式

    HDHive ResourceInfo: title, share_url, share_size, website, is_free, unlock_points 等
    统一格式: {"url": "...", "title": "...", "update_time": ""}

    :param hdhive_resources: HDHive 返回的资源列表
    :return: 统一格式的资源列表
    """
    converted = []
    for resource in hdhive_resources:
        # HDHive 资源可能是对象或字典
        if hasattr(resource, 'url'):
            url = resource.url or ""
        elif isinstance(resource, dict):
            url = resource.get("url", "") or resource.get("share_url", "")
        else:
            url = ""

        if hasattr(resource, 'title'):
            title = resource.title or ""
        elif isinstance(resource, dict):
            title = resource.get("title", "")
        else:
            title = ""

        if url:  # 只添加有 URL 的资源
            converted.append({
                "url": url,
                "title": title,
                "update_time": ""
            })
    return converted
