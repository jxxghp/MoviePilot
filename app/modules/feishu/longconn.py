"""
飞书事件长连接（WebSocket）客户端。

协议与 lark-oapi 1.6 ``lark_oapi.ws.Client`` 一致：先以应用凭据换取连接地址与客户端配置，
再通过 WebSocket 收发 pbbp2 ``Frame`` 二进制帧。CONTROL 帧承载 ping/pong 心跳，DATA 帧承载
事件；事件处理完成后把原帧改写为应答帧回传，其中 payload 为 ``{"code": 200, "data": base64}``。

整个连接在一个线程内同步运行：阻塞 recv 以心跳间隔为超时，超时即发送 ping，因此收发都在
同一线程，无需发送锁或事件循环。
"""

import base64
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

import websocket

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

# 本模块不引用同包的 openapi，避免子模块经父包形成导入环；域名默认值与其保持一致。
FEISHU_DOMAIN = "https://open.feishu.cn"
# 事件报文与应答结果的 JSON 结构。
JsonDict = Dict[str, Any]
# 事件处理回调：入参为事件 JSON 原文，返回值（如卡片回调的 toast）会编码进应答帧。
EventHandler = Callable[[bytes], Optional[JsonDict]]

# 以应用凭据换取 WebSocket 连接地址的接口路径。
_ENDPOINT_PATH = "/callback/ws/endpoint"
# pbbp2 Frame.method
_FRAME_CONTROL = 0
_FRAME_DATA = 1
# 连接地址获取与握手失败时的错误码，含义同 lark-oapi ws.const。
_CODE_OK = 0
_CODE_SYSTEM_BUSY = 1
_CODE_FORBIDDEN = 403
_CODE_AUTH_FAILED = 514
_CODE_INTERNAL_ERROR = 1000040343
_CODE_EXCEED_CONN_LIMIT = 1000040350
# 分包事件的合包等待时间。
_COMBINE_TTL_SECONDS = 5
# 获取连接地址与 WebSocket 握手各自的超时秒数。
_CONNECT_TIMEOUT_SECONDS = 30
# 发送 ping 时若超过「心跳间隔 + 宽限」仍未收到任何入站帧，判定连接已静默断开并重连；
# 服务端对每个 ping 回 pong，正常连接不会触发。对应原 SDK 依赖 websockets 默认 ping 超时的检测。
_LIVENESS_GRACE_SECONDS = 30


class FatalConnectionError(Exception):
    """凭据错误、无权限或连接数超限等重试无意义的失败，长连接线程据此退出。"""


# ---------------------------------------------------------------- pbbp2 帧编解码


def _encode_varint(value: int) -> bytes:
    """编码 protobuf varint；负数按 64 位补码处理（int32 负值规则）。"""
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """从 ``pos`` 解码一个 varint，返回 (值, 下一个位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 截断")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint 过长")


def _to_int32(value: int) -> int:
    """把 varint 解码结果还原为有符号 int32。"""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def _encode_bytes_field(number: int, value: bytes) -> bytes:
    """编码 length-delimited 字段。"""
    return _encode_varint(number << 3 | 2) + _encode_varint(len(value)) + value


def _iter_fields(data: bytes) -> Iterator[Tuple[int, int, Union[int, bytes]]]:
    """遍历 protobuf 消息字段，产出 (字段号, wire type, 值)；未知的定长字段按类型跳过。"""
    pos = 0
    while pos < len(data):
        key, pos = _decode_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if wire_type == 0:
            value, pos = _decode_varint(data, pos)
            yield number, wire_type, value
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("字段长度越界")
            yield number, wire_type, data[pos:pos + length]
            pos += length
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"不支持的 wire type：{wire_type}")


@dataclass
class Frame:
    """
    pbbp2.Frame（proto2）。

    SeqID/LogID/service/method 为 required 字段，编码时总是写出；其余 optional 字段为 None
    时不写出，以便应答帧原样保留服务端下发的字段。
    """

    seq_id: int = 0  # SeqID，uint64
    log_id: int = 0  # LogID，uint64
    service: int = 0  # service，int32，取自连接地址的 service_id
    method: int = 0  # method，int32，0 为 CONTROL，1 为 DATA
    headers: List[Tuple[str, str]] = field(default_factory=list)  # 有序键值头，如 type/message_id/sum/seq
    payload_encoding: Optional[str] = None  # payload_encoding，负载编码，服务端当前不下发
    payload_type: Optional[str] = None  # payload_type，负载类型，服务端当前不下发
    payload: Optional[bytes] = None  # payload，事件 JSON、pong 配置或应答 JSON
    log_id_new: Optional[str] = None  # LogIDNew，服务端链路追踪 ID，应答时原样回传

    def header(self, key: str) -> Optional[str]:
        """返回首个同名头的值。"""
        for name, value in self.headers:
            if name == key:
                return value
        return None

    def encode(self) -> bytes:
        """序列化为 protobuf 二进制。"""
        out = bytearray()
        for number, scalar in ((1, self.seq_id), (2, self.log_id), (3, self.service), (4, self.method)):
            out += _encode_varint(number << 3) + _encode_varint(scalar)
        for key, value in self.headers:
            header = _encode_bytes_field(1, key.encode("utf-8")) + _encode_bytes_field(2, value.encode("utf-8"))
            out += _encode_bytes_field(5, header)
        for number, text in ((6, self.payload_encoding), (7, self.payload_type)):
            if text is not None:
                out += _encode_bytes_field(number, text.encode("utf-8"))
        if self.payload is not None:
            out += _encode_bytes_field(8, self.payload)
        if self.log_id_new is not None:
            out += _encode_bytes_field(9, self.log_id_new.encode("utf-8"))
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> "Frame":
        """从 protobuf 二进制解析帧。"""
        frame = cls()
        for number, wire_type, value in _iter_fields(data):
            if wire_type == 0 and isinstance(value, int):
                if number == 1:
                    frame.seq_id = value
                elif number == 2:
                    frame.log_id = value
                elif number == 3:
                    frame.service = _to_int32(value)
                elif number == 4:
                    frame.method = _to_int32(value)
            elif wire_type == 2 and isinstance(value, bytes):
                if number == 5:
                    header = {n: v for n, t, v in _iter_fields(value) if t == 2}
                    frame.headers.append((
                        bytes(header.get(1, b"")).decode("utf-8"),
                        bytes(header.get(2, b"")).decode("utf-8"),
                    ))
                elif number == 6:
                    frame.payload_encoding = value.decode("utf-8")
                elif number == 7:
                    frame.payload_type = value.decode("utf-8")
                elif number == 8:
                    frame.payload = value
                elif number == 9:
                    frame.log_id_new = value.decode("utf-8")
        return frame


# ---------------------------------------------------------------- 长连接客户端


@dataclass
class _ClientConfig:
    """服务端下发的重连与心跳配置，单位为秒；默认值同 lark-oapi。"""

    reconnect_count: int = -1  # 连续重连次数上限，负数表示不限
    reconnect_interval: int = 120  # 两次重连尝试之间的间隔
    reconnect_nonce: int = 30  # 首次重连前的随机抖动上限
    ping_interval: int = 120  # 心跳间隔

    def update(self, conf: JsonDict) -> None:
        """合并服务端 ClientConfig，缺失字段保持原值。"""
        for attr, key in (
                ("reconnect_count", "ReconnectCount"),
                ("reconnect_interval", "ReconnectInterval"),
                ("reconnect_nonce", "ReconnectNonce"),
                ("ping_interval", "PingInterval"),
        ):
            value = conf.get(key)
            if isinstance(value, int):
                setattr(self, attr, value)


class FeishuLongConnection:
    """
    单个应用的事件长连接。``run()`` 在调用线程阻塞运行并自动重连，``stop()`` 可从任意线程
    调用：设置停止标记并中断阻塞的 recv，``run()`` 随即返回。
    """

    def __init__(
            self,
            app_id: str,
            app_secret: str,
            on_event: EventHandler,
            domain: str = FEISHU_DOMAIN,
            name: str = "feishu",
    ):
        """
        :param app_id: 自建应用 App ID
        :param app_secret: 自建应用 App Secret
        :param on_event: 事件处理回调，在长连接线程内同步调用
        :param domain: 开放平台域名
        :param name: 渠道配置名，仅用于日志
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_event = on_event
        self._domain = domain.rstrip("/")
        self._name = name
        self._config = _ClientConfig()
        self._stop_event = threading.Event()
        self._ws_lock = threading.Lock()
        self._ws: Optional[websocket.WebSocket] = None
        # 分包事件缓存：message_id -> (过期时间, 各分片)
        self._fragments: Dict[str, Tuple[float, List[Optional[bytes]]]] = {}
        # 最近一次连接是否建立成功；run() 据此把「连上后断开」与「连接失败」区分开。
        self._connected = False

    # ------------------------------------------------------------ 生命周期

    def run(self) -> None:
        """连接并处理事件直到 ``stop()``；断线后按服务端配置抖动重连，致命错误时退出。"""
        # 连续连接失败次数；一次成功建立的连接断开后重新从抖动重连开始，与 SDK 每轮 _reconnect 一致。
        failures = 0
        while not self._stop_event.is_set():
            self._connected = False
            try:
                self._run_once()
            except FatalConnectionError as err:
                logger.error("飞书长连接 %s 无法建立，停止重连：%s", self._name, err)
                return
            except Exception as err:
                if self._stop_event.is_set():
                    return
                logger.warning("飞书长连接 %s 断开或连接失败：%s", self._name, err)
            failures = 1 if self._connected else failures + 1
            if self._stop_event.is_set():
                return
            reconnect_count = self._config.reconnect_count
            if 0 <= reconnect_count < failures:
                logger.error("飞书长连接 %s 连续 %s 次连接失败，停止重连", self._name, reconnect_count)
                return
            if failures <= 1:
                delay = random.random() * max(self._config.reconnect_nonce, 0)
            else:
                delay = max(self._config.reconnect_interval, 1)
            logger.info("飞书长连接 %s 将在 %.0f 秒后重连", self._name, delay)
            if self._stop_event.wait(delay):
                return

    def stop(self) -> None:
        """请求停止，并中断正在阻塞的连接或接收。"""
        self._stop_event.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                # abort 关闭底层套接字，唤醒阻塞在 recv 的长连接线程。
                ws.abort()
            except Exception as err:
                logger.debug(f"中断飞书长连接失败：{err}")

    # ------------------------------------------------------------ 连接

    def _fetch_endpoint(self) -> str:
        """以应用凭据换取 WebSocket 地址，并应用服务端下发的客户端配置。"""
        response = RequestUtils(
            headers={"locale": "zh", "Content-Type": "application/json"},
            timeout=_CONNECT_TIMEOUT_SECONDS,
            verify=True,
        ).post_res(
            f"{self._domain}{_ENDPOINT_PATH}",
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
        )
        if response is None:
            raise ConnectionError("获取长连接地址无响应")
        if response.status_code != 200:
            raise ConnectionError(f"获取长连接地址失败：HTTP {response.status_code}")
        body = response.json()
        code = body.get("code")
        if code in (_CODE_SYSTEM_BUSY, _CODE_INTERNAL_ERROR):
            raise ConnectionError(f"获取长连接地址失败：code={code}, msg={body.get('msg')}")
        if code != _CODE_OK:
            raise FatalConnectionError(f"获取长连接地址失败：code={code}, msg={body.get('msg')}")
        data = body.get("data") or {}
        if isinstance(data.get("ClientConfig"), dict):
            self._config.update(data["ClientConfig"])
        url = data.get("URL")
        if not url:
            raise ConnectionError("长连接地址为空")
        return str(url)

    @staticmethod
    def _raise_for_handshake(err: websocket.WebSocketBadStatusException) -> None:
        """按握手响应头区分致命失败（无权限、连接数超限）与可重试失败。"""
        headers = {str(k).lower(): v for k, v in (err.resp_headers or {}).items()}
        status = headers.get("handshake-status")
        message = headers.get("handshake-msg")
        if status is None:
            raise ConnectionError(f"长连接握手失败：HTTP {err.status_code}")
        code = int(status)
        if code == _CODE_FORBIDDEN or (
                code == _CODE_AUTH_FAILED
                and str(headers.get("handshake-autherrcode")) == str(_CODE_EXCEED_CONN_LIMIT)
        ):
            raise FatalConnectionError(f"长连接握手被拒绝：code={code}, msg={message}")
        raise ConnectionError(f"长连接握手失败：code={code}, msg={message}")

    def _run_once(self) -> None:
        """建立一次连接并收发帧，连接关闭或出错时抛出异常返回。"""
        url = self._fetch_endpoint()
        query = parse_qs(urlparse(url).query)
        conn_id = (query.get("device_id") or [""])[0]
        service_id = int((query.get("service_id") or ["0"])[0])
        if self._stop_event.is_set():
            return
        try:
            # 地址查询串含连接凭据，只记录 conn_id，不输出完整地址。
            # 与 lark-oapi 一致直连，不读取 http(s)_proxy 环境变量。
            ws = websocket.create_connection(
                url,
                timeout=_CONNECT_TIMEOUT_SECONDS,
                skip_utf8_validation=True,
                enable_multithread=True,
                http_no_proxy=["*"],
            )
        except websocket.WebSocketBadStatusException as err:
            self._raise_for_handshake(err)
            raise
        with self._ws_lock:
            if self._stop_event.is_set():
                ws.close(timeout=1)
                return
            self._ws = ws
        self._connected = True
        logger.info("飞书长连接 %s 已连接：conn_id=%s", self._name, conn_id)
        try:
            self._serve(ws, service_id)
        finally:
            with self._ws_lock:
                self._ws = None
            try:
                ws.close(timeout=1)
            except Exception as err:
                logger.debug(f"关闭飞书长连接失败：{err}")
            logger.info("飞书长连接 %s 已断开：conn_id=%s", self._name, conn_id)

    def _serve(self, ws: websocket.WebSocket, service_id: int) -> None:
        """收发循环：recv 以距下次心跳的时间为超时，超时即发送 ping；长时间无入站帧时判定断线。"""
        next_ping = time.monotonic()
        last_received = next_ping
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_ping:
                silent = now - last_received
                if silent > max(self._config.ping_interval, 1) + _LIVENESS_GRACE_SECONDS:
                    raise ConnectionError(f"长连接 {silent:.0f} 秒未收到服务端数据")
                ping = Frame(service=service_id, method=_FRAME_CONTROL, headers=[("type", "ping")])
                ws.send(ping.encode(), opcode=websocket.ABNF.OPCODE_BINARY)
                next_ping = now + max(self._config.ping_interval, 1)
            ws.settimeout(max(next_ping - time.monotonic(), 0.1))
            try:
                opcode, data = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            last_received = time.monotonic()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                raise ConnectionError("服务端关闭了长连接")
            if opcode != websocket.ABNF.OPCODE_BINARY:
                continue
            reply = self._handle_frame(data)
            if reply is not None:
                ws.send(reply, opcode=websocket.ABNF.OPCODE_BINARY)

    # ------------------------------------------------------------ 帧处理

    def _handle_frame(self, data: bytes) -> Optional[bytes]:
        """处理一帧，返回需要回写的应答帧；解析或处理失败只记录日志，不中断连接。"""
        try:
            frame = Frame.decode(data)
            if frame.method == _FRAME_CONTROL:
                self._handle_control_frame(frame)
                return None
            if frame.method == _FRAME_DATA:
                return self._handle_data_frame(frame)
        except Exception as err:
            logger.error("飞书长连接 %s 处理消息失败：%s", self._name, err)
        return None

    def _handle_control_frame(self, frame: Frame) -> None:
        """pong 帧可能携带新的客户端配置。"""
        if frame.header("type") == "pong" and frame.payload:
            conf = json.loads(frame.payload.decode("utf-8"))
            if isinstance(conf, dict):
                self._config.update(conf)

    def _combine(self, message_id: str, total: int, seq: int, part: bytes) -> Optional[bytes]:
        """缓存分包，全部到齐时返回拼接后的负载；过期未齐的分包被丢弃。"""
        now = time.monotonic()
        for key in [k for k, (expires, _) in self._fragments.items() if expires < now]:
            del self._fragments[key]
        _, parts = self._fragments.get(message_id, (0.0, [None] * total))
        if len(parts) != total or not 0 <= seq < total:
            return None
        parts[seq] = part
        if any(item is None for item in parts):
            self._fragments[message_id] = (now + _COMBINE_TTL_SECONDS, parts)
            return None
        self._fragments.pop(message_id, None)
        return b"".join(item for item in parts if item is not None)

    def _handle_data_frame(self, frame: Frame) -> Optional[bytes]:
        """分发事件帧并构造应答帧；card 类型帧与 lark-oapi 一致不处理也不应答。"""
        message_type = frame.header("type")
        payload = frame.payload or b""
        total = int(frame.header("sum") or 1)
        if total > 1:
            combined = self._combine(
                frame.header("message_id") or "", total, int(frame.header("seq") or 0), payload
            )
            if combined is None:
                return None
            payload = combined
        if message_type != "event":
            return None

        started = time.monotonic()
        try:
            result = self._on_event(payload)
            response: JsonDict = {"code": 200}
            if result is not None:
                response["data"] = base64.b64encode(
                    json.dumps(result, ensure_ascii=False).encode("utf-8")
                ).decode("ascii")
        except Exception as err:
            logger.error(
                "飞书长连接 %s 事件处理失败：message_id=%s, trace_id=%s, err=%s",
                self._name,
                frame.header("message_id"),
                frame.header("trace_id"),
                err,
            )
            response = {"code": 500}
        frame.headers.append(("biz_rt", str(int((time.monotonic() - started) * 1000))))
        frame.payload = json.dumps(response).encode("utf-8")
        return frame.encode()
