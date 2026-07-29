import traceback
from pathlib import Path
from threading import Lock
from typing import List, Optional

from app.chain.storage import StorageChain
from app.log import logger
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.snapshot import SnapshotStore

snapshot_lock = Lock()


class RemotePoller:
    """
    远程目录轮询监控：快照、比对并分发变化文件。
    """

    def __init__(self, store: SnapshotStore, dispatcher: TransferDispatcher):
        """
        初始化远程轮询监控。
        :param store: 快照存储
        :param dispatcher: 整理分发器
        """
        self._store = store
        self._dispatcher = dispatcher

    def poll(self, storage: str, mon_paths: List[Path]) -> Optional[int]:
        """
        执行一轮轮询监控。
        :param storage: 存储名称
        :param mon_paths: 监控路径列表
        :return: 本轮快照文件数量，出错时返回 None
        """
        monitor_scope = ",".join(str(mon_path) for mon_path in mon_paths) or "未配置路径"
        with snapshot_lock:
            try:
                # 加载上次快照数据
                old_snapshot_data = self._store.load(storage)
                old_snapshot = old_snapshot_data.get('snapshot', {}) if old_snapshot_data else {}
                last_snapshot_time = old_snapshot_data.get('timestamp', 0) if old_snapshot_data else 0

                # 判断是否为首次快照：检查快照文件是否存在且有效
                is_first_snapshot = old_snapshot_data is None
                new_snapshot = {}
                for mon_path in mon_paths:
                    logger.debug(f"开始对 {storage}:{mon_path} 进行快照...")

                    # 生成新快照（增量模式）
                    snapshot = StorageChain().snapshot_storage(
                        storage=storage,
                        path=mon_path,
                        last_snapshot_time=last_snapshot_time
                    )

                    if snapshot is None:
                        logger.warn(f"获取 {storage}:{mon_path} 快照失败")
                        continue
                    new_snapshot.update(snapshot)
                    logger.info(f"{storage}:{mon_path} 快照完成，发现 {len(snapshot)} 个文件")
                file_count = len(new_snapshot)
                if not is_first_snapshot:
                    self._handle_changes(storage, old_snapshot, new_snapshot)
                else:
                    logger.info(f"{storage} 首次快照完成，共 {file_count} 个文件")
                    logger.info("*** 首次快照仅建立基准，不会处理现有文件。后续监控将处理新增和修改的文件 ***")

                # 保存新快照
                self._store.save(storage, new_snapshot, file_count, last_snapshot_time)
                return file_count

            except Exception as e:
                logger.error(f"轮询监控 {storage}:{monitor_scope} 出现错误：{e}\n{traceback.format_exc()}")
                return None

    def _handle_changes(self, storage: str, old_snapshot: dict, new_snapshot: dict):
        """
        比对快照并把变化文件送入整理链。
        :param storage: 存储名称
        :param old_snapshot: 旧快照
        :param new_snapshot: 新快照
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

            # 保存快照
            self._store.save(storage, new_snapshot, file_count)

            return True

        except Exception as e:
            logger.error(f"强制全量扫描失败: {storage}:{mon_path} - {e}")
            return False
