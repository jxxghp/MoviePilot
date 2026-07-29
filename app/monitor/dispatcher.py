import re
import traceback
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional

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

    def handle_file(self, storage: str, event_path: Path, file_size: float = None) -> bool:
        """
        整理一个文件。
        :param storage: 存储
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        :return: 是否进入整理链
        """
        with self._lock:
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
            if has_transfer_history is not False:
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
