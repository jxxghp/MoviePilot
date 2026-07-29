import re
import traceback
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from app.chain.transfer import TransferChain
from app.core.cache import TTLCache
from app.core.config import settings
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.schemas import FileItem


class TransferDispatcher:
    """
    将监控事件分发到整理链：候选判定、TTL 去重、整理历史查重与整理触发。
    """
    # 历史查询失败待重试队列上限，防止长时间故障期间无限增长
    MAX_PENDING_RETRIES = 1000
    # 单个文件的最大重试次数（按健康检查周期计，60 次约 1 小时）
    MAX_RETRY_ATTEMPTS = 60

    def __init__(self, all_exts: Optional[List[str]] = None, cache: Optional[Any] = None):
        """
        初始化整理分发器。
        :param all_exts: 监控的文件扩展名，默认取系统配置
        :param cache: 去重缓存，默认使用 10 秒 TTL 缓存
        """
        self.all_exts = all_exts if all_exts is not None else (
                settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT)
        self._cache = cache if cache is not None else TTLCache(region="monitor", maxsize=1024, ttl=10)
        self._lock = Lock()
        # 历史查询失败待重试的文件
        self._pending_retries: Dict[str, Dict[str, Any]] = {}
        self._pending_guard = Lock()

    @staticmethod
    def _is_bluray_sub(_path: Path) -> bool:
        """
        判断是否蓝光原盘目录内的媒体流文件。
        """
        return True if re.search(r"BDMV[/\\]STREAM", _path.as_posix(), re.IGNORECASE) else False

    @staticmethod
    def _get_bluray_dir(_path: Path) -> Optional[Path]:
        """
        获取蓝光原盘BDMV目录的上级目录。
        """
        for p in _path.parents:
            if p.name == "BDMV":
                return p.parent
        return None

    @staticmethod
    def _has_suffix_in(file_path: Path, extensions: List[str]) -> bool:
        """
        判断路径后缀是否命中给定扩展名列表。
        """
        if not file_path.suffix:
            return False
        return file_path.suffix.casefold() in {ext.casefold() for ext in extensions}

    def is_transfer_candidate_path(self, file_path: Path) -> bool:
        """
        判断监控事件路径是否需要进入整理链。
        """
        if self._has_suffix_in(file_path, settings.DOWNLOAD_TMPEXT):
            return False
        return self._has_suffix_in(file_path, self.all_exts)

    @staticmethod
    def _build_transfer_src_path(event_path: Path, is_bluray_folder: bool) -> str:
        """
        生成整理记录使用的源路径。
        """
        if is_bluray_folder:
            return f"{event_path.as_posix()}/"
        return event_path.as_posix()

    @staticmethod
    def _has_transfer_history(storage: str, src_path: str) -> Optional[bool]:
        """
        判断源文件是否已经存在整理记录。
        :return: True/False 查询成功，None 查询失败
        """
        try:
            return bool(TransferHistoryOper().get_by_src(src_path, storage=storage))
        except Exception as err:
            logger.error(f"查询整理历史失败: {src_path} - {err}")
            return None

    @staticmethod
    def _pending_key(storage: str, event_path: Path) -> str:
        """
        生成待重试文件的唯一键。
        """
        return f"{storage}:{Path(event_path).as_posix()}"

    def _register_pending(self, storage: str, event_path: Path, file_size: float = None):
        """
        登记历史查询失败的文件待重试，重复失败累计次数，超限后放弃。
        :param storage: 存储
        :param event_path: 原始事件路径
        :param file_size: 文件大小
        """
        key = self._pending_key(storage, event_path)
        with self._pending_guard:
            entry = self._pending_retries.get(key)
            if entry:
                entry["attempts"] += 1
                if entry["attempts"] >= self.MAX_RETRY_ATTEMPTS:
                    self._pending_retries.pop(key, None)
                    logger.error(f"整理历史查询持续失败，已放弃重试: {key}")
                return
            if len(self._pending_retries) >= self.MAX_PENDING_RETRIES:
                logger.error(f"整理重试队列已满，丢弃: {key}")
                return
            self._pending_retries[key] = {
                "storage": storage,
                "event_path": event_path,
                "file_size": file_size,
                "attempts": 1
            }
        logger.warn(f"整理历史查询失败，已登记待重试: {key}")

    def _discard_pending(self, storage: str, event_path: Path):
        """
        历史查询已得到确定结果，移除待重试登记。
        :param storage: 存储
        :param event_path: 原始事件路径
        """
        with self._pending_guard:
            self._pending_retries.pop(self._pending_key(storage, event_path), None)

    def retry_pending(self):
        """
        重试历史查询失败的文件，由健康检查周期驱动。
        成功或得到确定结果的条目在 handle_file 内部自动移除。
        """
        with self._pending_guard:
            items = list(self._pending_retries.values())
        for item in items:
            logger.info(f"重试整理: {item['storage']}:{item['event_path']}")
            self.handle_file(storage=item["storage"], event_path=item["event_path"],
                             file_size=item["file_size"])

    def handle_file(self, storage: str, event_path: Path, file_size: float = None) -> bool:
        """
        整理一个文件。
        :param storage: 存储
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        :return: 是否进入整理链
        """
        with self._lock:
            # 登记重试用原始事件路径，蓝光目录解析在重试时重新执行
            origin_path = event_path
            is_bluray_folder = False
            # 蓝光原盘文件处理
            if self._is_bluray_sub(event_path):
                event_path = self._get_bluray_dir(event_path)
                if not event_path:
                    return False
                is_bluray_folder = True
            elif not self.is_transfer_candidate_path(event_path):
                return False

            # TTL缓存控重
            if self._cache.get(str(event_path)):
                return False
            self._cache[str(event_path)] = True

            src_path = self._build_transfer_src_path(
                event_path=event_path,
                is_bluray_folder=is_bluray_folder,
            )
            has_transfer_history = self._has_transfer_history(
                storage=storage,
                src_path=src_path,
            )
            if has_transfer_history is None:
                # 查询失败是暂时故障，登记待重试（由健康检查周期驱动），不能永久跳过
                self._register_pending(storage=storage, event_path=origin_path, file_size=file_size)
                return False
            self._discard_pending(storage=storage, event_path=origin_path)
            if has_transfer_history:
                return False

            try:
                if is_bluray_folder:
                    logger.info(f"开始整理蓝光原盘: {event_path}")
                else:
                    logger.info(f"开始整理文件: {event_path}")
                # 开始整理
                TransferChain().do_transfer(
                    fileitem=FileItem(
                        storage=storage,
                        path=src_path,
                        type="file" if not is_bluray_folder else "dir",
                        name=event_path.name,
                        basename=event_path.stem,
                        extension=event_path.suffix[1:],
                        size=file_size
                    )
                )
                return True
            except Exception as e:
                logger.error("目录监控整理文件发生错误：%s - %s" % (str(e), traceback.format_exc()))
                return False
