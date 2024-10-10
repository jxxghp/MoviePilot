import bisect
import datetime
import hashlib
import random
import re
from typing import Union, Tuple, Optional, Any, List, Generator
from urllib import parse

import cn2an
import dateparser
import dateutil.parser

from app.schemas.types import MediaType


_special_domains = [
    'u2.dmhy.org',
    'pt.ecust.pp.ua',
]


class StringUtils:

    @staticmethod
    def num_filesize(text: Union[str, int, float]) -> int:
        """
        将文件大小文本转化为字节
        """
        if not text:
            return 0
        if not isinstance(text, str):
            text = str(text)
        if text.isdigit():
            return int(text)
        text = text.replace(",", "").replace(" ", "").upper()
        size = re.sub(r"[KMGTPI]*B?", "", text, flags=re.IGNORECASE)
        try:
            size = float(size)
        except ValueError:
            return 0
        if text.find("PB") != -1 or text.find("PIB") != -1:
            size *= 1024 ** 5
        elif text.find("TB") != -1 or text.find("TIB") != -1:
            size *= 1024 ** 4
        elif text.find("GB") != -1 or text.find("GIB") != -1:
            size *= 1024 ** 3
        elif text.find("MB") != -1 or text.find("MIB") != -1:
            size *= 1024 ** 2
        elif text.find("KB") != -1 or text.find("KIB") != -1:
            size *= 1024
        return round(size)

    @staticmethod
    def str_timelong(time_sec: Union[str, int, float]) -> str:
        """
        将数字转换为时间描述
        """
        if not isinstance(time_sec, int) or not isinstance(time_sec, float):
            try:
                time_sec = float(time_sec)
            except ValueError:
                return ""
        d = [(0, '秒'), (60 - 1, '分'), (3600 - 1, '小时'), (86400 - 1, '天')]
        s = [x[0] for x in d]
        index = bisect.bisect_left(s, time_sec) - 1
        if index == -1:
            return str(time_sec)
        else:
            b, u = d[index]
        return str(round(time_sec / (b + 1))) + u

    @staticmethod
    def str_secends(time_sec: Union[str, int, float]) -> str:
        """
        将秒转为时分秒字符串
        """
        hours = time_sec // 3600
        remainder_seconds = time_sec % 3600
        minutes = remainder_seconds // 60
        seconds = remainder_seconds % 60

        time: str = str(int(seconds)) + '秒'
        if minutes:
            time = str(int(minutes)) + '分' + time
        if hours:
            time = str(int(hours)) + '时' + time
        return time

    @staticmethod
    def is_chinese(word: Union[str, list]) -> bool:
        """
        判断是否含有中文
        """
        if not word:
            return False
        if isinstance(word, list):
            word = " ".join(word)
        chn = re.compile(r'[\u4e00-\u9fff]')
        if chn.search(word):
            return True
        else:
            return False

    @staticmethod
    def is_japanese(word: str) -> bool:
        """
        判断是否含有日文
        """
        jap = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
        if jap.search(word):
            return True
        else:
            return False

    @staticmethod
    def is_korean(word: str) -> bool:
        """
        判断是否包含韩文
        """
        kor = re.compile(r'[\uAC00-\uD7FF]')
        if kor.search(word):
            return True
        else:
            return False

    @staticmethod
    def is_all_chinese(word: str) -> bool:
        """
        判断是否全是中文
        """
        for ch in word:
            if ch == ' ':
                continue
            if '\u4e00' <= ch <= '\u9fff':
                continue
            else:
                return False
        return True

    @staticmethod
    def is_english_word(word: str) -> bool:
        """
        判断是否为英文单词，有空格时返回False
        """
        return word.encode().isalpha()

    @staticmethod
    def str_int(text: str) -> int:
        """
        web字符串转int
        :param text:
        :return:
        """
        if text:
            text = text.strip()
        if not text:
            return 0
        try:
            return int(text.replace(',', ''))
        except ValueError:
            return 0

    @staticmethod
    def str_float(text: str) -> float:
        """
        web字符串转float
        :param text:
        :return:
        """
        if text:
            text = text.strip()
        if not text:
            return 0.0
        try:
            text = text.replace(',', '')
            if text:
                return float(text)
        except ValueError:
            pass
        return 0.0

    @staticmethod
    def clear(text: Union[list, str], replace_word: str = "",
              allow_space: bool = False) -> Union[list, str]:
        """
        忽略特殊字符
        """
        # 需要忽略的特殊字符
        CONVERT_EMPTY_CHARS = r"[、.。,，·:：;；!！'’\"“”()（）\[\]【】「」\-—―\+\|\\_/&#～~]"
        if not text:
            return text
        if not isinstance(text, list):
            text = re.sub(r"[\u200B-\u200D\uFEFF]",
                          "",
                          re.sub(r"%s" % CONVERT_EMPTY_CHARS, replace_word, text),
                          flags=re.IGNORECASE)
            if not allow_space:
                return re.sub(r"\s+", "", text)
            else:
                return re.sub(r"\s+", " ", text).strip()
        else:
            return [StringUtils.clear(x) for x in text]

    @staticmethod
    def clear_upper(text: str) -> str:
        """
        去除特殊字符，同时大写
        """
        if not text:
            return ""
        return StringUtils.clear(text).upper().strip()

    @staticmethod
    def str_filesize(size: Union[str, float, int], pre: int = 2) -> str:
        """
        将字节计算为文件大小描述（带单位的格式化后返回）
        """
        if size is None:
            return ""
        size = re.sub(r"\s|B|iB", "", str(size), re.I)
        if size.replace(".", "").isdigit():
            try:
                size = float(size)
                d = [(1024 - 1, 'K'), (1024 ** 2 - 1, 'M'), (1024 ** 3 - 1, 'G'), (1024 ** 4 - 1, 'T')]
                s = [x[0] for x in d]
                index = bisect.bisect_left(s, size) - 1
                if index == -1:
                    return str(size) + "B"
                else:
                    b, u = d[index]
                return str(round(size / (b + 1), pre)) + u
            except ValueError:
                return ""
        if re.findall(r"[KMGTP]", size, re.I):
            return size
        else:
            return size + "B"

    @staticmethod
    def url_equal(url1: str, url2: str) -> bool:
        """
        比较两个地址是否为同一个网站
        """
        if not url1 or not url2:
            return False
        if url1.startswith("http"):
            url1 = parse.urlparse(url1).netloc
        if url2.startswith("http"):
            url2 = parse.urlparse(url2).netloc
        if url1.replace("www.", "") == url2.replace("www.", ""):
            return True
        return False

    @staticmethod
    def get_url_netloc(url: str) -> Tuple[str, str]:
        """
        获取URL的协议和域名部分
        """
        if not url:
            return "", ""
        if not url.startswith("http"):
            return "http", url
        addr = parse.urlparse(url)
        return addr.scheme, addr.netloc

    @staticmethod
    def get_url_domain(url: str) -> str:
        """
        获取URL的域名部分，只保留最后两级
        """
        if not url:
            return ""
        for domain in _special_domains:
            if domain in url:
                return domain
        _, netloc = StringUtils.get_url_netloc(url)
        if netloc:
            locs = netloc.split(".")
            if len(locs) > 3:
                return netloc
            return ".".join(locs[-2:])
        return ""

    @staticmethod
    def get_url_sld(url: str) -> str:
        """
        获取URL的二级域名部分，不含端口，若为IP则返回IP
        """
        if not url:
            return ""
        _, netloc = StringUtils.get_url_netloc(url)
        if not netloc:
            return ""
        netloc = netloc.split(":")[0].split(".")
        if len(netloc) >= 2:
            return netloc[-2]
        return netloc[0]

    @staticmethod
    def get_url_host(url: str) -> str:
        """
        获取URL的一级域名
        """
        if not url:
            return ""
        _, netloc = StringUtils.get_url_netloc(url)
        if not netloc:
            return ""
        return netloc.split(".")[-2]

    @staticmethod
    def get_base_url(url: str) -> str:
        """
        获取URL根地址
        """
        if not url:
            return ""
        scheme, netloc = StringUtils.get_url_netloc(url)
        return f"{scheme}://{netloc}"

    @staticmethod
    def clear_file_name(name: str) -> Optional[str]:
        if not name:
            return None
        return re.sub(r"[*?\\/\"<>~|]", "", name, flags=re.IGNORECASE).replace(":", "：")

    @staticmethod
    def generate_random_str(randomlength: int = 16) -> str:
        """
        生成一个指定长度的随机字符串
        """
        random_str = ''
        base_str = 'ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789'
        length = len(base_str) - 1
        for i in range(randomlength):
            random_str += base_str[random.randint(0, length)]
        return random_str

    @staticmethod
    def get_time(date: Any) -> Optional[datetime.datetime]:
        try:
            return dateutil.parser.parse(date)
        except dateutil.parser.ParserError:
            return None

    @staticmethod
    def unify_datetime_str(datetime_str: str) -> str:
        """
        日期时间格式化 统一转成 2020-10-14 07:48:04 这种格式
        # 场景1: 带有时区的日期字符串 eg: Sat, 15 Oct 2022 14:02:54 +0800
        # 场景2: 中间带T的日期字符串 eg: 2020-10-14T07:48:04
        # 场景3: 中间带T的日期字符串 eg: 2020-10-14T07:48:04.208
        # 场景4: 日期字符串以GMT结尾 eg: Fri, 14 Oct 2022 07:48:04 GMT
        # 场景5: 日期字符串以UTC结尾 eg: Fri, 14 Oct 2022 07:48:04 UTC
        # 场景6: 日期字符串以Z结尾 eg: Fri, 14 Oct 2022 07:48:04Z
        # 场景7: 日期字符串为相对时间 eg: 1 month, 2 days ago
        :param datetime_str:
        :return:
        """
        # 传入的参数如果是None 或者空字符串 直接返回
        if not datetime_str:
            return datetime_str

        try:
            return dateparser.parse(datetime_str).strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            print(str(e))
            return datetime_str

    @staticmethod
    def format_timestamp(timestamp: str, date_format: str = '%Y-%m-%d %H:%M:%S') -> str:
        """
        时间戳转日期
        :param timestamp:
        :param date_format:
        :return:
        """
        if isinstance(timestamp, str) and not timestamp.isdigit():
            return timestamp
        try:
            return datetime.datetime.fromtimestamp(int(timestamp)).strftime(date_format)
        except Exception as e:
            print(str(e))
            return timestamp

    @staticmethod
    def str_to_timestamp(date_str: str) -> float:
        """
        日期转时间戳
        :param date_str:
        :return:
        """
        if not date_str:
            return 0
        try:
            return dateparser.parse(date_str).timestamp()
        except Exception as e:
            print(str(e))
            return 0

    @staticmethod
    def to_bool(text: str, default_val: bool = False) -> bool:
        """
        字符串转bool
        :param text: 要转换的值
        :param default_val: 默认值
        :return:
        """
        if isinstance(text, str) and not text:
            return default_val
        if isinstance(text, bool):
            return text
        if isinstance(text, int) or isinstance(text, float):
            return True if text > 0 else False
        if isinstance(text, str) and text.lower() in ['y', 'true', '1', 'yes', 'on']:
            return True
        return False

    @staticmethod
    def str_from_cookiejar(cj: dict) -> str:
        """
        将cookiejar转换为字符串
        :param cj:
        :return:
        """
        return '; '.join(['='.join(item) for item in cj.items()])

    @staticmethod
    def get_idlist(content: str, dicts: List[dict]):
        """
        从字符串中提取id列表
        :param content: 字符串
        :param dicts: 字典
        :return:
        """
        if not content:
            return []
        id_list = []
        content_list = content.split()
        for dic in dicts:
            if dic.get('name') in content_list and dic.get('id') not in id_list:
                id_list.append(dic.get('id'))
                content = content.replace(dic.get('name'), '')
        return id_list, re.sub(r'\s+', ' ', content).strip()

    @staticmethod
    def md5_hash(data: Any) -> str:
        """
        MD5 HASH
        """
        if not data:
            return ""
        return hashlib.md5(str(data).encode()).hexdigest()

    @staticmethod
    def str_timehours(minutes: int) -> str:
        """
        将分钟转换成小时和分钟
        :param minutes:
        :return:
        """
        if not minutes:
            return ""
        hours = minutes // 60
        minutes = minutes % 60
        if hours:
            return "%s小时%s分" % (hours, minutes)
        else:
            return "%s分钟" % minutes

    @staticmethod
    def str_amount(amount: object, curr="$") -> str:
        """
        格式化显示金额
        """
        if not amount:
            return "0"
        return curr + format(amount, ",")

    @staticmethod
    def count_words(text: str) -> int:
        """
        计算字符串中包含的单词或汉字的数量，需要兼容中英文混合的情况
        :param text: 要计算的字符串
        :return: 字符串中包含的词数量
        """
        if not text:
            return 0
        # 使用正则表达式匹配汉字和英文单词
        chinese_pattern = '[\u4e00-\u9fa5]'
        english_pattern = '[a-zA-Z]+'

        # 匹配汉字和英文单词
        chinese_matches = re.findall(chinese_pattern, text)
        english_matches = re.findall(english_pattern, text)

        # 过滤掉空格和数字
        chinese_words = [word for word in chinese_matches if word.isalpha()]
        english_words = [word for word in english_matches if word.isalpha()]

        # 计算汉字和英文单词的数量
        chinese_count = len(chinese_words)
        english_count = len(english_words)

        return chinese_count + english_count

    @staticmethod
    def split_text(text: str, max_length: int) -> Generator:
        """
        把文本拆分为固定字节长度的数组，优先按换行拆分，避免单词内拆分
        """
        if not text:
            yield ''
        # 分行
        lines = re.split('\n', text)
        buf = ''
        for line in lines:
            if len(line.encode('utf-8')) > max_length:
                # 超长行继续拆分
                blank = ""
                if re.match(r'^[A-Za-z0-9.\s]+', line):
                    # 英文行按空格拆分
                    parts = line.split()
                    blank = " "
                else:
                    # 中文行按字符拆分
                    parts = line
                part = ''
                for p in parts:
                    if len((part + p).encode('utf-8')) > max_length:
                        # 超长则Yield
                        yield (buf + part).strip()
                        buf = ''
                        part = f"{blank}{p}"
                    else:
                        part = f"{part}{blank}{p}"
                if part:
                    # 将最后的部分追加到buf
                    buf += part
            else:
                if len((buf + "\n" + line).encode('utf-8')) > max_length:
                    # buf超长则Yield
                    yield buf.strip()
                    buf = line
                else:
                    # 短行直接追加到buf
                    if buf:
                        buf = f"{buf}\n{line}"
                    else:
                        buf = line
        if buf:
            # 处理文本末尾剩余部分
            yield buf.strip()

    @staticmethod
    def get_keyword(content: str) \
            -> Tuple[Optional[MediaType], Optional[str], Optional[int], Optional[int], Optional[str], Optional[str]]:
        """
        从搜索关键字中拆分中年份、季、集、类型
        """
        if not content:
            return None, None, None, None, None, None

        # 去掉查询中的电影或电视剧关键字
        mtype = MediaType.TV if re.search(r'^(电视剧|动漫|\s+电视剧|\s+动漫)', content) else None
        content = re.sub(r'^(电影|电视剧|动漫|\s+电影|\s+电视剧|\s+动漫)', '', content).strip()

        # 稍微切一下剧集吧
        season_num = None
        episode_num = None
        season_re = re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*季', content, re.IGNORECASE)
        if season_re:
            mtype = MediaType.TV
            season_num = int(cn2an.cn2an(season_re.group(1), mode='smart'))

        episode_re = re.search(r'第\s*([0-9一二三四五六七八九十百零]+)\s*集', content, re.IGNORECASE)
        if episode_re:
            mtype = MediaType.TV
            episode_num = int(cn2an.cn2an(episode_re.group(1), mode='smart'))
            if episode_num and not season_num:
                season_num = 1

        year_re = re.search(r'[\s(]+(\d{4})[\s)]*', content)
        year = year_re.group(1) if year_re else None

        key_word = re.sub(
            r'第\s*[0-9一二三四五六七八九十]+\s*季|第\s*[0-9一二三四五六七八九十百零]+\s*集|[\s(]+(\d{4})[\s)]*', '',
            content, flags=re.IGNORECASE).strip()
        key_word = re.sub(r'\s+', ' ', key_word) if key_word else year

        return mtype, key_word, season_num, episode_num, year, content

    @staticmethod
    def str_title(s: str) -> str:
        """
        大写首字母兼容None
        """
        return s.title() if s else s

    @staticmethod
    def escape_markdown(content: str) -> str:
        """
        Escapes Markdown characters in a string of Markdown.

        Credits to: simonsmh

        :param content: The string of Markdown to escape.
        :type content: :obj:`str`

        :return: The escaped string.
        :rtype: :obj:`str`
        """

        parses = re.sub(r"([_*\[\]()~`>#+\-=|.!{}])", r"\\\1", content)
        reparse = re.sub(r"\\\\([_*\[\]()~`>#+\-=|.!{}])", r"\1", parses)
        return reparse

    @staticmethod
    def get_domain_address(address: str, prefix: bool = True) -> Tuple[Optional[str], Optional[int]]:
        """
        从地址中获取域名和端口号
        :param address: 地址
        :param prefix：返回域名是否要包含协议前缀
        """
        if not address:
            return None, None
        # 去掉末尾的/
        address = address.rstrip("/")
        if prefix and not address.startswith("http"):
            # 如果需要包含协议前缀，但地址不包含协议前缀，则添加
            address = "http://" + address
        elif not prefix and address.startswith("http"):
            # 如果不需要包含协议前缀，但地址包含协议前缀，则去掉
            address = address.split("://")[-1]
        # 拆分域名和端口号
        parts = address.split(":")
        if len(parts) > 3:
            # 处理不希望包含多个冒号的情况（除了协议后的冒号）
            return None, None
        # 不含端口地址
        domain = ":".join(parts[:-1]).rstrip('/')
        # 端口号
        try:
            port = int(parts[-1])
        except ValueError:
            # 端口号不是整数，返回 None 表示无效
            return None, None
        return domain, port

    @staticmethod
    def str_series(array: List[int]) -> str:
        """
        将季集列表转化为字符串简写
        """

        # 确保数组按照升序排列
        array.sort()

        result = []
        start = array[0]
        end = array[0]

        for i in range(1, len(array)):
            if array[i] == end + 1:
                end = array[i]
            else:
                if start == end:
                    result.append(str(start))
                else:
                    result.append(f"{start}-{end}")
                start = array[i]
                end = array[i]

        # 处理最后一个序列
        if start == end:
            result.append(str(start))
        else:
            result.append(f"{start}-{end}")

        return ",".join(result)

    @staticmethod
    def format_ep(nums: List[int]) -> str:
        """
        将剧集列表格式化为连续区间
        """
        if not nums:
            return ""
        if len(nums) == 1:
            return f"E{nums[0]:02d}"
        # 将数组升序排序
        nums.sort()
        formatted_ranges = []
        start = nums[0]
        end = nums[0]

        for i in range(1, len(nums)):
            if nums[i] == end + 1:
                end = nums[i]
            else:
                if start == end:
                    formatted_ranges.append(f"E{start:02d}")
                else:
                    formatted_ranges.append(f"E{start:02d}-E{end:02d}")
                start = end = nums[i]

        if start == end:
            formatted_ranges.append(f"E{start:02d}")
        else:
            formatted_ranges.append(f"E{start:02d}-E{end:02d}")

        formatted_string = "、".join(formatted_ranges)
        return formatted_string

    @staticmethod
    def is_number(text: str) -> bool:
        """
        判断字符是否为可以转换为整数或者浮点数
        """
        if not text:
            return False
        try:
            float(text)
            return True
        except ValueError:
            return False

    @staticmethod
    def find_common_prefix(str1: str, str2: str) -> str:
        if not str1 or not str2:
            return ''
        common_prefix = []
        min_len = min(len(str1), len(str2))

        for i in range(min_len):
            if str1[i] == str2[i]:
                common_prefix.append(str1[i])
            else:
                break

        return ''.join(common_prefix)

    @staticmethod
    def compare_version(v1: str, v2: str) -> int:
        """
        比较两个版本号的大小，v1 > v2时返回1，v1 < v2时返回-1，v1 = v2时返回0
        """
        if not v1 or not v2:
            return 0
        v1 = v1.replace('v', '')
        v2 = v2.replace('v', '')
        v1 = [int(x) for x in v1.split('.')]
        v2 = [int(x) for x in v2.split('.')]
        for i in range(min(len(v1), len(v2))):
            if v1[i] > v2[i]:
                return 1
            elif v1[i] < v2[i]:
                return -1
        if len(v1) > len(v2):
            return 1
        elif len(v1) < len(v2):
            return -1
        else:
            return 0

    @staticmethod
    def diff_time_str(time_str: str):
        """
        输入YYYY-MM-DD HH24:MI:SS 格式的时间字符串，返回距离现在的剩余时间：xx天xx小时xx分钟
        """
        if not time_str:
            return ''
        try:
            time_obj = datetime.datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return time_str
        now = datetime.datetime.now()
        diff = time_obj - now
        diff_seconds = diff.seconds
        diff_days = diff.days
        diff_hours = diff_seconds // 3600
        diff_minutes = (diff_seconds % 3600) // 60
        if diff_days > 0:
            return f'{diff_days}天{diff_hours}小时{diff_minutes}分钟'
        elif diff_hours > 0:
            return f'{diff_hours}小时{diff_minutes}分钟'
        elif diff_minutes > 0:
            return f'{diff_minutes}分钟'
        else:
            return ''
