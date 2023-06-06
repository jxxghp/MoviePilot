import re
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple, Union

from app.core import MediaInfo, MetaInfo, settings
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.utils.system import SystemUtils

lock = Lock()


class FileTransferModule(_ModuleBase):

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def transfer(self, path: str, mediainfo: MediaInfo) -> Optional[bool]:
        """
        TODO 文件转移
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        pass

    @staticmethod
    def __transfer_command(file_item: Path, target_file: Path, rmt_mode) -> int:
        """
        使用系统命令处理单个文件
        :param file_item: 文件路径
        :param target_file: 目标文件路径
        :param rmt_mode: RmtMode转移方式
        """
        with lock:
            if rmt_mode == 'link':
                # 硬链接
                retcode, retmsg = SystemUtils.link(file_item, target_file)
            elif rmt_mode == 'softlink':
                # 软链接
                retcode, retmsg = SystemUtils.softlink(file_item, target_file)
            elif rmt_mode == 'move':
                # 移动
                retcode, retmsg = SystemUtils.move(file_item, target_file)
            else:
                # 复制
                retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode

    def __transfer_other_files(self, org_path: Path, new_path: Path, rmt_mode: str, over_flag: bool) -> int:
        """
        根据文件名转移其他相关文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param rmt_mode: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        retcode = self.__transfer_subtitles(org_path, new_path, rmt_mode)
        if retcode != 0:
            return retcode
        retcode = self.__transfer_audio_track_files(org_path, new_path, rmt_mode, over_flag)
        if retcode != 0:
            return retcode
        return 0

    def __transfer_subtitles(self, org_path: Path, new_path: Path, rmt_mode: str) -> int:
        """
        根据文件名转移对应字幕文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param rmt_mode: RmtMode转移方式
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
                    if metainfo.get_season_string() \
                            and metainfo.get_season_string() != sub_metainfo.get_season_string():
                        continue
                    if metainfo.get_episode_string() \
                            and metainfo.get_episode_string() != sub_metainfo.get_episode_string():
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
                                                                  rmt_mode=rmt_mode)
                                if retcode == 0:
                                    logger.info(f"字幕 {file_item.name} {rmt_mode}完成")
                                    break
                                else:
                                    logger.error(f"字幕 {file_item.name} {rmt_mode}失败，错误码 {retcode}")
                                    return retcode
                            # 如果字幕文件的大小与已存在文件相同, 说明已经转移过了, 则跳出循环
                            elif new_file.stat().st_size == file_item.stat().st_size:
                                logger.info(f"字幕 new_file 已存在")
                                break
                            # 否则 循环继续 > 通过new_sub_tag_list 获取新的tag附加到字幕文件名, 继续检查是否能转移
                        except OSError as reason:
                            logger.info(f"字幕 {new_file} 出错了,原因: {reason}")
        return 0

    def __transfer_audio_track_files(self, org_path: Path, new_path: Path, rmt_mode: str, over_flag: bool) -> int:
        """
        根据文件名转移对应音轨文件
        :param org_path: 原文件名
        :param new_path: 新文件名
        :param rmt_mode: RmtMode转移方式
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
                                                      rmt_mode=rmt_mode)
                    if retcode == 0:
                        logger.info(f"音轨文件 {file_name} {rmt_mode}完成")
                    else:
                        logger.error(f"音轨文件 {file_name} {rmt_mode}失败，错误码：{retcode}")
                except OSError as reason:
                    logger.error(f"音轨文件 {file_name} {rmt_mode}失败：{reason}")
        return 0

    def __transfer_bluray_dir(self, file_path: Path, new_path: Path, rmt_mode: str) -> int:
        """
        转移蓝光文件夹
        :param file_path: 原路径
        :param new_path: 新路径
        :param rmt_mode: RmtMode转移方式
        """
        logger.info(f"正在{rmt_mode}目录：{file_path} 到 {new_path}")
        # 复制
        retcode = self.__transfer_dir_files(src_dir=file_path,
                                            target_dir=new_path,
                                            rmt_mode=rmt_mode)
        if retcode == 0:
            logger.info(f"文件 {file_path} {rmt_mode}完成")
        else:
            logger.error(f"文件{file_path} {rmt_mode}失败，错误码：{retcode}")

        return retcode

    def __transfer_dir_files(self, src_dir: Path, target_dir: Path, rmt_mode: str) -> int:
        """
        按目录结构转移所有文件
        :param src_dir: 原路径
        :param target_dir: 新路径
        :param rmt_mode: RmtMode转移方式
        """
        retcode = 0
        for file in src_dir.glob("**/*"):
            new_file = target_dir.with_name(src_dir.name)
            if new_file.exists():
                logger.warn(f"{new_file} 文件已存在")
                continue
            if not new_file.parent.exists():
                new_file.parent.mkdir(parents=True)
            retcode = self.__transfer_command(file_item=file,
                                              target_file=new_file,
                                              rmt_mode=rmt_mode)
            if retcode != 0:
                break

        return retcode

    def __transfer_file(self, file_item: Path, new_file: Path, rmt_mode: str,
                        over_flag: bool = False, old_file: Path = None) -> int:
        """
        转移一个文件，同时处理其他相关文件
        :param file_item: 原文件路径
        :param new_file: 新文件路径
        :param rmt_mode: RmtMode转移方式
        :param over_flag: 是否覆盖，为True时会先删除再转移
        """
        if not over_flag and new_file.exists():
            logger.warn(f"文件已存在：{new_file}")
            return 0
        if over_flag and old_file and old_file.exists():
            logger.info(f"正在删除已存在的文件：{old_file}")
            old_file.unlink()
        logger.info(f"正在转移文件：{file_item.name} 到 {new_file}")
        retcode = self.__transfer_command(file_item=file_item,
                                          target_file=new_file,
                                          rmt_mode=rmt_mode)
        if retcode == 0:
            logger.info(f"文件 {file_item.name} {rmt_mode}完成")
        else:
            logger.error(f"文件 {file_item.name} {rmt_mode}失败，错误码：{retcode}")
            return retcode
        # 处理其他相关文件
        return self.__transfer_other_files(org_path=file_item,
                                           new_path=new_file,
                                           rmt_mode=rmt_mode,
                                           over_flag=over_flag)

    def transfer_media(self,
                       in_path: Path,
                       meidainfo: MediaInfo,
                       rmt_mode: str = None,
                       target_dir: Path = None
                       ) -> Tuple[bool, str]:
        """
        识别并转移一个文件、多个文件或者目录
        :param in_path: 转移的路径，可能是一个文件也可以是一个目录
        :param target_dir: 目的文件夹，非空的转移到该文件夹，为空时则按类型转移到配置文件中的媒体库文件夹
        :param rmt_mode: 文件转移方式
        :param meidainfo: 媒体信息
        :return: 处理状态，错误信息
        """
        pass

    def __get_naming_dict(self, meta: MetaBase, mediainfo: MediaInfo) -> dict:
        """
        根据媒体信息，返回Format字典
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        """
        pass

    def get_movie_dest_path(self, meta: MetaBase, mediainfo: MediaInfo) -> Tuple[str, str]:
        """
        计算电影文件路径
        :return: 电影目录、电影名称
        """
        pass

    def get_tv_dest_path(self, meta: MetaBase, mediainfo: MediaInfo) -> Tuple[str, str, str]:
        """
        计算电视剧文件路径
        :return: 电视剧目录、季目录、集名称
        """
        pass
