import unittest

from tests.test_cookiecloud import CookieCloudTest
from tests.test_filter import FilterTest
from tests.test_metainfo import MetaInfoTest
from tests.test_recognize import RecognizeTest
from tests.test_transfer import TransferTest

if __name__ == '__main__':
    suite = unittest.TestSuite()

    # 测试过滤器
    suite.addTest(FilterTest('test_filter'))
    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    # 测试媒体识别
    suite.addTest(RecognizeTest('test_recognize'))
    # 测试CookieCloud同步
    suite.addTest(CookieCloudTest('test_cookiecloud'))
    # 测试文件转移
    suite.addTest(TransferTest('test_transfer'))

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
