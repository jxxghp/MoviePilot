import json
import time
from typing import Dict, List, Optional, Tuple

from app.core.cache import FileCache
from app.core.config import settings
from app.log import logger


class SnapshotStore:
    """
    远程目录监控快照的存取与比对。
    """

    def __init__(self, cache: Optional[FileCache] = None):
        """
        初始化快照存储。
        :param cache: 快照文件缓存，默认使用 CACHE_PATH/snapshots
        """
        self._cache = cache if cache is not None else FileCache(base=settings.CACHE_PATH / "snapshots")

    def save(self, storage: str, snapshot: Dict, file_count: int = 0,
             last_snapshot_time: Optional[float] = None) -> bool:
        """
        保存快照到文件缓存。
        :param storage: 存储名称
        :param snapshot: 快照数据
        :param file_count: 文件数量，用于调整监控间隔
        :param last_snapshot_time: 上次快照时间戳
        :return: 是否保存成功
        """
        try:
            snapshot_time = max((item.get('modify_time', 0) for item in snapshot.values()), default=None)
            if snapshot_time is None:
                snapshot_time = last_snapshot_time or time.time()
            snapshot_data = {
                'timestamp': snapshot_time,
                'file_count': file_count,
                'snapshot': snapshot
            }
            cache_key = f"{storage}_snapshot"
            snapshot_json = json.dumps(snapshot_data, ensure_ascii=False, indent=2)
            self._cache.set(cache_key, snapshot_json.encode('utf-8'), region="snapshots")
            logger.debug(f"快照已保存到缓存: {storage}")
            return True
        except Exception as e:
            logger.error(f"保存快照失败: {e}")
            return False

    def load_checked(self, storage: str) -> Tuple[Optional[Dict], bool]:
        """
        从文件缓存加载快照，并区分「快照不存在」与「读取失败」。
        读取失败时不能当作首次快照处理，否则会静默丢弃已有基线。
        :param storage: 存储名称
        :return: (快照数据或None, 是否读取成功)
        """
        try:
            cache_key = f"{storage}_snapshot"
            snapshot_data = self._cache.get(cache_key, region="snapshots")
            if snapshot_data:
                data = json.loads(snapshot_data.decode('utf-8'))
                logger.debug(f"成功加载快照: {storage}, 包含 {len(data.get('snapshot', {}))} 个文件")
                return data, True
            logger.debug(f"快照文件不存在: {storage}")
            return None, True
        except Exception as e:
            logger.error(f"加载快照失败: {e}")
            return None, False

    def load(self, storage: str) -> Optional[Dict]:
        """
        从文件缓存加载快照。
        :param storage: 存储名称
        :return: 快照数据或None
        """
        data, _ = self.load_checked(storage)
        return data

    def reset(self, storage: str) -> bool:
        """
        重置快照，强制下次扫描时重新建立基准。
        :param storage: 存储名称
        :return: 是否成功
        """
        try:
            cache_key = f"{storage}_snapshot"
            if self._cache.exists(cache_key, region="snapshots"):
                self._cache.delete(cache_key, region="snapshots")
                logger.info(f"快照已重置: {storage}")
                return True
            logger.debug(f"快照文件不存在，无需重置: {storage}")
            return True
        except Exception as e:
            logger.error(f"重置快照失败: {storage} - {e}")
            return False

    @staticmethod
    def compare(old_snapshot: Dict, new_snapshot: Dict) -> Dict[str, List]:
        """
        比对快照，找出变化的文件（只处理新增和修改，不处理删除）。
        :param old_snapshot: 旧快照
        :param new_snapshot: 新快照
        :return: 变化信息
        """
        changes = {
            'added': [],
            'modified': []
        }

        old_files = set(old_snapshot.keys())
        new_files = set(new_snapshot.keys())

        # 新增文件
        changes['added'] = list(new_files - old_files)

        # 修改文件（大小或时间变化）
        for file_path in old_files & new_files:
            old_info = old_snapshot[file_path]
            new_info = new_snapshot[file_path]

            # 检查文件大小变化
            old_size = old_info.get('size', 0) if isinstance(old_info, dict) else old_info
            new_size = new_info.get('size', 0) if isinstance(new_info, dict) else new_info

            # 检查修改时间变化（如果有的话）
            old_time = old_info.get('modify_time', 0) if isinstance(old_info, dict) else 0
            new_time = new_info.get('modify_time', 0) if isinstance(new_info, dict) else 0

            if old_size != new_size or (old_time and new_time and old_time != new_time):
                changes['modified'].append(file_path)

        return changes

    @staticmethod
    def adjust_interval(file_count: int) -> int:
        """
        根据文件数量动态调整监控间隔。
        :param file_count: 文件数量
        :return: 监控间隔（分钟）
        """
        if file_count < 100:
            return 5  # 5分钟
        elif file_count < 500:
            return 10  # 10分钟
        elif file_count < 1000:
            return 15  # 15分钟
        else:
            return 30  # 30分钟
