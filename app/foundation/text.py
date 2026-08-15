"""无业务状态的文本识别、清理、转换和分段能力。"""

import random
import re
from typing import Generator, List, Optional, Union

from jieba_next import cut as jieba_next_cut
from zhconv_rs import zhconv as _zhconv  # pylint: disable=no-name-in-module


def cut(text: str, HMM: bool = True, cut_all: bool = False) -> list[str]:
    """
    使用 jieba-next 执行中文分词，并兼容 jieba.cut 的常用参数名。
    """
    return list(jieba_next_cut(text, HMM=HMM, cut_all=cut_all))


def convert(text: str, target: str) -> str:
    """使用 zhconv-rs 执行中文简繁转换，并隔离第三方包的函数名差异。"""
    return _zhconv(text, target)


def contains_chinese(value: Union[str, list]) -> bool:
    """判断文本或文本列表中是否包含中文字符。"""
    if not value:
        return False
    if isinstance(value, list):
        value = " ".join(value)
    return re.search(r"[\u4e00-\u9fff]", value) is not None


def contains_japanese(value: str) -> bool:
    """判断文本中是否包含平假名或片假名。"""
    return re.search(r"[\u3040-\u309F\u30A0-\u30FF]", value) is not None


def contains_korean(value: str) -> bool:
    """判断文本中是否包含韩文字符。"""
    return re.search(r"[\uAC00-\uD7FF]", value) is not None


def is_all_chinese(value: str) -> bool:
    """判断除空格外的全部字符是否都是中文。"""
    return all(character == " " or "\u4e00" <= character <= "\u9fff" for character in value)


def is_english_word(value: str) -> bool:
    """判断文本是否为不含空格的英文字母单词。"""
    return value.encode().isalpha()


def parse_int(value: str) -> int:
    """解析可能带千位分隔符的整数，无法解析时返回零。"""
    if value:
        value = value.strip()
    if not value:
        return 0
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return 0


def parse_float(value: str) -> float:
    """解析可能带千位分隔符的浮点数，无法解析时返回零。"""
    if value:
        value = value.strip()
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def remove_punctuation(
    value: Union[list, str],
    replacement: str = "",
    allow_space: bool = False,
) -> Union[list, str]:
    """移除历史匹配规则使用的标点和零宽字符。"""
    punctuation = r"[、.。,，·:：;；!！?？'’\"“”()（）\[\]【】「」\-—―\+\|\\_/&#～~]"
    if not value:
        return value
    if isinstance(value, list):
        return [remove_punctuation(item) for item in value]
    normalized = re.sub(
        r"[\u200B-\u200D\uFEFF]",
        "",
        re.sub(punctuation, replacement, value, flags=re.IGNORECASE),
        flags=re.IGNORECASE,
    )
    if not allow_space:
        return re.sub(r"\s+", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_upper(value: Optional[str]) -> str:
    """移除历史匹配标点、空白并转换为大写。"""
    if not value:
        return ""
    return remove_punctuation(value).upper().strip()


def sanitize_filename(value: str) -> Optional[str]:
    """移除文件名中不允许使用的字符并替换英文冒号。"""
    if not value:
        return None
    return re.sub(r"[*?\\/\"<>~|]", "", value, flags=re.IGNORECASE).replace(":", "：")


def random_string(length: int = 16) -> str:
    """生成兼容历史字符集的指定长度随机字符串。"""
    alphabet = "ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789"
    last_index = len(alphabet) - 1
    return "".join(alphabet[random.randint(0, last_index)] for _index in range(length))


def parse_bool(value, default: bool = False) -> bool:
    """按历史配置规则把字符串或数值转换为布尔值。"""
    if isinstance(value, str) and not value:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return isinstance(value, str) and value.lower() in {"y", "true", "1", "yes", "on"}


def cookiejar_to_string(cookiejar: dict) -> str:
    """把键值形式的 CookieJar 序列化为 Cookie 请求头文本。"""
    return "; ".join("=".join(item) for item in cookiejar.items())


def extract_named_ids(content: str, entries: List[dict]):
    """从空格分隔文本中提取命名条目 ID，并返回剩余内容。"""
    if not content:
        return []
    identifiers = []
    content_parts = content.split()
    for entry in entries:
        if entry.get("name") in content_parts and entry.get("id") not in identifiers:
            identifiers.append(entry.get("id"))
            content = content.replace(entry.get("name"), "")
    return identifiers, re.sub(r"\s+", " ", content).strip()


def format_amount(amount: object, currency: str = "$") -> str:
    """使用千位分隔符和货币前缀格式化金额。"""
    if not amount:
        return "0"
    return currency + format(amount, ",")


def count_words(value: str) -> int:
    """统计中英文混合文本中的汉字数和英文单词数。"""
    if not value:
        return 0
    chinese_words = [
        word for word in re.findall(r"[\u4e00-\u9fa5]", value) if word.isalpha()
    ]
    english_words = [word for word in re.findall(r"[a-zA-Z]+", value) if word.isalpha()]
    return len(chinese_words) + len(english_words)


def split_by_bytes(value: str, max_length: int) -> Generator[str, None, None]:
    """按 UTF-8 字节上限分段，优先保持换行和英文单词完整。"""
    if not value:
        yield ""
    lines = re.split("\n", value)
    buffer = ""
    for line in lines:
        if len(line.encode("utf-8")) > max_length:
            separator = ""
            if re.match(r"^[A-Za-z0-9.\s]+", line):
                parts = line.split()
                separator = " "
            else:
                parts = line
            part = ""
            for item in parts:
                if len((part + item).encode("utf-8")) > max_length:
                    yield (buffer + part).strip()
                    buffer = ""
                    part = f"{separator}{item}"
                else:
                    part = f"{part}{separator}{item}"
            if part:
                buffer += part
        elif len((buffer + "\n" + line).encode("utf-8")) > max_length:
            yield buffer.strip()
            buffer = line
        elif buffer:
            buffer = f"{buffer}\n{line}"
        else:
            buffer = line
    if buffer:
        yield buffer.strip()


def title_case(value: Optional[str]) -> str:
    """转换为标题大小写，并兼容空值。"""
    return value.title() if value else value


def escape_markdown(value: str) -> str:
    """转义 Markdown 保留字符，并保持历史二次转义语义。"""
    escaped = re.sub(r"([_*\[\]()~`>#+\-=|.!{}])", r"\\\1", value)
    return re.sub(r"\\\\([_*\[\]()~`>#+\-=|.!{}])", r"\1", escaped)


def is_number(value: str) -> bool:
    """判断文本能否转换为整数或浮点数。"""
    if not value:
        return False
    try:
        float(value)
        return True
    except ValueError:
        return False


def common_prefix(first: str, second: str) -> str:
    """返回两个字符串从首字符开始的公共前缀。"""
    if not first or not second:
        return ""
    prefix = []
    for first_character, second_character in zip(first, second):
        if first_character != second_character:
            break
        prefix.append(first_character)
    return "".join(prefix)


def strip_optional(value) -> Optional[str]:
    """去除可空值两端空白，并保持 None。"""
    return value.strip() if value is not None else None


def natural_sort_key(value: str) -> List[Union[int, str]]:
    """把文本拆成数字和小写文本片段，供自然排序使用。"""
    if value is None:
        return []
    if not isinstance(value, str):
        value = str(value)
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]
