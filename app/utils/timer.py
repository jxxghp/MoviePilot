import datetime
import random
from typing import List


class TimerUtils:

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
            # 更新当前时间为下一个任务的时间触发器
            random_trigger += random_interval
            # 达到结否时间时退出
            if now.hour > end_hour:
                break
            # 添加到队列
            trigger.append(random_trigger)

        return trigger
