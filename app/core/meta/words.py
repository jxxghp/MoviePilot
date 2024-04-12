from typing import List, Tuple

import cn2an
import regex as re

from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class WordsMatcher(metaclass=Singleton):

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def prepare(self, title: str) -> Tuple[str, List[str]]:
        """
        预处理标题，支持三种格式
        1：屏蔽词
        2：被替换词 => 替换词
        3：前定位词 <> 后定位词 >> 偏移量（EP）
        """
        appley_words = []
        # 读取自定义识别词
        words: List[str] = self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []
        for word in words:
            if not word or word.startswith("#"):
                continue
            try:
                if word.count(" => ") and word.count(" && ") and word.count(" >> ") and word.count(" <> "):
                    # 替换词
                    thc = str(re.findall(r'(.*?)\s*=>', word)[0]).strip()
                    # 被替换词
                    bthc = str(re.findall(r'=>\s*(.*?)\s*&&', word)[0]).strip()
                    # 集偏移前字段
                    pyq = str(re.findall(r'&&\s*(.*?)\s*<>', word)[0]).strip()
                    # 集偏移后字段
                    pyh = str(re.findall(r'<>(.*?)\s*>>', word)[0]).strip()
                    # 集偏移
                    offsets = str(re.findall(r'>>\s*(.*?)$', word)[0]).strip()
                    # 替换词
                    title, message, state = self.__replace_regex(title, thc, bthc)
                    if state:
                        # 替换词成功再进行集偏移
                        title, message, state = self.__episode_offset(title, pyq, pyh, offsets)
                elif word.count(" => "):
                    # 替换词
                    strings = word.split(" => ")
                    title, message, state = self.__replace_regex(title, strings[0], strings[1])
                elif word.count(" >> ") and word.count(" <> "):
                    # 集偏移
                    strings = word.split(" <> ")
                    offsets = strings[1].split(" >> ")
                    strings[1] = offsets[0]
                    title, message, state = self.__episode_offset(title, strings[0], strings[1], offsets[1])
                else:
                    # 屏蔽词
                    if not word.strip():
                        continue
                    title, message, state = self.__replace_regex(title, word, "")

                if state:
                    appley_words.append(word)

            except Exception as err:
                logger.warn(f"自定义识别词 {word} 预处理标题失败：{str(err)} - 标题：{title}")

        return title, appley_words

    @staticmethod
    def __replace_regex(title: str, replaced: str, replace: str) -> Tuple[str, str, bool]:
        """
        正则替换
        """
        try:
            if not re.findall(r'%s' % replaced, title):
                return title, "", False
            else:
                return re.sub(r'%s' % replaced, r'%s' % replace, title), "", True
        except Exception as err:
            logger.warn(f"自定义识别词正则替换失败：{str(err)} - 标题：{title}，被替换词：{replaced}，替换词：{replace}")
            return title, str(err), False

    @staticmethod
    def __episode_offset(title: str, front: str, back: str, offset: str) -> Tuple[str, str, bool]:
        """
        集数偏移
        """
        try:
            if back and not re.findall(r'%s' % back, title):
                return title, "", False
            if front and not re.findall(r'%s' % front, title):
                return title, "", False
            offset_word_info_re = re.compile(r'(?<=%s.*?)[0-9一二三四五六七八九十]+(?=.*?%s)' % (front, back))
            episode_nums_str = re.findall(offset_word_info_re, title)
            if not episode_nums_str:
                return title, "", False
            episode_nums_offset_str = []
            offset_order_flag = False
            for episode_num_str in episode_nums_str:
                episode_num_int = int(cn2an.cn2an(episode_num_str, "smart"))
                offset_caculate = offset.replace("EP", str(episode_num_int))
                episode_num_offset_int = int(eval(offset_caculate))
                # 向前偏移
                if episode_num_int > episode_num_offset_int:
                    offset_order_flag = True
                # 向后偏移
                elif episode_num_int < episode_num_offset_int:
                    offset_order_flag = False
                # 原值是中文数字，转换回中文数字，阿拉伯数字则还原0的填充
                if not episode_num_str.isdigit():
                    episode_num_offset_str = cn2an.an2cn(episode_num_offset_int, "low")
                else:
                    count_0 = re.findall(r"^0+", episode_num_str)
                    if count_0:
                        episode_num_offset_str = f"{count_0[0]}{episode_num_offset_int}"
                    else:
                        episode_num_offset_str = str(episode_num_offset_int)
                episode_nums_offset_str.append(episode_num_offset_str)
            episode_nums_dict = dict(zip(episode_nums_str, episode_nums_offset_str))
            # 集数向前偏移，集数按升序处理
            if offset_order_flag:
                episode_nums_list = sorted(episode_nums_dict.items(), key=lambda x: x[1])
            # 集数向后偏移，集数按降序处理
            else:
                episode_nums_list = sorted(episode_nums_dict.items(), key=lambda x: x[1], reverse=True)
            for episode_num in episode_nums_list:
                episode_offset_re = re.compile(
                    r'(?<=%s.*?)%s(?=.*?%s)' % (front, episode_num[0], back))
                title = re.sub(episode_offset_re, r'%s' % episode_num[1], title)
            return title, "", True
        except Exception as err:
            logger.warn(f"自定义识别词集数偏移失败：{str(err)} - 标题：{title}，前定位词：{front}，后定位词：{back}，偏移量：{offset}")
            return title, str(err), False
