#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_splits.py — 核对映射 ETF 的"疑似份额折算"事件到底是拆分还是真实申购

判定原理：
  折算因子 = 累计净值比 ÷ 单位净值比
    - 拆分/合并：单位净值被"人为"压低/抬高，累计净值不变 → 因子 = 2 / 3 / 0.1 …
    - 真实大额申购：单位净值与累计净值同步变动 → 因子 ≈ 1.0000
  再用"单位净值比 × 份额倍率 ÷ 累计净值比 ≈ 1"做交叉验证。

用法：
  python src/check_splits.py            # 只打印核对结果与可粘贴的 CSV 行
  python src/check_splits.py --append   # 确认是折算的，直接追加到 src/splits.csv
"""
import argparse
import os
import sys
import time

# 与 collect.py 一致：默认删代理直连
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

import build_report as B

TOL = 0.03  # 单位净值比与理论值的允许偏差


def fetch_nav(code, d, back=20, fwd=5):
    """取事件日前后净值，返回 (事件日单位净值比, 累计净值比, 事件日, Note)"""
    import akshare as ak
    start = (pd.Timestamp(d) - pd.Timedelta(days=back)).strftime("%Y%m%d")
    end = (pd.Timestamp(d) + pd.Timedelta(days=fwd)).strftime("%Y%m%d")
    df = ak.fund_etf_fund_info_em(fund=code, start_date=start, end_date=end)
    if df is None or df.empty:
        return None
    df["净值日期"] = pd.to_datetime(df["净值日期"])
    df = df.sort_values("净值日期").reset_index(drop=True)
    cur = df[df["净值日期"] == pd.Timestamp(d)]
    prev = df[df["净值日期"] < pd.Timestamp(d)]
    if cur.empty or prev.empty:
        return None
    cur, prev = cur.iloc[0], prev.iloc[-1]
    try:
        unit_ratio = float(cur["单位净值"]) / float(prev["单位净值"])
        cum_ratio = float(cur["累计净值"]) / float(prev["累计净值"])
    except (ZeroDivisionError, ValueError, KeyError):
        return None
    note = "单位净值%.4f→%.4f，累计净值%.4f→%.4f" % (
        prev["单位净值"], cur["单位净值"], prev["累计净值"], cur["累计净值"])
    return unit_ratio, cum_ratio, note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true", help="把判定为折算的事件写入 src/splits.csv")
    args = ap.parse_args()

    df = B.load_share_df()
    emap = pd.read_csv(B.MAP_CSV, dtype=str)
    emap["code"] = emap["code"].str.zfill(6)
    watch = set(emap["code"])
    splits, flows = B.load_splits()
    known = {c: set(d) for c, d in splits.items()}
    for c, d in flows.items():
        known.setdefault(c, set()).update(d)

    # 只扫映射标的的候选事件
    cands = []
    for code, g in df.sort_values(["code", "trade_date"]).groupby("code"):
        if code not in watch or len(g) < 3:
            continue
        ratio = (g["share"] / g["share"].shift(1)).to_numpy()
        dates = g["trade_date"].to_numpy()
        seen = known.get(code, set())
        for k in range(1, len(g)):
            r = ratio[k]
            if r > B.SPLIT_HI or r < B.SPLIT_LO:
                d = pd.Timestamp(dates[k]).strftime("%Y-%m-%d")
                if d not in seen:
                    cands.append((code, d, r, g["name"].iloc[0] if "name" in g else ""))

    if not cands:
        print("映射标的无未登记候选事件，口径正常。")
        return

    print("待核对候选事件 %d 起，开始拉取净值...\n" % len(cands))
    rows = []
    for code, d, r, name in cands:
        nav = None
        for attempt in range(3):
            try:
                nav = fetch_nav(code, d)
                break
            except Exception as e:  # 网络/接口异常
                if attempt < 2:
                    time.sleep(2)
                else:
                    print("%s %s 净值获取失败: %s" % (code, d, str(e)[:80]))
        time.sleep(0.3)
        if nav is None:
            rows.append((code, name, d, r, None, None, None, "净值缺失，无法判定", None, ""))
            continue
        unit_ratio, cum_ratio, note = nav
        # 折算因子 = 累计净值比 ÷ 单位净值比：真实申购 ≈1，拆分/合并 = 拆分倍数
        split_factor = cum_ratio / unit_ratio
        score = unit_ratio * r / cum_ratio  # 交叉验证项，便于人工复核
        verdict = "折算" if abs(split_factor - 1) > TOL else "真实申购"
        rows.append((code, name, d, r, unit_ratio, cum_ratio, split_factor,
                     "%s（因子 %.4f，判据值 %.4f）" % (verdict, split_factor, score), score, note))

    print("%-8s %-14s %-12s %9s %9s %9s %9s  %s"
          % ("代码", "名称", "日期", "份额倍率", "单位净值比", "累计净值比", "折算因子", "判定"))
    print("-" * 110)
    for row in rows:
        code, name, d, r, ur, cr, tf, verdict = row[:8]
        print("%-8s %-14s %-12s %9.4f %9s %9s %9s  %s"
              % (code, str(name)[:12], d, r,
                 ("%.4f" % ur) if ur else "-", ("%.4f" % cr) if cr else "-",
                 ("%.4f" % tf) if tf else "-", verdict))

    confirmed = [r for r in rows if r[7].startswith("折算")]
    rejected = [r for r in rows if r[7].startswith("真实申购")]
    if confirmed:
        print("\n确认是折算的 %d 起（因子取净值推导值，保留折算日的真实申赎）：" % len(confirmed))
        for r in confirmed:
            print("  %s,%s,%.4f,折算,%s" % (r[0], r[2], r[6], r[9]))
    if rejected:
        print("\n确认是真实申购的 %d 起（不复权，仅登记以免重复告警）：" % len(rejected))
        for r in rejected:
            print("  %s,%s,%.4f,申购,%s" % (r[0], r[2], r[6], r[9]))
    if not confirmed and not rejected:
        print("\n没有可判定的候选事件。")
        return

    if args.append:
        add = [(r[0], r[2], r[6], "折算" if r[7].startswith("折算") else "申购", r[9])
               for r in confirmed + rejected]
        old = pd.read_csv(B.SPLITS_CSV, dtype=str) if os.path.exists(B.SPLITS_CSV) else pd.DataFrame()
        have = set()
        if not old.empty:
            for _, r in old.iterrows():
                have.add((str(r["code"]).zfill(6), str(r["date"])))
        add = [a for a in add if (str(a[0]).zfill(6), str(a[1])) not in have]
        if not add:
            print("\n无需追加（都已在 src/splits.csv 中）。")
            return
        cols = ["code", "date", "ratio", "verdict", "note"]
        if old.empty:
            new = pd.DataFrame(add, columns=cols)
        else:
            # 旧行补 verdict 列（默认折算），再追加新行
            if "verdict" not in old.columns:
                old["verdict"] = "折算"
            new = pd.concat([old[cols], pd.DataFrame(add, columns=cols)], ignore_index=True)
        new.to_csv(B.SPLITS_CSV, index=False, encoding="utf-8")
        print("\n已追加 %d 行到 %s" % (len(add), B.SPLITS_CSV))
        print("重新运行 python src/build_report.py 即生效。")


if __name__ == "__main__":
    main()
