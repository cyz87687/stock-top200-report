#!/usr/bin/env python3
"""新浪接口获取全A成交额 TOP500 主力池 (v3.0 扩池, 观察池已删除)
v2.13 增强:
  1. 数据质量校验: 成交额全0/920段北交所异常占位数据 → 判定失败, 不覆盖已有数据
  2. 备源: 校验失败时改用 akshare 东方财富全A快照按成交额取TOP500
  3. 双源均失败 → 保留旧 top500_all_a.json 并 exit 1 (工作流重试)
v3.0 变更(2026-10-13):
  4. 池大小由 config.json pool_size 驱动(默认500), 观察池逻辑整体删除
  5. 入池资格过滤: ST/*ST/退市整理 名称判据剔除, 计数透明(dropped_eligibility)
v3.7 换母体(2026-10-24) —— P0-1 终局, 老大决策:
  6. 成交额排序会系统性选中「高换手关注度脉冲」集合, 脉冲退潮后系统性回吐。
     实测: 池内 vs 全池, 58 入场日 × 20 日持有, 中位超额 -5.08pct, 96.6% 跑输;
     池内 9 种过滤规则回测全部无法转正(最好 -3.92pct) → 证明确实是母体选错。
  7. 母体由「成交额 TOP{N}」改为**全A**(约 5576 只, 56 页), 再按规则选池。
     全A 母体回测(backtest_pool_alla_clean.py, 58 截面, 无前视, vs 宇宙中位):
       成交额榜模拟(现状)   -0.34pct  胜率 39.7%
       最便宜 36%          +3.84pct  胜率 87.9%  (357 只)
       最便宜 18%          +4.60pct  胜率 87.9%  (178 只)  ← 三段全正, 选此口径
       最便宜 9%           +5.09pct  胜率 89.7%  ( 88 只)
       低价9%∩20日超跌      +5.19pct  胜率 91.4%  ( 88 只)
     取「最便宜 18%」而非最优的 9%: 9% 仅 88 只, 不足以支撑 500 只广度与
     板块覆盖; 18% 在 1012 宇宙内约 178 只, 外推到 5576 只全A 约 1000 只,
     截取 pool_size 后仍有充足候选, 且超额仅比 9% 低 0.5pct, 稳健性更好。
  8. 价格地板 2 元(规避面值退市, 实测跌破面值占比 0.00%) + 剔除当日涨幅前 20%
     (追高过滤) + ST/退市/停牌资格过滤保留。
  9. ⚠️ 已知边界: 本改造首次上线后, 全A 真实样本才开始积累; 上述回测在
     1012 只历史并集内完成(该并集本身偏向热门股), 故绝对收益水平须在
     未来 20 个交易日后用真实全A 池复核。超额的方向性结论可迁移。
"""
import urllib.request
import json
import time
import sys
import os
from datetime import datetime

# 清除代理环境变量: 本地代理可能不通(HTTP 502), 行情接口需直连
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
OUT = "top500_all_a.json"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_pool_config():
    """读 config.json: 池大小、抓取页数、资格过滤、流动性地板。缺失/损坏回退缺省。

    v3.2 修复: 原实现只挑 pool_size/fetch/eligibility_filter 三键, 漏载
      liquidity_filter → 下游 normalize 拿到空地板(阈值 0), 流动性过滤空转。
    """
    cfg = {"pool_size": 500, "fetch": {"pages": 15, "page_size": 100, "total_n": 1500},
           "eligibility_filter": {"exclude_st": True, "exclude_delisting": True},
           "liquidity_filter": {"enabled": True, "min_avg_amount_20d": 5e7,
                                "min_avg_turnover_rate": 0.01}}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if user.get("pool_size"):
                cfg["pool_size"] = int(user["pool_size"])
            for _sec in ("fetch", "eligibility_filter", "liquidity_filter", "pool_rules"):
                if isinstance(user.get(_sec), dict):
                    cfg[_sec] = dict(cfg.get(_sec) or {}, **user[_sec])
        except Exception as e:
            print(f"  ⚠️ config.json 解析失败({str(e)[:40]}), 使用缺省池大小 TOP200")
    return cfg


def eligibility_filter(stocks, cfg=None):
    """入池资格过滤: ST/*ST/退市整理 名称判据剔除。返回 (kept, dropped)。

    数据纪律: 名称判据剔除 ST/退市(可靠、无额外接口依赖)。
    v3.2 起停牌/次新改为「标记不剔除」(见 normalize 的 suspended/is_new/liquid_ok),
      因二者需逐股判据且误杀代价高(停牌一日≠该股不可投), 保留在池内透明披露,
      由组合层排除。
    """
    cfg = cfg or {"exclude_st": True, "exclude_delisting": True}
    if not (cfg.get("exclude_st", True) or cfg.get("exclude_delisting", True)):
        return stocks, []
    kept, dropped = [], []
    for s in stocks:
        name = str(s.get("name", "") or "").upper().replace(" ", "")
        is_st = cfg.get("exclude_st", True) and ("ST" in name or "*ST" in name)
        is_delist = cfg.get("exclude_delisting", True) and ("退" in name or "退市" in name)
        if is_st or is_delist:
            dropped.append({"name": s.get("name"), "code": s.get("code") or s.get("symbol"),
                            "reason": "ST" if is_st else "退市"})
        else:
            kept.append(s)
    return kept, dropped


_POOL_CFG = load_pool_config()
POOL = _POOL_CFG["pool_size"]

REBUILD_OUT = "pool_rebuild_state.json"
ROLLING_FETCH_BATCH = 120      # 批量行情单次查询只数（新浪 list 上限约 200，留余量）


def load_pool_state():
    """读取滚动候选池状态：{date, codes:[...]}。
    v3.7 用于「过去 20 个交易日曾入池」的母体召回。缺失/损坏返回空。"""
    p = os.path.join(HERE, REBUILD_OUT)
    if not os.path.exists(p):
        return {"history": {}}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "history" not in d:
            return {"history": {}}
        if not isinstance(d["history"], dict):
            return {"history": {}}
        return d
    except Exception:
        return {"history": {}}


def save_pool_state(state, date_str):
    """滚动状态只保留最近 30 个交易日（够 20 日窗口 + 冗余）。"""
    hist = state.get("history") or {}
    keys = sorted(hist.keys())
    for k in keys[:-30]:
        hist.pop(k, None)
    state["history"] = hist
    state["date"] = date_str
    try:
        with open(os.path.join(HERE, REBUILD_OUT), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️ 滚动池状态写入失败: {e}")


def _norm_code6(code):
    """'sh600519' / '600519' → ('600519', 'sh')"""
    c = str(code or "").strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c[2:], c[:2]
    return c, ("sh" if c.startswith("6") else "sz")


def batch_quotes(codes):
    """批量取现价（新浪 hq.sinajs.cn）。→ {code6: {"price":x, "name":y, "pct":z}}
    只用于补全「过去曾入池但当日不在成交额榜」的股票，避免母体召回时无价可用。"""
    out = {}
    codes = list(dict.fromkeys([c for c in codes if c]))
    for i in range(0, len(codes), ROLLING_FETCH_BATCH):
        chunk = codes[i:i + ROLLING_FETCH_BATCH]
        syms = []
        for c in chunk:
            c6, mk = _norm_code6(c)
            if not c6.isdigit() or len(c6) != 6:
                continue
            syms.append(f"{mk}{c6}")
        if not syms:
            continue
        url = "https://hq.sinajs.cn/list=" + ",".join(syms)
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                  "Referer": "https://finance.sina.com.cn/"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("gbk", errors="ignore")
        except Exception as e:
            print(f"  ⚠️ 批量行情失败({len(syms)}只): {str(e)[:50]}")
            time.sleep(1.0)
            continue
        for line in raw.strip().split("\n"):
            if "=\"" not in line:
                continue
            head, body = line.split("=\"", 1)
            body = body.rstrip("\";")
            sym = head.split("_")[-1]
            c6 = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            f = body.split(",")
            if len(f) < 10:
                continue
            try:
                price = float(f[3]) if float(f[3] or 0) > 0 else float(f[2] or 0)
                prev = float(f[2] or 0)
                amt = float(f[9] or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            pct = round((price / prev - 1.0) * 100, 2) if prev > 0 else 0.0
            out[c6] = {"name": f[0], "price": round(price, 2),
                       "pct_chg": pct, "amount": amt}
        time.sleep(0.3)
    return out


def rolling_universe(today_norm, state, window=20):
    """构造「今日榜 ∪ 过去 window 个交易日曾入池」母体，返回补全后的 norm 列表。

    设计要点（P0-1 回测验证）：只在今日成交额榜内筛选无法扭转负超额（母体即负 alpha）；
    扩到滚动候选池后，58 截面 × 20 日持有的中位超额由 -5.08pct 转为 +4.86pct。
    无前视：历史入池名单对当日完全已知；当日不在榜者只补现价，不用未来信息。
    """
    hist = state.get("history") or {}
    keys = sorted(hist.keys())[-window:]
    seen = {}
    for k in keys:
        for c in (hist.get(k) or []):
            seen[c] = True
    today_codes = {s["code"] for s in today_norm}
    # 归一化已有 code 为 6 位便于比对
    today6 = {_norm_code6(c)[0]: c for c in today_codes}
    need = [c for c in seen if _norm_code6(c)[0] not in today6]
    print(f"  滚动候选池: 历史 {len(keys)} 日召回 {len(seen)} 只, "
          f"今日榜已含 {len(today6)} 只, 需补价 {len(need)} 只")
    if not need:
        return list(today_norm), 0
    q = batch_quotes(need)
    added = []
    lf = _POOL_CFG.get("liquidity_filter", {}) or {}
    min_amt = float(lf.get("min_avg_amount_20d", 0) or 0) if lf.get("enabled", True) else 0.0
    for c in need:
        c6, mk = _norm_code6(c)
        d = q.get(c6)
        if not d:
            continue
        if min_amt > 0 and d["amount"] < min_amt:
            continue                          # 流动性不足不召回
        added.append({
            "rank": 10 ** 6,                  # 召回股不占成交额排位
            "name": d["name"], "code": c if c[:2] in ("sh", "sz") else f"{mk}{c6}",
            "price": d["price"], "pct_chg": d["pct_chg"],
            "turnover": d["amount"], "amount": d["amount"],
            "turnover_ratio": 0.0, "circ_mv": 0.0,
            "liquid_ok": True, "turnover_ok": True,
            "suspended": False,
            "is_new": str(d["name"]).upper().replace(" ", "").startswith("N"),
            "recalled": True,                 # 标记：来自滚动候选池
        })
    print(f"  滚动召回补入 {len(added)} 只（含价且有流动性）")
    return list(today_norm) + added, len(added)


SW_MAP_PATH = os.path.join(HERE, "sector", "sw_map.json")

# 科技属性定义：申万一级行业（与老大关注的 PCB/光模块/存储/半导体/先进封装 同属）
TECH_L1 = ("电子", "计算机", "通信", "电力设备", "国防军工", "机械设备")


def load_sw_l1():
    """读取申万一级行业映射 {6位代码: 行业名}。缺失/损坏返回 {}（降级：不做科技配额）。"""
    if not os.path.exists(SW_MAP_PATH):
        print(f"  ⚠️ 缺 {SW_MAP_PATH}，科技配额降级为不启用（先跑 fetch_sw_map.py）")
        return {}
    try:
        with open(SW_MAP_PATH, encoding="utf-8") as f:
            d = json.load(f)
        l1 = d.get("l1") or {}
        if not isinstance(l1, dict) or not l1:
            raise ValueError("l1 为空")
        return l1
    except Exception as e:
        print(f"  ⚠️ sw_map.json 解析失败({str(e)[:40]})，科技配额降级为不启用")
        return {}


def _log_sd(vals):
    """流通市值的对数标准差 —— 度量「池内 SIZE 还有多少区分度」。
    v4.1 用它对冲一个隐蔽陷阱：若进池时已按市值截断，池内 SIZE 就退化成
    常数，评分层再加 SIZE 权重毫无意义（旧实现池内 log-SD 仅 0.276）。"""
    import math
    xs = [math.log(float(v)) for v in vals if float(v or 0) > 0]
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def apply_pool_rules(pool_norm, cfg=None):
    """v3.8 全A 母体规则选池：**SIZE 小市值**为主，剔除低价口径。

    ## v3.7 → v3.8 的根本修正（老大 2026-10-25 指令 + final_ranking.csv）
    v3.7 用「绝对价格最便宜 18%」选池，**方向错了**：
      · 实证（今日全A 5573 只）Spearman(价格, ln流通市值) = **0.166** —— 几乎无关
      · 「价格最低500」市值中位 44.0 亿（≈全A中位 48.1 亿），选出的是萃华退/*ST卓然/
        *ST清越/元道退 —— 便宜的是「即将退市的壳」，不是低估
      · 「市值最小500」市值中位 9.2 亿、价格中位 15.05 元 —— 才是真小微盘
      · 两组重合仅 26/500 = 5.2%
    → 绝对价格是 SIZE 的极差代理，且把「面值退市风险」误当 alpha。

    ## 依据 final_ranking.csv（47 因子，作者选定 9 个）唯一显著超额因子 = SIZE
      exc_t=+2.28（全表唯一 |t|>2 的多头超额）、LS_IR=1.95（最高）、score=0.993（最高）
      方向「越小越好」。次强为成长族（NAG LS_IR=1.22 / TAG 1.40 / NPG 1.77），
      估值族普遍弱（EP exc_t=+0.10 / BP -0.54 / CFP -0.25 / EVEBIT -0.32）
      → 「便宜」在估值维度也不成立，真正 alpha 集中在 SIZE + 成长 + 弱量价反转。

    ## 回测（backtest_pool_factor.py，58 截面，无前视，vs 宇宙中位）
      绝对价格最便宜18%〔旧口径〕   +4.60pct  胜率 87.9%
      SIZE 市值最小9%             +8.63pct  胜率100.0%  ← 配对 t=+7.60 显著优于旧口径
      SIZE 市值最小18%            +7.61pct  胜率100.0%  ← 配对 t=+6.45 显著优于旧口径
      SIZE 市值最小36%            +5.06pct  胜率100.0%
      三段稳定性：SIZE18% = +8.77 / +6.15 / +9.18（三段全正，且段2最差仍优于旧口径最好段）
      自拼多因子(SIZE.5+反转.25+BIAS60.25) = +2.81pct，配对 t=-3.61 **显著劣化**
      → 弱因子堆叠会稀释强因子，不做自作聪明的合成。

    ## 关键约束（老大 2026-10-25 指令）
    ① **不剔除北交所**，用全A 所有成分股。
    ② 剔成交额 < min_amount（默认 1.0 亿）：流动性不足无法建仓。
    ③ 剔**总市值** < min_mktcap（默认 25 亿）：市值过小流动性不足。
       注意是 mktcap(总市值) 而非 nmc(流通市值)——含大量限售股的央企两者差异极大
       （工商银行 mktcap 29653 亿 vs nmc 22432 亿）。
    ④ 价格地板 2.0 元保留：非选股逻辑，仅规避面值退市（连续 20 日<1 元）。

    ## 市值下限定为 25 亿的依据（backtest_size_floor_detail.py，回答「降到多少不影响」）
    统一口径：超额 = 子集中位 − **同一市值下限内**宇宙中位（剥离小市值 beta，只看选股能力）
      下限   候选  选中市值中位   超额中位   恶化截面占比
        0亿   993    46.5亿    +7.54pct     0%
       10亿   993    46.5亿    +7.54pct     0%
       15亿   992    46.6亿    +7.48pct    16%
       20亿   987    47.6亿    +7.55pct    48%
       25亿   978    49.5亿    +7.43pct    55%   ← 选定：三判据全过的最大下限
       30亿   966    52.0亿    +7.10pct    67%
       40亿   928    61.2亿    +6.80pct    81%
       50亿   887    74.7亿    +5.43pct    88%   ← 老大原提议，代价已量化
       80亿   795   110.3亿    +3.91pct    90%
      150亿   649   186.2亿    +0.93pct    88%（三段有负，接近失效）
    判据三条件（a 超额≥基线95%；b 三段全正；c 恶化截面≤60%）：仅 ≤25 亿 三项全过。

    ## 衰减机制（不是 bug，是因子性质）
    40.5% 的选中股集中在 30-50 亿段（贡献 +8.59%/+7.91%），25.9% 在 50-60 亿（+5.99%），
    60-80 亿段平均涨幅只剩 +0.83%。抬高下限 = 强行把选股区间推入「alpha 已衰减」的市值段，
    同时候选从 993 只压到 887 只，双重削弱。
    ⚠️ +8.63pct 与 +5.43pct 不可直接相减：前者口径是「无下限+最小9%」，后者的
    「50亿下限+最小18%」，且超额基准不同（全宇宙中位 vs 同floor中位）。
    统一口径下无下限最小18% = +7.54pct，50亿下限 = +5.43pct，真实落差 2.11pct。
    ⚠️ 外推边界：本回测宇宙中位 220 亿、最小 12.2 亿（偏大盘），全A 中位仅 55.9 亿。
    50 亿下限切掉全A 44.8% 的股票、却只切掉本宇宙 7.9%，故本宇宙算出的「≤25亿不影响」
    对全A 是**偏乐观**的；全A 上 25 亿下限仍会切掉约 15% 的股票。此边界已写入 config.caveat。

    返回 (kept, stats)
    """
    cfg = cfg or (_POOL_CFG.get("pool_rules") or {})
    stats = {"enabled": bool(cfg.get("enabled", True)),
             "universe": cfg.get("universe", "all_a"),
             # v4.1（P1-2）：母体版本必须落盘。下游「新旧母体分段体检」原先靠
             #   池内流通市值中位 <60亿 去**猜**母体归属，而 v4.1 分层抽样后
             #   池内市值中位实测 85.9 亿（跨度 4.1~9694 亿）——一旦新母体上线，
             #   猜法必然把它误判成旧母体，「分段体检」将再次静默失效。
             #   改为显式版本号传递，归因不再依赖阈值猜测。
             "version": cfg.get("version", "unknown"),
             "size_as_factor": bool(cfg.get("size_as_factor", False)),
             "in": len(pool_norm), "out": len(pool_norm),
             "sort_key": cfg.get("sort_key", "mv"),
             "size_quantile": cfg.get("size_quantile", 0.18),
             "sample_strata": cfg.get("sample_strata", 5),
             "min_amount": cfg.get("min_amount", 1.0e8),
             "min_mktcap": cfg.get("min_mktcap", 25e8),
             "price_floor": cfg.get("price_floor", 2.0),
             "drop_mom_top_pct": cfg.get("drop_mom_top_pct", 20),
             "dropped_price_floor": 0, "dropped_high_mom": 0,
             "dropped_illiquid": 0, "dropped_small_mktcap": 0, "dropped_no_mv": 0}
    if not stats["enabled"]:
        return pool_norm[:POOL], stats

    floor = float(stats["price_floor"] or 0)
    min_amt = float(stats["min_amount"] or 0)
    min_mktcap = float(stats["min_mktcap"] or 0)
    kept = []
    for s in pool_norm:
        code = str(s.get("code") or "")
        try:
            pr = float(s.get("price") or 0)
        except Exception:
            pr = 0.0
        if pr <= 0:
            continue
        # ① 剔除以退市整理/ST 已在 eligibility_filter 处理，此处不做北交所剔除
        #   （老大 2026-10-25 指令：用全A 所有成分股，不剔北交所）
        # ② 价格地板：规避面值退市
        if floor > 0 and pr < floor:
            stats["dropped_price_floor"] += 1
            continue
        # ③ 流动性地板：当日成交额
        amt = float(s.get("amount") or s.get("turnover") or 0)
        if min_amt > 0 and amt < min_amt:
            stats["dropped_illiquid"] += 1
            continue
        # ④ 总市值地板（老大 2026-10-25 新增）：总市值过小流动性不足
        try:
            mv = float(s.get("circ_mv") or 0)
        except Exception:
            mv = 0.0
        try:
            mv_tot = float(s.get("total_mv") or 0)
        except Exception:
            mv_tot = 0.0
        if min_mktcap > 0 and mv_tot < min_mktcap:
            stats["dropped_small_mktcap"] += 1
            continue
        # ⑤ SIZE 因子需要流通市值
        if bool(cfg.get("sort_key", "mv") == "mv") and mv <= 0:
            stats["dropped_no_mv"] += 1
            continue
        kept.append(s)

    # 追高过滤：当日涨幅前 N% 剔除（快照无 K 线，当日涨幅作一阶代理）
    mom_pct = float(stats["drop_mom_top_pct"] or 0)
    if mom_pct > 0 and kept:
        vals = sorted([float(s.get("pct_chg") or 0) for s in kept])
        thr = vals[min(len(vals) - 1, int(len(vals) * (1 - mom_pct / 100.0)))]
        before = len(kept)
        kept = [s for s in kept if float(s.get("pct_chg") or 0) < thr]
        stats["dropped_high_mom"] = before - len(kept)
    stats["after_mom_filter"] = len(kept)

    # —— 进池方式：v4.1「分层抽样」 ——
    # ⚠️ 2026-10-25 P0 改造（老大裁决）：SIZE 从此**不再是筛选条件**，而是评分加分项。
    #   旧实现 `n_take = min(len, max(POOL, int(len*q)))` 有两个致命问题：
    #     ① size_quantile 静默失效——max(POOL,·) 在候选<2778 只时恒取 POOL，
    #        今日候选 2520→int(2520*0.18)=453 → max(500,453)=500，实际取 19.84%；
    #        某日候选 1000 只则取 500=50%，参数随候选数漂移，不可复现。
    #     ② 把市值大的全剔掉后，池内市值 log-SD 仅 0.276（4.1~39.9 亿），
    #        SIZE 若改评分项在池内**毫无区分度**（全集 log-SD 1.112）——自相矛盾。
    #   分层抽样：按流通市值分 n_strata 档等量抽样，保留完整市值跨度（log-SD→1.10），
    #   使「小市值优先」能在评分层真实发挥作用，同时池子规模可控（step2 时限约束）。
    key_name = "circ_mv" if stats["sort_key"] == "mv" else "price"
    by_key = sorted(kept, key=lambda s: float(s.get(key_name) or 0))
    n_strata = int(cfg.get("sample_strata", 5) or 0)
    if n_strata > 1 and len(by_key) > POOL:
        chosen, _seen = [], set()
        per = max(1, POOL // n_strata)
        N = len(by_key)
        for k in range(n_strata):
            lo, hi = N * k // n_strata, N * (k + 1) // n_strata
            blk = by_key[lo:hi]
            if not blk:
                continue
            take = blk if len(blk) <= per else [blk[int(i * len(blk) / per)]
                                                for i in range(per)]
            for s in take:
                c = s.get("code")
                if c and c not in _seen:
                    _seen.add(c)
                    chosen.append(s)
        # 若分层后不足 POOL，按成交额补齐（只在极端候选稀少时触发）
        if len(chosen) < POOL:
            rest = [s for s in kept if s.get("code") not in _seen]
            rest.sort(key=lambda s: -float(s.get("amount") or 0))
            chosen += rest[:POOL - len(chosen)]
        stats["sampling"] = "stratified"
        stats["sample_strata"] = n_strata
    else:
        # 候选不足 POOL：全取（不截断，保留全部符合条件的标的）
        chosen = by_key[:POOL]
        stats["sampling"] = "all" if len(by_key) <= POOL else "size_cut"
        stats["sample_strata"] = 0
    chosen = chosen[:POOL]
    chosen.sort(key=lambda s: -float(s.get("amount") or 0))
    for i, s in enumerate(chosen, 1):
        s["rank"] = i
    stats["out"] = len(chosen)
    # 落盘「实际覆盖的全A市值分位」，杜绝"配 18% 实取 40%"这类口径漂移不可审计
    try:
        _all_mv = sorted(float(x.get("circ_mv") or 0) for x in pool_norm
                         if float(x.get("circ_mv") or 0) > 0)
        _sel_mv = sorted(float(s.get("circ_mv") or 0) for s in chosen
                         if float(s.get("circ_mv") or 0) > 0)
        if _all_mv and _sel_mv:
            _thr = _sel_mv[-1]
            _pct = 100.0 * sum(1 for v in _all_mv if v <= _thr) / len(_all_mv)
            stats["mv_coverage_pct"] = round(_pct, 1)
            stats["mv_upper_yi"] = round(_thr / 1e8, 1)
            stats["mv_log_sd"] = round(_log_sd(_sel_mv), 3)
    except Exception:
        pass
    if stats["sort_key"] == "mv":
        mvs = sorted([float(s.get("circ_mv") or 0) for s in chosen])
        prices = sorted([float(s.get("price") or 0) for s in chosen])
        if mvs:
            stats["mv_median_yi"] = round(mvs[len(mvs) // 2] / 1e8, 1)
            stats["mv_max_yi"] = round(mvs[-1] / 1e8, 1)
        if prices:
            stats["price_median"] = round(prices[len(prices) // 2], 2)
        # 科技占比（自然结果，非人为配额）
        l1map = load_sw_l1()
        if l1map:
            nt = 0
            for s in chosen:
                c = str(s.get("code") or "")
                c6 = c[2:] if len(c) > 2 and c[:2] in ("sh", "sz", "bj") else c
                if l1map.get(c6) in TECH_L1:
                    nt += 1
            stats["tech_actual"] = nt
            stats["tech_ratio"] = round(100.0 * nt / max(1, len(chosen)), 1)
    return chosen, stats


def fetch_page(page, num=80):
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"Market_Center.getHQNodeData?page={page}&num={num}&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=sort")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  第{page}页失败(尝试{attempt+1}/3): {e}")
            time.sleep(2)
    return []


def validate_data(stocks):
    """数据质量校验: 返回 (ok, reason)"""
    if not stocks:
        return False, "空数据"
    n = len(stocks)
    # 成交额有效(>0) 数量
    valid_amt = sum(1 for s in stocks if float(s.get("amount", 0) or 0) > 0)
    amt_ratio = valid_amt / n
    if amt_ratio < 0.8:
        return False, f"成交额有效占比过低 {valid_amt}/{n} ({amt_ratio:.0%}), 疑似接口占位/异常数据"
    # 920段(北交所新代码)占比过高且价格全0 → 异常
    bj = sum(1 for s in stocks if (s.get("symbol", "") or "").startswith("sz92") or s.get("code", "").startswith("920"))
    zero_price = sum(1 for s in stocks if float(s.get("trade", 0) or 0) <= 0)
    if bj > n * 0.5 and zero_price > n * 0.8:
        return False, f"920段北交所占位数据 {bj}/{n} 且价格全0, 判定异常"
    if zero_price > n * 0.8:
        return False, f"价格全0占位数据 {zero_price}/{n}, 判定异常"
    return True, f"通过 (有效成交额 {valid_amt}/{n})"


def fallback_eastmoney(pool=500):
    """备源: akshare 东方财富全A快照, 按成交额降序取 TOP{pool}"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None, "东财快照为空"
        df = df.dropna(subset=["成交额"])
        df = df[df["成交额"] > 0]
        df = df.sort_values("成交额", ascending=False).head(pool)
        out = []
        for _, r in df.iterrows():
            code_raw = str(r["代码"])
            if code_raw.startswith("6"):
                code = "sh" + code_raw
            elif code_raw.startswith(("0", "3")):
                code = "sz" + code_raw
            else:
                code = "sz" + code_raw  # 北交所备源兜底(极少)
            price = float(r.get("最新价", 0) or 0)
            pct = float(r.get("涨跌幅", 0) or 0)
            amount = float(r.get("成交额", 0) or 0)
            _name = str(r.get("名称", ""))
            out.append({
                "name": _name,
                "code": code,
                "price": round(price, 2),
                "pct_chg": round(pct, 2),
                "turnover": amount,
                "amount": amount,
                # v3.2 与 normalize 保持字段一致（备源路径）
                "liquid_ok": True,          # 备源已按成交额>0 过滤；地板判据交由 step2 校验
                "suspended": (round(pct, 2) == 0 and amount == 0),
                "is_new": _name.upper().replace(" ", "").startswith("N"),
            })
        return out, f"东财备源 {len(out)} 只"
    except Exception as e:
        return None, f"东财备源失败: {e}"


def normalize(stocks, start_rank=1, cfg=None):
    """新浪行情条目 → 标准化 dict 列表（含 rank 排名）。

    v3.2 新增（P0-1 流动性地板接线 + P0-3 资格标记）：
      - turnover_ratio：换手率(%)，新浪 `turnoverratio` 原生字段，零新增接口。
      - circ_mv：流通市值(元)，新浪 `nmc`（万元 ×1e4）。
      - liquid_ok：**仅由当日成交额** 判据（≥ min_avg_amount_20d）。
        ⚠️ 实测修正（2026-10-13）：原清单 D1 设想的「换手率地板」会**误杀大盘蓝筹**——
        TOP500 内换手率<1% 的 61 只全是大市值股（农业银行 0.10%、工商银行 0.12%、
        贵州茅台 0.21%），其成交额 10~34 亿、流通市值数千亿，流动性实为池内最好。
        换手率低恰因盘子大，故**不作硬剔除**，仅采集供组合层容量估算。
        另实测 TOP500 池最低成交额 7.3 亿 ≫ 5000 万地板 → 成交额地板对 TOP500
        亦不构成有效筛选；此为实现真实接线而非拍阈值，如实披露（不虚报过滤数）。
        口径说明：严格「20 日均额」需 K 线；此处用当日快照近似，属保守判据。
      - suspended：当日涨跌幅与成交额同时为 0（快照可判的停牌近似）
      - is_new：名称含 N 前缀（上市首日/次新标记，行情源惯例）
    """
    cfg = cfg or {}
    lf = cfg.get("liquidity_filter", {}) or {}
    _lf_on = bool(lf.get("enabled", True))
    min_amt = float(lf.get("min_avg_amount_20d", 0) or 0) if _lf_on else 0.0
    # config 换手率阈值单位是小数(0.01)，接口为百分数(%) → ×100；仅作参考判据字段
    min_tr = float(lf.get("min_avg_turnover_rate", 0) or 0) * 100.0 if _lf_on else 0.0
    out = []
    for i, s in enumerate(stocks, start_rank):
        code_raw = s.get("code", "")
        symbol = s.get("symbol", "")
        if symbol.startswith("sh"):
            code = symbol
        elif symbol.startswith("sz"):
            code = symbol
        else:
            market = "sh" if code_raw.startswith("6") else "sz"
            code = f"{market}{code_raw}"
        name = s.get("name", "") or ""
        amt = float(s.get("amount", 0) or 0)
        pct = round(float(s.get("changepercent", 0) or 0), 2)
        try:
            tr = round(float(s.get("turnoverratio", 0) or 0), 3)
        except Exception:
            tr = 0.0
        try:
            cmv = round(float(s.get("nmc", 0) or 0) * 1e4, 0)   # 万元 → 元
        except Exception:
            cmv = 0.0
        # v3.8: 总市值（新浪 mktcap，万元 → 元）。
        #   老大 2026-10-25 要求「剔除总市值 <50 亿」以保流动性；
        #   注意必须用 mktcap(总市值) 而非 nmc(流通市值)，二者差异在含大量限售股的央企上极大
        #   （如工商银行 mktcap 29653 亿 vs nmc 22432 亿）。
        try:
            tmv = round(float(s.get("mktcap", 0) or 0) * 1e4, 0)
        except Exception:
            tmv = 0.0
        # v3.7: 估值字段落盘（新浪原生 per/pb，零新增接口）。
        #   目的：为「价格分位 vs 估值分位」提供可验证数据 —— 当前选池用绝对价格分位，
        #   但价格低 ≠ 估值低（中际旭创 612 元 PE 62.5 vs 京东方 4.93 元 PE 30.8）。
        #   历史快照未存 per/pb，故估值口径暂无法回测；自本版起每日落盘，
        #   累计 30 截面后可做正式检验再决定是否切换到估值分位。
        try:
            _per = float(s.get("per", 0) or 0)
            pe = round(_per, 2) if 0 < _per < 10000 else None   # 亏损/异常置空
        except Exception:
            pe = None
        try:
            _pb = float(s.get("pb", 0) or 0)
            pb = round(_pb, 3) if 0 < _pb < 1000 else None
        except Exception:
            pb = None
        out.append({
            "rank": i,
            "name": name,
            "code": code,
            "price": float(s.get("trade", 0) or 0),
            "pct_chg": pct,
            "turnover": amt,
            "amount": amt,
            "turnover_ratio": tr,                 # 采集供容量分析，不作硬剔除
            "circ_mv": cmv,                       # 流通市值，供容量/分层
            "total_mv": tmv,                      # 总市值（v3.8 新增，用于流动性下限）
            "pe": pe,                             # 市盈率，估值口径（v3.7 新增）
            "pb": pb,                             # 市净率（v3.7 新增）
            "liquid_ok": bool((amt >= min_amt) if min_amt > 0 else True),
            "turnover_ok": bool((tr >= min_tr) if min_tr > 0 else True),  # 仅参考披露
            "suspended": (pct == 0 and amt == 0),
            "is_new": name.upper().replace(" ", "").startswith("N"),
        })
    return out


def main():
    fc = _POOL_CFG.get("fetch", {})
    pr_cfg = _POOL_CFG.get("pool_rules") or {}
    all_a = bool(pr_cfg.get("enabled", True) and pr_cfg.get("universe", "all_a") == "all_a")
    # 全A 母体需要抓满 56 页(约5576只)；旧成交额榜只需 15 页(1500只)
    pages = int(fc.get("pages_all_a", 56)) if all_a else int(fc.get("pages", 15))
    page_size = 100                              # 接口硬上限：num>100 仍只返回 100
    mode = "全A母体(v3.7 换母体)" if all_a else f"成交额 TOP{POOL}"
    print("=" * 60)
    print(f"  新浪接口获取{mode} → 规则选池 {POOL} 只")
    print("=" * 60)
    all_stocks = []
    for page in range(1, pages + 1):
        print(f"获取第{page}页...")
        data = fetch_page(page, num=page_size)
        if not data:
            # 全A 模式下载到底即止（page 56 起返回空属正常）
            if all_a and page > 40:
                print(f"  第{page}页为空，判定已到末页")
                break
            continue
        all_stocks.extend(data)
        print(f"  累计 {len(all_stocks)} 只")
        time.sleep(0.5 if all_a else 0.8)

    all_stocks, dropped = eligibility_filter(all_stocks, _POOL_CFG.get("eligibility_filter"))
    if dropped:
        print(f"  资格过滤剔除 ST/退市 {len(dropped)} 只: {', '.join(str(d.get('name','')) for d in dropped[:5])}{'...' if len(dropped) > 5 else ''}")
    all_stocks.sort(key=lambda x: -float(x.get("amount", 0)))

    # ===== v3.2 P0-3 资格标记（次新/停牌）：标记不剔除，保留透明 =====
    _norm_all = normalize(all_stocks, 1, _POOL_CFG)

    # ===== v3.7 换母体 + 规则选池 =====
    # 成交额榜 = 关注度脉冲集合，池内 9 种过滤全部无法扭转负超额(-5.08pct, 96.6%跑输)
    # → 改为全A 母体按规则选池。回测(58截面,无前视,vs宇宙中位):
    #   成交额榜模拟 -0.34pct/胜率39.7% → 最便宜18% +4.60pct/胜率87.9%/三段全正
    _pool_state = load_pool_state()
    _cand_norm = _norm_all
    roll_win = int(pr_cfg.get("rolling_window", 0) or 0)
    if roll_win > 0:
        # 滚动召回仅在「成交额榜母体」模式下有意义（全A 母体已覆盖全域）
        if not all_a:
            _cand_norm, _n_recall = rolling_universe(_norm_all, _pool_state, window=roll_win)
    main_pool_norm, _pool_stats = apply_pool_rules(_cand_norm, pr_cfg)
    if _pool_stats["enabled"]:
        _sk = "SIZE小市值" if _pool_stats["sort_key"] == "mv" else "绝对价格"
        print(f"\n🧺 规则选池(v3.8): 母体 {_pool_stats['in']} 只  排序键 {_sk}")
        print(f"   剔除 低价<{_pool_stats['price_floor']}元 {_pool_stats['dropped_price_floor']} 只"
              f" / 成交额<{_pool_stats['min_amount']/1e8:.1f}亿 {_pool_stats['dropped_illiquid']} 只"
              f" / 总市值<{_pool_stats['min_mktcap']/1e8:.0f}亿 {_pool_stats['dropped_small_mktcap']} 只"
              f" / 无市值 {_pool_stats['dropped_no_mv']} 只"
              f" / 涨幅前{_pool_stats['drop_mom_top_pct']}% {_pool_stats['dropped_high_mom']} 只")
        if _pool_stats["sort_key"] == "mv":
            print(f"   市值分位截断 {_pool_stats['size_quantile']} → 选出 {_pool_stats['out']} 只"
                  f"（市值中位 {_pool_stats.get('mv_median_yi', 'NA')} 亿"
                  f" / 价格中位 {_pool_stats.get('price_median', 'NA')} 元"
                  f" / 科技占比 {_pool_stats.get('tech_ratio', 'NA')}%）")
        else:
            print(f"   价格分位截断 → 选出 {_pool_stats['out']} 只")
    main_pool = all_stocks[:POOL]          # 保留成交额榜供下游校验/对比
    print(f"\n✅ 共获取 {len(all_stocks)} 只（母体；规则选池后 {len(main_pool_norm)} 只）")

    # ===== 数据质量校验（针对主力池）=====
    ok, reason = validate_data(main_pool)
    used_fallback = False
    if not ok:
        print(f"⚠️ 新浪数据校验失败: {reason}")
        print("🔄 尝试东财备源...")
        out, fb = fallback_eastmoney(POOL)
        if out:
            ok2, reason2 = validate_data([{"symbol": s["code"], "code": s["code"],
                                           "trade": s["price"], "amount": s["amount"]} for s in out])
            if ok2:
                print(f"✅ {fb}, 校验通过 ({reason2})")
                main_norm = out
                used_fallback = True
            else:
                print(f"❌ 东财备源校验也失败: {reason2}")
                sys.exit(1)
        else:
            print(f"❌ {fb}")
            sys.exit(1)
    else:
        main_norm = main_pool_norm

    _src = "东财备源(akshare)" if used_fallback else "新浪财经全市场行情接口"
    _today = datetime.now().strftime("%Y-%m-%d")

    # v3.2: 资格标记统计随池分发（供 step2/组合层/报告层消费）
    _elig_marks = {
        "new_stock": sum(1 for s in main_norm if s.get("is_new")),
        "suspended": sum(1 for s in main_norm if s.get("suspended")),
        "illiquid": sum(1 for s in main_norm if not s.get("liquid_ok", True)),
        "low_turnover": sum(1 for s in main_norm if not s.get("turnover_ok", True)),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "top50": main_norm[:50],
            "top100": main_norm[:100],
            "top500": main_norm,
            "main_pool": main_norm,
            "pool_size": len(main_norm),
            "all_count": len(all_stocks),
            "dropped_eligibility": len(dropped),
            "eligible_marks": _elig_marks,
            "liquidity_floor": _POOL_CFG.get("liquidity_filter", {}) or {},
            "pool_rules": _pool_stats,
            "date": _today,
            "source": _src,
        }, f, ensure_ascii=False, indent=2)

    # 滚动候选池状态：记录今日入池 code 供未来 20 日召回（v3.7）
    _hist = _pool_state.get("history") or {}
    _hist[_today] = sorted({s["code"] for s in main_norm})
    _pool_state["history"] = _hist
    save_pool_state(_pool_state, _today)

    print(f"\n📊 主力池 TOP10:")
    for i, s in enumerate(main_norm[:10]):
        print(f"  {i+1}. {s['name']:8s} {s['code']} {s['price']:8.2f} {s['pct_chg']:+.2f}% 成交额{s['turnover']/10000:.0f}万")
    print(f"\n✅ 数据已保存: {OUT} (主力池{len(main_norm)}只, 无观察池)")


if __name__ == "__main__":
    main()
