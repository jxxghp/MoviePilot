from pathlib import Path
from types import SimpleNamespace

from app.chain.transfer import TransferChain, _MultiSeasonTransferContext
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfoPath
from app.schemas import FileItem
from app.schemas.types import MediaType


def _fileitem(path: str, item_type: str = "file") -> FileItem:
    file_path = Path(path)
    return FileItem(
        storage="local",
        path=file_path.as_posix(),
        type=item_type,
        name=file_path.name,
        basename=file_path.stem,
        extension=file_path.suffix.lstrip("."),
        size=1024,
    )


def _history(path: str, seasons: str, torrent_name: str = ""):
    return SimpleNamespace(
        path=path,
        seasons=seasons,
        episodes="",
        torrent_name=torrent_name,
        torrent_description="",
    )


def _mediainfo(*season_lengths: int) -> MediaInfo:
    media = MediaInfo()
    media.type = MediaType.TV
    media.seasons = {
        index: list(range(1, season_length + 1))
        for index, season_length in enumerate(season_lengths, start=1)
    }
    return media


def test_multi_season_context_maps_absolute_episode_to_second_season():
    """
    A two-season pack can use global episode numbers while the second top-level
    directory has no explicit S02 marker.
    """
    root = "/downloads/[ReleaseGroup] Placeholder Series Alpha [Lite]"
    first_season = (
        f"{root}/[ReleaseGroup] Placeholder Series Alpha [1080p][Lite]/"
        "[ReleaseGroup] Placeholder Series Alpha [02][1080p].mkv"
    )
    second_season = (
        f"{root}/[ReleaseGroup] Placeholder Series Alpha Arc Two [1080p][Lite]/"
        "[ReleaseGroup] Placeholder Series Alpha Arc Two [14][1080p].mkv"
    )
    context = TransferChain._build_multi_season_context(
        download_history=_history(
            root,
            "S01-S02",
            "Placeholder Series Alpha S01-S02 1080p",
        ),
        file_items=[(_fileitem(first_season), False), (_fileitem(second_season), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    meta = MetaInfoPath(Path(second_season))
    TransferChain._apply_multi_season_context(meta, Path(second_season), context)
    TransferChain._remap_absolute_episode(
        meta, context.source_seasons, _mediainfo(12, 12), Path(second_season)
    )

    assert context.source_seasons == (1, 2)
    assert meta.begin_season == 2
    assert meta.begin_episode == 2
    assert meta.season_episode == "S02 E02"


def test_multi_season_context_maps_second_directory_with_tilde_range():
    """
    A S01~S02 pack should still infer the second top-level directory as season 2
    and map absolute episode 16 to S02E04.
    """
    root = "/downloads/[ReleaseGroup] Placeholder Series Beta S01~S02 [Lite]"
    first_season = (
        f"{root}/[ReleaseGroup] Placeholder Series Beta [1080p][Lite]/"
        "[ReleaseGroup] Placeholder Series Beta [02][1080p].mkv"
    )
    second_season = (
        f"{root}/[ReleaseGroup] Placeholder Series Beta Arc Two [1080p][Lite]/"
        "[ReleaseGroup] Placeholder Series Beta Arc Two [16][1080p].mkv"
    )
    context = TransferChain._build_multi_season_context(
        download_history=_history(
            root,
            "S01~S02",
            "Placeholder Series Beta S01~S02 1080p",
        ),
        file_items=[(_fileitem(first_season), False), (_fileitem(second_season), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    meta = MetaInfoPath(Path(second_season))
    TransferChain._apply_multi_season_context(meta, Path(second_season), context)
    TransferChain._remap_absolute_episode(
        meta, context.source_seasons, _mediainfo(12, 12), Path(second_season)
    )

    assert context.source_seasons == (1, 2)
    assert meta.begin_season == 2
    assert meta.begin_episode == 4
    assert meta.season_episode == "S02 E04"


def test_multi_season_context_keeps_dot_separated_root_directory():
    """
    Dot-separated release names may look like file paths; they still need to be
    treated as torrent root directories when the source item is a directory.
    """
    root = "/downloads/Placeholder.Series.Epsilon.S01-S02.1080p"
    first_season = (
        f"{root}/Placeholder.Series.Epsilon.Part.One/"
        "Placeholder Series Epsilon [02][1080p].mkv"
    )
    second_season = (
        f"{root}/Placeholder.Series.Epsilon.Part.Two/"
        "Placeholder Series Epsilon [14][1080p].mkv"
    )
    context = TransferChain._build_multi_season_context(
        download_history=_history(
            root,
            "S01-S02",
            "Placeholder Series Epsilon S01-S02 1080p",
        ),
        file_items=[(_fileitem(first_season), False), (_fileitem(second_season), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    meta = MetaInfoPath(Path(second_season))
    TransferChain._apply_multi_season_context(meta, Path(second_season), context)
    TransferChain._remap_absolute_episode(
        meta, context.source_seasons, _mediainfo(12, 12), Path(second_season)
    )

    assert context.top_dir_seasons["Placeholder.Series.Epsilon.Part.Two"] == 2
    assert meta.begin_season == 2
    assert meta.begin_episode == 2
    assert meta.season_episode == "S02 E02"


def test_multi_season_context_does_not_expand_enumerated_seasons():
    """
    Enumerated seasons are not continuous ranges; S01&S03 must not imply S02.
    """
    assert TransferChain._parse_multi_season_numbers("Placeholder S01&S03") == (1, 3)
    assert TransferChain._parse_multi_season_numbers("Placeholder S01,S03") == (1, 3)
    assert TransferChain._parse_multi_season_numbers("Placeholder S01&S02&S03") == (1, 2, 3)
    assert TransferChain._parse_multi_season_numbers(
        "Placeholder \u7b2c1\u30012\u30013\u5b63"
    ) == (1, 2, 3)
    assert TransferChain._parse_multi_season_numbers(
        "Placeholder \u7b2c1\u5b63\uff0c\u7b2c2\u5b63\uff0c\u7b2c3\u5b63"
    ) == (1, 2, 3)
    assert TransferChain._parse_multi_season_numbers("Placeholder S01-S03") == (1, 2, 3)
    assert TransferChain._parse_multi_season_numbers("Placeholder S01-02") == (1, 2)
    assert TransferChain._parse_multi_season_numbers("Placeholder S01~02") == (1, 2)
    assert TransferChain._parse_multi_season_numbers(
        "Placeholder \u7b2c1\u5b63-\u7b2c3\u5b63"
    ) == (1, 2, 3)
    assert TransferChain._parse_multi_season_numbers(
        "Placeholder \u7b2c1\u5b63\u3001\u7b2c3\u5b63"
    ) == (1, 3)
    assert TransferChain._extract_single_season_marker("Placeholder S01-02") is None
    assert TransferChain._extract_single_season_marker(
        "Placeholder \u7b2c1\u5b63-\u7b2c3\u5b63"
    ) is None


def test_multi_season_context_accepts_mixed_chinese_numbers():
    """
    Mixed Chinese/Arabic season numbers should be parsed instead of silently
    becoming the wrong value.
    """
    assert TransferChain._cn_number_to_int("\u53412") == 12
    assert TransferChain._cn_number_to_int("2\u5341") == 20
    assert TransferChain._cn_number_to_int("\u5341x") is None


def test_multi_season_context_ignores_parent_directory_ranges():
    """
    A generic parent folder named like a season range must not provide source
    seasons for an unrelated child torrent root.
    """
    root = "/downloads/S01-S02/Placeholder Series Mu"
    source = f"{root}/[ReleaseGroup] Placeholder Series Mu [01][1080p].mkv"
    context = TransferChain._build_multi_season_context(
        download_history=_history(root, "", ""),
        file_items=[(_fileitem(source), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    assert context.source_seasons == ()
    assert context.top_dir_seasons == {}


def test_multi_season_context_requires_explicit_episode():
    """
    Collection-like titles can contain part ranges; without an episode marker
    they should not receive TV season context.
    """
    source = "/downloads/[ReleaseGroup] Placeholder Collection \u7b2c1-3\u90e8.mkv"
    meta = MetaInfoPath(Path(source))
    meta.begin_season = None
    meta.end_season = None
    meta.total_season = 1
    meta.begin_episode = None
    meta.end_episode = None
    context = _MultiSeasonTransferContext(source_seasons=(1, 2, 3))

    changed = TransferChain._apply_multi_season_context(meta, Path(source), context)

    assert changed is False
    assert meta.begin_season is None
    assert meta.begin_episode is None


def test_multi_season_context_accepts_episode_object_lists():
    """
    Season episode lists can contain TMDB-like dicts or objects.
    """
    media = MediaInfo()
    media.type = MediaType.TV
    media.seasons = {
        1: [
            {"episode_number": 1},
            SimpleNamespace(episode_number="2"),
            3,
        ]
    }

    assert TransferChain._season_episode_list(media, 1) == [1, 2, 3]


def test_multi_season_context_sorts_episode_lists():
    """
    Absolute episode mapping should not depend on upstream episode list order.
    """
    media = MediaInfo()
    media.type = MediaType.TV
    media.seasons = {1: [3, 1, 2], 0: [1]}

    assert TransferChain._season_episode_list(media, 1) == [1, 2, 3]
    assert TransferChain._season_episode_list(media, 0) == [1]


def test_multi_season_context_ignores_non_dict_seasons():
    """
    Unexpected season payloads should disable absolute remapping instead of
    interrupting transfer.
    """
    media = MediaInfo()
    media.type = MediaType.TV
    media.seasons = [{"season_number": 1, "episode_count": 3}]

    assert TransferChain._season_episode_list(media, 1) == []


def test_multi_season_context_normalizes_total_fields():
    """
    Applying source context should normalize stale total fields even when the
    parsed season and episode numbers are already correct.
    """
    season_marker = "\u7b2c2\u5b63"
    episode_marker = "\u7b2c03\u8bdd"
    source = (
        "/downloads/[ReleaseGroup] Placeholder Series Theta "
        f"{season_marker} {episode_marker} [1080p].mkv"
    )
    meta = MetaInfoPath(Path(source))
    meta.begin_season = 2
    meta.end_season = None
    meta.total_season = 4
    meta.begin_episode = 3
    meta.end_episode = None
    meta.total_episode = 4
    context = _MultiSeasonTransferContext(source_seasons=(1, 2))

    changed = TransferChain._apply_multi_season_context(meta, Path(source), context)

    assert changed is True
    assert meta.begin_season == 2
    assert meta.end_season is None
    assert meta.total_season == 1
    assert meta.begin_episode == 3
    assert meta.end_episode is None
    assert meta.total_episode == 1


def test_multi_season_context_keeps_zero_season_marker():
    """
    S00 is a valid explicit season marker for specials and should not be treated
    as missing context.
    """
    episode_marker = "\u7b2c01\u8bdd"
    source = (
        "/downloads/[ReleaseGroup] Placeholder Series Iota "
        f"S00 {episode_marker} [1080p].mkv"
    )
    meta = MetaInfoPath(Path(source))
    context = _MultiSeasonTransferContext(source_seasons=(1, 2))

    TransferChain._apply_multi_season_context(meta, Path(source), context)

    assert meta.begin_season == 0
    assert meta.begin_episode == 1


def test_multi_season_context_keeps_explicit_season_local_episode():
    """
    If a path already says S02E14 and season 2 has episode 14, keep it as
    S02E14 instead of treating 14 as a full-pack absolute episode number.
    """
    source = "/downloads/[ReleaseGroup] Placeholder Series Zeta S02E14 [1080p].mkv"
    meta = MetaInfoPath(Path(source))
    meta.begin_season = 2
    meta.end_season = None
    meta.total_season = 1
    meta.begin_episode = 14
    meta.end_episode = None
    meta.total_episode = 1

    changed = TransferChain._remap_absolute_episode(
        meta, (1, 2), _mediainfo(24, 24), Path(source)
    )

    assert changed is False
    assert meta.begin_season == 2
    assert meta.begin_episode == 14
    assert meta.season_episode == "S02 E14"


def test_multi_season_context_reports_status_field_changes():
    """
    Queue state must be updated when remapping only normalizes season status
    fields.
    """
    source = "/downloads/[ReleaseGroup] Placeholder Series Eta [02][1080p].mkv"
    meta = MetaInfoPath(Path(source))
    meta.begin_season = 2
    meta.end_season = 3
    meta.total_season = 2
    meta.begin_episode = 2
    meta.end_episode = None
    meta.total_episode = 1

    changed = TransferChain._remap_absolute_episode(
        meta, (2, 3), _mediainfo(12, 12, 12), Path(source)
    )

    assert changed is True
    assert meta.begin_season == 2
    assert meta.end_season is None
    assert meta.total_season == 1
    assert meta.begin_episode == 2
    assert meta.end_episode is None
    assert meta.total_episode == 1


def test_multi_season_context_keeps_inferred_season_local_episode():
    """
    Once the directory context has inferred season 2, a valid S02 local episode
    should not be remapped as a full-pack absolute episode.
    """
    source = (
        "/downloads/[ReleaseGroup] Placeholder Series Kappa/"
        "[ReleaseGroup] Placeholder Series Kappa Arc Two [02][1080p].mkv"
    )
    meta = MetaInfoPath(Path(source))
    meta.begin_season = 2
    meta.end_season = None
    meta.total_season = 1
    meta.begin_episode = 2
    meta.end_episode = None
    meta.total_episode = 1

    changed = TransferChain._remap_absolute_episode(
        meta, (1, 2), _mediainfo(1, 12), Path(source)
    )

    assert changed is False
    assert meta.begin_season == 2
    assert meta.begin_episode == 2
    assert meta.season_episode == "S02 E02"


def test_multi_season_context_drops_source_seasons_for_explicit_task_season():
    """
    A user-provided task season should not carry source seasons that can remap it
    later in the queue.
    """
    context = _MultiSeasonTransferContext(source_seasons=(1, 2))

    assert TransferChain._task_source_seasons(context, season=2) == []
    assert TransferChain._task_source_seasons(context, season=None) == [1, 2]


def test_multi_season_context_normalizes_backslash_paths():
    """
    Downloader paths can contain backslashes and should use the same POSIX
    normalization as multi-season relative path handling.
    """
    path = r"C:\downloads\Placeholder.Series.Lambda.S01-S02"

    assert (
        TransferChain._normalize_posix_path(path).as_posix()
        == "C:/downloads/Placeholder.Series.Lambda.S01-S02"
    )


def test_multi_season_context_uses_explicit_chinese_season_marker():
    """
    When a file has an explicit Chinese season marker, that local marker wins
    over the root-level multi-season range.
    """
    season_range = "\u7b2c1-3\u671f"
    season_marker = "\u7b2c2\u671f"
    episode_marker = "\u7b2c03\u8bdd"
    root = f"/downloads/[ReleaseGroup] Placeholder Series Gamma {season_range}"
    source = (
        f"{root}/[ReleaseGroup] Placeholder Series Gamma {season_marker} "
        f"{episode_marker} [1080p].mkv"
    )
    context = TransferChain._build_multi_season_context(
        download_history=_history(
            root,
            "S01-S03",
            "Placeholder Series Gamma S1-S3 1080p",
        ),
        file_items=[(_fileitem(source), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    meta = MetaInfoPath(Path(source))
    TransferChain._apply_multi_season_context(meta, Path(source), context)
    TransferChain._remap_absolute_episode(
        meta, context.source_seasons, _mediainfo(12, 12, 12), Path(source)
    )

    assert meta.begin_season == 2
    assert meta.begin_episode == 3
    assert meta.season_episode == "S02 E03"


def test_multi_season_context_keeps_local_episode_with_explicit_season():
    """
    A later season can restart local episode numbers from 01; it must not be
    remapped through the full-pack cumulative episode table.
    """
    season_range = "\u7b2c1-6\u671f"
    season_marker = "\u7b2c4\u671f"
    episode_marker = "\u7b2c01\u8bdd"
    root = f"/downloads/[ReleaseGroup] Placeholder Series Delta {season_range}"
    source = (
        f"{root}/[ReleaseGroup] Placeholder Series Delta Arc Four "
        f"{season_marker} {episode_marker} [1080p].mkv"
    )
    context = TransferChain._build_multi_season_context(
        download_history=_history(
            root,
            "S01-S06",
            "Placeholder Series Delta S1-S6 1080p",
        ),
        file_items=[(_fileitem(source), False)],
        source_fileitem=_fileitem(root, item_type="dir"),
    )

    meta = MetaInfoPath(Path(source))
    TransferChain._apply_multi_season_context(meta, Path(source), context)
    TransferChain._remap_absolute_episode(
        meta, context.source_seasons, _mediainfo(13, 12, 12, 12, 1, 2), Path(source)
    )

    assert meta.begin_season == 4
    assert meta.begin_episode == 1
    assert meta.season_episode == "S04 E01"
