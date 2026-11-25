"""
离线音频容器文件构造；不注册测试用例。
"""

import struct
import wave
from pathlib import Path


def _write_wave(path: Path) -> Path:
    """
    生成三秒双声道 PCM，避免测试依赖本机 ffmpeg。
    """
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b"\0" * 44100 * 4 * 3)
    return path


def _write_dsf(path: Path) -> Path:
    """
    生成带单个双声道 DSD 数据块的合法 DSF，供实际 ID3 读写使用。
    """
    payload = b"\x69" * 8192
    header = struct.pack("<4sQQQ", b"DSD ", 28, 92 + len(payload), 0)
    fmt = struct.pack("<4sQIIIIIIQII", b"fmt ", 52, 1, 0, 2, 2, 2822400, 1, 32768, 4096, 0)
    path.write_bytes(header + fmt + struct.pack("<4sQ", b"data", 12 + len(payload)) + payload)
    return path


def _write_aiff(path: Path) -> Path:
    """
    标准FORM/COMM/SSND块生成三秒44.1kHz PCM，采样率使用AIFF扩展浮点格式。
    """
    common = struct.pack(">hIh", 2, 44100 * 3, 16) + bytes.fromhex("400eac44000000000000")
    sound = b"\0" * (8 + 44100 * 4 * 3)
    chunks = b"COMM" + struct.pack(">I", len(common)) + common + b"SSND" + struct.pack(">I", len(sound)) + sound
    path.write_bytes(b"FORM" + struct.pack(">I", len(chunks) + 4) + b"AIFF" + chunks)
    return path


def _write_dff(path: Path) -> Path:
    """
    生成合法DSDIFF声道交织DSD数据及PROP，验证与DSF不同的原生ID3容器。
    """
    def chunk(name: bytes, data: bytes) -> bytes:
        """
        DSDIFF块以64位大端长度计数，奇数字节补齐不计入内容长度。
        """
        return name + struct.pack(">Q", len(data)) + data + b"\0" * (len(data) % 2)

    properties = b"SND " + chunk(b"FS  ", struct.pack(">I", 2822400))
    properties += chunk(b"CHNL", struct.pack(">H", 2) + b"SLFTSRGT") + chunk(b"CMPR", b"DSD \x00")
    content = b"DSD " + chunk(b"FVER", struct.pack(">I", 0x01050000)) + chunk(b"PROP", properties)
    content += chunk(b"DSD ", b"\x69" * (2822400 * 3 * 2 // 8))
    path.write_bytes(chunk(b"FRM8", content))
    return path
