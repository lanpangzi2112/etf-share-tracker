# -*- coding: utf-8 -*-
"""生成 ETF 标的分类清单（单个自包含 HTML）。

按「板块 → 主题 → 标的」组织全量 ETF，附每只的最新份额、占主题比例、
交易所与在档状态。页面自带搜索、仅显示在档、展开/折叠、板块锚点，
无外部依赖，可直接双击打开或打印。

用法：
  python src/tools/build_catalog.py                      # 输出到工作区根/ETF分类清单.html
  python src/tools/build_catalog.py --out docs/catalog.html
  python src/tools/build_catalog.py --out /abs/path.html

数据来自 src/etf_map.csv 与 data/daily/share_*.parquet（自动取最新交易日）。
映射表每次变动后跑一次本脚本即可，不需要手工改 HTML。
"""
import argparse
import html
from pathlib import Path

import pandas as pd


def _find_root() -> Path:
    p = Path(__file__).resolve().parent
    while p.parent != p:
        if p.name == "etf-share-daily":
            return p
        p = p.parent
    raise SystemExit("找不到 etf-share-daily 仓库根")


ROOT = _find_root()
MAP = ROOT / "src/etf_map.csv"
DAILY = ROOT / "data/daily"
DEFAULT_OUT = ROOT.parent / "ETF分类清单.html"

CSS = r"""*{box-sizing:border-box}html,body{margin:0;padding:0}
body{background:#fafafa;color:#222;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 80px}
h1{font-size:22px;font-weight:600;margin:0 0 4px}
.sub{color:#888;font-size:12px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.card{background:#fff;border:1px solid #eaeaea;border-radius:10px;padding:12px 14px}
.card .k{color:#888;font-size:12px}.card .v{font-size:22px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.toolbar{position:sticky;top:0;z-index:20;background:#fafafa;padding:10px 0 12px;border-bottom:1px solid #e8e8e8;margin-bottom:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input[type=text]{flex:1;min-width:220px;padding:8px 12px;border:1px solid #d8d8d8;border-radius:8px;font-size:14px;background:#fff;outline:none}
input[type=text]:focus{border-color:#5b9bd5}
button{padding:7px 12px;border:1px solid #d8d8d8;background:#fff;border-radius:8px;cursor:pointer;font-size:13px;color:#444}
button:hover{border-color:#5b9bd5;color:#185FA5}
label.chk{font-size:13px;color:#555;display:flex;gap:5px;align-items:center;cursor:pointer;white-space:nowrap}
#cnt{font-size:12px;color:#888;white-space:nowrap}
.nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.nav a{font-size:13px;padding:5px 11px;border:1px solid #e2e2e2;border-radius:16px;background:#fff;color:#555;text-decoration:none}
.nav a:hover{border-color:#5b9bd5;color:#185FA5}
section.cat{margin-bottom:26px;scroll-margin-top:74px}
.cat-h{display:flex;align-items:baseline;gap:12px;border-bottom:2px solid #e6e6e6;padding-bottom:7px;margin-bottom:12px;flex-wrap:wrap}
.cat-h h2{font-size:17px;font-weight:600;margin:0}
.cat-h .m{font-size:12px;color:#888}
details.theme{background:#fff;border:1px solid #eaeaea;border-radius:10px;margin-bottom:8px;overflow:hidden}
details.theme[open]{border-color:#d4e2f2}
summary{padding:10px 14px;cursor:pointer;list-style:none;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
summary::-webkit-details-marker{display:none}
summary:before{content:"▸";color:#aaa;font-size:12px}
details[open]>summary:before{content:"▾"}
.th-name{font-weight:600;font-size:14px;min-width:110px}
.th-m{font-size:12px;color:#888;font-variant-numeric:tabular-nums}
.bar{height:5px;background:#eef4fb;border-radius:3px;flex:1;min-width:70px;max-width:220px;overflow:hidden}
.bar i{display:block;height:100%;background:#85B7EB}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#888;font-weight:400;font-size:12px;padding:7px 10px;background:#fbfbfb;border-top:1px solid #efefef;border-bottom:1px solid #efefef;white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid #f4f4f4}
tr.row:hover td{background:#f7fbff}
td.code{font-family:ui-monospace,Consolas,monospace;color:#185FA5;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.idx{color:#999;font-size:12px}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;background:#f0f0f0;color:#777}
.tag.sse{background:#eef4fb;color:#185FA5}.tag.szse{background:#fdf1e8;color:#a55a20}
.st-live{color:#1d9e75;font-size:12px}.st-off{color:#bbb;font-size:12px;white-space:nowrap}
.note{font-size:12px;color:#999;margin:6px 0 14px;line-height:1.8}
footer{margin-top:32px;padding-top:14px;border-top:1px solid #e8e8e8;color:#999;font-size:12px}
.empty{padding:16px;color:#999;font-size:13px;display:none}
@media(max-width:640px){.idxcol,.srccol{display:none}.wrap{padding:14px 10px 60px}td,th{padding:6px}}
@media print{.toolbar,.nav{display:none}details.theme{break-inside:avoid}details:not([open])>.tbl{display:block}}"""

JS = r"""const qEl=document.getElementById('q'), liveEl=document.getElementById('liveOnly');
const allRows=[...document.querySelectorAll('tr.row')];
const allThemes=[...document.querySelectorAll('details.theme')];
const allCats=[...document.querySelectorAll('section.cat')];
const cntEl=document.getElementById('cnt');
function apply(){
  const q=qEl.value.trim().toLowerCase(), live=liveEl.checked;
  let n=0;
  allRows.forEach(tr=>{
    const ok=(!q||tr.dataset.q.indexOf(q)>=0)&&(!live||tr.dataset.live==='1');
    tr.style.display=ok?'':'none'; if(ok)n++;
  });
  allThemes.forEach(d=>{
    const vis=[...d.querySelectorAll('tr.row')].some(tr=>tr.style.display!=='none');
    d.style.display=vis?'':'none';
    if(q&&vis)d.open=true;
  });
  allCats.forEach(s=>{
    s.style.display=[...s.querySelectorAll('details.theme')].some(d=>d.style.display!=='none')?'':'none';
  });
  cntEl.textContent=q||live?('匹配 '+n+' 只'):('共 '+allRows.length+' 只');
}
qEl.addEventListener('input',()=>{if(!qEl.value.trim())allThemes.forEach(d=>d.open=false);apply()});
liveEl.addEventListener('change',apply);
document.getElementById('expand').onclick=()=>allThemes.forEach(d=>{if(d.style.display!=='none')d.open=true});
document.getElementById('collapse').onclick=()=>allThemes.forEach(d=>d.open=false);
document.getElementById('clear').onclick=()=>{qEl.value='';liveEl.checked=false;allThemes.forEach(d=>d.open=false);apply()};
apply();"""

esc = lambda s: html.escape(str(s), quote=True)
fnum = lambda v: "—" if v is None else f"{v:,.2f}"


def load():
    m = pd.read_csv(MAP, dtype=str)
    m["code"] = m["code"].str.zfill(6)
    fs = sorted(DAILY.glob("share_*.parquet"))
    if not fs:
        raise SystemExit("data/daily 里没有份额文件")
    cols = ["trade_date", "code", "name", "share"]
    a = pd.concat([pd.read_parquet(f, columns=cols) for f in fs], ignore_index=True)
    a["code"] = a["code"].astype(str).str.zfill(6)
    a["trade_date"] = a["trade_date"].astype(str).str[:10]
    last = pd.read_parquet(fs[-1], columns=["code", "name", "share", "source"])
    last["code"] = last["code"].astype(str).str.zfill(6)
    b = fs[-1].stem[6:14]
    return m, a, last, f"{b[:4]}-{b[4:6]}-{b[6:]}", len(fs)


def make_item(cat, theme, code, idx, L, G):
    inf = G.loc[code] if code in G.index else None
    cur = L.loc[code] if code in L.index else None
    return dict(
        cat=cat, theme=theme, code=code, idx=idx,
        name=str(cur["name"]) if cur is not None else (str(inf["name"]) if inf is not None else ""),
        share=round(float(cur["share"]), 4) if cur is not None else None,
        source=str(cur["source"]) if cur is not None else None,
        first=str(inf["first"]) if inf is not None else "",
        last=str(inf["last"]) if inf is not None else "",
        live=cur is not None,
    )


def row_html(r, total):
    q = " ".join([r["code"], r["name"], r["idx"], r["theme"], r["cat"]]).lower()
    pct = f"{(r['share'] or 0) / total * 100:.1f}%" if (total and r["share"] is not None) else "—"
    st = ('<span class="st-live">在档</span>' if r["live"]
          else f'<span class="st-off">已停止披露 · {esc(r["last"])}</span>')
    src = (f'<span class="tag {esc(r["source"])}">'
           f'{"沪" if r["source"] == "sse" else "深"}</span>') if r["source"] else "—"
    return (f'<tr class="row" data-q="{esc(q)}" data-live="{1 if r["live"] else 0}">'
            f'<td class="code">{esc(r["code"])}</td><td>{esc(r["name"])}</td>'
            f'<td class="idx idxcol">{esc(r["idx"])}</td><td class="num">{fnum(r["share"])}</td>'
            f'<td class="num">{pct}</td><td class="srccol">{src}</td><td>{st}</td></tr>')


def table_html(items, total):
    head = ('<table><thead><tr><th>代码</th><th>ETF简称</th><th class="idxcol">跟踪指数 / 标签</th>'
            '<th style="text-align:right">份额(亿份)</th><th style="text-align:right">占主题</th>'
            '<th class="srccol">交易所</th><th>状态</th></tr></thead><tbody>')
    return head + "".join(row_html(r, total) for r in items) + "</tbody></table>"


def build(out: Path):
    m, a, last, date, n_files = load()
    L = last.set_index("code")
    G = a.groupby("code").agg(first=("trade_date", "min"),
                              last=("trade_date", "max"), name=("name", "last"))
    cats = list(dict.fromkeys(m["category"]))
    tord = {c: list(dict.fromkeys(m[m.category == c]["theme"])) for c in cats}

    rows = [make_item(r["category"], r["theme"], r["code"], r["name"], L, G)
            for _, r in m.iterrows()]
    un = [make_item("未分类", "未分类", c, "—", L, G)
          for c in sorted(set(a["code"]) - set(m["code"]))]
    un.sort(key=lambda x: -(x["share"] or 0))

    by = {}
    for r in rows:
        by.setdefault((r["cat"], r["theme"]), []).append(r)
    for k in by:
        by[k].sort(key=lambda x: -(x["share"] or -1))
    tt = {k: sum(x["share"] or 0 for x in v) for k, v in by.items()}
    ct = {c: sum(v for (cc, t), v in tt.items() if cc == c) for c in cats}
    n_theme = sum(len(v) for v in tord.values())
    n_total = len(rows) + len(un)
    total_share = float(last["share"].sum())

    P = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>ETF 标的分类清单</title><style>", CSS, "</style></head><body><div class='wrap'>",
         "<h1>ETF 标的分类清单</h1>",
         f"<div class='sub'>数据截至 {esc(date)} · 来源：上交所 / 深交所官方份额 · "
         f"份额单位：亿份（已按折算复权）</div><div class='cards'>",
         f"<div class='card'><div class='k'>板块</div><div class='v'>{len(cats)}</div></div>",
         f"<div class='card'><div class='k'>主题</div><div class='v'>{n_theme}</div></div>",
         f"<div class='card'><div class='k'>已分类标的</div><div class='v'>{len(rows):,}</div></div>",
         f"<div class='card'><div class='k'>未分类标的</div><div class='v'>{len(un)}</div></div>",
         f"<div class='card'><div class='k'>最新在档</div><div class='v'>{len(last):,}</div></div>",
         f"<div class='card'><div class='k'>在档合计份额</div><div class='v'>{total_share:,.0f}</div></div>",
         "</div><div class='toolbar'>",
         "<input type='text' id='q' placeholder='搜索代码 / 简称 / 指数 / 主题 / 板块…'>",
         "<label class='chk'><input type='checkbox' id='liveOnly'> 仅显示在档</label>",
         "<button id='expand'>展开全部</button><button id='collapse'>折叠全部</button>"
         "<button id='clear'>重置</button>",
         f"<span id='cnt'>共 {n_total} 只</span></div><div class='nav'>",
         "".join(f"<a href='#cat{i}'>{esc(c)}</a>" for i, c in enumerate(cats)),
         f"<a href='#unmapped'>未分类 ({len(un)})</a></div>"]

    for i, c in enumerate(cats):
        ths = tord[c]
        nc = sum(len(by.get((c, t), [])) for t in ths)
        nl = sum(1 for t in ths for r in by.get((c, t), []) if r["live"])
        P.append(f"<section class='cat' id='cat{i}'><div class='cat-h'><h2>{esc(c)}</h2>"
                 f"<span class='m'>{len(ths)} 个主题 · {nc} 只标的 · 在档 {nl} 只 · "
                 f"合计 {ct[c]:,.1f} 亿份</span></div>")
        for t in ths:
            items = by.get((c, t), [])
            v = tt.get((c, t), 0)
            pct = v / ct[c] * 100 if ct[c] else 0
            P.append(f"<details class='theme'><summary><span class='th-name'>{esc(t)}</span>"
                     f"<span class='th-m'>{len(items)} 只 · {v:,.1f} 亿份 · 占板块 {pct:.1f}%</span>"
                     f"<span class='bar'><i style='width:{min(pct, 100):.1f}%'></i></span></summary>"
                     f"<div class='tbl'>{table_html(items, v)}</div></details>")
        P.append("</section>")

    ut = sum(r["share"] or 0 for r in un)
    P.append("<section class='cat' id='unmapped'><div class='cat-h'><h2>未分类标的</h2>"
             f"<span class='m'>{len(un)} 只 · 在档 {sum(1 for r in un if r['live'])} 只 · "
             f"合计 {ut:,.1f} 亿份</span></div>"
             "<div class='note'>这些标的有份额数据但未进入看板主题。包含此前主动剔除的"
             "信用债 / 区域主题 / 金融综合、2026-11-08 移除的「港股跨境科技」65 只与"
             "「科技综合」20 只（跨行业泛科技，塞进单一赛道会污染语义），以及尚未归类的"
             "新上市基金。它们不参与看板统计。</div>")
    P.append(f"<details class='theme' open><summary><span class='th-name'>全部未分类</span>"
             f"<span class='th-m'>{len(un)} 只 · {ut:,.1f} 亿份</span></summary>"
             f"<div class='tbl'>{table_html(un, ut)}</div></details></section>")
    P.append(f"<footer>生成自 src/etf_map.csv 与 data/daily/ 全部 {n_files} 个交易日文件 · "
             f"份额口径与看板一致</footer>")
    P.append("</div><script>" + JS + "</script></body></html>")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(P), encoding="utf-8")
    print(f"已生成 {out}  ({out.stat().st_size / 1024:.1f} KB)")
    print(f"数据截至 {date} | {len(cats)} 板块 / {n_theme} 主题 / "
          f"已分类 {len(rows):,} / 未分类 {len(un)} / 共 {n_total} 只")


def main():
    ap = argparse.ArgumentParser(description="生成 ETF 标的分类清单 HTML")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径（默认工作区根/ETF分类清单.html）")
    build(Path(ap.parse_args().out))


if __name__ == "__main__":
    main()
