from typing import Dict, List, Optional

from app.db import DbOper
from app.db.models.downloadfailure import DownloadFailure


class DownloadFailureOper(DbOper):
    """
    下载失败冷却记录管理。
    """

    def get_active_by_fingerprints(
            self,
            fingerprints: List[str],
            now_time: str,
    ) -> Dict[str, DownloadFailure]:
        """
        批量按指纹查询仍在冷却期的失败记录。
        """
        failures = DownloadFailure.get_active_by_fingerprints(
            self._db,
            fingerprints=fingerprints,
            now_time=now_time,
        )
        return {
            failure.fingerprint: failure
            for failure in failures
            if failure and failure.fingerprint
        }

    def record_failure(
            self,
            fingerprint: str,
            now_time: str,
            next_retry_at: str,
            **kwargs: object,
    ) -> DownloadFailure:
        """
        新增或更新资源失败记录。
        """
        return DownloadFailure.record_failure(
            self._db,
            fingerprint=fingerprint,
            now_time=now_time,
            next_retry_at=next_retry_at,
            **kwargs,
        )

    def delete_expired(
            self,
            before_time: str,
            limit: Optional[int] = 500,
    ) -> int:
        """
        删除已过期较久的失败记录。
        """
        return DownloadFailure.delete_expired(
            self._db,
            before_time=before_time,
            limit=limit,
        )
