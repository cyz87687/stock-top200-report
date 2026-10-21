#!/usr/bin/env bash
# 10-20 数据链路剩余步骤（step2 已于跨零点debug跑完并改回正确文件名）
# 顺序对齐 .github/workflows/daily-refresh.yml 的 Step 3.5~4
set -uo pipefail

cd /Users/yzreal/WorkBuddy/2026-07-24-18-07-15/stock-top200-report
PY=/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python
LOG=refresh_log_rest_1020.txt
SCORED=top200_scored_2026-10-20.json
DATA_DATE=2026-10-20

# 加载 LLM 环境（generate_ai_assessment 内部会 pop 代理）
set -a
source ./.llm_env
set +a

echo "=== 10-20 剩余步骤开始 $(date '+%Y-%m-%d %H:%M:%S') ===" > "$LOG"
echo "代理: HTTP_PROXY=${HTTP_PROXY:-<空>} HTTPS_PROXY=${HTTPS_PROXY:-<空>}" >> "$LOG"
echo "SCORED=$SCORED DATA_DATE=$DATA_DATE" >> "$LOG"

# Step 3.5: AI assessment
echo "[$(date '+%H:%M:%S')] Running generate_ai_assessment.py ..." >> "$LOG"
if $PY generate_ai_assessment.py "$SCORED" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI assessment generated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI assessment failed, fallback" >> "$LOG"
fi

# Step 3.6: Enhance
echo "[$(date '+%H:%M:%S')] Running enhance_scores.py ..." >> "$LOG"
if $PY enhance_scores.py "$SCORED" ai_assessment.json >> "$LOG" 2>&1; then
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
if $PY apply_sector_momentum.py "$SCORED" --mode evidence >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Sector dimension merged" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Sector merge failed" >> "$LOG"
fi

# Step 3.9: Portfolio
echo "[$(date '+%H:%M:%S')] Running pick_portfolio.py ..." >> "$LOG"
if $PY pick_portfolio.py "$SCORED" --top 10 --per-sector 3 --per-parent 2 >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Portfolio generated" >> "$LOG"
  $PY gen_portfolio_report.py >> "$LOG" 2>&1 || echo "⚠️ Portfolio report failed" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Portfolio generation failed" >> "$LOG"
fi

# Step 3.95: Track
echo "[$(date '+%H:%M:%S')] Running track_recommendations.py ..." >> "$LOG"
if $PY track_recommendations.py "$SCORED" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Recommendation tracking updated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Recommendation tracking failed" >> "$LOG"
fi

# Step 3.96: Factor IC
echo "[$(date '+%H:%M:%S')] Running factor_eval.py ..." >> "$LOG"
if $PY factor_eval.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Factor IC evaluation updated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Factor IC evaluation failed" >> "$LOG"
fi

# Step 3.97: Selfcheck
echo "[$(date '+%H:%M:%S')] Running selfcheck_pipeline.py ..." >> "$LOG"
$PY selfcheck_pipeline.py "$SCORED" >> "$LOG" 2>&1 \
  || echo "[$(date '+%H:%M:%S')] ⚠️ selfcheck reported suspicious items" >> "$LOG"

# Step 3.98: Focus pool
echo "[$(date '+%H:%M:%S')] Running fetch_focus_list.py ..." >> "$LOG"
if $PY fetch_focus_list.py --date "$DATA_DATE" --top 40 --scored "$SCORED" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI focus pool generated" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI focus pool failed" >> "$LOG"
fi

# Step 4: Report
echo "[$(date '+%H:%M:%S')] Running gen_report_top200.py ..." >> "$LOG"
if $PY gen_report_top200.py "$SCORED" >> "$LOG" 2>&1; then
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

echo "[$(date '+%H:%M:%S')] ✅ 10-20 剩余步骤完成。产物:" >> "$LOG"
ls -1 top200_report_*.html top200_scored_[0-9]*.json index.html focus_pool_*.json 2>/dev/null >> "$LOG"
echo "result=success" >> "$LOG"
