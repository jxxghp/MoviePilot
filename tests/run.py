import unittest

from tests.test_bluray import BluRayTest
from tests.test_mediascrape import (
    TestMediaScrapingPaths,
    TestMediaScrapingNFO,
    TestMediaScrapingImages,
    TestMediaScrapingTVDirectory,
    TestMediaScrapeEvents
)
from tests.test_metainfo import MetaInfoTest
from tests.test_object import ObjectUtilsTest
from tests.test_subscribe_chain import SubscribeChainTest


if __name__ == '__main__':
    suite = unittest.TestSuite()

    # 测试名称识别
    suite.addTest(MetaInfoTest('test_metainfo'))
    suite.addTest(MetaInfoTest('test_emby_format_ids'))
    suite.addTest(ObjectUtilsTest('test_check_method'))

    # 测试自定义识别词功能
    suite.addTest(MetaInfoTest('test_metainfopath_with_custom_words'))
    suite.addTest(MetaInfoTest('test_metainfopath_without_custom_words'))
    suite.addTest(MetaInfoTest('test_metainfopath_with_empty_custom_words'))
    suite.addTest(MetaInfoTest('test_custom_words_apply_words_recording'))

    # 测试蓝光目录识别
    suite.addTest(BluRayTest())

    # 测试媒体刮削
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestMediaScrapingPaths))
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestMediaScrapingNFO))
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestMediaScrapingImages))
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestMediaScrapingTVDirectory))
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestMediaScrapeEvents))

    # 测试订阅洗版匹配
    suite.addTest(SubscribeChainTest('test_is_episode_range_covered'))

    # 运行测试
    runner = unittest.TextTestRunner()
    runner.run(suite)
