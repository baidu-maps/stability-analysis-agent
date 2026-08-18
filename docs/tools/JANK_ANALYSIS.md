# Trace / Jank 分析

当前 Agent 提供独立的 `jank_analyzer` 工具，用于标准化 HarmonyOS trace analyzer 产生的 JSON/CSV 结果。它与 Crash、JS Crash、JS Heap 和 Native Leak workflow 分离。

## CLI

```bash
python3 cli/jank.py /path/to/frames.json \
  --mode frame \
  --deadline-ms 16.67 \
  --top-n 20 \
  --output reports/jank.json
```

支持具名模式：`frame`、`arkui`、`fence`、`flutter`、`web`、`pmu`、`completion_latency`、`cpu`。

## 当前能力

- 识别 `.htrace`、`.trace`、`.ftrace`、`.pb` 和 JSON/CSV 分析结果。
- 对 JSON/CSV 帧结果按 deadline 归一化和判定丢帧。
- 输出帧 Top-N、jank 事件、线程聚合统计和 jank 率。
- 匹配 `JANK-FM-*`：主线程业务、Build、Layout、Render、Fence、GC、I/O、调度/CPU 抢占。
- 二级问题类型与三级组件/函数联合输出（`joint_root_cause`），避免只报“Layout 耗时”却没有具体组件。
- 完成时延模式支持 `touch/input/start` 到 `complete/end` 标签；缺失标签时返回明确补充建议。

## 外部 trace analyzer

仓库不复制华为平台二进制。对原始二进制 trace，工具返回 `unsupported`，需要后续配置 adapter 调用外部 `analysis_mac`、Linux 或 Windows analyzer，并把其 JSON/CSV 输出再次交给本工具标准化。

## Tool System

工具名：`jank_analyzer`，输入示例：

```json
{"path": "/path/to/frames.json", "mode": "frame", "deadline_ms": 16.67}
```

原始 trace、外部命令和 HTML 展示层不应直接拼入 Crash 的 `06_ai_prompt.md`；应保留为独立性能报告或 sidecar。

## 测试

```bash
python3 -B -m unittest test.tools.test_jank_analysis
```
