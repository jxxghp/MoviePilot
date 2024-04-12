import unittest

from tests.test_metainfo import MetaInfoTest

if __name__ == '__main__':
    suite = unittest.TestSuite()

    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
