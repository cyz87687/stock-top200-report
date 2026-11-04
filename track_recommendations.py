#!/usr/bin/env python3
"""
推荐跟踪 (P0-4): 记录每日推荐/组合快照, 回填 T+1/T+5/T+20 收益, 统计评级胜率。

设计:
1. 每日从 scored json 中抽取"推荐标的"(S/A级)与"报告组合"(AI组合 + 量化组合), 打标签存档;
2. 后续运行(次日或手动)时, 用真实K线回填各标的相对推荐日的 T+N 收盘涨跌幅;
3. 汇总各评级 / 各组合的命中率与平均超额收益 → 输出 recommendation_track.json + 控制台摘要。

数据纪律: 收盘涨跌幅全部来自真实K线(腾讯前复权, 新浪兜底), 无K线则留空不编造。

用法:
  python3 track_recommendations.py                  # 用最新 scored json 记录 + 回填
  python3 track_recommendations.py <scored.json>    # 指定输入
  python3 track_recommendations.py --report         # 只打印已存档统计(不抓K线)
"""
import os
import sys
import json
import glob
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK_FILE = os.path.join(HERE, "recommendation_track.json")
SECTOR_DIR = os.path.join(HERE, "sector")

# 清除可能干扰的代理
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

TENCENT_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HORIZONS = [1, 5, 20]

# ===== P0-4 (2026-10-28) T+20 回填三条硬约束 =====
# 背景：T+20 在 2026-10-27 前全部为 None，跟踪表承诺「超额 vs 沪深300」却在该周期无数据，
#   读者只能看到「积累中」而不知道「什么时候会有」；更严重的是原实现有两个口径缺陷，
#   在 T+1/T+5 上被样本量掩盖，一旦 T+20 到期就会放大 20 倍：
#
#   缺陷① 用「个股自身有价格的日子」当交易日轴（dates = kl 的全部日期，j = idx + h）。
#          个股停牌期间 kl 缺该日 → j 直接跨过停牌区间，T+20 实际取到的是 T+21/T+23，
#          而不同股票错位天数不同 → 同一天同一 horizon 的样本不在同一时间窗，
#          均值/胜率不再可比。修法：改用**全市场交易日历**定位目标日（_build_calendar）。
#
#   缺陷② 基准价优先取 rec["pick_price"]（scored json 里当时的**未复权**原始价），
#          而终点价取自**最新前复权**序列。若期间发生除权除息，两个价格不在同一量尺：
#          实测 601600 于 2026-10-23 除息 0.28 元，pick_price 8.744 vs 前复权 8.464
#          （差 3.26%）→ 用 pick_price 当分母等于把分红当成亏损，系统性低估收益。
#          样本里 86/1493 条偏差 >0.01%、55 条 >0.2%、28 条 >1%。
#          修法：基准价一律取**前复权序列**在基准日的收盘价，pick_price 仅作留痕字段
#          （base_price_raw）用于核对，不参与计算。
#
#   缺陷③ 「未成熟」与「已回填但为 0」在数据结构上不可区分（都是 None）。
#          修法：每 horizon 写 status：ok / unmatured / no_data，并给出该 horizon
#          最早可回填日（_META.maturity），页面据此显式披露，而非留空让人误读为「无超额」。
# v2.44 P1-2: 基准指数(用于计算超额收益 alpha)。
# 交易者需要区分"选股能力"与"大盘 beta": 绝对收益 +1% 在上证 +0.05% 的日子里
# 是超额 +0.95%, 在大盘 +3% 的日子里则是跑输。基准取沪深300(sh000300)。
BENCH_SYMBOL = "sh000300"
BENCH_NAME = "沪深300"


def _sym(code):
    """归一化为腾讯/新浪 K线所需的符号格式 (sh600519 / sz300450 / bj430047)。

    兼容两种输入:
      - 带市场前缀: 'sz300450' / 'SH600519'  → 直接归一化前缀大小写后返回
      - 纯数字:    '300450' / '600519'      → 按首位数字推断市场
    历史 bug: scored json 的 code 已是 'sz300450' 形式, 旧实现对其 zfill 后
    用 startswith 判断数字前缀, 全部失配返回 None, 导致回填恒为 0 个收益点。
    """
    c = str(code).strip().lower()
    # 情况1: 已带前缀 (sh/sz/bj)
    if len(c) > 2 and c[:2] in ("sh", "sz", "bj") and c[2:].isdigit():
        return c
    c = str(code).strip().zfill(6)
    if c.startswith(('60', '68', '9', '51', '58')):
        return 'sh' + c
    if c.startswith(('00', '30', '20', '1', '15', '16')):
        return 'sz' + c
    if c.startswith(('8', '4', '92')):
        return 'bj' + c
    return None


def fetch_kline(code, need=60):
    """→ {date: close} 前复权日线 (腾讯多域名轮询 + 新浪兜底)

    数据纪律: 全部来自真实接口; 任一源失败则尝试下一个; 全失败返回 {} (留空不编造)。
    """
    sym = _sym(code)
    if not sym:
        return {}
    # --- 源1: 腾讯 (多域名轮询, ifzq.gtimg.cn 为准) ---
    for host in TENCENT_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode('utf-8')
            if txt.lstrip().startswith('<'):
                continue
            j = json.loads(txt)
            d = j.get('data', {}).get(sym, {})
            kl = d.get('qfqday') or d.get('day') or []
            out = {}
            for x in kl:
                if len(x) >= 3:
                    try:
                        out[str(x[0])] = float(x[2])
                    except (ValueError, TypeError):
                        continue
            if out:
                return out
        except Exception:
            continue
    # --- 源2: 新浪 (不复权, 最后手段) ---
    try:
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={max(need, 60)}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode('utf-8')
        arr = json.loads(txt)
        out = {}
        for x in (arr or []):
            if isinstance(x, dict) and x.get('day') and x.get('close') is not None:
                try:
                    out[str(x['day'])] = float(x['close'])
                except (ValueError, TypeError):
                    continue
        if out:
            return out
    except Exception:
        pass
    return {}


def load_track():
    if os.path.exists(TRACK_FILE):
        try:
            return json.load(open(TRACK_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"records": []}


def save_track(t):
    json.dump(t, open(TRACK_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _find_latest_scored(explicit=None):
    if explicit:
        return explicit
    fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
          if "_watch_" not in f and ".bak" not in f]
    return fs[0] if fs else None


def record_snapshot(scored_path, replace_day=False):
    """从 scored json 抽取当日推荐快照

    v2.45: 新增 replace_day —— 本地复盘链路专用。
      背景: GitHub Actions 于 15:30 先用「规则引擎兜底评级」记录一次当日快照;
            本地复盘(逐公司核查)会改变评级分布(实测 S:0→S:10), 再跑本脚本时
            按 (date,kind,code) 去重 → 只会「追加」新入选的标的, 而旧的那批
            (已被本地复盘的评级淘汰)仍留在表里 → 当日样本虚增、胜率被污染。
      故本地复盘时传 replace_day=True: 先删除当日全部快照, 再按最终评级重建,
      保证「当日推荐集 = 本地复盘后的最终结论」, 一个交易日只有一套推荐。
      注: 同日记录此时尚未回填 T+N(T+N 需未来K线), 故删除不损失已有收益数据。
    """
    data = json.load(open(scored_path, encoding="utf-8"))
    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
    results = data.get("results", [])
    name_by = {r["name"]: r for r in results}

    recs = []
    # 1) S/A 级个股(评级推荐)
    for r in results:
        if r.get("rating") in ("S", "A"):
            recs.append({
                "kind": "rating", "label": r["rating"], "name": r["name"], "code": r["code"],
                "base_price": r.get("price"), "total": r.get("total"),
            })
    # 2) AI 组合
    try:
        ai = json.load(open(os.path.join(HERE, "ai_assessment.json"), encoding="utf-8"))
        if date in str(ai.get("date", "")):
            for p in (ai.get("portfolio") or {}).get("picks", []):
                nm = p.get("name") if isinstance(p, dict) else p
                r = name_by.get(nm)
                if r:
                    recs.append({"kind": "ai_portfolio", "label": "AI组合", "name": nm,
                                 "code": r["code"], "base_price": r.get("price"), "total": r.get("total")})
    except Exception:
        pass
    # 3) 量化组合(A动量/B反转)
    try:
        qf = os.path.join(SECTOR_DIR, f"portfolio_{date}.json")
        if not os.path.exists(qf):
            qf = os.path.join(SECTOR_DIR, "portfolio_latest.json")
        if os.path.exists(qf):
            qj = json.load(open(qf, encoding="utf-8"))
            for mode, pf in (qj.get("portfolios") or {}).items():
                picks = pf.get("picks") if isinstance(pf, dict) else pf
                for p in (picks or []):
                    nm = p.get("name")
                    r = name_by.get(nm)
                    if r:
                        recs.append({"kind": f"quant_{mode}", "label": f"量化·{mode}", "name": nm,
                                     "code": r["code"], "base_price": r.get("price"), "total": r.get("total")})
    except Exception:
        pass

    track = load_track()
    # v2.45: 本地复盘 → 先清空当日快照再重建(避免同日双套推荐)
    replaced = 0
    if replace_day:
        _before = len(track["records"])
        _dropped = [x for x in track["records"] if x.get("date") == date]
        track["records"] = [x for x in track["records"] if x.get("date") != date]
        replaced = _before - len(track["records"])
        if replaced:
            _filled = sum(1 for x in _dropped
                          if any(v is not None for v in (x.get("rets") or {}).values()))
            print(f"♻️  replace-day: 移除当日旧快照 {replaced} 条"
                  f"（其中已回填收益 {_filled} 条）")
    # 去重: 同 date + kind + code 只记一次
    exist = {(x["date"], x["kind"], x["code"]) for x in track["records"]}
    added = 0
    for rc in recs:
        key = (date, rc["kind"], rc["code"])
        if key in exist:
            continue
        rc.update({"date": date, "pick_price": rc.get("base_price"),
                   "rets": {str(h): None for h in HORIZONS},
                   "excess": {str(h): None for h in HORIZONS}})
        track["records"].append(rc)
        added += 1
    save_track(track)
    _tag = f"（replace-day 替换 {replaced} 条）" if replace_day else ""
    print(f"📝 记录 {date} 推荐快照: 新增 {added} 条 (共 {len(track['records'])} 条){_tag}")
    return track


def _last_closed_trade_date():
    """返回「已收盘的最新交易日」(北京时间为准), 格式 YYYY-MM-DD。

    数据纪律要点: 腾讯/新浪 K 线在盘中会返回当日实时价(未收盘), 若直接用作 T+N
    收盘价会把盘中波动误当收盘收益。故:
      - 北京时间 15:00 前 → 当日未收盘, 截止日回退到前一日;
      - 再回退跳过周末(节假日无法在此精确判断, 由 K 线实际存在的日期兜底过滤)。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    bj = _dt.now(_tz.utc) + _td(hours=8)
    d = bj.date()
    if bj.hour < 15:
        d = d - _td(days=1)
    while d.weekday() >= 5:
        d = d - _td(days=1)
    return d.strftime('%Y-%m-%d')


def _close_of(bar):
    """从一根K线里取收盘价。兼容两种存储格式:
      - dict 形式 {date: close}            (fetch_kline 的返回值)
      - list 形式 {date: [open,close,high,low,vol]}  (data/klines_long.json 的格式)
    ⚠️ 曾把 list 直接当数字相减 → TypeError: unsupported operand type(s) for -: 'list' and 'float'。
       两种格式必须在此收口, 不许在调用处各自假设。
    """
    if isinstance(bar, (int, float)):
        return float(bar)
    if isinstance(bar, (list, tuple)) and len(bar) >= 2:
        c = bar[1]
        if isinstance(c, (int, float)):
            return float(c)
    return None


def fetch_kline_ohlcv(code, need=60):
    """→ {date: [open, close, high, low, vol]} 前复权日线 (腾讯, 与 klines_long 同格式)

    供调用方需要复权原始数组时使用; 只关心收盘价的场景用 fetch_kline / _close_of。
    """
    sym = _sym(code)
    if not sym:
        return {}
    for host in TENCENT_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode('utf-8')
            if txt.lstrip().startswith('<'):
                continue
            j = json.loads(txt)
            d = j.get('data', {}).get(sym, {})
            kl = d.get('qfqday') or d.get('day') or []
            out = {}
            for x in kl:
                if len(x) >= 3:
                    try:
                        out[str(x[0])] = [float(x[1]), float(x[2])]
                    except (ValueError, TypeError):
                        continue
            if out:
                return out
        except Exception:
            continue
    return {}


def _build_calendar(kl_cache, min_ratio=0.5):
    """→ 全市场交易日历 [date, ...] 升序。

    判据: 某日期在 >= min_ratio 的个股K线里出现 → 视为交易日。
    ⚠️ 不用「个股自身日期序列」当轴（那是原实现的缺陷①）：停牌股会让 horizon 错位。
       用横截面出现率做判据，单只停牌不影响日历；周末/节假日几乎所有个股都缺失
       → 自然被排除，无需另建节假日表。
    """
    from collections import Counter
    cnt = Counter()
    for kl in kl_cache.values():
        for d in kl:
            cnt[d] += 1
    if not cnt:
        return []
    n = len(kl_cache)
    return sorted(d for d, c in cnt.items() if c >= n * min_ratio)


def _maturity_map(calendar, cutoff, first_pick_date=None):
    """→ {horizon: {"earliest": "YYYY-MM-DD", "projected": bool}}

    定义: T+h 的「最早可回填日」= **最早的推荐日** + h 个交易日。
    作用: 当前未成熟的 horizon 也要给出具体日期，页面告诉用户「什么时候会有值」，
          而不是永远显示「积累中」。

    ⚠️ 两个必须在注释里锁死的边界：
      ① 必须从**最早推荐日**起算，不能从日历起点起算 —— 首版取 calendar[h] 得到
         「T+20 最早可回填 2025-08-26」（日历第 20 天），比最早推荐日早了 13 个月。
         日历覆盖 301 天是因为建面板时回补了历史行情，与推荐时序无关。
      ② 目标日超出已知日历时**按工作日向前推算**，并标 projected=True。
         项目里没有节假日表，节假日会让推算日偏早（真实成熟日更晚几天），
         故对外必须写「预估」而不是当作确定事实。
    """
    from datetime import datetime as _dt, timedelta as _td
    cal = [d for d in calendar if d <= cutoff]
    if not cal:
        return {h: {"earliest": None, "projected": False} for h in HORIZONS}
    start = first_pick_date or cal[0]
    bi = next((i for i, d in enumerate(cal) if d >= start), 0)
    out = {}
    for h in HORIZONS:
        j = bi + h
        if j < len(cal):
            out[h] = {"earliest": cal[j], "projected": False}
            continue
        # 超出已知日历：从最后一个已知交易日起按工作日推算
        need = j - (len(cal) - 1)
        d = _dt.strptime(cal[-1], "%Y-%m-%d").date()
        got = 0
        guard = 0
        while got < need and guard < 400:
            d += _td(days=1)
            guard += 1
            if d.weekday() < 5:      # 仅跳过周末；节假日无数据源，已知会偏早
                got += 1
        out[h] = {"earliest": d.strftime("%Y-%m-%d"), "projected": True}
    return out



def backfill(track, local_kl_path=None, use_local=True):
    """用真实K线回填 T+N 收益 + 超额收益 alpha (仅使用已收盘交易日数据)

    v2.44 P1-2: 超额收益 = 个股 T+N 涨跌幅 − 基准(沪深300)同期涨跌幅。
    P0-4 (2026-10-28): 三条口径修复，详见文件头 HORIZONS 下方注释。
      - 交易日轴改用全市场日历（停牌不再让 horizon 错位）
      - 基准价改用前复权收盘（除权除息不再被当成亏损）
      - 每 horizon 写 status: ok/unmatured/no_data + 记录最早可回填日

    参数:
      local_kl_path: 本地K线缓存路径（默认 data/klines_long.json）。
        本地缓存覆盖全市场且已前复权，优先使用可大幅减少联网抓取；
        缓存里没有的 code 才回落联网抓取。
      use_local: False 时强制全部联网（调试用）。
    """
    # ---- 1. 股票池 ----
    codes = {x["code"] for x in track["records"]}
    kl_cache = {}
    cutoff = _last_closed_trade_date()
    print(f"📈 回填收益: 涉及 {len(codes)} 只个股 (数据截止 {cutoff}, 盘中不回填当日) ...")

    # ---- 2. 优先读本地缓存（全市场前复权，一次 IO 替代 N 次 HTTP）----
    local_raw = {}
    if use_local:
        lp = local_kl_path or os.path.join(HERE, "data", "klines_long.json")
        if os.path.exists(lp):
            try:
                with open(lp, encoding="utf-8") as f:
                    local_raw = json.load(f)
                print(f"   📦 本地缓存 {os.path.basename(lp)}: {len(local_raw)} 只")
            except Exception as _e:
                print(f"   ⚠️ 本地缓存读取失败({_e})，回落联网抓取")
                local_raw = {}
    for c in codes:
        c6 = str(c)[-6:]
        if local_raw.get(c6):
            kl_cache[c] = local_raw[c6]
    n_local = len(kl_cache)
    n_need = len(codes) - n_local
    if n_need:
        print(f"   🌐 本地未覆盖 {n_need} 只，联网抓取 ...")
    for i, c in enumerate(sorted(codes - set(kl_cache)), 1):
        kl_cache[c] = fetch_kline_ohlcv(c)
        if i % 20 == 0:
            print(f"      ... {i}/{n_need}")
        time.sleep(0.05)
    print(f"   ✅ K线就绪 {sum(1 for v in kl_cache.values() if v)}/{len(codes)} 只"
          f"（本地 {n_local}）")

    # ---- 3. 交易日历（全市场横截面口径，替代个股自身日期轴）----
    calendar = _build_calendar(kl_cache)
    cal_cut = [d for d in calendar if d <= cutoff]
    if not cal_cut:
        print("   ⚠️ 无法构建交易日历，本次不回填")
        return track
    _pick_dates = sorted({x["date"] for x in track["records"] if x.get("date")})
    maturity = _maturity_map(calendar, cutoff, _pick_dates[0] if _pick_dates else None)
    print(f"   📅 交易日历 {len(cal_cut)} 天（{cal_cut[0]} ~ {cal_cut[-1]}）")
    _mat_txt = " / ".join(
        f"T+{h}@{maturity[h]['earliest']}{'(预估)' if maturity[h]['projected'] else ''}"
        for h in HORIZONS)
    print(f"   ⏳ 最早可回填日: {_mat_txt}")

    # ---- 4. 基准指数（沪深300）----
    bench_kl = {}
    bl = local_raw.get(BENCH_SYMBOL) or local_raw.get(BENCH_SYMBOL[-6:])
    if bl:
        bench_kl = bl
        print(f"📈 基准 {BENCH_NAME}({BENCH_SYMBOL}): 取自本地缓存 {len(bl)} 根K线")
    else:
        bench_kl = fetch_kline_ohlcv(BENCH_SYMBOL)
        print(f"📈 基准 {BENCH_NAME}({BENCH_SYMBOL}): "
              f"{'已获取 ' + str(len(bench_kl)) + ' 根K线' if bench_kl else '获取失败, 超额收益将留空(不编造)'}")
    # 基准也走同一日历：基准停牌（极少）时用日历定位后向前找最近有效收盘
    bench_close = {d: _close_of(v) for d, v in (bench_kl or {}).items()}

    def _bench_on(d):
        """取基准在 d 当天的收盘；缺失则取 d 之前最近一个有效收盘（不向未来借）。"""
        if d in bench_close and bench_close[d]:
            return bench_close[d]
        for x in reversed(cal_cut):
            if x <= d and bench_close.get(x):
                return bench_close[x]
        return None

    # ---- 5. 逐条回填 ----
    filled = 0
    filled_ex = 0
    n_unmat = 0
    n_nodata = 0
    for rec in track["records"]:
        kl = kl_cache.get(rec["code"]) or {}
        rec.setdefault("rets", {})
        rec.setdefault("excess", {})
        rec.setdefault("status", {})
        base_d = rec["date"]

        # 基准日 = 日历上 >= 推荐日 的首个交易日
        bi = next((i for i, d in enumerate(cal_cut) if d >= base_d), None)
        if bi is None:
            # 推荐日晚于数据截止（理论上不该发生）→ 全部标 unmatured
            for h in HORIZONS:
                rec["rets"].setdefault(str(h), None)
                rec["excess"].setdefault(str(h), None)
                rec["status"][str(h)] = "unmatured"
            n_unmat += 1
            continue
        base_day = cal_cut[bi]
        rec["base_trade_date"] = base_day

        # 基准价：一律前复权收盘（缺陷②修复）
        #   原始 pick_price 仅留痕，不参与计算；两者都缺才算 no_data
        base_close = _close_of(kl.get(base_day)) if kl else None
        rec["base_price_raw"] = rec.get("pick_price")   # 留痕：scored json 的原始未复权价
        if base_close:
            rec["base_price_qfq"] = round(base_close, 4)

        b0 = _bench_on(base_day)

        for h in HORIZONS:
            key = str(h)
            tj = bi + h
            if tj >= len(cal_cut):
                # 未成熟：保留旧值（数据源尾部抖动不清空），并标记状态
                rec["rets"].setdefault(key, None)
                rec["excess"].setdefault(key, None)
                rec["status"][key] = "unmatured"
                continue
            target_day = cal_cut[tj]
            c1 = _close_of(kl.get(target_day)) if kl else None
            if not base_close or c1 is None:
                # 基准价或目标日价格缺失（停牌/退市/缓存缺）→ 不编造
                rec["rets"].setdefault(key, None)
                rec["excess"].setdefault(key, None)
                rec["status"][key] = "no_data"
                n_nodata += 1
                continue
            ret = (c1 - base_close) / base_close * 100
            rec["rets"][key] = round(ret, 2)
            rec["status"][key] = "ok"
            filled += 1
            # 超额 = 个股收益 − 基准同期（基准日 → 目标日，与个股同一日历窗）
            b1 = _bench_on(target_day)
            if b0 and b1 and b0 != 0:
                rec["excess"][key] = round(ret - (b1 - b0) / b0 * 100, 2)
                filled_ex += 1
            else:
                rec["excess"].setdefault(key, None)

    # ---- 6. 元信息：让「未成熟」在数据层就可查，页面不必猜 ----
    #   三个状态必须分开，否则页面无法区分「等时间」与「行情没更新」：
    #     earliest_backfill  理论成熟日（推荐日 + h 个交易日）
    #     earliest_projected 该日期是否为工作日推算（未扣节假日，会偏早）
    #     stale              成熟日已过但仍无值 → 归因为行情未更新，不是逻辑缺陷
    _today = datetime.now().strftime("%Y-%m-%d")
    _stale = {}
    for h in HORIZONS:
        _d = maturity[h]["earliest"]
        _n_ok_h = sum(1 for x in track["records"]
                      if isinstance((x.get("rets") or {}).get(str(h)), (int, float)))
        _stale[str(h)] = bool(_d and _d < _today and _n_ok_h == 0)
    track["_meta"] = {
        "backfilled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_cutoff": cutoff,
        "calendar": {"n_days": len(cal_cut), "start": cal_cut[0], "end": cal_cut[-1]},
        "horizons": [str(h) for h in HORIZONS],
        "earliest_backfill": {str(h): maturity[h]["earliest"] for h in HORIZONS},
        "earliest_projected": {str(h): maturity[h]["projected"] for h in HORIZONS},
        "stale": _stale,
        "basis": "个股与基准均为前复权收盘价；交易日轴=全市场交易日历",
        "bench": BENCH_SYMBOL,
    }

    save_track(track)
    print(f"✅ 回填完成: {filled} 个收益点, {filled_ex} 个超额点 "
          f"（未成熟 {n_unmat} 条记录, 价格缺失 {n_nodata} 个点）")
    return track



def summarize(track):
    """输出统计: 各评级/组合的命中率与平均收益"""
    from collections import defaultdict
    agg = defaultdict(lambda: {str(h): [] for h in HORIZONS})
    agg_ex = defaultdict(lambda: {str(h): [] for h in HORIZONS})
    for rec in track["records"]:
        for h in HORIZONS:
            v = (rec.get("rets") or {}).get(str(h))
            if v is not None:
                agg[rec["kind"]][str(h)].append(v)
                agg["__ALL__"][str(h)].append(v)
            e = (rec.get("excess") or {}).get(str(h))
            if e is not None:
                agg_ex[rec["kind"]][str(h)].append(e)
                agg_ex["__ALL__"][str(h)].append(e)

    # P0-4: 未成熟状态与最早可回填日必须显式打印，不能只留「—」
    meta = track.get("_meta") or {}
    ear = meta.get("earliest_backfill") or {}
    stat_cnt = defaultdict(int)
    for rec in track["records"]:
        for h in HORIZONS:
            stat_cnt[(str(h), (rec.get("status") or {}).get(str(h)) or "?")] += 1

    lines = ["", "=" * 78, "📊 推荐跟踪统计 (T+N 收盘涨跌幅, 真实K线, 个股与基准均前复权)", "=" * 78]
    if meta:
        lines.append(f"数据截止 {meta.get('data_cutoff', '—')} · 交易日历 "
                     f"{(meta.get('calendar') or {}).get('n_days', '—')} 天 · "
                     f"基准 {meta.get('bench', '—')}")
    lines.append(f"{'分组':<16}{'样本':>5}{'T+1均':>9}{'T+5均':>9}{'T+20均':>9}"
                 f"{'T+5胜率':>9}{'T+1超额':>9}{'T+5超额':>9}")
    lines.append("-" * 78)
    order = ["__ALL__", "rating", "ai_portfolio", "quant_momentum", "quant_reversal"]
    kinds = order + [k for k in agg if k not in order]
    for k in kinds:
        if k not in agg:
            continue
        d = agg[k]
        e = agg_ex[k]
        if not d["1"] and not d["5"] and not d["20"]:
            continue
        avg = lambda h: (sum(d[h]) / len(d[h])) if d[h] else float('nan')
        avg_e = lambda h: (sum(e[h]) / len(e[h])) if e[h] else float('nan')
        n5 = len(d["5"])
        win = (sum(1 for x in d["5"] if x > 0) / n5 * 100) if n5 else float('nan')
        lbl = "全部推荐" if k == "__ALL__" else ("评级S/A" if k == "rating" else k)
        lines.append(f"{lbl:<16}{len(d['1']):>5}{avg('1'):>8.2f}%{avg('5'):>8.2f}%"
                     f"{avg('20'):>8.2f}%{win:>8.1f}%{avg_e('1'):>8.2f}%{avg_e('5'):>8.2f}%")

    # 未成熟披露
    lines.append("-" * 78)
    ear = meta.get("earliest_backfill") or {}
    proj = meta.get("earliest_projected") or {}
    for h in HORIZONS:
        n_ok = stat_cnt[(str(h), "ok")]
        n_un = stat_cnt[(str(h), "unmatured")]
        n_nd = stat_cnt[(str(h), "no_data")]
        _m = ear.get(str(h))
        if _m:
            _tag = "（预估，未扣节假日）" if proj.get(str(h)) else ""
            tail = f"，最早批次可回填 {_m}{_tag}" if n_ok == 0 else f"，最早批次 {_m}"
        else:
            tail = ""
        lines.append(f"T+{h:<3} ok={n_ok:<5} 未成熟={n_un:<5} 价格缺失={n_nd:<5}{tail}")
    lines.append("=" * 78)
    print("\n".join(lines))




def main():
    args = sys.argv[1:]
    report_only = "--report" in args
    replace_day = "--replace-day" in args
    no_local = "--no-local" in args
    # --no-new-snapshot: 只回填不新增快照。
    #   用途①：T+N 到期靠时间推进，周末/无当日 scored 时也要能更新；
    #   用途②：验证回填口径时避免改动样本集（曾因此误把 1493 条变 1777 条，
    #          而 scored 文件名 2026-10-27 内部 date 实为 2026-10-26 → 多记一批）。
    no_snap = "--no-new-snapshot" in args
    explicit = next((a for a in args if not a.startswith("--")), None)

    track = load_track()
    if not report_only:
        sp = None if no_snap else _find_latest_scored(explicit)
        if sp:
            track = record_snapshot(sp, replace_day=replace_day)
        track = backfill(track, use_local=not no_local)
    summarize(track)



if __name__ == "__main__":
    main()
