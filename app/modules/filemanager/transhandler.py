import re
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple

from jinja2 import Template

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.helper.message import TemplateHelper
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas import TransferInfo, TmdbEpisode, TransferDirectoryConf, FileItem, TransferInterceptEventData
from app.schemas.types import MediaType, ChainEventType
from app.utils.system import SystemUtils
from app.schemas import TransferRenameEventData

lock = Lock()


class TransHandler:
    """
    文件转移整理类
    """

    result: Optional[TransferInfo] = None
    inner_lock: Lock = Lock()

    def __init__(self):
        self.__reset_result()

    def __reset_result(self):
        """
        重置结果
        """
        self.result = TransferInfo()

    def __set_result(self, **kwargs):
        """
        设置结果
        """
        with self.inner_lock:
            # 设置值
            for key, value in kwargs.items():
                if hasattr(self.result, key):
                    current_value = getattr(self.result, key)
                    if current_value is None:
                        current_value = value
                    elif isinstance(current_value, list):
                        if isinstance(value, list):
                            current_value.extend(value)
                        else:
                            current_value.append(value)
                    elif isinstance(current_value, dict):
                        if isinstance(value, dict):
                            current_value.update(value)
                        else:
                            current_value[key] = value
                    elif isinstance(current_value, int):
                        current_value += value
                    else:
                        current_value = value
                    setattr(self.result, key, current_value)

    def transfer_media(self,
                       fileitem: FileItem,
                       in_meta: MetaBase,
                       mediainfo: MediaInfo,
                       target_storage: str,
                       target_path: Path,
                       transfer_type: str,
                       source_oper: StorageBase,
                       target_oper: StorageBase,
                       need_scrape: Optional[bool] = False,
                       need_rename: Optional[bool] = True,
                       need_notify: Optional[bool] = True,
                       overwrite_mode: Optional[str] = None,
                       episodes_info: List[TmdbEpisode] = None
                       ) -> TransferInfo:
        """
        识别并整理一个文件或者一个目录下的所有文件
        :param fileitem: 整理的文件对象，可能是一个文件也可以是一个目录
        :param in_meta：预识别元数据
        :param mediainfo: 媒体信息
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param transfer_type: 文件整理方式
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param need_scrape: 是否需要刮削
        :param need_rename: 是否需要重命名
        :param need_notify: 是否需要通知
        :param overwrite_mode: 覆盖模式
        :param episodes_info: 当前季的全部集信息
        :return: TransferInfo、错误信息
        """

        # 重置结果
        self.__reset_result()

        # 重命名格式
        rename_format = settings.TV_RENAME_FORMAT \
            if mediainfo.type == MediaType.TV else settings.MOVIE_RENAME_FORMAT

        # 判断是否为文件夹
        if fileitem.type == "dir":
            # 整理整个目录，一般为蓝光原盘
            if need_rename:
                new_path = self.get_rename_path(
                    path=target_path,
                    template_string=rename_format,
                    rename_dict=self.get_naming_dict(meta=in_meta,
                                                     mediainfo=mediainfo)
                ).parent
            else:
                new_path = target_path / fileitem.name
            # 整理目录
            new_diritem, errmsg = self.__transfer_dir(fileitem=fileitem,
                                                      mediainfo=mediainfo,
                                                      source_oper=source_oper,
                                                      target_oper=target_oper,
                                                      target_storage=target_storage,
                                                      target_path=new_path,
                                                      transfer_type=transfer_type)
            if not new_diritem:
                logger.error(f"文件夹 {fileitem.path} 整理失败：{errmsg}")
                self.__set_result(success=False,
                                  message=errmsg,
                                  fileitem=fileitem,
                                  transfer_type=transfer_type,
                                  need_notify=need_notify)
                return self.result

            logger.info(f"文件夹 {fileitem.path} 整理成功")
            # 计算目录下所有文件大小
            total_size = sum(file.stat().st_size for file in Path(fileitem.path).rglob('*') if file.is_file())
            # 返回整理后的路径
            self.__set_result(success=True,
                              fileitem=fileitem,
                              target_item=new_diritem,
                              target_diritem=new_diritem,
                              total_size=total_size,
                              need_scrape=need_scrape,
                              need_notify=need_notify,
                              transfer_type=transfer_type)
            return self.result
        else:
            # 整理单个文件
            if mediainfo.type == MediaType.TV:
                # 电视剧
                if in_meta.begin_episode is None:
                    logger.warn(f"文件 {fileitem.path} 整理失败：未识别到文件集数")
                    self.__set_result(success=False,
                                      message=f"未识别到文件集数",
                                      fileitem=fileitem,
                                      fail_list=[fileitem.path],
                                      transfer_type=transfer_type,
                                      need_notify=need_notify)
                    return self.result

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
            if need_rename:
                new_file = self.get_rename_path(
                    path=target_path,
                    template_string=rename_format,
                    rename_dict=self.get_naming_dict(
                        meta=in_meta,
                        mediainfo=mediainfo,
                        episodes_info=episodes_info,
                        file_ext=f".{fileitem.extension}"
                    )
                )
            else:
                new_file = target_path / fileitem.name

            # 判断是否要覆盖
            overflag = False
            # 计算重命名中的文件夹层级
            rename_format_level = len(rename_format.split("/")) - 1
            folder_path = new_file.parents[rename_format_level - 1]
            # 目标目录
            target_diritem = target_oper.get_folder(folder_path)
            if not target_diritem:
                logger.error(f"目标目录 {folder_path} 获取失败")
                self.__set_result(success=False,
                                  message=f"目标目录 {folder_path} 获取失败",
                                  fileitem=fileitem,
                                  fail_list=[fileitem.path],
                                  transfer_type=transfer_type,
                                  need_notify=need_notify)
                return self.result
            # 目标文件
            target_item = target_oper.get_item(new_file)
            if target_item:
                # 目标文件已存在
                target_file = new_file
                if target_storage == "local" and new_file.is_symlink():
                    target_file = new_file.readlink()
                    if not target_file.exists():
                        overflag = True
                if not overflag:
                    # 目标文件已存在
                    logger.info(f"目的文件系统中已经存在同名文件 {target_file}，当前整理覆盖模式设置为 {overwrite_mode}")
                    if overwrite_mode == 'always':
                        # 总是覆盖同名文件
                        overflag = True
                    elif overwrite_mode == 'size':
                        # 存在时大覆盖小
                        if target_item.size < fileitem.size:
                            logger.info(f"目标文件文件大小更小，将覆盖：{new_file}")
                            overflag = True
                        else:
                            self.__set_result(success=False,
                                              message=f"媒体库存在同名文件，且质量更好",
                                              fileitem=fileitem,
                                              target_item=target_item,
                                              target_diritem=target_diritem,
                                              fail_list=[fileitem.path],
                                              transfer_type=transfer_type,
                                              need_notify=need_notify)
                            return self.result
                    elif overwrite_mode == 'never':
                        # 存在不覆盖
                        self.__set_result(success=False,
                                          message=f"媒体库存在同名文件，当前覆盖模式为不覆盖",
                                          fileitem=fileitem,
                                          target_item=target_item,
                                          target_diritem=target_diritem,
                                          fail_list=[fileitem.path],
                                          transfer_type=transfer_type,
                                          need_notify=need_notify)
                        return self.result
                    elif overwrite_mode == 'latest':
                        # 仅保留最新版本
                        logger.info(f"当前整理覆盖模式设置为仅保留最新版本，将覆盖：{new_file}")
                        overflag = True
            else:
                if overwrite_mode == 'latest':
                    # 文件不存在，但仅保留最新版本
                    logger.info(f"当前整理覆盖模式设置为 {overwrite_mode}，仅保留最新版本，正在删除已有版本文件 ...")
                    self.__delete_version_files(target_oper, new_file)
            # 整理文件
            new_item, err_msg = self.__transfer_file(fileitem=fileitem,
                                                     mediainfo=mediainfo,
                                                     target_storage=target_storage,
                                                     target_file=new_file,
                                                     transfer_type=transfer_type,
                                                     over_flag=overflag,
                                                     source_oper=source_oper,
                                                     target_oper=target_oper)
            if not new_item:
                logger.error(f"文件 {fileitem.path} 整理失败：{err_msg}")
                self.__set_result(success=False,
                                  message=err_msg,
                                  fileitem=fileitem,
                                  fail_list=[fileitem.path],
                                  transfer_type=transfer_type,
                                  need_notify=need_notify)
                return self.result

            logger.info(f"文件 {fileitem.path} 整理成功")
            self.__set_result(success=True,
                              fileitem=fileitem,
                              target_item=new_item,
                              target_diritem=target_diritem,
                              need_scrape=need_scrape,
                              transfer_type=transfer_type,
                              need_notify=need_notify)
            return self.result

    @staticmethod
    def __transfer_command(fileitem: FileItem, target_storage: str,
                           source_oper: StorageBase, target_oper: StorageBase,
                           target_file: Path, transfer_type: str,
                           ) -> Tuple[Optional[FileItem], str]:
        """
        处理单个文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_file: 目标文件路径
        :param transfer_type: 整理方式
        """

        def __get_targetitem(_path: Path) -> FileItem:
            """
            获取文件信息
            """
            return FileItem(
                storage=target_storage,
                path=str(_path).replace("\\", "/"),
                name=_path.name,
                basename=_path.stem,
                type="file",
                size=_path.stat().st_size,
                extension=_path.suffix.lstrip('.'),
                modify_time=_path.stat().st_mtime
            )

        if (fileitem.storage != target_storage
                and fileitem.storage != "local" and target_storage != "local"):
            return None, f"不支持 {fileitem.storage} 到 {target_storage} 的文件整理"

        # 加锁
        with lock:
            if fileitem.storage == "local" and target_storage == "local":
                # 创建目录
                if not target_file.parent.exists():
                    target_file.parent.mkdir(parents=True)
                # 本地到本地
                if transfer_type == "copy":
                    state = source_oper.copy(fileitem, target_file.parent, target_file.name)
                elif transfer_type == "move":
                    state = source_oper.move(fileitem, target_file.parent, target_file.name)
                elif transfer_type == "link":
                    state = source_oper.link(fileitem, target_file)
                elif transfer_type == "softlink":
                    state = source_oper.softlink(fileitem, target_file)
                else:
                    return None, f"不支持的整理方式：{transfer_type}"
                if state:
                    return __get_targetitem(target_file), ""
                else:
                    return None, f"{fileitem.path} {transfer_type} 失败"
            elif fileitem.storage == "local" and target_storage != "local":
                # 本地到网盘
                filepath = Path(fileitem.path)
                if not filepath.exists():
                    return None, f"文件 {filepath} 不存在"
                if transfer_type == "copy":
                    # 复制
                    # 根据目的路径创建文件夹
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        # 上传文件
                        new_item = target_oper.upload(target_fileitem, filepath, target_file.name)
                        if new_item:
                            return new_item, ""
                        else:
                            return None, f"{fileitem.path} 上传 {target_storage} 失败"
                    else:
                        return None, f"【{target_storage}】{target_file.parent} 目录获取失败"
                elif transfer_type == "move":
                    # 移动
                    # 根据目的路径获取文件夹
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        # 上传文件
                        new_item = target_oper.upload(target_fileitem, filepath, target_file.name)
                        if new_item:
                            # 删除源文件
                            source_oper.delete(fileitem)
                            return new_item, ""
                        else:
                            return None, f"{fileitem.path} 上传 {target_storage} 失败"
                    else:
                        return None, f"【{target_storage}】{target_file.parent} 目录获取失败"
            elif fileitem.storage != "local" and target_storage == "local":
                # 网盘到本地
                if target_file.exists():
                    logger.warn(f"文件已存在：{target_file}")
                    return __get_targetitem(target_file), ""
                # 网盘到本地
                if transfer_type in ["copy", "move"]:
                    # 下载
                    tmp_file = source_oper.download(fileitem=fileitem, path=target_file.parent)
                    if tmp_file:
                        # 创建目录
                        if not target_file.parent.exists():
                            target_file.parent.mkdir(parents=True)
                        # 将tmp_file移动后target_file
                        SystemUtils.move(tmp_file, target_file)
                        if transfer_type == "move":
                            # 删除源文件
                            source_oper.delete(fileitem)
                        return __get_targetitem(target_file), ""
                    else:
                        return None, f"{fileitem.path} {fileitem.storage} 下载失败"
            elif fileitem.storage == target_storage:
                # 同一网盘
                if transfer_type == "copy":
                    # 复制文件到新目录
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        if source_oper.move(fileitem, Path(target_fileitem.path), target_file.name):
                            return target_oper.get_item(target_file), ""
                        else:
                            return None, f"【{target_storage}】{fileitem.path} 复制文件失败"
                    else:
                        return None, f"【{target_storage}】{target_file.parent} 目录获取失败"
                elif transfer_type == "move":
                    # 移动文件到新目录
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        if source_oper.move(fileitem, Path(target_fileitem.path), target_file.name):
                            return target_oper.get_item(target_file), ""
                        else:
                            return None, f"【{target_storage}】{fileitem.path} 移动文件失败"
                    else:
                        return None, f"【{target_storage}】{target_file.parent} 目录获取失败"
                else:
                    return None, f"不支持的整理方式：{transfer_type}"

        return None, "未知错误"

    def __transfer_other_files(self, fileitem: FileItem, target_storage: str,
                               source_oper: StorageBase, target_oper: StorageBase,
                               target_file: Path, transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理其他相关文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_file: 目标路径
        :param transfer_type: 整理方式
        """
        # 整理字幕
        state, errmsg = self.__transfer_subtitles(fileitem=fileitem,
                                                  target_storage=target_storage,
                                                  source_oper=source_oper,
                                                  target_oper=target_oper,
                                                  target_file=target_file,
                                                  transfer_type=transfer_type)
        if not state:
            return False, errmsg
        # 整理音轨文件
        state, errmsg = self.__transfer_audio_track_files(fileitem=fileitem,
                                                          target_storage=target_storage,
                                                          source_oper=source_oper,
                                                          target_oper=target_oper,
                                                          target_file=target_file,
                                                          transfer_type=transfer_type)

        return state, errmsg

    def __transfer_subtitles(self, fileitem: FileItem, target_storage: str,
                             source_oper: StorageBase, target_oper: StorageBase,
                             target_file: Path, transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理对应字幕文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_file: 目标路径
        :param transfer_type: 整理方式
        """
        # 字幕正则式
        _zhcn_sub_re = r"([.\[(](((zh[-_])?(cn|ch[si]|sg|sc))|zho?" \
                       r"|chinese|(cn|ch[si]|sg|zho?|eng)[-_&]?(cn|ch[si]|sg|zho?|eng)" \
                       r"|简[体中]?)[.\])])" \
                       r"|([\u4e00-\u9fa5]{0,3}[中双][\u4e00-\u9fa5]{0,2}[字文语][\u4e00-\u9fa5]{0,3})" \
                       r"|简体|简中|JPSC|sc_jp" \
                       r"|(?<![a-z0-9])gb(?![a-z0-9])"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))" \
                       r"|(cht|eng)[-_&]?(cht|eng)" \
                       r"|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|JPTC|tc_jp" \
                       r"|(?<![a-z0-9])big5(?![a-z0-9])"
        _eng_sub_re = r"[.\[(]eng[.\])]"

        # 比对文件名并整理字幕
        org_path = Path(fileitem.path)
        # 查找上级文件项
        parent_item: FileItem = source_oper.get_parent(fileitem)
        if not parent_item:
            return False, f"{org_path} 上级目录获取失败"
        # 字幕文件列表
        file_list: List[FileItem] = source_oper.list(parent_item) or []
        file_list = [f for f in file_list if f.type == "file" and f.extension
                     and f".{f.extension.lower()}" in settings.RMT_SUBEXT]
        if len(file_list) == 0:
            logger.info(f"{parent_item.path} 目录下没有找到字幕文件...")
        else:
            logger.info(f"字幕文件清单：{[f.name for f in file_list]}")
            # 识别文件名
            metainfo = MetaInfoPath(org_path)
            for sub_item in file_list:
                # 识别字幕文件名
                sub_file_name = re.sub(_zhtw_sub_re,
                                       ".",
                                       re.sub(_zhcn_sub_re,
                                              ".",
                                              sub_item.name,
                                              flags=re.I),
                                       flags=re.I)
                sub_file_name = re.sub(_eng_sub_re, ".", sub_file_name, flags=re.I)
                sub_metainfo = MetaInfoPath(Path(sub_item.path))
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
                    if re.search(_zhcn_sub_re, sub_item.name, re.I):
                        new_file_type = ".chi.zh-cn"
                    elif re.search(_zhtw_sub_re, sub_item.name,
                                   re.I):
                        new_file_type = ".zh-tw"
                    elif re.search(_eng_sub_re, sub_item.name, re.I):
                        new_file_type = ".eng"
                    # 通过对比字幕文件大小  尽量整理所有存在的字幕
                    file_ext = f".{sub_item.extension}"
                    new_sub_tag_dict = {
                        ".eng": ".英文",
                        ".chi.zh-cn": ".简体中文",
                        ".zh-tw": ".繁体中文"
                    }
                    new_sub_tag_list = [
                        (".default" + new_file_type if (
                                (settings.DEFAULT_SUB == "zh-cn" and new_file_type == ".chi.zh-cn") or
                                (settings.DEFAULT_SUB == "zh-tw" and new_file_type == ".zh-tw") or
                                (settings.DEFAULT_SUB == "eng" and new_file_type == ".eng")
                        ) else new_file_type) if t == 0 else "%s%s(%s)" % (new_file_type,
                                                                           new_sub_tag_dict.get(
                                                                               new_file_type, ""
                                                                           ),
                                                                           t) for t in range(6)
                    ]
                    for new_sub_tag in new_sub_tag_list:
                        new_file: Path = target_file.with_name(target_file.stem + new_sub_tag + file_ext)
                        # 如果字幕文件不存在, 直接整理字幕, 并跳出循环
                        try:
                            logger.debug(f"正在处理字幕：{sub_item.name}")
                            new_item, errmsg = self.__transfer_command(fileitem=sub_item,
                                                                       target_storage=target_storage,
                                                                       source_oper=source_oper,
                                                                       target_oper=target_oper,
                                                                       target_file=new_file,
                                                                       transfer_type=transfer_type)
                            if new_item:
                                logger.info(f"字幕 {sub_item.name} 整理完成")
                                self.__set_result(
                                    subtitle_list=[sub_item.path],
                                    subtitle_list_new=[new_item.path],
                                )
                                break
                            else:
                                logger.error(f"字幕 {sub_item.name} 整理失败：{errmsg}")
                                return False, errmsg
                        except Exception as error:
                            logger.info(f"字幕 {new_file} 出错了,原因: {str(error)}")
        return True, ""

    def __transfer_audio_track_files(self, fileitem: FileItem, target_storage: str,
                                     source_oper: StorageBase, target_oper: StorageBase,
                                     target_file: Path, transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理对应音轨文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_file: 目标路径
        :param transfer_type: 整理方式
        """
        org_path = Path(fileitem.path)
        # 查找上级文件项
        parent_item: FileItem = source_oper.get_parent(fileitem)
        if not parent_item:
            return False, f"{org_path} 上级目录获取失败"
        file_list: List[FileItem] = source_oper.list(parent_item)
        # 匹配音轨文件
        pending_file_list: List[FileItem] = [file for file in file_list
                                             if Path(file.name).stem == org_path.stem
                                             and file.type == "file" and file.extension
                                             and f".{file.extension.lower()}" in settings.RMT_AUDIOEXT]
        if len(pending_file_list) == 0:
            return True, f"{parent_item.path} 目录下没有找到匹配的音轨文件"
        logger.debug("音轨文件清单：" + str(pending_file_list))
        for track_file in pending_file_list:
            track_ext = f".{track_file.extension}"
            new_track_file = target_file.with_name(target_file.stem + track_ext)
            try:
                logger.info(f"正在整理音轨文件：{track_file} 到 {new_track_file}")
                new_item, errmsg = self.__transfer_command(fileitem=track_file,
                                                           target_storage=target_storage,
                                                           source_oper=source_oper,
                                                           target_oper=target_oper,
                                                           target_file=new_track_file,
                                                           transfer_type=transfer_type)
                if new_item:
                    logger.info(f"音轨文件 {org_path.name} 整理完成")
                    self.__set_result(
                        audio_list=[track_file.path],
                        audio_list_new=[new_item.path],
                    )
                else:
                    logger.error(f"音轨文件 {org_path.name} 整理失败：{errmsg}")
            except Exception as error:
                logger.error(f"音轨文件 {org_path.name} 整理失败：{str(error)}")
        return True, ""

    def __transfer_dir(self, fileitem: FileItem, mediainfo: MediaInfo,
                       source_oper: StorageBase, target_oper: StorageBase,
                       transfer_type: str, target_storage: str, target_path: Path) -> Tuple[Optional[FileItem], str]:
        """
        整理整个文件夹
        :param fileitem: 源文件
        :param mediainfo: 媒体信息
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param transfer_type: 整理方式
        :param target_storage: 目标存储
        :param target_path: 目标路径
        """
        logger.info(f"正在整理目录：{fileitem.path} 到 {target_path}")
        target_item = target_oper.get_folder(target_path)
        if not target_item:
            return None, f"获取目标目录失败：{target_path}"
        event_data = TransferInterceptEventData(
            fileitem=fileitem,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type
        )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if event and event.event_data:
            event_data = event.event_data
            # 如果事件被取消，跳过文件整理
            if event_data.cancel:
                logger.debug(
                    f"Transfer dir canceled by event: {event_data.source},"
                    f"Reason: {event_data.reason}")
                return None, event_data.reason
        # 处理所有文件
        state, errmsg = self.__transfer_dir_files(fileitem=fileitem,
                                                  target_storage=target_storage,
                                                  source_oper=source_oper,
                                                  target_oper=target_oper,
                                                  target_path=target_path,
                                                  transfer_type=transfer_type)
        if state:
            return target_item, errmsg
        else:
            return None, errmsg

    def __transfer_dir_files(self, fileitem: FileItem, target_storage: str,
                             source_oper: StorageBase, target_oper: StorageBase,
                             transfer_type: str, target_path: Path) -> Tuple[bool, str]:
        """
        按目录结构整理目录下所有文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_path: 目标路径
        :param transfer_type: 整理方式
        """
        file_list: List[FileItem] = source_oper.list(fileitem)
        # 整理文件
        for item in file_list:
            if item.type == "dir":
                # 递归整理目录
                new_path = target_path / item.name
                state, errmsg = self.__transfer_dir_files(fileitem=item,
                                                          target_storage=target_storage,
                                                          source_oper=source_oper,
                                                          target_oper=target_oper,
                                                          transfer_type=transfer_type,
                                                          target_path=new_path)
                if not state:
                    return False, errmsg
            else:
                # 整理文件
                new_file = target_path / item.name
                new_item, errmsg = self.__transfer_command(fileitem=item,
                                                           target_storage=target_storage,
                                                           source_oper=source_oper,
                                                           target_oper=target_oper,
                                                           target_file=new_file,
                                                           transfer_type=transfer_type)
                if not new_item:
                    return False, errmsg
                self.__set_result(
                    file_list=[item.path],
                    file_list_new=[new_item.path],
                )
        # 返回成功
        return True, ""

    def __transfer_file(self, fileitem: FileItem, mediainfo: MediaInfo,
                        source_oper: StorageBase, target_oper: StorageBase,
                        target_storage: str, target_file: Path,
                        transfer_type: str, over_flag: Optional[bool] = False) -> Tuple[Optional[FileItem], str]:
        """
        整理一个文件，同时处理其他相关文件
        :param fileitem: 原文件
        :param mediainfo: 媒体信息
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_storage: 目标存储
        :param target_file: 新文件
        :param transfer_type: 整理方式
        :param over_flag: 是否覆盖，为True时会先删除再整理
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        """
        logger.info(f"正在整理文件：【{fileitem.storage}】{fileitem.path} 到 【{target_storage}】{target_file}，"
                    f"操作类型：{transfer_type}")
        event_data = TransferInterceptEventData(
            fileitem=fileitem,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_file,
            transfer_type=transfer_type,
            options={
                "over_flag": over_flag
            }
        )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if event and event.event_data:
            event_data = event.event_data
            # 如果事件被取消，跳过文件整理
            if event_data.cancel:
                logger.debug(
                    f"Transfer file canceled by event: {event_data.source},"
                    f"Reason: {event_data.reason}")
                return None, event_data.reason
        if target_storage == "local" and (target_file.exists() or target_file.is_symlink()):
            if not over_flag:
                logger.warn(f"文件已存在：{target_file}")
                return None, f"{target_file} 已存在"
            else:
                logger.info(f"正在删除已存在的文件：{target_file}")
                target_file.unlink()
        new_item, errmsg = self.__transfer_command(fileitem=fileitem,
                                                   target_storage=target_storage,
                                                   source_oper=source_oper,
                                                   target_oper=target_oper,
                                                   target_file=target_file,
                                                   transfer_type=transfer_type)
        if new_item:
            self.__set_result(
                file_list=[fileitem.path],
                file_list_new=[new_item.path],
                file_count=1,
                total_size=fileitem.size,
            )
            # 处理其他相关文件
            self.__transfer_other_files(fileitem=fileitem,
                                        target_storage=target_storage,
                                        source_oper=source_oper,
                                        target_oper=target_oper,
                                        target_file=target_file,
                                        transfer_type=transfer_type)
            return new_item, errmsg

        return None, errmsg

    @staticmethod
    def get_dest_path(mediainfo: MediaInfo, target_path: Path,
                      need_type_folder: Optional[bool] = False, need_category_folder: Optional[bool] = False):
        """
        获取目标路径
        """
        if need_type_folder:
            target_path = target_path / mediainfo.type.value
        if need_category_folder and mediainfo.category:
            target_path = target_path / mediainfo.category
        return target_path

    @staticmethod
    def get_dest_dir(mediainfo: MediaInfo, target_dir: TransferDirectoryConf,
                     need_type_folder: Optional[bool] = None, need_category_folder: Optional[bool] = None) -> Path:
        """
        根据设置并装媒体库目录
        :param mediainfo: 媒体信息
        :param target_dir: 媒体库根目录
        :param need_type_folder: 是否需要按媒体类型创建目录
        :param need_category_folder: 是否需要按媒体类别创建目录
        """
        if need_type_folder is None:
            need_type_folder = target_dir.library_type_folder
        if need_category_folder is None:
            need_category_folder = target_dir.library_category_folder
        if not target_dir.media_type and need_type_folder:
            # 一级自动分类
            library_dir = Path(target_dir.library_path) / mediainfo.type.value
        elif target_dir.media_type and need_type_folder:
            # 一级手动分类
            library_dir = Path(target_dir.library_path) / target_dir.media_type
        else:
            library_dir = Path(target_dir.library_path)
        if not target_dir.media_category and need_category_folder and mediainfo.category:
            # 二级自动分类
            library_dir = library_dir / mediainfo.category
        elif target_dir.media_category and need_category_folder:
            # 二级手动分类
            library_dir = library_dir / target_dir.media_category

        return library_dir

    @staticmethod
    def get_naming_dict(meta: MetaBase, mediainfo: MediaInfo, file_ext: Optional[str] = None,
                        episodes_info: List[TmdbEpisode] = None) -> dict:
        """
        根据媒体信息，返回Format字典
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        """
        return TemplateHelper().builder.build(meta=meta, mediainfo=mediainfo,
                                              file_extension=file_ext, episodes_info=episodes_info)

    @staticmethod
    def __delete_version_files(storage_oper: StorageBase, path: Path) -> bool:
        """
        删除目录下的所有版本文件
        :param storage_oper: 存储操作对象
        :param path: 目录路径
        """
        # 存储
        if not storage_oper:
            return False
        # 识别文件中的季集信息
        meta = MetaInfoPath(path)
        season = meta.season
        episode = meta.episode
        logger.warn(f"正在删除目标目录中其它版本的文件：{path.parent}")
        # 获取父目录
        parent_item = storage_oper.get_item(path.parent)
        if not parent_item:
            logger.warn(f"目录 {path.parent} 不存在")
            return False
        # 检索媒体文件
        media_files = storage_oper.list(parent_item)
        if not media_files:
            logger.info(f"目录 {path.parent} 中没有文件")
            return False
        # 删除文件
        for media_file in media_files:
            media_path = Path(media_file.path)
            if media_path == path:
                continue
            if media_file.type != "file":
                continue
            if f".{media_file.extension.lower()}" not in settings.RMT_MEDIAEXT:
                continue
            # 识别文件中的季集信息
            filemeta = MetaInfoPath(media_path)
            # 相同季集的文件才删除
            if filemeta.season != season or filemeta.episode != episode:
                continue
            logger.info(f"正在删除文件：{media_file.name}")
            storage_oper.delete(media_file)
        return True

    @staticmethod
    def get_rename_path(template_string: str, rename_dict: dict, path: Path = None) -> Path:
        """
        生成重命名后的完整路径，支持智能重命名事件
        :param template_string: Jinja2 模板字符串
        :param rename_dict: 渲染上下文，用于替换模板中的变量
        :param path: 可选的基础路径，如果提供，将在其基础上拼接生成的路径
        :return: 生成的完整路径
        """
        # 创建jinja2模板对象
        template = Template(template_string)
        # 渲染生成的字符串
        render_str = template.render(rename_dict)

        logger.debug(f"Initial render string: {render_str}")
        # 发送智能重命名事件
        event_data = TransferRenameEventData(
            template_string=template_string,
            rename_dict=rename_dict,
            render_str=render_str,
            path=path
        )
        event = eventmanager.send_event(ChainEventType.TransferRename, event_data)
        # 检查事件返回的结果
        if event and event.event_data:
            event_data: TransferRenameEventData = event.event_data
            if event_data.updated and event_data.updated_str:
                logger.debug(f"Render string updated by event: "
                             f"{render_str} -> {event_data.updated_str} (source: {event_data.source})")
                render_str = event_data.updated_str

        # 目的路径
        if path:
            return path / render_str
        else:
            return Path(render_str)
