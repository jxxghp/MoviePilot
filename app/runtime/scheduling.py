import datetime
import random
from typing import List, Optional, Tuple, Union

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger


class TimerUtils:
    """
    定时与时间差计算工具。
    """

    SCHEDULE_TRIGGER_TYPES = ("date", "cron")

    @staticmethod
    def normalize_schedule_trigger(
            trigger_type: str,
            trigger_value: str,
            timezone_name: str,
            require_future: bool = False,
    ) -> Tuple[str, str]:
        """
        校验并规范化单次时间或五段 cron 表达式。

        :param trigger_type: 触发类型，支持 date 或 cron
        :param trigger_value: 带时区或本地时间字符串，或五段 cron 表达式
        :param timezone_name: 无显式时区时使用的系统时区
        :param require_future: 单次任务是否必须安排在未来
        :return: 规范化后的触发类型和触发值
        """
        normalized_type = str(trigger_type or "").strip().lower()
        normalized_value = str(trigger_value or "").strip()
        if normalized_type not in TimerUtils.SCHEDULE_TRIGGER_TYPES:
            raise ValueError("trigger_type 仅支持 date 或 cron")
        if not normalized_value:
            raise ValueError("trigger 不能为空")

        timezone = pytz.timezone(timezone_name)
        if normalized_type == "cron":
            normalized_value = " ".join(normalized_value.split())
            if len(normalized_value.split()) != 5:
                raise ValueError("cron 必须是标准五段表达式：分 时 日 月 周")
            CronTrigger.from_crontab(normalized_value, timezone=timezone)
            return normalized_type, normalized_value

        try:
            run_at = datetime.datetime.fromisoformat(normalized_value)
        except ValueError as err:
            raise ValueError(
                "date 时间必须使用 ISO 8601 格式，例如 2026-07-19 20:30:00"
            ) from err
        if run_at.tzinfo is None:
            run_at = timezone.localize(run_at)
        else:
            run_at = run_at.astimezone(timezone)
        if require_future and run_at <= datetime.datetime.now(timezone):
            raise ValueError("单次任务的触发时间必须晚于当前时间")
        return normalized_type, run_at.isoformat(timespec="seconds")

    @staticmethod
    def build_schedule_trigger(
            trigger_type: str,
            trigger_value: str,
            timezone_name: str,
    ) -> Union[CronTrigger, DateTrigger]:
        """
        构建 APScheduler 单次或 cron 触发器。

        :param trigger_type: 触发类型，支持 date 或 cron
        :param trigger_value: 已配置的触发时间或 cron 表达式
        :param timezone_name: 调度器使用的系统时区
        :return: APScheduler 触发器
        """
        normalized_type, normalized_value = TimerUtils.normalize_schedule_trigger(
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            timezone_name=timezone_name,
        )
        timezone = pytz.timezone(timezone_name)
        if normalized_type == "cron":
            return CronTrigger.from_crontab(normalized_value, timezone=timezone)
        return DateTrigger(
            run_date=datetime.datetime.fromisoformat(normalized_value),
            timezone=timezone,
        )

    @staticmethod
    def get_schedule_next_run_time(
            trigger_type: str,
            trigger_value: str,
            timezone_name: str,
            now: Optional[datetime.datetime] = None,
    ) -> Optional[datetime.datetime]:
        """
        计算指定触发配置的下一次执行时间。

        :param trigger_type: 触发类型，支持 date 或 cron
        :param trigger_value: 已配置的触发时间或 cron 表达式
        :param timezone_name: 调度器使用的系统时区
        :param now: 可选的计算基准时间
        :return: 下一次执行时间，不再触发时返回 None
        """
        timezone = pytz.timezone(timezone_name)
        current_time = now or datetime.datetime.now(timezone)
        if current_time.tzinfo is None:
            current_time = timezone.localize(current_time)
        trigger = TimerUtils.build_schedule_trigger(
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            timezone_name=timezone_name,
        )
        return trigger.get_next_fire_time(None, current_time)

    @staticmethod
    def random_scheduler(num_executions: int = 1,
                         begin_hour: int = 7,
                         end_hour: int = 23,
                         min_interval: int = 20,
                         max_interval: int = 40) -> List[datetime.datetime]:
        """
        按执行次数生成随机定时器
        :param num_executions: 执行次数
        :param begin_hour: 开始时间
        :param end_hour: 结束时间
        :param min_interval: 最小间隔分钟
        :param max_interval: 最大间隔分钟
        """
        trigger: list = []
        # 当前时间
        now = datetime.datetime.now()
        # 创建随机的时间触发器
        random_trigger = now.replace(hour=begin_hour, minute=0, second=0, microsecond=0)
        for _ in range(num_executions):
            # 随机生成下一个任务的时间间隔
            interval_minutes = random.randint(min_interval, max_interval)
            random_interval = datetime.timedelta(minutes=interval_minutes)
            # 记录上一个任务的时间触发器
            last_random_trigger = random_trigger
            # 更新当前时间为下一个任务的时间触发器
            random_trigger += random_interval
            # 达到结束时间或者时间出现倒退时退出
            if random_trigger.hour > end_hour \
                    or random_trigger.hour < last_random_trigger.hour:
                break
            # 添加到队列
            trigger.append(random_trigger)

        return trigger

    @staticmethod
    def random_even_scheduler(num_executions: int = 1,
                              begin_hour: int = 7,
                              end_hour: int = 23) -> List[datetime.datetime]:
        """
        按执行次数尽可能平均生成随机定时器
        :param num_executions: 执行次数
        :param begin_hour: 计划范围开始的小时数
        :param end_hour: 计划范围结束的小时数
        """
        trigger_times = []
        start_time = datetime.datetime.now().replace(hour=begin_hour, minute=0, second=0, microsecond=0)
        end_time = datetime.datetime.now().replace(hour=end_hour, minute=0, second=0, microsecond=0)

        # 计算范围内的总分钟数
        total_minutes = int((end_time - start_time).total_seconds() / 60)
        # 计算每个执行时间段的平均长度
        segment_length = total_minutes // num_executions

        for i in range(num_executions):
            # 在每个段内随机选择一个点
            start_segment = segment_length * i
            end_segment = start_segment + segment_length
            minute = random.randint(start_segment, end_segment - 1)
            trigger_time = start_time + datetime.timedelta(minutes=minute)
            trigger_times.append(trigger_time)

        return trigger_times

    @staticmethod
    def time_difference(input_datetime: datetime) -> str:
        """
        判断输入时间与当前的时间差，如果输入时间大于当前时间则返回时间差，否则返回空字符串
        """
        if not input_datetime:
            return ""
        current_datetime = datetime.datetime.now(datetime.timezone.utc).astimezone()
        time_difference = input_datetime - current_datetime

        if time_difference.total_seconds() < 0:
            return ""

        days = time_difference.days
        hours, remainder = divmod(time_difference.seconds, 3600)
        minutes, second = divmod(remainder, 60)

        time_difference_string = ""
        if days > 0:
            time_difference_string += f"{days}天"
        if hours > 0:
            time_difference_string += f"{hours}小时"
        if minutes > 0:
            time_difference_string += f"{minutes}分钟"
        if not time_difference_string and second:
            time_difference_string = f"{second}秒"

        return time_difference_string

    @staticmethod
    def diff_minutes(input_datetime: datetime) -> int:
        """
        计算当前时间与输入时间的分钟差
        """
        if not input_datetime:
            return 0
        time_difference = datetime.datetime.now() - input_datetime
        return int(time_difference.total_seconds() / 60)
