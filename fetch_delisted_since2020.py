"""fetch_delisted_since2020.py —— 补齐 2020 年后退市股的行情与财务缓存。

为什么必须做（本轮最严重的数据缺陷，实测）：
    domain_2029 取自「当前在市候选池」→ 2020 年后退市的 **247 只一只都没有**。
    实测 panel_round124_long 的 2029 只里，99.5% 活到 2026-11-03，
    只有 11 只中途消失。这就是**幸存者偏差样本**：
    退市股是表现最差的一批，系统性剔除会扭曲整个 IC 与分组收益。

    更严重的连带效应：财务缓存 `data/fin_cache_em` 对 247 只退市股覆盖为 0/247
    → 这些股票的 ep/quality/growth 全 None → 连「打分」都做不到。

做法：
    1) 行情：ak.stock_zh_a_hist(adjust="qfq")，写 data/klines_2020_cache/{code}.json
       格式与 fetch_klines_2020.py 完全一致（rows=[date,o,c,h,l,vol手,amt,turn]），
       这样 assemble 阶段无需区分来源。
    2) 财务：ak.stock_financial_abstract，写 data/fin_cache_em/{code}.json
       键名沿用东财真实指标名（见 build_panel_long.py 注释）。
    ⚠️ 本机长任务会被 SIGTERM → 分批 + 缓存断点续传 + 前台串行。
"""
import json
import os
import sys
import time

import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
KLDIR = os.path.join(HERE, "data", "klines_2020_cache")
FINDIR = os.path.join(HERE, "data", "fin_cache_em")
SRC = os.path.join(HERE, "data", "delisted_since2020.json")
START, END = "20200101", "20261104"


def fetch_kline(code):
    fp = os.path.join(KLDIR, f"{code}.json")
    if os.path.exists(fp):
        return "cached"
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=START, end_date=END, adjust="qfq")
    except Exception as e:
        return f"fail:{type(e).__name__}"
    if df is None or len(df) == 0:
        return "empty"
    # ⚠️ 必须按**列名**取值，不能用位置索引（本轮血案）：
    #   退市股的 stock_zh_a_hist 返回 **12 列**（比在市股多一列「股票代码」）：
    #     ['日期','股票代码','开盘','收盘','最高','最低','成交量',
    #      '成交额','振幅','涨跌幅','涨跌额','换手率']
    #   而位置索引 r[1..7] 会整体错位一列 → open 被写成「股票代码」，
    #   实测 000038 的 open 恒等于 38.000、且 low(12.27) > high(12.11)。
    #   后果：high<low 167,871 条、close 越界 182,469 条。
    rows = []
    for r in df.to_dict("records"):
        d = str(r.get("日期"))[:10]
        try:
            o = float(r["开盘"]); c = float(r["收盘"])
            h = float(r["最高"]); l = float(r["最低"])
            v = float(r["成交量"])
            amt = float(r["成交额"]) if r.get("成交额") is not None else 0.0
            t = float(r["换手率"]) if r.get("换手率") is not None else 0.0
        except (TypeError, ValueError, KeyError):
            continue
        rows.append([d, o, c, h, l, v, amt, t])
    if not rows:
        return "empty"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"code": code, "start": START, "end": END, "rows": rows},
                  f, ensure_ascii=False)
    return "ok"


def fetch_fin(code):
    """拉退市股财务并写成**与现有缓存完全一致**的 {sym, cached_at, records} 结构。

    ⚠️ 格式必须一致（本轮血案）：
       第一版直接把 stock_financial_abstract 的原始宽表转成
       {类别: {报告期: 值}} 落盘 —— 与 data/fin_cache_em 现有的
       {sym, cached_at, records:[{选项,指标,报告期:值}]} **完全不同**。
       而 build_panel_long.load_fin() 只认 records 列表 →
       退市股的 ep / quality / growth 会静默全 None（有值但读不到）。
    """
    fp = os.path.join(FINDIR, f"{code}.json")
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                b = json.load(f)
            if isinstance(b, dict) and "records" in b:
                return "cached"
        except Exception:
            pass                       # 旧格式 → 重写
    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception as e:
        return f"fail:{type(e).__name__}"
    if df is None or len(df) == 0:
        return "empty"
    cols = list(df.columns)
    # 东财宽表：['选项','指标', '20260630', '20260331', ...]
    # 报告期列 = 名字是 8 位数字的列
    pcols = [c for c in cols if str(c).isdigit() and len(str(c)) == 8]
    if not pcols:
        return "empty"
    records = []
    for row in df.to_dict("records"):
        rec = {"选项": row.get("选项"), "指标": row.get("指标")}
        got = False
        for c in pcols:
            v = row.get(c)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv != fv:
                continue
            rec[str(c)] = fv
            got = True
        if got and rec.get("指标"):
            records.append(rec)
    if not records:
        return "empty"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"sym": code,
                   "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "records": records}, f, ensure_ascii=False)
    return "ok"


def main():
    os.makedirs(KLDIR, exist_ok=True)
    os.makedirs(FINDIR, exist_ok=True)
    with open(SRC, encoding="utf-8") as f:
        dl = json.load(f)
    args = sys.argv[1:]
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    skip_k = "--skip-kline" in args
    skip_f = "--skip-fin" in args

    t0 = time.time()
    stat = {}
    n = 0
    for r in dl:
        if limit and n >= limit:
            break
        c = r["code"]
        n += 1
        if not skip_k:
            s = fetch_kline(c)
            stat[f"kline:{s}"] = stat.get(f"kline:{s}", 0) + 1
        if not skip_f:
            s = fetch_fin(c)
            stat[f"fin:{s}"] = stat.get(f"fin:{s}", 0) + 1
        if n % 25 == 0:
            print(f"  {n}/{len(dl)}  {time.time()-t0:.0f}s  {stat}", flush=True)
    print(f"\n完成 {n} 只 / {time.time()-t0:.0f}s")
    for k in sorted(stat):
        print(f"  {k}: {stat[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
