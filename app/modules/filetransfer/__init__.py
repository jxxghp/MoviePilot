import re
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple, Union, Dict

from jinja2 import Template

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.modules import _ModuleBase
from app.schemas import TransferInfo, ExistMediaInfo, TmdbEpisode, MediaDirectory
from app.schemas.types import MediaType
from app.utils.system import SystemUtils

lock = Lock()


class FileTransferModule(_ModuleBase):
    """
    文件整理模块
    """

    def __init__(self):
        super().__init__()
        self.directoryhelper = DirectoryHelper()
        self.messagehelper = MessageHelper()

    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
        return "文件整理"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        directoryhelper = DirectoryHelper()
        # 检查下载目录
        download_paths = directoryhelper.get_download_dirs()
        if not download_paths:
            return False, "下载目录未设置"
        for d_path in download_paths:
            path = d_path.path
            if not path:
                return False, f"下载目录 {d_path.name} 对应路径未设置"
            download_path = Path(path)
            if not download_path.exists():
                return False, f"下载目录 {d_path.name} 对应路径 {path} 不存在"
        # 检查媒体库目录
        libaray_paths = directoryhelper.get_library_dirs()
        if not libaray_paths:
            return False, "媒体库目录未设置"
        for l_path in libaray_paths:
            path = l_path.path
            if not path:
                return False, f"媒体库目录 {l_path.name} 对应路径未设置"
            library_path = Path(path)
            if not library_path.exists():
                return False, f"媒体库目录{l_path.name} 对应的路径 {path} 不存在"
        # 检查硬链接条件
        if settings.DOWNLOADER_MONITOR and settings.TRANSFER_TYPE == "link":
            for d_path in download_paths:
                link_ok = False
                for l_path in libaray_paths:
                    if SystemUtils.is_same_disk(Path(d_path.path), Path(l_path.path)):
                        link_ok = True
                        break
                if not link_ok:
                    return False, f"媒体库目录中未找到" \
                                  f"与下载目录 {d_path.path} 在同一磁盘/存储空间/映射路径的目录，将无法硬链接"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def recommend_name(self, meta: MetaBase, mediainfo: MediaInfo) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        # 重命名格式
        rename_format = settings.TV_RENAME_FORMAT \
            if mediainfo.type == MediaType.TV else settings.MOVIE_RENAME_FORMAT
        # 获取重命名后的名称
        path = self.get_rename_path(
            template_string=rename_format,
            rename_dict=self.__get_naming_dict(meta=meta,
                                               mediainfo=mediainfo,
                                               file_ext=Path(meta.title).suffix)
        )
        return str(path)

    def transfer(self, path: Path, meta: MetaBase, mediainfo: MediaInfo,
                 transfer_type: str, target: Path = None,
                 episodes_info: List[TmdbEpisode] = None,
                 scrape: bool = None) -> TransferInfo:
        """
        文件转移
        :param path:  文件路径
        :param meta: 预识别的元数据，仅单文件转移时传递
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移方式
        :param target:  目标路径
        :param episodes_info: 当前季的全部集信息
        :param scrape: 是否刮削元数据
        :return: {path, target_path, message}
        """
        # 目标路径不能是文件
        if target and target.is_file():
            logger.error(f"转移目标路径是一个文件 {target} 是一个文件")
            return TransferInfo(success=False,
                                path=path,
                                message=f"{target} 不是有效目录")
        # 获取目标路径
        directoryhelper = DirectoryHelper()
        if target:
            dir_info = directoryhelper.get_library_dir(mediainfo, in_path=path, to_path=target)
        else:
            dir_info = directoryhelper.get_library_dir(mediainfo, in_path=path)
        if dir_info:
            # 是否需要刮削
            if scrape is None:
                need_scrape = dir_info.scrape
            else:
                need_scrape = scrape
            # 拼装媒体库一、二级子目录
            target = self.__get_dest_dir(mediainfo=mediainfo, target_dir=dir_info)
        elif target:
            # 自定义目标路径
            need_scrape = scrape or False
        else:
            # 未找到有效的媒体库目录
            logger.error(
                f"{mediainfo.type.value} {mediainfo.title_year} 未找到有效的媒体库目录，无法转移文件，源路径：{path}")
            return TransferInfo(success=False,
                                path=path,
                                message="未找到有效的媒体库目录")

        logger.info(f"获取转移目标路径：{target}")
        # 转移
        return self.transfer_media(in_path=path,
                                   in_meta=meta,
                                   mediainfo=mediainfo,
                                   transfer_type=transfer_type,
                                   target_dir=target,
                                   episodes_info=episodes_info,
                                   need_scrape=need_scrape)

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
            elif transfer_type == 'rclone_move':
                # Rclone 移动
                retcode, retmsg = SystemUtils.rclone_move(file_item, target_file)
            elif transfer_type == 'rclone_copy':
                # Rclone 复制
                retcode, retmsg = SystemUtils.rclone_copy(file_item, target_file)
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
                       r"|简体|简中|JPSC" \
                       r"|(?<![a-z0-9])gb(?![a-z0-9])"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))" \
                       r"|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|JPTC" \
                       r"|(?<![a-z0-9])big5(?![a-z0-9])"
        _eng_sub_re = r"[.\[(]eng[.\])]"

        # 比对文件名并转移字幕
        org_dir: Path = org_path.parent
        file_list: List[Path] = SystemUtils.list_files(org_dir, settings.RMT_SUBEXT)
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
                    if metainfo.part and metainfo.part != sub_metainfo.part:
                        continue
                    if metainfo.season \
                            and metainfo.season != sub_metainfo.season:
                        continue
                    if metainfo.episode \
                            and metainfo.episode != sub_metainfo.episode:
                        continue
                    new_file_type = ""
                    # 兼容jellyfin字幕识别(多重识别), emby则会识别最后一个后缀
                    if re.search(_zhcn_sub_re, file_item.name, re.I):
                        new_file_type = ".chi.zh-cn"
                    elif re.search(_zhtw_sub_re, file_item.name,
                                   re.I):
                        new_file_type = ".zh-tw"
                    elif re.search(_eng_sub_re, file_item.name, re.I):
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
        file_list: List[Path] = SystemUtils.list_files(dir_name, ['.mka'])
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

    def __transfer_dir(self, file_path: Path, new_path: Path, transfer_type: str) -> int:
        """
        转移整个文件夹
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
        按目录结构转移目录下所有文件
        :param src_dir: 原路径
        :param target_dir: 新路径
        :param transfer_type: RmtMode转移方式
        """
        retcode = 0
        for file in src_dir.glob("**/*"):
            # 过滤掉目录
            if file.is_dir():
                continue
            # 使用target_dir的父目录作为新的父目录
            new_file = target_dir.joinpath(file.relative_to(src_dir))
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
                        over_flag: bool = False) -> int:
        """
        转移一个文件，同时处理其他相关文件
        :param file_item: 原文件路径
        :param new_file: 新文件路径
        :param transfer_type: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        if new_file.exists() or new_file.is_symlink():
            if not over_flag:
                logger.warn(f"文件已存在：{new_file}")
                return 0
            else:
                logger.info(f"正在删除已存在的文件：{new_file}")
                new_file.unlink()
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
    def __get_dest_dir(mediainfo: MediaInfo, target_dir: MediaDirectory) -> Path:
        """
        根据设置并装媒体库目录
        :param mediainfo: 媒体信息
        :target_dir: 媒体库根目录
        :typename_dir: 是否加上类型目录
        """
        if not target_dir.media_type and target_dir.auto_category:
            # 一级自动分类
            download_dir = Path(target_dir.path) / mediainfo.type.value
        else:
            download_dir = Path(target_dir.path)

        if not target_dir.category and target_dir.auto_category and mediainfo.category:
            # 二级自动分类
            download_dir = download_dir / mediainfo.category

        return download_dir

    def transfer_media(self,
                       in_path: Path,
                       in_meta: MetaBase,
                       mediainfo: MediaInfo,
                       transfer_type: str,
                       target_dir: Path,
                       episodes_info: List[TmdbEpisode] = None,
                       need_scrape: bool = False
                       ) -> TransferInfo:
        """
        识别并转移一个文件或者一个目录下的所有文件
        :param in_path: 转移的路径，可能是一个文件也可以是一个目录
        :param in_meta：预识别元数据
        :param mediainfo: 媒体信息
        :param target_dir: 媒体库根目录
        :param transfer_type: 文件转移方式
        :param episodes_info: 当前季的全部集信息
        :param need_scrape: 是否需要刮削
        :return: TransferInfo、错误信息
        """
        # 检查目录路径
        if not in_path.exists():
            return TransferInfo(success=False,
                                path=in_path,
                                message=f"{in_path} 路径不存在")

        if transfer_type not in ['rclone_copy', 'rclone_move']:
            # 检查目标路径
            if not target_dir.exists():
                logger.info(f"目标路径不存在，正在创建：{target_dir} ...")
                target_dir.mkdir(parents=True, exist_ok=True)

        # 重命名格式
        rename_format = settings.TV_RENAME_FORMAT \
            if mediainfo.type == MediaType.TV else settings.MOVIE_RENAME_FORMAT

        # 判断是否为文件夹
        if in_path.is_dir():
            # 转移整个目录
            # 是否蓝光原盘
            bluray_flag = SystemUtils.is_bluray_dir(in_path)
            if bluray_flag:
                logger.info(f"{in_path} 是蓝光原盘文件夹")
            # 原文件大小
            file_size = in_path.stat().st_size
            # 目的路径
            new_path = self.get_rename_path(
                path=target_dir,
                template_string=rename_format,
                rename_dict=self.__get_naming_dict(meta=in_meta,
                                                   mediainfo=mediainfo)
            ).parent
            # 转移蓝光原盘
            retcode = self.__transfer_dir(file_path=in_path,
                                          new_path=new_path,
                                          transfer_type=transfer_type)
            if retcode != 0:
                logger.error(f"文件夹 {in_path} 转移失败，错误码：{retcode}")
                return TransferInfo(success=False,
                                    message=f"错误码：{retcode}",
                                    path=in_path,
                                    target_path=new_path,
                                    is_bluray=bluray_flag)

            logger.info(f"文件夹 {in_path} 转移成功")
            # 返回转移后的路径
            return TransferInfo(success=True,
                                path=in_path,
                                target_path=new_path,
                                total_size=file_size,
                                is_bluray=bluray_flag,
                                need_scrape=need_scrape)
        else:
            # 转移单个文件
            if mediainfo.type == MediaType.TV:
                # 电视剧
                if in_meta.begin_episode is None:
                    logger.warn(f"文件 {in_path} 转移失败：未识别到文件集数")
                    return TransferInfo(success=False,
                                        message=f"未识别到文件集数",
                                        path=in_path,
                                        fail_list=[str(in_path)])

                # 文件结束季为空
                in_meta.end_season = None
                # 文件总季数为1
                if in_meta.total_season:
                    in_meta.total_season = 1
                # 文件不可能超过2集
                if in_meta.total_episode > 2:
                    in_meta.total_episode = 1
                    in_meta.end_episode = None

            # 目的文件名
            new_file = self.get_rename_path(
                path=target_dir,
                template_string=rename_format,
                rename_dict=self.__get_naming_dict(
                    meta=in_meta,
                    mediainfo=mediainfo,
                    episodes_info=episodes_info,
                    file_ext=in_path.suffix
                )
            )

            # 判断是否要覆盖
            overflag = False
            target_file = new_file
            if new_file.exists() or new_file.is_symlink():
                if new_file.is_symlink():
                    target_file = new_file.readlink()
                    if not target_file.exists():
                        overflag = True
                if not overflag:
                    # 目标文件已存在
                    logger.info(f"目标文件已存在，转移覆盖模式：{settings.OVERWRITE_MODE}")
                    match settings.OVERWRITE_MODE:
                        case 'always':
                            # 总是覆盖同名文件
                            overflag = True
                        case 'size':
                            # 存在时大覆盖小
                            if target_file.stat().st_size < in_path.stat().st_size:
                                logger.info(f"目标文件文件大小更小，将覆盖：{new_file}")
                                overflag = True
                            else:
                                return TransferInfo(success=False,
                                                    message=f"媒体库中已存在，且质量更好",
                                                    path=in_path,
                                                    target_path=new_file,
                                                    fail_list=[str(in_path)])
                        case 'never':
                            # 存在不覆盖
                            return TransferInfo(success=False,
                                                message=f"媒体库中已存在，当前设置为不覆盖",
                                                path=in_path,
                                                target_path=new_file,
                                                fail_list=[str(in_path)])
                        case 'latest':
                            # 仅保留最新版本
                            logger.info(f"仅保留最新版本，将覆盖：{new_file}")
                            overflag = True
            else:
                if settings.OVERWRITE_MODE == 'latest':
                    # 文件不存在，但仅保留最新版本
                    logger.info(f"转移覆盖模式：{settings.OVERWRITE_MODE}，仅保留最新版本")
                    self.delete_all_version_files(new_file)
            # 原文件大小
            file_size = in_path.stat().st_size
            # 转移文件
            retcode = self.__transfer_file(file_item=in_path,
                                           new_file=new_file,
                                           transfer_type=transfer_type,
                                           over_flag=overflag)
            if retcode != 0:
                logger.error(f"文件 {in_path} 转移失败，错误码：{retcode}")
                return TransferInfo(success=False,
                                    message=f"错误码：{retcode}",
                                    path=in_path,
                                    target_path=new_file,
                                    fail_list=[str(in_path)])

            logger.info(f"文件 {in_path} 转移成功")
            return TransferInfo(success=True,
                                path=in_path,
                                target_path=new_file,
                                file_count=1,
                                total_size=file_size,
                                is_bluray=False,
                                file_list=[str(in_path)],
                                file_list_new=[str(new_file)],
                                need_scrape=need_scrape)

    @staticmethod
    def __get_naming_dict(meta: MetaBase, mediainfo: MediaInfo, file_ext: str = None,
                          episodes_info: List[TmdbEpisode] = None) -> dict:
        """
        根据媒体信息，返回Format字典
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        """

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

        # 获取集标题
        episode_title = None
        if meta.begin_episode and episodes_info:
            for episode in episodes_info:
                if episode.episode_number == meta.begin_episode:
                    episode_title = episode.name
                    break

        return {
            # 标题
            "title": __convert_invalid_characters(mediainfo.title),
            # 英文标题
            "en_title": __convert_invalid_characters(mediainfo.en_title),
            # 原语种标题
            "original_title": __convert_invalid_characters(mediainfo.original_title),
            # 原文件名
            "original_name": meta.title,
            # 识别名称（优先使用中文）
            "name": meta.name,
            # 识别的英文名称（可能为空）
            "en_name": meta.en_name,
            # 年份
            "year": mediainfo.year or meta.year,
            # 资源类型
            "resourceType": meta.resource_type,
            # 特效
            "effect": meta.resource_effect,
            # 版本
            "edition": meta.edition,
            # 分辨率
            "videoFormat": meta.resource_pix,
            # 制作组/字幕组
            "releaseGroup": meta.resource_team,
            # 视频编码
            "videoCodec": meta.video_encode,
            # 音频编码
            "audioCodec": meta.audio_encode,
            # TMDBID
            "tmdbid": mediainfo.tmdb_id,
            # IMDBID
            "imdbid": mediainfo.imdb_id,
            # 豆瓣ID
            "doubanid": mediainfo.douban_id,
            # 季号
            "season": meta.season_seq,
            # 季年份根据season值获取
            "season_year": mediainfo.season_years.get(
                int(meta.season_seq),
                None) if (mediainfo.season_years and meta.season_seq) else None,
            # 集号
            "episode": meta.episode_seqs,
            # 季集 SxxExx
            "season_episode": "%s%s" % (meta.season, meta.episodes),
            # 段/节
            "part": meta.part,
            # 剧集标题
            "episode_title": __convert_invalid_characters(episode_title),
            # 文件后缀
            "fileExt": file_ext,
            # 自定义占位符
            "customization": meta.customization
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

    def media_exists(self, mediainfo: MediaInfo, **kwargs) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在于本地文件系统，只支持标准媒体库结构
        :param mediainfo:  识别的媒体信息
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        # 目的路径
        dest_paths = DirectoryHelper().get_library_dirs()
        # 检查每一个媒体库目录
        for dest_path in dest_paths:
            # 媒体分类路径
            target_dir = self.__get_dest_dir(mediainfo=mediainfo, target_dir=dest_path)
            if not target_dir.exists():
                continue

            # 重命名格式
            rename_format = settings.TV_RENAME_FORMAT \
                if mediainfo.type == MediaType.TV else settings.MOVIE_RENAME_FORMAT
            # 获取相对路径（重命名路径）
            meta = MetaInfo(mediainfo.title)
            rel_path = self.get_rename_path(
                template_string=rename_format,
                rename_dict=self.__get_naming_dict(meta=meta,
                                                   mediainfo=mediainfo)
            )

            # 取相对路径的第1层目录
            if rel_path.parts:
                media_path = target_dir / rel_path.parts[0]
            else:
                continue

            # 检查媒体文件夹是否存在
            if not media_path.exists():
                continue

            # 检索媒体文件
            media_files = SystemUtils.list_files(directory=media_path, extensions=settings.RMT_MEDIAEXT)
            if not media_files:
                continue

            if mediainfo.type == MediaType.MOVIE:
                # 电影存在任何文件为存在
                logger.info(f"文件系统已存在：{mediainfo.title_year}")
                return ExistMediaInfo(type=MediaType.MOVIE)
            else:
                # 电视剧检索集数
                seasons: Dict[int, list] = {}
                for media_file in media_files:
                    file_meta = MetaInfo(media_file.stem)
                    season_index = file_meta.begin_season or 1
                    episode_index = file_meta.begin_episode
                    if not episode_index:
                        continue
                    if season_index not in seasons:
                        seasons[season_index] = []
                    seasons[season_index].append(episode_index)
                # 返回剧集情况
                logger.info(f"{mediainfo.title_year} 文件系统已存在：{seasons}")
                return ExistMediaInfo(type=MediaType.TV, seasons=seasons)
        # 不存在
        return None

    @staticmethod
    def delete_all_version_files(path: Path) -> bool:
        """
        删除目录下的所有版本文件
        :param path: 目录路径
        """
        # 识别文件中的季集信息
        meta = MetaInfoPath(path)
        season = meta.season
        episode = meta.episode
        # 检索媒体文件
        logger.warn(f"正在删除目标目录中其它版本的文件：{path.parent}")
        media_files = SystemUtils.list_files(directory=path.parent, extensions=settings.RMT_MEDIAEXT)
        if not media_files:
            logger.info(f"目录中没有媒体文件：{path.parent}")
            return False
        # 删除文件
        for media_file in media_files:
            if str(media_file) == str(path):
                continue
            # 识别文件中的季集信息
            filemeta = MetaInfoPath(media_file)
            # 相同季集的文件才删除
            if filemeta.season != season or filemeta.episode != episode:
                continue
            logger.info(f"正在删除文件：{media_file}")
            media_file.unlink()
        return True
