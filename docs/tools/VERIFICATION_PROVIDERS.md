# Verification Providers

Verification profiles are executable capability declarations. The Agent may
inspect the declared capability metadata and select a `check_id` plus a fixed
purpose, but it cannot provide or alter the executable command. Runtime always
loads argv, fixture, timeout and iteration limits from the selected profile
check. Repository discovery may suggest checks, but suggestions are never run.

The same contract covers local builds, test runners, device runners and custom
environments through `provider` and `kind`; the core flow has no frontend or
native-specific execution branch. Without a profile, verification stops at L0
and L1 static evidence and reports `not_configured` / `static_only`.

Example model selection (non-executable):

```json
{
  "verification_claim": {
    "statement": "The target crash signature is no longer produced",
    "required_evidence": ["stack_signature"],
    "minimum_level": "L3"
  },
  "reproduction_plan": {
    "check_id": "native_replay",
    "purpose": "pre_fix_reproduce"
  }
}
```

验证能力是可选的 post-fix provider，不属于 Crash 解析或诊断的必要步骤。
工程没有可用的构建入口时，provider 本身仍返回 `unavailable`；CLI/daemon 会把“已有 AI 修改但未配置 provider”提升为 `verification_pending`，不应被解释为诊断失败。

## 配置本地命令

在运行请求的 `verification` 字段或系统配置 `metadata.verification` 中提供命令数组：

```json
{
  "metadata": {
    "verification": {
      "command": ["./gradlew", "testDebugUnitTest"],
      "mode": "test",
      "timeout_sec": 900,
      "rollback_on_verification_failure": true,
      "modes": ["build", "test"]
    }
  }
}
```

也支持命令字符串，但数组更适合跨平台和参数边界明确的场景。命令通过
`subprocess.run(..., shell=False)` 执行，支持以下占位符：

- `{workspace}`：当前代码 workspace 的真实绝对路径
- `{target}`：可选验证目标
- `{changed_files}`：本次修改文件，以换行分隔

例如：

```json
{
  "verification": {
    "command": ["python3", "scripts/verify.py", "{workspace}", "{changed_files}"],
    "mode": "build"
  }
}
```

## Provider 接口

实现 `VerificationProvider`，并返回 `VerificationResult`：

```python
from services.verification import VerificationRequest, VerificationResult

class CommandVerificationProvider:
    name = "local_command"

    def capabilities(self, request):
        ...

    def verify(self, request: VerificationRequest) -> VerificationResult:
        ...
```

结果状态包括：

- `passed`：验证完成且通过
- `failed`：验证完成但命令或检查失败
- `timeout`：超过 provider 的执行时间
- `unavailable`：当前环境没有可用验证能力
- `pending`：已有修改，等待用户提交明确的验证 provider/命令
- `skipped`：没有产生可验证的修改

daemon 在 `verification_pending` 状态会保留本次 run 和隔离 workspace。提交：

```text
POST /runs/{run_id}/verification
```

请求体必须包含显式 `command`，例如
`{"command":["./gradlew","test"],"mode":"test","timeout_sec":900}`。
恢复接口只运行这条命令；验证通过才同步隔离 workspace，失败或超时不会同步。

可以为 provider 增加策略边界：

```json
{
  "verification": {
    "command": ["pytest"],
    "policy": {
      "allowed_commands": ["pytest"],
      "allowed_roots": ["/workspace/project"],
      "allow_network": false,
      "allow_destructive": false
    }
  }
}
```

策略拒绝会返回 `unavailable`，不会执行命令，也不会同步修改。

## Act 工具

以下工具可作为显式验证入口（内部复用 `CommandVerificationProvider`）：

- `run_build` — mode=build，`risk=execute`
- `run_tests` — mode=test
- `run_static_check` — syntax/static/lint
- `reproduce_crash` — mode=reproduce，需要 RuntimeAuthorization capability

在 verification config 中也可使用 `"provider": "reproduce"` 并配置 `command`。

CLI 会将结果写入报告目录的 `09_verification.json`。验证结果不会写入
`round_0/06_ai_prompt.md`，也不会改变原有 prompt 组装契约。

## 推荐扩展方向

后续可增加 `xcodebuild`、Gradle、CMake/Ninja 等本地 provider。
每个 provider 应先通过 `capabilities()` 声明支持的验证级别，再执行具体检查；
不能验证时返回 `unavailable`，不要伪造成功结果。

`discover_verification_candidates(workspace)` 会只读扫描常见工程文件并返回候选命令。
候选命令仅用于展示和人工/上层策略选择，不会被自动执行；Xcode scheme、CMake
build 目录等仍需要项目配置明确指定。

验证命令返回失败或超时时，CLI 默认使用 `CodeFixer` 生成的原始源码备份回滚已应用修改。
Daemon 在 `POST /runs/{id}/verification` 失败/超时分支同样尝试回滚。

验证通过后，CLI 与 daemon 均可运行 **post-fix 复诊**（`parse_stack_only` 子流程，不再次改码）。
结果写入 `09_verification.json` 的 `post_fix_diagnosis` 字段；复诊失败会将 run 标记为 error
（daemon）或 `post_fix_diagnosis_failed`（CLI metadata）。
`unavailable` 不会触发回滚；也可以将 `rollback_on_verification_failure` 设为 `false`。

## 隔离修复 workspace

AI 修复默认在独立 Git worktree 中执行修复和验证：

```json
{
  "verification": {
    "command": ["cmake", "--build", "{workspace}/build"]
  }
}
```

隔离模式要求原始 code root 位于 Git 仓库内且相关范围没有未提交修改。验证通过后，
只有已验证的修改文件会同步回原 workspace；验证失败或验证不可用时不会同步。报告会
额外写入 `09_ai_fix_workspace.json` 和隔离 workspace 的 patch。

只有同时提供 `allow_unisolated: true` 和状态为 `granted` 的高风险 approval，才允许
关闭隔离。没有显式验证 provider 时只返回 `verification_pending`；自动发现的候选命令
仅展示，绝不会执行。

## 显式 verification.tool 接线

当 `provider` 解析为 `none` 且配置同时包含 `tool` 与 `command` 时，repair harness 会经
`ToolExecutionGateway` 调用 registry 中的 act 工具（如 `run_build`），而不是自动执行候选命令：

```json
{
  "verification": {
    "tool": "run_build",
    "command": ["cmake", "--build", "build"],
    "timeout_sec": 300
  }
}
```

仍须显式配置；无 `command` 时保持 `verification_pending` 安全语义。
