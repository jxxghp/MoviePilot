"""
验证中文分词工具的词语拆分和兼容行为。
"""

from app.foundation.text import convert, cut


def test_cut_accepts_hmm_argument():
    """
    验证分词入口支持公开的 HMM 参数名。
    """
    words = cut("台湾后台测试", HMM=False)

    assert "".join(words) == "台湾后台测试"
    assert "后台" in words


def test_cut_preserves_full_mode_contract():
    """
    全模式保留原生分词的完整候选顺序与内容。
    """
    assert cut("南京市长江大桥", cut_all=True) == [
        "南京",
        "南京市",
        "京市",
        "市长",
        "长江",
        "长江大桥",
        "大桥",
    ]


def test_convert_uses_rust_mediawiki_contract():
    """
    标准与 free-threaded 运行时应共享同一中文转换语义。
    """
    assert convert("后台", "zh-hant") == "後台"
