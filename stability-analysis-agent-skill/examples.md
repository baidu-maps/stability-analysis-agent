# 命令示例

以下命令假设已安装 `stability-analysis-agent`，且在含 demo 的仓库根目录执行（Demo 路径）；分析自有 case 时替换路径即可。

## 1. 交互向导（新手）

```bash
sa-agent
```

选择「快速开始修复」，输入 crash_log / library_dir / code_root。

## 2. Demo — 无 LLM Key 完整工具链

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

输出：`cli_reports/<timestamp>/round_0/06_ai_prompt.md`

## 3. Demo — 完整 AI 分析

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir
```

需已配置 LLM（`~/.config/stability-analysis-agent/agent_config.local.json` 或交互配置）。

## 4. 仅解析日志

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --scope parse_log_only
```

## 5. 解析 + 符号化（无源码）

```bash
sa-agent \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --scope parse_stack_only
```

## 6. 多代码根目录

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/module_a \
  --code-root /path/to/module_b
```

## 7. 偏修复输出的 AI 分析

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/source \
  --prompt-mode fix \
  --agent-loop single
```

## 8. 多轮上下文补充（analysis 默认）

```bash
sa-agent \
  --crash-log /path/to/crash.log \
  --library-dir /path/to/libs \
  --code-root /path/to/source \
  --prompt-mode analysis \
  --agent-loop context_loop \
  --max-agent-rounds 3
```

## 9. 从 stdin 读日志

```bash
cat /path/to/crash.log | sa-agent --crash-log - --library-dir /path/to/libs --scope parse_log_only
```

## 10. Daemon 委托

```bash
# 终端 1
sa-agent --daemon-server --host 127.0.0.1 --port 8765

# 终端 2
sa-agent --daemon http://127.0.0.1:8765 \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

## 11. 源码开发（未 pip 安装）

```bash
cd stability-analysis-agent
pip install -e .
python3 cli/main.py \
  --crash-log examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-root examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

## 12. 安装本 Skill 到 Claude Code

```bash
git clone https://github.com/baidu-maps/stability-analysis-agent.git
cp -R stability-analysis-agent/stability-analysis-agent-skill \
      ~/.claude/skills/stability-analysis-agent
```

## 13. Python API（进程内调用）

```python
from cli.api import run_from_interactive_state

state = {
    "crash_log": "examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash",
    "library_dir": "examples/crash_cases/demo_basic/lib/mac",
    "code_roots": ["examples/crash_cases/demo_basic/code_dir"],
    "scope": "gen_prompt_only",
}
run_from_interactive_state(state)
```
