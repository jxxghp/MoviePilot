import json
import os
import time
from io import BytesIO
from typing import Tuple

from PIL import Image
from lxml import etree
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class Tjupt(_ISiteSigninHandler):
    """
    北洋签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "tjupt.org"

    # 签到地址
    _sign_in_url = 'https://www.tjupt.org/attendance.php'

    # 已签到
    _sign_regex = ['<a href="attendance.php">今日已签到</a>']

    # 签到成功
    _succeed_regex = ['这是您的首次签到，本次签到获得\\d+个魔力值。',
                      '签到成功，这是您的第\\d+次签到，已连续签到\\d+天，本次签到获得\\d+个魔力值。',
                      '重新签到成功，本次签到获得\\d+个魔力值']

    # 存储正确的答案，后续可直接查
    _answer_path = settings.TEMP_PATH / "signin/"
    _answer_file = _answer_path / "tjupt.json"

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxy = site_info.get("proxy")
        render = site_info.get("render")

        # 创建正确答案存储目录
        if not os.path.exists(os.path.dirname(self._answer_file)):
            os.makedirs(os.path.dirname(self._answer_file))

        # 获取北洋签到页面html
        html_text = self.get_page_source(url=self._sign_in_url,
                                         cookie=site_cookie,
                                         ua=ua,
                                         proxy=proxy,
                                         render=render)

        # 获取签到后返回html，判断是否签到成功
        if not html_text:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in html_text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        sign_status = self.sign_in_result(html_res=html_text,
                                          regexs=self._sign_regex)
        if sign_status:
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'

        # 没有签到则解析html
        html = etree.HTML(html_text)
        if not html:
            return False, '签到失败'
        img_url = html.xpath('//table[@class="captcha"]//img/@src')[0]

        if not img_url:
            logger.error(f"{site} 签到失败，未获取到签到图片")
            return False, '签到失败，未获取到签到图片'

        # 签到图片
        img_url = "https://www.tjupt.org" + img_url
        logger.info(f"获取到签到图片 {img_url}")
        # 获取签到图片hash
        captcha_img_res = RequestUtils(cookies=site_cookie,
                                       ua=ua,
                                       proxies=settings.PROXY if proxy else None
                                       ).get_res(url=img_url)
        if not captcha_img_res or captcha_img_res.status_code != 200:
            logger.error(f"{site} 签到图片 {img_url} 请求失败")
            return False, '签到失败，未获取到签到图片'
        captcha_img = Image.open(BytesIO(captcha_img_res.content))
        captcha_img_hash = self._tohash(captcha_img)
        logger.debug(f"签到图片hash {captcha_img_hash}")

        # 签到答案选项
        values = html.xpath("//input[@name='answer']/@value")
        options = html.xpath("//input[@name='answer']/following-sibling::text()")

        if not values or not options:
            logger.error(f"{site} 签到失败，未获取到答案选项")
            return False, '签到失败，未获取到答案选项'

        # value+选项
        answers = list(zip(values, options))
        logger.debug(f"获取到所有签到选项 {answers}")

        # 查询已有答案
        exits_answers = {}
        try:
            with open(self._answer_file, 'r') as f:
                json_str = f.read()
            exits_answers = json.loads(json_str)
            # 查询本地本次验证码hash答案
            captcha_answer = exits_answers[captcha_img_hash]

            # 本地存在本次hash对应的正确答案再遍历查询
            if captcha_answer:
                for value, answer in answers:
                    if str(captcha_answer) == str(answer):
                        # 确实是答案
                        return self.__signin(answer=value,
                                             site_cookie=site_cookie,
                                             ua=ua,
                                             proxy=proxy,
                                             site=site)
        except (FileNotFoundError, IOError, OSError) as e:
            logger.debug(f"查询本地已知答案失败：{str(e)}，继续请求豆瓣查询")

        # 本地不存在正确答案则请求豆瓣查询匹配
        for value, answer in answers:
            if answer:
                # 豆瓣检索
                db_res = RequestUtils().get_res(url=f'https://movie.douban.com/j/subject_suggest?q={answer}')
                if not db_res or db_res.status_code != 200:
                    logger.debug(f"签到选项 {answer} 未查询到豆瓣数据")
                    continue

                # 豆瓣返回结果
                db_answers = json.loads(db_res.text)
                if not isinstance(db_answers, list):
                    db_answers = [db_answers]

                if len(db_answers) == 0:
                    logger.debug(f"签到选项 {answer} 查询到豆瓣数据为空")

                for db_answer in db_answers:
                    answer_img_url = db_answer['img']

                    # 获取答案hash
                    answer_img_res = RequestUtils(referer="https://movie.douban.com").get_res(url=answer_img_url)
                    if not answer_img_res or answer_img_res.status_code != 200:
                        logger.debug(f"签到答案 {answer} {answer_img_url} 请求失败")
                        continue

                    answer_img = Image.open(BytesIO(answer_img_res.content))
                    answer_img_hash = self._tohash(answer_img)
                    logger.debug(f"签到答案图片hash {answer} {answer_img_hash}")

                    # 获取选项图片与签到图片相似度，大于0.9默认是正确答案
                    score = self._comparehash(captcha_img_hash, answer_img_hash)
                    logger.info(f"签到图片与选项 {answer} 豆瓣图片相似度 {score}")
                    if score > 0.9:
                        # 确实是答案
                        return self.__signin(answer=value,
                                             site_cookie=site_cookie,
                                             ua=ua,
                                             proxy=proxy,
                                             site=site,
                                             exits_answers=exits_answers,
                                             captcha_img_hash=captcha_img_hash)

            # 间隔5s，防止请求太频繁被豆瓣屏蔽ip
            time.sleep(5)
        logger.error(f"豆瓣图片匹配，未获取到匹配答案")

        # 没有匹配签到成功，则签到失败
        return False, '签到失败，未获取到匹配答案'

    def __signin(self, answer, site_cookie, ua, proxy, site, exits_answers=None, captcha_img_hash=None):
        """
        签到请求
        """
        data = {
            'answer': answer,
            'submit': '提交'
        }
        logger.debug(f"提交data {data}")
        sign_in_res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=settings.PROXY if proxy else None
                                   ).post_res(url=self._sign_in_url, data=data)
        if not sign_in_res or sign_in_res.status_code != 200:
            logger.error(f"{site} 签到失败，签到接口请求失败")
            return False, '签到失败，签到接口请求失败'

        # 获取签到后返回html，判断是否签到成功
        sign_status = self.sign_in_result(html_res=sign_in_res.text,
                                          regexs=self._succeed_regex)
        if sign_status:
            logger.info(f"签到成功")
            if exits_answers and captcha_img_hash:
                # 签到成功写入本地文件
                self.__write_local_answer(exits_answers=exits_answers or {},
                                          captcha_img_hash=captcha_img_hash,
                                          answer=answer)
            return True, '签到成功'
        else:
            logger.error(f"{site} 签到失败，请到页面查看")
            return False, '签到失败，请到页面查看'

    def __write_local_answer(self, exits_answers, captcha_img_hash, answer):
        """
        签到成功写入本地文件
        """
        try:
            exits_answers[captcha_img_hash] = answer
            # 序列化数据
            formatted_data = json.dumps(exits_answers, indent=4)
            with open(self._answer_file, 'w') as f:
                f.write(formatted_data)
        except (FileNotFoundError, IOError, OSError) as e:
            logger.debug(f"签到成功写入本地文件失败：{str(e)}")

    @staticmethod
    def _tohash(img, shape=(10, 10)):
        """
        获取图片hash
        """
        img = img.resize(shape)
        gray = img.convert('L')
        s = 0
        hash_str = ''
        for i in range(shape[1]):
            for j in range(shape[0]):
                s = s + gray.getpixel((j, i))
        avg = s / (shape[0] * shape[1])
        for i in range(shape[1]):
            for j in range(shape[0]):
                if gray.getpixel((j, i)) > avg:
                    hash_str = hash_str + '1'
                else:
                    hash_str = hash_str + '0'
        return hash_str

    @staticmethod
    def _comparehash(hash1, hash2, shape=(10, 10)):
        """
        比较图片hash
        返回相似度
        """
        n = 0
        if len(hash1) != len(hash2):
            return -1
        for i in range(len(hash1)):
            if hash1[i] == hash2[i]:
                n = n + 1
        return n / (shape[0] * shape[1])
