"""测历史市值批量获取的可行性与速率。"""
import time

import akshare as ak
import pandas as pd


def test_baidu_batch():
    print("=== 百度总市值批量速率 ===")
    codes = ["000001", "000002", "600519", "300750", "688981", "002594",
             "601318", "000858", "600036", "002415"]
    t0 = time.time()
    ok, fail = 0, 0
    for s in codes:
        try:
            df = ak.stock_zh_valuation_baidu(symbol=s, indicator="总市值", period="全部")
            if df is not None and len(df):
                ok += 1
        except Exception:
            fail += 1
    el = time.time() - t0
    print(f"  ok={ok} fail={fail}  耗时 {el:.1f}s → {el/len(codes):.2f}s/只")
    print(f"  全A 5927 只预估 {5927*el/len(codes)/60:.0f} 分钟")


def test_baidu_range():
    print("\n=== 百度总市值区间是否覆盖 2020-01 ===")
    df = ak.stock_zh_valuation_baidu(symbol="600519", indicator="总市值", period="全部")
    print(f"  列: {list(df.columns)}")
    print(f"  行数 {len(df)}  {df.iloc[0,0]} ~ {df.iloc[-1,0]}")
    d20 = df[df.iloc[:, 0].astype(str) >= "2020-01-01"]
    print(f"  2020-01 起行数 {len(d20)}")
    print(f"  样例: {d20.head(2).to_dict('records')}")


def test_derive_mv():
    """验证 total_mv 与 close×shares 的一致性。"""
    print("\n=== 交叉验证：baidu 总市值 vs close×shares ===")
    df = ak.stock_zh_valuation_baidu(symbol="600519", indicator="总市值", period="全部")
    df = df[df.iloc[:, 0].astype(str) >= "2026-10-25"]
    kh = ak.stock_zh_a_hist(symbol="600519", period="daily",
                            start_date="20261025", end_date="20261104", adjust="")
    print("  baidu 近几日市值(亿):", [(str(r[0]), round(r[1] / 1e8, 1)) for _, r in df.iterrows()])
    print("  未复权收盘:", [(str(r["日期"]), r["收盘"]) for _, r in kh.iterrows()][-3:])


if __name__ == "__main__":
    test_baidu_range()
    test_derive_mv()
    test_baidu_batch()
