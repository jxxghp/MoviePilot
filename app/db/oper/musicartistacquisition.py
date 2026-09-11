"""艺术家作品获取任务的事务内操作。"""

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.musicartistacquisition import MusicArtistAcquisition


class MusicArtistAcquisitionOper(DbOper):
    """只使用调用方传入的 Session，不自行提交。"""

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self._session = db

    def get(self, job_id: str) -> MusicArtistAcquisition | None:
        return cast(
            MusicArtistAcquisition | None,
            self._session.execute(
                select(MusicArtistAcquisition).where(MusicArtistAcquisition.job_id == job_id)
            ).scalar_one_or_none(),
        )

    def find_by_plan_key(self, *, username: str, plan_key: str) -> MusicArtistAcquisition | None:
        return cast(
            MusicArtistAcquisition | None,
            self._session.execute(
                select(MusicArtistAcquisition).where(
                    MusicArtistAcquisition.username == username,
                    MusicArtistAcquisition.plan_key == plan_key,
                )
            ).scalar_one_or_none(),
        )

    def create(self, record: MusicArtistAcquisition) -> MusicArtistAcquisition:
        self._session.add(record)
        self._session.flush()
        return record

    def update(
        self, record: MusicArtistAcquisition, values: dict[str, object]
    ) -> MusicArtistAcquisition:
        record.update(self._session, values)
        self._session.flush()
        return record
