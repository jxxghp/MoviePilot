import unittest

from tests.test_bluray import BluRayTest
from tests.test_metainfo import MetaInfoTest

if __name__ == '__main__':
    suite = unittest.TestSuite()

    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    suite.addTest(MetaInfoTest('test_emby_format_ids'))

    # 测试蓝光目录识别
    suite.addTest(BluRayTest())

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
