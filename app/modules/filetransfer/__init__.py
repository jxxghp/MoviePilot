import re
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple, Union

from jinja2 import Template

from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.core.config import settings
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.schemas import TransferInfo
from app.utils.system import SystemUtils
from app.schemas.types import MediaType

lock = Lock()


class FileTransferModule(_ModuleBase):

    def init_module(self) -> None:
        pass

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def transfer(self, path: Path, mediainfo: MediaInfo, transfer_type: str) -> Optional[TransferInfo]:
        """
        文件转移
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移方式
        :return: {path, target_path, message}
        """
        # 获取目标路径
        target_path = self.get_target_path(in_path=path)
        if not target_path:
            logger.error("未找到媒体库目录，无法转移文件")
            return TransferInfo(message="未找到媒体库目录，无法转移文件")
        # 转移
        result = self.transfer_media(in_path=path,
                                     mediainfo=mediainfo,
                                     transfer_type=transfer_type,
                                     target_dir=target_path)
        if not result:
            return TransferInfo()
        if isinstance(result, str):
            return TransferInfo(message=result)
        # 解包结果
        is_bluray, target_path, file_list, file_size, fail_list, msg = result
        # 返回
        return TransferInfo(path=path,
                            target_path=target_path,
                            message=msg,
                            file_count=len(file_list),
                            total_size=file_size,
                            fail_list=fail_list,
                            is_bluray=is_bluray,
                            file_list=file_list)

    @staticmethod
    def __transfer_command(file_item: Path, target_file: Path, transfer_type: str) -> int:
        """
        使用系统命令处理单个文件
        :param file_item: 文件路径
        :param target_file: 目标文件路径
        :param transfer_type: RmtMode转移方式
        """
        with lock:

            # 转移
            if transfer_type == 'link':
                # 硬链接
                retcode, retmsg = SystemUtils.link(file_item, target_file)
            elif transfer_type == 'softlink':
                # 软链接
                retcode, retmsg = SystemUtils.softlink(file_item, target_file)
            elif transfer_type == 'move':
                # 移动
                retcode, retmsg = SystemUtils.move(file_item, target_file)
            else:
                # 复制
                retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode

    def __transfer_other_files(self, org_path: Path, new_path: Path,
                               transfer_type: str, over_flag: bool) -> int:
        """
        根据文件名转移其他相关文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param transfer_type: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        retcode = self.__transfer_subtitles(org_path, new_path, transfer_type)
        if retcode != 0:
            return retcode
        retcode = self.__transfer_audio_track_files(org_path, new_path, transfer_type, over_flag)
        if retcode != 0:
            return retcode
        return 0

    def __transfer_subtitles(self, org_path: Path, new_path: Path, transfer_type: str) -> int:
        """
        根据文件名转移对应字幕文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param transfer_type: RmtMode转移方式
        """
        # 字幕正则式
        _zhcn_sub_re = r"([.\[(](((zh[-_])?(cn|ch[si]|sg|sc))|zho?" \
                       r"|chinese|(cn|ch[si]|sg|zho?|eng)[-_&](cn|ch[si]|sg|zho?|eng)" \
                       r"|简[体中]?)[.\])])" \
                       r"|([\u4e00-\u9fa5]{0,3}[中双][\u4e00-\u9fa5]{0,2}[字文语][\u4e00-\u9fa5]{0,3})" \
                       r"|简体|简中" \
                       r"|(?<![a-z0-9])gb(?![a-z0-9])"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))" \
                       r"|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体" \
                       r"|(?<![a-z0-9])big5(?![a-z0-9])"
        _eng_sub_re = r"[.\[(]eng[.\])]"

        # 比对文件名并转移字幕
        org_dir: Path = org_path.parent
        file_list: List[Path] = SystemUtils.list_files_with_extensions(org_dir, settings.RMT_SUBEXT)
        if len(file_list) == 0:
            logger.debug(f"{org_dir} 目录下没有找到字幕文件...")
        else:
            logger.debug("字幕文件清单：" + str(file_list))
            # 识别文件名
            metainfo = MetaInfo(title=org_path.name)
            for file_item in file_list:
                # 识别字幕文件名
                sub_file_name = re.sub(_zhtw_sub_re,
                                       ".",
                                       re.sub(_zhcn_sub_re,
                                              ".",
                                              file_item.name,
                                              flags=re.I),
                                       flags=re.I)
                sub_file_name = re.sub(_eng_sub_re, ".", sub_file_name, flags=re.I)
                sub_metainfo = MetaInfo(title=file_item.name)
                # 匹配字幕文件名
                if (org_path.stem == Path(sub_file_name).stem) or \
                        (sub_metainfo.cn_name and sub_metainfo.cn_name == metainfo.cn_name) \
                        or (sub_metainfo.en_name and sub_metainfo.en_name == metainfo.en_name):
                    if metainfo.season \
                            and metainfo.season != sub_metainfo.season:
                        continue
                    if metainfo.episode \
                            and metainfo.episode != sub_metainfo.episode:
                        continue
                    new_file_type = ""
                    # 兼容jellyfin字幕识别(多重识别), emby则会识别最后一个后缀
                    if re.search(_zhcn_sub_re, file_item.stem, re.I):
                        new_file_type = ".chi.zh-cn"
                    elif re.search(_zhtw_sub_re, file_item.stem,
                                   re.I):
                        new_file_type = ".zh-tw"
                    elif re.search(_eng_sub_re, file_item.stem, re.I):
                        new_file_type = ".eng"
                    # 通过对比字幕文件大小  尽量转移所有存在的字幕
                    file_ext = file_item.suffix
                    new_sub_tag_dict = {
                        ".eng": ".英文",
                        ".chi.zh-cn": ".简体中文",
                        ".zh-tw": ".繁体中文"
                    }
                    new_sub_tag_list = [
                        new_file_type if t == 0 else "%s%s(%s)" % (new_file_type,
                                                                   new_sub_tag_dict.get(
                                                                       new_file_type, ""
                                                                   ),
                                                                   t) for t in range(6)
                    ]
                    for new_sub_tag in new_sub_tag_list:
                        new_file: Path = new_path.with_name(new_path.stem + new_sub_tag + file_ext)
                        # 如果字幕文件不存在, 直接转移字幕, 并跳出循环
                        try:
                            if not new_file.exists():
                                logger.debug(f"正在处理字幕：{file_item.name}")
                                retcode = self.__transfer_command(file_item=file_item,
                                                                  target_file=new_file,
                                                                  transfer_type=transfer_type)
                                if retcode == 0:
                                    logger.info(f"字幕 {file_item.name} {transfer_type}完成")
                                    break
                                else:
                                    logger.error(f"字幕 {file_item.name} {transfer_type}失败，错误码 {retcode}")
                                    return retcode
                            # 如果字幕文件的大小与已存在文件相同, 说明已经转移过了, 则跳出循环
                            elif new_file.stat().st_size == file_item.stat().st_size:
                                logger.info(f"字幕 new_file 已存在")
                                break
                            # 否则 循环继续 > 通过new_sub_tag_list 获取新的tag附加到字幕文件名, 继续检查是否能转移
                        except OSError as reason:
                            logger.info(f"字幕 {new_file} 出错了,原因: {reason}")
        return 0

    def __transfer_audio_track_files(self, org_path: Path, new_path: Path,
                                     transfer_type: str, over_flag: bool) -> int:
        """
        根据文件名转移对应音轨文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param transfer_type: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        dir_name = org_path.parent
        file_name = org_path.name
        file_list: List[Path] = SystemUtils.list_files_with_extensions(dir_name, ['.mka'])
        pending_file_list: List[Path] = [file for file in file_list if org_path.stem == file.stem]
        if len(pending_file_list) == 0:
            logger.debug(f"{dir_name} 目录下没有找到匹配的音轨文件")
        else:
            logger.debug("音轨文件清单：" + str(pending_file_list))
            for track_file in pending_file_list:
                track_ext = track_file.suffix
                new_track_file = new_path.with_name(new_path.stem + track_ext)
                if new_track_file.exists():
                    if not over_flag:
                        logger.warn(f"音轨文件已存在：{new_track_file}")
                        continue
                    else:
                        logger.info(f"正在删除已存在的音轨文件：{new_track_file}")
                        new_track_file.unlink()
                try:
                    logger.info(f"正在转移音轨文件：{track_file} 到 {new_track_file}")
                    retcode = self.__transfer_command(file_item=track_file,
                                                      target_file=new_track_file,
                                                      transfer_type=transfer_type)
                    if retcode == 0:
                        logger.info(f"音轨文件 {file_name} {transfer_type}完成")
                    else:
                        logger.error(f"音轨文件 {file_name} {transfer_type}失败，错误码：{retcode}")
                except OSError as reason:
                    logger.error(f"音轨文件 {file_name} {transfer_type}失败：{reason}")
        return 0

    def __transfer_bluray_dir(self, file_path: Path, new_path: Path, transfer_type: str) -> int:
        """
        转移蓝光文件夹
        :param file_path: 原路径
        :param new_path: 新路径
        :param transfer_type: RmtMode转移方式
        """
        logger.info(f"正在{transfer_type}目录：{file_path} 到 {new_path}")
        # 复制
        retcode = self.__transfer_dir_files(src_dir=file_path,
                                            target_dir=new_path,
                                            transfer_type=transfer_type)
        if retcode == 0:
            logger.info(f"文件 {file_path} {transfer_type}完成")
        else:
            logger.error(f"文件{file_path} {transfer_type}失败，错误码：{retcode}")

        return retcode

    def __transfer_dir_files(self, src_dir: Path, target_dir: Path, transfer_type: str) -> int:
        """
        按目录结构转移所有文件
        :param src_dir: 原路径
        :param target_dir: 新路径
        :param transfer_type: RmtMode转移方式
        """
        retcode = 0
        for file in src_dir.glob("**/*"):
            new_file = target_dir.with_name(src_dir.name)
            if new_file.exists():
                logger.warn(f"{new_file} 文件已存在")
                continue
            if not new_file.parent.exists():
                new_file.parent.mkdir(parents=True, exist_ok=True)
            retcode = self.__transfer_command(file_item=file,
                                              target_file=new_file,
                                              transfer_type=transfer_type)
            if retcode != 0:
                break

        return retcode

    def __transfer_file(self, file_item: Path, new_file: Path, transfer_type: str,
                        over_flag: bool = False, old_file: Path = None) -> int:
        """
        转移一个文件，同时处理其他相关文件
        :param file_item: 原文件路径
        :param new_file: 新文件路径
        :param transfer_type: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        if not over_flag and new_file.exists():
            logger.warn(f"文件已存在：{new_file}")
            return 0
        if over_flag and old_file and old_file.exists():
            logger.info(f"正在删除已存在的文件：{old_file}")
            old_file.unlink()
        logger.info(f"正在转移文件：{file_item} 到 {new_file}")
        # 创建父目录
        new_file.parent.mkdir(parents=True, exist_ok=True)
        retcode = self.__transfer_command(file_item=file_item,
                                          target_file=new_file,
                                          transfer_type=transfer_type)
        if retcode == 0:
            logger.info(f"文件 {file_item} {transfer_type}完成")
        else:
            logger.error(f"文件 {file_item} {transfer_type}失败，错误码：{retcode}")
            return retcode
        # 处理其他相关文件
        return self.__transfer_other_files(org_path=file_item,
                                           new_path=new_file,
                                           transfer_type=transfer_type,
                                           over_flag=over_flag)

    @staticmethod
    def __is_bluray_dir(dir_path: Path) -> bool:
        """
        判断是否为蓝光原盘目录
        """
        # 蓝光原盘目录必备的文件或文件夹
        required_files = ['BDMV', 'CERTIFICATE']
        # 检查目录下是否存在所需文件或文件夹
        for item in required_files:
            if (dir_path / item).exists():
                return True
        return False

    def transfer_media(self,
                       in_path: Path,
                       mediainfo: MediaInfo,
                       transfer_type: str,
                       target_dir: Path = None
                       ) -> Union[str, Tuple[bool, Path, list, int, List[Path], str]]:
        """
        识别并转移一个文件、多个文件或者目录
        :param in_path: 转移的路径，可能是一个文件也可以是一个目录
        :param target_dir: 目的文件夹，非空的转移到该文件夹，为空时则按类型转移到配置文件中的媒体库文件夹
        :param transfer_type: 文件转移方式
        :param mediainfo: 媒体信息
        :return: 是否蓝光原盘、目的路径、处理文件清单、总大小、失败文件列表、错误信息
        """
        # 检查目录路径
        if not in_path.exists():
            return f"{in_path} 路径不存在"

        if not target_dir.exists():
            return f"{target_dir} 目标路径不存在"

        if mediainfo.type == MediaType.MOVIE:
            if settings.LIBRARY_MOVIE_NAME:
                target_dir = target_dir / settings.LIBRARY_MOVIE_NAME / mediainfo.category
            else:
                # 目的目录加上类型和二级分类
                target_dir = target_dir / mediainfo.type.value / mediainfo.category

        if mediainfo.type == MediaType.TV:
            if settings.LIBRARY_TV_NAME:
                target_dir = target_dir / settings.LIBRARY_TV_NAME / mediainfo.category
            else:
                # 目的目录加上类型和二级分类
                target_dir = target_dir / mediainfo.type.value / mediainfo.category

        # 重命名格式
        rename_format = settings.TV_RENAME_FORMAT \
            if mediainfo.type == MediaType.TV else settings.MOVIE_RENAME_FORMAT

        # 总大小
        total_filesize = 0

        # 处理文件清单
        file_list = []

        # 失败文件清单
        fail_list = []

        # 错误信息
        err_msgs = []

        # 判断是否为蓝光原盘
        bluray_flag = self.__is_bluray_dir(in_path)
        if bluray_flag:
            # 识别目录名称，不包括后缀
            meta = MetaInfo(in_path.stem)
            # 目的路径
            new_path = self.get_rename_path(
                path=target_dir,
                template_string=rename_format,
                rename_dict=self.__get_naming_dict(meta=meta,
                                                   mediainfo=mediainfo)
            ).parent
            # 转移蓝光原盘
            retcode = self.__transfer_bluray_dir(file_path=in_path,
                                                 new_path=new_path,
                                                 transfer_type=transfer_type)
            if retcode != 0:
                return f"{retcode}，蓝光原盘转移失败"
            else:
                # 计算大小
                total_filesize += in_path.stat().st_size
                # 返回转移后的路径
                return bluray_flag, new_path, [], total_filesize, [], ""
        else:
            # 获取文件清单
            transfer_files: List[Path] = SystemUtils.list_files_with_extensions(in_path, settings.RMT_MEDIAEXT)
            if len(transfer_files) == 0:
                return f"{in_path} 目录下没有找到可转移的文件"
            # 识别目录名称，不包括后缀
            meta = MetaInfo(in_path.stem)
            # 目的路径
            new_path = target_dir / self.get_rename_path(
                template_string=rename_format,
                rename_dict=self.__get_naming_dict(meta=meta,
                                                   mediainfo=mediainfo)
            ).parents[-2].name
            # 转移所有文件
            for transfer_file in transfer_files:
                try:
                    # 识别文件元数据，不包含后缀
                    file_meta = MetaInfo(transfer_file.stem)
                    # 开始季
                    if not file_meta.begin_season:
                        file_meta.begin_season = meta.begin_season
                    # 结束季为空
                    file_meta.end_season = None
                    # 总季数
                    if file_meta.begin_season:
                        file_meta.total_seasons = 1
                    # 开始集
                    if not file_meta.begin_episode:
                        file_meta.begin_episode = meta.begin_episode
                    # 结束集
                    if not file_meta.end_episode:
                        file_meta.end_episode = meta.end_episode
                    # 总集数
                    if not file_meta.total_episode:
                        file_meta.total_episode = meta.total_episode
                    # 版本
                    if not file_meta.resource_type:
                        file_meta.resource_type = meta.resource_type
                    # 分辨率
                    if not file_meta.resource_pix:
                        file_meta.resource_pix = meta.resource_pix
                    # 制作组/字幕组
                    if not file_meta.resource_team:
                        file_meta.resource_team = meta.resource_team
                    # 特效
                    if not file_meta.resource_effect:
                        file_meta.resource_effect = meta.resource_effect
                    # 视频编码
                    if not file_meta.video_encode:
                        file_meta.video_encode = meta.video_encode
                    # 音频编码
                    if not file_meta.audio_encode:
                        file_meta.audio_encode = meta.audio_encode
                    # Part
                    if not file_meta.part:
                        file_meta.part = meta.part
                    # 目的文件名
                    new_file = self.get_rename_path(
                        path=target_dir,
                        template_string=rename_format,
                        rename_dict=self.__get_naming_dict(meta=file_meta,
                                                           mediainfo=mediainfo,
                                                           file_ext=transfer_file.suffix)
                    )
                    # 判断是否要覆盖
                    overflag = False
                    if new_file.exists():
                        if new_file.stat().st_size < transfer_file.stat().st_size:
                            logger.info(f"目标文件已存在，但文件大小更小，将覆盖：{new_file}")
                            overflag = True
                    # 转移文件
                    retcode = self.__transfer_file(file_item=transfer_file,
                                                   new_file=new_file,
                                                   transfer_type=transfer_type,
                                                   over_flag=overflag)
                    if retcode != 0:
                        logger.error(f"{transfer_file} 转移文件失败，错误码：{retcode}")
                        err_msgs.append(f"{transfer_file.name}：错误码 {retcode}")
                        fail_list.append(transfer_file)
                        continue
                    # 计算文件数
                    file_list.append(str(transfer_file))
                    # 计算大小
                    total_filesize += transfer_file.stat().st_size
                except Exception as err:
                    err_msgs.append(f"{transfer_file.name}：{err}")
                    logger.error(f"{transfer_file}转移失败：{err}")
                    fail_list.append(transfer_file)

            if not file_list:
                # 没有成功的
                return "\n".join(err_msgs)

            # 蓝光原盘、新路径、处理文件清单、总大小、失败文件列表、错误信息
            return bluray_flag, new_path, file_list, total_filesize, fail_list, "\n".join(err_msgs)

    @staticmethod
    def __get_naming_dict(meta: MetaBase, mediainfo: MediaInfo, file_ext: str = None) -> dict:
        """
        根据媒体信息，返回Format字典
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        """
        return {
            # 标题
            "title": mediainfo.title,
            # 原文件名
            "original_name": meta.org_string,
            # 原语种标题
            "original_title": mediainfo.original_title,
            # 识别名称
            "name": meta.name,
            # 年份
            "year": mediainfo.year,
            # 版本
            "edition": meta.edition,
            # 分辨率
            "videoFormat": meta.resource_pix,
            # 制作组/字幕组
            "releaseGroup": meta.resource_team,
            # 特效
            "effect": meta.resource_effect,
            # 视频编码
            "videoCodec": meta.video_encode,
            # 音频编码
            "audioCodec": meta.audio_encode,
            # TMDBID
            "tmdbid": mediainfo.tmdb_id,
            # IMDBID
            "imdbid": mediainfo.imdb_id,
            # 季号
            "season": meta.season_seq,
            # 集号
            "episode": meta.episode_seqs,
            # 季集 SxxExx
            "season_episode": "%s%s" % (meta.season, meta.episodes),
            # 段/节
            "part": meta.part,
            # 文件后缀
            "fileExt": file_ext
        }

    @staticmethod
    def get_rename_path(template_string: str, rename_dict: dict, path: Path = None) -> Path:
        """
        生成重命名后的完整路径
        """
        # 创建jinja2模板对象
        template = Template(template_string)
        # 渲染生成的字符串
        render_str = template.render(rename_dict)
        # 目的路径
        if path:
            return path / render_str
        else:
            return Path(render_str)

    @staticmethod
    def get_target_path(in_path: Path = None) -> Optional[Path]:
        """
        计算一个最好的目的目录，有in_path时找与in_path同路径的，没有in_path时，顺序查找1个符合大小要求的，没有in_path和size时，返回第1个
        :param in_path: 源目录
        """
        if not settings.LIBRARY_PATH:
            return None
        # 目的路径，多路径以,分隔
        dest_paths = str(settings.LIBRARY_PATH).split(",")
        # 只有一个路径，直接返回
        if len(dest_paths) == 1:
            return Path(dest_paths[0])
        # 匹配有最长共同上级路径的目录
        max_length = 0
        target_path = None
        if in_path:
            for path in dest_paths:
                relative = Path(path).relative_to(in_path).as_posix()
                if relative.startswith("..") or len(relative) > max_length:
                    max_length = len(relative)
                    target_path = path
            if target_path:
                return Path(target_path)
        # 顺序匹配第1个满足空间存储要求的目录
        if in_path.exists():
            file_size = in_path.stat().st_size
            for path in dest_paths:
                if SystemUtils.free_space(Path(path)) > file_size:
                    return Path(path)
        # 默认返回第1个
        return Path(dest_paths[0])
