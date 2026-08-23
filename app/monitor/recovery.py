"""
监控恢复动作的有限等待执行单元。

FUSE/网络挂载有两种故障形态，下游程序的免疫力完全不同：

- crash 型：调用抛错（如 Transport endpoint is not connected）。异常能被捕获，
  退避重启即可自愈，由 watcher 自身的重启循环覆盖。
- block 型：调用既不返回错误也不返回结果，永久悬挂。**没有任何超时参数能救
  一个已经发出的 stat**，阻塞其上的线程无法被 Python 回收（线程没有强杀接口）。

本模块提供 block 型故障下唯一可行的两种自保手段：

1. RecoveryExecutor —— 把会触碰挂载的动作放进一次性守护线程执行，调用方只
   等待有限时间。超时后当前健康检查不再等待，但线程句柄仍由生命周期 owner
   持有到真实终态，换取看门狗可继续检测且停机屏障不会伪装收敛。
2. probe_path —— 用子进程而非线程做挂载探测。子进程可以被 kill，因此探测
   本身是可放弃的，隔离期间可以无限次周期重试而不累积不可回收的资源。
"""
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional

from app.runtime.log import logger

# 探测子进程执行的脚本：只对目标路径做一次 stat，成功退出 0，失败退出非 0。
# 用 sys.executable 而不是 test/stat 等外部命令，避免依赖发行版的 coreutils 布局。
_PROBE_SCRIPT = "import os, sys; os.stat(sys.argv[1])"
# 探测子进程超时后，等待它响应 SIGKILL 的宽限秒数。挂在 FUSE 上的进程可能一时
# 收不掉，宽限期满就不再等待，残留进程由后续 Popen 自动回收，绝不能无限等待
# ——否则「可放弃的探测」又变回一次不可放弃的阻塞。
_PROBE_KILL_GRACE = 5


class RecoveryState(str, Enum):
    """
    一次恢复动作的执行结论。
    """
    # 动作已在限定时间内执行完毕（内部抛异常也算完成，异常已记录）
    COMPLETED = "completed"
    # 超时仍未返回，判定为 block 型挂载故障，本轮不再等待但继续持有线程
    TIMEOUT = "timeout"
    # 同 key 的上一个动作仍未结束，本次未提交，避免持续泄漏冻死的线程
    BUSY = "busy"


class RecoveryExecutor:
    """
    按 key 隔离的一次性恢复线程执行器。

    每个 key 同一时刻最多有一个在途动作：上一个还冻着就不再提交新的，否则每个
    健康检查周期都会在同一个死挂载上多泄漏一个线程。
    """

    def __init__(self) -> None:
        """初始化在途线程注册表和当前生命周期的提交状态。"""
        # key -> 该 key 最近一次提交的执行线程
        self._running: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._accepting = True

    def run(self, actions: Dict[str, Callable[[], None]], timeout: float) -> Dict[str, RecoveryState]:
        """
        并发执行一批恢复动作，整批最多等待 timeout 秒。

        并发而非串行是必需的：串行等待会让总耗时随监控目录数线性增长，
        13 个目录都挂死时健康检查会被拖过下一个周期，等于又一次自我冻结。
        :param actions: key -> 无参恢复动作
        :param timeout: 整批动作的最长等待秒数
        :return: key -> 执行结论
        """
        results: Dict[str, RecoveryState] = {}
        started = []
        for key, action in actions.items():
            thread = self._start(key, action)
            if thread is None:
                results[key] = RecoveryState.BUSY
                logger.warn(f"上一次恢复动作仍未返回，本轮跳过以避免线程泄漏: {key}")
                continue
            started.append((key, thread))

        deadline = time.monotonic() + timeout
        for key, thread in started:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                results[key] = RecoveryState.TIMEOUT
                logger.error(f"恢复动作超过 {timeout} 秒未返回，判定挂载无响应，本轮不再等待: {key}")
            else:
                results[key] = RecoveryState.COMPLETED
        return results

    def discard(self, key: str) -> None:
        """
        清理一个已到终态的 key；活线程继续保留，供生命周期停机屏障追踪。
        :param key: 动作标识
        """
        with self._lock:
            thread = self._running.get(key)
            if thread is None or not thread.is_alive():
                self._running.pop(key, None)

    def clear(self) -> None:
        """
        清理已经完成的记录，仍存活的线程继续由执行器持有。

        兼容旧调用名，但不再丢弃挂死线程：遗失句柄会让宿主错误释放它仍可能使用
        的数据库和整理链资源。
        """
        with self._lock:
            self._running = {
                key: thread
                for key, thread in self._running.items()
                if thread.is_alive()
            }

    def request_stop(self) -> None:
        """封住新恢复动作提交，既有线程只能自然完成。"""
        with self._lock:
            self._accepting = False

    def close(self, deadline: float) -> bool:
        """在绝对截止时间内等待全部恢复线程，超时继续保留活线程句柄。"""
        self.request_stop()
        with self._lock:
            threads = tuple(self._running.values())
        current_thread = threading.current_thread()
        for thread in threads:
            if thread is current_thread or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.clear()
        with self._lock:
            return not any(thread.is_alive() for thread in self._running.values())

    def reopen(self) -> bool:
        """在旧恢复线程全部结束后，为新的 Monitor 生命周期恢复提交。"""
        self.clear()
        with self._lock:
            if any(thread.is_alive() for thread in self._running.values()):
                return False
            self._running.clear()
            self._accepting = True
            return True

    def running_threads(self) -> tuple[threading.Thread, ...]:
        """返回仍存活且由执行器持有的恢复线程快照。"""
        with self._lock:
            return tuple(
                thread for thread in self._running.values() if thread.is_alive()
            )

    def _start(self, key: str, action: Callable[[], None]) -> Optional[threading.Thread]:
        """
        为一个 key 启动执行线程，该 key 仍有在途动作时不启动。
        :param key: 动作标识
        :param action: 无参恢复动作
        :return: 执行线程，未启动时为 None
        """
        with self._lock:
            if not self._accepting:
                return None
            running = self._running.get(key)
            if running is not None and running.is_alive():
                return None
            thread = threading.Thread(
                target=self._execute,
                args=(key, action),
                name=f"MoviePilot-MonitorRecovery-{key}"[:120],
                daemon=True
            )
            self._running[key] = thread
            # 登记与 start 必须处于同一所有权临界区。否则 close() 可能把尚未
            # is_alive() 的句柄当成已结束并移除，随后该线程才真正启动。
            try:
                thread.start()
            except BaseException:
                if self._running.get(key) is thread:
                    self._running.pop(key, None)
                raise
            return thread

    @staticmethod
    def _execute(key: str, action: Callable[[], None]) -> None:
        """
        执行一个恢复动作，异常只记录不外抛，避免一个目录的失败连累整批恢复。
        :param key: 动作标识
        :param action: 无参恢复动作
        """
        try:
            action()
        except Exception as err:
            logger.error(f"执行目录监控恢复动作失败: {key} - {err}")


def probe_path(path: Path, timeout: float) -> bool:
    """
    用可放弃的子进程探测一个路径是否仍能被访问。

    必须是子进程：在本线程里直接 stat，block 型故障下这个调用永不返回，探测
    线程就成了又一个不可回收的悬挂线程；子进程可以在超时后被 kill，因此隔离
    期间可以无限次周期探测。
    :param path: 待探测路径
    :param timeout: 探测超时秒数
    :return: 路径是否可访问
    """
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _PROBE_SCRIPT, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as err:
        logger.error(f"启动挂载探测子进程失败: {path} - {err}")
        return False

    try:
        return process.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        logger.warn(f"挂载探测 {timeout} 秒无响应，判定挂载仍未恢复: {path}")
        process.kill()
        try:
            # 不能用无超时的 wait()：进程若卡在 FUSE 上收不掉 SIGKILL，
            # 这里就会替它把调用线程也一起挂住
            process.wait(timeout=_PROBE_KILL_GRACE)
        except subprocess.TimeoutExpired:
            logger.warn(f"挂载探测子进程未能及时退出，交由系统回收: {path}")
        return False
    except Exception as err:
        logger.error(f"挂载探测执行失败: {path} - {err}")
        return False
