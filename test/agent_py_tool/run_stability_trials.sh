#!/bin/bash
# 三轮 full 分析：记录 AI 改码结果并撤销源码，用于稳定性对比
set -euo pipefail
REPO="/Users/liuhong_cd/baidu/personal-code/github-repos/stability-analysis-agent"
MAPSDK="/Users/liuhong_cd/baidu/mapclient/mapsdk-vector/engine-dev"
CRASH="/Users/liuhong_cd/baidu/mapclient/stability-analysis-agent/crash_cases/ios/uni_pub_v20.16.0_A8.1.0_i7.1.0/openmap-client-2942/log/crash.rtf"
OUT_BASE="${STABILITY_TRIALS_OUT:-$REPO/reports/stability_trials_20260602}"
mkdir -p "$OUT_BASE"

cd "$REPO"
for i in 1 2 3; do
  echo "========== TRIAL $i / 3 =========="
  git -C "$MAPSDK" checkout -- src/app/walk/panodata/ src/app/walk/guidance/navi_control/ 2>/dev/null || true
  git -C "$MAPSDK" diff -w --stat src/app/walk/ | head -3 || true

  python3 cli/main.py \
    --crash-log-file "$CRASH" \
    --code-roots "/Users/liuhong_cd/baidu/mapclient/mapsdk-vector" \
    --scope full 2>&1 | tee "$OUT_BASE/trial_${i}_cli.log"

  LATEST=$(ls -td "$REPO"/reports/*_analysis_full_direct_crash "$REPO"/cli_reports/*_analysis_full_direct_crash 2>/dev/null | head -1)
  TRIAL_DIR="$OUT_BASE/trial_$i"
  mkdir -p "$TRIAL_DIR"
  cp "$LATEST/07_apply_ai_fixes.json" "$TRIAL_DIR/" 2>/dev/null || true
  cp "$LATEST/07b_fix_extract_debug.json.json" "$TRIAL_DIR/" 2>/dev/null || true
  cp "$LATEST/final_output.md" "$TRIAL_DIR/" 2>/dev/null || true
  echo "$LATEST" > "$TRIAL_DIR/report_dir.txt"

  # 撤销：从 report original_sources 恢复，再 git 兜底
  if [ -d "$LATEST/original_sources" ]; then
    while IFS= read -r -d '' bak; do
      rel="${bak#"$LATEST/original_sources/"}"
      dest="$MAPSDK/$rel"
      if [ -f "$bak" ] && [ -f "$dest" ]; then
        cp "$bak" "$dest"
        echo "  restored: $rel"
      fi
    done < <(find "$LATEST/original_sources" -type f -print0 2>/dev/null)
  fi
  git -C "$MAPSDK" checkout -- src/app/walk/panodata/ src/app/walk/guidance/navi_control/ 2>/dev/null || true
  echo "  post-revert diff:" "$(git -C "$MAPSDK" diff -w --stat src/app/walk/ 2>/dev/null | wc -l) lines"
done

python3 "$REPO/test/agent_py_tool/compare_stability_trials.py" "$OUT_BASE"
echo "Done. Results: $OUT_BASE/summary.md"
