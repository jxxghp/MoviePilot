import regex as re

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class ReleaseGroupsMatcher(metaclass=Singleton):
    """
    识别制作组、字幕组
    """
    __release_groups: str = None
    # 内置组
    RELEASE_GROUPS: dict = {
        "0ff": ['FF(?:(?:A|WE)B|CD|E(?:DU|B)|TV)'],
        "1pt": [],
        "52pt": [],
        "audiences": ['Audies', 'AD(?:Audio|E(?:|book)|Music|Web)'],
        "azusa": [],
        "beitai": ['BeiTai'],
        "btschool": ['Bts(?:CHOOL|HD|PAD|TV)', 'Zone'],
        "carpt": ['CarPT'],
        "chdbits": ['CHD(?:|Bits|PAD|(?:|HK)TV|WEB)', 'StBOX', 'OneHD', 'Lee', 'xiaopie'],
        "discfan": [],
        "dragonhd": [],
        "eastgame": ['(?:(?:iNT|(?:HALFC|Mini(?:S|H|FH)D))-|)TLF'],
        "filelist": [],
        "gainbound": ['(?:DG|GBWE)B'],
        "hares": ['Hares(?:|(?:M|T)V|Web)'],
        "hd4fans": [],
        "hdarea": ['HDA(?:pad|rea|TV)', 'EPiC'],
        "hdatmos": [],
        "hdbd": [],
        "hdchina": ['HDC(?:|hina|TV)', 'k9611', 'tudou', 'iHD'],
        "hddolby": ['D(?:ream|BTV)', '(?:HD|QHstudI)o'],
        "hdfans": ['beAst(?:|TV)'],
        "hdhome": ['HDH(?:|ome|Pad|TV|WEB)'],
        "hdpt": ['HDPT(?:|Web)'],
        "hdsky": ['HDS(?:|ky|TV|Pad|WEB)', 'AQLJ'],
        "hdtime": [],
        "HDU": [],
        "hdvideo": [],
        "hdzone": ['HDZ(?:|one)'],
        "hhanclub": ['HHWEB'],
        "hitpt": [],
        "htpt": ['HTPT'],
        "iptorrents": [],
        "joyhd": [],
        "keepfrds": ['FRDS', 'Yumi', 'cXcY'],
        "lemonhd": ['L(?:eague(?:(?:C|H)D|(?:M|T)V|NF|WEB)|HD)', 'i18n', 'CiNT'],
        "mteam": ['MTeam(?:|TV)', 'MPAD'],
        "nanyangpt": [],
        "nicept": [],
        "oshen": [],
        "ourbits": ['Our(?:Bits|TV)', 'FLTTH', 'Ao', 'PbK', 'MGs', 'iLove(?:HD|TV)'],
        "piggo": ['PiGo(?:NF|(?:H|WE)B)'],
        "ptchina": [],
        "pterclub": ['PTer(?:|DIY|Game|(?:M|T)V|WEB)'],
        "pthome": ['PTH(?:|Audio|eBook|music|ome|tv|WEB)'],
        "ptmsg": [],
        "ptsbao": ['PTsbao', 'OPS', 'F(?:Fans(?:AIeNcE|BD|D(?:VD|IY)|TV|WEB)|HDMv)', 'SGXT'],
        "pttime": [],
        "putao": ['PuTao'],
        "soulvoice": [],
        "springsunday": ['CMCT(?:|V)'],
        "sharkpt": ['Shark(?:|WEB|DIY|TV|MV)'],
        "tccf": [],
        "tjupt": ['TJUPT'],
        "totheglory": ['TTG', 'WiKi', 'NGB', 'DoA', '(?:ARi|ExRE)N'],
        "U2": [],
        "ultrahd": [],
        "others": ['B(?:MDru|eyondHD|TN)', 'C(?:fandora|trlhd|MRG)', 'DON', 'EVO', 'FLUX', 'HONE(?:|yG)',
                   'N(?:oGroup|T(?:b|G))', 'PandaMoon', 'SMURF', 'T(?:EPES|aengoo|rollHD )'],
        "anime": ['ANi', 'HYSUB', 'KTXP', 'LoliHouse', 'MCE', 'Nekomoe kissaten', 'SweetSub', 'MingY', 
                  '(?:Lilith|NC)-Raws', '织梦字幕组', '枫叶字幕组', '猎户手抄部', '喵萌奶茶屋', '漫猫字幕社', 
                  '霜庭云花Sub', '北宇治字幕组', '氢气烤肉架', '云歌字幕组', '萌樱字幕组','极影字幕社','悠哈璃羽字幕社',
                  '❀拨雪寻春❀', '沸羊羊(?:制作|字幕组)', '(?:桜|樱)都字幕组',]
    }

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        release_groups = []
        for site_groups in self.RELEASE_GROUPS.values():
            for release_group in site_groups:
                release_groups.append(release_group)
        self.__release_groups = '|'.join(release_groups)

    def match(self, title: str = None, groups: str = None):
        """
        :param title: 资源标题或文件名
        :param groups: 制作组/字幕组
        :return: 匹配结果
        """
        if not title:
            return ""
        if not groups:
            # 自定义组
            custom_release_groups = self.systemconfig.get(SystemConfigKey.CustomReleaseGroups)
            if custom_release_groups:
                custom_release_groups_str = '|'.join(custom_release_groups)
                groups = f"{self.__release_groups}|{custom_release_groups_str}"
            else:
                groups = self.__release_groups
        title = f"{title} "
        groups_re = re.compile(r"(?<=[-@\[￡【&])(?:%s)(?=[@.\s\]\[】&])" % groups, re.I)
        # 处理一个制作组识别多次的情况，保留顺序
        unique_groups = []
        for item in re.findall(groups_re, title):
            if item not in unique_groups:
                unique_groups.append(item)
        return "@".join(unique_groups)
