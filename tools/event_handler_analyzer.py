#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EventHandler 队列分析与 Binder 链路追踪。

用于 ANR/Freeze 场景的核心分析工具：
1. 解析 EventHandler 消息队列，找出阻塞主线程的耗时任务
2. 追踪 Binder/IPC 调用链，定位跨进程阻塞的真正源头
3. 检测死锁环

支持：
- 旧式 ``Current Running: task, start := ms`` 文本
- 鸿蒙 AppFreeze ``EventHandler dump``（``start at 时间戳, Event { task name = ... }``）

参考华为 DFX Skills appfreeze-analysis 的设计。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple


# =====================================================================
# EventHandler 队列分析
# =====================================================================

@dataclass
class QueuedEvent:
    """队列中的一个事件/任务。"""
    name: str
    trigger_time: str = ""
    complete_time: str = ""
    duration_ms: float = 0.0
    is_running: bool = False
    priority: str = ""
    caller: str = ""
    send_thread: str = ""


@dataclass
class EventHandlerAnalysis:
    """EventHandler 队列分析结果。"""
    current_running: Optional[QueuedEvent] = None
    running_duration_ms: float = 0.0
    long_events: List[QueuedEvent] = field(default_factory=list)  # >1s 的耗时任务
    queue_depth: int = 0
    is_blocked: bool = False
    blocking_cause: str = ""
    dump_cur_time: str = ""
    pending_by_priority: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        cur = None
        if self.current_running:
            cur = {
                "name": self.current_running.name,
                "duration_ms": self.running_duration_ms,
                "caller": self.current_running.caller or None,
                "send_thread": self.current_running.send_thread or None,
                "start_at": self.current_running.trigger_time or None,
                "priority": self.current_running.priority or None,
            }
        return {
            "current_running": cur,
            "long_events": [
                {
                    "name": e.name,
                    "duration_ms": e.duration_ms,
                    "priority": e.priority or None,
                    "caller": e.caller or None,
                }
                for e in self.long_events[:5]
            ],
            "queue_depth": self.queue_depth,
            "pending_by_priority": dict(self.pending_by_priority),
            "dump_cur_time": self.dump_cur_time or None,
            "is_blocked": self.is_blocked,
            "blocking_cause": self.blocking_cause,
        }

    def render_markdown(self) -> str:
        if not self.current_running and not self.long_events and not self.pending_by_priority:
            return ""
        lines = ["EventHandler 队列分析:"]
        if self.current_running:
            dur = self.running_duration_ms
            extra = []
            if self.current_running.caller:
                extra.append(f"caller={self.current_running.caller}")
            if self.current_running.send_thread:
                extra.append(f"send_tid={self.current_running.send_thread}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(
                f"- **当前执行**: `{self.current_running.name}` "
                f"(已执行 {dur:.0f}ms){suffix}"
            )
        if self.is_blocked:
            lines.append(f"- **阻塞原因**: {self.blocking_cause}")
        if self.pending_by_priority:
            pending = ", ".join(
                f"{k}={v}" for k, v in sorted(self.pending_by_priority.items()) if v
            )
            if pending:
                lines.append(f"- **待处理队列**: {pending}（合计 {self.queue_depth}）")
        if self.long_events:
            lines.append("- **耗时任务**:")
            for e in self.long_events[:5]:
                lines.append(f"  - {e.name}: {e.duration_ms:.0f}ms")
        return "\n".join(lines)


# =====================================================================
# Binder/IPC 链路追踪
# =====================================================================

@dataclass
class BinderCall:
    """一次 Binder 通信。"""
    src_pid: str
    src_tid: str
    dst_pid: str
    dst_tid: str
    interface: str = ""
    wait_time_ms: float = 0.0
    state: str = ""  # "waiting" / "reply" / "dead"


@dataclass
class BinderChain:
    """Binder 调用链（故障传播路径）。"""
    chain: List[BinderCall] = field(default_factory=list)
    has_deadlock: bool = False
    deadlock_cycle: List[str] = field(default_factory=list)  # [tid1, tid2, ...]
    root_blocker: str = ""  # 最终阻塞源
    total_wait_ms: float = 0.0
    ipc_thread_count: int = 0
    note_zh: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_length": len(self.chain),
            "has_deadlock": self.has_deadlock,
            "deadlock_cycle": self.deadlock_cycle,
            "root_blocker": self.root_blocker,
            "total_wait_ms": self.total_wait_ms,
            "ipc_thread_count": self.ipc_thread_count,
            "note_zh": self.note_zh or None,
            "calls": [
                {
                    "src": f"{c.src_pid}:{c.src_tid}",
                    "dst": f"{c.dst_pid}:{c.dst_tid}",
                    "interface": c.interface,
                    "wait_ms": c.wait_time_ms,
                }
                for c in self.chain[:10]
            ],
        }

    def render_markdown(self) -> str:
        if not self.chain and not self.ipc_thread_count:
            return ""
        lines = ["Binder/IPC 链路分析:"]
        if self.has_deadlock:
            lines.append(f"- ⚠️ **检测到死锁环**: {' → '.join(self.deadlock_cycle)}")
        if self.root_blocker:
            lines.append(f"- **根源阻塞者**: {self.root_blocker}")
        if self.chain:
            lines.append(f"- 链路长度: {len(self.chain)}, 总等待: {self.total_wait_ms:.0f}ms")
            lines.append("")
            lines.append("| 来源 | 目标 | 接口 | 等待时间 |")
            lines.append("|------|------|------|----------|")
            for c in self.chain[:8]:
                lines.append(
                    f"| {c.src_pid}:{c.src_tid} | {c.dst_pid}:{c.dst_tid} | "
                    f"{c.interface[:30]} | {c.wait_time_ms:.0f}ms |"
                )
        elif self.ipc_thread_count:
            lines.append(
                f"- 日志中出现 {self.ipc_thread_count} 个 OS_IPC 线程"
                "（未见显式 binder wait 链路文本）"
            )
            if self.note_zh:
                lines.append(f"- {self.note_zh}")
        return "\n".join(lines)


# =====================================================================
# 解析器实现
# =====================================================================

# 旧式 Current Running
_RUNNING_LEGACY_RE = re.compile(
    r"Current Running:\s*(.+?)(?:,\s*start\s*:?=\s*(\d+))?$",
    re.IGNORECASE | re.MULTILINE,
)
# 鸿蒙: Current Running: start at <ts>, Event { ... }
_RUNNING_OHOS_RE = re.compile(
    r"Current Running:\s*start\s+at\s+"
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s*,\s*"
    r"Event\s*\{([^}]*)\}",
    re.IGNORECASE,
)
_CURTIME_RE = re.compile(
    r"EventHandler\s+dump\s+begin\s+curTime:\s*"
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)",
    re.IGNORECASE,
)
_TASK_NAME_RE = re.compile(r"task\s*name\s*=\s*([^,}]+)", re.IGNORECASE)
_CALLER_RE = re.compile(r"caller\s*=\s*\[([^\]]*)\]", re.IGNORECASE)
_SEND_THREAD_RE = re.compile(r"send\s*thread\s*=\s*(\d+)", re.IGNORECASE)
_PRIORITY_RE = re.compile(r"priority\s*=\s*([^,}]+)", re.IGNORECASE)
_TRIGGER_TIME_RE = re.compile(
    r"trigger\s*time\s*=\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)",
    re.IGNORECASE,
)
_COMPLETE_TIME_RE = re.compile(
    r"completeTime\s*time\s*=\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)?",
    re.IGNORECASE,
)
_HANDLE_TIME_RE = re.compile(
    r"handle\s*time\s*=\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)",
    re.IGNORECASE,
)
_OHOS_EVENT_LINE_RE = re.compile(
    r"No\.\s*\d+\s*:\s*Event\s*\{([^}]*)\}",
    re.IGNORECASE,
)
_QUEUE_SIZE_RE = re.compile(
    r"Total\s+size\s+of\s+(\w+)\s+events\s*:\s*(\d+)",
    re.IGNORECASE,
)
_HISTORY_EVENT_RE = re.compile(
    r"(?:task|event):\s*(.+?)\s*,?\s*trigger\s*:?=\s*(\d+)\s*,?\s*complete\s*:?=\s*(\d+)",
    re.IGNORECASE,
)

# Binder patterns
_BINDER_WAIT_RE = re.compile(
    r"(?:binder|ipc)\s+(?:call|wait).*?from\s+(\d+):(\d+)\s+to\s+(\d+):(\d+)",
    re.IGNORECASE,
)
_BINDER_TRANSACTION_RE = re.compile(
    r"(\d+):(\d+)\s*(?:→|->|to)\s*(\d+):(\d+)\s+(\S+)\s+(?:wait|pending)\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)
_IPC_THREAD_RE = re.compile(
    r"Tid:(\d+),\s*Name:(OS_IPC[^\s,]*)",
    re.IGNORECASE,
)


def _parse_ohos_ts(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _delta_ms(start: Optional[datetime], end: Optional[datetime]) -> float:
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _field_from_event_body(body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    m = _TASK_NAME_RE.search(body or "")
    if m:
        out["name"] = m.group(1).strip()
    m = _CALLER_RE.search(body or "")
    if m:
        out["caller"] = m.group(1).strip()
    m = _SEND_THREAD_RE.search(body or "")
    if m:
        out["send_thread"] = m.group(1).strip()
    m = _PRIORITY_RE.search(body or "")
    if m:
        out["priority"] = m.group(1).strip()
    m = _TRIGGER_TIME_RE.search(body or "")
    if m:
        out["trigger"] = m.group(1).strip()
    m = _HANDLE_TIME_RE.search(body or "")
    if m:
        out["handle"] = m.group(1).strip()
    m = _COMPLETE_TIME_RE.search(body or "")
    if m and m.group(1):
        out["complete"] = m.group(1).strip()
    return out


class EventHandlerAnalyzer:
    """EventHandler 队列解析与分析。"""

    def parse_from_log(
        self,
        log_content: str,
        freeze_threshold_ms: float = 3000.0,
    ) -> EventHandlerAnalysis:
        """从日志中解析 EventHandler 队列信息。

        Args:
            log_content: ANR/Freeze 日志文本
            freeze_threshold_ms: 认定为阻塞的时间阈值

        Returns:
            EventHandlerAnalysis 结果
        """
        result = EventHandlerAnalysis()
        text = log_content or ""

        # dump curTime（取最后一次 dump，通常更接近 freeze 时刻）
        cur_times = list(_CURTIME_RE.finditer(text))
        cur_dt: Optional[datetime] = None
        if cur_times:
            result.dump_cur_time = cur_times[-1].group(1).strip()
            cur_dt = _parse_ohos_ts(result.dump_cur_time)

        # 1) 鸿蒙 Current Running
        ohos_runs = list(_RUNNING_OHOS_RE.finditer(text))
        if ohos_runs:
            m = ohos_runs[-1]
            start_s = m.group(1).strip()
            fields = _field_from_event_body(m.group(2))
            name = fields.get("name") or "unknown_event"
            event = QueuedEvent(
                name=name,
                is_running=True,
                trigger_time=start_s,
                caller=fields.get("caller", ""),
                send_thread=fields.get("send_thread", ""),
                priority=fields.get("priority", ""),
            )
            start_dt = _parse_ohos_ts(start_s)
            result.running_duration_ms = _delta_ms(start_dt, cur_dt)
            result.current_running = event
        else:
            # 2) 旧式 Current Running（避免误匹配鸿蒙行）
            for m in _RUNNING_LEGACY_RE.finditer(text):
                raw = m.group(1).strip()
                if raw.lower().startswith("start at"):
                    continue
                # 截断 Event { 之前的短任务名
                task_name = raw.split(",")[0].strip()
                if "Event {" in task_name:
                    continue
                start_time = m.group(2)
                event = QueuedEvent(name=task_name or raw[:80], is_running=True)
                if start_time:
                    event.trigger_time = start_time
                    # 旧格式 start 为绝对/相对 ms 数字，无法可靠换算 duration
                result.current_running = event

        # 3) 鸿蒙 History / 待处理队列 Event 行
        events: List[QueuedEvent] = []
        for m in _OHOS_EVENT_LINE_RE.finditer(text):
            fields = _field_from_event_body(m.group(1))
            name = fields.get("name") or ""
            if not name:
                # watchdog Timer 等可能只有 id/caller
                if fields.get("caller"):
                    name = fields["caller"].split("(")[0].strip() or "anonymous_event"
                else:
                    continue
            trigger_s = fields.get("trigger") or fields.get("handle") or ""
            complete_s = fields.get("complete") or ""
            duration = _delta_ms(_parse_ohos_ts(trigger_s), _parse_ohos_ts(complete_s))
            # 未完成且与 dump curTime 可比
            if duration <= 0 and trigger_s and cur_dt and not complete_s:
                duration = _delta_ms(_parse_ohos_ts(trigger_s), cur_dt)
            event = QueuedEvent(
                name=name,
                trigger_time=trigger_s,
                complete_time=complete_s,
                duration_ms=duration,
                priority=fields.get("priority", ""),
                caller=fields.get("caller", ""),
                send_thread=fields.get("send_thread", ""),
            )
            events.append(event)
            if duration > freeze_threshold_ms:
                result.long_events.append(event)

        # 4) 旧式 history
        if not events:
            for m in _HISTORY_EVENT_RE.finditer(text):
                name = m.group(1).strip()
                trigger = int(m.group(2))
                complete = int(m.group(3))
                duration = float(complete - trigger)
                event = QueuedEvent(
                    name=name,
                    trigger_time=str(trigger),
                    complete_time=str(complete),
                    duration_ms=duration,
                )
                events.append(event)
                if duration > freeze_threshold_ms:
                    result.long_events.append(event)

        # 5) 待处理队列深度（Total size of High events : N）
        pending: Dict[str, int] = {}
        for m in _QUEUE_SIZE_RE.finditer(text):
            key = m.group(1).strip()
            pending[key] = int(m.group(2))
        result.pending_by_priority = pending
        pending_sum = sum(pending.values())
        # queue_depth：优先待处理合计；否则历史事件数（去重 dump 时可能翻倍，取一半上限）
        if pending_sum > 0:
            result.queue_depth = pending_sum
        else:
            result.queue_depth = len(events)

        # 长任务按 duration 降序，去重同名保留最长
        if result.long_events:
            best: Dict[str, QueuedEvent] = {}
            for e in result.long_events:
                prev = best.get(e.name)
                if prev is None or e.duration_ms > prev.duration_ms:
                    best[e.name] = e
            result.long_events = sorted(
                best.values(), key=lambda x: x.duration_ms, reverse=True
            )

        # Determine if blocked
        if result.current_running and result.running_duration_ms > freeze_threshold_ms:
            result.is_blocked = True
            result.blocking_cause = (
                f"当前任务 '{result.current_running.name}' 已执行 "
                f"{result.running_duration_ms:.0f}ms（阈值 {freeze_threshold_ms:.0f}ms）"
            )
        elif result.long_events:
            result.is_blocked = True
            result.blocking_cause = f"历史存在 {len(result.long_events)} 个耗时任务"
        elif pending_sum >= 5 and result.current_running:
            result.is_blocked = True
            result.blocking_cause = (
                f"当前任务 '{result.current_running.name}' 执行中，"
                f"仍有 {pending_sum} 个待处理事件堆积"
            )

        return result


class BinderChainTracer:
    """Binder/IPC 调用链追踪。"""

    def trace_from_log(
        self,
        log_content: str,
        fault_tid: str = "",
    ) -> BinderChain:
        """从日志中追踪 Binder 调用链。

        Args:
            log_content: 包含 Binder 通信记录的日志
            fault_tid: 故障线程 TID（作为追踪起点）

        Returns:
            BinderChain 结果
        """
        result = BinderChain()
        text = log_content or ""

        ipc_names = {m.group(2) for m in _IPC_THREAD_RE.finditer(text)}
        result.ipc_thread_count = len(ipc_names)
        if result.ipc_thread_count and "BinderInvoker" in text:
            result.note_zh = (
                "存在 BinderInvoker/IPC 工作线程栈，但缺少显式 from→to wait 文本；"
                "请结合主线程 EventHandler 与 IPC 线程栈判断是否跨进程阻塞。"
            )

        # Parse all binder calls
        calls: List[BinderCall] = []
        for m in _BINDER_WAIT_RE.finditer(text):
            calls.append(BinderCall(
                src_pid=m.group(1),
                src_tid=m.group(2),
                dst_pid=m.group(3),
                dst_tid=m.group(4),
            ))
        for m in _BINDER_TRANSACTION_RE.finditer(text):
            calls.append(BinderCall(
                src_pid=m.group(1),
                src_tid=m.group(2),
                dst_pid=m.group(3),
                dst_tid=m.group(4),
                interface=m.group(5),
                wait_time_ms=float(m.group(6)),
            ))

        if not calls:
            return result

        result.chain = calls
        result.total_wait_ms = sum(c.wait_time_ms for c in calls)

        # Build adjacency graph for deadlock detection
        graph: Dict[str, List[str]] = defaultdict(list)
        for call in calls:
            src_key = f"{call.src_pid}:{call.src_tid}"
            dst_key = f"{call.dst_pid}:{call.dst_tid}"
            graph[src_key].append(dst_key)

        # Detect cycles (deadlock)
        cycle = self._detect_cycle(graph)
        if cycle:
            result.has_deadlock = True
            result.deadlock_cycle = cycle

        # Find root blocker (end of chain with no outgoing waits)
        if fault_tid:
            root = self._find_root_blocker(graph, fault_tid)
            result.root_blocker = root

        return result

    def _detect_cycle(self, graph: Dict[str, List[str]]) -> List[str]:
        """DFS cycle detection in directed graph."""
        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        path: List[str] = []

        def dfs(node: str) -> List[str]:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    result = dfs(neighbor)
                    if result:
                        return result
                elif neighbor in rec_stack:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    return path[cycle_start:] + [neighbor]

            path.pop()
            rec_stack.discard(node)
            return []

        for node in graph:
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return []

    def _find_root_blocker(self, graph: Dict[str, List[str]], start: str) -> str:
        """从故障线程出发，沿图遍历找到最终阻塞源。"""
        visited: Set[str] = set()
        current = start
        while current not in visited:
            visited.add(current)
            neighbors = graph.get(current, [])
            if not neighbors:
                return current  # No outgoing = root blocker
            current = neighbors[0]
        return current  # Cycled back = part of deadlock
