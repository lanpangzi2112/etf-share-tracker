#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_report.py — 从 data/share_*.parquet + src/etf_map.csv 生成 N-Tab HTML 看板

用法：
  python build_report.py                # 月频视图（每月最后一个交易日截面）
  python build_report.py --freq daily   # 日频视图
输出：
  docs/index.html   ECharts 内联单文件，可直接部署 GitHub Pages / 发手机
  docs/meta.json    生成信息（数据范围、ETF 数等）
"""
import argparse
import glob
import json
import math
import os

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
MAP_CSV = os.path.join(ROOT, "src", "etf_map.csv")
VENDOR_JS = os.path.join(ROOT, "vendor", "echarts.min.js")
OUT_HTML = os.path.join(ROOT, "docs", "index.html")
OUT_META = os.path.join(ROOT, "docs", "meta.json")

PALETTE = ["#5b9bd5", "#e8963c", "#4caf50", "#e6c33c", "#9b59b6", "#26a69a",
           "#b5651d", "#666666", "#d4a017", "#8d6e63", "#7fb3e0", "#2e5b8f",
           "#e8836b", "#8fc98f", "#a3a02e", "#555555"]


def log(msg):
    print("[build] %s" % msg, flush=True)


def nice_axis(peak, n, pad=1.0, floor_at=None):
    """轴上限与刻度步长都取整：返回 (max, step)，满足 max = step * n >= peak * pad。
    step 优先取整数（份额/百分比轴刻度可读性）；债券等小额轴允许 1 位小数。"""
    v = float(peak) * pad
    if floor_at is not None:
        v = max(v, float(floor_at))
    if v <= 0:
        return 1.0, 1.0
    e = math.floor(math.log10(v / n))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        step = m * (10 ** e)
        if step * n >= v:
            if step >= 1 and step != int(step):
                step = float(math.ceil(step))  # 步长向上取整
            return step * n, step
    step = float(10 ** (e + 1))
    return step * n, step


def load_share_df():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "daily", "share_*.parquet")))
    if not files:
        raise SystemExit("data/daily/ 下没有 parquet，先运行 collect.py")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df


# 单日份额变动超过该倍数即视为"折算候选"（拆分/合并）。
# 注意：真实大额申购同样会造成跳变，仅凭阈值无法区分，因此只对
# src/splits.csv 白名单里登记过的事件复权，其余候选只告警不改数。
SPLIT_HI = 1.9
SPLIT_LO = 1.0 / 1.9
SPLITS_CSV = os.path.join(ROOT, "src", "splits.csv")


def load_splits():
    """读取已核对的份额异动事件表 → (折算表, 申购表)，均为 {code: {日期: 比例}}。

    折算表：确认是份额拆分/合并，需要复权；
    申购表：确认是真实大额申赎，不复权也不告警。
    旧格式（无 verdict 列）一律视为折算，保持向后兼容。
    """
    if not os.path.exists(SPLITS_CSV):
        log("警告：未找到 src/splits.csv，不做份额折算复权")
        return {}, {}
    conf = pd.read_csv(SPLITS_CSV, dtype=str)
    cols = {c.strip().lower(): c for c in conf.columns}
    ver_col = cols.get("verdict")
    adj, flow = {}, {}
    for _, r in conf.iterrows():
        code = str(r["code"]).zfill(6)
        d = pd.Timestamp(str(r["date"])).strftime("%Y-%m-%d")
        verdict = (str(r[ver_col]).strip() if ver_col else "") or "折算"
        (adj if verdict == "折算" else flow).setdefault(code, {})[d] = float(r["ratio"])
    log("折算白名单：复权 %d 起，已确认真实申购 %d 起"
        % (sum(len(v) for v in adj.values()), sum(len(v) for v in flow.values())))
    return adj, flow


def adjust_splits(df, splits, watch_codes=None, known_flows=None):
    """份额折算前复权（白名单制）。

    只有 src/splits.csv 里 verdict=折算 的事件才复权：把折算日之前的份额
    按精确折算因子乘回去，使序列在"当前份额单位"下连续。未登记的跳变一律
    不改动——因为真实大额申购与拆分在份额序列上无法区分，必须先用净值
    确认再登记。已登记为"申购"的事件静默跳过（不再告警）。

    watch_codes 内的代码若出现未登记候选，会逐条告警（其余代码只计数），
    避免日志被非映射标的刷屏。

    原始 parquet 不改动，复权只发生在生成报告时。
    """
    df = df.sort_values(["code", "trade_date"]).copy()
    df["share"] = df["share"].astype(float)
    factor = pd.Series(1.0, index=df.index)
    applied, pending = [], []
    known_flows = known_flows or {}
    watch = set(watch_codes or ())

    for code, g in df.groupby("code", sort=False):
        if len(g) < 3:
            continue
        s = g["share"]
        ratio = (s / s.shift(1)).to_numpy()
        idx = g.index.to_numpy()
        dates = g["trade_date"].to_numpy()
        conf = splits.get(code, {})
        flows = known_flows.get(code, {})
        for k in range(1, len(g)):
            r = ratio[k]
            if r <= SPLIT_HI and r >= SPLIT_LO:
                continue
            d = pd.Timestamp(dates[k]).strftime("%Y-%m-%d")
            if d in conf:
                # 用白名单里的精确折算因子（净值推导），而非观测倍率——
                # 折算当日可能同时有真实申赎，用观测倍率会把它一并抹掉
                factor.loc[idx[:k]] *= conf[d]
                applied.append((code, d, conf[d]))
                if abs(r / conf[d] - 1) > 0.01:
                    log("  提示：%s %s 折算日另有约 %+.1f%% 真实申赎（已保留）"
                        % (code, d, (r / conf[d] - 1) * 100))
            elif d in flows:
                continue  # 已核对是真实申购，不动也不告警
            elif code in watch:
                pending.append((code, d, r))

    if applied:
        df["share"] = df["share"] * factor
        for code, d, r in applied:
            log("  折算复权 %s %s 因子 %.4f" % (code, d, r))
    log("份额折算复权：已按白名单复权 %d 起" % len(applied))
    for code, d, r in pending:
        log("  [警告] 疑似折算未确认 %s %s 倍率 %.4f，未复权"
            "（确认后登记到 src/splits.csv）" % (code, d, r))
    if pending:
        log("共 %d 起未登记候选，建议运行 python src/check_splits.py 核对" % len(pending))
    return df


def month_end_sample(s_daily):
    """日频序列 → 每月最后一个交易日截面"""
    return s_daily.groupby(s_daily.index.to_period("M")).last().sort_index()


def theme_series(df_daily, codes, all_codes, grace=20):
    """主题内 ETF 份额合计（日频，固定队列 + 晚上市标的静默并入）。

    队列锚定“序列首日已上市”的同题基金（保证跨年可比，起点即真实市场
    存量）；之后上市的同题基金从**上市满 grace 个交易日**起并入序列——
    首发募集期份额暴烈波动，上市即入会制造一次假跳升，等募集稳定后再
    计入。个别交易日缺失用前值填充（最多 5 天）；连续缺失（清盘/停止
    披露）则该基金退出合计。返回 (Series, 缺失代码列表)。
    """
    codes = [c for c in codes if c in set(all_codes)]
    missing = sorted(set(codes) - set(all_codes))
    if not codes:
        return None, missing
    sub = df_daily[df_daily["code"].isin(codes)]
    if sub.empty:
        return None, missing
    start = sub["trade_date"].min()
    cohort = sub[sub["trade_date"] == start]["code"].unique().tolist()
    late = sorted(set(codes) - set(cohort))
    piv = (sub.pivot_table(index="trade_date", columns="code", values="share",
                           aggfunc="last")
           .sort_index())
    # 晚上市标的：上市满 grace 个交易日后才计入（之前的值置 NaN 后由 sum 跳过）
    dates = piv.index
    pos = {d: i for i, d in enumerate(dates)}
    first = sub.groupby("code")["trade_date"].min()
    for c in late:
        if c not in piv.columns:
            continue
        i0 = pos[first[c]]
        for d in dates[i0:min(i0 + grace, len(dates))]:
            piv.loc[d, c] = float("nan")
    if late:
        log("  晚上市标的 %d 只，自上市满 %d 交易日起并入: %s"
            % (len(late), grace, ",".join(late[:8]) + ("..." if len(late) > 8 else "")))
    piv = piv.ffill(limit=5)
    s = piv.sum(axis=1, min_count=1).dropna()
    if len(s) < 3:
        return None, missing
    return s, missing


def fmt_top3(pairs):
    return "  ".join("%s(%+.1f%%)" % (n, c) for n, c in pairs[:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", choices=["month", "daily"], default="month")
    args = ap.parse_args()

    df = load_share_df()
    log("数据范围 %s ~ %s，共 %d 行，%d 只 ETF"
        % (df["trade_date"].min().date(), df["trade_date"].max().date(), len(df), df["code"].nunique()))

    emap = pd.read_csv(MAP_CSV, dtype=str)
    emap["code"] = emap["code"].str.zfill(6)
    # 份额折算前复权：只认 src/splits.csv 白名单（原始数据不动）
    splits, flows = load_splits()
    df = adjust_splits(df, splits, set(emap["code"]), flows)
    all_codes = set(df["code"])

    cats = []
    for cat, g in emap.groupby("category", sort=False):
        themes, colors, order = [], {}, []
        for theme, tg in g.groupby("theme", sort=False):
            s, missing = theme_series(df, tg["code"].tolist(), all_codes)
            if missing:
                log("警告：%s-%s 以下代码在数据中不存在（请核对 etf_map.csv）: %s"
                    % (cat, theme, ",".join(missing)))
            if s is None or len(s) < 2:
                log("跳过 %s-%s：有效数据点不足 2 个" % (cat, theme))
                continue
            themes.append((theme, s))
            colors[theme] = PALETTE[len(order) % len(PALETTE)]
            order.append(theme)
        if not themes:
            log("跳过类别 %s：无有效主题" % cat)
            continue
        cats.append({"name": cat, "themes": themes, "colors": colors, "order": order})
        log("%s：%d 个主题（%s）" % (cat, len(themes), "/".join(order)))

    if not cats:
        raise SystemExit("没有任何类别生成成功，请检查 etf_map.csv 与数据")

    # ---------- 全局对齐轴 ----------
    # 各主题起点不同（补采 2021-2024 后尤其明显：晚上市的基金主题起点晚），
    # 月份/日期轴取所有主题的并集，序列 reindex 对齐、缺失填 None（前端断点）。
    def _union_axis(freq):
        idx = set()
        for c in cats:
            for _, s in c["themes"]:
                sm = month_end_sample(s) if freq == "month" else s
                idx |= set(sm.index)
        return sorted(idx)

    def _align(s, axis, freq):
        sm = month_end_sample(s) if freq == "month" else s
        return sm.reindex(axis)

    def _num(v):
        return None if v is None or pd.isna(v) else round(float(v), 2)

    def _valid(v):
        return [x for x in v if x is not None]

    def _first_valid(v):
        vv = _valid(v)
        return vv[0] if vv else None

    def _last_valid(v):
        vv = _valid(v)
        return vv[-1] if vv else None

    GLOBAL_AXIS = _union_axis(args.freq)

    # ---------- 每类面板 ----------
    for c in cats:
        # 图3 用日频最近 5 个交易日
        d5_all = []
        for theme, s in c["themes"]:
            tail = s.tail(5)
            base = float(tail.iloc[0])
            p3 = [round(float(v) / base * 100, 1) for v in tail.values]
            d5_all.append((theme, p3, [d.strftime("%m-%d") for d in tail.index]))
        n5 = max(len(x[2]) for x in d5_all)
        c["days5"] = next(x[2] for x in d5_all if len(x[2]) == n5)

        xs, p1s, p2s = [], {}, {}
        for theme, s in c["themes"]:
            al = _align(s, GLOBAL_AXIS, args.freq)
            xs = [d.strftime("%Y-%m") if args.freq == "month" else d.strftime("%Y-%m-%d")
                  for d in GLOBAL_AXIS]
            p1 = [_num(v) for v in al.values]
            p1s[theme] = p1
            b = _first_valid(p1)
            p2s[theme] = [None if v is None else round(v / b * 100, 1)
                          for v in p1] if b else [None] * len(p1)
        c["months"] = xs
        c["panel1"] = p1s
        c["panel2"] = p2s
        c["panel3"] = {theme: p3 for theme, p3, _ in d5_all}

        # 轴范围（上限与刻度步长都取整：max = step × n，刻度全是整数）
        peak1 = max(max(_valid(v)) for v in p1s.values())
        peak2 = max(max(_valid(v)) for v in p2s.values())
        lo3 = min(min(v) for v in c["panel3"].values())
        hi3 = max(max(v) for v in c["panel3"].values())
        c["y1max"], c["y1int"] = nice_axis(peak1, 7, 1.12)
        c["y2max"], c["y2int"] = nice_axis(peak2, 5, 1.06, floor_at=110)
        lo3v = min(math.floor(lo3 * 0.98), 98)
        span = max(hi3 * 1.02, 102) - lo3v
        _, step3 = nice_axis(span, 5, 1.0)
        c["y3"] = [lo3v, lo3v + step3 * 5]
        c["y3int"] = step3

        # TOP3（按变化幅度绝对值排序，与原看板口径一致；取各序列最后有效值）
        c["top3_p2"] = fmt_top3(sorted(((n, _last_valid(v) - 100) for n, v in p2s.items()),
                                       key=lambda x: -abs(x[1])))
        c["top3_p3"] = fmt_top3(sorted(((n, v[-1] - 100) for n, v in c["panel3"].items()),
                                       key=lambda x: -abs(x[1])))

        # 分析要素
        # 长期冠军/失血王按「绝对增量」排序（亿份）——5 年窗口下百分比会被
        # 小基数主题绑架（0.02→7.97 亿份 = +79600%），绝对增量才反映真实吸金
        def _delta(n):
            p = p1s[n]
            a, b = _first_valid(p), _last_valid(p)
            return (b - a) if (a is not None and b is not None) else 0.0

        def _delta_pct(n):
            p = p1s[n]
            a, b = _first_valid(p), _last_valid(p)
            return (b / a * 100 - 100) if (a and b is not None) else 0.0

        chg2 = sorted(((n, _delta(n)) for n in p1s), key=lambda x: -x[1])
        chg3 = sorted(((n, v[-1] - 100) for n, v in c["panel3"].items()), key=lambda x: -abs(x[1]))
        c["leader"], c["leader_chg"] = chg2[0]
        c["leader_pct"] = _delta_pct(c["leader"])
        c["laggard"], c["laggard_chg"] = chg2[-1]
        c["laggard_pct"] = _delta_pct(c["laggard"])
        c["d5_leader"], c["d5_chg"] = chg3[0]
        c["d5_laggard"], c["d5_lchg"] = chg3[-1]
        lead_p1 = p1s[c["leader"]]
        peak_i = max((i for i, v in enumerate(lead_p1) if v is not None),
                     key=lambda i: lead_p1[i], default=0)
        c["leader_peak"] = xs[peak_i] if xs else "-"
        c["leader_end"] = _last_valid(lead_p1)
        c["n_series"] = len(c["themes"])
        peak_v = lead_p1[peak_i] if peak_i < len(lead_p1) else None
        c["drawdown"] = (c["leader_end"] - peak_v) / peak_v * 100 if peak_v else 0.0

        start_lbl = xs[0] if xs else "-"
        d5_dir = "流入" if c["d5_chg"] >= 0 else "流出"
        shape = ""
        if c["drawdown"] <= -30:
            shape = "，较峰值 %s 回撤 %.0f%%" % (c["leader_peak"], -c["drawdown"])
        c["analysis"] = (
            "<b>结构：</b>共 %d 个板块。<b>%s</b>是长期吸金主线，自 %s 以来累计份额净增 "
            "<span class='%s'>%+.1f 亿份</span>（%+.1f%%）%s，当前合计 %s 亿份；"
            "失血最多的是 <b>%s</b>（<span class='%s'>%+.1f 亿份</span>，%+.1f%%）。<br>"
            "<b>近5个交易日：</b><b>%s</b>资金%s最明显（<span class='%s'>%+.1f%%</span>），"
            "<b>%s</b>相对走弱（<span class='%s'>%+.1f%%</span>）。<br>"
            "<b>读法：</b>图①看累计规模，图②看相对起点的高低，图③看最新5日动能——三张图结合可判断"
            "“谁在持续吸金、谁在被赎回”。长期排名按净增份额（亿份）计，避免小基数主题百分比失真。"
        ) % (c["n_series"], c["leader"], start_lbl,
             "up" if c["leader_chg"] >= 0 else "down", c["leader_chg"], c["leader_pct"], shape,
             "{:,.1f}".format(c["leader_end"] if c["leader_end"] else 0),
             c["laggard"], "up" if c["laggard_chg"] >= 0 else "down", c["laggard_chg"], c["laggard_pct"],
             c["d5_leader"], d5_dir, "up" if c["d5_chg"] >= 0 else "down", c["d5_chg"],
             c["d5_laggard"], "up" if c["d5_lchg"] >= 0 else "down", c["d5_lchg"])

    # ---------- 汇总 ----------
    summary = [{"name": c["name"], "n": c["n_series"], "leader": c["leader"],
                "leader_chg": round(c["leader_chg"], 1), "d5": c["d5_leader"],
                "d5_chg": round(c["d5_chg"], 1)} for c in cats]

    ranked = sorted(cats, key=lambda c: -c["leader_chg"])
    ranked5 = sorted(cats, key=lambda c: -abs(c["d5_chg"]))
    last_date = df["trade_date"].max().strftime("%Y-%m-%d")
    lines = ["<b>1. 数据概况。</b>数据截至 %s，覆盖 %d 只 ETF、%d 个类别；"
             "长期吸金冠军为<b>%s</b>的<b>%s</b>（净增 %+.1f 亿份，%+.1f%%），"
             "近5日动能最强的是<b>%s</b>的<b>%s</b>（%+.1f%%）。长期排名按净增份额计，避免小基数主题百分比失真。"
             % (last_date, int(df["code"].nunique()), len(cats),
                ranked[0]["name"], ranked[0]["leader"], ranked[0]["leader_chg"], ranked[0]["leader_pct"],
                ranked5[0]["name"], ranked5[0]["d5_leader"], ranked5[0]["d5_chg"])]
    for i, c in enumerate(cats, 2):
        extra = ""
        if c["drawdown"] <= -30:
            extra = "，较峰值回撤 %.0f%%" % (-c["drawdown"])
        lines.append("<b>%d. %s。</b>长期吸金主线 %s（净增 %+.1f 亿份，%+.1f%%）%s；失血最多 %s（%+.1f 亿份）；"
                     "近5日 %s %+.1f%%。"
                     % (i, c["name"], c["leader"], c["leader_chg"], c["leader_pct"], extra,
                        c["laggard"], c["laggard_chg"], c["d5_leader"], c["d5_chg"]))
    sumAna = "<br>".join(lines)

    # ---------- 组装 DATA（与页面 JS 约定一致） ----------
    DATA = {
        "months": cats[0]["months"], "days5": cats[0]["days5"],
        "cats": [{"key": c["name"], "name": c["name"], "y1max": c["y1max"],
                  "y1int": c["y1int"], "y2max": c["y2max"], "y2int": c["y2int"],
                  "y3": c["y3"], "y3int": c["y3int"],
                  "top3_p2": c["top3_p2"], "top3_p3": c["top3_p3"],
                  "panel1": c["panel1"], "panel2": c["panel2"], "panel3": c["panel3"],
                  "colors": c["colors"], "order": c["order"],
                  "analysis": c["analysis"]} for c in cats],
        "summary": summary, "sumAna": sumAna,
    }

    with open(VENDOR_JS, "r", encoding="utf-8") as f:
        echarts_lib = f.read()
    assert "</script" not in echarts_lib
    emit(DATA, echarts_lib, cats, df, args.freq, last_date)


def emit(DATA, echarts_lib, cats, df, freq, last_date):
    import datetime as dt
    html = HTML_TEMPLATE
    html = html.replace("__ECHARTS__", echarts_lib)
    html = html.replace("__DATA__", json.dumps(DATA, ensure_ascii=False))
    n_cats = len(cats)
    html = html.replace("（8大类）", "（%d大类）" % n_cats)
    html = html.replace("8 大类 × 3 张图", "%d 大类 × 3 张图" % n_cats)
    html = html.replace("鼠标或手指滑动查看数值",
                        "鼠标或手指滑动查看数值 · 数据截至 %s" % last_date)
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "freq": freq, "data_until": last_date,
                   "n_etf": int(df["code"].nunique()), "n_cats": n_cats,
                   "summary": DATA["summary"]}, f, ensure_ascii=False, indent=2)
    log("已生成 %s（%.1f KB）" % (OUT_HTML, os.path.getsize(OUT_HTML) / 1024))


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETF份额变化全景分析（8大类）</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body { background: #fff; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: #333; }
  .wrap { width: 100%; max-width: 1000px; margin: 0 auto; padding: 18px 14px 60px; }
  h1 { font-size: 20px; text-align: center; margin: 0 0 4px; }
  .sub { text-align: center; color: #999; font-size: 12px; margin-bottom: 14px; }
  .tab-bar { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; margin-bottom: 14px;
             position: sticky; top: 0; background: #fff; z-index: 10; padding: 8px 0;
             border-bottom: 1px solid #f0f0f0; }
  .tab-bar button { border: 1px solid #ddd; background: #fafafa; color: #555; font-size: 13px;
                    padding: 6px 14px; border-radius: 16px; cursor: pointer; }
  .tab-bar button.active { background: #5b9bd5; border-color: #5b9bd5; color: #fff; font-weight: 600; }
  .tab-page { display: none; }
  .tab-page.active { display: block; }
  .panel-title { font-size: 15px; font-weight: 600; margin: 20px 0 4px; }
  .band-legend { font-size: 12px; color: #888; margin: 0 0 4px; line-height: 1.8; }
  .band-legend span { margin-right: 14px; white-space: nowrap; }
  .sw { display: inline-block; width: 14px; height: 10px; vertical-align: middle; margin-right: 4px; border-radius: 2px; }
  .chart { width: 100%; }
  .h380 { height: 380px; } .h400 { height: 400px; } .h320 { height: 320px; } .h300 { height: 300px; }
  .note { display: inline-block; margin: 4px 0 8px; padding: 4px 10px; font-size: 12px;
          background: #fffbe6; border: 1px solid #ffe58f; border-radius: 3px; color: #666; }
  .analysis { margin: 14px 0 6px; padding: 12px 14px; background: #f7f9fc; border-left: 3px solid #5b9bd5;
              border-radius: 4px; font-size: 13px; line-height: 1.9; }
  .analysis b { color: #2e5b8f; }
  .up { color: #d43f3a; font-weight: 600; }
  .down { color: #4caf50; font-weight: 600; }
  table.sum { width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0 18px; }
  table.sum th, table.sum td { border: 1px solid #eee; padding: 7px 9px; text-align: center; }
  table.sum th { background: #f5f7fa; font-weight: 600; }
  table.sum td.l { text-align: left; }
  @media (max-width: 640px) {
    .wrap { padding: 12px 8px 40px; }
    h1 { font-size: 17px; }
    .tab-bar button { font-size: 12px; padding: 5px 10px; }
    .panel-title { font-size: 14px; }
    .h380 { height: 290px; } .h400 { height: 300px; } .h320 { height: 250px; } .h300 { height: 240px; }
    .analysis { font-size: 12px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>ETF份额变化全景分析</h1>
  <div class="sub">8 大类 × 3 张图：累计份额变化（亿份）/ 长期变化率（起始日=100%）/ 近5自然日变化率 · 鼠标或手指滑动查看数值</div>
  <div class="tab-bar" id="tabBar"></div>
  <div id="pages"></div>
</div>

<script>__ECHARTS__</script>
<script>
const DATA = __DATA__;
const MONTHS = DATA.months, DAYS5 = DATA.days5;
const chartsByTab = {};
let curTab = 0;

// ---------- 构建 Tab 与页面 ----------
const bar = document.getElementById('tabBar'), pages = document.getElementById('pages');
DATA.cats.forEach((c, i) => {
  const b = document.createElement('button');
  b.textContent = (i + 1) + '.' + c.name;
  b.onclick = () => showTab(i);
  bar.appendChild(b);
  const p = document.createElement('div');
  p.className = 'tab-page'; p.id = 'page-' + i;
  p.innerHTML =
    '<div class="panel-title">① 份额变化（亿份，累计）</div><div id="t' + i + 'c1" class="chart h400"></div>' +
    '<div class="panel-title">② 长期份额变化率（起始日=100%）</div>' +
    '<div class="band-legend"><span><i class="sw" style="background:rgba(232,150,60,.25)"></i>上涨区（高于100%）</span>' +
    '<span><i class="sw" style="background:rgba(120,120,120,.12)"></i>下跌区（低于100%）</span><span>- - 基准线（100%）</span></div>' +
    '<div id="t' + i + 'c2" class="chart h400"></div><div class="note" id="t' + i + 'n2"></div>' +
    '<div class="panel-title">③ 近5自然日内交易日份额变化率（区间首日=100%）</div>' +
    '<div class="band-legend"><span><i class="sw" style="background:rgba(76,175,80,.18)"></i>上涨区（高于100%）</span>' +
    '<span><i class="sw" style="background:rgba(91,155,213,.15)"></i>下跌区（低于100%）</span><span>- - 基准线（100%）</span></div>' +
    '<div id="t' + i + 'c3" class="chart h320"></div><div class="note" id="t' + i + 'n3"></div>' +
    '<div class="analysis" id="t' + i + 'ana"></div>';
  pages.appendChild(p);
});
// 汇总页
const bs = document.createElement('button');
bs.textContent = (DATA.cats.length + 1) + '.汇总分析';
bs.onclick = () => showTab(DATA.cats.length);
bar.appendChild(bs);
const ps = document.createElement('div');
ps.className = 'tab-page'; ps.id = 'page-sum';
ps.innerHTML =
  '<div class="panel-title">各类别对比：长期冠军 vs 近5日最强</div>' +
  '<div id="cSum" class="chart h380"></div>' +
  '<div class="panel-title">汇总表</div><table class="sum" id="sumTable"></table>' +
  '<div class="panel-title">跨类别要点</div><div class="analysis" id="sumAna"></div>';
pages.appendChild(ps);

// ---------- 图表配置 ----------
const baseTooltip = {
  trigger: 'axis',
  axisPointer: { type: 'line', lineStyle: { color: '#999', type: 'dashed', width: 1 }, snap: true },
  backgroundColor: 'rgba(255,255,255,0.96)', borderColor: '#eee',
  textStyle: { color: '#333', fontSize: 12 },
  extraCssText: 'box-shadow:0 2px 12px rgba(0,0,0,.12);border-radius:4px;'
};
const baseLegend = { type: 'scroll', top: 8, left: 'center', itemWidth: 18, itemHeight: 10,
                     itemGap: 12, textStyle: { fontSize: 12, color: '#333' } };
const gridCommon = { top: 52, left: 46, right: 22, bottom: 42, containLabel: true };
const xAxisCommon = { type: 'category', boundaryGap: false,
                      axisLabel: { color: '#666', fontSize: 11, interval: 'auto', rotate: 35 } };
const yAxisCommon = { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } },
                      axisLabel: { color: '#666', fontSize: 11 } };

function mkSeries(store, colors, extra) {
  return colors.map(([n, col]) => Object.assign({
    name: n, type: 'line', color: col, smooth: false, showSymbol: false,
    symbol: 'circle', symbolSize: 7, lineStyle: { width: 1.6 },
    emphasis: { focus: 'series' }, data: store[n]
  }, extra || {}));
}

function initTab(i) {
  const c = DATA.cats[i], list = [];
  const colors = c.order.map(n => [n, c.colors[n]]);
  const el1 = document.getElementById('t' + i + 'c1');
  const ch1 = echarts.init(el1);
  ch1.setOption({
    legend: baseLegend, grid: gridCommon,
    tooltip: Object.assign({}, baseTooltip, { valueFormatter: v => v.toLocaleString() }),
    xAxis: Object.assign({}, xAxisCommon, { data: MONTHS }),
    yAxis: Object.assign({}, yAxisCommon, { min: 0, max: c.y1max, interval: c.y1int }),
    dataZoom: [{ type: 'inside', rangeMode: ['value', 'value'] }],
    series: mkSeries(c.panel1, colors)
  });
  list.push(ch1);

  const ch2 = echarts.init(document.getElementById('t' + i + 'c2'));
  ch2.setOption({
    legend: baseLegend, grid: gridCommon,
    tooltip: Object.assign({}, baseTooltip, { valueFormatter: v => v + '%' }),
    xAxis: Object.assign({}, xAxisCommon, { data: MONTHS }),
    yAxis: Object.assign({}, yAxisCommon, { min: 0, max: c.y2max, interval: c.y2int,
                                            axisLabel: { color: '#666', fontSize: 11, formatter: '{value}%' } }),
    dataZoom: [{ type: 'inside', rangeMode: ['value', 'value'] }],
    series: mkSeries(c.panel2, colors, {
      markLine: { symbol: 'none', lineStyle: { color: '#999', type: 'dashed', width: 1 },
                  label: { show: false }, data: [{ yAxis: 100 }] },
      markArea: { silent: true, data: [
        [{ yAxis: c.y2max, itemStyle: { color: 'rgba(232,150,60,.07)' } }, { yAxis: 100 }],
        [{ yAxis: 100, itemStyle: { color: 'rgba(120,120,120,.05)' } }, { yAxis: 0 }]] }
    })
  });
  list.push(ch2);
  document.getElementById('t' + i + 'n2').textContent = '长期变化 TOP3: ' + c.top3_p2;

  const y3 = c.y3;
  const ch3 = echarts.init(document.getElementById('t' + i + 'c3'));
  ch3.setOption({
    legend: baseLegend, grid: { top: 52, left: 46, right: 22, bottom: 46, containLabel: true },
    tooltip: Object.assign({}, baseTooltip, { valueFormatter: v => v + '%' }),
    xAxis: { type: 'category', boundaryGap: false, data: DAYS5, name: '日期',
             nameLocation: 'middle', nameGap: 28, nameTextStyle: { color: '#666', fontSize: 12 },
             axisLabel: { color: '#666', fontSize: 12 } },
    yAxis: Object.assign({}, yAxisCommon, { min: y3[0], max: y3[1], interval: c.y3int,
                                            axisLabel: { color: '#666', fontSize: 11, formatter: '{value}%' } }),
    series: mkSeries(c.panel3, colors, {
      markLine: { symbol: 'none', lineStyle: { color: '#999', type: 'dashed', width: 1 },
                  label: { show: false }, data: [{ yAxis: 100 }] },
      markArea: { silent: true, data: [
        [{ yAxis: y3[1], itemStyle: { color: 'rgba(76,175,80,.10)' } }, { yAxis: 100 }],
        [{ yAxis: 100, itemStyle: { color: 'rgba(91,155,213,.08)' } }, { yAxis: y3[0] }]] }
    })
  });
  list.push(ch3);
  document.getElementById('t' + i + 'n3').textContent = '近5自然日区间变化 TOP3: ' + c.top3_p3;
  document.getElementById('t' + i + 'ana').innerHTML = c.analysis;
  chartsByTab[i] = list;
}

function initSum() {
  const s = DATA.summary;
  const names = s.map(x => x.name);
  const ch = echarts.init(document.getElementById('cSum'));
  ch.setOption({
    legend: { top: 6, left: 'center', textStyle: { fontSize: 12 } },
    grid: { top: 46, left: 60, right: 30, bottom: 30, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
               valueFormatter: v => v + '%' },
    xAxis: { type: 'category', data: names, axisLabel: { color: '#666', fontSize: 12 } },
    yAxis: { type: 'value', name: '变化率(%)', nameTextStyle: { color: '#666' },
             axisLabel: { color: '#666', formatter: '{value}%' },
             splitLine: { lineStyle: { color: '#f0f0f0' } } },
    series: [
      { name: '长期冠军变化', type: 'bar', barWidth: '32%',
        data: s.map(x => ({ value: x.leader_chg,
          itemStyle: { color: x.leader_chg >= 0 ? '#d43f3a' : '#4caf50' } })),
        label: { show: true, position: 'top', fontSize: 11, color: '#666', formatter: '{c}%' } },
      { name: '近5日最强变化', type: 'bar', barWidth: '32%',
        data: s.map(x => ({ value: x.d5_chg,
          itemStyle: { color: x.d5_chg >= 0 ? '#e8836b' : '#8fc98f' } })),
        label: { show: true, position: 'top', fontSize: 11, color: '#666', formatter: '{c}%' } }
    ]
  });
  chartsByTab['sum'] = [ch];

  let rows = '<tr><th>类别</th><th>板块数</th><th>长期冠军</th><th>长期变化</th><th>近5日最强</th><th>近5日变化</th></tr>';
  s.forEach(x => {
    rows += '<tr><td>' + x.name + '</td><td>' + x.n + '</td><td class="l">' + x.leader + '</td>' +
      '<td class="' + (x.leader_chg >= 0 ? 'up' : 'down') + '">' + (x.leader_chg >= 0 ? '+' : '') + x.leader_chg + '%</td>' +
      '<td class="l">' + x.d5 + '</td>' +
      '<td class="' + (x.d5_chg >= 0 ? 'up' : 'down') + '">' + (x.d5_chg >= 0 ? '+' : '') + x.d5_chg + '%</td></tr>';
  });
  document.getElementById('sumTable').innerHTML = rows;
  document.getElementById('sumAna').innerHTML = DATA.sumAna;
}

function showTab(i) {
  curTab = i;
  document.querySelectorAll('.tab-bar button').forEach((b, j) =>
    b.classList.toggle('active', j === i));
  document.querySelectorAll('.tab-page').forEach((p, j) =>
    p.classList.toggle('active', j === i));
  if (i === DATA.cats.length) {
    if (!chartsByTab['sum']) initSum(); else chartsByTab['sum'].forEach(c => c.resize());
  } else {
    if (!chartsByTab[i]) initTab(i); else chartsByTab[i].forEach(c => c.resize());
  }
}

let rt = null;
window.addEventListener('resize', () => {
  clearTimeout(rt);
  rt = setTimeout(() => { (chartsByTab[curTab] || []).forEach(c => c.resize()); }, 120);
});
showTab(0);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
