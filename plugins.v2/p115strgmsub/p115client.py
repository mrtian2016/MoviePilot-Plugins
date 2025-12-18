"""
115网盘客户端封装
"""
from pathlib import Path
from typing import Optional, List, Dict, Any

from app.log import logger
try:
    from p115client import P115Client, check_response
    from p115client.util import share_extract_payload
    from p115client.tool.iterdir import share_iterdir
    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False
    logger.warning("p115client 未安装，115网盘功能不可用，请安装: pip install p115client")


class P115ClientManager:
    """115网盘客户端管理器"""

    def __init__(self, cookies: str, user_agent: str = None):
        """
        初始化115客户端

        :param cookies: 115 Cookie
        :param user_agent: User-Agent
        """
        self.cookies = cookies
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.client: Optional[Any] = None
        self.path_cache: Dict[str, int] = {"/": 0}

        if P115_AVAILABLE and cookies:
            try:
                self.client = P115Client(cookies, app="web")
            except Exception as e:
                logger.error(f"初始化 P115Client 失败: {e}")

    def check_login(self) -> bool:
        """检查登录状态"""
        if not self.client:
            return False

        try:
            user_info = self.client.user_my_info()
            if user_info.get("state"):
                uname = user_info.get('data', {}).get('uname', '未知')
                logger.info(f"115 登录成功: {uname}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查 115 登录状态失败: {e}")
            return False

    def get_pid_by_path(self, path: str, mkdir: bool = True) -> int:
        """
        通过文件夹路径获取 CID (Directory ID)

        :param path: 文件夹路径 (例如: /我的接收/电影)
        :param mkdir: 如果目录不存在，是否创建
        :return: 文件夹 ID，0 为根目录，-1 为获取失败
        """
        if not self.client:
            return -1

        # 规范化路径
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")

        # 根目录
        if not path or path == "/":
            return 0

        # 尝试从缓存获取
        if path in self.path_cache:
            return self.path_cache[path]

        # 尝试直接通过 API 获取
        try:
            resp = self.client.fs_dir_getid(path)
            # check_response(resp) # fs_dir_getid 通常返回 dict
            if resp.get("id"):
                cid = int(resp["id"])
                self.path_cache[path] = cid
                return cid
        except Exception as e:
            # logger.debug(f"直接获取路径 ID 失败: {e}")
            pass

        # 如果不创建，则返回失败
        if not mkdir:
            return -1

        # 递归创建/查找
        parent_id = 0
        current_path = ""
        parts = [p for p in path.split("/") if p]

        for part in parts:
            current_path = f"{current_path}/{part}"

            # 检查缓存
            if current_path in self.path_cache:
                parent_id = self.path_cache[current_path]
                continue

            # 尝试获取该层级 ID
            try:
                resp = self.client.fs_dir_getid(current_path)
                if resp.get("id"):
                    cid = int(resp["id"])
                    self.path_cache[current_path] = cid
                    parent_id = cid
                    continue
            except Exception:
                pass

            # 创建目录
            try:
                # 逐级创建
                resp = self.client.fs_makedirs_app(part, pid=parent_id)
                check_response(resp)
                if resp.get("state"):
                    cid = int(resp["cid"])
                    self.path_cache[current_path] = cid
                    parent_id = cid
                    logger.info(f"创建目录成功: {current_path} -> {cid}")
                else:
                    logger.error(f"创建目录失败 {current_path}: {resp.get('error')}")
                    return -1
            except Exception as e:
                logger.error(f"创建目录异常 {current_path}: {e}")
                return -1

        return parent_id

    def extract_share_info(self, url: str) -> Dict[str, str]:
        """
        解析分享链接，获取 share_code 和 receive_code

        :param url: 115 分享链接
        :return: {"share_code": ..., "receive_code": ...}
        """
        if not P115_AVAILABLE:
            return {}

        try:
            payload = share_extract_payload(url)
            return {
                "share_code": payload.get("share_code", ""),
                "receive_code": payload.get("receive_code", "")
            }
        except Exception as e:
            logger.error(f"解析分享链接失败: {e}")
            return {}

    def list_share_files(
            self,
            share_url: str,
            cid: int = 0,
            max_depth: int = 3
    ) -> List[dict]:
        """
        列出分享链接内的文件

        :param share_url: 115 分享链接
        :param cid: 目录 ID，0 为根目录
        :param max_depth: 最大递归深度
        :return: 文件列表
        """
        if not self.client:
            return []

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return []

        return self._list_share_files_recursive(
            share_code=share_code,
            receive_code=receive_code,
            cid=cid,
            depth=1,
            max_depth=max_depth
        )

    def _list_share_files_recursive(
            self,
            share_code: str,
            receive_code: str,
            cid: int = 0,
            depth: int = 1,
            max_depth: int = 3
    ) -> List[dict]:
        "递归列出分享文件"""
        if depth > max_depth:
            return []

        files = []
        try:
            iterator = share_iterdir(
                self.client,
                share_code=share_code,
                receive_code=receive_code,
                cid=cid,
                app="web",
            )

            for item in iterator:
                file_info = {
                    "id": str(item.get("id", "")),
                    "name": item.get("name", ""),
                    "size": item.get("size", 0),
                    "is_dir": item.get("is_dir", False),
                    "sha1": item.get("sha1", ""),
                    "pick_code": item.get("pick_code", ""),
                }

                # 递归获取子目录内容
                if file_info["is_dir"] and depth < max_depth:
                    sub_cid = int(item.get("id", 0))
                    children = self._list_share_files_recursive(
                        share_code=share_code,
                        receive_code=receive_code,
                        cid=sub_cid,
                        depth=depth + 1,
                        max_depth=max_depth
                    )
                    file_info["children"] = children

                files.append(file_info)

        except Exception as e:
            logger.error(f"列出分享文件失败: {e}")

        return files

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        """
        转存整个分享链接到指定目录

        :param share_url: 115 分享链接
        :param save_path: 保存路径
        :return: 是否成功
        """
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        # 获取目标目录 CID
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        logger.info(f"转存分享到目录 ID: {parent_id} ({save_path})")

        # 执行转存 (file_id=0 表示转存所有内容)
        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": "0",
            "cid": parent_id,
            "is_check": 0,
        }

        try:
            resp = self.client.share_receive(payload)

            if resp.get("state"):
                logger.info(f"转存成功！已保存到: {save_path}")
                return True
            else:
                error_msg = resp.get("error", "未知错误")
                logger.error(f"转存失败: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"转存过程中发生异常: {e}")
            return False

    def transfer_file(
            self,
            share_url: str,
            file_id: str,
            save_path: str
    ) -> bool:
        """
        转存分享中的单个文件

        :param share_url: 115 分享链接
        :param file_id: 文件 ID
        :param save_path: 保存路径
        :return: 是否成功
        """
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        # 获取目标目录 CID
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        # 执行单文件转存
        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": file_id,
            "cid": parent_id,
            "is_check": 0,
        }
        # logger.info(payload)

        try:
            resp = self.client.share_receive(payload)

            if resp.get("state"):
                logger.info(f"文件转存成功！文件ID: {file_id}, 保存到: {save_path}")
                return True
            else:
                error_msg = resp.get("error", "未知错误")
                # 检查是否是重复文件
                if "重复" in error_msg or "已存在" in error_msg:
                    logger.info(f"文件已存在，跳过: {file_id}")
                    return True
                logger.error(f"文件转存失败: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"文件转存过程中发生异常: {e}")
            return False

    def list_files(self, path: str) -> List[dict]:
        """
        列出指定路径下的文件

        :param path: 目录路径
        :return: 文件列表
        """
        if not self.client:
            return []

        cid = self.get_pid_by_path(path, mkdir=False)
        if cid == -1:
            return []

        try:
            resp = self.client.fs_files({"cid": cid, "limit": 1000})
            if resp.get("state"):
                return resp.get("data", [])
            return []
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            return []

    def list_directories(self, path: str) -> List[dict]:
        """
        列出指定路径下的所有目录（不包含文件）

        :param path: 目录路径
        :return: 目录列表，每个目录包含 name 和 path 字段
        """
        files = self.list_files(path)
        
        # 过滤出目录（fid=0 表示目录）
        directories = []
        for f in files:
            if f.get("fid") == 0:  # 是目录
                dir_name = f.get("name", "")
                dir_path = f"{path.rstrip('/')}/{dir_name}" if path != "/" else f"/{dir_name}"
                directories.append({
                    "name": dir_name,
                    "path": dir_path,
                    "cid": f.get("cid", 0)
                })
        
        return directories