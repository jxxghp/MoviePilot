"""
QQ Bot Gateway WebSocket 客户端
连接 QQ 开放平台 Gateway，接收 C2C 和群聊消息并转发至 MP 消息链
"""

import json
import threading
import time
from typing import Callable, List, Optional

import websocket

from app.log import logger

# QQ Bot intents
INTENT_GROUP_AND_C2C = 1 << 25  # 群聊和 C2C 私聊


def run_gateway(
    app_id: str,
    app_secret: str,
    config_name: str,
    get_token_fn: Callable[[str, str], str],
    get_gateway_url_fn: Callable[[str], str],
    on_message_fn: Callable[[dict], None],
    stop_event: threading.Event,
    ws_holder: List,
) -> None:
    """
    在后台线程中运行 Gateway WebSocket 连接
    :param app_id: QQ 机器人 AppID
    :param app_secret: QQ 机器人 AppSecret
    :param config_name: 配置名称，用于消息来源标识
    :param get_token_fn: 获取 access_token 的函数 (app_id, app_secret) -> token
    :param get_gateway_url_fn: 获取 gateway URL 的函数 (token) -> url
    :param on_message_fn: 收到消息时的回调 (payload_dict) -> None
    :param stop_event: 停止事件，set 时退出循环
    :param ws_holder: 调用方持有的单元素列表，存放当前 WebSocketApp，供 stop() 时 close 以打断 run_forever
    """
    last_seq: Optional[int] = None
    heartbeat_interval_ms: Optional[int] = None
    heartbeat_timer: Optional[threading.Timer] = None

    def send_heartbeat():
        nonlocal heartbeat_timer
        if stop_event.is_set():
            return
        try:
            if ws_holder and ws_holder[0]:
                payload = {"op": 1, "d": last_seq}
                ws_holder[0].send(json.dumps(payload))
                logger.debug(f"[QQ Gateway:{config_name}] Heartbeat sent, seq={last_seq}")
        except Exception as err:
            logger.debug(f"[QQ Gateway:{config_name}] Heartbeat error: {err}")
        if heartbeat_interval_ms and not stop_event.is_set():
            heartbeat_timer = threading.Timer(heartbeat_interval_ms / 1000.0, send_heartbeat)
            heartbeat_timer.daemon = True
            heartbeat_timer.start()

    def on_ws_message(_, message):
        nonlocal last_seq, heartbeat_interval_ms, heartbeat_timer
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as err:
            logger.error(f"[QQ Gateway:{config_name}] Invalid JSON: {err}")
            return

        op = payload.get("op")
        d = payload.get("d")
        s = payload.get("s")
        t = payload.get("t")

        if s is not None:
            last_seq = s

        logger.debug(f"[QQ Gateway:{config_name}] op={op} t={t}")

        if op == 10:  # Hello
            heartbeat_interval_ms = d.get("heartbeat_interval", 30000)
            logger.info(f"[QQ Gateway:{config_name}] Hello received, heartbeat_interval={heartbeat_interval_ms}")

            # Identify
            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENT_GROUP_AND_C2C,
                    "shard": [0, 1],
                },
            }
            ws_holder[0].send(json.dumps(identify))
            logger.info(f"[QQ Gateway:{config_name}] Identify sent")

            # 启动心跳
            if heartbeat_timer:
                heartbeat_timer.cancel()
            heartbeat_timer = threading.Timer(heartbeat_interval_ms / 1000.0, send_heartbeat)
            heartbeat_timer.daemon = True
            heartbeat_timer.start()

        elif op == 0:  # Dispatch
            if t == "READY":
                session_id = d.get("session_id", "")
                logger.info(f"[QQ Gateway:{config_name}] 连接成功 Ready, session_id={session_id}")
            elif t == "RESUMED":
                logger.info(f"[QQ Gateway:{config_name}] 连接成功 Session resumed")
            elif t == "C2C_MESSAGE_CREATE":
                author = d.get("author", {})
                user_openid = author.get("user_openid", "")
                content = d.get("content", "").strip()
                msg_id = d.get("id", "")
                if content:
                    on_message_fn({
                        "type": "C2C_MESSAGE_CREATE",
                        "content": content,
                        "author": {"user_openid": user_openid},
                        "id": msg_id,
                        "timestamp": d.get("timestamp", ""),
                    })
            elif t == "GROUP_AT_MESSAGE_CREATE":
                author = d.get("author", {})
                member_openid = author.get("member_openid", "")
                group_openid = d.get("group_openid", "")
                content = d.get("content", "").strip()
                msg_id = d.get("id", "")
                if content:
                    on_message_fn({
                        "type": "GROUP_AT_MESSAGE_CREATE",
                        "content": content,
                        "author": {"member_openid": member_openid},
                        "id": msg_id,
                        "group_openid": group_openid,
                        "timestamp": d.get("timestamp", ""),
                    })
            # 其他事件忽略

        elif op == 7:  # Reconnect
            logger.info(f"[QQ Gateway:{config_name}] Reconnect requested")
            # 当前实现不自动重连，由外层循环处理

        elif op == 9:  # Invalid Session
            logger.warning(f"[QQ Gateway:{config_name}] Invalid session")
            if ws_holder and ws_holder[0]:
                ws_holder[0].close()

    def on_ws_error(_, error):
        logger.error(f"[QQ Gateway:{config_name}] WebSocket error: {error}")

    def on_ws_close(_, close_status_code, close_msg):
        logger.info(f"[QQ Gateway:{config_name}] WebSocket closed: {close_status_code} {close_msg}")
        if heartbeat_timer:
            heartbeat_timer.cancel()
        ws_holder.clear()

    reconnect_delays = [1, 2, 5, 10, 30, 60]
    attempt = 0

    while not stop_event.is_set():
        try:
            token = get_token_fn(app_id, app_secret)
            gateway_url = get_gateway_url_fn(token)
            logger.info(f"[QQ Gateway:{config_name}] Connecting to {gateway_url[:60]}...")

            ws = websocket.WebSocketApp(
                gateway_url,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            ws_holder.clear()
            ws_holder.append(ws)

            # run_forever 会阻塞，需要传入 stop_event 的检查
            # websocket-client 的 run_forever 支持 ping_interval, ping_timeout
            # 我们使用自定义心跳，所以不设置 ping
            ws.run_forever(
                ping_interval=None,
                ping_timeout=None,
                skip_utf8_validation=True,
            )

        except Exception as e:
            logger.error(f"[QQ Gateway:{config_name}] Connection error: {e}")

        if stop_event.is_set():
            break

        delay = reconnect_delays[min(attempt, len(reconnect_delays) - 1)]
        attempt += 1
        logger.info(f"[QQ Gateway:{config_name}] Reconnecting in {delay}s (attempt {attempt})")
        for _ in range(delay * 10):
            if stop_event.is_set():
                break
            time.sleep(0.1)

    if heartbeat_timer:
        heartbeat_timer.cancel()
    logger.info(f"[QQ Gateway:{config_name}] Gateway thread stopped")
