"""音乐艺人图片获取与旁挂文件写入链。"""

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Optional

from app.chain.base import ChainBase
from app.chain.storage import StorageChain
from app.domain.context import MusicArtistInfo, MusicInfo
from app.runtime.log import logger
from app.schemas.types import MediaSource
from app.schemas.workflow import FileItem

_ArtistImageLoader = Callable[[Optional[str]], tuple[Optional[bytes], str]]


class MusicArtworkChain(ChainBase):
    """按标准艺人身份生成播放器可读的 artist.* 旁挂图片。"""

    ARTIST_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

    def __init__(self) -> None:
        """初始化艺人详情与存储能力。"""
        super().__init__()
        self.storagechain = StorageChain()

    def scrape_artist_images(
            self,
            files: list[FileItem],
            media: list[Optional[MusicInfo]],
            overwrite: bool,
            image_loader: _ArtistImageLoader,
    ) -> dict[str, int]:
        """按音频所在专辑目录去重生成 artist.* 旁挂图片。

        同一目录出现多个主艺人时按合辑处理，不写入可能错配的艺人图片。

        :param files: 已刮削的音频文件
        :param media: 与音频文件对齐的音乐身份
        :param overwrite: 是否覆盖已有艺人图片
        :param image_loader: 经过宿主缓存和代理的图片下载器
        :return: 新增、已存在、未匹配和失败数量
        """
        counts = {"saved": 0, "existing": 0, "missing": 0, "failed": 0}
        directory_artists = self._directory_artists(files, media)
        artist_cache: dict[tuple[MediaSource, str], Optional[MusicArtistInfo]] = {}
        image_cache: dict[str, tuple[Optional[bytes], str]] = {}
        for artists in directory_artists.values():
            if len(artists) != 1:
                continue
            (artist_key, audio_item), = artists.items()
            if self._find_artist_sidecar(audio_item) and not overwrite:
                counts["existing"] += 1
                continue
            if artist_key not in artist_cache:
                artist_cache[artist_key] = self._artist_detail(*artist_key)
            artist_info = artist_cache[artist_key]
            image_url = artist_info.image_url if artist_info else None
            if not image_url:
                counts["missing"] += 1
                continue
            if image_url not in image_cache:
                image_cache[image_url] = image_loader(image_url)
            image_data, image_mime = image_cache[image_url]
            if not image_data:
                counts["failed"] += 1
                continue
            status = self._write_artist_sidecar(
                fileitem=audio_item,
                content=image_data,
                mime=image_mime,
                overwrite=overwrite,
            )
            counts["saved" if status else "failed"] += 1
        return counts

    @staticmethod
    def append_summary(message: str, counts: dict[str, int]) -> str:
        """将艺人图片处理计数追加到音乐刮削结果。"""
        labels = {
            "saved": ("新增", "张"),
            "existing": ("已存在", "张"),
            "missing": ("未匹配", "项"),
            "failed": ("失败", "项"),
        }
        for status, (label, unit) in labels.items():
            if count := counts.get(status, 0):
                message += f"，艺人图片{label} {count} {unit}"
        return message

    @classmethod
    def _directory_artists(
            cls,
            files: list[FileItem],
            media: list[Optional[MusicInfo]],
    ) -> dict[tuple[str, str], dict[tuple[MediaSource, str], FileItem]]:
        """按目录归并主艺人身份，供合辑歧义保护和请求去重使用。"""
        directory_artists: dict[
            tuple[str, str],
            dict[tuple[MediaSource, str], FileItem],
        ] = {}
        for audio_item, item_media in zip(files, media):
            artist = cls._primary_artist(item_media)
            if not artist:
                continue
            directory_key = (
                audio_item.storage or "local",
                Path(audio_item.path).parent.as_posix(),
            )
            directory_artists.setdefault(directory_key, {})[artist] = audio_item
        return directory_artists

    @staticmethod
    def _primary_artist(
            mediainfo: Optional[MusicInfo],
    ) -> Optional[tuple[MediaSource, str]]:
        """返回音乐身份中的第一个标准艺人，避免用名称猜测同名艺人。"""
        if not mediainfo or not mediainfo.media_source:
            return None
        for artist_id in mediainfo.artist_ids or []:
            normalized_id = str(artist_id or "").strip()
            if normalized_id:
                return mediainfo.media_source, normalized_id
        return None

    def _artist_detail(
            self,
            media_source: MediaSource,
            artist_id: str,
    ) -> Optional[MusicArtistInfo]:
        """通过模块合同获取艺人详情，并校验返回的来源身份。"""
        result = self.run_module(
            "music_artist",
            media_source=media_source,
            media_id=artist_id,
        )
        if isinstance(result, MusicArtistInfo):
            artist = result
        elif isinstance(result, dict):
            artist = MusicArtistInfo.from_dict(result)
        else:
            return None
        if artist.media_source != media_source or artist.media_id != artist_id:
            return None
        return artist

    def _find_artist_sidecar(self, fileitem: FileItem) -> Optional[FileItem]:
        """查找音轨所在目录已有的 artist.* 艺人图片。"""
        parent_path = Path(fileitem.path).parent
        for extension in self.ARTIST_IMAGE_EXTENSIONS:
            item = self.storagechain.get_file_item(
                storage=fileitem.storage,
                path=parent_path / f"artist{extension}",
            )
            if item:
                return item
        return None

    @staticmethod
    def _image_extension(mime: str) -> str:
        """将常见图片 MIME 类型映射为旁挂文件扩展名。"""
        return {
            "image/gif": ".gif",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(str(mime or "").casefold(), ".jpg")

    def _write_artist_sidecar(
            self,
            fileitem: FileItem,
            content: bytes,
            mime: str,
            overwrite: bool,
    ) -> bool:
        """原子写入本地艺人图片或上传到远端音轨目录。"""
        extension = self._image_extension(mime)
        target_path = Path(fileitem.path).parent / f"artist{extension}"
        temp_path: Optional[Path] = None
        try:
            if fileitem.storage == "local":
                with NamedTemporaryFile(
                        mode="wb",
                        dir=target_path.parent,
                        prefix=f".{target_path.name}.",
                        delete=False,
                ) as temp_file:
                    temp_file.write(content)
                    temp_path = Path(temp_file.name)
                temp_path.replace(target_path)
            else:
                parent = self.storagechain.get_parent_item(fileitem)
                if not parent:
                    logger.warning(f"无法获取远端艺人图片目录：{fileitem.path}")
                    return False
                with NamedTemporaryFile(
                        mode="wb",
                        suffix=extension,
                        prefix="moviepilot-artist-",
                        delete=False,
                ) as temp_file:
                    temp_file.write(content)
                    temp_path = Path(temp_file.name)
                if not self.storagechain.upload_file(
                        parent,
                        temp_path,
                        new_name=target_path.name,
                ):
                    return False
            if overwrite:
                self._remove_alternate_images(fileitem, keep_extension=extension)
            return True
        except OSError as err:
            logger.warning(f"保存艺人图片失败：{target_path} - {err}")
            return False
        finally:
            if temp_path and temp_path.exists() and temp_path != target_path:
                self._cleanup_temp_file(temp_path)

    def _remove_alternate_images(
            self,
            fileitem: FileItem,
            keep_extension: str,
    ) -> None:
        """覆盖艺人图片后删除其它已知扩展名，避免播放器读取旧图。"""
        parent_path = Path(fileitem.path).parent
        for extension in self.ARTIST_IMAGE_EXTENSIONS:
            if extension == keep_extension:
                continue
            item = self.storagechain.get_file_item(
                storage=fileitem.storage,
                path=parent_path / f"artist{extension}",
            )
            if item and not self.storagechain.delete_file(item):
                logger.warning(f"删除旧艺人图片失败：{item.path}")

    @staticmethod
    def _cleanup_temp_file(path: Path) -> None:
        """删除远端上传后的本地临时图片。"""
        try:
            path.unlink(missing_ok=True)
        except OSError as err:
            logger.warning(f"清理临时艺人图片失败：{path} - {err}")
