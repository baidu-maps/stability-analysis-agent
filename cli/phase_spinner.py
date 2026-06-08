#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段进度 spinner：在终端同一行循环显示 . .. ... 防止用户误以为卡死。"""

import sys
import time
import threading
from typing import Optional


class PhaseSpinner:
    """阶段进度指示器（context manager）。

    用法::

        with PhaseSpinner("堆栈符号化", step=2, total_steps=5) as sp:
            do_work()
        # 自动打印: [阶段 2/5] 堆栈符号化 ✓ 0.03s

    LLM 阶段可调用 sp.set_tokens(input_tokens, output_tokens) 在完成行追加 token 统计。
    """

    _DOTS = [".", "..", "..."]
    _INTERVAL = 0.5  # 刷新间隔（秒）

    def __init__(self, label: str, step: int, total_steps: int):
        self.label = label
        self.step = step
        self.total_steps = total_steps
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time: float = 0.0
        self._input_tokens: Optional[int] = None
        self._output_tokens: Optional[int] = None
        self._partial_failure: bool = False
        self._is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    # ---- public API ----

    def set_partial_failure(self, failed: bool = True) -> None:
        """阶段内业务失败但未抛异常时，结束行显示 ✗ 而非 ✓。"""
        self._partial_failure = bool(failed)

    def set_tokens(self, input_tokens: Optional[int] = None, output_tokens: Optional[int] = None):
        """设置 token 统计（在阶段结束前调用）。"""
        if input_tokens is not None:
            self._input_tokens = input_tokens
        if output_tokens is not None:
            self._output_tokens = output_tokens

    # ---- context manager ----

    def __enter__(self):
        self._start_time = time.time()
        if self._is_tty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # 非 TTY：仅打印起始行
            sys.stdout.write(f"[阶段 {self.step}/{self.total_steps}] {self.label}...\n")
            sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        elapsed = time.time() - self._start_time
        failed = exc_type is not None or self._partial_failure
        self._print_done(elapsed, error=failed)
        return False  # 不吞异常

    # ---- internal ----

    def _spin(self):
        prefix = f"[阶段 {self.step}/{self.total_steps}] {self.label}"
        idx = 0
        while not self._stop.is_set():
            dots = self._DOTS[idx % 3]
            line = f"\r{prefix}{dots}"
            # 用空格覆盖上一次较长的尾部残留
            sys.stdout.write(line + "   ")
            sys.stdout.flush()
            idx += 1
            self._stop.wait(self._INTERVAL)

    def _print_done(self, elapsed: float, error: bool = False):
        mark = "✗" if error else "✓"
        time_str = self._fmt_time(elapsed)
        line = f"[阶段 {self.step}/{self.total_steps}] {self.label} {mark} {time_str}"
        if self._input_tokens is not None or self._output_tokens is not None:
            inp = self._fmt_tokens(self._input_tokens)
            out = self._fmt_tokens(self._output_tokens)
            line += f" (输入 {inp} / 输出 {out} tokens)"
        if self._is_tty:
            # 覆盖 spinner 行
            sys.stdout.write(f"\r{line}   \n")
        else:
            sys.stdout.write(f"{line}\n")
        sys.stdout.flush()

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        if seconds < 0.01:
            return "0.01s"
        if seconds < 1:
            return f"{seconds:.2f}s"
        if seconds < 60:
            return f"{seconds:.1f}s"
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"

    @staticmethod
    def _fmt_tokens(count: Optional[int]) -> str:
        if count is None or count == 0:
            return "0"
        if count >= 10000:
            return f"{count / 1000:.1f}k"
        return f"{count:,}"
