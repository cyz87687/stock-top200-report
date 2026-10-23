#!/usr/bin/env bash
# 本地每日评分链路（复用 .github/workflows/daily-refresh.yml 顺序，适配本地环境）
#   - 数据源走 HTTP(S)_PROXY（已在环境注入）
#   - generate_ai_assessment 会自行清除代理并读 .llm_env 的 LLM_* 变量（方舟真实 LLM）
#   - 使用 managed python 绝对路径，避免系统版本差异
set -uo pipefail

cd /Users/yzreal/WorkBuddy/2026-07-24-18-07-15/stock-top200-report
PY=/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python
LOG=refresh_log_local.txt

# 加载 LLM 环境（generate_ai_assessment 内部会 pop 代理，不影响其他数据源子进程）
set -a
source ./.llm_env
set +a

echo "=== 本地刷新开始 $(date '+%Y-%m-%d %H:%M:%S') ===" > "$LOG"
echo "代理: HTTP_PROXY=${HTTP_PROXY:-<空>} HTTPS_PROXY=${HTTPS_PROXY:-<空>}" >> "$LOG"
echo "LLM: BASE_URL=${LLM_BASE_URL:-<默认>} MODEL=${LLM_MODEL:-<默认>} KEY=${LLM_API_KEY:+已设置}" >> "$LOG"

BJ_TODAY=$(date '+%Y-%m-%d')
echo "BJ_TODAY=$BJ_TODAY" >> "$LOG"

SCORED_JSON="top200_scored_${BJ_TODAY}.json"
# 幂等守卫（本地）：若今日 scored 已存在，则跳过 fetch/check/step2（行情已收盘固定，省去重复拉取）
if [ -f "$SCORED_JSON" ]; then
  echo "=== 今日 $SCORED_JSON 已存在，跳过 fetch/check/step2，仅重跑后续+报告 ===" >> "$LOG"
  SKIP_CORE=1
else
  SKIP_CORE=0
fi

if [ "$SKIP_CORE" -eq 0 ]; then
  # Step 1: Fetch
  echo "[$(date '+%H:%M:%S')] Running fetch_top200_sina.py ..." >> "$LOG"
  if $PY fetch_top200_sina.py >> "$LOG" 2>&1; then
    echo "[$(date '+%H:%M:%S')] ✅ Fetch succeeded" >> "$LOG"
  else
    echo "[$(date '+%H:%M:%S')] ❌ Fetch failed" >> "$LOG"
    echo "result=fetch_failed" >> "$LOG"
    exit 1
  fi

  # Step 1.5: 数据新鲜度守卫
  echo "[$(date '+%H:%M:%S')] Running check_data_fresh.py ..." >> "$LOG"
  if $PY check_data_fresh.py >> "$LOG" 2>&1; then
    echo "[$(date '+%H:%M:%S')] ✅ Data freshness OK" >> "$LOG"
  else
    echo "[$(date '+%H:%M:%S')] ❌ Stale/holiday data" >> "$LOG"
    echo "result=stale_data" >> "$LOG"
    exit 1
  fi

  # Step 2: Score
  INPUT_JSON="top500_all_a.json"
  if [ ! -f "$INPUT_JSON" ]; then
    echo "[$(date '+%H:%M:%S')] ❌ $INPUT_JSON not found" >> "$LOG"
    exit 1
  fi
  echo "[$(date '+%H:%M:%S')] Running step2_stockscorer_v2.py ..." >> "$LOG"
  if $PY step2_stockscorer_v2.py "$INPUT_JSON" >> "$LOG" 2>&1; then
    echo "[$(date '+%H:%M:%S')] ✅ Scoring succeeded" >> "$LOG"
  else
    echo "[$(date '+%H:%M:%S')] ❌ Scoring failed" >> "$LOG"
    exit 1
  fi
fi

# 定位 scored
SCORED_JSON=$(ls -t top200_scored_*.json 2>/dev/null | grep -v "_watch_" | head -1)
if [ -z "$SCORED_JSON" ]; then
  echo "[$(date '+%H:%M:%S')] ❌ No scored JSON found" >> "$LOG"
  exit 1
fi
echo "SCORED_JSON=$SCORED_JSON" >> "$LOG"

# Step 3.5: AI assessment
echo "[$(date '+%H:%M:%S')] Running generate_ai_assessment.py ..." >> "$LOG"
if $PY generate_ai_assessment.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI assessment generated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI assessment failed, fallback" >> "$LOG"
fi

# Step 3.6: Enhance
echo "[$(date '+%H:%M:%S')] Running enhance_scores.py ..." >> "$LOG"
if $PY enhance_scores.py "$SCORED_JSON" ai_assessment.json >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Enhancement succeeded" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Enhancement failed" >> "$LOG"
fi

# Step 3.7: Sector momentum
echo "[$(date '+%H:%M:%S')] Running sector_momentum_daily.py ..." >> "$LOG"
if $PY sector_momentum_daily.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Sector momentum computed" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Sector momentum failed" >> "$LOG"
fi

# Step 3.8: Apply sector
echo "[$(date '+%H:%M:%S')] Running apply_sector_momentum.py ..." >> "$LOG"
if $PY apply_sector_momentum.py "$SCORED_JSON" --mode evidence >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Sector dimension merged" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Sector merge failed" >> "$LOG"
fi

# Step 3.9: Portfolio
echo "[$(date '+%H:%M:%S')] Running pick_portfolio.py ..." >> "$LOG"
if $PY pick_portfolio.py "$SCORED_JSON" --top 10 --per-sector 3 --per-parent 2 >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Portfolio generated" >> "$LOG"
  $PY gen_portfolio_report.py >> "$LOG" 2>&1 || echo "⚠️ Portfolio report failed" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Portfolio generation failed" >> "$LOG"
fi

# Step 3.95: Track
echo "[$(date '+%H:%M:%S')] Running track_recommendations.py ..." >> "$LOG"
if $PY track_recommendations.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Recommendation tracking updated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Recommendation tracking failed" >> "$LOG"
fi

# Step 3.96: Factor IC + 十维权重每日重估 (v3.5.5)
echo "[$(date '+%H:%M:%S')] Running factor_eval.py (Rank IC + daily weight re-estimation) ..." >> "$LOG"
if $PY factor_eval.py --optimize-weights >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Factor IC + weight re-estimation updated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Factor IC evaluation failed" >> "$LOG"
fi

# Step 3.97: Selfcheck
echo "[$(date '+%H:%M:%S')] Running selfcheck_pipeline.py ..." >> "$LOG"
$PY selfcheck_pipeline.py "$SCORED_JSON" >> "$LOG" 2>&1 \
  || echo "[$(date '+%H:%M:%S')] ⚠️ selfcheck reported suspicious items" >> "$LOG"

# Step 3.98: Focus pool
echo "[$(date '+%H:%M:%S')] Running fetch_focus_list.py ..." >> "$LOG"
if $PY fetch_focus_list.py --date "$BJ_TODAY" --top 40 --scored "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI focus pool generated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI focus pool failed" >> "$LOG"
fi

# Step 4: Report
echo "[$(date '+%H:%M:%S')] Running gen_report_top200.py ..." >> "$LOG"
if $PY gen_report_top200.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Report generation succeeded" >> "$LOG"
  LATEST_HTML=$(ls -t top200_report_*.html 2>/dev/null | head -1)
  if [ -n "$LATEST_HTML" ]; then
    cp "$LATEST_HTML" index.html
    echo "[$(date '+%H:%M:%S')] ✅ Copied $LATEST_HTML → index.html" >> "$LOG"
  fi
else
  echo "[$(date '+%H:%M:%S')] ❌ Report generation failed" >> "$LOG"
  exit 1
fi

# Prune（保持与线上一致的保留策略）
echo "[$(date '+%H:%M:%S')] Pruning old files ..." >> "$LOG"
KEEP_SCORED=2
KEEP_OTHER=1
prune_keep() {
  local i=0 f
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    i=$((i+1))
    if [ $i -gt "$2" ]; then rm -f "$f" && echo "prune: $f" >> "$LOG"; fi
  done < <(ls -1 $1 2>/dev/null | sort -r)
}
prune_keep "top200_report_*.html" $KEEP_OTHER
ls -1 top200_scored_[0-9]*.json 2>/dev/null | sort -r | awk -v k=$KEEP_SCORED 'NR>k' | xargs -r rm -f
prune_keep "sector/portfolio_[0-9]*.json" $KEEP_OTHER
prune_keep "sector/portfolio_report_*.html" $KEEP_OTHER
prune_keep "sector/sector_momentum_[0-9]*.json" $KEEP_OTHER
prune_keep "focus_pool_[0-9]*.json" $KEEP_OTHER
rm -f *.bak *.bak_* *.prebak 2>/dev/null || true

echo "[$(date '+%H:%M:%S')] ✅ 本地刷新完成。产物:" >> "$LOG"
ls -1 top200_report_*.html top200_scored_[0-9]*.json index.html 2>/dev/null >> "$LOG"
echo "result=success" >> "$LOG"
