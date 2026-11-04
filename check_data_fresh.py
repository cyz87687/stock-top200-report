#!/usr/bin/env python3
"""数据新鲜度守卫（v2.47 P1-7 → v3.0 H2 扩池覆盖率）—— 阻止假日/接口故障的陈旧数据进入评分链路。

背景（实测事故）：workflow 在 09-10、09-25、10-01~10-07 七个非正常日照常产出 scored json
（09-25 中秋、10-01~07 国庆为假日；09-10 疑似接口返回旧快照）。fetch 的 validate_data
只校验"成交额>0"，而假日快照的成交额同样>0 → 放行 → 陈旧价格进入 factor_history，
污染 Rank IC 的交易日对齐（v1 口径缺陷之一）。

三类检查，任一不过即 exit 非零（令 workflow 失败重试，不写评分数据）：
  1) 指数日线日期：上证指数最近一根日线 < 北京当日 → 假日/未收盘/行情接口延迟；
  2) 价格陈旧：新抓取的 top500_all_a.json 与上一交易日 scored json 价格 ≥80% 相同
     → 行情接口返回了旧快照；
  3) v3.0 H2 池覆盖率：主力池有效标的数 / 期望池大小(config.json pool_size)，
     <96% 警告(exit 0, 报告层公告) / <90% 熔断(exit 4, workflow 重试)。

用法（workflow Step 1.5，fetch 成功之后、step2 之前）：
  python3 check_data_fresh.py          # 通过 exit 0；不新鲜 exit 2/3/4
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


def index_last_date_sina():
    """新浪日线最新日期。==实测缺陷：新浪日线当日收盘后长时间不更新==。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=5")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data:
        return None
    return str(data[-1].get("day"))[:10]


def index_last_date_tx():
    """腾讯 fqkline 上证指数最新日期（备源）。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=sh000001,day,,,8,qfq")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    d = (j.get("data") or {}).get("sh000001") or {}
    kl = d.get("qfqday") or d.get("day") or []
    if not kl:
        return None
    return str(kl[-1][0])[:10]


def index_last_date():
    """上证指数最近一根日线日期 —— **多源取最新**。

    ==修复背景（2026-11-04 实测事故）==
    原实现只用新浪日线。今日新浪日线停在 11-03，而腾讯 fqkline 已有 11-04
    （上证收 3686.17，与 fetch 拿到的个股收盘价逐位一致）→ 单源判据把
    「真实存在的当日数据」误判成 stale，整条日链在 Step 1.5 被拦停。

    ==为什么不直接放宽阈值==
    判据本身是对的（「指数最新日线 < 北京当日 ⇒ 不新鲜」）。错的是把
    **单一数据源**当成事实基准。故改为多源交叉取 max：
      · 任一源报当日 → 当日行情确实存在 → 放行
      · 所有源都落后 → 真假日/真延迟 → 仍然拦停（风控不降级）
    两源历史日期一致，仅当日更新速度不同，故不存在「取 max 放到假数据」的风险。
    """
    import sys as _sys
    got = {}
    for nm, fn in (("sina", index_last_date_sina), ("tx", index_last_date_tx)):
        try:
            v = fn()
        except Exception as e:                       # 单源故障不拖垮整体
            print(f"   ⚠️ 指数源 {nm} 失败: {e}", file=_sys.stderr)
            continue
        if v:
            got[nm] = v
    if not got:
        return None
    newest = max(got.values())
    detail = " / ".join(f"{k}={v}" for k, v in sorted(got.items()))
    if len(set(got.values())) > 1:
        print(f"   ⚠️ 指数源不同步（{detail}）→ 取最新 {newest}")
    return newest


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
    """新 top500_all_a.json 与上一交易日 scored json 的价格相同占比；无比对对象返回 None。"""
    new_p = os.path.join(HERE, "top500_all_a.json")
    if not os.path.exists(new_p):
        new_p = os.path.join(HERE, "top200_all_a.json")  # 过渡期兼容
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
    for s in (new.get("main_pool") or new.get("top500") or new.get("top200") or []):
        p = s.get("price")
        code = _norm(s.get("code"))
        if code in old_px and isinstance(p, (int, float)) and p > 0:
            common += 1
            if abs(p - old_px[code]) < 1e-9:
                same += 1
    if common < 50:
        return None
    return same / common


def load_pool_config():
    """config.json 池参数(缺失回退旧口径 TOP200)"""
    try:
        cfg = json.load(open(os.path.join(HERE, 'config.json'), encoding='utf-8'))
        cg = cfg.get('coverage_guard', {})
        return {'pool_size': int(cfg.get('pool_size', 200)),
                'min_coverage': float(cg.get('min_pool_coverage', 0.96)),
                'meltdown': float(cg.get('meltdown_coverage', 0.90))}
    except Exception:
        return {'pool_size': 200, 'min_coverage': 0.96, 'meltdown': 0.90}


def pool_coverage():
    """v3.0 H2: 主力池覆盖率 = 有效标的数(price>0 且 amount>0) / 期望池大小。
    无比对文件返回 None。"""
    new_p = os.path.join(HERE, "top500_all_a.json")
    if not os.path.exists(new_p):
        new_p = os.path.join(HERE, "top200_all_a.json")  # 过渡期兼容
    if not os.path.exists(new_p):
        return None
    try:
        new = json.load(open(new_p, encoding="utf-8"))
    except Exception:
        return None
    main = new.get("main_pool") or new.get("top500") or new.get("top200") or []
    valid = [s for s in main
             if isinstance(s.get("price"), (int, float)) and s["price"] > 0
             and isinstance(s.get("amount"), (int, float)) and s["amount"] > 0]
    cfg = load_pool_config()
    # v4.11（2026-10-26）：config.pool_size<=0 = 不限规模，此时「期望池大小」不存在，
    #   必须用**文件内实际写入的 pool_size**（fetch 写的是 len(main_norm)）作基准。
    #   ⚠️ 若照旧用 cfg["pool_size"]=0，ratio = valid/1 恒 >1 → 熔断永不触发，
    #      覆盖率守门在「不限规模」模式下静默失效（与 P0「读文件自查基准必须对齐实际」同类坑）。
    expected = cfg["pool_size"]
    if expected <= 0:
        expected = int(new.get("pool_size") or 0) or len(main)
    return {"valid": len(valid), "expected": expected,
            "ratio": len(valid) / max(1, expected),
            "min_coverage": cfg["min_coverage"], "meltdown": cfg["meltdown"]}


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

    # 检查 3（v3.0 H2）：主力池覆盖率
    cov = pool_coverage()
    if cov is not None:
        print(f"   池覆盖率检测: 有效标的 {cov['valid']}/{cov['expected']} ({cov['ratio']:.1%}, "
              f"阈值 警告<{cov['min_coverage']:.0%} 熔断<{cov['meltdown']:.0%})")
        if cov["ratio"] < cov["meltdown"]:
            print(f"❌ 池覆盖率过低: {cov['ratio']:.1%} < {cov['meltdown']:.0%}")
            print("   → 行情接口大面积缺数，终止本次刷新（workflow 重试）")
            return 4
        if cov["ratio"] < cov["min_coverage"]:
            print(f"⚠️ 池覆盖率低于 {cov['min_coverage']:.0%}: {cov['ratio']:.1%}（放行，报告层将公告）")
        else:
            print("✅ 池覆盖率正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
