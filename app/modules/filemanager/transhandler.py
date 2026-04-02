import re
from pathlib import Path
from typing import Optional, List, Tuple

from jinja2 import Template

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.helper.directory import DirectoryHelper
from app.helper.message import TemplateHelper
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas import TransferInfo, TmdbEpisode, TransferDirectoryConf, FileItem, TransferInterceptEventData, \
    TransferRenameEventData
from app.schemas.types import MediaType, ChainEventType
from app.utils.system import SystemUtils


class TransHandler:
    """
    文件转移整理类
    """

    def __init__(self):
        pass

    @staticmethod
    def __update_result(result: TransferInfo, **kwargs):
        """
        更新结果
        """
        # 设置值
        for key, value in kwargs.items():
            if hasattr(result, key):
                current_value = getattr(result, key)
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
                elif isinstance(current_value, bool):
                    current_value = value
                elif isinstance(current_value, int):
                    current_value += (value or 0)
                else:
                    current_value = value
                setattr(result, key, current_value)

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

        def __is_subtitle_file(_fileitem: FileItem) -> bool:
            """
            判断是否为字幕文件
            :param _fileitem: 文件项
            :return: True/False
            """
            if not _fileitem.extension:
                return False
            if f".{_fileitem.extension.lower()}" in settings.RMT_SUBEXT:
                return True
            return False

        def __is_extra_file(_fileitem: FileItem) -> bool:
            """
            判断是否为附加文件
            :param _fileitem: 文件项
            :return: True/False
            """
            if not _fileitem.extension:
                return False
            if f".{_fileitem.extension.lower()}" in (settings.RMT_SUBEXT + settings.RMT_AUDIOEXT):
                return True
            return False

        # 整理结果
        result = TransferInfo()

        try:

            # 重命名格式
            rename_format = settings.RENAME_FORMAT(mediainfo.type)

            # 判断是否为文件夹
            if fileitem.type == "dir":
                # 整理整个目录，一般为蓝光原盘
                if need_rename:
                    new_path = self.get_rename_path(
                        path=target_path,
                        template_string=rename_format,
                        rename_dict=self.get_naming_dict(meta=in_meta,
                                                         mediainfo=mediainfo),
                        source_path=fileitem.path
                    )
                    new_path = DirectoryHelper.get_media_root_path(
                        rename_format, rename_path=new_path
                    )
                    if not new_path:
                        self.__update_result(
                            result=result,
                            success=False,
                            message="重命名格式无效",
                            fileitem=fileitem,
                            transfer_type=transfer_type,
                            need_notify=need_notify,
                        )
                        return result
                else:
                    new_path = target_path / fileitem.name
                # 原盘大小只计算STREAM目录内的文件大小
                if stream_fileitem := source_oper.get_item(
                        Path(fileitem.path) / "BDMV" / "STREAM"
                ):
                    fileitem.size = sum(
                        file.size for file in source_oper.list(stream_fileitem) or []
                    )
                # 整理目录
                new_diritem, errmsg = self.__transfer_dir(fileitem=fileitem,
                                                          mediainfo=mediainfo,
                                                          source_oper=source_oper,
                                                          target_oper=target_oper,
                                                          target_storage=target_storage,
                                                          target_path=new_path,
                                                          transfer_type=transfer_type,
                                                          result=result)
                if not new_diritem:
                    logger.error(f"文件夹 {fileitem.path} 整理失败：{errmsg}")
                    self.__update_result(result=result,
                                         success=False,
                                         message=errmsg,
                                         fileitem=fileitem,
                                         transfer_type=transfer_type,
                                         need_notify=need_notify)
                    return result

                logger.info(f"文件夹 {fileitem.path} 整理成功")
                # 返回整理后的路径
                self.__update_result(result=result,
                                     success=True,
                                     fileitem=fileitem,
                                     target_item=new_diritem,
                                     target_diritem=new_diritem,
                                     need_scrape=need_scrape,
                                     need_notify=need_notify,
                                     transfer_type=transfer_type)
                return result
            else:
                # 整理单个文件
                if mediainfo.type == MediaType.TV:
                    # 电视剧
                    if in_meta.begin_episode is None:
                        logger.warn(f"文件 {fileitem.path} 整理失败：未识别到文件集数")
                        self.__update_result(result=result,
                                             success=False,
                                             message="未识别到文件集数",
                                             fileitem=fileitem,
                                             fail_list=[fileitem.path],
                                             transfer_type=transfer_type,
                                             need_notify=need_notify)
                        return result

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
                        ),
                        source_path=fileitem.path
                    )

                    # 针对字幕文件，文件名中补充额外标识信息
                    if __is_subtitle_file(fileitem):
                        new_file = self.__rename_subtitles(fileitem, new_file)

                    # 文件目录
                    folder_path = DirectoryHelper.get_media_root_path(
                        rename_format, rename_path=new_file
                    )
                    if not folder_path:
                        self.__update_result(
                            result=result,
                            success=False,
                            message="重命名格式无效",
                            fileitem=fileitem,
                            fail_list=[fileitem.path],
                            transfer_type=transfer_type,
                            need_notify=need_notify,
                        )
                        return result
                else:
                    new_file = target_path / fileitem.name
                    folder_path = target_path

                # 目标目录
                target_diritem = target_oper.get_folder(folder_path)
                if not target_diritem:
                    logger.error(f"目标目录 {folder_path} 获取失败")
                    self.__update_result(result=result,
                                         success=False,
                                         message=f"目标目录 {folder_path} 获取失败",
                                         fileitem=fileitem,
                                         fail_list=[fileitem.path],
                                         transfer_type=transfer_type,
                                         need_notify=need_notify)
                    return result

                # 判断是否要覆盖，附加文件强制覆盖
                overflag = False
                if not __is_extra_file(fileitem):
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
                            logger.info(
                                f"目的文件系统中已经存在同名文件 {target_file}，当前整理覆盖模式设置为 {overwrite_mode}")
                            if overwrite_mode == 'always':
                                # 总是覆盖同名文件
                                overflag = True
                            elif overwrite_mode == 'size':
                                # 存在时大覆盖小
                                if target_item.size < fileitem.size:
                                    logger.info(f"目标文件文件大小更小，将覆盖：{new_file}")
                                    overflag = True
                                else:
                                    self.__update_result(result=result,
                                                         success=False,
                                                         message=f"媒体库存在同名文件，且质量更好",
                                                         fileitem=fileitem,
                                                         target_item=target_item,
                                                         target_diritem=target_diritem,
                                                         fail_list=[fileitem.path],
                                                         transfer_type=transfer_type,
                                                         need_notify=need_notify)
                                    return result
                            elif overwrite_mode == 'never':
                                # 存在不覆盖
                                self.__update_result(result=result,
                                                     success=False,
                                                     message=f"媒体库存在同名文件，当前覆盖模式为不覆盖",
                                                     fileitem=fileitem,
                                                     target_item=target_item,
                                                     target_diritem=target_diritem,
                                                     fail_list=[fileitem.path],
                                                     transfer_type=transfer_type,
                                                     need_notify=need_notify)
                                return result
                            elif overwrite_mode == 'latest':
                                # 仅保留最新版本
                                logger.info(f"当前整理覆盖模式设置为仅保留最新版本，将覆盖：{new_file}")
                                overflag = True
                    else:
                        if overwrite_mode == 'latest':
                            # 文件不存在，但仅保留最新版本
                            logger.info(
                                f"当前整理覆盖模式设置为 {overwrite_mode}，仅保留最新版本，正在删除已有版本文件 ...")
                            self.__delete_version_files(target_oper, new_file)
                else:
                    # 附加文件 总是需要覆盖
                    overflag = True

                # 整理文件
                new_item, err_msg = self.__transfer_file(fileitem=fileitem,
                                                         mediainfo=mediainfo,
                                                         target_storage=target_storage,
                                                         target_file=new_file,
                                                         transfer_type=transfer_type,
                                                         over_flag=overflag,
                                                         source_oper=source_oper,
                                                         target_oper=target_oper,
                                                         result=result)
                if not new_item:
                    logger.error(f"文件 {fileitem.path} 整理失败：{err_msg}")
                    self.__update_result(result=result,
                                         success=False,
                                         message=err_msg,
                                         fileitem=fileitem,
                                         fail_list=[fileitem.path],
                                         transfer_type=transfer_type,
                                         need_notify=need_notify)
                    return result

                logger.info(f"文件 {fileitem.path} 整理成功")
                self.__update_result(result=result,
                                     success=True,
                                     fileitem=fileitem,
                                     target_item=new_item,
                                     target_diritem=target_diritem,
                                     need_scrape=need_scrape,
                                     transfer_type=transfer_type,
                                     need_notify=need_notify)
                return result
        except Exception as e:
            logger.error(f"媒体整理出错：{e}")
            return TransferInfo(success=False, message=str(e))

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
                path=_path.as_posix(),
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

        if fileitem.storage == "local" and target_storage == "local":
            # 创建目录
            if not target_file.parent.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
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
                        target_file.parent.mkdir(parents=True, exist_ok=True)
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
            if not source_oper.is_support_transtype(transfer_type):
                return None, f"存储 {fileitem.storage} 不支持 {transfer_type} 整理方式"

            if transfer_type == "copy":
                # 复制文件到新目录
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    if source_oper.copy(fileitem, Path(target_fileitem.path), target_file.name):
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
            elif transfer_type == "link":
                if source_oper.link(fileitem, target_file):
                    return target_oper.get_item(target_file), ""
                else:
                    return None, f"【{target_storage}】{fileitem.path} 创建硬链接失败"
            else:
                return None, f"不支持的整理方式：{transfer_type}"

        return None, "未知错误"

    @staticmethod
    def __rename_subtitles(sub_item: FileItem, new_file: Path) -> Path:
        """
        重命名字幕文件，补充附加信息
        """
        # 字幕正则式
        _zhcn_sub_re = r"([.\[(\s](((zh[-_])?(cn|ch[si]|sg|sc))|zho?" \
                       r"|chinese|(cn|ch[si]|sg|zho?)[-_&]?(cn|ch[si]|sg|zho?|eng|jap|ja|jpn)" \
                       r"|eng[-_&]?(cn|ch[si]|sg|zho?)|(jap|ja|jpn)[-_&]?(cn|ch[si]|sg|zho?)" \
                       r"|简[体中]?)[.\])\s])" \
                       r"|([\u4e00-\u9fa5]{0,3}[中双][\u4e00-\u9fa5]{0,2}[字文语][\u4e00-\u9fa5]{0,3})" \
                       r"|简体|简中|JPSC|sc_jp" \
                       r"|(?<![a-z0-9])gb(?![a-z0-9])"
        _zhtw_sub_re = r"([.\[(\s](((zh[-_])?(hk|tw|cht|tc))" \
                       r"|cht[-_&]?(cht|eng|jap|ja|jpn)" \
                       r"|eng[-_&]?cht|(jap|ja|jpn)[-_&]?cht" \
                       r"|繁[体中]?)[.\])\s])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|JPTC|tc_jp" \
                       r"|(?<![a-z0-9])big5(?![a-z0-9])"
        _ja_sub_re = r"([.\[(\s](ja-jp|jap|ja|jpn" \
                     r"|(jap|ja|jpn)[-_&]?eng|eng[-_&]?(jap|ja|jpn))[.\])\s])" \
                     r"|日本語|日語"
        _eng_sub_re = r"[.\[(\s]eng[.\])\s]"

        # 原文件后缀
        file_ext = f".{sub_item.extension}"
        # 新文件后缀
        new_file_type = ""

        # 识别字幕语言
        if re.search(_zhcn_sub_re, sub_item.name, re.I):
            new_file_type = ".chi.zh-cn"
        elif re.search(_zhtw_sub_re, sub_item.name, re.I):
            new_file_type = ".zh-tw"
        elif re.search(_ja_sub_re, sub_item.name, re.I):
            new_file_type = ".ja"
        elif re.search(_eng_sub_re, sub_item.name, re.I):
            new_file_type = ".eng"

        # 添加默认字幕标识
        if ((settings.DEFAULT_SUB == "zh-cn" and new_file_type == ".chi.zh-cn")
                or (settings.DEFAULT_SUB == "zh-tw" and new_file_type == ".zh-tw")
                or (settings.DEFAULT_SUB == "ja" and new_file_type == ".ja")
                or (settings.DEFAULT_SUB == "eng" and new_file_type == ".eng")):
            new_sub_tag = ".default" + new_file_type
        else:
            new_sub_tag = new_file_type

        return new_file.with_name(new_file.stem + new_sub_tag + file_ext)

    def __transfer_dir(self, fileitem: FileItem, mediainfo: MediaInfo,
                       source_oper: StorageBase, target_oper: StorageBase,
                       transfer_type: str, target_storage: str, target_path: Path,
                       result: TransferInfo) -> Tuple[Optional[FileItem], str]:
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
                                                  transfer_type=transfer_type,
                                                  result=result)
        if state:
            return target_item, errmsg
        else:
            return None, errmsg

    def __transfer_dir_files(self, fileitem: FileItem, target_storage: str,
                             source_oper: StorageBase, target_oper: StorageBase,
                             transfer_type: str, target_path: Path,
                             result: TransferInfo) -> Tuple[bool, str]:
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
                                                          target_path=new_path,
                                                          result=result)
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
                self.__update_result(
                    result=result,
                    file_list=[item.path],
                    file_list_new=[new_item.path],
                )
        # 返回成功
        return True, ""

    def __transfer_file(self, fileitem: FileItem, mediainfo: MediaInfo,
                        source_oper: StorageBase, target_oper: StorageBase,
                        target_storage: str, target_file: Path,
                        transfer_type: str, result: TransferInfo,
                        over_flag: Optional[bool] = False) -> Tuple[Optional[FileItem], str]:
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
        else:
            exists_item = target_oper.get_item(target_file)
            if exists_item:
                if not over_flag:
                    logger.warn(f"文件已存在：【{target_storage}】{target_file}")
                    return None, f"【{target_storage}】{target_file} 已存在"
                else:
                    logger.info(f"正在删除已存在的文件：【{target_storage}】{target_file}")
                    target_oper.delete(exists_item)
        # 执行文件整理命令
        new_item, errmsg = self.__transfer_command(fileitem=fileitem,
                                                   target_storage=target_storage,
                                                   source_oper=source_oper,
                                                   target_oper=target_oper,
                                                   target_file=target_file,
                                                   transfer_type=transfer_type)
        if new_item:
            self.__update_result(
                result=result,
                file_list=[fileitem.path],
                file_list_new=[new_item.path],
                file_count=1,
                total_size=fileitem.size,
            )
            return new_item, errmsg

        return None, errmsg

    @staticmethod
    def get_dest_path(mediainfo: MediaInfo, target_path: Path,
                      need_type_folder: Optional[bool] = False, need_category_folder: Optional[bool] = False):
        """
        获取目标路径
        """
        if need_type_folder and mediainfo.type:
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
        if not target_dir.media_type and need_type_folder and mediainfo.type:
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
            # 当前只有视频文件需要保留最新版本，其余格式无需处理，以避免误删 (issue 5449)
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
    def get_rename_path(template_string: str, rename_dict: dict,
                        path: Path = None, source_path: str = None) -> Path:
        """
        生成重命名后的完整路径，支持智能重命名事件
        :param template_string: Jinja2 模板字符串
        :param rename_dict: 渲染上下文，用于替换模板中的变量
        :param path: 可选的基础路径，如果提供，将在其基础上拼接生成的路径
        :param source_path: 源文件路径，即待整理的文件路径
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
            path=path,
            source_path=source_path
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
