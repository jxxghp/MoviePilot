"""
进程内按键互斥的锁集合。
"""

import threading
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

# 分段数只影响不同键之间偶然共用同一把锁的概率；锁只在极短的下载器操作期间持有，64 段足够。
_DEFAULT_STRIPES = 64


class KeyedLocks:
    """
    按字符串键串行化同一资源上的操作，不同键之间尽量并行。

    采用固定数量的分段锁，不随键的数量增长，也不需要清理；不同键落到同一段时只是多等一会儿，
    不影响正确性。一次持有多个键时按段号升序加锁，避免两个调用方交叉持有造成死锁。
    锁可重入：同一线程在持有期间再次进入同一键（例如持锁结算时又调用了同样持锁的暂停接口）不会
    自己卡死；但持有一组键时不要再去获取另一组键，嵌套获取不同的键会破坏升序加锁、可能死锁。
    只在单个进程内有效，无法协调下载器之外的其他程序。
    """

    def __init__(self, stripes: int = _DEFAULT_STRIPES) -> None:
        """
        创建分段锁。

        :param stripes: 分段数量，必须为正数
        """
        if stripes <= 0:
            raise ValueError("分段数量必须为正数")
        self._locks = tuple(threading.RLock() for _ in range(stripes))

    def _stripe(self, key: str) -> int:
        """
        把键稳定映射到分段；键不区分大小写（下载器返回的 hash 大小写不一）。
        """
        return zlib.crc32(key.lower().encode("utf-8")) % len(self._locks)

    @contextmanager
    def hold(self, keys: Iterable[str]) -> Iterator[None]:
        """
        在上下文期间持有给定键对应的锁；空键集合不加锁。

        :param keys: 需要互斥的资源键，例如下载任务 hash（同一种子的 v1/v2 hash 可一并传入）
        """
        stripes = sorted({self._stripe(key) for key in keys if key})
        acquired: list[threading.RLock] = []
        try:
            for stripe in stripes:
                lock = self._locks[stripe]
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()


# 下载器任务变更锁：同一下载任务（按 hash）上所有"读取状态 → 据此改动任务或记录"的操作都在这把锁内
# 完成，读到的状态在改动生效前不会被宿主内的其他流程改掉。目前包括：
# 1. 下载链提交下载：读取已确认选集记录 → 下载器受理（新建，或在已有任务上追加文件、恢复、移除"已整理"）
#    → 写入下载历史（含累积的确认选集记录）。历史读写也在锁内，否则两次并发追加各自读到旧记录、
#    各自合并后写回，后写的会覆盖先写的，丢掉一次确认；
# 2. 暂停 / 恢复（``ChainBase.stop_torrents`` / ``start_torrents``）：否则用户刚暂停，订阅又凭暂停前读到的
#    标签把任务恢复；
# 3. 整理链移动模式"检查可删除 + 删除任务"：否则刚确认可以删除，下载链就追加了新文件，随后删除会连同
#    新文件一起删掉，订阅却已记成下载中；
# 4. 整理链"确认已下载完成 + 写入已整理标签"：否则刚确认完成，下载链就追加了数据已齐的文件，随后写入的
#    标签会让追加的文件再也不进入下载器整理。
# 锁管不到 MP 进程之外的操作（下载器界面、第三方工具、直接操作下载器客户端的插件与智能助手脚本），
# 这些由回读确认、"最保守的标签生效"、恢复后发现被重新暂停就撤销恢复等规则兜底。
# 新增会改动下载任务的流程时，请同样在这把锁内完成。
#
# 持锁期间只有下载器 RPC、本地数据库写入和事件/消息入队：DownloadAdded 等广播事件由独立线程异步消费，
# 插件回调执行时锁通常已经释放，回调里可以安全调用 ChainBase 的下载、暂停、恢复接口；即使赶上持锁，
# 也只是在另一个线程里短暂等待，不会死锁。锁可重入，同一线程嵌套进入同一 hash 不会卡住自己。
downloader_task_locks = KeyedLocks()
