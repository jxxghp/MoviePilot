"""艺术家合集覆盖与聚合任务规则测试。"""

from dataclasses import replace

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.application.music.acquisition import (
    ArtistAcquisitionSnapshot,
    ArtistWork,
    configure_artist_acquisition_repository,
    evaluate_collection_coverage,
    is_artist_collection_resource,
)
from app.chain.musicacquisition import MusicArtistAcquisitionChain
from app.db.adapters.musicartistacquisition import (
    TransactionalMusicArtistAcquisitionRepository,
)
from app.db.models.musicartistacquisition import MusicArtistAcquisition


def test_collection_title_year_range_is_only_probable_coverage() -> None:
    coverage = evaluate_collection_coverage(
        works=(ArtistWork(media_id="rg-1", title="Need This", year=2002),),
        file_paths=("Artist/Unknown Album/01.flac",),
        resource_title="Artist Complete Discography 2001-2003 FLAC",
    )

    assert coverage.confirmed_count == 0
    assert coverage.probable_count == 1
    assert coverage.works[0].state == "probable"


def test_collection_file_path_confirms_release_title_and_year() -> None:
    coverage = evaluate_collection_coverage(
        works=(
            ArtistWork(
                media_id="rg-1",
                title="自定义",
                year=2009,
                title_aliases=("Custom Definition",),
            ),
        ),
        file_paths=("Vae Xu/Custom Definition (2009)/01.flac",),
        resource_title="Vae Xu Music Collection",
        folder_name="Vae Xu Discography",
    )

    assert coverage.confirmed_count == 1
    assert coverage.missing_count == 0
    assert coverage.works[0].state == "confirmed"
    assert "Custom Definition" in coverage.works[0].evidence


def test_resource_title_match_is_only_probable_without_internal_path() -> None:
    coverage = evaluate_collection_coverage(
        works=(ArtistWork(media_id="rg-1", title="Hotel California", year=1976),),
        file_paths=(),
        resource_title="Eagles - Hotel California (1976) Collection",
    )

    assert coverage.confirmed_count == 0
    assert coverage.probable_count == 1
    assert coverage.works[0].state == "probable"


def test_artist_collection_signal_supports_chinese_and_english_terms() -> None:
    assert is_artist_collection_resource("许嵩 2006-2022 专辑合集")
    assert is_artist_collection_resource("Vae Xu Complete Discography FLAC")
    assert not is_artist_collection_resource("Vae Xu - 自定义 (2009)")


def test_artist_acquisition_repository_round_trips_json_plan() -> None:
    engine = sa.create_engine("sqlite://")
    MusicArtistAcquisition.__table__.create(engine)
    repository = TransactionalMusicArtistAcquisitionRepository(sessionmaker(bind=engine))
    snapshot = ArtistAcquisitionSnapshot(
        job_id="job-1",
        plan_key="a" * 64,
        username="admin",
        artist_source="musicbrainz",
        artist_id="artist-1",
        artist_name="Artist",
        state="submitting",
        scope={"works": [{"media_id": "rg-1"}]},
        plan={"collection": {"title": "Artist Discography"}},
        created_at="2026-09-11T00:00:00+00:00",
        updated_at="2026-09-11T00:00:00+00:00",
    )

    stored = repository.create(snapshot)
    updated = repository.update(replace(stored, state="submitted", results=[{"download_id": "hash"}]))

    assert repository.find_by_plan_key(username="admin", plan_key="a" * 64) == updated
    assert repository.get("job-1") == updated
    engine.dispose()


def test_aggregate_task_submits_collection_before_missing_supplements(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    MusicArtistAcquisition.__table__.create(engine)
    repository = TransactionalMusicArtistAcquisitionRepository(sessionmaker(bind=engine))
    configure_artist_acquisition_repository(repository)
    calls: list[dict] = []

    class FakeDownloadChain:
        def download_single(self, **kwargs):
            calls.append(kwargs)
            return f"hash-{len(calls)}"

    monkeypatch.setattr("app.chain.musicacquisition.DownloadChain", FakeDownloadChain)
    try:
        result = MusicArtistAcquisitionChain().submit(
            username="admin",
            artist_source="musicbrainz",
            artist_id="artist-1",
            artist_name="Artist",
            works=(
                {
                    "type": "音乐",
                    "music_type": "album",
                    "media_source": "musicbrainz",
                    "media_id": "rg-1",
                    "title": "Album One",
                    "album_type": "Album",
                },
            ),
            collection={
                "torrent": {"title": "Artist Discography", "enclosure": "collection"},
                "coverage": [{"media_id": "rg-1", "state": "confirmed"}],
            },
            supplements=(
                {
                    "media": {
                        "type": "音乐",
                        "music_type": "album",
                        "media_source": "musicbrainz",
                        "media_id": "rg-2",
                        "title": "Album Two",
                        "album_type": "Album",
                    },
                    "torrent": {"title": "Album Two FLAC", "enclosure": "supplement"},
                },
            ),
            normalize_source=True,
        )
    finally:
        configure_artist_acquisition_repository(None)
        engine.dispose()

    assert result.state == "submitted"
    assert result.covered_count == 1
    assert [item["role"] for item in result.results] == ["collection", "supplement"]
    assert calls[0]["context"].media_info.library_category == "Artist Collection"
    assert calls[1]["context"].media_info.media_id == "rg-2"
    assert all(call["source"] == "ArtistAcquisition" for call in calls)
    assert all(call["normalize_source"] is True for call in calls)
