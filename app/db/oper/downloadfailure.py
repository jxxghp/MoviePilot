from typing import Dict, List, Optional

from app.db.base import DbOper
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
        failures = self._execute_sync_query(
            lambda session: DownloadFailure.get_active_by_fingerprints(
                session,
                fingerprints=fingerprints,
                now_time=now_time,
            )
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
        return self._execute_sync_write(
            lambda session: DownloadFailure.record_failure(
                session,
                fingerprint=fingerprint,
                now_time=now_time,
                next_retry_at=next_retry_at,
                **kwargs,
            )
        )

    def delete_expired(
            self,
            before_time: str,
            limit: Optional[int] = 500,
    ) -> int:
        """
        删除已过期较久的失败记录。
        """
        return self._execute_sync_write(
            lambda session: DownloadFailure.delete_expired(
                session,
                before_time=before_time,
                limit=limit,
            )
        )
