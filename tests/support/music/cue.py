"""
真实整轨音频与 CUE 回放的共享工厂；不注册测试用例。
"""

import shutil
from pathlib import Path

CUE = '''REM DATE 2003
PERFORMER "测试歌手"
TITLE "专辑示例"
FILE "image.flac" WAVE
  TRACK 01 AUDIO
    TITLE "第一首歌曲"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "第二首歌曲"
    INDEX 00 00:00:70
    INDEX 01 00:01:00
'''


def _image_pair(tmp_path, *, encoding="utf-8", text=CUE, cue_name="album.cue"):
    """
    创建三秒实际整轨 FLAC 和两个逻辑曲目的 CUE。
    """
    source = tmp_path / "source"
    source.mkdir()
    audio = source / "image.flac"
    cue = source / cue_name
    shutil.copyfile(Path(__file__).parents[2] / "fixtures/audio/silence.flac", audio)
    cue.write_bytes(text.encode(encoding))
    return audio, cue
