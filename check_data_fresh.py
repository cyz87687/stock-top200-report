#!/usr/bin/env python3
"""数据新鲜度守卫（v2.47 P1-7）—— 阻止假日/接口故障的陈旧数据进入评分链路。

背景（实测事故）：workflow 在 09-10、09-25、10-01~10-07 七个非正常日照常产出 scored json
（09-25 中秋、10-01~07 国庆为假日；09-10 疑似接口返回旧快照）。fetch 的 validate_data
只校验"成交额>0"，而假日快照的成交额同样>0 → 放行 → 陈旧价格进入 factor_history，
污染 Rank IC 的交易日对齐（v1 口径缺陷之一）。

两类检查，任一不过即 exit 非零（令 workflow 失败重试，不写评分数据）：
  1) 指数日线日期：上证指数最近一根日线 < 北京当日 → 假日/未收盘/行情接口延迟；
  2) 价格陈旧：新抓取的 top200_all_a.json 与上一交易日 scored json 价格 ≥80% 相同
     → 行情接口返回了旧快照。

用法（workflow Step 1.5，fetch 成功之后、step2 之前）：
  python3 check_data_fresh.py          # 通过 exit 0；不新鲜 exit 2/3
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

# 清除代理环境变量: 行情接口需直连（与 fetch_top200_sina.py 一致）
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

UA = "Mozilla/5.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def _norm(code):
    c = str(code or "").strip().lower()
    return c[2:] if len(c) > 6 and c[:2] in ("sh", "sz", "bj") else c


def bj_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


def index_last_date():
    """上证指数最近一根日线日期（新浪日线，项目既有可用通道）。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=5")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data:
        return None
    return str(data[-1].get("day"))[:10]


def prev_scored_path():
    """远程/本地 git 中上一份 scored json（瘦身后仓库仅保留最新一份）。"""
    try:
        out = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"],
                             capture_output=True, text=True, cwd=HERE, timeout=60).stdout
    except Exception:
        return None
    files = [f for f in out.splitlines()
             if f.startswith("top200_scored_") and "_watch_" not in f]
    return sorted(files)[-1] if files else None


def price_stale_ratio():
    """新 top200_all_a.json 与上一交易日 scored json 的价格相同占比；无比对对象返回 None。"""
    new_p = os.path.join(HERE, "top200_all_a.json")
    if not os.path.exists(new_p):
        return None
    prev = prev_scored_path()
    if not prev:
        return None
    try:
        raw = subprocess.run(["git", "show", f"HEAD:{prev}"],
                             capture_output=True, text=True, cwd=HERE, timeout=60).stdout
        pd = json.loads(raw)
        new = json.load(open(new_p, encoding="utf-8"))
    except Exception:
        return None
    old_px = {}
    for r in pd.get("results", []):
        if isinstance(r.get("price"), (int, float)) and r["price"] > 0:
            old_px[_norm(r.get("code"))] = r["price"]
    common = same = 0
    for s in (new.get("top200") or []):
        p = s.get("price")
        code = _norm(s.get("code"))
        if code in old_px and isinstance(p, (int, float)) and p > 0:
            common += 1
            if abs(p - old_px[code]) < 1e-9:
                same += 1
    if common < 50:
        return None
    return same / common


def main():
    today = bj_today()

    # 检查 1：指数日线日期
    try:
        last = index_last_date()
    except Exception as e:
        print(f"⚠️ 指数日线获取失败({e})，跳过日期检查（仅靠价格陈旧检测）")
        last = None
    if last and last < today:
        print(f"❌ 数据不新鲜: 上证指数日线最新为 {last}，北京当日 {today}")
        print("   → 假日/未收盘/行情接口延迟，终止本次刷新（不写评分数据）")
        return 2
    if last:
        print(f"✅ 指数日线最新 {last} = 北京当日 {today}")

    # 检查 2：价格陈旧
    ratio = price_stale_ratio()
    if ratio is not None:
        print(f"   价格陈旧检测: 与上一交易日 scored json 价格相同占比 {ratio:.0%}")
        if ratio >= 0.8:
            print(f"❌ 数据陈旧: {ratio:.0%} 个股价格与上一交易日完全相同")
            print("   → 行情接口返回旧快照，终止本次刷新")
            return 3
        print("✅ 价格已更新（非陈旧快照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
