# AppFreeze / ANR 分析

`appfreeze_diagnosis` 建立在现有 `anr_diagnosis`、EventHandler 和 Binder 分析能力之上，补充华为 AppFreeze Skill 的证据组织和多采样归因，不重新实现底层日志解析。

## CLI

```bash
python3 cli/appfreeze.py \
  reports/<run>/01_crash_log_parser.json \
  --raw-content /path/to/faultlog.txt \
  --output reports/<run>/04f_appfreeze_diagnosis.json
```

## 输出

- Freeze 类型和超时阈值：`APPFREEZE`、`INPUT_BLOCK`、`THREAD_BLOCK_3S/6S/20S`、`LIFECYCLE_TIMEOUT`、`RENDER_SERVICE_TIMEOUT`、`FFRT_TIMEOUT`
- 多时间点采样栈稳定前缀聚类
- 从 raw faultlog 解析 EventHandler、Binder 传播图、CPU/内存/热节流
- 系统噪声门禁：CPU ≥85%、可用内存 <800MB 或 thermal note 时优先作为环境证据
- 3s/6s 采样栈对比（阻塞 vs 繁忙）和业务帧热点
- EventHandler、Binder 和 FFRT 依赖环证据
- `FREEZE-FM-*` 故障模式
- `evidence_chain`、根因置信度和缺失证据
- 应用侧修复建议与系统环境观察分离

## Tool System

工具名：`appfreeze_diagnosis`。输入可包含现有 `parse_result`、`raw_content`、`samples` 和 `ffrt_edges`。

建议保存为 `04f_appfreeze_diagnosis.json` sidecar，不把完整 faultlog、Binder 图或 FFRT 图直接拼入 Crash 主 Prompt。

## 测试

```bash
python3 -B -m unittest test.tools.test_appfreeze
```
