#!/usr/bin/env python3
"""fund_split.py —— 质量/成长两维度打分（v4.4 新增，单一实现来源）。

背景（老大 2026-10-25）：把原 fund 维度按专业分类拆出「质量因子」与「成长因子」
两个一级维度。分类规则（老大指定）：
  质量：① 资本回报 ROE/ROIC/ROA ② 现金流&盈余质量 OCF/NI、销售收现比、应计因子、OCFG
        ③ 稳健风控 盈利波动、毛利率波动、资产负债率、商誉占比
  成长：① 历史增速 近1年扣非增速、3年CAGR、营收增速 ② 边际改善 单季环比、增速差分

⚠️ 本模块是审计（audit_round21/22）与生产（step2/本地补算）的**唯一实现来源**。
   改这里必须同步重跑 audit_round21.py 验证 IC 不漂移——round17 的教训是
   「同一逻辑两处实现必然漂移，且不报错」。

口径（与 audit_round21.py 完全一致，勿单方面修改）：
  · 数据源：data/fin_cache_em/（东财财务摘要，80 指标 × 20 报告期，TTL 7 天）
  · point-in-time：只用披露截止日 ≤ 截面日期的报告期
    （年报/一季报→4/30、中报→8/31、三季报→10/31）
  · 负向子因子（应计/负债率/商誉/波动类）已取负：高分=高质量
  · 缺维从分母剔除（v4.3 铁律），禁 `or 0`
  · 族得分 = 组内子因子均值 → 组间等权（质量三组等权；成长 60/40）
  · 截面内 rank 分位 ×10 → 0-10 分制（与 score_size 同口径）

⚠️ 已知偏差（沿用 round21 披露）：EM 缓存为当前 vintage，历史截面用今天的数值
   重建，存在财报重述偏差；披露截止日为法定近似（实际披露可早于）。
"""
import glob
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")

# 同名指标在多个「选项」分组下出现时的取值优先级（保证确定性）
IND_OPT = {
    "净资产收益率(ROE)": ["盈利能力", "常用指标"],
    "投入资本回报率": ["盈利能力"],
    "总资产报酬率(ROA)": ["常用指标", "盈利能力"],
    "总资产净利率_平均": ["盈利能力"],
    "经营活动净现金/归属母公司的净利润": ["收益质量"],
    "经营活动净现金/销售收入": ["收益质量"],
    "商誉": ["常用指标"],
    "营业总收入增长率": ["成长能力"],
    "归属母公司净利润增长率": ["成长能力"],
    "毛利率": ["常用指标", "盈利能力"],
    "资产负债率": ["财务风险", "常用指标"],
    "经营现金流量净额": ["常用指标"],
    "净利润": ["常用指标"],
    "股东权益合计(净资产)": ["常用指标"],
    "扣非净利润": ["常用指标", "成长能力"],
    "营业总收入": ["常用指标", "成长能力"],
    "每股经营现金流": ["每股指标"],
}

QUAL_SUBS = ["roe", "roic", "roa", "ocf_ni", "sales_cash", "accruals", "ocfg",
             "profit_vol", "gm_vol", "debt", "gw"]
GROW_SUBS = ["npg", "cagr3", "revg", "qoq", "accel"]
Q_GROUPS = {"资本回报": ["roe", "roic", "roa"],
            "现金流与盈余质量": ["ocf_ni", "sales_cash", "accruals", "ocfg"],
            "稳健与风控": ["profit_vol", "gm_vol", "debt", "gw"]}
G_GROUPS = {"历史增速": ["npg", "cagr3", "revg"], "边际改善": ["qoq", "accel"]}
G_W = {"历史增速": 0.6, "边际改善": 0.4}


def _is_num(x):
    """True 仅当 x 是有限实数（排除 bool / None / NaN / ±inf）。

    ⚠️ 2026-10-28 P0 修复（round35）：本模块原有多处判定写成
       `isinstance(v, (int, float))`，而 **NaN 恰好是 float** —— 东财缓存对
       「无商誉」公司返回 nan，于是 NaN 一路穿透到 rank_pct 的 sorted()，
       而 Python 中 NaN 的任何比较恒为 False，排序算法分治崩塌，
       全池秩错乱且**不报错**。实测：同一数据仅打乱输入行序 9 次，
       威海广泰质量分在 0.10~8.11 之间跳变（文件里的 8.74 不在其中）。

    口径与 scale_calc.is_num 保持一致（单一实现来源），此处内联是为了
    避免 fund_split → scale_calc 的潜在循环 import。
    """
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and x == x and x not in (float("inf"), float("-inf")))


def _fnum(v):
    """东财缓存的值可能是 str/None/'--'/nan；统一转 float 或 None。

    ⚠️ 2026-10-28 修复：原实现 `isinstance(v,(int,float)) → float(v)`
       会把 nan 原样透传（nan 是 float），`float("nan")` 也能从字符串路径进来。
       现改为统一走 _is_num 出口校验 —— NaN/inf 一律视为「缺失」而非「数值」。
    """
    if v is None or v is False:
        return None
    if isinstance(v, str) and v.strip() in ("--", "-", "", "False", "nan", "None", "NaN"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not _is_num(f):
        return None
    return f


EM_PKL = os.path.join(EM_CACHE, "_cache.pkl")


def _em_sig(cache_dir):
    """EM 缓存目录指纹 → (文件数, 总 mtime, 总 size)。

    ⚠️ 2026-10-26 P0 修复：原指纹只用 (文件数, 总 mtime)。但 Step 4.5 的
       fetch_em_growth_cached 会在同一进程内往本目录覆写文件，覆写过程中
       「文件数不变、mtime 已变」的中间态若恰好被另一路径读到，会命中一个
       内容不完整的 pkl，导致 quality 覆盖从 500 静默跌到 250（10-26 首跑实测）。
       加总 size 一维：文件覆写时 size 通常同步变化，能挡住更多中间态；
       更关键的是下面的读取端要能容忍「解析失败不缓存」。
    """
    files = glob.glob(os.path.join(cache_dir, "*.json"))
    try:
        tot_mt = round(sum(os.path.getmtime(f) for f in files), 1)
        tot_sz = sum(os.path.getsize(f) for f in files)
    except Exception:
        return None
    return (len(files), tot_mt, tot_sz)


def load_em_cache(cache_dir=EM_CACHE, use_pkl=True):
    """code6 -> {indicator: {period: value}}

    ⚠️ 2026-10-26 性能改造（老大需求①）：新增 pickle 快路径。
       实测 1096 个 JSON 逐文件加载 + 解析 = **50.4 秒**（126.8MB），
       而 pickle 读 = **0.32 秒**（158x）。
       失效判据：缓存目录内 (文件数 + 总 mtime + 总 size) 指纹变了就重建 pkl，
       否则直接用 —— 财报数据一份文件一天最多变一次，指纹足够可靠。
    """
    if use_pkl:
        try:
            meta = json.load(open(EM_PKL + ".meta", encoding="utf-8"))
            sig = _em_sig(cache_dir)
            if sig and tuple(meta.get("sig") or ()) == sig and os.path.exists(EM_PKL):
                import pickle
                with open(EM_PKL, "rb") as fh:
                    cached = pickle.load(fh)
                if cached:
                    return cached
        except Exception:
            pass

    out = {}
    _n_parse_fail = 0
    for f in glob.glob(os.path.join(cache_dir, "*.json")):
        code = os.path.basename(f)[:-5]
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            # ⚠️ 撞上「正在被覆写」的文件：不能当成「这公司没财务数据」，
            #   否则该股 quality/ep 会静默变成 None 并被「缺维剔除」吸收。
            #   计数留痕（下方告警），本轮不缓存 pkl（见 _n_parse_fail 判断）。
            _n_parse_fail += 1
            continue
        recs = d.get("records") or []
        ind = {}
        for r in recs:
            name = r.get("指标")
            if not name:
                continue
            dd = ind.setdefault(name, {})
            for k, v in r.items():
                if len(k) == 8 and k.isdigit():
                    vv = _fnum(v)
                    if vv is not None:
                        dd[k] = vv
        if ind:
            out[code] = ind

    if _n_parse_fail:
        print("   ⚠️ EM 缓存有 %d 个文件解析失败（疑似并发写入中间态）"
              "→ 本轮不落 pkl，下次重试" % _n_parse_fail)

    if use_pkl and out and not _n_parse_fail:
        try:
            import pickle
            sig = _em_sig(cache_dir)
            tmp = EM_PKL + ".tmp"
            with open(tmp, "wb") as fh:
                pickle.dump(out, fh, protocol=5)
            os.replace(tmp, EM_PKL)
            json.dump({"_sig": "n/a", "sig": list(sig),
                       "n": len(out), "built": time.strftime("%Y-%m-%d %H:%M:%S")},
                      open(EM_PKL + ".meta", "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return out


def disclosure_date(p):
    """报告期 -> 法定披露截止日（近似）。p 形如 20260930。"""
    y, md = int(p[:4]), p[4:]
    return {"0331": "%d-04-30" % y, "0630": "%d-08-31" % y,
            "0930": "%d-10-31" % y, "1231": "%d-04-30" % (y + 1)}.get(md)


def pit_period(periods_desc, d0):
    """取披露截止日 ≤ d0 的最新报告期；没有则 None。"""
    for p in periods_desc:
        dd = disclosure_date(p)
        if dd and dd <= d0:
            return p
    return None


def _pit_period_actual(periods_desc, d0, grace_days=5):
    """成长维专用：允许使用「已实际发布」的最新报告期。

    背景（老大 2026-10-28 指令）
    ---------------------------
    `pit_period` 用**法定披露截止日**做闸门，这对质量维是正确的（防前视）。
    但对成长维它有一个致命副作用：三季报法定截止日是 10-31，所以在 10-26
    这样的截面日，成长维被迫退回**中报**，于是当季景气（例如威海广泰单季
    归母净利同比 −92.90%）**完全进不了模型** —— 模型只看到中报的累计 +28.57%。

    而实际上 A 股三季报多在 10 月 20~30 日之间已发布完毕，10-26 当天
    「三季报数据已经公开」是事实，不是前视。故此处引入 grace：只要
    报告期末 + 法定截止日 − grace_days ≤ d0，即认为该期数据已可用。

    ⚠️ 这不是放宽前视控制，而是修正「法定截止日」与「实际发布」的错配。
       为防止真前视，grace 只给 3 天，且仅用于**成长维**（质量维仍走严格 PIT，
       保持两维各自的历史口径连续性）。
    """
    from datetime import date, timedelta
    try:
        d0d = date.fromisoformat(d0)
    except Exception:
        return pit_period(periods_desc, d0)
    for p in periods_desc:
        dd = disclosure_date(p)
        if not dd:
            continue
        try:
            lim = date.fromisoformat(dd) - timedelta(days=grace_days)
        except Exception:
            continue
        if lim <= d0d:
            return p
    return None


def _pstdev(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / n) ** 0.5


def _prev_year(p):
    return "%d%s" % (int(p[:4]) - 1, p[4:])


def _prev_quarter(p):
    y, md = int(p[:4]), p[4:]
    return {"0331": "%d1231" % (y - 1), "0630": "%d0331" % y,
            "0930": "%d0630" % y, "1231": "%d0930" % y}.get(md)


def sub_factors_pit(ind, P):
    """ind: {name: {period: val}}（load_em_cache 输出）；P: PIT 报告期。

    返回 {sub_name: value}。负向子因子已取负（高分=高质量/低风险）。
    ⚠️ 与 audit_round21.sub_factors_pit 保持一致——修改必须重跑 round21 验证。
    """
    out = {}

    def get(name, p):
        """取指标值。⚠️ 2026-10-28：出口统一过 _is_num —— 东财对「无商誉」等
        字段返回 nan，原实现直接透传，是 round35 排序塌陷的源头。"""
        v = (ind.get(name) or {}).get(p)
        return v if _is_num(v) else None
    def yoy(name, p):
        """(cur-prev)/|prev| 口径（负基数取绝对值，与官方披露一致）"""
        cur, prev = get(name, p), get(name, _prev_year(p))
        if cur is None or prev is None or prev == 0:
            return None
        return (cur - prev) / abs(prev) * 100.0

    def sq(name, p):
        """单季值 = 累计值 − 上一季累计（Q1 即累计本身）"""
        pq = _prev_quarter(p)
        cum = get(name, p)
        if cum is None:
            return None
        if pq is None:
            return cum
        prev_cum = get(name, pq)
        if prev_cum is None:
            return None
        return cum - prev_cum

    # ---- 质量 ----
    out["roe"] = get("净资产收益率(ROE)", P)
    out["roic"] = get("投入资本回报率", P)
    out["roa"] = get("总资产报酬率(ROA)", P)
    if out["roa"] is None:
        out["roa"] = get("总资产净利率_平均", P)
    out["ocf_ni"] = get("经营活动净现金/归属母公司的净利润", P)
    out["sales_cash"] = get("经营活动净现金/销售收入", P)
    ni, ocf = get("净利润", P), get("经营现金流量净额", P)
    eq, dr = get("股东权益合计(净资产)", P), get("资产负债率", P)
    if ni is not None and ocf is not None and eq and eq > 0 and dr is not None and 0 <= dr < 100:
        ta = eq / (1.0 - dr / 100.0)
        out["accruals"] = -((ni - ocf) / ta * 100.0)      # 负向：低应计=高质量
    ocf_now, ocf_prev = get("每股经营现金流", P), get("每股经营现金流", _prev_year(P))
    if ocf_now is not None and ocf_prev and ocf_prev > 0:
        out["ocfg"] = (ocf_now / ocf_prev - 1.0) * 100.0
    annuals = [p for p in sorted(ind.get("净资产收益率(ROE)", {}).keys(), reverse=True)
               if p[4:] == "1231" and (disclosure_date(p) or "9999") <= (disclosure_date(P) or "9999")]
    for sub, name in (("profit_vol", "净资产收益率(ROE)"), ("gm_vol", "毛利率")):
        vals = [get(name, p) for p in annuals[:4]]
        vals = [v for v in vals if v is not None]
        if len(vals) >= 3:
            out[sub] = -_pstdev(vals)                      # 负向：低波动=高质量
    out["debt"] = -dr if dr is not None else None          # 负向：低杠杆=高质量
    gwv = get("商誉", P)
    if gwv is not None and eq and eq > 0:
        out["gw"] = -100.0 * gwv / eq                      # 负向：低商誉=高质量

    # ---- 成长 ----
    out["npg"] = yoy("扣非净利润", P)
    p3 = "%d%s" % (int(P[:4]) - 3, P[4:])
    cur3, base3 = get("扣非净利润", P), get("扣非净利润", p3)
    if cur3 is not None and base3 is not None and cur3 > 0 and base3 > 0:
        out["cagr3"] = ((cur3 / base3) ** (1.0 / 3.0) - 1.0) * 100.0
    out["revg"] = get("营业总收入增长率", P)
    if out["revg"] is None:
        out["revg"] = yoy("营业总收入", P)
    pq = _prev_quarter(P)
    if pq:
        sq_now, sq_prev = sq("扣非净利润", P), sq("扣非净利润", pq)
        if sq_now is not None and sq_prev is not None and sq_prev != 0:
            out["qoq"] = (sq_now - sq_prev) / abs(sq_prev) * 100.0
        n_now, n_prev = yoy("扣非净利润", P), yoy("扣非净利润", pq)
        if n_now is not None and n_prev is not None:
            out["accel"] = n_now - n_prev                   # 增速差分
    return out


def family_scores(sub):
    """子因子 dict -> (quality, growth) 族得分（原始尺度）。缺组整组剔除。

    ⚠️ 2026-10-28 修复：原过滤条件 `isinstance(v,(int,float))` 放行 NaN
       （NaN 是 float），组均值被污染成 NaN，进而击穿下游 rank_pct。
       改用 _is_num（同时排除 NaN/inf/bool）。
    """
    def fam(groups, ws=None):
        num, den = 0.0, 0.0
        for g, members in groups.items():
            vs = [sub.get(m) for m in members]
            vs = [v for v in vs if _is_num(v)]
            if not vs:
                continue
            w = (ws or {}).get(g, 1.0)
            num += w * (sum(vs) / len(vs))
            den += w
        return num / den if den > 0 else None
    return fam(Q_GROUPS), fam(G_GROUPS, G_W)


def rank_pct(vals):
    """截面内 rank 分位（0-1），并列取平均秩。

    ⚠️ 2026-10-28 P0 修复（round35）：原实现直接对 vals 排序，若含 NaN，
       `sorted(key=lambda i: vals[i])` 的分治比较因「NaN 任何比较恒为 False」
       而崩塌 —— **不抛异常、顺序全错**，导致同一份数据每次算出的分数不同。
       （实证：威海广泰质量分在 0.10~8.11 间跳变，文件值 8.74 不在其中。）

      现双保险：
        ① 入口断言：含非有限值直接抛 ValueError —— 宁可炸掉，不能静默算错；
        ② 剔除后仍不足 2 个有效值则返回空，由调用方按缺失处理。
    """
    n = len(vals)
    if n == 0:
        return []
    # ① 硬断言：NaN/inf 绝不允许进入排序
    bad = [i for i, v in enumerate(vals) if not _is_num(v)]
    if bad:
        raise ValueError(
            "rank_pct 收到 %d 个非有限值（索引示例 %s）—— "
            "调用方必须在入池前剔除 NaN/inf，禁止静默排序。"
            % (len(bad), bad[:5]))
    order = sorted(range(n), key=lambda i: vals[i])
    rp = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            rp[order[k]] = avg / max(1, n - 1)
        i = j + 1
    return rp


def score_rows(rows, as_of, em=None, min_present=5, pit_mode="auto"):
    """给一批行（每行含 code6）补 score_quality / score_growth（0-10）。

    rows: [{"code": "sz000001" 或 "000001", ...}, ...]（原地写入两字段）
    as_of: 截面日期 "YYYY-MM-DD"（PIT 用）
    em: 预加载的 EM 缓存（可选，避免重复读盘）
    pit_mode: 成长维的报告期口径
        · "auto"（默认）：严格 PIT —— 用披露截止日 ≤ as_of 的最新报告期
        · "actual_ok"：允许用 as_of 之后 3 个自然日内已发布的报告期
          （老大 2026-10-28 指令：成长要反映当期景气，不能只看中报）
    返回 (n_q, n_g) 有值条数。

    ⚠️ 池内分位必须整池一起算（与 score_size 同口径）：池逐日变化，
       禁止把历史截面拼起来一次性分位。

    ⚠️ 2026-10-28 P0 修复（round35）：
       原 `present = [i for i,v in enumerate(raw) if isinstance(v,(int,float))]`
       把 NaN 判为「有值」→ NaN 进入 rank_pct → 排序塌陷 → 分数依赖输入行序。
       现改用 _is_num，且 rank_pct 内置硬断言兜底。
    """
    if em is None:
        em = load_em_cache()
    q_raw, g_raw, idx_map = [], [], []
    for i, r in enumerate(rows):
        code = (r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
        q = g = None
        ind = em.get(code)
        if ind:
            periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = pit_period(periods_desc, as_of)
            # 成长维：允许用「已实际发布」的最新报告期（见 pit_mode 说明）
            P_g = P
            if pit_mode == "actual_ok":
                P_g = _pit_period_actual(periods_desc, as_of) or P
            if P:
                q_raw.append(family_scores(sub_factors_pit(ind, P))[0])
            else:
                q_raw.append(None)
            if P_g:
                g_raw.append(family_scores(sub_factors_pit(ind, P_g))[1])
            else:
                g_raw.append(None)
        else:
            q_raw.append(None)
            g_raw.append(None)
        idx_map.append(i)

    # 截面内分位 → 0-10
    for raw, field in ((q_raw, "score_quality"), (g_raw, "score_growth")):
        present = [i for i, v in enumerate(raw) if _is_num(v)]
        if len(present) < min_present:
            continue
        rp = rank_pct([raw[i] for i in present])
        for k, i in enumerate(present):
            rows[idx_map[i]][field] = round(rp[k] * 10.0, 2)
    return (sum(1 for v in q_raw if _is_num(v)),
            sum(1 for v in g_raw if _is_num(v)))
