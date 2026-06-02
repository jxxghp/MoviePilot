#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
TemplateContextBuilder 的并发安全单元测试。

历史上 builder 持有 ``self._context`` 实例字段，``build()`` 内 ``clear()`` →
``_add_*`` → 推导式返回这一序列在 ``TRANSFER_THREADS > 1`` 下会被多线程相互
覆盖，导致同一 builder 实例并发调用产生互相串味的 rename_dict。本测试在多
线程下连续调用 ``build()``，校验每个线程拿到的字典只反映自己的入参。
"""
import threading
import unittest

from app.helper.message import TemplateContextBuilder


class TemplateContextBuilderConcurrencyTest(unittest.TestCase):
    """
    使用 8 个线程并发调用同一 TemplateContextBuilder 实例的 build()，
    确保各自的 file_extension / 自定义 kwargs 不会被其它线程覆盖。
    """

    THREAD_COUNT = 8
    ITERATIONS_PER_THREAD = 200

    def test_concurrent_build_no_cross_contamination(self):
        builder = TemplateContextBuilder()
        errors = []

        def worker(tag: int) -> None:
            try:
                for _ in range(self.ITERATIONS_PER_THREAD):
                    ctx = builder.build(
                        file_extension=f".{tag}",
                        marker=tag,
                    )
                    self.assertEqual(ctx.get("fileExt"), f".{tag}")
                    self.assertEqual(ctx.get("marker"), tag)
            except AssertionError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,), name=f"builder-{i}")
            for i in range(self.THREAD_COUNT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(
            errors,
            msg=f"检测到并发串味，共 {len(errors)} 条；首个错误：{errors[0] if errors else ''}",
        )

    def test_build_returns_independent_dicts(self):
        """
        即便不开线程，连续两次 build() 也应当返回相互独立的 dict 实例，
        避免无状态化后调用方误以为返回的还是 builder 内部共享对象。
        """
        builder = TemplateContextBuilder()
        first = builder.build(file_extension=".a", marker=1)
        second = builder.build(file_extension=".b", marker=2)
        self.assertIsNot(first, second)
        self.assertEqual(first.get("fileExt"), ".a")
        self.assertEqual(second.get("fileExt"), ".b")
        # 第二次调用不应反向污染第一次的结果
        self.assertEqual(first.get("marker"), 1)

    def test_build_exposes_video_bit_from_meta(self):
        """
        模板上下文应提供独立 videoBit 字段，避免用户只能从 videoCodec 中手工拆位深。
        """
        meta = type("FakeMeta", (), {})()
        meta.begin_episode = None
        meta.title = "Movie.2024.1080p.x265.10bit.mkv"
        meta.name = "Movie"
        meta.en_name = "Movie"
        meta.year = "2024"
        meta.season_seq = ""
        meta.season = ""
        meta.episode_seqs = ""
        meta.episode = ""
        meta.part = None
        meta.customization = None
        meta.fps = None
        meta.resource_type = None
        meta.resource_effect = None
        meta.edition = ""
        meta.resource_pix = "1080p"
        meta.resource_term = "1080p"
        meta.resource_team = None
        meta.video_encode = "x265 10bit"
        meta.video_bit = "10bit"
        meta.audio_encode = "AAC"
        meta.web_source = None

        context = TemplateContextBuilder().build(meta=meta)

        self.assertEqual(context.get("videoCodec"), "x265 10bit")
        self.assertEqual(context.get("videoBit"), "10bit")
