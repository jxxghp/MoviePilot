"""
rTorrent XML-RPC 客户端及 SCGI 传输，负责下载器响应映射。
"""

import http.client
import io
import socket
import traceback
import xmlrpc.client
from collections.abc import Buffer, Callable
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union, cast
from urllib.parse import urlparse

from app.runtime.log import logger


class TorrentData(TypedDict):
    """
    rTorrent 多调用结果映射，保留现有下载器字典字段。

    :ivar hash: 种子哈希，供任务操作和筛选使用
    :ivar name: 下载器中的任务名称
    :ivar total_size: 总字节数
    :ivar completed: 已下载字节数
    :ivar dlspeed: 当前下载速率，字节每秒
    :ivar upspeed: 当前上传速率，字节每秒
    :ivar state: 启动状态，0 为停止，1 为启动
    :ivar complete: 完成标记，0 为未完成，1 为完成
    :ivar save_path: 保存目录
    :ivar tags: custom1 存储的逗号分隔标签
    :ivar is_active: 下载器返回的活动状态
    :ivar is_open: 下载器返回的文件打开状态
    :ivar ratio: 从千分比转换后的分享率
    :ivar content_path: 完整内容路径；下载器未提供时为 None，由调用端回退计算
    :ivar progress: 下载进度百分比，零大小任务为 0
    """

    hash: str
    name: str
    total_size: Union[int, float]
    completed: Union[int, float]
    dlspeed: Union[int, float]
    upspeed: Union[int, float]
    state: Union[int, float]
    complete: Union[int, float]
    save_path: str
    tags: str
    is_active: Union[int, float]
    is_open: Union[int, float]
    ratio: float
    content_path: Optional[str]
    progress: float


class TorrentFileData(TypedDict):
    """
    rTorrent 文件索引和分块进度的字典合同。

    :ivar id: 文件在种子中的零基索引
    :ivar name: 种子内文件路径
    :ivar size: 文件字节数
    :ivar priority: 下载优先级，0 表示不下载
    :ivar progress: 按已完成分块计算的百分比
    """

    id: int
    name: str
    size: Union[int, float]
    priority: Union[int, float]
    progress: float


def _rpc_rows(value: object) -> list[list[object]]:
    """
    校验 XML-RPC 多调用的二维数组，异常仍由调用方原有错误分支处理。
    """
    if not isinstance(value, (list, tuple)):
        raise TypeError("rTorrent multicall response must be an array")
    rows: list[list[object]] = []
    for row in value:
        if not isinstance(row, (list, tuple)):
            raise TypeError("rTorrent multicall row must be an array")
        rows.append(list(row))
    return rows


def _rpc_text(value: object) -> str:
    """
    校验外部返回的文本，不将意外对象强转为路径或标签。
    """
    if not isinstance(value, str):
        raise TypeError("rTorrent response field must be text")
    return value


def _rpc_optional_text(value: object) -> Optional[str]:
    """
    保留下载器空路径返回，使调用端继续回退到保存目录和任务名。
    """
    if value is None:
        return None
    return _rpc_text(value)


def _rpc_number(value: object) -> Union[int, float]:
    """
    保留 XML-RPC 数值原值，拒绝不能执行原有算术的字段。
    """
    if not isinstance(value, (int, float)):
        raise TypeError("rTorrent response field must be numeric")
    return value


def _rpc_integer(value: object) -> int:
    """
    保留比率和分块字段原有的 int 转换，兼容数字字符串。
    """
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise TypeError("rTorrent response field must be convertible to int")
    return int(value)


class SCGITransport(xmlrpc.client.Transport):
    """
    通过 SCGI 传输 XML-RPC 报文，为 rTorrent 提供直连通道。
    """

    def single_request(
        self, host: Union[str, tuple[str, dict[str, str]]], handler: str,
        request_body: Buffer, verbose: bool = False,
    ) -> tuple[Any, ...]:
        """
        通过临时连接交换 SCGI 报文，并在解析响应前关闭连接。

        :param host: 目标主机及端口，省略端口时使用 5000
        :param handler: 为 XML-RPC 传输接口保留，实际请求路径固定为 /RPC2
        :param request_body: 已经编码的 XML-RPC 请求字节
        :param verbose: 是否启用标准库响应解析的调试输出
        :return: 标准库解码后的 XML-RPC 返回值；连接、协议和服务端故障由调用方处理
        """
        request_body = bytes(request_body)
        if isinstance(host, tuple):
            host = host[0]
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
            sock.sendall(netstring + request_body)
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
        self.verbose = verbose
        return self.parse_response(self._build_response(response))

    @staticmethod
    def _build_response(data: bytes) -> http.client.HTTPResponse:
        """
        构造类文件对象用于parse_response。
        """
        class _FakeSocket(io.BytesIO):
            """
            为 HTTPResponse 提供内存 socket 接口，复用标准库 XML-RPC 解码。
            """

            def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
                """
                返回当前字节流供 HTTPResponse 读取。
                """
                return self

        raw = b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\n\r\n" + data
        # HTTPResponse 只调用 makefile；内存适配器由上方实现该接口。
        response = http.client.HTTPResponse(cast(socket.socket, _FakeSocket(raw)))
        response.begin()
        return response


class Rtorrent:
    """
    Rtorrent 下载器客户端，封装连接、任务查询和管理操作。
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        """
        根据显式配置连接 rTorrent；连接失败时保留待重连状态。

        :param host: HTTP 代理地址或 scgi:// 直连地址；缺少时不创建代理
        :param port: 可选端口，提供时追加到 host 后
        :param username: HTTP 代理认证用户名，SCGI 模式不使用
        :param password: HTTP 代理认证密码，SCGI 模式不使用
        :param kwargs: 为统一服务配置保留的额外参数，当前不读取
        """
        self._proxy = None
        self._host = ""
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
        连接rTorrent。
        """
        if not self._host:
            return None
        try:
            url = self._host
            if url.startswith("scgi://"):
                # SCGI直连模式
                logger.info(f"正在通过SCGI连接 rTorrent：{url}")
                # ServerProxy 仅接受 HTTP 协议地址；该 URI 只用于解析地址，实际报文仍由 SCGITransport 发送。
                proxy_url = "http://" + url[len("scgi://"):]
                proxy = xmlrpc.client.ServerProxy(proxy_url, transport=SCGITransport())
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
        判断已配置的下载器是否需要重新连接。

        :return: 连接配置存在但客户端未建立时返回 True；缺少配置时返回 False
        """
        if not self._host:
            return False
        return True if not self._proxy else False

    def reconnect(self) -> None:
        """
        按当前连接配置重建客户端；失败时保留未连接状态。
        """
        self._proxy = self.__login_rtorrent()

    def get_torrents(
        self,
        ids: Optional[Union[str, list[str]]] = None,
        status: Optional[str] = None,
        tags: Optional[Union[str, list[str]]] = None,
    ) -> Tuple[List[TorrentData], bool]:
        """
        查询种子任务并按标识和标签筛选。

        :param ids: 种子哈希或哈希列表，省略时查询全部任务
        :param status: 保留的接口参数，当前实现不按此参数筛选
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 任务列表和错误标志；未连接或查询失败时返回空列表及 True，查询无匹配项时错误标志为 False
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
            results = self._proxy.d.multicall2("", "main", *fields)
            torrents: list[TorrentData] = []
            for r in _rpc_rows(results):
                torrent: TorrentData = {
                    "hash": _rpc_text(r[0]),
                    "name": _rpc_text(r[1]),
                    "total_size": _rpc_number(r[2]),
                    "completed": _rpc_number(r[3]),
                    "dlspeed": _rpc_number(r[4]),
                    "upspeed": _rpc_number(r[5]),
                    "state": _rpc_number(r[6]),
                    "complete": _rpc_number(r[7]),
                    "save_path": _rpc_text(r[8]),
                    "tags": _rpc_text(r[9]),
                    "is_active": _rpc_number(r[10]),
                    "is_open": _rpc_number(r[11]),
                    "ratio": _rpc_integer(r[12]) / 1000.0 if r[12] else 0,
                    "progress": 0,
                    "content_path": _rpc_optional_text(r[13]),
                }
                if torrent["total_size"] > 0:
                    torrent["progress"] = (
                        torrent["completed"] / torrent["total_size"] * 100
                    )
                else:
                    torrent["progress"] = 0

                if ids:
                    if isinstance(ids, str):
                        ids_list = [ids.upper()]
                    else:
                        ids_list = [i.upper() for i in ids]
                    if torrent["hash"].upper() not in ids_list:
                        continue

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
        self, ids: Optional[Union[str, list[str]]] = None, tags: Optional[Union[str, list[str]]] = None
    ) -> Optional[List[TorrentData]]:
        """
        获取已完成的种子任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
        """
        if not self._proxy:
            return None
        torrents, error = self.get_torrents(ids=ids, tags=tags)
        if error:
            return None
        return [t for t in torrents if t.get("complete") == 1]

    def get_downloading_torrents(
        self, ids: Optional[Union[str, list[str]]] = None, tags: Optional[Union[str, list[str]]] = None
    ) -> Optional[List[TorrentData]]:
        """
        获取尚未完成的种子任务。

        :param ids: 种子哈希或哈希列表，省略时不限任务
        :param tags: 必须同时包含的标签；字符串按逗号分隔，省略时不按标签筛选
        :return: 匹配的任务列表；未连接或查询失败时返回 None，无匹配项时返回空列表
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
        **kwargs: object,
    ) -> bool:
        """
        通过 XML-RPC 添加种子任务。

        :param content: 种子文件字节、磁力链接或 URL；磁力链接的字节形式先解码为字符串
        :param is_paused: 是否以暂停状态添加
        :param download_dir: 下载器保存目录，省略时不覆盖下载器配置
        :param tags: 写入 d.custom1 的标签列表
        :param cookie: 兼容统一下载接口的站点 Cookie，当前实现不使用
        :param kwargs: 兼容统一调用的额外参数，当前实现不使用
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self._proxy or not content:
            return False
        try:
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

    def start_torrents(self, ids: Union[str, list[str]]) -> bool:
        """
        启动指定种子任务。

        批量操作按任务依次发送，后续失败不会撤销此前已完成的请求。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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

    def stop_torrents(self, ids: Union[str, list[str]]) -> bool:
        """
        暂停指定种子任务。

        批量操作按任务依次发送，后续失败不会撤销此前已完成的请求。

        :param ids: 种子哈希或哈希列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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

    def delete_torrents(self, delete_file: bool, ids: Union[str, list[str]],
                        *, path_mapper: Optional[Callable[[Path], str]] = None) -> bool:
        """
        删除指定种子任务，并按选项删除下载文件。

        先将rTorrent内容路径映射到MoviePilot本地文件系统，文件清理成功后才移除任务。
        清理失败或映射为远程存储时保留任务，以便用户核对和重试。

        :param delete_file: 是否同时删除下载文件
        :param ids: 种子哈希或哈希列表
        :param path_mapper: 将下载器内容路径映射为宿主本地路径；省略时保留直接客户端调用合同
        :return: 文件清理及任务删除均完成时返回 True，失败时返回 False
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
                    base_path = self._proxy.d.base_path(tid)
                    if not base_path:
                        logger.warning("下载器未返回文件路径，保留任务以便人工处理")
                        return False
                    path = Path(_rpc_text(base_path))
                    mapped = path_mapper(path) if path_mapper else path.as_posix()
                    # 云存储URI不能交给本地Path删除；下载器任务也保留以便核对。
                    if ":" in mapped and not (len(mapped) > 2 and mapped[1] == ":"):
                        logger.warning("下载器文件映射为远程存储，拒绝本地删除")
                        return False
                    path = Path(mapped)
                    if path.is_dir():
                        import shutil
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                self._proxy.d.erase(tid)
            return True
        except Exception as err:
            logger.error(f"删除种子出错：{str(err)}")
            return False

    def get_files(self, tid: str) -> Optional[List[TorrentFileData]]:
        """
        读取任务的文件大小、下载优先级和完成进度。

        :param tid: 种子哈希
        :return: 文件字典列表；size 单位为字节，progress 为 0 至 100 的百分比；
            哈希为空、未连接或查询失败时返回 None
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
            result: list[TorrentFileData] = []
            for idx, f in enumerate(_rpc_rows(files)):
                result.append(
                    {
                        "id": idx,
                        "name": _rpc_text(f[0]),
                        "size": _rpc_number(f[1]),
                        "priority": _rpc_number(f[2]),
                        "progress": _rpc_integer(f[3]) / _rpc_integer(f[4]) * 100 if _rpc_integer(f[4]) > 0 else 0,
                    }
                )
            return result
        except Exception as err:
            logger.error(f"获取种子文件列表出错：{str(err)}")
            return None

    def set_files(
        self, torrent_hash: Optional[str] = None, file_ids: Optional[Union[list[int], list[str]]] = None, priority: int = 0
    ) -> bool:
        """
        批量设置文件优先级并通知任务更新优先级。

        :param torrent_hash: 种子哈希，不接受空值
        :param file_ids: 种子内部的非空文件索引列表
        :param priority: 文件优先级，0 表示不下载，1 表示普通下载
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self._proxy:
            return False
        if not torrent_hash or not file_ids:
            return False
        try:
            for file_id in file_ids:
                self._proxy.f.priority.set(f"{torrent_hash}:f{file_id}", priority)
            self._proxy.d.update_priorities(torrent_hash)
            return True
        except Exception as err:
            logger.error(f"设置种子文件状态出错：{str(err)}")
            return False

    def set_torrents_tag(
        self, ids: Union[str, list[str]], tags: List[str], overwrite: bool = False
    ) -> bool:
        """
        将任务标签写入 rTorrent 的 d.custom1 字段。

        :param ids: 种子哈希或哈希列表
        :param tags: 待写入的标签列表
        :param overwrite: 是否覆盖已有标签；默认合并并去重
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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
                    self._proxy.d.custom1.set(tid, ",".join(tags))
                else:
                    existing = self._proxy.d.custom1(tid)
                    existing_tags = (
                        [t.strip() for t in _rpc_text(existing).split(",") if t.strip()]
                        if existing
                        else []
                    )
                    merged = list(set(existing_tags + tags))
                    self._proxy.d.custom1.set(tid, ",".join(merged))
            return True
        except Exception as err:
            logger.error(f"设置种子Tag出错：{str(err)}")
            return False

    def remove_torrents_tag(self, ids: Union[str, list[str]], tag: Union[str, list[str]]) -> bool:
        """
        移除指定任务的标签，保留其他标签。

        :param ids: 种子哈希或哈希列表
        :param tag: 待移除的单个标签或标签列表
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
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
                    [t.strip() for t in _rpc_text(existing).split(",") if t.strip()]
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
        读取单个种子在 d.custom1 中保存的标签。

        :param ids: 单个种子哈希
        :return: 标签列表；未连接、标签为空或查询失败时返回空列表
        """
        if not self._proxy:
            return []
        try:
            existing = self._proxy.d.custom1(ids)
            return (
                [t.strip() for t in _rpc_text(existing).split(",") if t.strip()]
                if existing
                else []
            )
        except Exception as err:
            logger.error(f"获取种子标签出错：{str(err)}")
            return []

    def get_torrent_id_by_tag(
        self, tags: Union[str, list[str]], status: Optional[str] = None
    ) -> Optional[str]:
        """
        通过临时标签查找新任务，找到后尝试移除查询标签。

        每次查询前等待 3 秒；移除临时标签失败不影响返回已找到的哈希。

        :param tags: 需要同时匹配的临时标签；字符串按单个标签处理
        :param status: 保留的兼容参数，当前不参与筛选
        :return: 首个匹配任务的哈希；最多查询 9 次仍未找到时返回 None
        """
        import time

        if isinstance(tags, str):
            tags = [tags]
        torrent_id = None
        for i in range(1, 10):
            time.sleep(3)
            torrents, error = self.get_torrents(tags=tags)
            if not error and torrents:
                torrent_id = torrents[0]["hash"]
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
        修改种子任务的保存目录。

        仅修改 rTorrent 的目录配置，不负责移动已有文件。

        :param hash_string: 种子哈希
        :param location: 下载器可访问的新目录路径，不接受空值
        :return: 请求完成时返回 True；未连接或请求失败时返回 False，不代表任务已完成该动作
        """
        if not self._proxy or not hash_string or not location:
            return False
        try:
            self._proxy.d.directory.set(hash_string, location)
            return True
        except Exception as err:
            logger.error(f"设置种子保存目录出错：{str(err)}")
            return False

    def transfer_info(self) -> Optional[Dict[str, object]]:
        """
        获取下载器全局传输统计。

        :return: 包含上下行速度（字节/秒）与累计传输量（字节）的字典；未连接或查询失败时返回 None
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
