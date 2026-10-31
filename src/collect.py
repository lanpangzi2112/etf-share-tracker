#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — 每交易日采集全市场 ETF 份额，落 data/share_YYYY.parquet

数据源（均免费、无需 key）：
  1. 上交所  ak.fund_etf_scale_sse(date)          官方份额，支持历史日期
  2. 深交所  ak.fund_scale_daily_szse(start, end) 官方日频份额
（东财快照源 2026-10-25 移除：历史累计采集 0 行，纯冗余且单位自适应逻辑有缺陷）

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
import json
import os
import socket
import sys
import time

import pandas as pd
import requests

# 交易所接口偶发挂起：akshare 内部的 requests.get 没有 timeout，上交所一旦不响应
# 会一直卡到 CI 超时。这里统一注入默认超时（socket 层 + requests 层双保险）。
# 2026-10-31 事故：10-30 上交所 923 只全部漏采，就是请求失败后静默返回空表所致。
HTTP_TIMEOUT = 30
socket.setdefaulttimeout(HTTP_TIMEOUT)
_orig_request = requests.api.request


def _request_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    return _orig_request(*args, **kwargs)


requests.api.request = _request_with_timeout

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


# 两所都必须有数据才算完整：上交所 ~923 只、深交所 ~768 只。
# 只凭其中一所写出的文件是残缺的，绝不能被当作"已入库"。
REQUIRED_SOURCES = ("sse", "szse")


def day_is_complete(day8):
    """该交易日的数据文件是否两所齐全。不存在也返回 False。"""
    path = os.path.join(DAILY_DIR, "share_%s.parquet" % day8)
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_parquet(path, columns=["source"])
    except Exception:
        return False
    if df.empty:
        return False
    return all(s in set(df["source"].dropna()) for s in REQUIRED_SOURCES)


def missing_trade_days(tds, max_back=30, exclude=(), cutoff=None):
    """截止日（含）之前最近 max_back 个交易日里，数据文件缺失或不完整的日期（升序）。
    用于优先补缺：防止 T 日数据当晚未发布、而 T+1 日先发布时，
    旧回退逻辑只认上一交易日、把 T 日永久跳过。
    注意：akshare 交易日历包含未来日期，必须按 cutoff 截断，
    否则扫描窗口会被未来日期占满，永远摸不到真正的历史缺口。
    2026-10-31 起：不再只看文件是否存在，还校验数据是否两所齐全
    ——残缺文件（如只有深交所）会被重新判定为缺口并重采。"""
    if cutoff is None:
        cutoff = dt.date.today().strftime("%Y%m%d")
    have = existing_data_days()
    eligible = [d for d in tds if d <= cutoff and d not in exclude]
    recent = eligible[-max_back:]
    out = []
    for d in recent:
        if d not in have:
            out.append(d)
        elif not day_is_complete(d):
            log("发现残缺数据 %s（缺上交所或深交所），列入补采" % d)
            out.append(d)
    return out


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


# 东财快照源已移除（2026-10-25）：该源历史累计采集 0 行（接口长期不可用），
# 纯冗余；且其单位自适应用 share.max() 判断，可能被单只超大份额 ETF 误导。
# 份额数据现在只取上交所 + 深交所两个官方源。


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
# 可重试的异常：网络层全部异常 + 接口返回非 JSON（交易所维护页/限流页）。
# 2026-10-31 事故：akshare 在 r.json() 上抛的 JSONDecodeError 不在原重试范围内，
# 导致一次失败就彻底放弃，直接返回空表。
_NET_ERRS = (
    requests.exceptions.RequestException,   # 含 ConnectionError/Timeout/HTTPError/ChunkedEncodingError
    json.JSONDecodeError,                   # 接口返回 HTML 而非 JSON
    TimeoutError,
    ConnectionError,
    OSError,
)


def retry(fn, tries=3, pause=4, what=""):
    """网络/接口异常重试；数据型错误（如交易所无该日数据）直接放弃返回空"""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if isinstance(e, _NET_ERRS) and i < tries - 1:
                log("%s请求异常，第 %d/%d 次重试: %s"
                    % (what, i + 1, tries - 1, str(e)[:100]))
                time.sleep(pause * (i + 1))
                continue
            log("%s失败: %s: %s" % (what, type(e).__name__, str(e)[:150]))
            return pd.DataFrame()
    return pd.DataFrame()


def collect_one(day8: str):
    """采集单个交易日 YYYYMMDD。

    返回 (merged_df, counts)。counts 记录两所各取到多少只，
    调用方必须用它判断数据是否完整——**只有单一交易所的结果不能当作成功**。
    """
    frames, counts = [], {}
    s = retry(lambda: fetch_sse(day8), what="上交所 ")
    counts["sse"] = len(s)
    log("上交所 %s: %d 只" % (day8, len(s)))
    frames.append(s)
    z = retry(lambda: fetch_szse(day8, day8), what="深交所 ")
    counts["szse"] = len(z)
    log("深交所 %s: %d 只" % (day8, len(z)))
    frames.append(z)
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(), counts
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["share"])
    merged = merged[merged["share"] > 0]
    # 同日同 code 优先级：sse > szse
    prio = {"sse": 0, "szse": 1}
    merged["_p"] = merged["source"].map(prio)
    merged = (merged.sort_values("_p")
                    .drop_duplicates(subset=["trade_date", "code"], keep="first")
                    .drop(columns="_p"))
    return merged, counts


def missing_sources(counts):
    """返回缺失的数据源列表；空列表表示两所齐全。"""
    return [s for s in REQUIRED_SOURCES if counts.get(s, 0) == 0]


def warn(msg):
    log(msg)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning title=采集数据不完整::%s" % msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="单个交易日 YYYYMMDD")
    ap.add_argument("--backfill", help="区间回填 YYYYMMDD:YYYYMMDD")
    ap.add_argument("--use-proxy", action="store_true", help="保留系统代理（默认禁代理直连）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --date：即使只有单一交易所数据也强制写入（默认拒绝残缺数据）")
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
                # 回填多为历史日期，交易所早期覆盖可能不全：缺一所只告警不拦截，
                # 但必须留痕，避免又把残缺文件当成完整数据。
                miss = [n for n, f in (("sse", s), ("szse", z))
                        if f is None or f.empty]
                if miss:
                    log("⚠ %s 缺少数据源 %s，按残缺写入（回填模式）" % (d, ",".join(miss)))
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
        merged, counts = collect_one(args.date)
        miss = missing_sources(counts)
        if merged.empty:
            log("未采到任何数据（%s）" % args.date)
            sys.exit(1)
        if miss and not args.force:
            # 残缺不落盘：否则会被 existing_data_days 当作已入库，缺口永久留存
            warn("%s 缺少数据源 %s（上交所%d只/深交所%d只），未写入；"
                 "可用 --force 强制写入残缺数据"
                 % (args.date, ",".join(miss), counts.get("sse", 0), counts.get("szse", 0)))
            sys.exit(1)
        upsert(merged)
        log("完成，入库 %d 只（%s）" % (len(merged), args.date))
        return

    have = existing_data_days()

    # 1) 优先补缺：最近交易日里数据文件缺失/残缺的先补（每次最多 3 个，最晚优先）。
    #    兜住 cron 被 GitHub 延迟/跳过、以及长假期间漏跑的情况。
    gaps = missing_trade_days(tds, exclude=(today,))
    for gap in reversed(gaps[-3:]):
        log("发现数据缺口 %s，优先补采" % gap)
        g, gcounts = collect_one(gap)
        gmiss = missing_sources(gcounts)
        if g.empty:
            log("缺口 %s 暂无数据（交易所未发布），下次运行再试" % gap)
        elif gmiss:
            warn("缺口 %s 仍缺数据源 %s（上交所%d只/深交所%d只），本轮不写入，下次重试"
                 % (gap, ",".join(gmiss), gcounts.get("sse", 0), gcounts.get("szse", 0)))
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
    if target in have and day_is_complete(target):
        log("上一交易日 %s 已入库且两所齐全，本次无需采集" % target)
        return
    log("目标数据日：上一交易日 %s" % target)
    merged, counts = collect_one(target)
    miss = missing_sources(counts)
    if merged.empty:
        # 不 exit(1)：采集失败不应阻塞后续的 commit / 构建 / 部署。
        # CI runner 是临时容器，一旦以失败状态退出，本轮"补缺"采到的数据
        # 会随容器销毁而丢失，缺口反而越积越多。正常退出让已有数据照常上线。
        msg = "上一交易日 %s 未采到数据（可能交易所未发布或接口异常），本轮跳过采集，继续用已有数据构建看板" % target
        log(msg)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::warning title=采集未取到数据::%s" % msg)
        return
    if miss:
        # 关键闸门：只有一所数据时不落盘。否则残缺文件会被当作已入库，
        # 缺口扫描（missing_trade_days）再也不会重试，缺口永久留存。
        warn("上一交易日 %s 数据不完整，缺数据源 %s（上交所%d只/深交所%d只），"
             "本轮不写入，下次运行自动重试"
             % (target, ",".join(miss), counts.get("sse", 0), counts.get("szse", 0)))
        return
    upsert(merged)
    log("完成，入库 %d 只（%s）" % (len(merged), target))


if __name__ == "__main__":
    main()
