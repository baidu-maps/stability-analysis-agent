# JS/ArkTS Heap 分析

当前 Agent 提供独立的 `js_heap_analyzer` 工具，用于分析 V8/HarmonyOS `.heapsnapshot`，不会改变 Crash 分析的 prompt 组装流程。

## 能力

- 自动识别 `.heapsnapshot`、`.rawheap` 和快照目录。
- 解析标准 V8 heap snapshot 的节点和边。
- 按 retained size 生成 Top-N 对象聚类。
- 输出紧凑引用链摘要和 root 节点信息。
- 根据 `GlobalHandler`、listener、timer、Promise、N-API 等线索生成 `JS-FM-*` 故障模式候选。
- 按 distance=1 根节点类型匹配 `ROOT_VM` / `ROOT_FRAME` / `ROOT_LOCAL_HANDLE` / `ROOT_GLOBAL_HANDLE`。
- `--baseline` 或目录内多快照会对比新增对象和 retained size 增长。

## Tool System 调用

```json
{
  "path": "/path/to/heap.heapsnapshot",
  "top_n": 20
}
```

工具名为 `js_heap_analyzer`，结果是 JSON，可作为独立报告或 workflow sidecar 使用。

也可以直接运行 CLI：

```bash
python3 cli/js_heap.py /path/to/heap.heapsnapshot --top-n 20 --output reports/js_heap.json
```

## rawheap

仓库不内置华为平台相关的 rawheap translator。对于 `.rawheap`，工具会返回 `unsupported` 和明确提示，调用方应先配置对应平台的外部 translator，将文件转换成 `.heapsnapshot` 后再分析。

## 设计边界

完整 heap snapshot 不会直接拼入 `round_0/06_ai_prompt.md`。调用方应只把 Top-N 聚类、故障模式和源码定位摘要作为后续 AI 输入，原始图和转换日志保留在独立 sidecar 报告中。

## 测试

```bash
python3 -B -m unittest test.tools.test_js_heap
```
