#!/usr/bin/env bash
# 推送今日产物 + 本地领先远程的代码到 GitHub（走 push_via_api.py，经代理）
# 关键: .gitignore 忽略 *.json, 未跟踪的 json 产物 git status 默认不显示,
#      故采用「领先 diff(代码/已提交数据) + 显式今日产物 glob(含被忽略 json)」双路收集, 再排序去重。
set -uo pipefail
cd /Users/yzreal/WorkBuddy/2026-07-24-18-07-15/stock-top200-report
PY=/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python
TODAY=2026-10-20   # 数据日期(step2跨零点运行, 文件名已按内部date字段改回10-20; 勿用系统日期)

files=()
add() { [ -f "$1" ] && files+=("$1"); }

# A) 本地领先 origin/main 的已提交文件 (v3.5.1 代码 + 昨日数据 + Q3表 + 设计md + index.html旧版 等)
while IFS= read -r f; do [ -f "$f" ] && files+=("$f"); done < <(git diff --name-only origin/main HEAD 2>/dev/null)

# B) 显式今日产物 (含被 .gitignore 忽略的 json, git status 默认不显示)
add index.html
add top200_report_$TODAY.html
add top200_scored_$TODAY.json
add top500_all_a.json
add ai_assessment.json
add focus_pool_$TODAY.json
add recommendation_track.json
add factor_history.json
add factor_ic.json
add sector/sw_map.json
add sector/sector_momentum_latest.json
add sector/sector_momentum_$TODAY.json
add sector/portfolio_$TODAY.json
add sector/portfolio_report_$TODAY.html
# C) factor-eval skill（.workbuddy/ 被 gitignore，需显式推送使其落地远程 git）+ 10-20 剩余步骤runner
add .workbuddy/skills/factor-eval/SKILL.md
add run_daily_rest_1020.sh

# 去重
uniq=()
while IFS= read -r f; do uniq+=("$f"); done < <(printf '%s\n' "${files[@]}" | sort -u)

echo "待推送文件数: ${#uniq[@]}"
printf '%s\n' "${uniq[@]}"

export COMMIT_MSG="2026-10-20 每日评分数据(东财按报告期growth 490/490/Q3景气加分32只/v3.5.2徽章修复保持; step2跨零点运行已按内部date改回10-20)"
$PY push_via_api.py "${uniq[@]}"
echo "push_exit=$?"
