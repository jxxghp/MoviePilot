from app.core.meta import MetaMusic


def parse_title(title: str) -> MetaMusic:
    """构造种子/文件名标题解析结果，供识别断言复用。"""
    return MetaMusic(org_string=title, title=title, parse_title=True)


def test_strip_track_prefix_handles_dot_separator():
    """曲序前缀 01. 应剥离并返回曲名。"""
    track, disc, title = MetaMusic.split_track_prefix("01.晴天")

    assert (track, disc, title) == (1, None, "晴天")


def test_strip_track_prefix_handles_dash_and_space():
    """常见 rip 命名 01 - 曲名 和 01 曲名 都应识别曲序。"""
    assert MetaMusic.split_track_prefix("03 - 七里香") == (3, None, "七里香")
    assert MetaMusic.split_track_prefix("05 借口") == (5, None, "借口")


def test_strip_track_prefix_handles_disc_track_number():
    """碟号-曲序前缀 1-02 应同时提取碟号和曲序。"""
    track, disc, title = MetaMusic.split_track_prefix("1-02 半岛铁盒")

    assert (track, disc, title) == (2, 1, "半岛铁盒")


def test_strip_track_prefix_handles_number_only_name():
    """纯数字文件名只能得到曲序，曲名返回 None 由调用方兜底。"""
    track, disc, title = MetaMusic.split_track_prefix("07")

    assert (track, disc, title) == (7, None, None)


def test_strip_track_prefix_keeps_normal_title():
    """普通曲名不应被误判为曲序前缀。"""
    assert MetaMusic.split_track_prefix("晴天") == (None, None, None)
    assert MetaMusic.split_track_prefix("2002") == (None, None, None)


def test_split_artist_title():
    """歌手 - 曲名结构应拆分，无分隔符时原文作为标题。"""
    artist, title = MetaMusic.split_artist_title("周杰伦 - 晴天")

    assert artist == "周杰伦"
    assert title == "晴天"
    assert MetaMusic.split_artist_title("晴天") == (None, "晴天")


def test_parse_disc_dir():
    """CD1、Disc 2 等碟片目录应识别碟号。"""
    assert MetaMusic.parse_disc_dir("CD1") == 1
    assert MetaMusic.parse_disc_dir("Disc 2") == 2
    assert MetaMusic.parse_disc_dir("disk03") == 3
    assert MetaMusic.parse_disc_dir("无损音乐") is None


def test_parse_album_dir_extracts_artist_album_year():
    """专辑目录名应提取歌手、专辑、年份和音质描述。"""
    info = MetaMusic.parse_album_dir("周杰伦 - 七里香 (2004) [FLAC 24bit-96kHz]")

    assert info["artist"] == "周杰伦"
    assert info["album"] == "七里香"
    assert info["year"] == 2004
    assert "FLAC" in info["quality_text"]


def test_parse_album_dir_without_artist():
    """没有歌手分隔的目录名整体作为专辑名。"""
    info = MetaMusic.parse_album_dir("Random Access Memories (2013)")

    assert info["artist"] is None
    assert info["album"] == "Random Access Memories"
    assert info["year"] == 2013


def test_apply_path_context_fills_wav_meta(tmp_path):
    """无标签 WAV 应从文件名和目录结构补齐曲序、专辑和歌手。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004) [FLAC]"
    album_dir.mkdir()
    wav_file = album_dir / "01.我的地盘.wav"
    wav_file.write_bytes(b"RIFF")

    meta = MetaMusic(org_string=wav_file.name, title=wav_file.stem, audio_format="WAV")
    meta.apply_path_context(wav_file)

    assert meta.track_number == 1
    assert meta.title == "我的地盘"
    assert meta.album == "七里香"
    assert meta.artists == ["周杰伦"]
    assert meta.album_artist == "周杰伦"
    assert meta.year == 2004


def test_apply_path_context_uses_disc_subdir(tmp_path):
    """CD1 子目录内的文件应继承碟号并向上找到专辑目录。"""
    album_dir = tmp_path / "Daft Punk - Discovery (2001)"
    disc_dir = album_dir / "CD1"
    disc_dir.mkdir(parents=True)
    wav_file = disc_dir / "01 - One More Time.flac"
    wav_file.write_bytes(b"fLaC")

    meta = MetaMusic(org_string=wav_file.name, title=wav_file.stem, audio_format="FLAC")
    meta.apply_path_context(wav_file)

    assert meta.disc_number == 1
    assert meta.track_number == 1
    assert meta.title == "One More Time"
    assert meta.album == "Discovery"
    assert meta.artists == ["Daft Punk"]


def test_apply_path_context_keeps_existing_tags(tmp_path):
    """已有标签字段不应被目录猜测覆盖。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    audio_file = album_dir / "01.我的地盘.mp3"
    audio_file.write_bytes(b"")

    meta = MetaMusic(
        org_string=audio_file.name,
        title="我的地盘",
        artists=["周杰伦"],
        album="七里香",
        year=2004,
        track_number=1,
        audio_format="MP3",
    )
    meta.apply_path_context(audio_file)

    assert meta.title == "我的地盘"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "七里香"
    assert meta.year == 2004


def test_apply_title_splits_artist_and_track():
    """标准「歌手 - 曲名」种子标题应拆分艺术家与曲名。"""
    meta = parse_title("周杰伦 - 晴天")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天"


def test_apply_title_splits_multi_artists():
    """多艺术家 & 联名写法应拆分为列表保留顺序。"""
    meta = parse_title("章子怡 & 周深 - 灯火里的中国")

    assert meta.artists == ["章子怡", "周深"]
    assert meta.title == "灯火里的中国"


def test_apply_title_strips_quality_tokens():
    """格式、位深采样与发行标记不应进入曲名。"""
    meta = parse_title("[250917] SARD UNDERGROUND - 故障した車 FLAC")

    assert meta.artists == ["SARD UNDERGROUND"]
    assert meta.title == "故障した車"
    assert meta.audio_format == "FLAC"


def test_apply_title_strips_video_tokens():
    """演唱会视频种子的分辨率与编码标记不应进入曲名。"""
    meta = parse_title("S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv")

    # 连续单字母空格序列是缩写点号被压平的结果，还原为 S.H.E 才能与条目署名比对
    assert meta.artists == ["S.H.E"]
    assert meta.title == "S.H.E十七音乐会"
    # 尾部年份提取为发行年份线索
    assert meta.year == 2018


def test_apply_title_splits_latin_hyphen_artist_album():
    """拉丁「艺术家-专辑」无空格连字符命名应拆分，单侧单词不采信。"""
    meta = parse_title("Gene Clark-White Light 1971 - FLAC 16bit 44 1khz")

    assert meta.artists == ["Gene Clark"]
    assert meta.title == "White Light"
    assert meta.year == 1971
    # 左侧单词（Heize-Undo）与右侧发布组标签不触发拆分
    assert parse_title("Heize-Undo.2022.FLAC").artists == []


def test_apply_title_splits_various_artists_prefix():
    """场景命名的 Various Artists-Title 无空格连字符前缀应拆分为合辑署名。"""
    meta = parse_title("Various.Artists-Reply.1988.OST")

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Reply 1988 OST"


def test_apply_title_splits_year_sandwich():
    """「艺术家 年份 专辑」三明治结构按中部年份拆分并提取年份。"""
    meta = parse_title("Leehom Wang 2010 The 18 Martial Arts")

    assert meta.artists == ["Leehom Wang"]
    assert meta.title == "The 18 Martial Arts"
    assert meta.year == 2010

    # 多艺术家分隔符与单词专辑名同样适用
    meta = parse_title("ASKA&SENS 1993 YAH YAH YAH")
    assert meta.artists == ["ASKA", "SENS"]
    assert meta.title == "YAH YAH YAH"
    assert meta.year == 1993


def test_apply_title_year_sandwich_guards():
    """三明治拆分的护栏：规格残留与长艺术家段不误拆。"""
    # 剩余段数字开头是规格（2.0）不是专辑名
    assert parse_title("K3 Kan Het S02 2014 2.0 -MINIBEL").artists == []
    # 剩余段连字符开头是发布组标签（-PTer）不是专辑名
    assert parse_title("Ashton Celebration 2013 -PTer").artists == []
    # 艺术家段超过 4 个词时拒绝拆分（含曲名与年份的完整标题）
    assert parse_title("Bee Gees One Night Only 1997 -ProfessorP").artists == []


def test_apply_title_restores_letter_abbrev():
    """单字母点号缩写还原不误伤合法缩写与单词。"""
    meta = parse_title("E.S.Posthumus - Ashielf Alpen FLAC")

    # 点分少于 3 处的合法缩写不被场景点分/还原逻辑破坏
    assert meta.artists == ["E.S.Posthumus"]
    assert meta.title == "Ashielf Alpen"


def test_apply_title_strips_release_group_tag():
    """格式标记后的发布组标签（大小写混合）应整体剔除。"""
    meta = parse_title("某某乐队 - 星空 FLAC-FHDMv")

    assert meta.title == "星空"


def test_apply_title_strips_date_prefix():
    """电视录制标题开头的日期前缀应剔除。"""
    meta = parse_title("2018.01.10 藤田麻衣子 思い続ければ FLAC")

    assert meta.title == "藤田麻衣子 思い続ければ"
    assert meta.audio_format == "FLAC"


def test_apply_title_date_prefix_with_time():
    """带时分秒的录制前缀（下划线分隔）同样应剔除。"""
    meta = parse_title("2024-01-27_20-00_ＷＯＷＯＷプライム_松任谷由実　５０ｔｈ　Ａｎｎｉｖｅｒｓａｒｙ")

    assert "2024" not in (meta.title or "")
    assert "松任谷由実" in (meta.title or "")
    assert "50th" in (meta.title or "")


def test_apply_title_keeps_song_named_with_year():
    """曲名自带的年份数字（非日期结构）不应被误剔除。"""
    meta = parse_title("2002年的第一场雪")

    assert meta.title == "2002年的第一场雪"


def test_apply_title_year_range_as_year():
    """全集标题尾部的年份区间应提取结束年并剥离出标题。"""
    meta = parse_title("天国的情人-邓丽君作品全集1967-1995")

    assert meta.title == "天国的情人"
    assert meta.artists == ["邓丽君"]
    assert meta.year == 1995


def test_apply_title_year_range_in_title_kept():
    """年份区间后随内容文字时属于标题本身，只提取年份不剥离。"""
    meta = parse_title("许茹芸 - 许茹芸1995-2000年光华真纪录 (2001)")

    assert meta.artists == ["许茹芸"]
    assert meta.title == "许茹芸1995-2000年光华真纪录"
    # 括号年份优先于区间提取的结束年
    assert meta.year == 2001


def test_apply_title_scene_dot_naming():
    """场景点分命名的点号应归一为空格，发布组标签剔除。"""
    meta = parse_title("Shan.Ge.Liao.Zai.2023.WEB-DL.FLAC-CMCTA")

    assert meta.title == "Shan Ge Liao Zai"
    assert meta.year == 2023
    assert meta.audio_format == "FLAC"


def test_apply_title_scene_dot_with_symbols():
    """点分命名中环绕符号的点（.&.、.-.）也应归一并支持艺术家拆分。"""
    meta = parse_title(
        "Deep.Purple.&.Orchestra.-.Live.At.Montreux.1999.2022.1080p.BluRay.AVC.DTS-HD.MA5.1"
    )

    assert meta.artists == ["Deep Purple", "Orchestra"]
    assert meta.title == "Live At Montreux 1999 2022"


def test_apply_title_keeps_artist_abbreviation_dots():
    """点分隔少于 3 处的艺术家缩写点号不应被归一。"""
    meta = parse_title("E.S.Posthumus - Maraboot")

    assert meta.artists == ["E.S.Posthumus"]
    assert meta.title == "Maraboot"


def test_apply_title_va_alias_artist():
    """合辑署名 VA 应归一为 MusicBrainz 规范署名 Various Artists。"""
    meta = parse_title("VA - Funky Jazz Saxophone 2024 FLAC")

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Funky Jazz Saxophone"
    assert meta.year == 2024


def test_apply_title_va_scene_prefix():
    """场景命名 VA-Title 无空格连字符写法应按别名前缀拆分。"""
    meta = parse_title(
        "VA-Once.Upon.a.Time.in.Hollywood.Original.Motion.Picture.Soundtrack.2019.FLAC.24bit.96kHz"
    )

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Once Upon a Time in Hollywood Original Motion Picture Soundtrack"
    assert meta.year == 2019


def test_apply_title_keeps_single_word_title():
    """无音质标记时「艺术家 - 单词曲名」的曲名不应被当发布组标签剔除。"""
    meta = parse_title("E.S.Posthumus - Maraboot")

    assert meta.title == "Maraboot"


def test_apply_title_keeps_title_with_quality_tokens():
    """存在音质标记时，空格连字符后的单词曲名也不应被当发布组标签剔除。"""
    meta = parse_title("Yes - Aurora [Bonus Tracks Edition, 24-bit Hi-Res] (2026) [FLAC]")

    assert meta.artists == ["Yes"]
    assert meta.title == "Aurora"
    assert meta.year == 2026


def test_apply_title_album_marker():
    """「歌手《专辑名》」书名号命名应提取艺术家、专辑与碟号。"""
    meta = parse_title("李宗盛《理性与感性作品音乐会-CD2》2006-FLAC-分轨")

    assert meta.artists == ["李宗盛"]
    assert meta.album == "理性与感性作品音乐会"
    assert meta.title == "理性与感性作品音乐会"
    assert meta.disc_number == 2
    assert meta.year == 2006


def test_apply_title_strips_cue_and_plus():
    """APE+CUE 类格式联合写法应剔除，残留加号不阻断标题提取。"""
    meta = parse_title("世界著名古典大师名版收藏（15）RCA发烧古典系列-2007-FLAC-APE+CUE")

    assert meta.artists == []
    assert meta.title == "世界著名古典大师名版收藏(15)RCA发烧古典系列"
    assert meta.year == 2007


def test_apply_title_cjk_hyphen_artist_suffix():
    """CJK「曲名-歌手」无空格连字符写法应反向拆分艺术家。"""
    meta = parse_title("因为有你-毛阿敏")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "因为有你"


def test_apply_title_double_em_dash_split():
    """双破折号分隔的「主题——歌手」写法应拆分艺术家。"""
    meta = parse_title("为你盛开——许巍 无尽光芒巡回演唱会 2025")

    assert meta.artists == ["许巍"]
    assert meta.title.startswith("为你盛开")


def test_apply_title_keeps_english_hyphen_title():
    """英文曲名中的连字符是标题组成部分，不做艺术家拆分。"""
    meta = parse_title("Dire Straits - Alchemy-Live 1983")

    assert meta.artists == ["Dire Straits"]
    assert meta.title == "Alchemy-Live"
    assert meta.year == 1983


def test_apply_title_lossless_declaration():
    """「无损」声明词应剥离出标题并推断无损音质。"""
    meta = parse_title("某某 - 晴天 FLAC 无损")

    assert meta.title == "晴天"
    assert meta.audio_lossless is True


def test_apply_title_track_prefix_in_title():
    """标题中的曲序前缀应提取为曲序并还原曲名。"""
    meta = parse_title("01.晴天")

    assert meta.track_number == 1
    assert meta.title == "晴天"


def test_apply_title_bracket_date_prefix():
    """发行日期方括号前缀应整体剔除。"""
    meta = parse_title("[250917] 某歌手 - 某曲名")

    assert meta.artists == ["某歌手"]
    assert meta.title == "某曲名"


def test_apply_title_spec_segments_do_not_shift_split():
    """尾部规格段（WEB-DL/位深/发布组）不应把拆分点推到艺术家与曲名的连字符上。"""
    meta = parse_title(
        "许茹芸 - 等得到 (电影《如影随心》主题曲 独唱版) (2019) - WEB-DL - 24bit ALAC-HHWEB")

    assert meta.artists == ["许茹芸"]
    # 含书名号的括号注释是版本说明，应拼回曲名而不触发专辑结构
    assert meta.title == "等得到 (电影《如影随心》主题曲 独唱版)"
    assert meta.year == 2019


def test_apply_title_album_marker_with_song_artist_prefix():
    """书名号前的「曲名-歌手」连字符段应反向拆分，首段作为曲名线索。"""
    meta = parse_title("为你盛开-许巍《无尽光芒》2019.FLAC")

    assert meta.artists == ["许巍"]
    assert meta.album == "无尽光芒"
    assert meta.title == "为你盛开"
    assert meta.year == 2019


def test_apply_title_album_marker_rest_disc_number():
    """书名号后仅剩碟号时应提取为碟号，曲名回退用专辑名。"""
    meta = parse_title("李宗盛《理性与感性作品音乐会》 CD2 (2006)")

    assert meta.artists == ["李宗盛"]
    assert meta.album == "理性与感性作品音乐会"
    assert meta.disc_number == 2
    assert meta.title == "理性与感性作品音乐会"
    assert meta.year == 2006


def test_apply_title_keeps_single_paren_disambiguation():
    """单层括号注释是条目消歧后缀，应保留在曲名中。"""
    meta = parse_title("周杰伦 - 晴天 (电影版)")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天 (电影版)"


def test_apply_title_collection_with_space_sample_rate():
    """合集/精选是发行形态标记，空格写法的采样率（44 1khz）也应剔除；
    剔除后仅剩悬空分隔符时艺术家仍需拆出。"""
    meta = parse_title("周杰伦 - 合集  2000-2022 - FLAC 16bit 44 1khz")

    assert meta.artists == ["周杰伦"]
    assert meta.title is None
    assert meta.year == 2022
    assert meta.audio_format == "FLAC"
