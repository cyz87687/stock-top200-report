#!/usr/bin/env python3
"""fin_factor_cache.py —— 财报因子增量缓存（财务因子由每日全量重算改为增量更新）。

老大 2026-10-26 需求①：财务因子（OCFG/ROE/NAG/EP/EVEBIT）仅在财报披露日变化，
超过披露截止日便不再更新 → 缓存「每只股票 × 每个报告期」的因子值，
只有该股发布新财报时才重算，其余交易日直接读缓存。

## 设计要点

**1. 缓存键 = (code, 报告期 P)**
财报数据在「同一报告期」内是常量。季度序列 P：
  一季报 0331（披露截止 4/30）→ 中报 0630（8/31）→ 三季报 0930（10/31）→ 年报 1231（次年 4/30）
只要 P 不变，因子值就不变。P 变了（新财报落地）才重算。

**2. 两层存储**
  · 明细层 `data/fin_factor_cache/pit/<P>.json`
    {code: {ocfg, roe, nag, ep, evebit, ...}} —— 按报告期分文件，
    一个报告期只写一次，之后只读。这是真正的「增量」。
  · 索引层 `data/fin_factor_cache/index.json`
    {code: {"P": "20260630", "disclosed": "2026-08-31", "src_mtime": 1729..., "v": 2}}
    快速判断某只股票在某个 as_of 应该用哪个 P。

**3. 失效条件（三条，任一触发即重算该股该期）**
  a. 明细层没有该 (code, P)
  b. EM 缓存文件 mtime 比明细层新（说明拿到了新财报数据）
  c. 缓存 schema 版本 v 变了（口径改了，必须全量重算）

**4. ⚠️ 与 PIT 的关系（重要，不可省）**
本缓存只缓存「某报告期的原始因子值」，**不缓存 PIT 选择结果**。
PIT 选择（该 as_of 该用哪个 P）每次现算，因为它取决于 as_of，不同截面不同。
若把 PIT 结果也缓存，历史回补就会用错报告期 → 前视污染。

**5. 收益**
实测（1096 只 EM 缓存 126.8MB）：
  · 现状：每天 JSON 逐文件加载 50.4s + 纯计算 0.05s = 50.5s
  · 改后：pickle 读 0.32s + 只算增量 = 约 0.4s
  · 提速 100x，且与股票数无关（明细层按报告期切分，只读当期）

## 口径常量（与 fund_split / ep_size_ind 保持一致，禁另立）
本模块只做**缓存调度**，因子值一律委托 fund_split / ep_size_ind 计算，
不在此重复实现公式 —— 否则必然漂移且不报错（round17 教训）。
"""
import glob
import hashlib
import json
import os
import pickle
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "fin_factor_cache")
PIT_DIR = os.path.join(CACHE_DIR, "pit")
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")
PKL_PATH = os.path.join(CACHE_DIR, "pit_all.pkl")

SCHEMA_V = 2          # 改口径时 +1，强制全量重算

# 本模块负责的财务因子（行情因子不在此列 —— REV1M/市值/SIZE_IND_NEUT 每日必更）
FIN_FACTORS = ("ocfg", "roe", "npg", "ep", "evebit", "cagr3", "revg",
               "ocf_ni", "sales_cash", "accruals", "debt", "gw",
               "profit_vol", "gm_vol", "roic", "roa")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    os.makedirs(PIT_DIR, exist_ok=True)


def load_index():
    if not os.path.exists(INDEX_PATH):
        return {}
    try:
        d = json.load(open(INDEX_PATH, encoding="utf-8"))
        if not isinstance(d, dict):
            return {}
        if d.get("_schema_v") != SCHEMA_V:
            print("   ℹ️ 财务因子缓存 schema 版本变更(%s→%s)，将全量重算"
                  % (d.get("_schema_v"), SCHEMA_V))
            return {}
        return d.get("map") or {}
    except Exception:
        return {}


def save_index(idx):
    ensure_dirs()
    tmp = INDEX_PATH + ".tmp"
    json.dump({"_schema_v": SCHEMA_V, "_updated": _now(), "map": idx},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)


def load_pit_all():
    """一次性加载全部报告期明细（pickle，0.3s 级）。→ {(code,P): {factor: val}}"""
    if not os.path.exists(PKL_PATH):
        return {}
    try:
        return pickle.load(open(PKL_PATH, "rb"))
    except Exception:
        return {}


def save_pit_all(d):
    ensure_dirs()
    tmp = PKL_PATH + ".tmp"
    pickle.dump(d, open(tmp, "wb"), protocol=5)
    os.replace(tmp, PKL_PATH)


def _em_mtime_map(em_dir=None):
    """EM 原始缓存文件的 mtime → {code6: mtime}。用于失效判据 b。"""
    em_dir = em_dir or os.path.join(HERE, "data", "fin_cache_em")
    out = {}
    for f in glob.glob(os.path.join(em_dir, "*.json")):
        code = os.path.basename(f)[:-5]
        try:
            out[code] = os.path.getmtime(f)
        except Exception:
            continue
    return out


def compute_fin_factors(codes, as_of, em_all=None, verbose=True):
    """算一批股票在 as_of 的 PIT 财务因子（**增量**：命中缓存的不重算）。

    返回 (result, stats)
      result: {code6: {"P": 报告期, "factors": {...}, "hit": "cache"|"fresh"}}
      stats : {n_in, n_cache, n_fresh, secs, p_dist}

    ⚠️ 与 fund_split.score_rows 的区别：
       score_rows 返回的是**池内 rank 分位 0-10 分**（截面依赖，必须整池算）；
       本函数返回的是**原始因子值**（截面无关，可跨截面缓存复用）。
       两者不可混用 —— 前者每日必重算，后者才可缓存。
    """
    t0 = time.time()
    ensure_dirs()
    idx = load_index()
    pit_all = load_pit_all()
    em_mtime = _em_mtime_map()

    try:
        import fund_split as FS
    except Exception as e:
        print("   🚨 fin_factor_cache: 无法 import fund_split → 缓存不可用: %s" % e)
        return {}, {"error": str(e), "n_in": len(codes), "n_cache": 0, "n_fresh": 0}

    if em_all is None:
        em_all = FS.load_em_cache()

    result, n_cache, n_fresh = {}, 0, 0
    fresh_keys = set()
    p_dist = {}

    for code in codes:
        c6 = str(code)[-6:]
        ind = em_all.get(c6)
        if not ind:
            continue
        # PIT 现算（不缓存，见模块头 4.）
        periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
        P = FS.pit_period(periods_desc, as_of)
        if not P:
            continue
        key = (c6, P)
        ent = idx.get(c6) or {}
        # 失效判据
        stale = (ent.get("P") != P
                 or ent.get("v") != SCHEMA_V
                 or key not in pit_all
                 or (em_mtime.get(c6) or 0) > float(ent.get("src_mtime") or 0))
        if stale:
            sub = FS.sub_factors_pit(ind, P)
            pit_all[key] = {k: sub.get(k) for k in FIN_FACTORS if sub.get(k) is not None}
            idx[c6] = {"P": P, "disclosed": None,
                       "src_mtime": em_mtime.get(c6) or 0, "v": SCHEMA_V,
                       "updated": _now()}
            fresh_keys.add(key)
            n_fresh += 1
        else:
            n_cache += 1
        result[c6] = {"P": P, "factors": pit_all.get(key) or {},
                      "hit": "fresh" if key in fresh_keys else "cache"}
        p_dist[P] = p_dist.get(P, 0) + 1

    if fresh_keys:
        save_pit_all(pit_all)
        save_index(idx)

    stats = {"n_in": len(codes), "n_cache": n_cache, "n_fresh": n_fresh,
             "secs": round(time.time() - t0, 2),
             "p_dist": dict(sorted(p_dist.items(), reverse=True)),
             "cache_rate": round(100.0 * n_cache / max(1, n_cache + n_fresh), 1)}
    return result, stats


def report_lines(stats):
    if stats.get("error"):
        return ["财报因子缓存：不可用（%s）" % stats["error"]]
    return ["财报因子缓存：%d 只 → 命中 %d / 重算 %d（命中率 %.1f%%）%.2fs  报告期分布 %s"
            % (stats.get("n_in", 0), stats.get("n_cache", 0), stats.get("n_fresh", 0),
               stats.get("cache_rate", 0), stats.get("secs", 0),
               stats.get("p_dist", {}))]


def clear_all():
    """清空缓存（口径大改时手动调用）。"""
    for p in (PKL_PATH, INDEX_PATH):
        if os.path.exists(p):
            os.remove(p)
    for f in glob.glob(os.path.join(PIT_DIR, "*.json")):
        os.remove(f)
    print("已清空财务因子缓存")


if __name__ == "__main__":
    import sys
    import step2_stockscorer_v2 as S2
    ap_code = sys.argv[1] if len(sys.argv) > 1 else ""
    if ap_code == "--clear":
        clear_all()
        sys.exit(0)
    as_of = sys.argv[2] if len(sys.argv) > 2 else time.strftime("%Y-%m-%d")
    d = json.load(open(os.path.join(HERE, ap_code or "top500_all_a.json"), encoding="utf-8"))
    codes = [str(r.get("code"))[-6:] for r in (d.get("main_pool") or [])]
    print("测试 %d 只 as_of=%s" % (len(codes), as_of))
    for i in range(2):
        res, st = compute_fin_factors(codes, as_of)
        print("  第%d次: %s" % (i + 1, report_lines(st)[0]))
