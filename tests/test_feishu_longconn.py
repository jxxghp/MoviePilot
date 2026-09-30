"""飞书事件长连接协议测试：pbbp2 帧编解码、事件应答、分包合并、握手失败分类与真实 WebSocket 收发。"""

import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest
import websocket
from websockets.sync.server import serve

from app.modules.feishu import longconn
from app.modules.feishu.longconn import FatalConnectionError, FeishuLongConnection, Frame

# 以下字节由 lark-oapi 1.6 的 pbbp2_pb2.Frame.SerializeToString() 生成，作为协议对照样本。
SDK_PING_FRAME = bytes.fromhex("08001000180720002a0c0a0474797065120470696e67")
SDK_EVENT_FRAME = bytes.fromhex(
    "08031009180720012a0d0a047479706512056576656e742a100a0a6d6573736167655f696412026d31"
    "2a080a0373756d1201312a080a037365711201302a0e0a0874726163655f69641202743142107b2273"
    "6368656d61223a22322e30227d4a014c"
)


def _event_frame(payload: bytes, message_type: str = "event", **headers: str) -> Frame:
    """构造服务端下发的 DATA 帧。"""
    frame_headers = [("type", message_type), ("message_id", headers.pop("message_id", "m1"))]
    frame_headers += [("sum", headers.pop("sum", "1")), ("seq", headers.pop("seq", "0"))]
    frame_headers += [("trace_id", "t1")]
    return Frame(seq_id=3, log_id=9, service=7, method=1, headers=frame_headers, payload=payload)


def _connection(on_event=None) -> FeishuLongConnection:
    return FeishuLongConnection("cli_app", "secret", on_event=on_event or (lambda payload: None))


def test_ping_frame_matches_sdk_encoding():
    """心跳帧与 SDK 编码逐字节一致，required 字段即使为 0 也写出。"""
    ping = Frame(service=7, method=0, headers=[("type", "ping")])

    assert ping.encode() == SDK_PING_FRAME


def test_event_frame_round_trips_sdk_bytes():
    """解析 SDK 编码的事件帧后再编码，字节不变，optional 字段按原样保留。"""
    frame = Frame.decode(SDK_EVENT_FRAME)

    assert (frame.seq_id, frame.log_id, frame.service, frame.method) == (3, 9, 7, 1)
    assert frame.header("type") == "event"
    assert frame.header("message_id") == "m1"
    assert frame.payload == b'{"schema":"2.0"}'
    assert frame.log_id_new == "L"
    assert frame.payload_encoding is None
    assert frame.encode() == SDK_EVENT_FRAME


def test_negative_int32_field_round_trips():
    """int32 负值按 10 字节 varint 编码，解码后恢复符号。"""
    frame = Frame(service=-5, method=1)

    assert Frame.decode(frame.encode()).service == -5


def test_truncated_frame_raises():
    """截断的帧不会被当作合法数据。"""
    with pytest.raises(ValueError):
        # 去掉最后一字节后，LogIDNew 声明的长度超出剩余数据。
        Frame.decode(SDK_EVENT_FRAME[:-1])


def test_event_reply_keeps_frame_and_encodes_result():
    """事件应答复用原帧字段并追加 biz_rt，处理结果以 base64 JSON 放在 data。"""
    received = []
    conn = _connection(lambda payload: received.append(payload) or {"toast": {"content": "操作已提交"}})

    reply = Frame.decode(conn._handle_frame(_event_frame(b'{"k": 1}').encode()))

    assert received == [b'{"k": 1}']
    assert (reply.seq_id, reply.log_id, reply.service, reply.method) == (3, 9, 7, 1)
    assert reply.header("message_id") == "m1"
    assert reply.header("biz_rt") is not None
    body = json.loads(reply.payload)
    assert body["code"] == 200
    assert json.loads(base64.b64decode(body["data"])) == {"toast": {"content": "操作已提交"}}


def test_event_reply_without_result_and_on_failure():
    """无返回值时只回 code 200；处理异常时回 500，连接不中断。"""
    assert json.loads(Frame.decode(_connection()._handle_frame(_event_frame(b"{}").encode())).payload) == {
        "code": 200
    }

    def fail(payload):
        raise RuntimeError("boom")

    reply = Frame.decode(_connection(fail)._handle_frame(_event_frame(b"{}").encode()))
    assert json.loads(reply.payload) == {"code": 500}


def test_card_type_frame_is_ignored_without_reply():
    """card 类型帧与 SDK 一致：不分发也不应答。"""
    received = []
    conn = _connection(received.append)

    assert conn._handle_frame(_event_frame(b"{}", message_type="card").encode()) is None
    assert received == []


def test_fragmented_event_is_combined_before_dispatch():
    """分包事件在全部分片到达后按 seq 顺序拼接再分发，只对最后一片应答。"""
    received = []
    conn = _connection(received.append)

    first = conn._handle_frame(_event_frame(b"world", message_id="big", sum="2", seq="1").encode())
    last = conn._handle_frame(_event_frame(b"hello ", message_id="big", sum="2", seq="0").encode())

    assert first is None
    assert last is not None
    assert received == [b"hello world"]
    assert conn._fragments == {}


def test_expired_fragments_are_dropped(monkeypatch):
    """超过合包等待时间的分片被丢弃，迟到的分片不会拼出残缺事件。"""
    received = []
    conn = _connection(received.append)
    now = [100.0]
    monkeypatch.setattr(longconn.time, "monotonic", lambda: now[0])

    conn._handle_frame(_event_frame(b"a", message_id="late", sum="2", seq="0").encode())
    now[0] += longconn._COMBINE_TTL_SECONDS + 1
    assert conn._handle_frame(_event_frame(b"b", message_id="late", sum="2", seq="1").encode()) is None
    assert received == []


def test_pong_payload_updates_client_config():
    """pong 帧携带的 ClientConfig 覆盖心跳与重连参数。"""
    conn = _connection()
    pong = Frame(method=0, headers=[("type", "pong")], payload=b'{"PingInterval": 15, "ReconnectCount": 3}')

    assert conn._handle_frame(pong.encode()) is None
    assert conn._config.ping_interval == 15
    assert conn._config.reconnect_count == 3
    assert conn._config.reconnect_interval == 120


def _endpoint_response(body, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: body)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"code": 1, "msg": "busy"}, ConnectionError),
        ({"code": 1000040343, "msg": "internal"}, ConnectionError),
        ({"code": 10003, "msg": "invalid app secret"}, FatalConnectionError),
    ],
)
def test_endpoint_errors_are_classified(monkeypatch, body, error):
    """繁忙与内部错误可重试，凭据等客户端错误视为致命。"""
    monkeypatch.setattr(longconn.RequestUtils, "post_res", lambda self, url, **kw: _endpoint_response(body))

    with pytest.raises(error):
        _connection()._fetch_endpoint()


def test_endpoint_success_applies_config(monkeypatch):
    """地址接口成功时返回 URL 并应用服务端配置，请求体携带应用凭据。"""
    calls = []

    def post_res(self, url, **kwargs):
        calls.append((url, kwargs["json"]))
        return _endpoint_response({
            "code": 0,
            "data": {"URL": "wss://example/ws?device_id=d&service_id=7", "ClientConfig": {"PingInterval": 30}},
        })

    monkeypatch.setattr(longconn.RequestUtils, "post_res", post_res)
    conn = _connection()

    assert conn._fetch_endpoint() == "wss://example/ws?device_id=d&service_id=7"
    assert conn._config.ping_interval == 30
    assert calls == [("https://open.feishu.cn/callback/ws/endpoint", {"AppID": "cli_app", "AppSecret": "secret"})]


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"handshake-status": "403", "handshake-msg": "forbidden"}, FatalConnectionError),
        (
            {"Handshake-Status": "514", "handshake-msg": "limit", "handshake-autherrcode": "1000040350"},
            FatalConnectionError,
        ),
        ({"handshake-status": "514", "handshake-msg": "auth", "handshake-autherrcode": "1"}, ConnectionError),
        ({}, ConnectionError),
    ],
)
def test_handshake_failures_are_classified(headers, error):
    """无权限与连接数超限不再重连，其余握手失败按可重试处理。"""
    err = websocket.WebSocketBadStatusException("bad", 400, resp_headers=headers)

    with pytest.raises(error):
        FeishuLongConnection._raise_for_handshake(err)


def test_fatal_error_stops_reconnect(monkeypatch):
    """致命错误让 run() 直接返回，不进入重连等待。"""
    attempts = []

    def run_once(self):
        attempts.append(1)
        raise FatalConnectionError("invalid")

    monkeypatch.setattr(FeishuLongConnection, "_run_once", run_once)

    _connection().run()
    assert attempts == [1]


def test_reconnect_count_limit(monkeypatch):
    """服务端限定重连次数时，连续失败超过上限后停止。"""
    attempts = []

    def run_once(self):
        attempts.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(FeishuLongConnection, "_run_once", run_once)
    conn = _connection()
    conn._config.update({"ReconnectCount": 2, "ReconnectInterval": 0, "ReconnectNonce": 0})
    monkeypatch.setattr(conn._stop_event, "wait", lambda timeout=None: False)

    conn.run()
    assert len(attempts) == 3


def test_reconnects_indefinitely_after_established_connections_drop(monkeypatch):
    """连上后断开不计入连续失败：多次断线后仍按抖动重连，不会因重连次数上限永久退出。"""
    attempts = []
    delays = []

    def run_once(self):
        attempts.append(1)
        if len(attempts) == 6:
            self._stop_event.set()
            return
        self._connected = True
        raise ConnectionError("peer closed")

    monkeypatch.setattr(FeishuLongConnection, "_run_once", run_once)
    monkeypatch.setattr(longconn.random, "random", lambda: 0.5)
    conn = _connection()
    conn._config.update({"ReconnectCount": 2, "ReconnectInterval": 120, "ReconnectNonce": 10})
    monkeypatch.setattr(conn._stop_event, "wait", lambda timeout=None: delays.append(timeout) or False)

    conn.run()
    assert len(attempts) == 6
    assert delays == [5.0] * 5


class _SilentSocket:
    """只接收 ping、从不回任何帧的连接，模拟链路静默断开。"""

    def __init__(self, clock):
        self.clock = clock
        self.sent = []

    def send(self, data, opcode=None):
        self.sent.append(Frame.decode(data).header("type"))

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv_data(self):
        self.clock[0] += self.timeout
        raise websocket.WebSocketTimeoutException("timeout")


def test_silent_connection_is_detected(monkeypatch):
    """超过心跳间隔加宽限仍无入站帧时抛出断线，交给 run() 重连。"""
    clock = [0.0]
    monkeypatch.setattr(longconn.time, "monotonic", lambda: clock[0])
    conn = _connection()
    conn._config.update({"PingInterval": 10})
    ws = _SilentSocket(clock)

    with pytest.raises(ConnectionError):
        conn._serve(ws, service_id=1)
    assert clock[0] > 10 + longconn._LIVENESS_GRACE_SECONDS
    assert set(ws.sent) == {"ping"}


def test_live_websocket_ping_event_ack_and_stop(monkeypatch):
    """
    对本地 WebSocket 服务端完整走一遍：连接后先发 ping，收到事件后回写应答，
    stop() 中断阻塞接收并让 run() 在预算内返回。
    """
    server_frames = []
    ack_received = threading.Event()

    def handler(ws):
        server_frames.append(Frame.decode(ws.recv()))
        ws.send(_event_frame(b'{"header": {"event_type": "demo"}}').encode())
        server_frames.append(Frame.decode(ws.recv()))
        ack_received.set()
        try:
            ws.recv()
        except Exception:
            return

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        monkeypatch.setattr(
            FeishuLongConnection,
            "_fetch_endpoint",
            lambda self: f"ws://127.0.0.1:{port}/ws?device_id=dev1&service_id=42",
        )
        events = []
        conn = _connection(lambda payload: events.append(payload) or {"ok": True})
        client_thread = threading.Thread(target=conn.run, daemon=True)
        client_thread.start()

        assert ack_received.wait(5)
        started = time.monotonic()
        conn.stop()
        client_thread.join(3)
        server.shutdown()

    assert not client_thread.is_alive()
    assert time.monotonic() - started < 3
    ping, ack = server_frames
    assert ping.header("type") == "ping"
    assert ping.service == 42
    assert events == [b'{"header": {"event_type": "demo"}}']
    assert ack.header("biz_rt") is not None
    assert json.loads(ack.payload)["code"] == 200
