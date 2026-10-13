#!/usr/bin/env bash
# =============================================================================
# 本地复盘链路编排器（阶段0 / 阶段2 / 阶段3 自动化；阶段1 由 Agent 完成）
# =============================================================================
# 背景与设计
#   GitHub Actions 每日收盘后自动跑「取数 → 算法评分 → (规则引擎兜底点评) → 生成页面」，
#   保证 15:30 后页面先有一版可用数据。
#   本脚本在其之上做「本地深度复盘覆盖」：用逐公司联网核查的结论，重跑增强与页面，
#   把规则引擎兜底的点评替换为真实核查结论。
#
# 三层工程职责（v3.0 起明确分离，勿再混跑）
#   研究层   factor_eval.py / factor-backtest/  —— 因子 IC 登记与权重再拟合(周期性人工触发)
#   生产评分层 step2_stockscorer_v2.py 等      —— 每日 Actions 自动链路(评分/增强/板块动量/报告)
#   复盘层   本脚本                            —— 本地深度复盘覆盖(逐公司核查 → 注入 → 重生成页面)
#   因子回测与每日复盘解耦: finalize 不再调用 factor_eval.py(IC 登记归 Actions Step 3.96)
#
# 链路时序（v3.0 起：复盘前先互联网搜索当日重点关注标的）
#   ① Actions 产出当日 top200_scored_<date>.json  ← 前置条件
#   ② ./run_local_review.sh prepare               ← 阶段0: 互联网搜索焦点清单(机器源) + 导出候选 + 模板
#   ③ 【Agent】先读焦点清单 → 联网检索补充(--merge-search) → 逐公司核查，填写 review_research_<date>.json ← 阶段1
#   ④ ./run_local_review.sh finalize              ← 阶段2 校验 + 阶段3 注入 + 重生成页面
#   ⑤ ./run_local_review.sh status                ← 随时查看进度
#
# 子命令
#   status    查看当日链路进度（各阶段产物是否存在、候选与核查数量）
#   prepare   阶段0：互联网搜索当日焦点清单 + 导出复盘输入数据包 + 生成填写模板
#   focus     仅跑互联网焦点清单（机器源 + --emit-template 模板 / --merge-search 合并 Agent 检索）
#   finalize  阶段2+3：校验汇总 → 生成 ai_assessment → enhance → 报告 → 同步 index
#   check     仅跑阶段2 校验（不写页面；--force 可强制出汇总）
#   push      提交并推送（沿用 api.github.com 通道，失败时提示 push_via_api.py）
#
# 说明
#   - 不配置任何 LLM 密钥：本链路不调用在线模型，结论来自 Agent 联网核查。
#   - 所有数值来自 scored json 真实字段，校验器（9 条规则）拦截编造与张冠李戴。
# =============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

PY="/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python"
[ -x "$PY" ] || PY="python3"

BJ_DATE="$(TZ=Asia/Shanghai date '+%Y-%m-%d')"
CMD="${1:-status}"
shift 2>/dev/null || true

# 允许 --date YYYY-MM-DD 覆盖（补跑/复现测试用）
_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --date) BJ_DATE="$2"; shift 2 ;;
    *) _args+=("$1"); shift ;;
  esac
done
set -- "${_args[@]:-}"

SCORED="top200_scored_${BJ_DATE}.json"
FOCUS="focus_pool_${BJ_DATE}.json"
# v3.4: 焦点池文件名由 focus_list_ 改为 focus_pool_（替代观察池）；兼容旧名
[ -f "$FOCUS" ] || [ ! -f "focus_list_${BJ_DATE}.json" ] || FOCUS="focus_list_${BJ_DATE}.json"
INPUT="review_input_${BJ_DATE}.json"
RESEARCH="review_research_${BJ_DATE}.json"
MERGED="review_research_merged_${BJ_DATE}.json"
ISSUES="review_research_issues_${BJ_DATE}.md"

mark() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

# ---------------------------------------------------------------------------
case "$CMD" in
status)
  mark "本地复盘链路进度 · ${BJ_DATE}"
  printf '  ① 算法评分产物  : '; [ -f "$SCORED" ] && ok "$SCORED" || bad "$SCORED 缺失（需等 Actions 跑完）"
  printf '  ①b 焦点清单      : '; [ -f "$FOCUS" ] && ok "$FOCUS" || warn "$FOCUS 未生成（prepare 会自动跑互联网搜索）"
  printf '  ② 候选数据包    : '; [ -f "$INPUT" ] && ok "$INPUT" || warn "$INPUT 未生成"
  printf '  ③ 逐公司资料    : '; [ -f "$RESEARCH" ] && ok "$RESEARCH" || warn "$RESEARCH 未填写（阶段1 待做）"
  printf '  ④ 校验汇总      : '; [ -f "$MERGED" ] && ok "$MERGED" || warn "$MERGED 未生成"
  if [ -f "$INPUT" ]; then
    "$PY" - "$INPUT" "$RESEARCH" "$FOCUS" <<'PY'
import json, sys, os
inp = json.load(open(sys.argv[1], encoding='utf-8'))
st = inp.get('stats', {})
print(f"\n  候选 {st.get('n_candidates')} 只  深度 {st.get('by_tier', {}).get('deep')} / "
      f"轻量 {st.get('by_tier', {}).get('light')}")
print(f"  来源 {st.get('by_source')}")
if os.path.exists(sys.argv[3]):
    fl = json.load(open(sys.argv[3], encoding='utf-8'))
    n_pool_out = sum(1 for c in inp['candidates']
                     if c.get('in_main_pool') is False)
    print(f"  互联网焦点 {len(fl.get('focus') or [])} 只（机器源 {fl.get('sources_used')}，"
          f"Agent检索补充 {fl.get('agent_search', {}).get('n_additions', 0)}；池外焦点入复盘 {n_pool_out} 只）")
if os.path.exists(sys.argv[2]):
    res = json.load(open(sys.argv[2], encoding='utf-8'))
    done = {c.get('code') for c in (res.get('companies') or [])
            if (c.get('verdict') or '').strip() and (c.get('findings') or '').strip()}
    need = {c['code'] for c in inp['candidates'] if c.get('tier') == 'deep'}
    print(f"  已填 {len(done)}/{len(inp['candidates'])} 只；"
          f"深度档完成 {len(need & done)}/{len(need)}")
PY
  fi
  ;;

prepare)
  mark "阶段0.5 · 每日 AI 焦点池（复盘前置，v3.4 替代观察池）"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --scored "$SCORED" || warn "焦点池机器源失败(非致命, 继续)"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --emit-template || true
  if [ -f "$FOCUS" ]; then
    ok "焦点池: $FOCUS（$( "$PY" -c "import json;print(len(json.load(open('$FOCUS'))['focus']))" 2>/dev/null || echo '?') 只）"
  fi

  mark "阶段0 · 导出复盘输入（含互联网焦点标的）"
  if [ ! -f "$SCORED" ]; then
    bad "未找到 $SCORED —— 请等 GitHub Actions 产出当日评分后重试"
    exit 1
  fi
  "$PY" export_review_input.py "$SCORED" --focus "$FOCUS" --out "$INPUT" || exit 1
  mark "生成待填模板"
  "$PY" merge_review_research.py --input "$INPUT" --emit-template || exit 1
  echo
  ok "下一步（阶段1）：Agent 先读 $FOCUS 当日重点关注标的，"
  ok "  ① 把联网检索结论（龙虎榜/公告/政策/题材催化）写入 focus_list_${BJ_DATE}.search.json"
  ok "  ② 跑 ./run_local_review.sh focus --merge-search 合并进焦点清单"
  ok "  ③ 逐公司联网核查，填写 review_research_${BJ_DATE}.json（模板在 .template.json）"
  ;;

focus)
  mark "每日 AI 焦点池 · ${BJ_DATE}"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --scored "$SCORED" "$@" || exit 1
  if [ -f "$FOCUS" ]; then
    # 焦点清单更新后用最新清单重导候选(保持候选包与焦点一致)
    if [ -f "$SCORED" ]; then
      "$PY" export_review_input.py "$SCORED" --focus "$FOCUS" --out "$INPUT" 2>/dev/null \
        && ok "已用最新焦点清单重导候选: $INPUT" \
        || warn "候选重导失败(非致命, finalize 前可手动重跑)"
    fi
  fi
  ;;

finalize)
  mark "阶段2 · 校验并汇总逐公司资料"
  if [ ! -f "$RESEARCH" ]; then
    bad "未找到 $RESEARCH —— 阶段1 未完成"
    exit 1
  fi
  "$PY" merge_review_research.py --input "$INPUT" --research "$RESEARCH" || {
    bad "校验未通过，已中止（详见 $ISSUES）"
    exit 2
  }

  mark "阶段3 · 注入 ai_assessment.json（本地复盘路径）"
  cp -f ai_assessment.json "ai_assessment.json.bak_${BJ_DATE}" 2>/dev/null || true
  "$PY" generate_ai_assessment.py "$SCORED" ai_assessment.json --from-review "$MERGED" || exit 1
  ok "已备份原 ai_assessment → ai_assessment.json.bak_${BJ_DATE}"

  mark "重跑增强 + 板块动量 + 生成报告"
  cp -f "$SCORED" "${SCORED}.prelocal" 2>/dev/null || true
  "$PY" enhance_scores.py "$SCORED" ai_assessment.json || warn "enhance 失败"
  "$PY" apply_sector_momentum.py "$SCORED" --mode evidence 2>/dev/null || warn "板块动量并入失败(非致命)"
  # --replace-day: Actions 已于 15:30 用规则引擎兜底评级记过一次快照,
  #   本地复盘改变了评级分布, 必须替换当日快照而非追加（否则当日样本虚增、胜率污染）
  "$PY" track_recommendations.py "$SCORED" --replace-day 2>/dev/null || warn "推荐跟踪更新失败(非致命)"
  # 因子有效性评估(Rank IC) 归属研究层, 由每日 Actions 链路(Step 3.96)登记,
  #   复盘层不再调用 —— 工程职责分离(因子回测/每日复盘解耦, 2026-10-13 v3.0)。
  #   本地如需手动评估: $PY factor_eval.py(幂等, 同日 upsert 覆盖)
  "$PY" gen_report_top200.py "$SCORED" || { bad "报告生成失败"; exit 1; }

  LATEST_HTML="$(ls -t top200_report_*.html 2>/dev/null | head -1)"
  if [ -n "$LATEST_HTML" ]; then
    cp "$LATEST_HTML" index.html && ok "已同步 $LATEST_HTML → index.html"
  fi

  mark "完成"
  ok "本地复盘已注入页面。建议本地打开 index.html 核对后再推送。"
  ;;

check)
  mark "阶段2 · 仅校验"
  "$PY" merge_review_research.py --input "$INPUT" --research "$RESEARCH" "$@"
  ;;

push)
  mark "提交并推送"
  for f in export_review_input.py merge_review_research.py; do
    git add -f "$f" 2>/dev/null || true
  done
  git add -u gen_report_top200.py generate_ai_assessment.py 2>/dev/null || true
  git add index.html top200_report_*.html 2>/dev/null || true
  git add -f top200_scored_*.json ai_assessment.json recommendation_track.json 2>/dev/null || true
  # 研究层产物仅在本地点火过 factor_eval.py 时才需捎带(日常由 Actions 提交, 此处一般无变更)
  git add -f factor_history.json factor_ic.json 2>/dev/null || true
  # 复盘产物入库（模板与合并结果也保留，便于回溯当日核查依据；焦点清单=当日搜索证据）
  git add -f review_input_*.json review_research_*.json focus_list_*.json 2>/dev/null || true
  git rm --cached -q review_research_*.template.json 2>/dev/null || true
  if git diff --cached --quiet; then
    echo "  无变更需要提交"
    exit 0
  fi
  git commit -m "local-review: 本地复盘 ${BJ_DATE}（逐公司核查结论注入页面）" || true
  if git push origin main; then
    ok "推送成功"
  else
    warn "git push 失败 —— 可改用 Git Data API 通道："
    warn "  HTTPS_PROXY=\$(env | grep -oE 'https_proxy=http://[^ ]+' | cut -d= -f2) \\"
    warn "    $PY push_via_api.py --all-changed"
  fi
  ;;

*)
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac
