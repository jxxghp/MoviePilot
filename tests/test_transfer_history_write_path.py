"""
整理历史的写入路径：add_transfer_success / add_transfer_fail。

这两个函数是整理历史表的**唯一**写入口，整理链的每一次成败都经由它们落库。
它们干的不是数据访问，而是把 FileItem / MetaBase / MediaInfo / TransferInfo
四个领域对象翻译成一行历史——字段映射错了不会报错，只会让历史里的季集、标题、
来源静静地记错，而整理查重、媒体库溯源、失败重试全都读这张表。

因此这里断言的是「落库后每个字段的实际值」，不是「调用了什么」。

它们与同一张表的读侧规则（查重闸，见 test_transfer_history_gate.py）同住
app/application/history.py；此前长在 TransferHistoryOper 上，故本文件旧名为
test_db_transferhistory_write_path.py。
"""
import pytest

from app import schemas
from app.application.history import add_transfer_fail, add_transfer_success
from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
from app.db.models.transferhistory import TransferHistory
from app.db.session import SessionFactory, async_session_scope
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.types import MediaSource, MediaType


@pytest.fixture(autouse=True)
def _track(db):
    """把整理历史表纳入用例级回收。"""
    db.watermark(TransferHistory)


def _fileitem(path: str, storage: str = "local") -> schemas.FileItem:
    """构造源文件项。"""
    return schemas.FileItem(storage=storage, path=path,
                            name=path.rsplit("/", 1)[-1], type="file")


def _repository() -> TransactionalTransferHistoryRepository:
    """构造类型化整理历史仓储。"""
    return TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def _transferinfo(dest: str = "/media/片名/Season 01/片名 - S01E02.mkv",
                  message: str = None, with_target: bool = True) -> schemas.TransferInfo:
    """构造整理结果。"""
    target = _fileitem(dest) if with_target else None
    return schemas.TransferInfo(success=not message,
                                fileitem=_fileitem("/downloads/片名.S01E02.2026.mkv"),
                                target_item=target,
                                file_list=["/downloads/片名.S01E02.2026.mkv"],
                                message=message)


def _mediainfo(title: str = "识别标题", mtype: MediaType = MediaType.TV,
               year: str = "2026", category: str = "国产剧",
               media_id: str = "5566", episode_group: str = None) -> MediaInfo:
    """构造识别结果。"""
    media = MediaInfo()
    media.type = mtype
    media.title = title
    media.year = year
    media.category = category
    media.media_source = MediaSource.TMDB
    media.media_id = media_id
    media.episode_group = episode_group
    return media


# --------------------------------------------------------------------------- #
# add_transfer_success
# --------------------------------------------------------------------------- #

def test_add_success_maps_every_field_onto_the_row(db):
    """
    成功整理的字段映射必须完整落库。

    源/目标各自带存储、季集来自识别结果、状态为成功——这些是整理查重与媒体库
    溯源的全部依据，错一项就查不回来。
    """
    oper = _repository()
    meta = MetaInfo("片名.S01E02.2026.mkv")

    add_transfer_success(transfer_history_oper=oper,
                         fileitem=_fileitem("/downloads/片名.S01E02.2026.mkv"),
                         mode="move", meta=meta, mediainfo=_mediainfo(),
                         transferinfo=_transferinfo(),
                         downloader="qbittorrent", download_hash="hash-ok")

    row = oper.get_by_src("/downloads/片名.S01E02.2026.mkv")
    assert row.status is True
    assert (row.src_storage, row.dest_storage) == ("local", "local")
    assert row.dest == "/media/片名/Season 01/片名 - S01E02.mkv"
    assert (row.mode, row.type, row.category) == ("move", MediaType.TV.value, "国产剧")
    assert (row.title, row.year) == ("识别标题", "2026")
    assert (row.seasons, row.episodes) == ("S01", "E02")
    assert (row.downloader, row.download_hash) == ("qbittorrent", "hash-ok")
    assert row.files == ["/downloads/片名.S01E02.2026.mkv"]
    assert row.errmsg is None


@pytest.mark.parametrize("failed", [False, True])
def test_identified_transfer_history_persists_effective_classification(
    db,
    failed: bool,
) -> None:
    """整理成功和已识别失败都必须保存最终分类的完整执行快照。"""
    oper = _repository()
    media = _mediainfo(category="不得作为真值")
    media.classification = ClassificationResult(
        recommended=ClassificationSelection(
            category_id="tv.auto",
            category_path=["自动"],
            rule_id="rule.auto",
            source="automatic",
        ),
        effective=ClassificationSelection(
            category_id="tv.manual",
            category_path=["剧集", "收藏"],
            source="subscription",
        ),
        policy_revision=13,
        state="complete",
    )
    media.set_library_category("剧集/收藏")
    source = f"/downloads/classification-{'fail' if failed else 'success'}.mkv"

    if failed:
        add_transfer_fail(
            transfer_history_oper=oper,
            fileitem=_fileitem(source),
            mode="copy",
            meta=MetaInfo("classification.mkv"),
            mediainfo=media,
            transferinfo=_transferinfo(message="失败"),
        )
    else:
        add_transfer_success(
            transfer_history_oper=oper,
            fileitem=_fileitem(source),
            mode="copy",
            meta=MetaInfo("classification.mkv"),
            mediainfo=media,
            transferinfo=_transferinfo(),
        )

    row = oper.get_by_src(source)
    assert row.media_category_id == "tv.manual"
    assert row.category == "剧集/收藏"
    assert row.classification_rule_id is None
    assert row.classification_policy_revision == 13
    assert row.classification_source == "subscription"


def test_add_success_persists_both_file_items(db):
    """
    源与目标的完整文件项都要落库。

    整理链回滚、重新整理都要拿原始 FileItem 复原，只存路径字符串不够。
    """
    oper = _repository()
    add_transfer_success(transfer_history_oper=oper,
                         fileitem=_fileitem("/downloads/item.mkv"), mode="link",
                         meta=MetaInfo("item.mkv"), mediainfo=_mediainfo(),
                         transferinfo=_transferinfo(dest="/media/item.mkv"))

    row = oper.get_by_src("/downloads/item.mkv")
    assert row.src_fileitem["path"] == "/downloads/item.mkv"
    assert row.dest_fileitem["path"] == "/media/item.mkv"


def test_add_success_tolerates_missing_target_item(db):
    """
    没有目标文件项时，目标三项一并留空而不是崩。

    某些存储的整理只回传结果不回传条目，硬取 target_item.path 会直接抛。
    """
    oper = _repository()
    add_transfer_success(transfer_history_oper=oper,
                         fileitem=_fileitem("/downloads/no-target.mkv"), mode="copy",
                         meta=MetaInfo("no-target.mkv"), mediainfo=_mediainfo(),
                         transferinfo=_transferinfo(with_target=False))

    row = oper.get_by_src("/downloads/no-target.mkv")
    assert (row.dest, row.dest_storage, row.dest_fileitem) == (None, None, None)
    assert row.status is True


def test_add_success_replaces_the_previous_record_of_the_same_source(db):
    """
    同一源路径重复整理只保留最新一条。

    经同源替换的先删后插实现；留下旧行会让查重命中过期的目标路径。
    """
    oper = _repository()
    for title in ("第一次", "第二次"):
        add_transfer_success(transfer_history_oper=oper,
                             fileitem=_fileitem("/downloads/dup.mkv"), mode="move",
                             meta=MetaInfo("dup.mkv"),
                             mediainfo=_mediainfo(title=title),
                             transferinfo=_transferinfo())

    rows = oper.list_success_by_src("/downloads/dup.mkv")
    assert [r.title for r in rows] == ["第二次"]


def test_add_success_prefers_track_title_for_music(db):
    """
    音乐文件记录曲目标题，而不是识别出的专辑/艺人名。

    音乐库按曲目组织，记成专辑名会让单曲在历史里全部重名、无法区分。
    """
    oper = _repository()
    music_meta = MetaInfo("周杰伦 - 晴天.flac")

    add_transfer_success(transfer_history_oper=oper,
                         fileitem=_fileitem("/downloads/晴天.flac"), mode="move",
                         meta=music_meta,
                         mediainfo=_mediainfo(title="叶惠美", mtype=MediaType.MUSIC),
                         transferinfo=_transferinfo(dest="/media/晴天.flac"))

    assert oper.get_by_src("/downloads/晴天.flac").title == "晴天"


def test_add_success_falls_back_to_recognized_title(db):
    """
    非音乐文件用识别标题，识别标题为空时退回文件名解析出的名字。
    """
    oper = _repository()
    meta = MetaInfo("片名.S01E02.2026.mkv")

    add_transfer_success(transfer_history_oper=oper,
                         fileitem=_fileitem("/downloads/fallback.mkv"), mode="move",
                         meta=meta, mediainfo=_mediainfo(title=None),
                         transferinfo=_transferinfo())

    assert oper.get_by_src("/downloads/fallback.mkv").title == meta.name


# --------------------------------------------------------------------------- #
# add_transfer_fail
# --------------------------------------------------------------------------- #

def test_add_fail_records_the_transfer_error_message(db):
    """
    整理失败时状态为失败，并保留具体错误信息。

    失败重试与人工排障都只能看这条 errmsg。
    """
    oper = _repository()

    add_transfer_fail(transfer_history_oper=oper,
                      fileitem=_fileitem("/downloads/fail.mkv"), mode="move",
                      meta=MetaInfo("fail.mkv"), mediainfo=_mediainfo(),
                      transferinfo=_transferinfo(message="目标路径不可写"))

    row = oper.get_by_src("/downloads/fail.mkv")
    assert row.status is False
    assert row.errmsg == "目标路径不可写"
    assert row.title == "识别标题"


def test_add_fail_uses_a_default_message_when_none_given(db):
    """
    整理结果没带错误信息时落一个兜底文案，不能留空。

    留空会让失败记录在界面上显示成「无错误」，与成功记录无从区分。
    """
    oper = _repository()

    add_transfer_fail(transfer_history_oper=oper,
                      fileitem=_fileitem("/downloads/nomsg.mkv"), mode="move",
                      meta=MetaInfo("nomsg.mkv"), mediainfo=_mediainfo(),
                      transferinfo=_transferinfo(message=""))

    assert oper.get_by_src("/downloads/nomsg.mkv").errmsg == "未知错误"


def test_add_fail_persists_episode_group(db):
    """
    失败记录要带上剧集组——重试时靠它还原到同一个剧集组，否则会整理错季。
    """
    oper = _repository()

    add_transfer_fail(transfer_history_oper=oper,
                      fileitem=_fileitem("/downloads/eg.mkv"), mode="move",
                      meta=MetaInfo("eg.mkv"),
                      mediainfo=_mediainfo(episode_group="eg-1"),
                      transferinfo=_transferinfo(message="出错"))

    assert oper.get_by_src("/downloads/eg.mkv").episode_group == "eg-1"


def test_add_fail_without_recognition_takes_the_unidentified_branch(db):
    """
    未识别到媒体信息时走另一条分支：错误文案固定，且不写目标路径。

    这条分支是「文件识别不出来」的唯一记录方式，丢了这些文件就彻底无迹可寻。
    """
    oper = _repository()
    meta = MetaInfo("无法识别的文件.S02E05.2020.mkv")

    add_transfer_fail(transfer_history_oper=oper,
                      fileitem=_fileitem("/downloads/unknown.mkv"), mode="move",
                      meta=meta)

    row = oper.get_by_src("/downloads/unknown.mkv")
    assert row.status is False
    assert row.errmsg == "未识别到媒体信息"
    assert row.dest is None
    assert (row.seasons, row.episodes) == ("S02", "E05")
    assert row.year == meta.year
    assert row.title == meta.name


def test_add_fail_unidentified_music_is_marked_as_a_recording(db):
    """
    未识别的音乐文件按单曲登记实体类型。

    音乐订阅按单曲/专辑分别查重，实体类型为空会让这条历史两边都匹配不上。
    """
    oper = _repository()

    add_transfer_fail(transfer_history_oper=oper,
                      fileitem=_fileitem("/downloads/unknown.flac"), mode="move",
                      meta=MetaInfo("周杰伦 - 晴天.flac"))

    row = oper.get_by_src("/downloads/unknown.flac")
    assert row.music_type == "recording"
    assert row.type == MediaType.MUSIC.value
    assert row.title == "晴天"


def test_add_fail_returns_the_persisted_row(db):
    """
    两条分支都要把落库后的记录返回，调用方据此拿主键做后续关联。
    """
    oper = _repository()

    identified = add_transfer_fail(transfer_history_oper=oper,
                                   fileitem=_fileitem("/downloads/r1.mkv"), mode="move",
                                   meta=MetaInfo("r1.mkv"), mediainfo=_mediainfo(),
                                   transferinfo=_transferinfo(message="错误"))
    unidentified = add_transfer_fail(transfer_history_oper=oper,
                                     fileitem=_fileitem("/downloads/r2.mkv"), mode="move",
                                     meta=MetaInfo("r2.mkv"))

    assert identified.id is not None
    assert unidentified.id is not None
    assert identified.id != unidentified.id
