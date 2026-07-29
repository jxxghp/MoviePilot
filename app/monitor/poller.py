import traceback
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional

from app.chain.storage import StorageChain
from app.log import logger
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.snapshot import SnapshotStore


class RemotePoller:
    """
    远程目录轮询监控：快照、比对并分发变化文件。
    """
    # 同一存储连续异常达到该次数后推送告警
    FAILURE_ALERT_THRESHOLD = 3

    def __init__(self, store: SnapshotStore, dispatcher: TransferDispatcher,
                 alert_cb: Optional[Callable[[str, str], None]] = None):
        """
        初始化远程轮询监控。
        :param store: 快照存储
        :param dispatcher: 整理分发器
        :param alert_cb: 告警回调 (storage, message)
        """
        self._store = store
        self._dispatcher = dispatcher
        self._alert_cb = alert_cb
        # 快照锁按存储隔离，避免一个慢存储阻塞其他存储的轮询
        self._locks: Dict[str, Lock] = {}
        self._locks_guard = Lock()
        # 各存储连续异常次数
        self._failure_counts: Dict[str, int] = {}

    def _get_lock(self, storage: str) -> Lock:
        """
        获取指定存储的快照锁。
        :param storage: 存储名称
        :return: 快照锁
        """
        with self._locks_guard:
            return self._locks.setdefault(storage, Lock())

    def _note_failure(self, storage: str, reason: str):
        """
        记录一次轮询异常，连续异常达到阈值时推送告警。
        :param storage: 存储名称
        :param reason: 异常原因
        """
        count = self._failure_counts.get(storage, 0) + 1
        self._failure_counts[storage] = count
        logger.warn(f"远程目录监控异常（连续第 {count} 次）: {storage} - {reason}")
        if count == self.FAILURE_ALERT_THRESHOLD and self._alert_cb:
            self._alert_cb(storage,
                           f"远程目录监控连续 {count} 次异常: {storage}\n原因: {reason}\n将继续按周期重试")

    def _note_success(self, storage: str):
        """
        记录一次轮询成功，此前告警过时推送恢复消息。
        :param storage: 存储名称
        """
        if self._failure_counts.get(storage, 0) >= self.FAILURE_ALERT_THRESHOLD and self._alert_cb:
            self._alert_cb(storage, f"远程目录监控已恢复: {storage}")
        self._failure_counts[storage] = 0

    def poll(self, storage: str, mon_paths: List[Path]) -> Optional[int]:
        """
        执行一轮轮询监控。
        :param storage: 存储名称
        :param mon_paths: 监控路径列表
        :return: 基线文件数量，本轮无有效结果时返回 None
        """
        monitor_scope = ",".join(str(mon_path) for mon_path in mon_paths) or "未配置路径"
        with self._get_lock(storage):
            try:
                # 加载上次快照数据，读取失败不能当作首次快照，否则会丢弃已有基线
                old_snapshot_data, load_ok = self._store.load_checked(storage)
                if not load_ok:
                    self._note_failure(storage, "读取快照基线失败，跳过本轮")
                    return None
                old_snapshot = old_snapshot_data.get('snapshot', {}) if old_snapshot_data else {}
                last_snapshot_time = old_snapshot_data.get('timestamp', 0) if old_snapshot_data else 0
                is_first_snapshot = old_snapshot_data is None

                new_snapshot = {}
                failed_paths = []
                for mon_path in mon_paths:
                    logger.debug(f"开始对 {storage}:{mon_path} 进行快照...")

                    # 生成新快照（增量模式）
                    snapshot = StorageChain().snapshot_storage(
                        storage=storage,
                        path=mon_path,
                        last_snapshot_time=last_snapshot_time
                    )

                    if snapshot is None:
                        failed_paths.append(str(mon_path))
                        logger.warn(f"获取 {storage}:{mon_path} 快照失败")
                        continue
                    new_snapshot.update(snapshot)
                    logger.info(f"{storage}:{mon_path} 快照完成，发现 {len(snapshot)} 个文件")

                if failed_paths and (is_first_snapshot or len(failed_paths) == len(mon_paths)):
                    # 首次基线必须完整建立；全部路径失败时本轮没有有效数据，均不落盘
                    self._note_failure(storage, f"快照失败: {','.join(failed_paths)}")
                    return None

                # 增量快照只包含变化子树，必须与基线合并才是完整视图；
                # 直接把增量当基线会导致下一轮把未扫到的旧文件全部误判为新增
                merged_snapshot = {**old_snapshot, **new_snapshot}
                file_count = len(merged_snapshot)

                if not is_first_snapshot:
                    self._handle_changes(storage, old_snapshot, new_snapshot)
                else:
                    logger.info(f"{storage} 首次快照完成，共 {file_count} 个文件")
                    logger.info("*** 首次快照仅建立基准，不会处理现有文件。后续监控将处理新增和修改的文件 ***")

                # 保存合并后的基线
                if not self._store.save(storage, merged_snapshot, file_count, last_snapshot_time):
                    self._note_failure(storage, "保存快照基线失败")
                    return None

                if failed_paths:
                    # 部分路径失败：成功路径已合并，失败路径保留旧基线，下轮重试
                    self._note_failure(storage, f"部分路径快照失败: {','.join(failed_paths)}")
                else:
                    self._note_success(storage)
                return file_count

            except Exception as e:
                logger.error(f"轮询监控 {storage}:{monitor_scope} 出现错误：{e}\n{traceback.format_exc()}")
                self._note_failure(storage, str(e))
                return None

    def _handle_changes(self, storage: str, old_snapshot: dict, new_snapshot: dict):
        """
        比对快照并把变化文件送入整理链。
        :param storage: 存储名称
        :param old_snapshot: 旧基线
        :param new_snapshot: 本轮增量快照
        """
        changes = SnapshotStore.compare(old_snapshot, new_snapshot)
        added_files = [
            file_path
            for file_path in changes['added']
            if self._dispatcher.is_transfer_candidate_path(Path(file_path))
        ]
        modified_files = [
            file_path
            for file_path in changes['modified']
            if self._dispatcher.is_transfer_candidate_path(Path(file_path))
        ]

        # 处理新增文件
        handled_added_count = 0
        for new_file in added_files:
            file_info = new_snapshot.get(new_file, {})
            file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
            if self._dispatcher.handle_file(storage=storage, event_path=Path(new_file), file_size=file_size):
                handled_added_count += 1

        # 处理修改文件
        handled_modified_count = 0
        for modified_file in modified_files:
            file_info = new_snapshot.get(modified_file, {})
            file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
            if self._dispatcher.handle_file(storage=storage, event_path=Path(modified_file), file_size=file_size):
                handled_modified_count += 1

        if handled_added_count or handled_modified_count:
            logger.info(f"{storage} 发现 {handled_added_count} 个新增文件，{handled_modified_count} 个修改文件")
        else:
            logger.debug(f"{storage} 无文件变化")

    def force_full_scan(self, storage: str, mon_path: Path) -> bool:
        """
        强制全量扫描并处理所有文件（包括已存在的文件）。
        :param storage: 存储名称
        :param mon_path: 监控路径
        :return: 是否成功
        """
        try:
            logger.info(f"开始强制全量扫描: {storage}:{mon_path}")

            # 生成快照
            new_snapshot = StorageChain().snapshot_storage(
                storage=storage,
                path=mon_path,
                last_snapshot_time=0  # 全量扫描，不使用增量
            )

            if new_snapshot is None:
                logger.warn(f"获取 {storage}:{mon_path} 快照失败")
                return False

            file_count = len(new_snapshot)
            logger.info(f"{storage}:{mon_path} 全量扫描完成，发现 {file_count} 个文件")

            # 处理所有文件
            processed_count = 0
            for file_path, file_info in new_snapshot.items():
                try:
                    if not self._dispatcher.is_transfer_candidate_path(Path(file_path)):
                        continue
                    file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                    if self._dispatcher.handle_file(storage=storage, event_path=Path(file_path),
                                                    file_size=file_size):
                        processed_count += 1
                except Exception as e:
                    logger.error(f"处理文件 {file_path} 失败: {e}")
                    continue

            logger.info(f"{storage}:{mon_path} 全量扫描完成，共处理 {processed_count}/{file_count} 个文件")

            # 全量扫描覆盖单个路径，与已有基线合并后落盘，避免覆盖其他监控路径的基线
            old_snapshot_data, load_ok = self._store.load_checked(storage)
            old_snapshot = old_snapshot_data.get('snapshot', {}) if (load_ok and old_snapshot_data) else {}
            merged_snapshot = {**old_snapshot, **new_snapshot}
            self._store.save(storage, merged_snapshot, len(merged_snapshot))

            return True

        except Exception as e:
            logger.error(f"强制全量扫描失败: {storage}:{mon_path} - {e}")
            return False
