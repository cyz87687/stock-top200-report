"""验证：用日线「换手率」反推流通股本 → 流通市值。"""
import akshare as ak


def test_derive():
    sym = "600519"
    df = ak.stock_zh_a_hist(symbol=sym, period="daily",
                            start_date="20260101", end_date="20261104", adjust="")
    df["流通股本_推算"] = df["成交量"] * 100 / (df["换手率"] / 100.0)
    df["流通市值_推算_亿"] = df["流通股本_推算"] * df["收盘"] / 1e8
    print(f"=== {sym} 换手率反推流通股本 ===")
    print(df[["日期", "收盘", "成交量", "换手率", "流通股本_推算", "流通市值_推算_亿"]]
          .head(6).to_string(index=False))
    print()
    s = df["流通股本_推算"]
    print(f"流通股本 变异系数 = {s.std()/s.mean()*100:.4f}%  （应≈0，股本未变）")
    print(f"推断流通股本 ≈ {s.median()/1e8:.2f} 亿股")


def test_cross_check():
    """与百度总市值交叉验证（茅台流通≈总股本）。"""
    print("\n=== 与百度总市值交叉验证 ===")
    df = ak.stock_zh_a_hist(symbol="600519", period="daily",
                            start_date="20260101", end_date="20261104", adjust="")
    df["mv"] = df["成交量"] * 100 / (df["换手率"] / 100.0) * df["收盘"] / 1e8
    bd = ak.stock_zh_valuation_baidu(symbol="600519", indicator="总市值", period="全部")
    bd["date"] = bd["date"].astype(str)
    bd = bd[(bd["date"] >= "2026-01-01") & (bd["date"] <= "2026-11-04")]
    m = {}
    for _, r in df.iterrows():
        m[str(r["日期"])] = r["mv"]
    for _, r in bd.head(8).iterrows():
        d = r["date"]
        ours = m.get(d)
        if ours:
            print(f"  {d}  反推 {ours:8.1f} 亿  百度 {r['value']:8.1f} 亿  "
                  f"偏差 {(ours/r['value']-1)*100:+.2f}%")


def test_small_cap():
    print("\n=== 小盘股检验（股本变动是否影响） ===")
    for sym in ("002415", "300750"):
        df = ak.stock_zh_a_hist(symbol=sym, period="daily",
                                start_date="20200101", end_date="20261104", adjust="")
        df["sh"] = df["成交量"] * 100 / (df["换手率"] / 100.0)
        s = df["sh"]
        print(f"  {sym}: {len(df)} 行  股本中位 {s.median()/1e8:.2f} 亿股  "
              f"变异系数 {s.std()/s.mean()*100:.2f}%")


if __name__ == "__main__":
    test_derive()
    test_cross_check()
    test_small_cap()
