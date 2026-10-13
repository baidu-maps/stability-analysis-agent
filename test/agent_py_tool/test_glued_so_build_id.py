#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精简栈把 BuildId 粘在 .so 后时，模块名只保留库文件名。"""

import json
import unittest

from tools.crash_log_parser_tool import crash_log_parser


GLUED_LOG = """\
SIGSEGV: Segfault

#00 pc 00000000004926b4 /data/app/x/lib/arm64/libBaiduMapSDK_map_v7_6_6.so[f0040528c6875de8345d6e3dc7109cc1af4ee389]

#01 pc 0000000000099fec /apex/com.android.runtime/lib64/bionic/libc.so[28057735687b475a45ee7ab9afcb5eb7]
"""

SPACED_LOG = """\
#00 pc 000000000073bcdc /data/app/app/lib/arm64/libDemoMapSDK_core_v7_6_1.so [arm64-v8a::1cf7a8708f9a04a37f85cdffa7983068]
"""


class TestGluedSoBuildId(unittest.TestCase):
    """模块名应是日志路径里的 .so 文件，不包含紧贴的 BuildId。"""

    def test_glued_build_id_is_not_part_of_the_module_name(self) -> None:
        """粘在 .so 后的 BuildId 不进入模块名。"""
        parsed = json.loads(crash_log_parser(GLUED_LOG))
        frames = parsed["threads"][0]["frames"]
        self.assertEqual(
            [frame["module"] for frame in frames],
            ["libBaiduMapSDK_map_v7_6_6.so", "libc.so"],
        )

    def test_spaced_arch_tag_still_strips_to_the_so_name(self) -> None:
        """空格后的 ``[arm64-v8a::buildId]`` 仍然不进入模块名。"""
        parsed = json.loads(crash_log_parser(SPACED_LOG))
        module = parsed["threads"][0]["frames"][0]["module"]
        self.assertEqual(module, "libDemoMapSDK_core_v7_6_1.so")


if __name__ == "__main__":
    unittest.main()
