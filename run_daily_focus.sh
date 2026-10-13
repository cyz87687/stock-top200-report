#!/usr/bin/env bash
# =============================================================================
# 每日 AI 焦点池 · 本地编排器（v3.4，2026-10-13 老大指令）
# =============================================================================
# 背景
#   v3.1 删除「观察池」后，当日重点关注标的改由「互联网搜索」提供，但该环节
#   此前只挂在本地复盘脚本、且没真正落地「每日本地 AI 联网筛选」。本脚本把
#   「每日 AI 焦点池」做成可编排的独立链路，替代已删除的观察池。
#
#   观察池（旧：按成交额 200~1000 名用内部评分选）
#       ↓ 替换为
#   每日 AI 焦点池（新：机器源 + 本地 AI 联网检索，按当日真实信息选）
#
# 链路
#   ① prepare  机器源跑焦点池（东财人气榜/涨跌停池/强势股池）+ 生成 Agent 联网检索模板
#   ② 【Agent】联网检索当日题材催化/公告/政策/龙虎榜，写入 focus_pool_<date>.search.json ← 由 Agent 完成
#   ③ merge    合并 Agent 检索结论（--merge-search）
#   ④ render   重生成报告（含「🎯 每日 AI 焦点池」区块）+ 同步 index.html
#   ⑤ push     推送上线（Git Data API 通道，兼容 Actions 自动提交）
#
# 子命令
#   status   查看当日链路进度
#   prepare  机器源 + 生成 Agent 模板
#   merge    合并 Agent 检索补充
#   render   重生成报告 + 同步 index
#   push     提交并推送
#   all      prepare + render（无 Agent 补充时的快速出稿）
#
# 说明
#   - 不配置任何 LLM 密钥：联网检索由 Agent 执行，脚本只负责落格式与合并。
#   - 所有数值来自真实互联网接口 + 当日 scored json，校验器拦截编造。
# =============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

PY="/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python"
[ -x "$PY" ] || PY="python3"

BJ_DATE="$(TZ=Asia/Shanghai date '+%Y-%m-%d')"
CMD="${1:-status}"
shift 2>/dev/null || true

_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --date) BJ_DATE="$2"; shift 2 ;;
    *) _args+=("$1"); shift ;;
  esac
done
# 仅在确有余参时展开，避免空数组产生一个空串参数传给子命令
if [ ${#_args[@]} -gt 0 ]; then set -- "${_args[@]}"; else set --; fi

SCORED="top200_scored_${BJ_DATE}.json"
POOL="focus_pool_${BJ_DATE}.json"
SEARCH="focus_pool_${BJ_DATE}.search.json"

mark() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

case "$CMD" in
status)
  mark "每日 AI 焦点池 · 链路进度 ${BJ_DATE}"
  printf '  ① 主力池评分   : '; [ -f "$SCORED" ] && ok "$SCORED" || warn "$SCORED 缺失（等 Actions 产出后可 --scored 指定）"
  printf '  ② 机器源焦点池 : '; [ -f "$POOL" ] && ok "$POOL" || warn "$POOL 未生成（先跑 prepare）"
  printf '  ③ Agent检索模板: '; [ -f "$SEARCH" ] && ok "$SEARCH" || warn "$SEARCH 未生成"
  if [ -f "$SEARCH" ]; then
    "$PY" - "$SEARCH" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
    n = len(d.get('additions') or [])
    s = (d.get('ai_summary') or '').strip()
    print(f"     Agent 已填 {n} 条" + (f"；主线: {s[:40]}" if s else "；主线: (空)"))
except Exception as e:
    print(f"     解析失败: {e}")
PY
  fi
  if [ -f "$POOL" ]; then
    "$PY" - "$POOL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
print(f"  焦点池 {len(d.get('focus') or [])} 只（池内 {d.get('n_in_pool')} / 池外 {d.get('n_out_pool')}；"
      f"机器源 {d.get('sources_used')}；AI补充 {d.get('agent_search',{}).get('n_additions',0)}）")
PY
  fi
  ;;

prepare)
  mark "① 机器源 · 每日 AI 焦点池（${BJ_DATE}）"
  _scored_arg=""
  [ -f "$SCORED" ] && _scored_arg="--scored $SCORED"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --top 40 $_scored_arg || warn "机器源失败(非致命)"
  mark "生成 Agent 联网检索模板"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --emit-template || true
  [ -f "$POOL" ] && ok "焦点池: $POOL"
  echo
  ok "下一步（Agent）：联网检索当日「题材催化 / 公告 / 政策 / 龙虎榜 / 产业链」，"
  ok "  写入 $SEARCH 的 additions（含 name/code/reason/source），并填 ai_summary；"
  ok "  再跑 ./run_daily_focus.sh merge 合并 → ./run_daily_focus.sh render 出稿。"
  ;;

merge)
  mark "③ 合并 Agent 联网检索补充（${BJ_DATE}）"
  if [ ! -f "$SEARCH" ]; then
    bad "未找到 $SEARCH —— 请先 prepare 并让 Agent 填写"
    exit 1
  fi
  _scored_arg=""
  [ -f "$SCORED" ] && _scored_arg="--scored $SCORED"
  "$PY" fetch_focus_list.py --date "$BJ_DATE" --top 40 $_scored_arg --merge-search "$@" || exit 1
  ok "已合并 → $POOL"
  ;;

render)
  mark "④ 重生成报告（含「每日 AI 焦点池」区块）"
  if [ ! -f "$SCORED" ]; then
    bad "未找到 $SCORED —— 无法确定报告基准数据"
    exit 1
  fi
  "$PY" gen_report_top200.py "$SCORED" || { bad "报告生成失败"; exit 1; }
  LATEST_HTML="$(ls -t top200_report_*.html 2>/dev/null | head -1)"
  if [ -n "$LATEST_HTML" ]; then
    cp "$LATEST_HTML" index.html && ok "已同步 $LATEST_HTML → index.html"
  fi
  ok "建议本地打开 index.html 核对「🎯 每日 AI 焦点池」区块后再推送。"
  ;;

push)
  mark "⑤ 提交并推送"
  git add index.html top200_report_*.html 2>/dev/null || true
  git add -f focus_pool_*.json 2>/dev/null || true
  git rm --cached -q focus_pool_*.search.json 2>/dev/null || true
  git add -f fetch_focus_list.py gen_report_top200.py 2>/dev/null || true
  if git diff --cached --quiet; then
    echo "  无变更需要提交"; exit 0
  fi
  git commit -m "focus-pool: 每日AI焦点池 ${BJ_DATE}" || true
  # 首选 Git Data API（github.com:443 常被拦截时仍可用，且兼容 Actions 自动提交）
  _proxy="$(env | grep -iE '^https_proxy=' | head -1 | cut -d= -f2-)"
  if HTTPS_PROXY="$_proxy" "$PY" push_via_api.py index.html $(ls -t top200_report_*.html 2>/dev/null | head -1) $POOL fetch_focus_list.py gen_report_top200.py 2>/dev/null; then
    ok "经 Git Data API 推送成功"
  elif git push origin main; then
    ok "经 git push 推送成功"
  else
    warn "两种通道均失败 —— 检查代理端口：env | grep -iE '^(https?_proxy)='"
  fi
  ;;

all)
  mark "全流程（机器源 + 出稿，无 Agent 补充）"
  "$0" prepare --date "$BJ_DATE"
  "$0" render --date "$BJ_DATE"
  ;;

*)
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac
