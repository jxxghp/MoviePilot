from typing import Optional, Tuple, Union

from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.thetvdb import tvdb_v4_official
from app.schemas.types import ModuleType, MediaRecognizeType


class TheTvDbModule(_ModuleBase):
    tvdb: tvdb_v4_official.TVDB = None

    def init_module(self) -> None:
        self._initialize_tvdb_session()

    def _initialize_tvdb_session(self) -> None:
        """
        创建或刷新 TVDB 登录会话
        """
        try:
            self.tvdb = tvdb_v4_official.TVDB(apikey=settings.TVDB_V4_API_KEY, pin=settings.TVDB_V4_API_PIN)
        except Exception as e:
            logger.error(f"TVDB 登录失败: {str(e)}")

    def _handle_tvdb_call(self, func, *args, **kwargs):
        """
        包裹 TVDB 调用，处理 token 失效情况并尝试重新初始化
        """
        try:
            return func(*args, **kwargs)
        except ValueError as e:
            # 检查错误信息中是否包含 token 失效相关描述
            if "Unauthorized" in str(e):
                logger.warning("TVDB Token 可能已失效，正在尝试重新登录...")
                self._initialize_tvdb_session()
                return func(*args, **kwargs)
            elif "NotFoundException" in str(e):
                logger.warning("TVDB 剧集不存在")
                return None
            else:
                raise
        except Exception as e:
            logger.error(f"TVDB 调用出错: {str(e)}")
            raise

    @staticmethod
    def get_name() -> str:
        return "TheTvDb"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型
        """
        return MediaRecognizeType.TVDB

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        try:
            self._handle_tvdb_call(self.tvdb.get_series, 81189)
            return True, ""
        except Exception as e:
            return False, str(e)

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        try:
            logger.info(f"开始获取TVDB信息: {tvdbid} ...")
            return self._handle_tvdb_call(self.tvdb.get_series_extended, tvdbid)
        except Exception as err:
            logger.error(f"获取TVDB信息失败: {str(err)}")
            return None

    def search_tvdb(self, title: str) -> list:
        """
        用标题搜索TVDB剧集
        :param title: 标题
        :return: TVDB信息
        """
        try:
            logger.info(f"开始用标题搜索TVDB剧集: {title} ...")
            res = self._handle_tvdb_call(self.tvdb.search, title)
            return [item for item in res if item.get("type") == "series"]
        except Exception as err:
            logger.error(f"用标题搜索TVDB剧集失败: {str(err)}")
            return []
