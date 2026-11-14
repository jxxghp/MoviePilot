"""
音乐专辑订阅的成功整理事实对账。
"""

from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.application.audio import AudioMetadataHelper
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.completion import _record_music_download_facts
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.domain.context import Context, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_completed_album_keys, music_subscription_album_matches
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType


def _music_album_tag_info(meta: MetaMusic) -> MusicInfo:
    """
    只使用入库音频标签的专辑与署名证据，不继承整理历史里可能串扰的媒体 ID。
    """
    return MusicInfo(
        music_type=MUSIC_ENTITY_ALBUM,
        title=meta.album,
        album=meta.album,
        artists=meta.artists,
        album_artist=meta.album_artist,
        musicbrainz_release_group_id=meta.musicbrainz_release_group_id,
        musicbrainz_release_id=meta.musicbrainz_release_id,
    )


def _read_music_album_evidence(dest: Optional[str], target: MusicInfo) -> Optional[MetaMusic]:
    """
    读取仍存在的本地入库音频；文件名和目录猜测不足以证明专辑完成。
    """
    if not dest:
        return None
    merged, tags, _ = AudioMetadataHelper.read_evidence(Path(dest))
    if tags is None or not music_subscription_album_matches(target, _music_album_tag_info(tags)):
        return None
    return merged


class SubscribeMusicProgressOwner(_SubscribeOwnerBase):
    """
    从当前订阅下载历史关联的成功入库文件恢复各发行版曲目覆盖。
    """

    def _reconcile_music_album_progress(
        self,
        subscribe: SubscriptionSnapshot,
        target: MusicInfo,
    ) -> SubscriptionSnapshot:
        """
        搜索前补记完整入库事实，普通订阅可完成；洗版仍按本轮音质升级合同推进。
        """
        if subscribe.best_version or target.music_type != MUSIC_ENTITY_ALBUM:
            return subscribe
        if not target.media_source or not target.media_id:
            return subscribe
        histories = self.download_history_repository.get_by_media_identity(
            target.media_source, target.media_id, music_type=MUSIC_ENTITY_ALBUM,
        )
        hashes = [history.download_hash for history in histories if history.download_hash]
        downloads = self._music_album_history_contexts(hashes, target)
        return _record_music_download_facts(
            self._SubscribeChain__apply_subscribe_update,
            self._SubscribeChain__update_subscribe_note,
            subscribe,
            downloads,
            target,
        )

    def _music_album_history_contexts(self, download_hashes: list[str], target: MusicInfo) -> list[Context]:
        """
        同发行版可跨资源累计，禁止不同发行版或专辑拼凑出完整覆盖。
        """
        groups: dict[str, list[MetaMusic]] = defaultdict(list)
        for download_hash in download_hashes:
            for meta in self._music_album_history_metas(download_hash, target):
                groups[meta.musicbrainz_release_id or "tags"].append(meta)
        return [
            Context(media_info=target, music_track_keys=keys, confirmed_full_coverage=True)
            for metas in groups.values()
            if (keys := music_completed_album_keys(metas, target))
        ]

    def _music_album_history_metas(self, download_hash: str, target: MusicInfo) -> list[MetaMusic]:
        """
        仅采纳成功本地整理且仍可读取标签的文件，不采纳历史行上的远端曲目总数。
        """
        metas: list[MetaMusic] = []
        seen: set[str] = set()
        for history in self.transfer_history_repository.list_by_hash(download_hash):
            if (not history.status or history.type != MediaType.MUSIC.value
                    or history.dest_storage != "local" or not history.dest or history.dest in seen):
                continue
            seen.add(history.dest)
            meta = _read_music_album_evidence(history.dest, target)
            if meta is not None:
                metas.append(meta)
        return metas
