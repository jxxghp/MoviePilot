#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
用于蓝光目录识别测试的虚拟文件树。

条目为名称与子项列表或字节大小组成的元组，None 表示空目录；测试据此构造内存中的文件项树。
"""

bluray_files = [
    (
        "FOLDER",
        [
            (
                "Digimon",
                [
                    (
                        "Digimon BluRay (2055)",
                        [
                            (
                                "BDMV",
                                [
                                    (
                                        "STREAM",
                                        [
                                            ("00000.m2ts", 104857600),
                                            ("00001.m2ts", 104857600),
                                        ],
                                    ),
                                ],
                            ),
                            ("CERTIFICATE", None),
                        ],
                    ),
                    (
                        "Digimon BluRay (2099)",
                        [
                            (
                                "BDMV",
                                [
                                    (
                                        "STREAM",
                                        [
                                            ("00000.m2ts", 104857600),
                                            ("00001.m2ts", 104857600),
                                            ("00002.m2ts.!qB", 104857600),
                                        ],
                                    ),
                                ],
                            ),
                            ("CERTIFICATE", None),
                        ],
                    ),
                    ("Digimon (2199)", [("Digimon.2199.mp4", 104857600)]),
                ],
            ),
            (
                "Pokemon BluRay (2016)",
                [
                    (
                        "BDMV",
                        [
                            (
                                "STREAM",
                                [
                                    ("00000.m2ts", 104857600),
                                    ("00001.m2ts", 104857600),
                                ],
                            )
                        ],
                    ),
                    ("CERTIFICATE", None),
                ],
            ),
            (
                "Pokemon BluRay (2021)",
                [
                    (
                        "BDMV",
                        [
                            (
                                "STREAM",
                                [
                                    ("00000.m2ts", 104857600),
                                    ("00001.m2ts", 104857600),
                                ],
                            )
                        ],
                    ),
                    ("CERTIFICATE", None),
                ],
            ),
            (
                "Pokemon (2028)",
                [
                    ("Pokemon.2028.mkv", 104857600),
                    ("Pokemon.2028.hdr.mkv.!qB", 104857600),
                ],
            ),
            ("Pokemon.2029.mp4", 104857600),
            ("Pokemon.2039.mp4", 104857600),
            ("Pokemon (2030)", [("S", 104857600)]),
            ("Pokemon (2031)", [("Pokemon (2031).mp4", 104857600)]),
        ],
    )
]
