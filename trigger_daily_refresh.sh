#!/bin/bash
# ============================================================================
# 每日刷新触发器 (供 WorkBuddy 定时任务调用)
#
# 背景: GitHub Actions 的 schedule 事件存在 4.8~8.8 小时系统性排队延迟
#       (实测 289~528 分钟, 20/20 超过 4h), 导致报告在北京 21:00~00:00 才更新。
#       本脚本改为「准点触发 workflow_dispatch」, 绕开 schedule 排队。
#
# 逻辑:
#   1. 取当前代理端口 (环境代理端口会变, 不可写死)
#   2. 查远程当日是否已产出 top200_scored_<当日>.json
#   3. 未产出 → 触发 workflow_dispatch; 已产出 → 跳过 (幂等)
#
# 用法: bash trigger_daily_refresh.sh
# ============================================================================
set -uo pipefail

REPO="cyz87687/stock-top200-report"
WORKFLOW="daily-refresh.yml"

# ---- 1. 动态获取代理 (环境代理端口每次可能不同) ----
PROXY=$(env | grep -iE '^(https?_proxy)=' | head -1 | sed 's/.*=//')
export HTTP_PROXY="${PROXY}"
export HTTPS_PROXY="${PROXY}"

# 北京时间当日
BJ_TODAY=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
BJ_NOW=$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')
BJ_HM=$(TZ=Asia/Shanghai date '+%H%M')
BJ_DOW=$(TZ=Asia/Shanghai date '+%u')   # 1=周一 ... 7=周日

echo "[${BJ_NOW} CST] 检查 ${REPO} 当日(${BJ_TODAY})数据 ..."
echo "  代理: ${PROXY:-（无）}"

# ---- 0. 收盘守卫: A股 15:00 收盘, 收盘前触发会抓到不完整数据 ----
#   周末(6/7)直接退出; 工作日 14:55 前不触发(预留 5 分钟缓冲, 避免尾盘瞬时价)。
if [ "${BJ_DOW}" -ge 6 ]; then
    echo "  ⏸️  周末(${BJ_DOW}), 非交易日, 跳过"
    exit 0
fi
if [ "${BJ_HM}" -lt "1455" ]; then
    echo "  ⏸️  当前 ${BJ_HM} 尚未收盘(需 >=1455), 跳过"
    exit 0
fi

# ---- 2. 查远程是否已有当日 scored json ----
CHECK_URL="https://api.github.com/repos/${REPO}/contents/top200_scored_${BJ_TODAY}.json"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    ${PROXY:+-x "$PROXY"} --connect-timeout 20 --max-time 40 \
    -H "Accept: application/vnd.github+json" \
    "${CHECK_URL}" 2>/dev/null || echo "000")

echo "  远程文件探测 HTTP: ${HTTP_CODE}"

if [ "${HTTP_CODE}" = "200" ]; then
    echo "  ✅ 当日数据已存在, 跳过触发 (幂等)"
    exit 0
fi

# ---- 3. 未产出 → 触发 workflow_dispatch ----
echo "  ⏳ 当日数据缺失, 触发 workflow_dispatch ..."
TRIGGER_OUT=$(gh workflow run "${WORKFLOW}" --repo "${REPO}" --ref main 2>&1)
TRIGGER_RC=$?
echo "  触发返回码: ${TRIGGER_RC}"
echo "  输出: ${TRIGGER_OUT}"

if [ ${TRIGGER_RC} -eq 0 ]; then
    echo "  ✅ 已触发, 报告将在约 40 分钟后产出"
else
    echo "  ❌ 触发失败, 请检查 gh 认证与网络"
    exit 1
fi
