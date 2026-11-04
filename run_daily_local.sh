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

# ═══════════════════════════════════════════════════════════════════════
# 步级状态记录（round117 P0-2 引入，round119 修定义位置）
# ─────────────────────────────────────────────────────────────────────
# 事故：本链 14 步里 12 步是 fail-open —— 失败只写一行 ⚠️ 到日志，主链照跑，
#   最后无条件 `echo result=success`。而 `result=*` **没有任何程序读取**
#   （实测全项目 grep：只有各脚本自己写自己的，没有消费者）。
#   ⇒ 「报告在降级状态下生成」这件事，报告自己不知道、也没人知道。
#
# 处置：每步跑完立刻 `_rec <step> <rc>` 落盘（append-only TSV），
#   gen_report 之前 `--finalize` 成 JSON，报告层据此披露「哪些步降级了 + 影响什么」。
#   ⚠️ 不改变任何步骤的阻塞性，只做记录与打标。
#
# ⚠️ selfcheck 的退出码必须如实记录（原来被 `|| echo` 吞掉）：
#   自检报可疑 = 十维总分一致性/未定义全局名可能有问题，
#   这比任何单步失败都更该让人看见。
#
# 🔒 round119 修复：以下定义**必须**放在第一个 `_rec` 调用点（Step 1 Fetch，
#   约第 60 行）之前。原实现把定义放在 Step 3.5（约第 160 行），bash 函数
#   必须先定义后使用 → fetch/check_data_fresh/step2 三步调用 `_rec` 时全部
#   `command not found`（实测 /tmp 日志 3 行），这三步的结果**从未落盘**。
#   后果：里那三步是 CRITICAL，pipeline_status 把它们判为 `not_run` →
#   verdict 恒为 `degraded`（即使它们全部成功、产物齐全）。
#   ⇒ 「记录器本身没就位」和「被记录的步骤没跑」在产物上完全一样。
PS_TSV=".pipeline_status.tsv"
$PY pipeline_status.py --reset >/dev/null 2>&1 || rm -f "$PS_TSV"
# _rec <step> <rc> [note]——记录一步结果（失败不影响主链）
_rec() {
  $PY pipeline_status.py --record "$1" --rc "${2:-0}" --note "${3:-}" >> "$LOG" 2>&1 || \
    echo "[$(date '+%H:%M:%S')] ⚠️ 状态记录失败: $1 rc=$2" >> "$LOG"
}

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
    echo "[$(date '+%H:%M:%S')] ✅ Fetch succeeded" >> "$LOG"; _rec fetch_top200_sina 0
  else
    echo "[$(date '+%H:%M:%S')] ❌ Fetch failed" >> "$LOG"
    _rec fetch_top200_sina 1 "行情源失败，当日无数据"
    echo "result=fetch_failed" >> "$LOG"
    exit 1
  fi

  # Step 1.5: 数据新鲜度守卫
  echo "[$(date '+%H:%M:%S')] Running check_data_fresh.py ..." >> "$LOG"
  if $PY check_data_fresh.py >> "$LOG" 2>&1; then
    echo "[$(date '+%H:%M:%S')] ✅ Data freshness OK" >> "$LOG"; _rec check_data_fresh 0
  else
    echo "[$(date '+%H:%M:%S')] ❌ Stale/holiday data" >> "$LOG"
    _rec check_data_fresh 1 "数据陈旧或非交易日"
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
    echo "[$(date '+%H:%M:%S')] ✅ Scoring succeeded" >> "$LOG"; _rec step2_stockscorer 0
  else
    echo "[$(date '+%H:%M:%S')] ❌ Scoring failed" >> "$LOG"
    _rec step2_stockscorer 1 "评分本体失败，无 scored"
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

# ═══════════════════════════════════════════════════════════════════════
# timeout 兼容层（2026-10-29 修复）
# ─────────────────────────────────────────────────────────────────────
# 事故：macOS 自带环境**没有 coreutils 的 `timeout`**（`which timeout` → not found，
#   也没有 gtimeout）。原脚本 step 3.44 / 3.45 写成 `timeout 600 $PY ...`，
#   于是 shell 报 "line 82: timeout: command not found" 并整条命令失败 →
#   **`append_klines_latest.py`（价格库增量）与 `archive_fund_universe.py`
#   （fund 全域归档）每天都被静默跳过**，只在日志留一行 ⚠️。
#   而 fund 归档正是 v4.14 P0 修复的核心步骤（覆盖率从 9% 提到全域），
#   长期跳过 = 该修复实际从未在本地链路生效。
#
# 修法：探测 `timeout` / `gtimeout`，都没有则定义 `_to()` 为纯 bash 超时包装
#   （后台执行 + 计时轮询 + 超时杀进程组），保证语义一致且跨平台。
# ═══════════════════════════════════════════════════════════════════════
if command -v timeout >/dev/null 2>&1; then
  _to() { timeout "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then
  _to() { gtimeout "$@"; }
else
  _to() {
    local secs="$1"; shift
    "$@" &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$waited" -ge "$secs" ]; then
        kill -TERM "$pid" 2>/dev/null
        sleep 1
        kill -KILL "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        return 124
      fi
      sleep 1
      waited=$((waited + 1))
    done
    wait "$pid"
  }
fi

# ═══════════════════════════════════════════════════════════════════════
# 步级状态记录：定义已上移至脚本头部（round119），此处不再重复定义。
#   原因：原位置在 Step 3.5，晚于 Step 1/1.5/2 的 `_rec` 调用 → 三步记录丢失。
# ═══════════════════════════════════════════════════════════════════════


#   archive_fund_universe 依赖 data/klines_long.json 的当日收盘价。
#   该库由 fetch_klines_long.py 全量维护（约 35 分钟），日常只需增量追加。
#   ⚠️ 必须在 archive_fund_universe 之前，否则归档拿不到当日价格 → 域为空。
echo "[$(date '+%H:%M:%S')] Running append_klines_latest.py (价格库增量) ..." >> "$LOG"
if _to 600 $PY append_klines_latest.py --need 8 >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Klines appended" >> "$LOG"; _rec append_klines_latest 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Klines append failed, archive may use stale price" >> "$LOG"
  _rec append_klines_latest 1 "价格库增量失败 → archive 会用陈旧收盘价"
fi

# Step 3.42 (P1-1 问题A): 刷新价格库元信息（有效横截面口径）
#   背景：data/klines_long_meta.json 原本**只有 fetch_klines_long.py 会写**，
#    而它是 35 分钟全量重抓、从未接入日链；日链用的是 append_klines_latest.py，
#    该脚本根本不碰 meta → 实测库到 2026-11-02、meta 停在 2026-10-27（差 6 天）。
#   本步重算并写回 valid_cross_sections（305 日）+ 剔除的 36 个建库初期日。
#   ⚠️ 必须在 append_klines_latest 之后（否则 meta 记的是昨天的库）。
echo "[$(date '+%H:%M:%S')] Running refresh_kline_meta.py (有效截面口径刷新) ..." >> "$LOG"
if $PY refresh_kline_meta.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Kline meta refreshed" >> "$LOG"; _rec refresh_kline_meta 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Kline meta refresh failed" >> "$LOG"
  _rec refresh_kline_meta 1 "有效截面口径未刷新 → 下游可能用到建库初期无效日"
fi

# Step 3.43 (P1-1 问题B): 前复权锚点漂移自检（分片轮转，每片约 36 只 ≈ 15s）
#   背景：append_klines_latest 的「已有日期绝不覆盖」在除权后会静默造成新旧锚点
#    混用（历史价不修正、新价已是新锚点）→ 跨除权日收益凭空多一个跌幅，不报错。
#   策略：按代码排序切 60 片，每天查一片，60 个交易日后全库各核一次；
#    游标持久化在 data/qfq_anchor_cursor.json，覆盖率可精确披露。
#   ⚠️ 曾考虑「每 20 日随机抽 10 只」，实测故障注入（000001 改 +2%）抽 60 只
#    都没命中 → 随机抽样对逐股独立事件近乎无效，故改轮转分片。
#   失败仅告警不阻断（锚点问题需要人工核查，不应让主链停在半路）。
echo "[$(date '+%H:%M:%S')] Running verify_qfq_anchor.py (前复权锚点自检) ..." >> "$LOG"
if _to 240 $PY verify_qfq_anchor.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ qfq anchor clean" >> "$LOG"; _rec verify_qfq_anchor 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ qfq anchor drift detected, see data/qfq_anchor_audit.json" >> "$LOG"
  _rec verify_qfq_anchor 1 "检出前复权锚点漂移 → 跨除权日收益率可能失真，需人工核查"
fi

# Step 3.45: fund 维全域 PIT 归档（v4.14 P0 修复，2026-10-29）
#   背景：唯一数据源 data/fwd_hist/*.json 每日只存「当日在 TOP 池内」的 190~490 只，
#    而研究面板域约 2000 只 → fund（权重 0.242，最大单一权重）覆盖率只有 9~22%，
#    且「fund 有值」与「在池」100% 重合 → 实测该指示变量 IC(h20) = −0.1796（t=−8.53），
#    fund 维被污染成「估值 × 入池」的混合体（见 audit_round45）。
#   ⚠️ 更严重：data/fwd_hist 全项目 16 个引用方无一写入，自 2026-10-23 已彻底断流。
#   本步把归档域从「池内」扩到「EM财务 ∩ 申万 ∩ 有价格」全域，并恢复每日写入。
#   必须在 step2 之后（需要当日价格与 PIT 报告期），必须在 gen_report 之前（报告读覆盖数）。
#   失败不阻断主链（报告层会自动隐藏本次新增的披露条，不回退旧值）。
echo "[$(date '+%H:%M:%S')] Running archive_fund_universe.py (fund 全域归档) ..." >> "$LOG"
if _to 900 $PY archive_fund_universe.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ fund universe archived" >> "$LOG"; _rec archive_fund_universe 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ fund archive failed/timeout, fund coverage stays as-is" >> "$LOG"
  _rec archive_fund_universe 1 "fund 全域归档失败/超时 → fund 维(.20)覆盖率停在旧值"
fi

# Step 3.46: 前瞻估值快照归档（2026-10-29 补接入，断流根因闭环）
#   背景：data/fwd_hist/*.json 是「当日快照、过期即失」的前瞻估值（fwd_pe / pe_ladder），
#    其唯一写入方 archive_fwd_snapshot.py 此前在日链中**零调用**（round101 诊断：
#    data/fwd_hist 自 2026-10-23 起彻底断流）。本步补接入，消除断流根因。
#   依赖 step2 产出的 $SCORED_JSON；日期取自文件名（archive_fwd_snapshot 的兜底分支
#    已修为 UTC 基准，但正常路径用文件名日期，无时区问题）。
#   失败不阻断主链（报告层按字段披露覆盖数，无则隐藏）。
echo "[$(date '+%H:%M:%S')] Running archive_fwd_snapshot.py (前瞻快照归档) ..." >> "$LOG"
if $PY archive_fwd_snapshot.py --scored "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ fwd snapshot archived" >> "$LOG"; _rec archive_fwd_snapshot 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ fwd snapshot failed, fwd coverage stays as-is" >> "$LOG"
  _rec archive_fwd_snapshot 1 "前瞻快照归档失败 → fwd_pe 覆盖缺失"
fi

# Step 3.5: AI assessment
echo "[$(date '+%H:%M:%S')] Running generate_ai_assessment.py ..." >> "$LOG"
if $PY generate_ai_assessment.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI assessment generated" >> "$LOG"; _rec generate_ai_assessment 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI assessment failed, fallback" >> "$LOG"
  _rec generate_ai_assessment 1 "AI 评估失败 → AI 加权维度缺失"
fi

# Step 3.6: Enhance
echo "[$(date '+%H:%M:%S')] Running enhance_scores.py ..." >> "$LOG"
if $PY enhance_scores.py "$SCORED_JSON" ai_assessment.json >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Enhancement succeeded" >> "$LOG"; _rec enhance_scores 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Enhancement failed" >> "$LOG"
  _rec enhance_scores 1 "评分增强未写入 → 增强项缺失"
fi

# Step 3.7: Sector momentum
echo "[$(date '+%H:%M:%S')] Running sector_momentum_daily.py ..." >> "$LOG"
if $PY sector_momentum_daily.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Sector momentum computed" >> "$LOG"; _rec sector_momentum_daily 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Sector momentum failed" >> "$LOG"
  _rec sector_momentum_daily 1 "行业动量未计算"
fi

# Step 3.8: Apply sector
echo "[$(date '+%H:%M:%S')] Running apply_sector_momentum.py ..." >> "$LOG"
if $PY apply_sector_momentum.py "$SCORED_JSON" --mode evidence >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Sector dimension merged" >> "$LOG"; _rec apply_sector_momentum 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Sector merge failed" >> "$LOG"
  _rec apply_sector_momentum 1 "行业维未并入 total → 与权重声明不符"
fi

# Step 3.9: Portfolio
echo "[$(date '+%H:%M:%S')] Running pick_portfolio.py ..." >> "$LOG"
if $PY pick_portfolio.py "$SCORED_JSON" --top 10 --per-sector 3 --per-parent 2 >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Portfolio generated" >> "$LOG"; _rec pick_portfolio 0
  $PY gen_portfolio_report.py >> "$LOG" 2>&1 || echo "⚠️ Portfolio report failed" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Portfolio generation failed" >> "$LOG"
  _rec pick_portfolio 1 "当日无组合建议"
fi

# Step 3.91: 组合层风险度量（round122 P0-3 → round122 P1 接日链）
#   背景：pick_portfolio 只管「选出哪几只 + 各占多少权重」，对组合的
#     **年化波动 / 相对候选池 beta / 回望最大回撤 / 满仓等价波动 / 集中度**
#     一概不算 —— 而这几项恰是金融工程最基础的组合体检项。
#     portfolio_risk.py 计算这些指标并把 risk 块**回填**进 sector/portfolio_<date>.json，
#     gen_report 的「量化组合层风险体检」块直接消费该块。
#   🔒 必须在 pick_portfolio 之后（要读当日选出的 weights 与 candidate_codes）、
#     且在 gen_report 之前（报告要渲染）。与 step2/enhance 同型的顺序依赖。
#   ⚠️ 铁律「脚本不在日链=必然陈旧」：此脚本此前为一次性脚本，未接入时
#     portfolio json 里的 risk 块永远是上一次手工跑的残留值。
if $PY portfolio_risk.py --inplace >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Portfolio risk metrics inplace" >> "$LOG"; _rec portfolio_risk 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Portfolio risk metrics failed" >> "$LOG"
  _rec portfolio_risk 1 "组合层波动/beta/回撤缺失 → 报告无法披露持仓固有风险"
fi

# Step 3.95: Track
echo "[$(date '+%H:%M:%S')] Running track_recommendations.py ..." >> "$LOG"
if $PY track_recommendations.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Recommendation tracking updated" >> "$LOG"; _rec track_recommendations 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Recommendation tracking failed" >> "$LOG"
  _rec track_recommendations 1 "跟踪曲线断点"
fi

# Step 3.96: 组合日频指标（依赖 Step 3.95 的 records，故必须排在其后）
if $PY portfolio_metrics.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Portfolio metrics updated" >> "$LOG"; _rec portfolio_metrics 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Portfolio metrics failed" >> "$LOG"
  _rec portfolio_metrics 1 "组合日频指标停在上一日 → 换手/回撤视图陈旧"
fi

# Step 3.955: 研究面板新鲜度 + 权重覆盖审计（round117 P0-4）
#   背景：data/panel_round66.json 是 36 个脚本共用的研究面板，唯一生产者
#     audit_round66_clean_panel.py **从未接入日链** → 按铁律「一次性脚本不在日链
#     必然陈旧」，实测末日停在 2026-10-27（落后 6 天）。
#   更严重的是**结构性盲区**：当前 9 维生效权重合计 1.0000，其中 score_growth
#     （权重 .10）在 panel 里根本没有这一列 → 任何基于 panel 的 IC / 门禁检验
#     都无法评估这 10% 权重。「G5 未过」不等于「该维度无效」，而是「没测」。
#   本步不重建 panel（属「补全数据」范畴，已明确先放一下），只做审计与披露。
#   必须在 gen_report 之前（报告要渲染）。
echo "[$(date '+%H:%M:%S')] Running panel_audit.py (面板新鲜度与权重盲区) ..." >> "$LOG"
if $PY panel_audit.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ panel audit done" >> "$LOG"; _rec panel_audit 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ panel audit failed" >> "$LOG"
  _rec panel_audit 1 "panel 审计失败 → 报告无法披露权重盲区"
fi

# Step 3.96: Factor IC + 十维权重每日重估 (v3.5.5)
# ⚠️ round125 修（原 P2-2，实为 P1）：上一版把注释与 echo **写在同一行**：
#     `# Step 3.96: ... (v3.5.5)echo "[$(date ...)] Running factor_eval.py ..."`
#   整行被 `#` 注掉 → 「Running factor_eval.py」这行日志**从未打印过**，
#   但真正致命的是它下面那个 if 块**一直在跑**（块本身没被注掉），
#   所以症状不是「不执行」，而是「执行了但日志里查不到它启动」——
#   排障时看日志会误判成「factor_eval 没进日链」。
#   实测证据：factor_ic.json 是 02:58 手工产物、日链日志 grep 'Factor IC' 为 0。
#   拆成两行后日志恢复，且与前后步骤格式统一。
echo "[$(date '+%H:%M:%S')] Running factor_eval.py (Rank IC + daily weight re-estimation) ..." >> "$LOG"
if $PY factor_eval.py --optimize-weights >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Factor IC + weight re-estimation updated" >> "$LOG"; _rec factor_eval 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ Factor IC evaluation failed" >> "$LOG"
  _rec factor_eval 1 "IC/权重视图停在上一日"
fi

# ══════════════════════════════════════════════════════════════════════════
# Step 3.965: 长面板增量（round124 P1-2 新增）
# ──────────────────────────────────────────────────────────────────────────
# 为什么必须进日链：
#   脚本不在日链 = 必然陈旧（本项目血泪铁律）。long panel 是 h=20 唯一
#   可引用 IC 的来源，若靠手工跑，一周后它就会与当日行情脱节，
#   而页面仍在显示它的结论 → 与 panel_round66「TTL 未过期≠数据新鲜」同型事故。
#
# 三条命令的顺序不可颠倒：
#   1) fetch_klines_2020.py   只补当日新增 bar（增量，--skip-cached 天然幂等）
#   2) fix_negative_qfq.py    修前复权负价（高分红股，东财口径固有缺陷）
#   3) recompute_ic_long_v2.py 重算三周期 IC → ic_long_2020.json
#      → ic_dsr.py             补算 DSR（P1-3 判定链依赖）
#
# ⚠️ 本机资源纪律：这三步都是分钟级，实测单步会被 SIGTERM 回收。
#   因此**默认降级为「仅当 ic_long_2020.json 超过 STALE_DAYS 天才重建」**，
#   而不是每日全量。IC 结论本来就不应逐日变动；日更的意义是「不脱节」，
#   不是「天天变数字」。强制重建用 --rebuild-long-ic。
LONG_IC_JSON="data/ic_long_2020.json"
LONG_STALE_DAYS=7
# 强制重建开关：环境变量 REBUILD_LONG_IC=1 或首个位置参数 --rebuild-long-ic
REBUILD_LONG_IC="${REBUILD_LONG_IC:-0}"
for _a in "$@"; do
  [ "$_a" = "--rebuild-long-ic" ] && REBUILD_LONG_IC=1
done
_long_needs_rebuild=1
if [ -f "$LONG_IC_JSON" ]; then
  _age=$(( ( $(date +%s) - $(stat -f %m "$LONG_IC_JSON" 2>/dev/null || echo 0) ) / 86400 ))
  if [ "$_age" -lt "$LONG_STALE_DAYS" ]; then
    _long_needs_rebuild=0
    echo "[$(date '+%H:%M:%S')] long IC 新鲜度 OK（${_age}d < ${LONG_STALE_DAYS}d），跳过重建" >> "$LOG"
    # ⚠️ 跳过也必须记账（2026-11-04 修）：原实现只打日志、不调 _rec，
    #   而 pipeline_status 的「应跑未跑」判据是「清单里存在但从未 _rec」，
    #   于是每天正常跳过都会在收尾时被报成 `❗ 应跑未跑: long_panel_ic`，
    #   把「按设计跳过」误诊成「漏跑」。同型病见 round117/119 的死代码事故。
    _rec long_panel_ic 0 "新鲜度 ${_age}d < ${LONG_STALE_DAYS}d，按设计跳过重建（非漏跑）"
  fi
fi
if [ "$_long_needs_rebuild" = "1" ] || [ "$REBUILD_LONG_IC" = "1" ]; then
  echo "[$(date '+%H:%M:%S')] Rebuilding long panel IC (round124 P0-1) ..." >> "$LOG"
  if $PY fetch_klines_2020.py --codes data/domain_2029.json --skip-cached >> "$LOG" 2>&1; then
    echo "[$(date '+%H:%M:%S')] ✅ long kline incremental done" >> "$LOG"
    if $PY recompute_ic_long_v2.py >> "$LOG" 2>&1; then
      $PY ic_dsr.py >> "$LOG" 2>&1 || true
      echo "[$(date '+%H:%M:%S')] ✅ long IC + DSR rebuilt" >> "$LOG"
      _rec long_panel_ic 0
    else
      echo "[$(date '+%H:%M:%S')] ⚠️ recompute_ic_long_v2 failed" >> "$LOG"
      _rec long_panel_ic 1 "h=20 权威 IC 停在上一版；页面须标注 long_panel_ic 已陈旧"
    fi
  else
    echo "[$(date '+%H:%M:%S')] ⚠️ long kline incremental failed" >> "$LOG"
    _rec long_panel_ic 1 "长面板行情增量失败；IC 未重算，页面须标注陈旧"
  fi
fi

# Step 3.97: Selfcheck
# ⚠️ round117 P0-2：退出码必须如实记录，不得再用 `|| echo` 吞掉。
#   自检 = 「未定义全局名扫描 + 十维总分一致性」，报可疑意味着**总分口径可能已错**，
#   比任何单步失败都严重。这里仍不阻断主链（报告还能出），但：
#     ① 状态文件记 rc=1 → 报告层必须显示「自检未通过」
#     ② 日志独立写一行醒目告警
echo "[$(date '+%H:%M:%S')] Running selfcheck_pipeline.py ..." >> "$LOG"
$PY selfcheck_pipeline.py "$SCORED_JSON" >> "$LOG" 2>&1
SELFCHECK_RC=$?
if [ "$SELFCHECK_RC" -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] ✅ selfcheck passed" >> "$LOG"
  _rec selfcheck_pipeline 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ selfcheck reported suspicious items (exit $SELFCHECK_RC)" >> "$LOG"
  echo "[$(date '+%H:%M:%S')]    → 详见上方 selfcheck 输出；报告层将标注「自检未通过」" >> "$LOG"
  _rec selfcheck_pipeline "$SELFCHECK_RC" "自检发现可疑项（退出码 $SELFCHECK_RC）"
fi

# Step 3.98: Focus pool
echo "[$(date '+%H:%M:%S')] Running fetch_focus_list.py ..." >> "$LOG"
if $PY fetch_focus_list.py --date "$BJ_TODAY" --top 40 --scored "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ AI focus pool generated" >> "$LOG"; _rec fetch_focus_list 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ AI focus pool failed" >> "$LOG"
  _rec fetch_focus_list 1 "关注池停在上一日"
fi

# Step 3.99: 市场状态与风险预算（round109 / round109b）
# ⚠️ 顺序铁律：必须在 gen_report_top200.py **之前**执行。
#   原因：报告层的 Hero「叠加风险预算后 建议敞口上限」与状态卡片都读
#         regime_forecast.json；若在 gen_report 之后跑，报告读到的是**昨天的**产物，
#         而日期校验会把它整块跳过 → 页面静默缺失（与 fetch_focus_list 同类的顺序坑）。
# ⚠️ 指数缓存同步：fetch_bench.py 单独运行才更新 data/bench/*.json。
#   若不跑，regime_model 会用陈旧指数算出与当日涨跌矛盾的状态
#   （实测踩坑：11-02 数据 82 分普涨，但指数缓存停在 10-29 → 判成"下跌市"）。
# 🔒 round109b 新增：regime_model.py 内置**日期硬门禁**，指数日期≠数据日期时
#   直接 exit 2 且**不写** regime_forecast.json。故此处必须区分 exit code，
#   不能一律当"失败不阻断" —— 门禁拦截是**数据管道故障**，需要人看见。
echo "[$(date '+%H:%M:%S')] Running fetch_bench.py (指数基准增量) ..." >> "$LOG"
if $PY fetch_bench.py >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ 指数基准已更新" >> "$LOG"; _rec fetch_bench 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ 指数基准更新失败（预判将基于旧指数，可能失真）" >> "$LOG"
  _rec fetch_bench 1 "指数基准未更新 → 市场状态可能基于陈旧指数而失真"
fi

echo "[$(date '+%H:%M:%S')] Running regime_model.py (市场状态与风险预算) ..." >> "$LOG"
$PY regime_model.py --out regime_forecast.json >> "$LOG" 2>&1
REGIME_RC=$?
if [ "$REGIME_RC" -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] ✅ 市场状态与风险预算已生成" >> "$LOG"; _rec regime_model 0
elif [ "$REGIME_RC" -eq 2 ]; then
  # 门禁拦截 or 生成失败：产物未更新，报告会显示占位 + 告警
  echo "[$(date '+%H:%M:%S')] ⛔ 市场状态门禁拦截/生成失败(exit 2)" >> "$LOG"
  echo "[$(date '+%H:%M:%S')]    → 常见原因：指数缓存落后于数据日期。已跳过生成，" \
       "报告将显示「数据管道告警」占位。" >> "$LOG"
  _rec regime_model 2 "日期门禁拦截（指数缓存落后于数据日期）→ 报告显示管道告警占位"
else
  echo "[$(date '+%H:%M:%S')] ⚠️ 市场状态生成异常(exit $REGIME_RC)，报告将显示占位说明" >> "$LOG"
  _rec regime_model "$REGIME_RC" "市场状态生成异常，报告显示占位"
fi

# Step 4: Report
# ⚠️ round117 P0-2：gen_report 之前必须 --finalize 步级状态，
#   否则报告读不到当日「哪些步降级了」→ 又会静默出一份"看起来正常"的报告。
echo "[$(date '+%H:%M:%S')] Finalizing pipeline status ..." >> "$LOG"
# SKIP_CORE=1 时 fetch/fresh/step2 未执行属预期（scored 已存在），要显式告知，
# 否则状态机会把这 3 步算成「应跑未跑」→ 误报 degraded。
PS_SKIP_ARG=""
[ "$SKIP_CORE" -eq 1 ] && PS_SKIP_ARG="--skip-core"
$PY pipeline_status.py --finalize --date "$BJ_TODAY" $PS_SKIP_ARG >> "$LOG" 2>&1 \
  || echo "[$(date '+%H:%M:%S')] ⚠️ 步级状态汇总失败（报告将显示「未记录」）" >> "$LOG"
PS_VERDICT=$($PY pipeline_status.py --show $PS_SKIP_ARG 2>/dev/null | head -1)
echo "[$(date '+%H:%M:%S')] Pipeline status: ${PS_VERDICT:-unknown}" >> "$LOG"

echo "[$(date '+%H:%M:%S')] Running gen_report_top200.py ..." >> "$LOG"
if $PY gen_report_top200.py "$SCORED_JSON" >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ Report generation succeeded" >> "$LOG"; _rec gen_report_top200 0
  LATEST_HTML=$(ls -t top200_report_*.html 2>/dev/null | head -1)
  if [ -n "$LATEST_HTML" ]; then
    cp "$LATEST_HTML" index.html
    echo "[$(date '+%H:%M:%S')] ✅ Copied $LATEST_HTML → index.html" >> "$LOG"
  fi
else
  echo "[$(date '+%H:%M:%S')] ❌ Report generation failed" >> "$LOG"
  _rec gen_report_top200 1 "报告生成失败"
  echo "result=report_failed" >> "$LOG"
  exit 1
fi

# Prune（保持与线上一致的保留策略）
# ═══════════════════════════════════════════════════════════════════════
# P1-3（2026-11-03）：保留窗口外的产物**归档而非删除**
#   原实现用 `rm -f` 直接删 —— 问题：
#     ① top200_scored_YYYY-MM-DD.json 是**当日全部评分结果**（约 10~12MB），
#        删掉后 reports/ 里引用该日数字的结论就失去了可复核来源；
#        实测 2026-10-29 那份 9.93MB 至今还在，只因此后没跑过日链，属"侥幸留存"。
#     ② 项目铁律要求「留证改 .snap_*」而不是删，但这里一直是 rm，口径不一致。
#   改为：移动到 archive/pruned/，保留 N 个交易日（默认 20）后仍然保留文件本身
#    （长期可人工清理），**任何情况下都不再 rm 生产产物**。
#   例外：*.bak/*.prebak 是临时备份，仍按原逻辑直接删（它们不是产物）。
#
# ⚠️ 保留窗口变量必须先定义：prune_keep 内部用 `[ $i -gt "$2" ]`，
#    若 $2 为空 → `-gt ""` 在部分 shell 下报错或被当作 0 → **会把全部文件归档**。
KEEP_SCORED=2       # 根目录 top200_scored_*.json 保留最近 2 个交易日
KEEP_OTHER=1        # 报告 html / 组合 / 行业动量 / 焦点池 各保留最近 1 个
ARCHIVE_PRUNED="archive/pruned"
mkdir -p "$ARCHIVE_PRUNED" 2>/dev/null || true
PRUNE_KEEP_DAYS=20          # 归档目录里按天数分桶，便于人工成批清理

prune_keep() {
  local i=0 f
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    i=$((i+1))
    if [ $i -gt "$2" ]; then
      # 归档（保留）而非删除
      mv "$f" "$ARCHIVE_PRUNED/" 2>/dev/null && echo "archived: $f → $ARCHIVE_PRUNED/" >> "$LOG"
    fi
  done < <(ls -1 $1 2>/dev/null | sort -r)
}
prune_keep "top200_report_*.html" $KEEP_OTHER
# 评分产物：保留最近 KEEP_SCORED 个，其余归档（原为 xargs rm -f）
ls -1 top200_scored_[0-9]*.json 2>/dev/null | sort -r | awk -v k=$KEEP_SCORED 'NR>k' | \
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    mv "$f" "$ARCHIVE_PRUNED/" 2>/dev/null && echo "archived: $f → $ARCHIVE_PRUNED/" >> "$LOG"
  done
prune_keep "sector/portfolio_[0-9]*.json" $KEEP_OTHER
prune_keep "sector/portfolio_report_*.html" $KEEP_OTHER
prune_keep "sector/sector_momentum_[0-9]*.json" $KEEP_OTHER
prune_keep "focus_pool_[0-9]*.json" $KEEP_OTHER
# 只有临时备份直接删（它们不是产物，无复核价值）
rm -f *.bak *.bak_* *.prebak 2>/dev/null || true

# 孤儿 json 检测 + 归档（P1-3）：三档判定（字面量/f-string/glob），只归档不删除
#   失败不阻断主链 —— 这只是整理，不影响当日数字。
if $PY orphan_scan.py --apply >> "$LOG" 2>&1; then
  echo "[$(date '+%H:%M:%S')] ✅ orphan scan done" >> "$LOG"; _rec orphan_scan 0
else
  echo "[$(date '+%H:%M:%S')] ⚠️ orphan scan failed" >> "$LOG"
  _rec orphan_scan 1 "孤儿 json 未归档 → 根目录继续膨胀"
fi

echo "[$(date '+%H:%M:%S')] ✅ 本地刷新完成。产物:" >> "$LOG"
ls -1 top200_report_*.html top200_scored_[0-9]*.json index.html 2>/dev/null >> "$LOG"

# ═══════════════════════════════════════════════════════════════════════
# 最终结果如实输出（round117 P0-2）
# ─────────────────────────────────────────────────────────────────────
# 原实现无条件 `echo result=success` —— 12 个 fail-open 步骤全失败也是 success。
# 现在按步级状态判决：ok / degraded / failed / unknown，并把降级步骤一并列出。
# ⚠️ 保留 result=success 这个字面值（外部可能已有消费者按它匹配），
#   降级时改为 result=degraded 以让差异可见。
# ═══════════════════════════════════════════════════════════════════════
PS_JSON="pipeline_status_${BJ_TODAY}.json"
PS_FINAL=$($PY - "$PS_JSON" <<'PYEOF' 2>/dev/null || echo "unknown"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    print(d.get("verdict") or "unknown")
except Exception:
    print("unknown")
PYEOF
)
echo "pipeline_verdict=${PS_FINAL}" >> "$LOG"
case "$PS_FINAL" in
  ok)       echo "result=success"  >> "$LOG" ;;
  degraded) echo "result=degraded" >> "$LOG"
            $PY pipeline_status.py --show 2>/dev/null | tail -n +2 >> "$LOG" || true ;;
  failed)   echo "result=failed"   >> "$LOG"
            $PY pipeline_status.py --show 2>/dev/null >> "$LOG" || true ;;
  *)        echo "result=unknown"  >> "$LOG" ;;
esac
