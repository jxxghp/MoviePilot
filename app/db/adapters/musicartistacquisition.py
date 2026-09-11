"""SQLAlchemy 艺术家作品获取任务仓储。"""

from collections.abc import Callable
from dataclasses import asdict

from sqlalchemy.orm import Session

from app.application.music.acquisition import ArtistAcquisitionSnapshot
from app.db.models.musicartistacquisition import MusicArtistAcquisition
from app.db.oper.musicartistacquisition import MusicArtistAcquisitionOper
from app.db.uow import SqlAlchemyUnitOfWork


def _snapshot(record: MusicArtistAcquisition) -> ArtistAcquisitionSnapshot:
    return ArtistAcquisitionSnapshot(
        job_id=record.job_id,
        plan_key=record.plan_key,
        username=record.username,
        artist_source=record.artist_source,
        artist_id=record.artist_id,
        artist_name=record.artist_name,
        state=record.state,
        scope=dict(record.scope or {}),
        plan=dict(record.plan or {}),
        results=list(record.results or []),
        total_count=record.total_count,
        covered_count=record.covered_count,
        supplement_count=record.supplement_count,
        failed_count=record.failed_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
    )


class TransactionalMusicArtistAcquisitionRepository:
    """为每次任务读写创建独立短事务。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get(self, job_id: str) -> ArtistAcquisitionSnapshot | None:
        with self._session_factory() as session:
            record = MusicArtistAcquisitionOper(session).get(job_id)
            return _snapshot(record) if record else None

    def find_by_plan_key(self, *, username: str, plan_key: str) -> ArtistAcquisitionSnapshot | None:
        with self._session_factory() as session:
            record = MusicArtistAcquisitionOper(session).find_by_plan_key(username=username, plan_key=plan_key)
            return _snapshot(record) if record else None

    def create(self, snapshot: ArtistAcquisitionSnapshot) -> ArtistAcquisitionSnapshot:
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                record = MusicArtistAcquisition(**asdict(snapshot))
                stored = MusicArtistAcquisitionOper(session).create(record)
                result = _snapshot(stored)
                transaction.commit()
                return result
            except Exception:
                transaction.rollback()
                raise

    def update(self, snapshot: ArtistAcquisitionSnapshot) -> ArtistAcquisitionSnapshot:
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = MusicArtistAcquisitionOper(session)
                record = oper.get(snapshot.job_id)
                if record is None:
                    raise LookupError(f"艺术家作品任务不存在：{snapshot.job_id}")
                values = {
                    field: getattr(snapshot, field)
                    for field in snapshot.__dataclass_fields__
                    if field not in {"job_id", "plan_key", "username"}
                }
                stored = oper.update(record, values)
                result = _snapshot(stored)
                transaction.commit()
                return result
            except Exception:
                transaction.rollback()
                raise
