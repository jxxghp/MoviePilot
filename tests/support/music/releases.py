"""
音乐发行组回放及识别装配；不注册测试用例。
"""

import shutil
from pathlib import Path
from mutagen.flac import FLAC
from app.application.audio import AudioMetadataHelper
from app.schemas.types import MediaType
from tests.support.transfer.files import make_fileitem, make_transfer_chain


RELEASE_A = "11111111-1111-4111-8111-111111111111"


RELEASE_B = "22222222-2222-4222-8222-222222222222"


def _audio(path: Path, *, artist="Artist", album="Record", release=None, release_type=None, disc=None, compilation=False):
    """
    写入真实 FLAC 标签，省略总碟数以验证目录补缺。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).parents[2] / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio["title"] = [path.stem]
    audio["tracknumber"] = ["1"]
    audio["date"] = ["2003"]
    for key, value in (("artist", artist), ("album", album), ("discnumber", disc),
                       ("musicbrainz_albumid", release), ("releasetype", release_type)):
        if value is not None:
            audio[key] = [str(value)]
    if compilation:
        audio["compilation"] = ["1"]
    audio.save()
    return make_fileitem(str(path))


def _prepare(items, mtype=MediaType.MUSIC):
    """
    构建隔离整理批次，保留生产读取及分组函数。
    """
    owner = make_transfer_chain()
    context = owner._prepare_music_batch_context([(item, False) for item in items], mtype)
    return owner, context


def _resolve(owner, context, item):
    """
    从批次读取真实文件的最终局部音乐上下文，不执行文件副作用。
    """
    return owner._resolve_music_batch_file_context(
        batch_context=context, file_item=item, file_path=Path(item.path),
        file_meta=AudioMetadataHelper.read(Path(item.path)), selected_tracks={}, fallback=None,
        discard_shared_identity=True, multi_track_batch=True, release_regions=None, release_scripts=None,
    )
