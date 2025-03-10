from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from typing import Any, Union
from typing import List, Dict, Optional, Callable

from app.utils.singleton import Singleton
from core.config import global_vars
from db.systemconfig_oper import SystemConfigOper
from log import logger
from schemas.types import SystemConfigKey


class MessageQueueManager(metaclass=Singleton):
    """
    消息发送队列管理器
    """
    def __init__(
            self,
            send_callback: Optional[Callable] = None,
            check_interval: int = 10
    ) -> None:
        """
        消息队列管理器初始化

        :param send_callback: 实际发送消息的回调函数
        :param check_interval: 时间检查间隔（秒）
        """
        self.schedule_periods = self._parse_schedule(
            SystemConfigOper().get(SystemConfigKey.NotificationSendTime)
        )
        self.queue: queue.Queue[Any] = queue.Queue()
        self.send_callback = send_callback
        self.check_interval = check_interval

        self._running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()

    @staticmethod
    def _parse_schedule(periods: Union[list, dict]) -> List[tuple[int, int, int, int]]:
        """
        将字符串时间格式转换为分钟数元组
        """
        parsed = []
        if not periods:
            return parsed
        if not isinstance(periods, list):
            periods = [periods]
        for period in periods:
            if not period:
                continue
            start_h, start_m = map(int, period['start'].split(':'))
            end_h, end_m = map(int, period['end'].split(':'))
            parsed.append((start_h, start_m, end_h, end_m))
        return parsed

    @staticmethod
    def _time_to_minutes(time_str: str) -> int:
        """
        将 'HH:MM' 格式转换为分钟数
        """
        hours, minutes = map(int, time_str.split(':'))
        return hours * 60 + minutes

    def _is_in_scheduled_time(self, current_time: datetime) -> bool:
        """
        检查当前时间是否在允许发送的时间段内
        """
        if not self.schedule_periods:
            return True
        current_minutes = current_time.hour * 60 + current_time.minute
        for period in self.schedule_periods:
            s_h, s_m, e_h, e_m = period
            start = s_h * 60 + s_m
            end = e_h * 60 + e_m

            if start <= end:
                if start <= current_minutes <= end:
                    return True
            else:
                if current_minutes >= start or current_minutes <= end:
                    return True
        return False

    def send_message(self, *args, **kwargs) -> None:
        """
        发送消息（立即发送或加入队列）
        """
        if self._is_in_scheduled_time(datetime.now()):
            self._send(*args, **kwargs)
        else:
            self.queue.put({
                "args": args,
                "kwargs": kwargs
            })
            logger.info(f"消息已加入队列，当前队列长度：{self.queue.qsize()}")

    def _send(self, *args, **kwargs) -> None:
        """
        实际发送消息（可通过回调函数自定义）
        """
        if self.send_callback:
            try:
                self.send_callback(*args, **kwargs)
            except Exception as e:
                logger.error(str(e))

    def _monitor_loop(self) -> None:
        """
        后台线程循环检查时间并处理队列
        """
        while self._running:
            current_time = datetime.now()
            if self._is_in_scheduled_time(current_time):
                while not self.queue.empty():
                    if global_vars.is_system_stopped:
                        break
                    if not self._is_in_scheduled_time(datetime.now()):
                        break
                    try:
                        message = self.queue.get_nowait()
                        self._send(*message['args'], **message['kwargs'])
                        logger.info(f"队列剩余消息：{self.queue.qsize()}")
                    except queue.Empty:
                        break
            time.sleep(self.check_interval)

    def stop(self) -> None:
        """
        停止队列管理器
        """
        self._running = False
        self.thread.join()


class MessageHelper(metaclass=Singleton):
    """
    消息队列管理器，包括系统消息和用户消息
    """

    def __init__(self):
        self.sys_queue = queue.Queue()
        self.user_queue = queue.Queue()

    def put(self, message: Any, role: str = "plugin", title: str = None, note: Union[list, dict] = None):
        """
        存消息
        :param message: 消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        :param title: 标题
        :param note: 附件json
        """
        if role in ["system", "plugin"]:
            # 没有标题时获取插件名称
            if role == "plugin" and not title:
                title = "插件通知"
            # 系统通知，默认
            self.sys_queue.put(json.dumps({
                "type": role,
                "title": title,
                "text": message,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "note": note
            }))
        else:
            if isinstance(message, str):
                # 非系统的文本通知
                self.user_queue.put(json.dumps({
                    "title": title,
                    "text": message,
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "note": note
                }))
            elif hasattr(message, "to_dict"):
                # 非系统的复杂结构通知，如媒体信息/种子列表等。
                content = message.to_dict()
                content['title'] = title
                content['date'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                content['note'] = note
                self.user_queue.put(json.dumps(content))

    def get(self, role: str = "system") -> Optional[str]:
        """
        取消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        """
        if role == "system":
            if not self.sys_queue.empty():
                return self.sys_queue.get(block=False)
        else:
            if not self.user_queue.empty():
                return self.user_queue.get(block=False)
        return None
