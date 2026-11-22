#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_publish.py — 实测沪深两所 ETF 份额「当日数据」的发布时点

交易所从未公布过时刻表（依据见 src/collect.py 文件头），只能轮询实测。
本脚本在时间窗内定时探测两所接口，记录各自首次达到基准量的北京时间。

基准量：取最近一个有数据的 parquet，按各所标的数的 90% 作为「已全量发布」的下限。

用法：
  python src/tools/probe_publish.py --once                    # 立即探一次（验证脚本/接口）
  python src/tools/probe_publish.py --date 20261123           # 默认 21:50~23:55，每 5 分钟
  python src/tools/probe_publish.py --date 20261123 --every 3 --to 23:59
  python src/tools/probe_publish.py --date 20261120 --once    # 已知交易日，验证取数正常

输出：终端逐条日志 + data/meta/publish_probe_YYYYMMDD.json
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
import collect as C

OUT_DIR = os.path.join(C.DATA_DIR, "probe")   # 诊断产物，已在 .gitignore 排除


def baseline():
    """各所「已全量发布」的下限：最近 parquet 的标的数 × 0.9。"""
    import pandas as pd

    files = sorted(glob.glob(os.path.join(C.DATA_DIR, "daily", "share_*.parquet")))
    if not files:
        return {"sse": 800, "szse": 600}, "固定下限（无历史 parquet）"
    vc = pd.read_parquet(files[-1], columns=["source"])["source"].value_counts().to_dict()
    th = {"sse": int(vc.get("sse", 0) * 0.9), "szse": int(vc.get("szse", 0) * 0.9)}
    if th["sse"] <= 0 or th["szse"] <= 0:
        return {"sse": 800, "szse": 600}, "固定下限（历史 parquet 缺 source）"
    return th, os.path.basename(files[-1]) + " 的 90%"


def probe_once(date8):
    rec = {"ts": C.now_bj().strftime("%Y-%m-%d %H:%M:%S")}
    for src in ("sse", "szse"):
        t0 = time.time()
        rec[src + "_err"] = None
        try:
            df = C.fetch_sse(date8) if src == "sse" else C.fetch_szse(date8, date8)
            rec[src] = 0 if df is None else len(df)
            rec[src + "_state"] = "ok" if rec[src] else "empty"
        except KeyError:
            # 上交所无该日数据时，akshare 内部按列名取值对不上，抛 KeyError 而非空表
            # （实测 2026-11-22 周日：KeyError；2026-11-20：正常 927 行）。
            # 这是「尚未发布」的信号，不是接口故障，不能算 error。
            rec[src] = 0
            rec[src + "_state"] = "unpublished"
        except Exception as e:
            rec[src] = 0
            rec[src + "_state"] = "error"
            rec[src + "_err"] = "%s: %s" % (type(e).__name__, str(e)[:100])
        rec[src + "_sec"] = round(time.time() - t0, 1)
    return rec


def _tag(rec, src, th):
    st = rec.get(src + "_state", "")
    if st == "unpublished":
        return "未发布"
    if st == "error":
        return "错误"
    if st == "empty":
        return "空表"
    return "OK" if rec[src] >= th[src] else "未全量"


def line(rec, th):
    return "%s | 上交所 %4d（%s）| 深交所 %4d（%s）| 耗时 %.0fs" % (
        rec["ts"][11:],
        rec["sse"], _tag(rec, "sse", th),
        rec["szse"], _tag(rec, "szse", th),
        rec["sse_sec"] + rec["szse_sec"],
    )


def save(date8, records, th):
    """按时间戳合并写入，多次 --once 探测（不同 automation）可累积到同一文件。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "publish_probe_%s.json" % date8)

    old = []
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8")).get("records", [])
        except Exception:
            old = []
    seen = {r["ts"] for r in old}
    merged = old + [r for r in records if r["ts"] not in seen]
    merged.sort(key=lambda r: r["ts"])

    first = {}
    for r in merged:
        for s in ("sse", "szse"):
            if s not in first and r.get(s, 0) >= th[s]:
                first[s] = r["ts"]
    obj = {
        "target_date": date8,
        "baseline": th,
        "first_full": first,
        "records": merged,
        "updated_at": C.now_bj().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="目标交易日 YYYYMMDD，默认北京今天")
    ap.add_argument("--from", dest="t_from", default="21:50")
    ap.add_argument("--to", dest="t_to", default="23:55")
    ap.add_argument("--every", type=int, default=5, help="探测间隔（分钟）")
    ap.add_argument("--once", action="store_true", help="只探一次")
    a = ap.parse_args()

    date8 = a.date or C.today_bj()
    th, th_src = baseline()
    print("目标交易日 %s | 基准 %s（%s）" % (date8, th, th_src), flush=True)

    records = []

    if a.once:
        rec = probe_once(date8)
        records.append(rec)
        if rec["sse_err"]:
            print("  上交所错误: %s" % rec["sse_err"], flush=True)
        if rec["szse_err"]:
            print("  深交所错误: %s" % rec["szse_err"], flush=True)
        print(line(rec, th), flush=True)
        print("已写入 %s" % save(date8, records, th), flush=True)
        return 0

    now = C.now_bj()

    def at(hhmm):
        h, m = [int(x) for x in hhmm.split(":")]
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t < now - dt.timedelta(hours=12):
            t += dt.timedelta(days=1)
        return t

    t_end = at(a.t_to)
    print("探测窗口 %s ~ %s，每 %d 分钟一次" % (a.t_from, a.t_to, a.every), flush=True)

    while True:
        if C.now_bj() > t_end:
            print("已到窗口结束时间，停止探测", flush=True)
            break
        rec = probe_once(date8)
        records.append(rec)
        if rec["sse_err"]:
            print("  上交所错误: %s" % rec["sse_err"], flush=True)
        if rec["szse_err"]:
            print("  深交所错误: %s" % rec["szse_err"], flush=True)
        print(line(rec, th), flush=True)
        save(date8, records, th)
        if rec["sse"] >= th["sse"] and rec["szse"] >= th["szse"]:
            print("两所均已全量，提前结束", flush=True)
            break
        time.sleep(a.every * 60)

    p = save(date8, records, th)
    obj = json.load(open(p, encoding="utf-8"))
    print("写入 %s" % p, flush=True)
    print("首次全量：上交所 %s / 深交所 %s" % (
        obj["first_full"].get("sse", "未出现"), obj["first_full"].get("szse", "未出现")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
