from app.foundation.text import cut


def test_cut_accepts_legacy_hmm_argument():
    """验证分词封装支持旧 jieba.cut 的 HMM 参数名。"""
    words = cut("台湾后台测试", HMM=False)

    assert "".join(words) == "台湾后台测试"
    assert "后台" in words
