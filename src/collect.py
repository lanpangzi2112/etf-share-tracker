#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — 每交易日采集全市场 ETF 份额，落 data/share_YYYY.parquet

数据源（均免费、无需 key）：
  1. 上交所  ak.fund_etf_scale_sse(date)          官方份额，支持历史日期
  2. 深交所  ak.fund_scale_daily_szse(start, end) 官方日频份额
  3. 东财    ak.fund_etf_spot_em()                 全市场快照（含"最新份额"），兜底

用法：
  python collect.py                               # 采集"上一交易日"（不采当天：交易所当日份额晚间才陆续
                                                  #   发布——上交所约 22:20、深交所约 23:20，晚间跑当天必然不全）
                                                  #   运行前先扫描并优先补采最近缺失的交易日，最多 3 个
  python collect.py --date 20261016               # 指定日期
  python collect.py --backfill 20260701:20261016  # 区间回填
  python collect.py --diag                        # 诊断模式：打印各源列名与样本
"""
import argparse
import datetime as dt
import os
import sys
import time

import pandas as pd

# 默认禁代理直连（交易所站点直连最稳；GitHub Actions 本就没有代理）。
# 如你的网络必须走代理才能访问外网，加 --use-proxy
_PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_ORIG_PROXY = {k: os.environ.get(k) for k in _PROXY_KEYS}
for _k in _PROXY_KEYS:
    os.environ.pop(_k, None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")


def log(msg):
    print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ---------------- 交易日 ----------------
def load_trade_dates():
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    # 注意：trade_date 列是 datetime，必须格式化成 YYYYMMDD 字符串
    return sorted(set(pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")))


def prev_trade_day(dates, day):
    prior = [d for d in dates if d < day]
    return prior[-1] if prior else None


def existing_data_days():
    """data/daily 里已入库的日期（YYYYMMDD 字符串集合）"""
    import glob as _glob
    out = set()
    for p in _glob.glob(os.path.join(DAILY_DIR, "share_*.parquet")):
        b = os.path.basename(p)[len("share_"):-len(".parquet")]
        if len(b) == 8 and b.isdigit():
            out.add(b)
    return out


def missing_trade_days(tds, max_back=30, exclude=(), cutoff=None):
    """截止日（含）之前最近 max_back 个交易日里，数据文件缺失的日期（升序）。
    用于优先补缺：防止 T 日数据当晚未发布、而 T+1 日先发布时，
    旧回退逻辑只认上一交易日、把 T 日永久跳过。
    注意：akshare 交易日历包含未来日期，必须按 cutoff 截断，
    否则扫描窗口会被未来日期占满，永远摸不到真正的历史缺口。"""
    if cutoff is None:
        cutoff = dt.date.today().strftime("%Y%m%d")
    have = existing_data_days()
    eligible = [d for d in tds if d <= cutoff and d not in exclude]
    recent = eligible[-max_back:]
    return [d for d in recent if d not in have]


# ---------------- 数据源 ----------------
def fetch_sse(date):  # date: YYYYMMDD
    import akshare as ak
    df = ak.fund_etf_scale_sse(date=date)
    if df is None or df.empty:
        return pd.DataFrame()
    # 份额单位自适应：akshare 各版本可能返回"份"或"万份"
    raw = pd.to_numeric(df["基金份额"], errors="coerce")
    unit = 1e4 if raw.median(skipna=True) < 1e6 else 1.0
    if unit != 1.0:
        log("上交所份额疑似万份单位，按万份换算")
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(df["统计日期"], errors="coerce").dt.strftime("%Y-%m-%d"),
        "code": df["基金代码"].astype(str).str.zfill(6),
        "name": df["基金简称"].astype(str),
        "share": raw * unit / 1e8,  # → 亿份
        "size": None, "nav": None, "close": None, "source": "sse",
    })
    return out


def fetch_szse(start, end):  # YYYYMMDD
    import akshare as ak
    df = ak.fund_scale_daily_szse(start_date=start, end_date=end, symbol="ETF")
    if df is None or df.empty:
        return pd.DataFrame()
    cols = list(df.columns)

    def pick(*keys):
        for k in keys:
            for c in cols:
                if k in c:
                    return c
        return None

    c_date = pick("日期")
    c_code = pick("基金代码", "代码")
    c_name = pick("基金简称", "名称")
    c_share = pick("份额")
    c_nav = pick("净值")
    if c_code is None or c_share is None:
        log("深交所返回列异常: %s" % cols)
        return pd.DataFrame()
    dates = df[c_date] if c_date else start  # 无日期列时用请求首日
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(dates, errors="coerce").dt.strftime("%Y-%m-%d"),
        "code": df[c_code].astype(str).str.zfill(6),
        "name": df[c_name].astype(str) if c_name else "",
        "share": pd.to_numeric(df[c_share].astype(str).str.replace(",", ""), errors="coerce") / 1e8,
        "size": None, "nav": pd.to_numeric(df[c_nav], errors="coerce") if c_nav else None,
        "close": None, "source": "szse",
    })
    return out


def fetch_em(day8):  # 东财快照，day8: YYYYMMDD（快照的业务日期）
    import akshare as ak
    df = ak.fund_etf_spot_em()
    if df is None or df.empty:
        return pd.DataFrame()
    share = pd.to_numeric(df["最新份额"], errors="coerce")
    unit = 1e8 if share.max(skipna=True) > 1e11 else 1e4  # 份 or 万份 → 亿份
    size = (pd.to_numeric(df["总市值"], errors="coerce") / 1e8) if "总市值" in df.columns else None
    close = pd.to_numeric(df["最新价"], errors="coerce") if "最新价" in df.columns else None
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(day8).strftime("%Y-%m-%d"),
        "code": df["代码"].astype(str).str.zfill(6),
        "name": df["名称"].astype(str),
        "share": share / unit,
        "size": size, "nav": None, "close": close,
        "source": "em",
    })
    return out


# ---------------- 存储 ----------------
# 存档策略：每日一个 parquet 小文件（data/daily/share_YYYYMMDD.parquet）。
# 一日一文件天然幂等（同日重跑覆盖），且 git 只新增不重写，仓库不会膨胀。
DAILY_DIR = os.path.join(DATA_DIR, "daily")


def upsert(new: pd.DataFrame) -> int:
    if new.empty:
        return 0
    total = 0
    for (td,), grp in new.groupby(["trade_date"], sort=True):
        day = td.replace("-", "")
        path = os.path.join(DAILY_DIR, "share_%s.parquet" % day)
        grp = (grp.drop_duplicates(subset=["trade_date", "code"], keep="last")
                  .sort_values(["trade_date", "code"])
                  .reset_index(drop=True))
        os.makedirs(DAILY_DIR, exist_ok=True)
        tmp = path + ".tmp"
        grp.to_parquet(tmp, index=False)
        os.replace(tmp, path)  # 原子替换，防中断写坏
        total += len(grp)
        log("写入 %s（%d 只）" % (os.path.basename(path), len(grp)))
    return total


# ---------------- 主流程 ----------------
import requests as _rq

_NET_ERRS = (_rq.exceptions.ConnectionError, _rq.exceptions.Timeout,
             _rq.exceptions.ChunkedEncodingError, TimeoutError, ConnectionError)


def retry(fn, tries=3, pause=4, what=""):
    """网络错误重试；数据型错误（如交易所当日无数据）直接放弃返回空"""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if isinstance(e, _NET_ERRS) and i < tries - 1:
                log("%s网络错误，第 %d 次重试: %s" % (what, i + 1, str(e)[:100]))
                time.sleep(pause)
                continue
            log("%s失败: %s" % (what, str(e)[:150]))
            return pd.DataFrame()
    return pd.DataFrame()


def collect_one(day8: str, with_em: bool = True) -> pd.DataFrame:
    """采集单个交易日 YYYYMMDD，返回合并后的长表"""
    frames = []
    s = retry(lambda: fetch_sse(day8), what="上交所 ")
    log("上交所 %s: %d 只" % (day8, len(s)))
    frames.append(s)
    z = retry(lambda: fetch_szse(day8, day8), what="深交所 ")
    log("深交所 %s: %d 只" % (day8, len(z)))
    frames.append(z)
    if with_em:
        e_ = retry(lambda: fetch_em(day8), tries=2, what="东财 ")
        log("东财快照: %d 只" % len(e_))
        frames.append(e_)
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["share"])
    merged = merged[merged["share"] > 0]
    # 同日同 code 优先级：sse > szse > em
    prio = {"sse": 0, "szse": 1, "em": 2}
    merged["_p"] = merged["source"].map(prio)
    merged = (merged.sort_values("_p")
                    .drop_duplicates(subset=["trade_date", "code"], keep="first")
                    .drop(columns="_p"))
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="单个交易日 YYYYMMDD")
    ap.add_argument("--backfill", help="区间回填 YYYYMMDD:YYYYMMDD")
    ap.add_argument("--no-em", action="store_true", help="不用东财快照")
    ap.add_argument("--use-proxy", action="store_true", help="保留系统代理（默认禁代理直连）")
    ap.add_argument("--diag", action="store_true", help="诊断：打印各源列名与样本")
    args = ap.parse_args()

    if args.use_proxy:
        for k, v in _ORIG_PROXY.items():
            if v is not None:
                os.environ[k] = v

    if args.diag:
        import akshare as ak
        for fn, kw in [("fund_etf_scale_sse", {"date": "20261016"}),
                       ("fund_scale_daily_szse", {"start_date": "20261016", "end_date": "20261016", "symbol": "ETF"})]:
            try:
                df = getattr(ak, fn)(**kw)
                print("== %s ==\n列: %s\n%s\n" % (fn, list(df.columns), df.head(3).to_string()))
            except Exception as e:
                print("== %s == 失败: %s\n" % (fn, e))
        try:
            df = ak.fund_etf_spot_em()
            print("== fund_etf_spot_em ==\n列: %s\n%s\n" % (list(df.columns), df.head(3).to_string()))
        except Exception as e:
            print("== fund_etf_spot_em == 失败: %s" % e)
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    tds = load_trade_dates()
    today = dt.date.today().strftime("%Y%m%d")

    if args.backfill:
        start, end = args.backfill.split(":")
        days = [d for d in tds if start <= d <= end]
        log("回填 %s ~ %s，共 %d 个交易日（%s ~ %s）" % (start, end, len(days), days[0] if days else "-", days[-1] if days else "-"))
        ok_days = 0
        for i, d in enumerate(days, 1):
            td = pd.to_datetime(d).strftime("%Y-%m-%d")
            s = retry(lambda: fetch_sse(d), tries=2, what="上交所 ")
            z = retry(lambda: fetch_szse(d, d), tries=2, what="深交所 ")
            merged = pd.concat([f for f in (s, z) if f is not None and not f.empty], ignore_index=True) \
                if any(f is not None and not f.empty for f in (s, z)) else pd.DataFrame()
            if not merged.empty:
                merged = merged.dropna(subset=["share"])
                merged = merged[merged["share"] > 0]
                prio = {"sse": 0, "szse": 1, "em": 2}
                merged["_p"] = merged["source"].map(prio)
                merged = (merged.sort_values("_p")
                                .drop_duplicates(subset=["trade_date", "code"], keep="first")
                                .drop(columns="_p"))
                upsert(merged)
                ok_days += 1
            else:
                log("%s 无数据（交易所未披露或非交易日），跳过" % d)
            if i % 20 == 0 or i == len(days):
                log("进度 %d/%d（成功 %d 天）" % (i, len(days), ok_days))
            time.sleep(0.3)
        log("回填完成：%d/%d 天入库" % (ok_days, len(days)))
        return

    # 显式指定日期：只采该日，不回退、不补缺
    if args.date:
        merged = collect_one(args.date, with_em=not args.no_em)
        if merged.empty:
            log("未采到任何数据")
            sys.exit(1)
        upsert(merged)
        log("完成，入库 %d 只（%s）" % (len(merged), args.date))
        return

    have = existing_data_days()

    # 1) 优先补缺：最近交易日里数据文件缺失的先补（每次最多 3 个，最晚优先）。
    #    兜住 cron 被 GitHub 延迟/跳过、以及长假期间漏跑的情况。
    gaps = missing_trade_days(tds, exclude=(today,))
    for gap in reversed(gaps[-3:]):
        log("发现数据缺口 %s，优先补采" % gap)
        g = collect_one(gap, with_em=not args.no_em)
        if g.empty:
            log("缺口 %s 暂无数据（交易所未发布），下次运行再试" % gap)
        else:
            upsert(g)
            have.add(gap)

    # 2) 主目标：固定取"上一交易日"，不取当天。
    #    交易所当日份额是晚间陆续发布的：上交所约 22:20、深交所约 23:20
    #    （实测：21:13 两所皆空，22:16 上交所已全量、深交所仍空）。
    #    因此晚间定时跑"当天"必然为空或残缺；改为取上一交易日，此时两所
    #    数据已发布近 20 小时，必然齐全，且对 cron 的实际执行时刻完全不敏感
    #    ——GitHub 把 19:25 延迟到 22:00 还是次日凌晨，结果都一样。
    target = prev_trade_day(tds, today)
    if not target:
        log("交易日历异常：找不到 %s 之前的交易日，退出" % today)
        sys.exit(1)
    if target in have:
        log("上一交易日 %s 已入库，数据已是最新，本次无需采集" % target)
        return
    log("目标数据日：上一交易日 %s" % target)
    merged = collect_one(target, with_em=not args.no_em)
    if merged.empty:
        log("上一交易日 %s 未采到数据（交易所异常），下次运行再试" % target)
        sys.exit(1)
    upsert(merged)
    log("完成，入库 %d 只（%s）" % (len(merged), target))


if __name__ == "__main__":
    main()
