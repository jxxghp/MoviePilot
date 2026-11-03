"""
按影视重命名模板生成下载媒体目录，并保存有限的命名事实快照。
"""

from collections.abc import Mapping
from typing import Optional, Union

from jinja2 import Environment, meta

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.messaging.message import TemplateContextBuilder
from app.domain.context import MediaInfo
from app.domain.mediafolder import media_folder_template_index, validate_media_folder_name
from app.schemas.types import MediaType

# 只允许与版本和季集无关的媒体标识，快照不包含原始对象或外部链接。
MEDIA_FOLDER_FIELDS = frozenset({
    "title", "en_title", "original_title", "year", "title_year",
    "tmdbid", "imdbid", "doubanid", "bangumiid", "anilistid", "media_source", "media_id",
})
# 限制历史中的单项命名事实，异常元数据不能使备注无界增长。
_MAX_CONTEXT_VALUE_LENGTH = 2048
MediaFolderValue = Optional[Union[str, int]]


def media_folder_context(media: MediaInfo) -> dict[str, MediaFolderValue]:
    """
    从已有模板构建器投影稳定命名字段，以 None 保存已知缺失事实。

    :param media: 已识别的电影或剧集信息
    :return: 可独立序列化的有界标量快照
    :raises ValueError: 媒体命名字段不是可安全保存的有限标量
    """
    try:
        context = TemplateContextBuilder().build(mediainfo=media, include_raw_objects=False)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("媒体目录命名事实无效") from error
    return validate_media_folder_context({key: context.get(key) for key in MEDIA_FOLDER_FIELDS})


def validate_media_folder_context(value: object) -> dict[str, MediaFolderValue]:
    """
    校验命名快照，保留字段缺失与已知空值的区别。

    :param value: 从媒体事实或历史备注取得的命名字段
    :return: 独立的允许字段字典
    :raises ValueError: 快照包含未知字段、非标量或过长内容
    """
    if not isinstance(value, dict) or not value.keys() <= MEDIA_FOLDER_FIELDS:
        raise ValueError("媒体目录命名快照无效")
    result: dict[str, MediaFolderValue] = {}
    for key, item in value.items():
        if item is None:
            result[key] = None
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int)) or len(str(item)) > _MAX_CONTEXT_VALUE_LENGTH:
            raise ValueError(f"媒体目录命名字段 {key} 无效")
        result[key] = item
    return result


def render_media_folder(
    media: MediaInfo,
    context: Optional[Mapping[str, MediaFolderValue]] = None,
) -> str:
    """
    仅用稳定媒体事实渲染媒体库模板中的媒体目录段。

    :param media: 决定电影或电视剧模板的媒体信息
    :param context: 历史中已冻结的命名字段；None 时使用当前完整媒体事实
    :return: 经校验的单层媒体目录名
    :raises ValueError: 模板无法定位媒体目录、引用不可用字段或渲染结果不安全
    """
    media_type = media.type
    if media_type is None or media_type not in {MediaType.MOVIE, MediaType.TV}:
        raise ValueError("媒体目录仅支持电影和电视剧")
    rename_format = get_chain_runtime_config_snapshot().rename_format(media_type)
    index = media_folder_template_index(rename_format)
    if index is None:
        raise ValueError("媒体目录模板缺少独立的标题目录，请检查影视重命名格式")
    template = rename_format.split("/")[index]
    values = media_folder_context(media) if context is None else validate_media_folder_context(dict(context))
    environment = Environment(autoescape=False)
    environment.globals.clear()
    try:
        required = meta.find_undeclared_variables(environment.parse(template))
        unsupported = required - MEDIA_FOLDER_FIELDS
        if unsupported:
            raise ValueError(f"媒体目录模板不支持这些变量：{', '.join(sorted(unsupported))}")
        missing = required - values.keys()
        if missing:
            raise ValueError(f"下载历史缺少媒体目录命名信息：{', '.join(sorted(missing))}")
        # 构建器省略缺失字段；保持 is defined、default 和直接输出的原有 Jinja 语义。
        result = environment.from_string(template).render({key: value for key, value in values.items() if value is not None})
    except Exception as error:
        raise ValueError(f"媒体目录模板无法渲染：{error}") from error
    return validate_media_folder_name(result)
