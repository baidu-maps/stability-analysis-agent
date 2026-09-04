(() => {
  "use strict";

  const STORAGE_INPUT_KEY = "saa_web_task_input_v2";
  const STORAGE_ENGINE_KEY = "saa_web_engine_v1";

  const TEMPLATES = [
    {
      id: "demo",
      label: "Demo · NullPtr",
      text: "examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash",
    },
    {
      id: "demo_dir",
      label: "Demo · 日志目录",
      text: "examples/crash_cases/demo_basic/logs/mac",
    },
    {
      id: "path_hint",
      label: "路径模板",
      text: "/path/to/your/crash.crash",
    },
    {
      id: "paste_hint",
      label: "粘贴日志",
      text: "在此粘贴完整崩溃日志（多行）…\nException Type:  EXC_BAD_ACCESS (SIGSEGV)\n",
    },
  ];

  const $ = (id) => document.getElementById(id);

  let currentRunId = null;
  let eventSource = null;
  let workspace = { library_dir: "", code_roots: [] };
  let vectorDbPrefs = { mode: "local", local_path: "" };
  let lastReportPath = "";
  let lastTracePayload = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function appendLog(line, cls) {
    const box = $("consoleLog");
    const prefix = cls ? `[${cls}] ` : "";
    box.textContent += prefix + line + "\n";
    box.scrollTop = box.scrollHeight;
  }

  function setHealth(ok, text) {
    $("healthDot").className = "dot " + (ok === true ? "ok" : ok === false ? "bad" : "unknown");
    $("healthText").textContent = text;
  }

  async function checkHealth() {
    try {
      const res = await fetch("/health");
      const data = await res.json();
      if (res.ok && data.ok) {
        setHealth(true, `已连接 · pid ${data.pid}`);
      } else {
        setHealth(false, "异常");
      }
    } catch (_) {
      setHealth(false, "未连接 daemon");
    }
  }

  function setStep(name) {
    const order = ["parse", "symbolize", "diagnose", "ai", "fix"];
    const idx = order.indexOf(name);
    document.querySelectorAll(".step").forEach((el) => {
      const step = el.dataset.step;
      const si = order.indexOf(step);
      el.classList.remove("active", "done");
      if (si < idx) el.classList.add("done");
      else if (si === idx) el.classList.add("active");
    });
  }

  function resetProgress() {
    $("progress").classList.remove("hidden");
    document.querySelectorAll(".step").forEach((el) => el.classList.remove("active", "done"));
    setStep("parse");
  }

  function bumpProgressFromLine(line) {
    const t = String(line || "");
    if (/01_crash_log_parser|parse_result|解析/.test(t)) setStep("parse");
    if (/02_memory_maps|03_add2line|符号化|symbol/.test(t)) setStep("symbolize");
    if (/04a_crash_diagnosis|evidence_compass|诊断/.test(t)) setStep("diagnose");
    if (/06_ai_prompt|06_ai_gen|AI_STREAM|round_\d/.test(t)) setStep("ai");
    if (/08_apply_ai_fixes|apply.*fix|改码|已修改/.test(t)) setStep("fix");
    const m = t.match(/report 已保存到:\s*(.+)/) || t.match(/reports\/[^\s]+/);
    if (m) lastReportPath = (m[1] || m[0]).trim();
  }

  function parseTaskInput(raw) {
    const text = String(raw || "").trim();
    if (!text) return { error: "请输入崩溃日志路径或粘贴日志内容" };

    const lines = text.split(/\r?\n/).filter((l) => l.trim());
    const single = lines.length === 1 ? lines[0].trim() : "";

    if (single && (single.endsWith("/") || single.endsWith("\\"))) {
      return { crash_log_dir: single };
    }
    if (
      single &&
      (/^[./~]/.test(single) || single.includes("/") || /\.(crash|log|txt|rtf)$/i.test(single))
    ) {
      return { crash_log: single };
    }
    if (
      lines.length > 1 ||
      /Exception Type:|SIGSEGV|SIGABRT|backtrace|Thread \d+|崩溃|Fatal/i.test(text)
    ) {
      return { crash_log_content: text };
    }
    if (single) {
      return { crash_log: single };
    }
    return { error: "无法识别输入，请填写路径或粘贴日志" };
  }

  function buildFullRunRequest(parsed) {
    const codeRoots = (workspace.code_roots || []).map((x) => String(x).trim()).filter(Boolean);
    const body = {
      ...parsed,
      library_dir: (workspace.library_dir || "").trim() || null,
      code_roots: codeRoots.length ? codeRoots : null,
      scope: "full",
      prompt_mode: "fix",
      agent_loop: "context_loop",
      apply_ai_fixes: true,
      backup_original_sources: true,
      output_format: "markdown",
      engine: $("engineSelect").value,
    };
    return body;
  }

  function renderTemplates() {
    const wrap = $("templateChips");
    wrap.innerHTML = "";
    for (const tpl of TEMPLATES) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = tpl.label;
      btn.addEventListener("click", () => {
        $("taskInput").value = tpl.text;
        localStorage.setItem(STORAGE_INPUT_KEY, tpl.text);
      });
      wrap.appendChild(btn);
    }
  }

  function hideVectorDbCommit() {
    $("vectorDbCommit").classList.add("hidden");
    $("vectorDbCommitMsg").textContent = "";
  }

  function showVectorDbCommit(runId) {
    $("vectorDbCommit").classList.remove("hidden");
    $("vectorDbCommitMsg").textContent = "";
    $("btnVectorDbCommit").onclick = () => commitVectorDb(runId);
    $("btnVectorDbSkip").onclick = () => hideVectorDbCommit();
  }

  function maybeOfferVectorDbCommit(runId, resultData) {
    const status = resultData && resultData.status;
    const out = String((resultData && resultData.output) || "");
    if (status !== "done") {
      hideVectorDbCommit();
      return;
    }
    if (/已修改|apply.*fix|改码/i.test(out) || lastReportPath) {
      showVectorDbCommit(runId);
    } else {
      hideVectorDbCommit();
    }
  }

  async function commitVectorDb(runId) {
    $("vectorDbCommitMsg").textContent = "写入中…";
    try {
      const res = await fetch(`/runs/${encodeURIComponent(runId)}/vector-db/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        $("vectorDbCommitMsg").textContent = `已写入 pattern_id: ${data.pattern_id || "?"}`;
        $("btnVectorDbCommit").disabled = true;
      } else {
        $("vectorDbCommitMsg").textContent = data.error || data.skipped_reason || "写入失败";
      }
    } catch (err) {
      $("vectorDbCommitMsg").textContent = String(err);
    }
  }

  async function loadPreferences() {
    try {
      const res = await fetch("/web/preferences");
      const data = await res.json();
      if (!res.ok) return;
      workspace = data.workspace || workspace;
      vectorDbPrefs = data.vector_db || vectorDbPrefs;
      $("wsLibraryDir").value = workspace.library_dir || "";
      $("wsCodeRoots").value = (workspace.code_roots || []).join("\n");
      const savedEngine = localStorage.getItem(STORAGE_ENGINE_KEY) || "direct";
      $("engineSelect").value = ["direct", "langchain", "langgraph"].includes(savedEngine) ? savedEngine : "direct";
      const mode = vectorDbPrefs.mode || "local";
      const path = vectorDbPrefs.local_path || "";
      $("vectorDbInfo").textContent =
        mode === "local" ? `本地 · ${path || "默认路径"}` : `远端（未实现）· ${vectorDbPrefs.remote_url || ""}`;
    } catch (_) {
      $("wsLibraryDir").value = "examples/crash_cases/demo_basic/lib/mac";
      $("wsCodeRoots").value = "examples/crash_cases/demo_basic/code_dir";
      $("vectorDbInfo").textContent = "本地 · 默认路径";
      $("engineSelect").value = localStorage.getItem(STORAGE_ENGINE_KEY) || "direct";
    }
  }

  async function saveWorkspace() {
    const payload = {
      workspace: {
        library_dir: $("wsLibraryDir").value.trim(),
        code_roots: $("wsCodeRoots")
          .value.split("\n")
          .map((x) => x.trim())
          .filter(Boolean),
      },
    };
    try {
      const res = await fetch("/web/preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok) {
        workspace = data.workspace || payload.workspace;
        $("skillOpMsg").textContent = "工作区已保存";
      }
    } catch (err) {
      $("skillOpMsg").textContent = String(err);
    }
  }

  function closeEvents() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function setRunning(running) {
    $("btnRun").disabled = running;
    $("btnCancel").disabled = !running;
  }

  function showResult(text) {
    $("resultSummary").textContent = text;
  }

  function showVerificationPanel(candidates) {
    $("verificationPanel").classList.remove("hidden");
    const list = $("verificationCandidates");
    list.innerHTML = "";
    const items = Array.isArray(candidates) ? candidates : [];
    for (const item of items) {
      const li = document.createElement("li");
      const cmd = Array.isArray(item.command) ? item.command.join(" ") : String(item.command || "");
      li.innerHTML = `<button type="button" class="btn ghost sm candidate-btn">${escapeHtml(cmd || item.reason || "candidate")}</button>`;
      li.querySelector("button").onclick = () => { $("verificationCommand").value = cmd; };
      list.appendChild(li);
    }
    $("btnVerify").onclick = resumeVerification;
  }

  async function renderCheckpoints(runId) {
    const panel = $("checkpointPanel");
    const list = $("checkpointList");
    list.innerHTML = "";
    try {
      const res = await fetch(`/runs/${encodeURIComponent(runId)}/checkpoints`);
      const data = await res.json();
      const checkpoints = data.checkpoints || [];
      if (!checkpoints.length) {
        panel.classList.add("hidden");
        return;
      }
      panel.classList.remove("hidden");
      for (const item of checkpoints) {
        const li = document.createElement("li");
        li.textContent = `${item.stage || "?"} · ${item.status || "?"} · ${item.idempotency_key || item.checkpoint_id || ""}`;
        list.appendChild(li);
      }
    } catch (_) {
      panel.classList.add("hidden");
    }
  }

  function showApprovalPanel(payload) {
    $("approvalPanel").classList.remove("hidden");
    const tool = (payload && payload.tool) || (payload && payload.pending_tool_approval && payload.pending_tool_approval.tool) || "unknown";
    $("approvalToolName").textContent = `工具: ${tool}`;
    $("btnApproveTool").onclick = () => resumeToolApproval(tool, payload);
  }

  async function resumeToolApproval(tool, payload) {
    if (!currentRunId) return;
    $("approvalMsg").textContent = "审批中…";
    const body = {
      tool,
      approval_id: (payload && payload.approval && payload.approval.approval_id) || undefined,
      fingerprint: (payload && payload.approval && payload.approval.command_fingerprint) || "",
      input: (payload && payload.pending_tool_approval && payload.pending_tool_approval.input) || {},
    };
    const res = await fetch(`/runs/${encodeURIComponent(currentRunId)}/tool-approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    $("approvalMsg").textContent = data.error || data.message || (data.tool_approval && data.tool_approval.status) || `状态: ${data.status || "?"}`;
    if (res.ok) fetchResult(currentRunId).catch(() => {});
  }

  async function resumeVerification() {
    if (!currentRunId) return;
    const command = $("verificationCommand").value.trim();
    if (!command) { $("verificationMsg").textContent = "请输入验证命令"; return; }
    $("verificationMsg").textContent = "验证中…";
    const res = await fetch(`/runs/${encodeURIComponent(currentRunId)}/verification`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command, post_fix_diagnosis: true }),
    });
    const data = await res.json();
    $("verificationMsg").textContent = data.message || data.error || `状态: ${data.status || "?"}`;
    if (res.ok && data.status !== "verification_pending") fetchResult(currentRunId).catch(() => {});
  }

  async function fetchResult(runId) {
    const [resultRes, runRes] = await Promise.all([
      fetch(`/runs/${encodeURIComponent(runId)}/result`),
      fetch(`/runs/${encodeURIComponent(runId)}`),
    ]);
    const data = await resultRes.json();
    const runData = runRes.ok ? await runRes.json() : {};
    const status = data.status || runData.status || "?";
    const err = data.error ? `\n错误: ${data.error}` : "";
    const report = lastReportPath ? `\n报告目录: ${lastReportPath}` : "";
    const out = (data.output || "").slice(0, 4000);
    showResult(`状态: ${status}${err}${report}\n\n--- 输出摘要 ---\n${out}`);
    renderTraceTimeline(runData.runtime_trace || data.runtime_trace);
    renderEvaluationSummary(runData, data);
    renderCheckpoints(runId).catch(() => {});
    const candidates = runData.discovered_candidates
      || (runData.verification && runData.verification.discovered_candidates)
      || [];
    if (candidates.length) showVerificationPanel(candidates);
    document.querySelectorAll(".step").forEach((el) => el.classList.add("done"));
    $("btnVectorDbCommit").disabled = false;
    maybeOfferVectorDbCommit(runId, data);
  }

  function renderEvaluationSummary(runData, resultData) {
    const panel = $("evaluationPanel");
    const box = $("evaluationSummary");
    const diagnosis = (resultData && resultData.crash_diagnosis) || runData.crash_diagnosis || {};
    const verification = (resultData && resultData.verification) || runData.verification || {};
    const status = (resultData && resultData.status) || runData.status || "?";
    const trace = runData.runtime_trace || (resultData && resultData.runtime_trace) || {};
    const budget = trace.budget || {};
    if (!status && !diagnosis.category && !verification.status) {
      panel.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    panel.classList.remove("hidden");
    box.innerHTML = [
      `<div><strong>运行状态</strong>: ${escapeHtml(status)}</div>`,
      `<div><strong>诊断类别</strong>: ${escapeHtml(diagnosis.category || diagnosis.fault_mode || "-")}</div>`,
      `<div><strong>验证</strong>: ${escapeHtml(verification.status || "-")}</div>`,
      `<div><strong>LLM/Tool</strong>: ${escapeHtml(String(budget.llm_calls || 0))}/${escapeHtml(String(budget.tool_calls || 0))}</div>`,
    ].join("");
  }

  function statusClass(status) {
    const value = String(status || "").toLowerCase();
    if (value === "success" || value === "completed" || value === "passed") return "trace-status-success";
    if (value === "denied" || value === "failed" || value === "error") return "trace-status-failed";
    if (value === "pending") return "trace-status-pending";
    return "";
  }

  function renderTraceBudget(trace) {
    const bar = $("traceBudgetBar");
    const budget = trace && trace.budget ? trace.budget : null;
    if (!budget) {
      bar.classList.add("hidden");
      bar.textContent = "";
      return;
    }
    bar.classList.remove("hidden");
    const tokens = (budget.token_usage && budget.token_usage.total_tokens) || 0;
    bar.textContent = [
      `engine=${trace.engine || "-"}`,
      `llm=${budget.llm_calls || 0}`,
      `tool=${budget.tool_calls || 0}`,
      `tokens=${tokens}`,
      `cost=${budget.estimated_cost || 0}`,
    ].join(" · ");
  }

  function populateTraceFilters(events) {
    const fill = (id, key) => {
      const select = $(id);
      const current = select.value;
      const values = Array.from(new Set(events.map((e) => String(e[key] || "")).filter(Boolean))).sort();
      select.innerHTML = `<option value="">全部</option>` + values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
      if (values.includes(current)) select.value = current;
    };
    fill("traceFilterStage", "stage");
    fill("traceFilterKind", "kind");
    fill("traceFilterStatus", "status");
  }

  function renderTraceTimeline(trace) {
    const panel = $("tracePanel");
    const body = $("traceTableBody");
    body.innerHTML = "";
    const events = trace && Array.isArray(trace.events) ? trace.events : [];
    renderTraceBudget(trace);
    if (!events.length) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    lastTracePayload = trace;
    populateTraceFilters(events);
    const stageFilter = $("traceFilterStage").value;
    const kindFilter = $("traceFilterKind").value;
    const statusFilter = $("traceFilterStatus").value;
    for (const event of events) {
      if (stageFilter && event.stage !== stageFilter) continue;
      if (kindFilter && event.kind !== kindFilter) continue;
      if (statusFilter && event.status !== statusFilter) continue;
      const row = document.createElement("tr");
      row.className = [
        statusClass(event.status),
        event.event === "tool.policy" && event.status === "denied" ? "trace-row-denied" : "",
      ].filter(Boolean).join(" ");
      row.innerHTML = [
        escapeHtml(event.seq ?? ""),
        escapeHtml(event.stage ?? ""),
        escapeHtml(event.kind ?? ""),
        escapeHtml(event.name ?? ""),
        escapeHtml(event.event ?? ""),
        escapeHtml(event.status ?? ""),
        escapeHtml(event.timestamp ?? ""),
        escapeHtml(event.duration_ms ?? ""),
      ].map((cell) => `<td>${cell}</td>`).join("");
      body.appendChild(row);
    }
  }

  async function startRun(ev) {
    ev.preventDefault();
    const raw = $("taskInput").value;
    localStorage.setItem(STORAGE_INPUT_KEY, raw);

    const parsed = parseTaskInput(raw);
    if (parsed.error) {
      showResult(parsed.error);
      return;
    }

    if (!(workspace.library_dir || "").trim()) {
      showResult("请先在左侧填写并保存「符号库 library_dir」");
      return;
    }
    if (!(workspace.code_roots || []).length) {
      showResult("请先在左侧填写并保存「源码 code_roots」");
      return;
    }

    closeEvents();
    $("consoleLog").textContent = "";
    lastReportPath = "";
    hideVectorDbCommit();
    $("verificationPanel").classList.add("hidden");
    $("approvalPanel").classList.add("hidden");
    $("tracePanel").classList.add("hidden");
    $("traceTableBody").innerHTML = "";
    $("traceBudgetBar").classList.add("hidden");
    $("evaluationPanel").classList.add("hidden");
    $("evaluationSummary").innerHTML = "";
    resetProgress();
    setRunning(true);
    $("runMeta").textContent = "";
    showResult("运行中…");

    const body = buildFullRunRequest(parsed);
    localStorage.setItem(STORAGE_ENGINE_KEY, body.engine);
    appendLog("POST /runs (full pipeline)", "req");

    let res;
    try {
      res = await fetch("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      appendLog(String(err), "error");
      showResult(String(err));
      setRunning(false);
      return;
    }

    const data = await res.json();
    if (!res.ok) {
      appendLog(JSON.stringify(data), "error");
      showResult(data.error || JSON.stringify(data));
      setRunning(false);
      return;
    }

    currentRunId = data.run_id;
    $("runMeta").textContent = `run_id: ${currentRunId}`;

    eventSource = new EventSource(`/runs/${encodeURIComponent(currentRunId)}/events`);
    eventSource.onmessage = (msg) => {
      try {
        const evObj = JSON.parse(msg.data);
        const t = evObj.type || "?";
        if (t === "keepalive") return;
        if (t === "stdout") {
          const line = evObj.data && evObj.data.line != null ? evObj.data.line : "";
          appendLog(line);
          bumpProgressFromLine(line);
        } else if (t === "stderr") {
          const line = evObj.data && evObj.data.line != null ? evObj.data.line : "";
          appendLog(line, "stderr");
          bumpProgressFromLine(line);
        } else if (t === "artifact_written") {
          const p = String(evObj.data && evObj.data.path || "");
          appendLog(p, "artifact");
          if (p.includes("reports/") && !p.endsWith(".md")) {
            lastReportPath = p.replace(/\/[^/]+\.(md|json|txt)$/, "");
          }
        } else if (t === "verification_pending") {
          showVerificationPanel((evObj.data && evObj.data.discovered_candidates) || []);
          showResult("等待用户配置验证命令");
        } else if (t === "approval_required" || t === "tool_approval_required") {
          showApprovalPanel(evObj.data || {});
          showResult("等待用户批准工具调用");
        } else if (t === "run_finished" || t === "run_canceled") {
          closeEvents();
          setRunning(false);
          fetchResult(currentRunId).catch((e) => showResult(String(e)));
        }
      } catch (_) {
        appendLog(msg.data, "raw");
      }
    };
    eventSource.onerror = () => {
      closeEvents();
      setRunning(false);
      if (currentRunId) fetchResult(currentRunId).catch(() => {});
    };
  }

  async function cancelRun() {
    if (!currentRunId) return;
    await fetch(`/runs/${encodeURIComponent(currentRunId)}/cancel`, { method: "POST" });
  }

  async function refreshSkills() {
    const list = $("skillList");
    list.innerHTML = "<li class='hint'>加载中…</li>";
    try {
      const res = await fetch("/skills");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      const skills = data.skills || [];
      if (!skills.length) {
        list.innerHTML = "<li class='hint'>尚未安装 skill</li>";
        return;
      }
      list.innerHTML = "";
      for (const s of skills) {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = s.command_name || s.display_name || "?";
        const toggle = document.createElement("label");
        toggle.className = "toggle";
        toggle.title = s.enabled === false ? "已关闭，点击启用" : "已启用，点击关闭";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = s.enabled !== false;
        input.addEventListener("change", async () => {
          await toggleSkill(s.command_name, input.checked);
          toggle.title = input.checked ? "已启用，点击关闭" : "已关闭，点击启用";
        });
        const span = document.createElement("span");
        toggle.append(input, span);
        li.append(name, toggle);
        list.appendChild(li);
      }
    } catch (err) {
      list.innerHTML = `<li class="hint">${escapeHtml(String(err))}</li>`;
    }
  }

  async function toggleSkill(name, enabled) {
    try {
      const res = await fetch("/web/preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill: name, enabled }),
      });
      const data = await res.json();
      if (!res.ok) {
        $("skillOpMsg").textContent = data.error || "切换失败";
        await refreshSkills();
      }
    } catch (err) {
      $("skillOpMsg").textContent = String(err);
      await refreshSkills();
    }
  }

  async function installSkill(ev) {
    ev.preventDefault();
    const source = $("skillSource").value.trim();
    if (!source) {
      $("skillOpMsg").textContent = "请填写 skill 目录或 zip 路径";
      return;
    }
    $("skillOpMsg").textContent = "安装中…";
    try {
      const res = await fetch("/skills/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, overwrite: true }),
      });
      const data = await res.json();
      if (res.ok) {
        $("skillOpMsg").textContent = `已安装: ${data.command_name || ""}`;
        $("skillSource").value = "";
        await refreshSkills();
      } else {
        $("skillOpMsg").textContent = data.error || "安装失败";
      }
    } catch (err) {
      $("skillOpMsg").textContent = String(err);
    }
  }

  function bind() {
    $("runForm").addEventListener("submit", startRun);
    $("btnCancel").addEventListener("click", cancelRun);
    $("btnSaveWorkspace").addEventListener("click", saveWorkspace);
    $("skillInstallForm").addEventListener("submit", installSkill);
    for (const id of ["traceFilterStage", "traceFilterKind", "traceFilterStatus"]) {
      $(id).addEventListener("change", () => {
        if (lastTracePayload) renderTraceTimeline(lastTracePayload);
      });
    }
  }

  bind();
  renderTemplates();
  const saved = localStorage.getItem(STORAGE_INPUT_KEY);
  if (saved) $("taskInput").value = saved;
  loadPreferences().then(refreshSkills);
  checkHealth();
  setInterval(checkHealth, 15000);
})();
