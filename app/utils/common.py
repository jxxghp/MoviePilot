import re
import time
from typing import Any

from app.schemas import ImmediateException, VersionComparisonException
from version import APP_VERSION


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 2, logger: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except ImmediateException:
                    raise
                except ExceptionToCheck as e:
                    msg = f"{str(e)}, {mdelay} 秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        return f_retry

    return deco_retry


def version_comparison(standard_version: str = APP_VERSION, app_version: str = APP_VERSION, mode: str = "min",
                       logger: Any = None):
    """
    版本比对
    :param standard_version: 标准版本号
    :param app_version: 当前版本号
    :param mode: 兼容识别最低版本，最高版本使用。可选值：max = 最高版本；min = 最低版本； equal = 匹配指定版本；默认为最低版本
    :param logger: 日志对象
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            try:
                def preprocess_version(version):
                    """
                    预处理版本号，去除首尾空字符串与换行符，去除开头大小写v，并拆分版本号
                    """
                    version = version.strip().lstrip('vV')
                    return re.split(r'[.-]', version)

                def conversion_version(version_list):
                    """
                    英文字符转换为数字，stable=-1，rc=-2，beta=-3，alpha=-4，其余不符合的都为-5
                    """
                    version_map = {"stable": -1, "rc": -2, "beta": -3, "alpha": -4}
                    return [int(item) if item.isdigit() else version_map.get(item, -5) for item in version_list]

                app_version_list = conversion_version(preprocess_version(version=app_version))
                standard_version_list = conversion_version(preprocess_version(version=standard_version))

                # 补全版本号位置，保持长度一致
                max_length = max(len(app_version_list), len(standard_version_list))
                app_version_list += [0] * (max_length - len(app_version_list))
                standard_version_list += [0] * (max_length - len(standard_version_list))

                for a, s in zip(app_version_list, standard_version_list):
                    # 最低版本
                    if mode == "min" and a < s:
                        raise VersionComparisonException(f"当前版本 {app_version} 小于标准版本 {standard_version}！")
                    # 最高版本
                    elif mode == "max" and a > s:
                        raise VersionComparisonException(f"当前版本 {app_version} 大于标准版本 {standard_version}！")
                    # 匹配版本
                    elif mode == "equal" and a != s:
                        raise VersionComparisonException(
                            f"当前版本 {app_version} 与指定标准版本 {standard_version} 不匹配！")
                    # 异常的模式
                    else:
                        raise VersionComparisonException(f"设置的版本比对模式 {mode} 不是有效的模式！")
                # 完成版本比对，允许执行函数
                return f(*args, **kwargs)

            except VersionComparisonException as e:
                logger.error(e) if logger else print(e)
                raise
            except Exception as e:
                msg = f"版本比对失败 - {str(e)}"
                logger.error(msg, exc_info=True) if logger else print(msg)
                raise

        return f_retry

    return deco_retry
