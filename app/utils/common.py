import re
import time
from typing import Any, Optional

from app.log import logger
from app.schemas import ImmediateException
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


def version_comparison(standard_version) -> Optional[bool]:
    """
    版本比对
    :param
    return: True - 当前版本大于等于输入版本，False - 当前版本输入标准版本, None - 版本比对失败

    - 可用于给插件或主程序进行版本比对使用，判断是否使用新特性，用于兼容旧版本主程序
    """
    try:
        app_version = APP_VERSION

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
            if a > s:
                return True
            elif a < s:
                return False
        # 版本号相同
        return True
    except Exception as e:
        logger.error(f"版本比对失败 - {str(e)}", exc_info=True)
        return None
