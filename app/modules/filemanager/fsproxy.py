"""
本地文件系统操作代理。

FUSE/网络挂载有两种故障形态：crash 型（调用抛错，可捕获、可重试）和 block 型
（调用既不返回错误也不返回结果，永久悬挂）。**Python 无法中断一个已经发出的
系统调用，也无法强杀线程**，所以 block 型故障下阻塞的线程永远无法回收——这正是
整理消费线程停摆、监控自愈路径自冻的根因。

本模块把这些调用放进一个常驻子进程执行。子进程可以被 SIGKILL，因此超时后能真正
回收；对调用方而言，超时表现为一个普通的 OSError 子类（FileSystemTimeout）。
换句话说：**把不可处理的 block 型故障，转换成系统各层已经能正确处理的 crash 型
故障**——退避重启、登记待重试这些既有机制立刻就能接管。

第一版只放行安全的操作：
- 只读（stat/exists/listdir）——强杀不产生任何副作用
- 同存储 rename——内核保证原子性，强杀后要么完全成功要么完全没发生
跨存储的复制+删除不在此列，它需要单独的可恢复语义（临时名 + 完成后 rename）。
"""
import errno as errno_module
import json
import os
import selectors
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core.config import settings
from app.log import logger

# worker 脚本路径。用绝对路径直接执行，而不是 -m 或 import：
# 直接执行文件不会触发 app/__init__.py 的导入链，代理启动才是毫秒级的
_WORKER_PATH = Path(__file__).parent / "fsworker.py"
# 单次快操作（stat/listdir/rename/unlink 等）的默认超时秒数
DEFAULT_TIMEOUT = 30
# 长耗时操作（复制）两次进度上报之间的最长间隔秒数。
# worker 每秒上报一次心跳，因此这个阈值判定的是「传输完全没有推进」，
# 而不是「传输很慢」——大文件复制几小时也不会误杀
DEFAULT_STALL_TIMEOUT = 120
# 强杀代理后等待它消失的宽限秒数，不能无限等待
_KILL_GRACE = 5


class FileSystemTimeout(OSError):
    """
    文件系统操作在代理中超时未返回，判定挂载无响应。

    继承 OSError 是刻意的：整理链、监控 watcher 等各层对 OSError 已有完整的
    退避重试与登记逻辑，block 型故障经此转换后可以直接复用它们。
    """


class FileSystemProxy:
    """
    常驻子进程文件系统代理。

    请求-响应严格串行（一个代理同时只处理一个请求），由锁保证。超时即强杀代理，
    下一次请求自动重启一个新的——启动成本是毫秒级，因为 worker 只依赖标准库。
    """

    def __init__(self, timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None):
        """
        :param timeout: 单次快操作的超时秒数，None 表示实时跟随系统设置
        :param stall_timeout: 长耗时操作两次进度上报之间的最长间隔秒数，
                              None 表示实时跟随系统设置
        """
        self._timeout_override = timeout
        self._stall_timeout_override = stall_timeout
        self._process: Optional[subprocess.Popen] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 对外操作
    # ------------------------------------------------------------------ #

    def stat(self, path: Path) -> Dict[str, Any]:
        """
        读取路径属性。
        :param path: 目标路径
        :return: {"size", "mtime", "is_dir", "is_file"}
        """
        return self._call("stat", path=str(path))

    def exists(self, path: Path) -> bool:
        """
        判断路径是否存在。

        只有 FileNotFoundError 才算「不存在」；其余 OSError（含超时）原样抛出，
        避免像 Path.exists() 那样把挂载抖动误判成文件消失。
        :param path: 目标路径
        :return: 是否存在
        """
        try:
            self._call("exists", path=str(path))
            return True
        except FileNotFoundError:
            return False

    def listdir(self, path: Path) -> List[str]:
        """
        列出目录条目名。
        :param path: 目标目录
        :return: 条目名列表
        """
        return self._call("listdir", path=str(path))

    def rename(self, src: Path, dst: Path) -> bool:
        """
        同一存储内重命名/移动。跨存储会抛 OSError(EXDEV)，由调用方走原有路径。
        :param src: 源路径
        :param dst: 目标路径
        :return: 是否成功
        """
        return self._call("rename", src=str(src), dst=str(dst))

    def copy(self, src: Path, dst: Path,
             progress_cb: Optional[Callable[[float], None]] = None,
             cancel_cb: Optional[Callable[[], bool]] = None,
             chunk_size: Optional[int] = None) -> Any:
        """
        复制文件内容并保留时间戳，按「进度无推进」判定挂死。

        复制大文件可能持续几小时，固定超时无法区分「正常但慢」和「已经挂死」。
        worker 每秒上报一次进度作为心跳，这里判定的是**两次上报之间的间隔**：
        超过 stall 阈值收不到任何一行，才认定挂载无响应并强杀 worker。

        取消检查放在父进程：worker 里读不到 global_vars 的传输取消标记，而父进程
        每收到一次进度就能检查一次，要取消直接杀掉 worker 即可，比在子进程里
        轮询标记更干净。
        :param src: 源文件
        :param dst: 目标文件（调用方应传临时名，完成后自行原子替换）
        :param progress_cb: 进度回调，入参为百分比
        :param cancel_cb: 取消检查回调，返回 True 表示应中止
        :param chunk_size: 分块大小
        :return: 成功时为 {"copied", "total"}，被取消或通信失败时为 False
        """
        payload = {"src": str(src), "dst": str(dst)}
        if chunk_size:
            payload["chunk_size"] = chunk_size
        if not self._enabled():
            return self._direct_copy(src, dst, progress_cb, cancel_cb, chunk_size)
        with self._lock:
            try:
                return self._request_stream(payload, progress_cb, cancel_cb)
            except FileSystemTimeout:
                raise
            except (BrokenPipeError, ConnectionError, json.JSONDecodeError, ValueError) as err:
                logger.error(f"文件系统代理复制通信异常: {src} -> {dst} - {err}")
                self._shutdown()
                return False

    def _request_stream(self, payload: Dict[str, Any],
                        progress_cb: Optional[Callable[[float], None]],
                        cancel_cb: Optional[Callable[[], bool]]) -> Any:
        """
        发起一次流式请求，逐行消费进度直到终态。
        :param payload: 请求参数
        :param progress_cb: 进度回调
        :param cancel_cb: 取消检查回调
        :return: 操作结果
        """
        self._ensure_worker()
        message = json.dumps({"op": "copy", **payload}) + "\n"
        self._process.stdin.write(message.encode("utf-8"))
        self._process.stdin.flush()

        while True:
            response = json.loads(self._read_line(timeout=self._stall_timeout).decode("utf-8"))
            progress = response.get("progress")
            if progress is not None:
                if cancel_cb is not None and cancel_cb():
                    logger.info(f"复制已取消: {payload.get('src')}")
                    # 取消就地生效：杀掉 worker 立刻中断传输，不必等它读完整个文件
                    self._shutdown()
                    return False
                if progress_cb is not None:
                    total = progress.get("total") or 0
                    progress_cb(progress.get("copied", 0) / total * 100 if total else 0)
                continue
            if response.get("ok"):
                return response.get("result")
            raise OSError(response.get("errno") or 0, response.get("error") or "unknown error")

    @staticmethod
    def _direct_copy(src: Path, dst: Path,
                     progress_cb: Optional[Callable[[float], None]],
                     cancel_cb: Optional[Callable[[], bool]],
                     chunk_size: Optional[int]) -> bool:
        """
        不经代理直接复制，供代理关闭时使用。
        """
        info = os.stat(src)
        total = info.st_size
        copied = 0
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                if cancel_cb is not None and cancel_cb():
                    return False
                buf = fsrc.read(chunk_size or 1024 * 1024)
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
                if progress_cb is not None and total:
                    progress_cb(copied / total * 100)
        os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
        return True

    def unlink(self, path: Path) -> bool:
        """
        删除单个文件。unlink 是原子操作，强杀后没有中间状态。
        :param path: 目标文件
        :return: 是否成功
        """
        return self._call("unlink", path=str(path))

    def rmtree(self, path: Path) -> bool:
        """
        递归删除目录，容忍部分失败（可重复执行直到成功）。
        :param path: 目标目录
        :return: 是否成功
        """
        return self._call("rmtree", path=str(path))

    def close(self):
        """
        关闭代理进程。
        """
        with self._lock:
            self._shutdown()

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    @property
    def _timeout(self) -> float:
        """
        单次快操作的超时秒数。

        实时读取而不是构造时固定：这三项都暴露在前端设置里，用户改完保存后
        必须立刻生效，否则会出现「改了没反应」的困惑。
        """
        if self._timeout_override is not None:
            return self._timeout_override
        return float(getattr(settings, "FS_PROXY_TIMEOUT", DEFAULT_TIMEOUT))

    @property
    def _stall_timeout(self) -> float:
        """
        长耗时操作两次进度上报之间的最长间隔秒数，同样实时跟随系统设置。
        """
        if self._stall_timeout_override is not None:
            return self._stall_timeout_override
        return float(getattr(settings, "FS_PROXY_STALL_TIMEOUT", DEFAULT_STALL_TIMEOUT))

    @staticmethod
    def _enabled() -> bool:
        """
        代理是否启用。关闭时退回直接调用，行为与引入代理之前完全一致。
        """
        return bool(getattr(settings, "FS_PROXY_ENABLED", True))

    @staticmethod
    def _direct(op: str, payload: Dict[str, Any]) -> Any:
        """
        不经代理直接执行操作，供代理关闭时使用。
        :param op: 操作名
        :param payload: 操作参数
        :return: 操作结果
        """
        if op == "stat":
            path = payload["path"]
            info = os.stat(path)
            return {
                "size": info.st_size,
                "mtime": info.st_mtime,
                "is_dir": os.path.isdir(path),
                "is_file": os.path.isfile(path),
            }
        if op == "exists":
            os.stat(payload["path"])
            return True
        if op == "listdir":
            return sorted(os.listdir(payload["path"]))
        if op == "rename":
            os.rename(payload["src"], payload["dst"])
            return True
        if op == "unlink":
            os.unlink(payload["path"])
            return True
        if op == "rmtree":
            shutil.rmtree(payload["path"], ignore_errors=True)
            return True
        raise ValueError(f"unknown op: {op}")

    def _call(self, op: str, **payload) -> Any:
        """
        执行一次代理调用。
        :param op: 操作名
        :param payload: 操作参数
        :return: 操作结果
        """
        if not self._enabled():
            return self._direct(op, payload)
        with self._lock:
            try:
                return self._request(op, payload)
            except FileSystemTimeout:
                # 超时说明挂载正在挂死，重试只会再冻一次，直接上报给调用方
                raise
            except (BrokenPipeError, ConnectionError, json.JSONDecodeError, ValueError) as err:
                # 代理进程意外退出或响应损坏，重启后重试一次
                logger.debug(f"文件系统代理通信异常，重启后重试: {op} - {err}")
                self._shutdown()
                return self._request(op, payload)

    def _request(self, op: str, payload: Dict[str, Any]) -> Any:
        """
        发送请求并等待响应。
        :param op: 操作名
        :param payload: 操作参数
        :return: 操作结果
        """
        self._ensure_worker()
        message = json.dumps({"op": op, **payload}) + "\n"
        self._process.stdin.write(message.encode("utf-8"))
        self._process.stdin.flush()

        response = json.loads(self._read_line().decode("utf-8"))
        if response.get("ok"):
            return response.get("result")
        # OSError(errno, strerror) 会自动映射到 FileNotFoundError 等具体子类，
        # 调用方沿用原有的异常分支即可，无需感知代理的存在
        raise OSError(response.get("errno") or 0, response.get("error") or "unknown error")

    def _read_line(self, timeout: Optional[float] = None) -> bytes:
        """
        读取一行响应，超时即强杀代理。
        :param timeout: 本次读取的超时秒数，默认用单次操作超时
        :return: 响应行
        """
        timeout = self._timeout if timeout is None else timeout
        if not self._selector.select(timeout=timeout):
            logger.error(f"文件系统操作 {timeout} 秒无响应，判定挂载挂死，正在回收代理进程")
            self._shutdown()
            raise FileSystemTimeout(
                errno_module.ETIMEDOUT,
                f"文件系统操作超过 {timeout} 秒无响应，挂载可能已无响应"
            )
        line = self._process.stdout.readline()
        if not line:
            raise BrokenPipeError("文件系统代理进程已退出")
        return line

    def _ensure_worker(self):
        """
        确保代理进程可用，不可用时重新启动。
        """
        if self._process is not None and self._process.poll() is None:
            return
        self._shutdown()
        self._process = subprocess.Popen(
            [sys.executable, str(_WORKER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._process.stdout, selectors.EVENT_READ)
        logger.debug(f"文件系统代理进程已启动: pid={self._process.pid}")

    def _shutdown(self):
        """
        回收代理进程。冻在挂载上的进程用 SIGKILL，且不无限等待它消失
        ——否则「可放弃的代理」又变回一次不可放弃的阻塞。
        """
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:  # noqa: BLE001
                pass
            self._selector = None
        process, self._process = self._process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
        if process.poll() is not None:
            return
        try:
            process.kill()
            process.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            logger.warn(f"文件系统代理进程未能及时退出，交由系统回收: pid={process.pid}")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"回收文件系统代理进程失败: {err}")


# 全局单例：local 存储本身是单例，代理也只需要一个
# 不传超时参数：让它实时跟随系统设置，前端改完保存即刻生效
fsproxy = FileSystemProxy()
