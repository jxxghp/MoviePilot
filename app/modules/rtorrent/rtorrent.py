import socket
import traceback
import xmlrpc.client
from pathlib import Path
from typing import Optional, Union, Tuple, List, Dict
from urllib.parse import urlparse

from app.runtime.log import logger


class SCGITransport(xmlrpc.client.Transport):
    """
    通过SCGI协议与rTorrent通信的Transport
    """

    def single_request(self, host, handler, request_body, verbose=False):
        # 建立socket连接
        parsed = urlparse(f"scgi://{host}")
        sock = socket.create_connection(
            (parsed.hostname, parsed.port or 5000), timeout=60
        )
        try:
            # 构造SCGI请求头
            headers = (
                f"CONTENT_LENGTH\x00{len(request_body)}\x00"
                f"SCGI\x001\x00"
                f"REQUEST_METHOD\x00POST\x00"
                f"REQUEST_URI\x00/RPC2\x00"
            )
            # netstring格式: "len:headers,"
            netstring = f"{len(headers)}:{headers},".encode()
            # 发送请求
            sock.sendall(netstring + request_body)
            # 读取响应
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            sock.close()

        # 跳过HTTP响应头
        header_end = response.find(b"\r\n\r\n")
        if header_end != -1:
            response = response[header_end + 4 :]

        # 解析XML-RPC响应
        return self.parse_response(self._build_response(response))

    @staticmethod
    def _build_response(data: bytes):
        """
        构造类文件对象用于parse_response
        """
        import io
        import http.client

        class _FakeSocket(io.BytesIO):
            def makefile(self, *args, **kwargs):
                return self

        raw = b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\n\r\n" + data
        response = http.client.HTTPResponse(_FakeSocket(raw))  # noqa
        response.begin()
        return response


class Rtorrent:
    """
    rTorrent下载器
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs,
    ):
        self._proxy = None
        if host and port:
            self._host = f"{host}:{port}"
        elif host:
            self._host = host
        else:
            logger.error("rTorrent配置不完整！")
            return
        self._username = username
        self._password = password
        self._proxy = self.__login_rtorrent()

    def __login_rtorrent(self) -> Optional[xmlrpc.client.ServerProxy]:
        """
        连接rTorrent
        """
        if not self._host:
            return None
        try:
            url = self._host
            if url.startswith("scgi://"):
                # SCGI直连模式
                logger.info(f"正在通过SCGI连接 rTorrent：{url}")
                proxy = xmlrpc.client.ServerProxy(url, transport=SCGITransport())
            else:
                # HTTP模式 (通过nginx/ruTorrent代理)
                if not url.startswith("http"):
                    url = f"http://{url}"
                # 注入认证信息到URL
                if self._username and self._password:
                    parsed = urlparse(url)
                    url = f"{parsed.scheme}://{self._username}:{self._password}@{parsed.hostname}"
                    if parsed.port:
                        url += f":{parsed.port}"
                    url += parsed.path or "/RPC2"
                logger.info(
                    f"正在通过HTTP连接 rTorrent：{url.split('@')[-1] if '@' in url else url}"
                )
                proxy = xmlrpc.client.ServerProxy(url)

            # 测试连接
            proxy.system.client_version()
            return proxy
        except Exception as err:
            stack_trace = "".join(
                traceback.format_exception(None, err, err.__traceback__)
            )[:2000]
            logger.error(f"rTorrent 连接出错：{str(err)}\n{stack_trace}")
            return None

    def is_inactive(self) -> bool:
        """
        判断是否需要重连
        """
        if not self._host:
            return False
        return True if not self._proxy else False

    def reconnect(self):
        """
        重连
        """
        self._proxy = self.__login_rtorrent()

    def get_torrents(
        self,
        ids: Optional[Union[str, list]] = None,
        status: Optional[str] = None,
        tags: Optional[Union[str, list]] = None,
    ) -> Tuple[List[Dict], bool]:
        """
        获取种子列表
        :return: 种子列表, 是否发生异常
        """
        if not self._proxy:
            return [], True
        try:
            # 使用d.multicall2获取种子列表
            fields = [
                "d.hash=",
                "d.name=",
                "d.size_bytes=",
                "d.completed_bytes=",
                "d.down.rate=",
                "d.up.rate=",
                "d.state=",
                "d.complete=",
                "d.directory=",
                "d.custom1=",
                "d.is_active=",
                "d.is_open=",
                "d.ratio=",
                "d.base_path=",
            ]
            # 获取所有种子
            results = self._proxy.d.multicall2("", "main", *fields)
            torrents = []
            for r in results:
                torrent = {
                    "hash": r[0],
                    "name": r[1],
                    "total_size": r[2],
                    "completed": r[3],
                    "dlspeed": r[4],
                    "upspeed": r[5],
                    "state": r[6],  # 0=stopped, 1=started
                    "complete": r[7],  # 0=incomplete, 1=complete
                    "save_path": r[8],
                    "tags": r[9],  # d.custom1 用于标签
                    "is_active": r[10],
                    "is_open": r[11],
                    "ratio": int(r[12]) / 1000.0 if r[12] else 0,
                    "content_path": r[13],  # base_path 即完整内容路径
                }
                # 计算进度
                if torrent["total_size"] > 0:
                    torrent["progress"] = (
                        torrent["completed"] / torrent["total_size"] * 100
                    )
                else:
                    torrent["progress"] = 0

                # ID过滤
                if ids:
                    if isinstance(ids, str):
                        ids_list = [ids.upper()]
                    else:
                        ids_list = [i.upper() for i in ids]
                    if torrent["hash"].upper() not in ids_list:
                        continue

                # 标签过滤
                if tags:
                    torrent_tags = [
                        t.strip() for t in torrent["tags"].split(",") if t.strip()
                    ]
                    if isinstance(tags, str):
                        tags_list = [t.strip() for t in tags.split(",")]
                    else:
                        tags_list = tags
                    if not set(tags_list).issubset(set(torrent_tags)):
                        continue

                torrents.append(torrent)
            return torrents, False
        except Exception as err:
            logger.error(f"获取种子列表出错：{str(err)}")
            return [], True

    def get_completed_torrents(
        self, ids: Union[str, list] = None, tags: Union[str, list] = None
    ) -> Optional[List[Dict]]:
        """
        获取已完成的种子
        """
        if not self._proxy:
            return None
        torrents, error = self.get_torrents(ids=ids, tags=tags)
        if error:
            return None
        return [t for t in torrents if t.get("complete") == 1]

    def get_downloading_torrents(
        self, ids: Union[str, list] = None, tags: Union[str, list] = None
    ) -> Optional[List[Dict]]:
        """
        获取正在下载的种子
        """
        if not self._proxy:
            return None
        torrents, error = self.get_torrents(ids=ids, tags=tags)
        if error:
            return None
        return [t for t in torrents if t.get("complete") == 0]

    def add_torrent(
        self,
        content: Union[str, bytes],
        is_paused: Optional[bool] = False,
        download_dir: Optional[str] = None,
        tags: Optional[List[str]] = None,
        cookie: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """
        添加种子
        :param content: 种子内容（bytes）或磁力链接/URL（str）
        :param is_paused: 添加后暂停
        :param download_dir: 下载路径
        :param tags: 标签列表
        :param cookie: Cookie
        :return: bool
        """
        if not self._proxy or not content:
            return False
        try:
            # 构造命令参数
            commands = []
            if download_dir:
                commands.append(f'd.directory.set="{download_dir}"')
            if tags:
                tag_str = ",".join(tags)
                commands.append(f'd.custom1.set="{tag_str}"')

            if isinstance(content, bytes):
                # 检查是否为磁力链接（bytes形式）
                if content.startswith(b"magnet:"):
                    content = content.decode("utf-8", errors="replace")
                else:
                    # 种子文件内容，使用load.raw
                    raw = xmlrpc.client.Binary(content)
                    if is_paused:
                        self._proxy.load.raw("", raw, *commands)
                    else:
                        self._proxy.load.raw_start("", raw, *commands)
                    return True

            # URL或磁力链接
            if is_paused:
                self._proxy.load.normal("", content, *commands)
            else:
                self._proxy.load.start("", content, *commands)
            return True
        except Exception as err:
            logger.error(f"添加种子出错：{str(err)}")
            return False

    def start_torrents(self, ids: Union[str, list]) -> bool:
        """
        启动种子
        """
        if not self._proxy:
            return False
        try:
            if isinstance(ids, str):
                ids = [ids]
            for tid in ids:
                self._proxy.d.start(tid)
            return True
        except Exception as err:
            logger.error(f"启动种子出错：{str(err)}")
            return False

    def stop_torrents(self, ids: Union[str, list]) -> bool:
        """
        停止种子
        """
        if not self._proxy:
            return False
        try:
            if isinstance(ids, str):
                ids = [ids]
            for tid in ids:
                self._proxy.d.stop(tid)
            return True
        except Exception as err:
            logger.error(f"停止种子出错：{str(err)}")
            return False

    def delete_torrents(self, delete_file: bool, ids: Union[str, list]) -> bool:
        """
        删除种子
        """
        if not self._proxy:
            return False
        if not ids:
            return False
        try:
            if isinstance(ids, str):
                ids = [ids]
            for tid in ids:
                if delete_file:
                    # 先获取base_path用于删除文件
                    try:
                        base_path = self._proxy.d.base_path(tid)
                        self._proxy.d.erase(tid)
                        if base_path:
                            import shutil

                            path = Path(base_path)
                            if path.is_dir():
                                shutil.rmtree(str(path), ignore_errors=True)
                            elif path.is_file():
                                path.unlink(missing_ok=True)
                    except Exception as e:
                        logger.warning(f"删除种子文件出错：{str(e)}")
                        self._proxy.d.erase(tid)
                else:
                    self._proxy.d.erase(tid)
            return True
        except Exception as err:
            logger.error(f"删除种子出错：{str(err)}")
            return False

    def get_files(self, tid: str) -> Optional[List[Dict]]:
        """
        获取种子文件列表
        """
        if not self._proxy:
            return None
        if not tid:
            return None
        try:
            files = self._proxy.f.multicall(
                tid,
                "",
                "f.path=",
                "f.size_bytes=",
                "f.priority=",
                "f.completed_chunks=",
                "f.size_chunks=",
            )
            result = []
            for idx, f in enumerate(files):
                result.append(
                    {
                        "id": idx,
                        "name": f[0],
                        "size": f[1],
                        "priority": f[2],
                        "progress": int(f[3]) / int(f[4]) * 100 if int(f[4]) > 0 else 0,
                    }
                )
            return result
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{str(err)}")
            return None

    def set_files(
        self, torrent_hash: str = None, file_ids: list = None, priority: int = 0
    ) -> bool:
        """
        设置下载文件的优先级，priority为0为不下载，priority为1为普通
        """
        if not self._proxy:
            return False
        if not torrent_hash or not file_ids:
            return False
        try:
            for file_id in file_ids:
                self._proxy.f.priority.set(f"{torrent_hash}:f{file_id}", priority)
            # 更新种子优先级
            self._proxy.d.update_priorities(torrent_hash)
            return True
        except Exception as err:
            logger.error(f"设置种子文件状态出错：{str(err)}")
            return False

    def set_torrents_tag(
        self, ids: Union[str, list], tags: List[str], overwrite: bool = False
    ) -> bool:
        """
        设置种子标签（使用d.custom1）
        :param ids: 种子Hash
        :param tags: 标签列表
        :param overwrite: 是否覆盖现有标签，默认为合并
        """
        if not self._proxy:
            return False
        if not ids:
            return False
        try:
            if isinstance(ids, str):
                ids = [ids]
            for tid in ids:
                if overwrite:
                    # 直接覆盖标签
                    self._proxy.d.custom1.set(tid, ",".join(tags))
                else:
                    # 获取现有标签
                    existing = self._proxy.d.custom1(tid)
                    existing_tags = (
                        [t.strip() for t in existing.split(",") if t.strip()]
                        if existing
                        else []
                    )
                    # 合并标签
                    merged = list(set(existing_tags + tags))
                    self._proxy.d.custom1.set(tid, ",".join(merged))
            return True
        except Exception as err:
            logger.error(f"设置种子Tag出错：{str(err)}")
            return False

    def remove_torrents_tag(self, ids: Union[str, list], tag: Union[str, list]) -> bool:
        """
        移除种子标签
        """
        if not self._proxy:
            return False
        if not ids:
            return False
        try:
            if isinstance(ids, str):
                ids = [ids]
            if isinstance(tag, str):
                tag = [tag]
            for tid in ids:
                existing = self._proxy.d.custom1(tid)
                existing_tags = (
                    [t.strip() for t in existing.split(",") if t.strip()]
                    if existing
                    else []
                )
                new_tags = [t for t in existing_tags if t not in tag]
                self._proxy.d.custom1.set(tid, ",".join(new_tags))
            return True
        except Exception as err:
            logger.error(f"移除种子Tag出错：{str(err)}")
            return False

    def get_torrent_tags(self, ids: str) -> List[str]:
        """
        获取种子标签
        """
        if not self._proxy:
            return []
        try:
            existing = self._proxy.d.custom1(ids)
            return (
                [t.strip() for t in existing.split(",") if t.strip()]
                if existing
                else []
            )
        except Exception as err:
            logger.error(f"获取种子标签出错：{str(err)}")
            return []

    def get_torrent_id_by_tag(
        self, tags: Union[str, list], status: Optional[str] = None
    ) -> Optional[str]:
        """
        通过标签多次尝试获取刚添加的种子ID，并移除标签
        """
        import time

        if isinstance(tags, str):
            tags = [tags]
        torrent_id = None
        for i in range(1, 10):
            time.sleep(3)
            torrents, error = self.get_torrents(tags=tags)
            if not error and torrents:
                torrent_id = torrents[0].get("hash")
                # 移除查找标签
                for tag in tags:
                    self.remove_torrents_tag(ids=torrent_id, tag=[tag])
                break
        return torrent_id

    @staticmethod
    def __build_throttle_name(torrent_hash: str) -> str:
        """
        生成单任务限速组名称。
        """
        return f"mp_{torrent_hash.lower()[:16]}"

    def change_torrent(
            self,
            hash_string: str,
            upload_limit: Optional[float] = None,
            download_limit: Optional[float] = None,
    ) -> bool:
        """
        修改单个种子的上传和下载限速。
        :param hash_string: 种子Hash
        :param upload_limit: 上传限速，单位 KB/s，0 表示不限速
        :param download_limit: 下载限速，单位 KB/s，0 表示不限速
        :return: 是否修改成功
        """
        if not self._proxy or not hash_string:
            return False
        try:
            throttle_name = self.__build_throttle_name(hash_string)
            if download_limit is not None:
                self._proxy.throttle.down.max.set(
                    throttle_name,
                    int(float(download_limit) * 1024),
                )
            if upload_limit is not None:
                self._proxy.throttle.up.max.set(
                    throttle_name,
                    int(float(upload_limit) * 1024),
                )
            self._proxy.d.throttle_name.set(hash_string, throttle_name)
            return True
        except Exception as err:
            logger.error(f"设置种子限速出错：{str(err)}")
            return False

    def set_torrent_location(self, hash_string: str, location: str) -> bool:
        """
        修改种子保存目录。
        :param hash_string: 种子Hash
        :param location: 新保存目录
        :return: 是否修改成功
        """
        if not self._proxy or not hash_string or not location:
            return False
        try:
            self._proxy.d.directory.set(hash_string, location)
            return True
        except Exception as err:
            logger.error(f"设置种子保存目录出错：{str(err)}")
            return False

    def transfer_info(self) -> Optional[Dict]:
        """
        获取传输信息
        """
        if not self._proxy:
            return None
        try:
            return {
                "dl_info_speed": self._proxy.throttle.global_down.rate(),
                "up_info_speed": self._proxy.throttle.global_up.rate(),
                "dl_info_data": self._proxy.throttle.global_down.total(),
                "up_info_data": self._proxy.throttle.global_up.total(),
            }
        except Exception as err:
            logger.error(f"获取传输信息出错：{str(err)}")
            return None
