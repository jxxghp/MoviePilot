from pathlib import Path
from typing import Optional, Set


class SecurityUtils:

    @staticmethod
    def is_safe_path(base_path: Path, user_path: Path, allowed_suffixes: Optional[Set[str]] = None) -> bool:
        """
        验证用户提供的路径是否在基准目录内，并检查文件类型是否合法，防止目录遍历攻击

        :param base_path: 基准目录，允许访问的根目录
        :param user_path: 用户提供的路径，需检查其是否位于基准目录内
        :param allowed_suffixes: 允许的文件后缀名集合，用于验证文件类型
        :return: 如果用户路径安全且位于基准目录内，且文件类型合法，返回 True；否则返回 False
        :raises Exception: 如果解析路径时发生错误，则捕获并记录异常
        """
        try:
            # resolve() 将相对路径转换为绝对路径，并处理符号链接和'..'
            base_path_resolved = base_path.resolve()
            user_path_resolved = user_path.resolve()

            # 检查用户路径是否在基准目录或基准目录的子目录内
            if base_path_resolved != user_path_resolved and base_path_resolved not in user_path_resolved.parents:
                return False

            # 如果指定了 allowed_suffixes，进一步检查文件后缀
            if allowed_suffixes and user_path.is_file() and user_path.suffix not in allowed_suffixes:
                return False

            # 所有检查通过
            return True
        except Exception as e:
            # 捕获并记录路径解析时的异常
            print(f"Error occurred while resolving paths: {e}")
            return False
