"""
文件系统操作代理 worker。

**本文件不能被 import，只能作为独立脚本执行**（fsproxy 用
`subprocess.Popen([sys.executable, <本文件绝对路径>])` 启动）。直接执行文件
路径不会触发 `app/__init__.py` 的导入链，因此这个进程只依赖标准库、启动是
毫秒级的；一旦走 import 就会把整个应用的依赖拉进来，代理被强杀后的重启成本
会高到无法接受。

存在的理由：FUSE/网络挂载进入 block 型故障时，`stat`/`listdir`/`rename` 这类
系统调用既不返回错误也不返回结果，而 Python 没有中断线程的手段——阻塞其上的
线程永远无法回收。放进独立进程后，父进程可以在超时后 SIGKILL 掉它，把
「不可处理的 block」转换成「可处理的 crash」。

协议：stdin/stdout 逐行 JSON。
  请求  {"op": "stat", "path": "/mnt/cd2/x.mkv"}
  成功  {"ok": true, "result": {...}}
  失败  {"ok": false, "errno": 2, "error": "No such file or directory"}
"""
import json
import os
import shutil
import sys
import time


def _stat(payload, _emit):
    """
    读取路径的基本属性。
    """
    path = payload["path"]
    info = os.stat(path)
    return {
        "size": info.st_size,
        "mtime": info.st_mtime,
        "is_dir": os.path.isdir(path),
        "is_file": os.path.isfile(path),
    }


def _exists(payload, _emit):
    """
    判断路径是否存在。

    用 os.stat 而不是 os.path.exists：后者会把任意 OSError 都归为「不存在」，
    挂载抖动会被误判成文件消失。这里让异常原样抛出，由父进程按 errno 区分。
    """
    os.stat(payload["path"])
    return True


def _listdir(payload, _emit):
    """
    列出目录下的条目名。
    """
    return sorted(os.listdir(payload["path"]))


def _copy(payload, emit):
    """
    分块复制文件内容并周期上报进度。

    进度上报同时充当心跳：复制大文件可能持续几小时，父进程无法用固定超时判断
    挂死，只能看「两次上报之间隔了多久」。因此这里按固定时间间隔上报，即使
    某一秒没读到数据也照常发——一旦挂载卡住，read/write 不返回，上报自然断流，
    父进程据此判定并强杀本进程。

    只复制内容和时间戳，不复制权限：目标目录的默认权限与继承 ACL 是媒体库的
    访问策略，用源文件权限覆盖会清除已继承的 ACL。
    """
    src, dst = payload["src"], payload["dst"]
    chunk_size = payload.get("chunk_size") or 1024 * 1024
    interval = payload.get("progress_interval") or 1.0

    info = os.stat(src)
    total = info.st_size
    copied = 0
    # 先发一次 0%：既让心跳立刻开始，也保证父进程在传输开始前就有一次检查
    # 取消的机会——否则小文件会在首次定时上报之前就复制完，取消形同虚设
    emit({"ok": True, "progress": {"copied": 0, "total": total}})
    last_emit = time.monotonic()
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            now = time.monotonic()
            if now - last_emit >= interval:
                last_emit = now
                emit({"ok": True, "progress": {"copied": copied, "total": total}})
    os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
    return {"copied": copied, "total": total}


def _rename(payload, _emit):
    """
    同一存储内重命名/移动。

    这是第一版唯一放行的写操作：同文件系统内的 rename 由内核保证原子性，
    进程被强杀后要么完全成功要么完全没发生，不存在需要清理的中间状态。
    跨存储的复制+删除不走这里，它需要单独的可恢复语义。
    """
    src, dst = payload["src"], payload["dst"]
    if os.stat(src).st_dev != os.stat(os.path.dirname(dst) or ".").st_dev:
        raise OSError(18, "Cross-device rename is not handled by the proxy")
    os.rename(src, dst)
    return True


def _unlink(payload, _emit):
    """
    删除单个文件。unlink 是原子操作，强杀后要么删掉了要么没删，没有中间状态。
    """
    os.unlink(payload["path"])
    return True


def _rmtree(payload, _emit):
    """
    递归删除目录。

    这一项不是原子的，强杀可能只删掉一部分。放行的理由是：删除被中断的后果
    （残留若干文件）远轻于写入被中断（留下叫最终文件名的半成品），而且调用方
    本来就以 ignore_errors 容忍部分失败、可以重复执行直到成功。
    """
    shutil.rmtree(payload["path"], ignore_errors=True)
    return True


_HANDLERS = {
    "stat": _stat,
    "exists": _exists,
    "listdir": _listdir,
    "copy": _copy,
    "rename": _rename,
    "unlink": _unlink,
    "rmtree": _rmtree,
    "ping": lambda _payload, _emit: True,
}


def _write(message):
    """
    输出一行响应。
    """
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    """
    请求循环：每读一行处理一个请求，直到 stdin 关闭。

    一个请求可能对应多行响应：长耗时操作先流式发若干 progress 行，最后发一行
    终态（result 或 error）。父进程据此区分「还在推进」和「已经挂死」。
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            handler = _HANDLERS.get(payload.get("op"))
            if handler is None:
                response = {"ok": False, "errno": 0,
                            "error": f"unknown op: {payload.get('op')}"}
            else:
                response = {"ok": True, "result": handler(payload, _write)}
        except OSError as err:
            response = {"ok": False, "errno": err.errno or 0,
                        "error": err.strerror or str(err)}
        except Exception as err:  # noqa: BLE001 - worker 不能因任何异常退出
            response = {"ok": False, "errno": 0, "error": str(err)}
        _write(response)


if __name__ == "__main__":
    main()
