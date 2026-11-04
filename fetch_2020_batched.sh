#!/bin/zsh
# fetch_2020_batched.sh —— 分批拉取 2020 起日线（绕过单进程资源上限）
#
# 背景：本机长/大任务会被 SIGTERM（实测 --limit 50 即 Exit 137，--limit 20 OK）。
#       故按 20 只/批循环，靠 klines_2020_cache 断点续传。
# 用法：zsh fetch_2020_batched.sh [每批只数] [起始offset]

PY=~/.workbuddy/binaries/python/envs/default/bin/python
BATCH=${1:-20}
START=${2:-0}
TOTAL=5945
LOG=/tmp/fetch2020_batched.log

echo "=== 分批拉取开始 $(date '+%H:%M:%S')  每批 $BATCH 只  from $START ===" >> $LOG

off=$START
ok_batches=0
bad_batches=0
while [ $off -lt $TOTAL ]; do
  out=/tmp/fb_$off.log
  $PY -u build_long_history_2020.py --offset $off --limit $BATCH \
      --workers 3 --fetch-only > $out 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    ok_batches=$((ok_batches+1))
  else
    bad_batches=$((bad_batches+1))
    echo "  [warn] offset=$off rc=$rc 跳过" >> $LOG
  fi
  if [ $((off % 400)) -eq 0 ]; then
    n=$(ls data/klines_2020_cache/*.json 2>/dev/null | wc -l | tr -d ' ')
    echo "  offset=$off  缓存 $n 只  ok=$ok_batches bad=$bad_batches  $(date '+%H:%M:%S')" >> $LOG
  fi
  off=$((off+BATCH))
done

n=$(ls data/klines_2020_cache/*.json 2>/dev/null | wc -l | tr -d ' ')
echo "=== 拉取结束 $(date '+%H:%M:%S')  成功批 $ok_batches / 失败批 $bad_batches  缓存 $n 只 ===" >> $LOG
