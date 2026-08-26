"""
QQ Bot Gateway WebSocket 客户端
连接 QQ 开放平台 Gateway，接收 C2C 和群聊消息并转发至 MP 消息链
"""

import json
import re
import threading
import time
from typing import Callable, List, Optional

import websocket

from app.runtime.log import logger

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
    heartbeat_thread: Optional[threading.Thread] = None
    heartbeat_stop_event: Optional[threading.Event] = None
    heartbeat_epoch = 0
    heartbeat_state_lock = threading.Lock()
    heartbeat_replace_lock = threading.RLock()

    def heartbeat_loop(
        generation_epoch: int,
        generation_stop_event: threading.Event,
        interval_seconds: float,
        ws: websocket.WebSocketApp,
    ) -> None:
        """在当前连接 generation 内串行发送心跳，停止后不再派生新线程。"""
        while not generation_stop_event.wait(interval_seconds):
            if stop_event.is_set():
                return
            with heartbeat_state_lock:
                if (
                    generation_epoch != heartbeat_epoch
                    or heartbeat_stop_event is not generation_stop_event
                ):
                    return
                sequence = last_seq
            try:
                payload = {"op": 1, "d": sequence}
                ws.send(json.dumps(payload))
                logger.debug(
                    f"[QQ Gateway:{config_name}] Heartbeat sent, seq={sequence}"
                )
            except Exception as err:
                logger.debug(f"[QQ Gateway:{config_name}] Heartbeat error: {err}")

    def invalidate_heartbeat_generation() -> tuple[Optional[threading.Thread], int]:
        """锁内失效当前 generation，并返回待等待的 owner 与新 epoch。"""
        nonlocal heartbeat_epoch
        with heartbeat_state_lock:
            heartbeat_epoch += 1
            current_thread = heartbeat_thread
            if heartbeat_stop_event is not None:
                heartbeat_stop_event.set()
            return current_thread, heartbeat_epoch

    def clear_heartbeat_generation(current_thread: Optional[threading.Thread]) -> None:
        """仅在快照仍是当前 owner 且已经终止时清除 generation 引用。"""
        nonlocal heartbeat_thread, heartbeat_stop_event
        if current_thread is not None and current_thread.is_alive():
            return
        with heartbeat_state_lock:
            if heartbeat_thread is current_thread:
                heartbeat_thread = None
                heartbeat_stop_event = None

    def stop_heartbeat(*, wait: bool) -> None:
        """停止当前心跳；等待阶段不持有状态锁，避免阻塞 close 回调。"""
        if wait:
            heartbeat_replace_lock.acquire()
        try:
            current_thread, _ = invalidate_heartbeat_generation()
            if (
                wait
                and current_thread is not None
                and current_thread.is_alive()
                and current_thread is not threading.current_thread()
            ):
                current_thread.join()
            clear_heartbeat_generation(current_thread)
        finally:
            if wait:
                heartbeat_replace_lock.release()

    def start_heartbeat(ws: websocket.WebSocketApp, interval_ms: int) -> None:
        """停止旧 generation 后启动一个由 Gateway 聚合的新心跳 owner。"""
        nonlocal heartbeat_thread, heartbeat_stop_event
        with heartbeat_replace_lock:
            current_thread, generation_epoch = invalidate_heartbeat_generation()
            if (
                current_thread is not None
                and current_thread.is_alive()
                and current_thread is not threading.current_thread()
            ):
                current_thread.join()
            clear_heartbeat_generation(current_thread)
            with heartbeat_state_lock:
                if (
                    generation_epoch != heartbeat_epoch
                    or stop_event.is_set()
                    or not ws_holder
                    or ws_holder[0] is not ws
                ):
                    return
                generation_stop_event = threading.Event()
                # 心跳是 Gateway 的常驻子 owner，不占用进程共享 ThreadHelper；Gateway 退出前统一 join。
                generation_thread = threading.Thread(
                    target=heartbeat_loop,
                    args=(
                        generation_epoch,
                        generation_stop_event,
                        interval_ms / 1000.0,
                        ws,
                    ),
                    daemon=True,
                    name=f"qq-gateway-heartbeat-{config_name}",
                )
                heartbeat_stop_event = generation_stop_event
                heartbeat_thread = generation_thread
                generation_thread.start()

    def on_ws_message(ws, message):
        """处理当前 WebSocket 连接收到的 QQ Gateway 消息。"""
        nonlocal last_seq
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
            with heartbeat_state_lock:
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
            ws.send(json.dumps(identify))
            logger.info(f"[QQ Gateway:{config_name}] Identify sent")

            start_heartbeat(ws, heartbeat_interval_ms)

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
                match = re.search(r'(agent_interaction:choice:[\w\-]+:\d+|agent_choice:[\w\-]+:\d+)', content)
                if match:
                    content = f"CALLBACK:{match.group(1)}"
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
                match = re.search(r'(agent_interaction:choice:[\w\-]+:\d+|agent_choice:[\w\-]+:\d+)', content)
                if match:
                    content = f"CALLBACK:{match.group(1)}"
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
            ws.close()

    def on_ws_error(_, error):
        """记录当前 WebSocket 连接上报的错误。"""
        logger.error(f"[QQ Gateway:{config_name}] WebSocket error: {error}")

    def on_ws_close(ws, close_status_code, close_msg):
        """失效当前连接及其心跳 generation，但不在回调线程等待。"""
        logger.info(f"[QQ Gateway:{config_name}] WebSocket closed: {close_status_code} {close_msg}")
        # close 回调可能由 stop() 调用线程触发，只发信号，统一由 Gateway 线程等待终态。
        with heartbeat_state_lock:
            is_current_connection = bool(ws_holder and ws_holder[0] is ws)
            if is_current_connection:
                ws_holder.clear()
        if is_current_connection:
            stop_heartbeat(wait=False)

    reconnect_delays = [1, 2, 5, 10, 30, 60]
    attempt = 0

    try:
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
                with heartbeat_state_lock:
                    ws_holder.clear()
                    ws_holder.append(ws)

                # websocket-client 的 run_forever 会阻塞；QQ 协议使用自定义心跳，不启用 ping。
                try:
                    ws.run_forever(
                        ping_interval=None,
                        ping_timeout=None,
                        skip_utf8_validation=True,
                    )
                finally:
                    # 旧连接的心跳必须先终止，下一轮 reconnect 才能取得 owner。
                    stop_heartbeat(wait=True)

            except Exception as err:
                logger.error(f"[QQ Gateway:{config_name}] Connection error: {err}")

            if stop_event.is_set():
                break

            delay = reconnect_delays[min(attempt, len(reconnect_delays) - 1)]
            attempt += 1
            logger.info(
                f"[QQ Gateway:{config_name}] Reconnecting in {delay}s (attempt {attempt})"
            )
            for _ in range(delay * 10):
                if stop_event.is_set():
                    break
                time.sleep(0.1)
    finally:
        stop_heartbeat(wait=True)
        logger.info(f"[QQ Gateway:{config_name}] Gateway thread stopped")
