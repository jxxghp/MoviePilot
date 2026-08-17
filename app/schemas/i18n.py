"""传输模型使用的文本本地化注入点。

pydantic 校验器需要把展示文本翻译为当前请求语言，但传输模型不应反向依赖运行时的
本地化实现。组合根在启动时通过 :func:`configure_translator` 注入真实翻译函数；
未注入前 :func:`translate` 原样返回文本，使 schemas 可以在组合根装配之前被导入。
"""
from typing import Callable, Optional


_translator: Optional[Callable[[str], str]] = None


def configure_translator(translator: Callable[[str], str]) -> None:
    """注入文本翻译函数，由组合根在启动时调用。

    :param translator: 接收原始文本、返回按当前请求语言翻译后文本的函数
    :return: 无
    """
    global _translator
    _translator = translator


def translate(text: str) -> str:
    """按已注入的翻译函数本地化文本，未注入时原样返回。

    :param text: 待翻译的原始文本
    :return: 翻译后的文本；未注册翻译函数时返回原文本
    """
    if _translator is None:
        return text
    return _translator(text)
