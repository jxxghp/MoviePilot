import unittest

from tests.test_metainfo import MetaInfoTest
from tests.test_recognize import RecognizeTest

if __name__ == '__main__':
    suite = unittest.TestSuite()
    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    # 测试媒体识别
    suite.addTest(RecognizeTest('test_recognize'))
    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
