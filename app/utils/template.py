import re
from typing import Any, Optional, List, Dict

from jinja2 import Template

from app.core.context import MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.utils.string import StringUtils


class TemplateRenderer:
    """Jinja2模板渲染上下文构建器"""

    def __init__(self):
        self._context = {}

    @staticmethod
    def render_template(template_content: str, context_data: dict) -> str:
        """
        :param template_content: 模板内容
        :param context_data: 上下文数据
        :return: 解析后的字典
        """
        # 创建jinja2模板对象
        template = Template(template_content)
        # 渲染生成的字符串
        rendered_output = template.render(context_data)

        return rendered_output

    @classmethod
    def for_context(
        cls,
        meta: Optional[MetaBase] = None,
        mediainfo: Optional[MediaInfo] = None,
        torrentinfo: Optional[TorrentInfo] = None,
        transferinfo: Optional[TransferInfo] = None,
        file_extension: Optional[str] = None,
        episodes_info: Optional[List[TmdbEpisode]] = None,
        include_raw_objects: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """
        :param meta: 媒体信息
        :param mediainfo: 媒体信息
        :param torrentinfo: 种子信息
        :param transferinfo: 传输信息
        :param file_extension: 文件扩展名
        :param episodes_info: 剧集信息
        :param include_raw_objects: 是否包含原始对象
        :return: 渲染上下文字典
        """
        instance = cls()
        instance._add_episode_details(meta, episodes_info)
        instance._add_media_info(mediainfo)
        instance._add_transfer_info(transferinfo)
        instance._add_torrent_info(torrentinfo)
        instance._add_file_info(file_extension)
        if kwargs: instance._context.update(kwargs)

        if include_raw_objects:
            instance._add_raw_objects(meta, mediainfo, torrentinfo, transferinfo, episodes_info)

        return instance._context

    def _add_media_info(self, mediainfo: MediaInfo):
        """
        增加媒体信息
        """
        if not mediainfo: return
        base_info = {
            # 标题
            "title": self.__convert_invalid_characters(mediainfo.title),
            # 英文标题
            "en_title": self.__convert_invalid_characters(mediainfo.en_title),
            # 原语种标题
            "original_title": self.__convert_invalid_characters(mediainfo.original_title),
            # 年份
            "year": mediainfo.year or self._context.get("year"),
        }

        _meta_season = self._context.get("season")
        media_info = {
            # 类型
            "type": mediainfo.type.value,
            # 类别
            "category": mediainfo.category,
            # 评分
            "vote_average": mediainfo.vote_average,
            # 海报
            "poster": mediainfo.get_poster_image(),
            # 背景图
            "backdrop": mediainfo.get_backdrop_image(),
            # 季年份根据season值获取
            "season_year": mediainfo.season_years.get(
                int(_meta_season),
                None) if (mediainfo.season_years and _meta_season) else None,
            # TMDBID
            "tmdbid": mediainfo.tmdb_id,
            # IMDBID
            "imdbid": mediainfo.imdb_id,
            # 豆瓣ID
            "doubanid": mediainfo.douban_id,
        }
        self._context.update({**base_info, **media_info})

    def _add_episode_details(self, meta: Optional[MetaBase], episodes: Optional[List[TmdbEpisode]]):
        """添加剧集详细信息"""
        if not meta:
            return

        episode_data = {"episode_title": None, "episode_date": None}
        if meta.begin_episode and episodes:
            for episode in episodes:
                if episode.episode_number == meta.begin_episode:
                    episode_data.update({
                        "episode_title": self.__convert_invalid_characters(episode.name),
                        "episode_date": episode.air_date if episode.air_date else None
                    })
                    break

        meta_info = {
            # 原文件名
            "original_name": meta.title,
            # 识别名称（优先使用中文）
            "name": meta.name,
            # 识别的英文名称（可能为空）
            "en_name": meta.en_name,
            # 年份
            "year": meta.year,
            # 季号
            "season": meta.season_seq,
            # 集号
            "episode": meta.episode_seqs,
            # 季集 SxxExx
            "season_episode": "%s%s" % (meta.season, meta.episode),
            # 段/节
            "part": meta.part,
            # 自定义占位符
            "customization": meta.customization,
            }

        tech_metadata = {
            # 资源类型
            "resourceType": meta.resource_type,
            # 特效
            "effect": meta.resource_effect,
            # 版本
            "edition": meta.edition,
            # 分辨率
            "videoFormat": meta.resource_pix,
            # 质量
            "resource_term": meta.resource_term,
            # 制作组/字幕组
            "releaseGroup": meta.resource_team,
            # 视频编码
            "videoCodec": meta.video_encode,
            # 音频编码
            "audioCodec": meta.audio_encode,
        }
        self._context.update({**meta_info, **tech_metadata, **episode_data})

    def _add_torrent_info(self, torrentinfo: Optional[TorrentInfo]):
        if not torrentinfo: return
        if torrentinfo.size:
            if str(torrentinfo.size).replace(".", "").isdigit():
                size = StringUtils.str_filesize(torrentinfo.size)
            else:
                size = torrentinfo.size

        if torrentinfo.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrentinfo.description)
            torrentinfo.description = re.sub(r'<[^>]+>', '', description)

        torrent_info = {
            # 种子标题
            "torrent_title": torrentinfo.title,
            # 发布时间
            "pubdate": torrentinfo.pubdate,
            # 免费剩余时间
            "freedate": torrentinfo.freedate_diff,
            # 做种数
            "seeders": torrentinfo.seeders,
            # 促销信息
            "volume_factor": torrentinfo.volume_factor,
            # Hit&Run
            "HR": "是" if torrentinfo.hit_and_run else "否",
            # 种子标签
            "labels": {' '.join(torrentinfo.labels)},
            # 描述
            "description": torrentinfo.description,
            # 站点名称
            "site": torrentinfo.site_name,
            # 种子大小
            "size": size,
        }
        self._context.update(torrent_info)

    def _add_transfer_info(self, transferinfo: Optional[TransferInfo]) -> Optional[Dict]:
        """添加文件转移上下文"""
        if not transferinfo: return
        ctx = {
            "transfer_type": transferinfo.transfer_type,
            "file_count": transferinfo.file_count,
            "total_size": StringUtils.str_filesize(transferinfo.total_size),
            "err_msg": transferinfo.message,
        }
        self._context.update(ctx)

    def _add_file_info(self, file_extension: Optional[str]):
        """添加文件信息"""
        if not file_extension: return
        file_info = {
            # 文件后缀
            "fileExt": file_extension,
        }
        self._context.update(file_info)

    def _add_raw_objects(
            self, meta: Optional[MetaBase], 
            mediainfo: Optional[MediaInfo], 
            torrentinfo: Optional[TorrentInfo],
            transferinfo: Optional[TransferInfo], 
            episodes_info: Optional[List[TmdbEpisode]]
        ):
        """添加原始对象引用"""
        raw_objects = {
            # 文件元数据
            "__meta__": meta,
            # 识别的媒体信息
            "__mediainfo__": mediainfo,
            # 种子信息
            "__torrentinfo__": torrentinfo,
            # 文件转移信息
            "__transferinfo__": transferinfo,
            # 当前季的全部集信息
            "__episodes_info__": episodes_info,
        }
        self._context.update({k: v for k, v in raw_objects.items() if v is not None})
    
    @staticmethod
    def __convert_invalid_characters(filename: str):
        if not filename:
            return filename
        invalid_characters = r'\/:*?"<>|'
        # 创建半角到全角字符的转换表
        halfwidth_chars = "".join([chr(i) for i in range(33, 127)])
        fullwidth_chars = "".join([chr(i + 0xFEE0) for i in range(33, 127)])
        translation_table = str.maketrans(halfwidth_chars, fullwidth_chars)
        # 将不支持的字符替换为对应的全角字符
        for char in invalid_characters:
            filename = filename.replace(char, char.translate(translation_table))
        return filename