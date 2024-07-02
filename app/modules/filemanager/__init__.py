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
from app.helper.module import ModuleHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.filemanager.storage import StorageBase
from app.schemas import TransferInfo, ExistMediaInfo, TmdbEpisode, TransferDirectoryConf, FileItem
from app.schemas.types import MediaType
from app.utils.system import SystemUtils

lock = Lock()


class FileManagerModule(_ModuleBase):
    """
    文件整理模块
    """

    _storage_schemas = []

    def __init__(self):
        super().__init__()
        self.directoryhelper = DirectoryHelper()
        self.messagehelper = MessageHelper()

    def init_module(self) -> None:
        # 加载模块
        self._storage_schemas = ModuleHelper.load('app.modules.filetransfer.storage',
                                                  filter_func=lambda _, obj: hasattr(obj, 'schema'))

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
        # 检查本地下载目录是否存在
        download_paths = directoryhelper.get_local_download_dirs()
        if not download_paths:
            return False, "下载目录未设置"
        for d_path in download_paths:
            path = d_path.download_path
            if not path:
                return False, f"下载目录 {d_path.name} 对应路径未设置"
            download_path = Path(path)
            if not download_path.exists():
                return False, f"下载目录 {d_path.name} 对应路径 {path} 不存在"
        # 检查本地媒体库目录是否存在
        libaray_paths = directoryhelper.get_local_library_dirs()
        if not libaray_paths:
            return False, "媒体库目录未设置"
        for l_path in libaray_paths:
            path = l_path.library_path
            if not path:
                return False, f"媒体库目录 {l_path.name} 对应路径未设置"
            library_path = Path(path)
            if not library_path.exists():
                return False, f"媒体库目录{l_path.name} 对应的路径 {path} 不存在"
        # TODO 检查硬链接条件

        # TODO 检查网盘目录
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

    def list_files(self, fileitem: FileItem) -> Optional[List[FileItem]]:
        """
        浏览文件
        :param fileitem: 源文件
        :return: 文件列表
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件浏览")
            return None
        return storage_oper.list(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """
        创建目录
        :param fileitem: 源文件
        :param name: 目录名
        :return: 创建的目录
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的目录创建")
            return None
        return storage_oper.create_folder(fileitem, name)

    def delete_file(self, fileitem: FileItem) -> bool:
        """
        删除文件或目录
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的删除处理")
            return False
        return storage_oper.delete(fileitem)

    def rename_file(self, fileitem: FileItem, name: str) -> bool:
        """
        重命名文件或目录
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的重命名处理")
            return False
        return storage_oper.rename(fileitem, name)

    def download_file(self, fileitem: FileItem, path: Path) -> bool:
        """
        下载文件
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的下载处理")
            return False
        return storage_oper.download(fileitem, path)

    def upload_file(self, fileitem: FileItem, path: Path) -> bool:
        """
        上传文件
        """
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的上传处理")
            return False
        return storage_oper.upload(fileitem, path)

    def transfer(self, fileitem: FileItem, meta: MetaBase, mediainfo: MediaInfo,
                 transfer_type: str, target_storage: str = None, target_path: Path = None,
                 episodes_info: List[TmdbEpisode] = None,
                 scrape: bool = None) -> TransferInfo:
        """
        文件整理
        :param fileitem:  源文件
        :param meta: 预识别的元数据，仅单文件整理时传递
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  整理方式
        :param target_storage:  目标存储
        :param target_path:  目标路径
        :param episodes_info: 当前季的全部集信息
        :param scrape: 是否刮削元数据
        :return: {path, target_path, message}
        """
        # 目标路径不能是文件
        if target_path and target_path.is_file():
            logger.error(f"整理目标路径 {target_path} 是一个文件")
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message=f"{target_path} 不是有效目录")
        # 获取目标路径
        directoryhelper = DirectoryHelper()
        if target_path:
            dir_info = directoryhelper.get_dir(mediainfo, dest_path=target_path)
        else:
            dir_info = directoryhelper.get_dir(mediainfo)
        if dir_info:
            # 是否需要刮削
            if scrape is None:
                need_scrape = dir_info.scraping
            else:
                need_scrape = scrape
            # 是否需要重命名
            need_rename = dir_info.renaming
            # 拼装媒体库一、二级子目录
            target_path = self.__get_dest_dir(mediainfo=mediainfo, target_dir=dir_info)
        elif target_path:
            # 自定义目标路径
            need_scrape = scrape or False
            need_rename = True
        else:
            # 未找到有效的媒体库目录
            logger.error(
                f"{mediainfo.type.value} {mediainfo.title_year} 未找到有效的媒体库目录，无法整理文件，源路径：{fileitem.path}")
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message="未找到有效的媒体库目录")

        logger.info(f"获取整理目标路径：{target_path}")
        # 整理
        return self.transfer_media(fileitem=fileitem,
                                   in_meta=meta,
                                   mediainfo=mediainfo,
                                   transfer_type=transfer_type,
                                   target_storage=target_storage,
                                   target_path=target_path,
                                   episodes_info=episodes_info,
                                   need_scrape=need_scrape,
                                   need_rename=need_rename)

    def __get_storage_oper(self, _storage: str):
        """
        获取存储操作对象
        """
        for storage_schema in self._storage_schemas:
            if storage_schema.schema == _storage:
                return storage_schema()
        return None

    def __list_files(self, fileitem: FileItem):
        """
        浏览文件
        """
        pass

    def __transfer_command(self, fileitem: FileItem, target_storage: str,
                           target_file: Path, transfer_type: str) -> Tuple[Optional[FileItem], str]:
        """
        处理单个文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
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

        if fileitem.storage != "local" and target_storage != "local":
            logger.error(f"不支持 {fileitem.storage} 到 {target_storage} 的文件整理")
            return None, f"不支持 {fileitem.storage} 到 {target_storage} 的文件整理"

        # 源操作对象
        source_oper: StorageBase = self.__get_storage_oper(fileitem.storage)
        # 目的操作对象
        target_oper: StorageBase = self.__get_storage_oper(target_storage)
        if not source_oper or not target_oper:
            logger.error(f"不支持的存储类型：{fileitem.storage} 或 {target_storage}")
            return None, f"不支持的存储类型：{fileitem.storage} 或 {target_storage}"

        # 加锁
        with lock:
            if fileitem.storage == "local" and target_storage == "local":
                # 本地到本地
                if transfer_type == "copy":
                    state = source_oper.copy(fileitem, target_file)
                elif transfer_type == "move":
                    state = source_oper.move(fileitem, target_file)
                elif transfer_type == "link":
                    state = source_oper.link(fileitem, target_file)
                elif transfer_type == "softlink":
                    state = source_oper.softlink(fileitem, target_file)
                if state:
                    return __get_targetitem(target_file), ""
            elif fileitem.storage == "local" and target_storage != "local":
                # 本地到网盘
                filepath = Path(fileitem.path)
                if not filepath.exists():
                    logger.error(f"文件 {filepath} 不存在")
                    return None, f"文件 {filepath} 不存在"
                if transfer_type == "copy":
                    # 复制
                    # 根据目的路径创建文件夹
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        # 上传文件
                        new_item = target_oper.upload(target_fileitem, filepath)
                        if new_item:
                            return new_item, ""
                elif transfer_type == "move":
                    # 移动
                    # 根据目的路径获取文件夹
                    target_fileitem = target_oper.get_folder(target_file.parent)
                    if target_fileitem:
                        # 上传文件
                        new_item = target_oper.upload(target_fileitem, filepath)
                        if new_item:
                            # 删除源文件
                            source_oper.delete(fileitem)
                            return new_item, ""
            elif fileitem.storage != "local" and target_storage == "local":
                # 检查本地是否存在
                if target_file.exists():
                    logger.warn(f"文件已存在：{target_file}")
                    return __get_targetitem(target_file), ""
                # 网盘到本地
                if transfer_type == "copy":
                    # 下载
                    if target_oper.download(fileitem, target_file):
                        return __get_targetitem(target_file), ""
                elif transfer_type == "move":
                    # 下载
                    if target_oper.download(fileitem, target_file):
                        # 删除源文件
                        source_oper.delete(fileitem)
                        return __get_targetitem(target_file), ""

        return None, "不支持的整理操作"

    def __transfer_other_files(self, fileitem: FileItem, target_storage: str, target_file: Path,
                               transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理其他相关文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param target_file: 目标路径
        :param transfer_type: 整理方式
        """
        # 整理字幕
        state, errmsg = self.__transfer_subtitles(fileitem=fileitem,
                                                  target_storage=target_storage,
                                                  target_file=target_file,
                                                  transfer_type=transfer_type)
        if not state:
            return False, errmsg
        # 整理音轨文件
        state, errmsg = self.__transfer_audio_track_files(fileitem=fileitem,
                                                          target_storage=target_storage,
                                                          target_file=target_file,
                                                          transfer_type=transfer_type)

        return state, errmsg

    def __transfer_subtitles(self, fileitem: FileItem, target_storage: str, target_file: Path,
                             transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理对应字幕文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param target_file: 目标路径
        :param transfer_type: 整理方式
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

        # 比对文件名并整理字幕
        org_path = Path(fileitem.path)
        org_dir: Path = org_path.parent
        # 列出所有字幕文件
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件整理")
            return False, f"不支持的文件存储：{fileitem.storage}"
        file_list: List[FileItem] = storage_oper.list(fileitem)
        if len(file_list) == 0:
            logger.debug(f"{org_dir} 目录下没有找到字幕文件...")
        else:
            logger.debug("字幕文件清单：" + str(file_list))
            # 识别文件名
            metainfo = MetaInfoPath(org_path)
            for sub_item in file_list:
                if f".{sub_item.extension.lower()}" not in settings.RMT_SUBEXT:
                    continue
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
                        new_file_type if t == 0 else "%s%s(%s)" % (new_file_type,
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
                                                                       target_file=new_file,
                                                                       transfer_type=transfer_type)
                            if new_item:
                                logger.info(f"字幕 {sub_item.name} {transfer_type}完成")
                                break
                            else:
                                logger.error(f"字幕 {sub_item.name} {transfer_type}失败：{errmsg}")
                                return False, errmsg
                        except Exception as error:
                            logger.info(f"字幕 {new_file} 出错了,原因: {str(error)}")
        return False, ""

    def __transfer_audio_track_files(self, fileitem: FileItem, target_storage: str, target_file: Path,
                                     transfer_type: str) -> Tuple[bool, str]:
        """
        根据文件名整理对应音轨文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param target_file: 目标路径
        :param transfer_type: 整理方式
        """
        org_path = Path(fileitem.path)
        dir_name = org_path.parent
        file_name = org_path.name
        # 列出所有音轨文件
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件整理")
            return False, f"不支持的文件存储：{fileitem.storage}"
        file_list: List[FileItem] = storage_oper.list(fileitem)
        # 匹配音轨文件
        pending_file_list: List[FileItem] = [file for file in file_list if Path(file.name).stem == org_path.name
                                             and f".{file.extension.lower()}" in settings.RMT_AUDIOEXT]
        if len(pending_file_list) == 0:
            logger.debug(f"{dir_name} 目录下没有找到匹配的音轨文件")
            return True, f"{dir_name} 目录下没有找到匹配的音轨文件"
        logger.debug("音轨文件清单：" + str(pending_file_list))
        for track_file in pending_file_list:
            track_ext = f".{track_file.extension}"
            new_track_file = target_file.with_name(target_file.stem + track_ext)
            try:
                logger.info(f"正在整理音轨文件：{track_file} 到 {new_track_file}")
                new_item, errmsg = self.__transfer_command(fileitem=track_file,
                                                           target_storage=target_storage,
                                                           target_file=new_track_file,
                                                           transfer_type=transfer_type)
                if new_item:
                    logger.info(f"音轨文件 {file_name} {transfer_type}完成")
                else:
                    logger.error(f"音轨文件 {file_name} {transfer_type}失败：{errmsg}")
            except Exception as error:
                logger.error(f"音轨文件 {file_name} {transfer_type}失败：{str(error)}")
        return True, ""

    def __transfer_dir(self, fileitem: FileItem, transfer_type: str,
                       target_storage: str, target_path: Path) -> Tuple[Optional[FileItem], str]:
        """
        整理整个文件夹
        :param fileitem: 源文件
        :param transfer_type: 整理方式
        :param target_storage: 目标存储
        :param target_path: 目标路径
        """
        # 获取目标目录
        target_oper: StorageBase = self.__get_storage_oper(target_storage)
        if not target_oper:
            logger.error(f"不支持 {target_storage} 的文件整理")
            return None, f"不支持的文件存储：{target_storage}"

        logger.info(f"正在{transfer_type}目录：{fileitem.path} 到 {target_path}")
        target_item = target_oper.get_folder(target_path)
        if not target_item:
            logger.info(f"获取目标目录失败：{target_path}")
            return None, f"获取目标目录失败：{target_path}"
        # 处理所有文件
        new_item, errmsg = self.__transfer_dir_files(fileitem=fileitem,
                                                     target_storage=target_storage,
                                                     target_path=target_path,
                                                     transfer_type=transfer_type)
        if new_item:
            logger.info(f"文件 {fileitem.path} {transfer_type}完成")
        else:
            logger.error(f"文件{fileitem.path} {transfer_type}失败：{errmsg}")

        return target_item, errmsg

    def __transfer_dir_files(self, fileitem: FileItem, transfer_type: str,
                             target_storage: str, target_path: Path) -> Tuple[Optional[FileItem], str]:
        """
        按目录结构整理目录下所有文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param transfer_type: 整理方式
        """
        # 列出所有文件
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件整理")
            return None, f"不支持的文件存储：{fileitem.storage}"
        file_list: List[FileItem] = storage_oper.list(fileitem)
        # 整理文件
        for item in file_list:
            if item.type == "dir":
                # 递归整理目录
                new_path = target_path / item.name
                new_item, errmsg = self.__transfer_dir_files(fileitem=item,
                                                             transfer_type=transfer_type,
                                                             target_storage=target_storage,
                                                             target_path=new_path)
                if not new_item:
                    return None, errmsg
            else:
                # 整理文件
                new_file = target_path / item.name
                new_item, errmsg = self.__transfer_command(fileitem=item,
                                                           target_storage=target_storage,
                                                           target_file=new_file,
                                                           transfer_type=transfer_type)
                if not new_item:
                    return None, errmsg
        # 返回成功
        return FileItem(), ""

    def __transfer_file(self, fileitem: FileItem, target_storage: str, target_file: Path,
                        transfer_type: str, over_flag: bool = False) -> Tuple[Optional[FileItem], str]:
        """
        整理一个文件，同时处理其他相关文件
        :param fileitem: 原文件
        :param target_storage: 目标存储
        :param target_file: 新文件
        :param transfer_type: 整理方式
        :param over_flag: 是否覆盖，为True时会先删除再整理
        """
        if target_storage == "local" and (target_file.exists() or target_file.is_symlink()):
            if not over_flag:
                logger.warn(f"文件已存在：{target_file}")
                return None, f"{target_file} 已存在"
            else:
                logger.info(f"正在删除已存在的文件：{target_file}")
                target_file.unlink()
        logger.info(f"正在整理文件：{fileitem.path} 到 {target_file}")
        new_item, errmsg = self.__transfer_command(fileitem=fileitem,
                                                   target_storage=target_storage,
                                                   target_file=target_file,
                                                   transfer_type=transfer_type)
        if new_item:
            logger.info(f"文件 {fileitem.path} {transfer_type}完成")
            # 处理其他相关文件
            self.__transfer_other_files(fileitem=fileitem,
                                        target_storage=target_storage,
                                        target_file=target_file,
                                        transfer_type=transfer_type)
            return new_item, errmsg

        logger.error(f"文件 {fileitem.path} {transfer_type}失败：{errmsg}")
        return None, errmsg

    @staticmethod
    def __get_dest_dir(mediainfo: MediaInfo, target_dir: TransferDirectoryConf) -> Path:
        """
        根据设置并装媒体库目录
        :param mediainfo: 媒体信息
        :target_dir: 媒体库根目录
        :typename_dir: 是否加上类型目录
        """
        if not target_dir.media_type and target_dir.library_type_folder:
            # 一级自动分类
            library_dir = Path(target_dir.library_path) / mediainfo.type.value
        else:
            library_dir = Path(target_dir.library_path)

        if not target_dir.media_category and target_dir.library_category_folder and mediainfo.category:
            # 二级自动分类
            library_dir = library_dir / mediainfo.category

        return library_dir

    def transfer_media(self,
                       fileitem: FileItem,
                       in_meta: MetaBase,
                       mediainfo: MediaInfo,
                       transfer_type: str,
                       target_storage: str,
                       target_path: Path,
                       episodes_info: List[TmdbEpisode] = None,
                       need_scrape: bool = False,
                       need_rename: bool = True
                       ) -> TransferInfo:
        """
        识别并整理一个文件或者一个目录下的所有文件
        :param fileitem: 整理的文件对象，可能是一个文件也可以是一个目录
        :param in_meta：预识别元数据
        :param mediainfo: 媒体信息
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param transfer_type: 文件整理方式
        :param episodes_info: 当前季的全部集信息
        :param need_scrape: 是否需要刮削
        :param need_rename: 是否需要重命名
        :return: TransferInfo、错误信息
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

        # 检查目录路径
        if fileitem.storage == "local" and not Path(fileitem.path).exists():
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message=f"{fileitem.path} 不存在")

        if target_storage == "local":
            # 检查目标路径
            if not target_path.exists():
                logger.info(f"目标路径不存在，正在创建：{target_path} ...")
                target_path.mkdir(parents=True, exist_ok=True)

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
                    rename_dict=self.__get_naming_dict(meta=in_meta,
                                                       mediainfo=mediainfo)
                ).parent
            else:
                new_path = target_path / fileitem.name
            # 整理目录
            new_item, errmsg = self.__transfer_dir(fileitem=fileitem,
                                                   target_storage=target_storage,
                                                   target_path=new_path,
                                                   transfer_type=transfer_type)
            if not new_item:
                logger.error(f"文件夹 {fileitem.path} 整理失败：{errmsg}")
                return TransferInfo(success=False,
                                    message=errmsg,
                                    fileitem=fileitem,
                                    target_path=new_path)

            logger.info(f"文件夹 {fileitem.path} 整理成功")
            # 返回整理后的路径
            return TransferInfo(success=True,
                                fileitem=fileitem,
                                target_fileitem=new_item,
                                total_size=fileitem.size,
                                need_scrape=need_scrape)
        else:
            # 整理单个文件
            if mediainfo.type == MediaType.TV:
                # 电视剧
                if in_meta.begin_episode is None:
                    logger.warn(f"文件 {fileitem.path} 整理失败：未识别到文件集数")
                    return TransferInfo(success=False,
                                        message=f"未识别到文件集数",
                                        fileitem=fileitem,
                                        fail_list=[fileitem.path])

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
                    rename_dict=self.__get_naming_dict(
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
            if target_storage == "local":
                # 本地目标存储
                if new_file.exists() or new_file.is_symlink():
                    # 本地目标文件已存在
                    target_file = new_file
                    if new_file.is_symlink():
                        target_file = new_file.readlink()
                        if not target_file.exists():
                            overflag = True
                    if not overflag:
                        # 目标文件已存在
                        logger.info(f"目标文件已存在，整理覆盖模式：{settings.OVERWRITE_MODE}")
                        match settings.OVERWRITE_MODE:
                            case 'always':
                                # 总是覆盖同名文件
                                overflag = True
                            case 'size':
                                # 存在时大覆盖小
                                if target_file.stat().st_size < fileitem.size:
                                    logger.info(f"目标文件文件大小更小，将覆盖：{new_file}")
                                    overflag = True
                                else:
                                    return TransferInfo(success=False,
                                                        message=f"媒体库中已存在，且质量更好",
                                                        fileitem=fileitem,
                                                        target_fileitem=__get_targetitem(target_file),
                                                        fail_list=[fileitem.path])
                            case 'never':
                                # 存在不覆盖
                                return TransferInfo(success=False,
                                                    message=f"媒体库中已存在，当前设置为不覆盖",
                                                    fileitem=fileitem,
                                                    target_fileitem=__get_targetitem(target_file),
                                                    fail_list=[fileitem.path])
                            case 'latest':
                                # 仅保留最新版本
                                logger.info(f"仅保留最新版本，将覆盖：{new_file}")
                                overflag = True
                else:
                    # FIXME
                    if settings.OVERWRITE_MODE == 'latest':
                        # 文件不存在，但仅保留最新版本
                        logger.info(f"整理覆盖模式：{settings.OVERWRITE_MODE}，仅保留最新版本")
                        self.delete_all_version_files(new_file)
            # 整理文件
            new_item, err_msg = self.__transfer_file(fileitem=fileitem,
                                                     target_storage=target_storage,
                                                     target_file=new_file,
                                                     transfer_type=transfer_type,
                                                     over_flag=overflag)
            if not new_item:
                logger.error(f"文件 {fileitem.path} 整理失败：{err_msg}")
                return TransferInfo(success=False,
                                    message=err_msg,
                                    fileitem=fileitem,
                                    fail_list=[fileitem.path])

            logger.info(f"文件 {fileitem.path} 整理成功")
            return TransferInfo(success=True,
                                fileitem=fileitem,
                                target_item=new_item,
                                file_count=1,
                                total_size=fileitem.size,
                                file_list=[fileitem.path],
                                file_list_new=[new_item.path],
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
