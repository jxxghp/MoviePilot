import re
from version import APP_VERSION


class VersionUtils:
    """
    版本处理工具
    """
    # 内置版本号转换字典
    version_map = {"stable": -1, "rc": -2, "beta": -3, "alpha": -4}
    # 不符合的版本号
    other = -5

    @staticmethod
    def preprocess_version(version: str) -> list:
        """
        预处理版本号，去除首尾空字符串与换行符，去除开头大小写v，并拆分版本号
        """
        return re.split(r'[.-]', version.strip().lstrip('vV'))

    def conversion_version(self, version_list) -> list:
        """
        英文字符转换为数字

        stable = -1，rc = -2，beta = -3，alpha = -4
        其余不符合的，都为-5
        """
        result = []
        for item in version_list:
            if item.isdigit():
                result.append(int(item))
            else:
                value = self.version_map.get(item, self.other)
                result.append(value)
        return result

    def version_comparison(self, input_ver: str = None, targe_ver: str = None, verbose: bool = True, logger: any = None,
                           compare_type: str = None) -> tuple[bool, str] | bool | tuple[None, Exception] | None:
        """
        版本比对

        :param targe_ver: 目标版本号
        :param input_ver: 输入版本号
        :param verbose: 是否输出比对结果的时候输出详细消息
        :param compare_type: 识别模式。支持直接使用符号进行比对。
        'ge' or '>=' ：输入 >= 目标；
        'le' or '<=' ：输入 <= 目标；
        'eq' or '==' ：输入 == 目标。
        'gt' or '>'  ：输入 > 目标；
        'lt' or '<'  ：输入 < 目标。
        :param logger: 日志对象
        :return:
        """
        try:
            if not input_ver:
                not_input_ver_msg = "输入版本号为空，默认使用当前项目后端版本号！"
                input_ver = APP_VERSION
                logger.warning(not_input_ver_msg) if logger else print(not_input_ver_msg)

            if not targe_ver:
                not_targe_ver_msg = "目标版本号为空，默认使用当前项目后端版本号！"
                targe_ver = APP_VERSION
                logger.warning(not_targe_ver_msg) if logger else print(not_targe_ver_msg)

            if not compare_type:
                not_compare_type_msg = "未设置版本比对模式，默认使用 '==' 模式！"
                compare_type = "=="
                logger.warning(not_compare_type_msg) if logger else print(not_compare_type_msg)

            if compare_type not in {"ge", "gt", "le", "lt", "eq", "==", ">=", ">", "<=", "<"}:
                raise ValueError(f"设置的版本比对模式 {compare_type} 不是有效的模式！")

            # 拆分获取版本号各个分段值做成列表
            input_ver_list = self.conversion_version(self.preprocess_version(version=input_ver))
            targe_ver_list = self.conversion_version(self.preprocess_version(version=targe_ver))

            # 补全版本号位置，保持长度一致
            max_length = max(len(input_ver_list), len(targe_ver_list))
            input_ver_list += [0] * (max_length - len(input_ver_list))
            targe_ver_list += [0] * (max_length - len(targe_ver_list))

            ver_comparison, ver_comparison_err = None, None
            for i, t in zip(input_ver_list, targe_ver_list):
                # 输入==目标
                if compare_type in {"eq", "=="}:
                    if i != t:
                        ver_comparison, ver_comparison_err = None, "不等于"
                        break
                    else:
                        ver_comparison, ver_comparison_err = "等于", None

                # 输入>=目标
                elif compare_type in {"ge", ">="}:
                    if i > t:
                        ver_comparison, ver_comparison_err = "大于", None
                        break
                    elif i < t:
                        ver_comparison, ver_comparison_err = None, "小于"
                        break
                    else:
                        ver_comparison, ver_comparison_err = "等于", None

                # 输入>目标
                elif compare_type in {"gt", ">"}:
                    if i > t:
                        ver_comparison, ver_comparison_err = "大于", None
                        break
                    elif i < t:
                        ver_comparison, ver_comparison_err = None, "小于"
                        break
                    else:
                        ver_comparison, ver_comparison_err = None, "等于"

                # 输入<=目标
                elif compare_type in {"le", "<="}:
                    if i > t:
                        ver_comparison, ver_comparison_err = None, "大于"
                        break
                    elif i < t:
                        ver_comparison, ver_comparison_err = "小于", None
                        break
                    else:
                        ver_comparison, ver_comparison_err = "等于", None

                # 输入<目标
                elif compare_type in {"lt", "<"}:
                    if i > t:
                        ver_comparison, ver_comparison_err = None, "大于"
                        break
                    elif i < t:
                        ver_comparison, ver_comparison_err = "小于", None
                        break
                    else:
                        ver_comparison, ver_comparison_err = None, "等于"

            msg = f"输入版本号 {input_ver} {ver_comparison if ver_comparison else ver_comparison_err} 目标版本号 {targe_ver} ！"

            # 是否需要返回详情消息
            if verbose:
                return True if ver_comparison else False, msg
            else:
                return True if ver_comparison else False

        except Exception as e:
            msg = f"版本比对失败 - {str(e)}"
            logger.error(msg, exc_info=True) if logger else print(msg)
            if verbose:
                return None, e
            else:
                return None

