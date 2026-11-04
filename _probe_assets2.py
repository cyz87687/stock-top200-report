# -*- coding: utf-8 -*-
"""只读数据资产核对脚本（_probe_assets2.py）。不写任何业务文件。"""
import os, sys, json, glob, re, traceback, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def safe(fn, name):
    try:
        fn()
    except Exception:
        print("[ERROR] %s" % name)
        traceback.print_exc()


# ---------------------------------------------------------------- 【1】
def p1():
    hdr("【1】fwd_hist 日期覆盖")
    files = sorted(glob.glob("data/fwd_hist/*.json"))
    dates = []
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        if m:
            dates.append(m.group(1))
    dates.sort()
    print("全部日期（%d 个）:" % len(dates))
    print(", ".join(dates))
    print("最小日期: %s" % dates[0])
    print("最大日期: %s" % dates[-1])
    print("文件数: %d" % len(files))
    # earliest file
    earliest = None
    for f in files:
        if dates[0] in os.path.basename(f):
            earliest = f
            break
    print("最早文件: %s" % earliest)
    d = json.load(open(earliest, encoding="utf-8"))
    print("顶层键: %s" % list(d.keys()))
    rows = d.get("rows") or []
    print("rows 条数: %d" % len(rows))
    if rows:
        print("rows[0] 全部字段名（%d 个）:" % len(rows[0]))
        print(list(rows[0].keys()))


# ---------------------------------------------------------------- 【2】
def p2():
    hdr("【2】申万 sw_map 规模")
    d = json.load(open("sector/sw_map.json", encoding="utf-8"))
    print("顶层键: %s" % list(d.keys()))
    l1 = d.get("l1") or {}
    l2 = d.get("l2") or {}
    l1m = d.get("l1_members") or {}
    l2m = d.get("l2_members") or {}
    l2p = d.get("l2_parent") or {}
    print("l1 条目数: %d" % len(l1))
    print("l2 条目数: %d" % len(l2))
    print("l2_parent 条目数: %d" % len(l2p))
    print("l1_members 条目数: %d" % len(l1m))
    print("l2_members 条目数: %d" % len(l2m))
    print("fetched_at: %s" % d.get("fetched_at"))

    # l1 取值 + 每行业股票数
    print("\n-- l1 去重行业名及覆盖股票数 --")
    from collections import Counter
    c = Counter(l1.values())
    print("l1 去重行业数（直接由 l1 映射统计）: %d" % len(c))
    for name, n in sorted(c.items(), key=lambda x: -x[1]):
        nm = len(l1m.get(name) or [])
        print("  %-10s  l1映射=%4d   l1_members=%4d  %s" %
              (name, n, nm, "" if n == nm else "  <差异>"))
    print("l1_members 覆盖股票总数: %d" % sum(len(v) for v in l1m.values()))
    print("l1 映射覆盖股票总数: %d" % len(l1))
    print("l1 与 l1_members 股票并集: %d" %
          len(set(l1.keys()) | set(x for v in l1m.values() for x in v)))

    # l1 全部取值（去重列表）
    print("\nl1 全部取值（去重，字母序）:")
    print(sorted(c.keys()))

    # l2 键中 6 位纯数字占比
    ks = list(l2.keys())
    num6 = sum(1 for k in ks if re.fullmatch(r"\d{6}", str(k)))
    print("\nl2 条目数: %d，键为 6 位纯数字的条数: %d，比例: %.4f (%.2f%%)" %
          (len(ks), num6, num6 / len(ks) if ks else 0, 100.0 * num6 / len(ks) if ks else 0))
    print("l2 非 6 位数字键示例（前 10）: %s" %
          [k for k in ks if not re.fullmatch(r"\d{6}", str(k))][:10])

    # l2_members 覆盖
    print("\nl2_members 覆盖股票总数: %d" % sum(len(v) for v in l2m.values()))
    print("l2_parent 去重父行业数: %d" % len(set(l2p.values())))


# ---------------------------------------------------------------- 【3】
def p3():
    hdr("【3】EM 财务缓存规模（ep_size_ind.load_em_all）")
    import random
    import ep_size_ind
    em = ep_size_ind.load_em_all()
    print("返回 dict 条目数: %d" % len(em))
    codes = list(em.keys())
    print("code 示例（前 5）: %s" % codes[:5])

    random.seed(42)
    sample = random.sample(codes, 3)
    print("\n-- 随机 3 个 code 明细 --")
    for cd in sample:
        rec = em[cd]
        n_ind = len(rec)
        # 每指标一个 period dict
        periods = set()
        for ind, pv in rec.items():
            if isinstance(pv, dict):
                periods |= set(pv.keys())
        print("  code=%s  指标数=%d  报告期数=%d  示例指标=%s  示例期间=%s" %
              (cd, n_ind, len(periods), list(rec.keys())[:3], sorted(periods)[:3]))

    cnts = []
    all_periods = set()
    for cd, rec in em.items():
        ps = set()
        for ind, pv in rec.items():
            if isinstance(pv, dict):
                ps |= set(pv.keys())
        cnts.append(len(ps))
        all_periods |= ps
    cnts_sorted = sorted(cnts)

    def pct(p):
        if not cnts_sorted:
            return 0
        i = int(round((len(cnts_sorted) - 1) * p))
        return cnts_sorted[i]

    print("\n-- 全部 code 的报告期数量分布 --")
    print("总数 code: %d" % len(cnts))
    print("min    = %d" % min(cnts))
    print("p25    = %d" % pct(0.25))
    print("median = %d" % statistics.median(cnts))
    print("p75    = %d" % pct(0.75))
    print("max    = %d" % max(cnts))
    print("最新报告期（全部 period 并集最大值）: %s" % max(all_periods))
    print("全部 period 并集数量: %d" % len(all_periods))
    print("period 并集样例（排序尾部 8 个）: %s" % sorted(all_periods)[-8:])


# ---------------------------------------------------------------- 【4】
def p4():
    hdr("【4】现有 factor_history.prices 的日期序列")
    d = json.load(open("factor_history.json", encoding="utf-8"))
    p = d.get("prices") or {}
    dates = p.get("dates") or []
    px = p.get("px") or {}
    print("prices.dates 长度: %d" % len(dates))
    print("首日期: %s" % (dates[0] if dates else "-"))
    print("尾日期: %s" % (dates[-1] if dates else "-"))
    print("fetched_at: %s" % p.get("fetched_at"))
    print("px 股票数: %d" % len(px))
    print("n_codes=%s n_open_codes=%s" % (p.get("n_codes"), p.get("n_open_codes")))

    def nonnull_at(series, i):
        return len(series) > i and series[i] is not None

    n_first1 = sum(1 for cd, s in px.items() if nonnull_at(s, 0))
    n_first10 = sum(1 for cd, s in px.items()
                    if any(nonnull_at(s, i) for i in range(min(10, len(dates)))))
    print("\n有非 null 收盘价的股票数：")
    print("  最早 1 个交易日 (dates[0]=%s) : %d 只" % (dates[0], n_first1))
    print("  最早 10 个交易日 (dates[0]~dates[%d]) : %d 只" %
          (min(9, len(dates) - 1), n_first10))

    # 辅助：每只股票的覆盖天数
    cover = [sum(1 for v in s if v is not None) for s in px.values()]
    print("\n（辅助）每只股票非 null 天数: min=%d median=%s max=%d" %
          (min(cover), statistics.median(cover), max(cover)))
    print("（辅助）序列长度分布: %s" %
          {L: sum(1 for s in px.values() if len(s) == L) for L in sorted(set(len(s) for s in px.values()))})

    # 辅助：首个非 null 索引分布（解释为何 dates[0] 只有极少数）
    from collections import Counter
    firsts = []
    for cd, s in px.items():
        idx = [i for i, v in enumerate(s) if v is not None]
        if idx:
            firsts.append(idx[0])
    c = Counter(firsts)
    print("\n（关键）每只股票首个非 null 收盘价所在索引分布（前 12）:")
    print(sorted(c.items())[:12])
    print("（关键）首个非 null 索引: min=%d median=%d max=%d" %
          (min(firsts), sorted(firsts)[len(firsts) // 2], max(firsts)))
    print("（关键）dates[13] = %s ，该索引上首个非 null 的股票数 = %d / %d" %
          (dates[13] if len(dates) > 13 else "-", c.get(13, 0), len(px)))
    print("（关键）dates[0] 即非 null 的股票: %s" %
          sorted([cd for cd, s in px.items() if s and s[0] is not None]))
    print("snapshots 条数: %d" % len(d.get("snapshots") or []))


# ---------------------------------------------------------------- 【5】
def p5():
    hdr("【5】data/universe_klines.json 的日期序列")
    d = json.load(open("data/universe_klines.json", encoding="utf-8"))
    print("原始股票键数: %d" % len(d))

    def norm(c):
        s = str(c).lower().replace("sh", "").replace("sz", "").replace("bj", "")
        s = re.sub(r"\D", "", s)
        return s.zfill(6)[-6:] if s else s

    norm_map = {}
    for k, v in d.items():
        norm_map.setdefault(norm(k), {}).update(v)

    all_dates = set()
    for v in norm_map.values():
        all_dates |= set(v.keys())
    ds = sorted(all_dates)
    print("日期总数: %d" % len(ds))
    print("首日期: %s" % (ds[0] if ds else "-"))
    print("尾日期: %s" % (ds[-1] if ds else "-"))
    print("归一为 6 位后去重股票数: %d" % len(norm_map))

    ge120 = sum(1 for v in norm_map.values() if len(v) >= 120)
    print("日期覆盖数 >= 120 的股票数: %d" % ge120)

    lens = [len(v) for v in norm_map.values()]
    print("（辅助）日期覆盖数: min=%d median=%s max=%d" % (min(lens), statistics.median(lens), max(lens)))
    # 原始键是否已全为 6 位
    raw6 = sum(1 for k in d if re.fullmatch(r"\d{6}", str(k)))
    print("（辅助）原始键已是 6 位纯数字的条数: %d / %d" % (raw6, len(d)))

    # 辅助：原始键存在 sh/sz 前缀重复，说明归一化会合并重复键
    print("（关键）原始键前缀形态分布: %s" %
          {("(无前缀)" if p == "" else p): n
           for p, n in __import__("collections").Counter(
               re.sub(r"\d", "", str(k)) for k in d).most_common()})
    grouped = {}
    for k in d:
        grouped.setdefault(norm(k), []).append(k)
    dups = {k: v for k, v in grouped.items() if len(v) > 1}
    print("（关键）归一后重复组数: %d，涉及原始键数: %d，重复组示例: %s" %
          (len(dups), sum(len(v) for v in dups.values()),
           list(dups.items())[:3]))
    # 组内日期集合是否一致
    incons = 0
    for cd, lst in grouped.items():
        if len(lst) > 1:
            base = set(d[lst[0]].keys())
            if any(set(d[x].keys()) != base for x in lst[1:]):
                incons += 1
    print("（关键）重复组内日期集合不一致的组数: %d（0 表示合并安全）" % incons)
    # 不归一对照
    print("（对照）按原始键（不归一）覆盖数>=120 的条数: %d" %
          sum(1 for v in d.values() if len(v) >= 120))


# ---------------------------------------------------------------- 【6】
def p6():
    hdr("【6】现有 top200_scored_*.json 清单")
    files = sorted(glob.glob("top200_scored_*.json"))
    print("文件名清单（%d 个）:" % len(files))
    for f in files:
        print("  %s  (%d bytes)" % (f, os.path.getsize(f)))
    latest = files[-1]
    print("\n最新文件: %s" % latest)
    d = json.load(open(latest, encoding="utf-8"))
    print("顶层键: %s" % list(d.keys()))
    print("date 字段: %s" % d.get("date"))
    r = d.get("results") or []
    print("results 长度: %d" % len(r))
    if r:
        print("results[0] 字段名（%d 个）:" % len(r[0]))
        print(list(r[0].keys()))

    def pos(x):
        try:
            return x is not None and float(x) > 0
        except (TypeError, ValueError):
            return False

    n_both = sum(1 for x in r if pos(x.get("circ_mv")) and pos(x.get("price")))
    n_circ = sum(1 for x in r if pos(x.get("circ_mv")))
    n_price = sum(1 for x in r if pos(x.get("price")))
    print("\ncirc_mv 与 price 均为正数的条数: %d / %d" % (n_both, len(r)))
    print("（辅助）circ_mv>0: %d，price>0: %d，circ_mv 为 null: %d，price 为 null: %d" %
          (n_circ, n_price,
           sum(1 for x in r if x.get("circ_mv") is None),
           sum(1 for x in r if x.get("price") is None)))

    # 各历史文件的 results 长度对照（看池子扩张轨迹）
    print("\n（辅助）各文件 results 长度 / 文件内 date 字段:")
    for f in files:
        try:
            dd = json.load(open(f, encoding="utf-8"))
            print("  %-34s results=%-6d date=%s pool_size=%s" %
                  (f, len(dd.get("results") or []), dd.get("date"), dd.get("pool_size")))
        except Exception as e:
            print("  %-34s [读取失败] %r" % (f, e))


if __name__ == "__main__":
    for fn, nm in [(p1, "p1"), (p2, "p2"), (p3, "p3"), (p4, "p4"), (p5, "p5"), (p6, "p6")]:
        safe(fn, nm)
    print("\n\n[DONE]")
