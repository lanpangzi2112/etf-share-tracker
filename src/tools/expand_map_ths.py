# -*- coding: utf-8 -*-
"""按**同花顺跟踪指数**重做 ETF 主题映射表（取代 expand_map_all.py）。

与旧版（按基金简称关键词匹配）的本质区别：
1. 主匹配源是同花顺全称剥离出的**跟踪指数名**（如 159783 → "中证科创创业50"，
   旧版用交易所旧简称"双创基金ETF"会误中贵金属的"金ETF"子串）；
2. 同花顺未覆盖的标的才回落到东财简称 + 交易所历史简称；
3. QDII 分流：港股 QDII 正常归港股主题，**非港股 QDII**（标普/纳指/日经/亚太
   /东南亚）强制归「海外」，不再污染境内主题；
4. 港股行业 ETF 归对应**行业**主题（香港证券→券商、港股红利→红利低波），
   「港股」只保留真正的港股宽基（恒生指数/港股通50/恒生国企）；
5. 商品型（同花顺基金类型=其他：黄金ETF、有色/豆粕期货ETF）单独处理。

输出：src/etf_map.csv（category, theme, code, name）

**写入保护**：本表的多数细化规则来自人工裁决（拆并主题、剔除跨行业宽基、
手工 pin 归属），而规则与 CSV 长期是并行演进的。为防止「重跑一次把人工调整
冲掉」，写入前会与现有 CSV 逐主题、逐成分比对：

    python src/tools/expand_map_ths.py            # 有结构性差异则拒绝写入
    python src/tools/expand_map_ths.py --check    # 只比对，不一致退出码 1
    python src/tools/expand_map_ths.py --force    # 确认后强写
    python src/tools/expand_map_ths.py --out /tmp/x.csv   # 输出到别处试跑

「结构性差异」= 主题增删、有成分被移除、行数变动超过 40。
单纯的**新增**标的（新发 ETF）不算，会照常写入并在日志里列出。
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

def _find_root() -> Path:
    """向上定位仓库根（etf-share-daily），脚本放在任何子目录都能用。"""
    p = Path(__file__).resolve().parent
    while p.parent != p:
        if p.name == "etf-share-daily":
            return p
        p = p.parent
    raise SystemExit("找不到 etf-share-daily 仓库根")


ROOT = _find_root()
sys.path.insert(0, str(ROOT / "src"))
import build_report as B  # noqa: E402

THS_INDEX = ROOT / "data/meta/ths_index.csv"
EM_NAMES = ROOT / "data/meta/fund_names_em.csv"
OUT = ROOT / "src/etf_map.csv"

# rank 越大越细分；同一 ETF 命中多个主题时保留 rank 最大的
THEMES = {
    "宽基": {
        # 境内大盘：沪深300 / 上证50 / 中证100 / A500 / 综指 / 中证800 / 深证100 等
        "大盘": (["沪深300", "上证50", "中证100", "A500", "中证A50", "上证综指",
                  "上证综合", "中证800", "深证100", "深100", "上证180", "180治理",
                  "MSCI中国A股", "MSCI中国A50", "A50", "A100", "中证A100", "300",
                  "深证成份", "深成指", "中证700", "上证380", "上证580", "深证成指",
                  "中证全指", "国证A50", "巨潮100", "基本面50", "深证50"],
                 # 金融类宽基（180金融/内地金融等）归金融地产，不留在「大盘」
                 ["300医药", "300非周", "300周期", "300成长", "300价值", "300金融",
                  "300地产", "300能源", "300消费", "300信息", "300工业", "300公用",
                  "300材料", "300电信", "增强", "等权", "金融"], 2),
        # 中小盘：中证500/1000/2000、创业板、科创板系列、北证50
        "中小盘": (["中证500", "中证1000", "中证2000", "创业板", "创业50", "创成长",
                    "创业创新", "科创板50", "科创板100", "科创板200", "科创板综",
                    "科创50", "科创100", "科创200", "科创综", "科创创业", "双创",
                    "北证50", "中小100", "中小板", "中小300", "深证300", "国证2000", "创50", "科创"],
                   # 只排除科创板**行业/风格**主题（它们归细分主题），宽基留在本主题；
                   # 「债」用于挡住 科创债 系列（上证AAA科创债等）被「科创」误收
                   ["500医药", "500信息", "500工业", "500材料", "500成长", "500价值",
                    "500低波", "500等权", "增强策略", "科创板半导体",
                    "科创板芯片", "科创板生物", "科创板新材料", "科创板新能源",
                    "科创板工业", "科创板信息", "科创板医药", "科创板人工智能",
                    "科创板成长", "科创板价值", "科创人工智能", "科创创新药",
                    "科创半导体", "科创芯片", "科创新能源", "科创价值", "科创成长",
                    "债"], 2),
        # 真正的港股宽基（不含行业/策略词，由细分主题优先收走行业 ETF）
        "港股": (["恒生指数", "恒生中国企业", "港股通50", "中华港股通", "MSCI香港",
                    "香港恒生", "恒生综合", "恒生港股通50", "恒生"],
                   ["科技", "互联", "医疗", "医药", "生物", "红利", "低波", "高股息",
                    "消费", "证券", "非银", "保险", "银行", "金融", "地产", "央企",
                    "创新药", "教育", "汽车", "公用", "能源", "制造", "服务", "文体",
                    "成长", "价值", "精明", "港股通创新药", "港股通医疗",
                    "港股通消费", "港股通科技", "港股通互联网", "港股通非银",
                    "港股通金融", "港股通红利", "港股通50增强"], 2),
        # 海外：非港股 QDII
        "海外": (["标普500", "纳斯达克", "纳指", "日经", "德国", "法国", "东南亚",
                  "亚太", "沙特", "道琼斯", "东证", "日本", "全球", "海外", "国际",
                  "MSCI美国", "MSCI欧洲", "MSCI新兴", "MSCI全球", "富时100",
                  "越南", "印度", "巴西", "韩国", "中韩", "沪港深", "新兴亚洲",
                  "泛东南亚", "环球", "亚洲"],
                 ["医药", "互联", "中国教育", "医疗"], 3),
        "央企国企": (["央企", "国企", "国资"], ["红利", "科技", "能源", "环保", "一带一路"], 4),
        "红利低波": (["红利", "低波", "高股息", "股息"], ["港股通科技", "恒生科技"], 5),
        "自由现金流": (["现金流", "自由现金流"], [], 5),
        "风格策略": (["价值", "成长", "质量", "动量", "低贝塔", "基本面", "等权",
                      "智选", "增强策略", "ESG", "治理", "龙头", "品牌", "民企",
                      "专精特新", "新兴产业", "产业升级", "小康"],
                     ["现金流", "自由现金流"], 5),
        # 2026-11-22：删除「区域主题」（成渝/长三角/大湾区/浙江等 15 只）。
        # 这批地方主题 ETF 体量小、跟踪指数互不重叠，看板上按「未分类」处理，
        # 不再单列一个主题。若日后要恢复，把原规则加回并跑 --force 即可。
    },
    "金融地产": {
        # 2026-10-25 重构：由 6 主题并为 4 主题（券商/银行/非银金融/地产基建）。
        # 「金融科技」12 只并入非银金融；「金融综合」（180金融、港股通内地金融等
        # 跨行业混合宽基）与「其他非银金融」两个兜底主题取消，成分并入券商/非银。
        "券商": (["证券", "券商", "证券公司", "香港证券", "港股通证券", "证券投资主题"],
                 ["证券保险", "金融科技", "非银", "非银行金融"], 10),
        "银行": (["银行"], ["港股通银行", "香港银行"], 10),
        "非银金融": (["证券保险", "保险", "多元金融", "信托", "非银", "非银行金融",
                      "港股通非银", "金融科技", "金融IT", "互联网金融"],
                     ["证券指数", "香港证券"], 9),
        "地产基建": (["地产", "房地产", "基建", "建筑", "建材", "800地产", "全指建筑"],
                     ["金融服务"], 9),
    },
    "债券": {
        # 2026-10-20：原「信用债」主题（37 只，城投/公司债/可转债等）整体退出分类，
        # 只保留利率债三条线。
        "长期利率债": (["30年国债", "10年国债", "十年国债", "国债", "国开", "7-10年",
                        "5-10年", "长久期"],
                       ["地方债", "公司债", "信用", "可转债", "城投", "短融",
                        "1-3年", "3-5年", "0-4年", "政金债ETF招商"], 12),
        "短中期利率债": (["短融", "短债", "1-3年", "3-5年", "0-4年", "政金债",
                          "政策性金融债", "中债-", "1-5年"],
                         ["地方债", "公司债", "信用", "可转债", "城投", "30年",
                          "7-10年"], 12),
        "地方政府债": (["地方债", "地债"], [], 12),
    },
    "科技": {
        # 2026-11-08：「科技综合」兜底主题取消。原来它是 rank=8 的排除法产物
        # （泛科技宽基没命中任何细分赛道就落这里），但跟踪的是跨行业指数，
        # 不能塞进单一赛道。剩余标的全部退出分类体系，见下方 DROP_CODES。
        # 跨境科技排除词（恒生科技/港股通科技/纳斯达克科技/东南亚科技）
        # 同步并入各细分主题，防止原「港股跨境科技」成分被误收。
        "电子信息": (["电子", "计算机", "信息技术", "消费电子", "电子50"],
                     ["半导体", "芯片", "人工智能", "通信", "金融科技", "科创",
                      "恒生科技", "港股通科技", "沪港深科技", "港股"], 9),
        "通信": (["通信", "5G", "光模块", "光通信", "电信"], [], 10),
        "软件": (["软件", "云计算", "云", "数据", "信创", "数字经济", "信息安全",
                  "网络安全", "物联网", "人工智能ETF"],
                 ["人工智能", "半导体", "芯片"], 10),
        "人工智能": (["人工智能", "AI", "算力", "智能"],
                     ["智能汽车", "智能制造", "智能家居", "智能消费", "智能装备"], 10),
        "机器人": (["机器人", "工业母机", "智能制造", "机床", "工程机械", "机械"], [], 10),
        "军工科技": (["军工", "国防", "航天", "航空", "中证军工", "国证军工", "兵器"],
                     [], 10),
        "卫星互联网": (["卫星", "空间", "北斗"], [], 11),
        "传媒游戏": (["传媒", "游戏", "影视", "动漫", "文化", "体育", "VR", "元宇宙"],
                     [], 10),
        "半导体芯片": (["半导体", "芯片", "集成电路", "芯", "存储", "晶圆"], [], 10),
    },
    "资源": {
        "有色": (["有色", "铜", "铝", "锌", "铅", "金属"], ["稀土", "稀有金属", "黄金",
                                                        "贵金属", "钢铁", "期货"], 9),
        "钢铁": (["钢铁"], [], 10),
        "稀土产业": (["稀土", "稀有金属"], [], 11),
        "化工": (["化工", "化学", "石化", "化纤", "橡胶", "塑料", "新材料", "材料"],
                 ["能源化工期货", "建筑材料"], 10),
        "石油能源": (["石油", "油气", "能源", "燃气", "天然气"],
                     ["新能源", "清洁能源", "期货", "标普"], 10),
        "煤炭": (["煤炭"], [], 10),
        # 2026-11-08：黄金股并入贵金属，原「黄金股」主题取消。
        "贵金属": (["黄金", "上海金", "贵金属", "金", "黄金股", "黄金产业", "沪深港黄金"],
                   ["有色", "矿业", "稀土", "现金", "资金", "基金", "金融", "金属"], 10),
        "商品": (["豆粕", "商品", "期货", "有色金属期货", "能源化工期货", "饲料"],
                 ["黄金"], 11),
    },
    "新能源": {
        "新能源综合": (["新能源", "科创板新能源", "科创新能源"],
                       ["电池", "光伏", "汽车", "材料"], 8),
        "环保碳中和": (["环保", "碳中和", "低碳", "长江保护", "绿色", "环境", "碳"],
                       ["亚太低碳", "富时亚太", "绿色电力"], 9),
        # 2026-11-01：电网设备从「电力」拆出（中证电网设备主题 / 恒生A股电网设备，5 只）
        "电网设备": (["电网设备"], [], 10),
        "电力": (["电力", "电网", "公用事业", "电力公用", "水电", "火电", "核电",
                  "绿色电力"],
                 ["新能源", "电网设备"], 9),
        "电池": (["电池", "储能", "锂", "新能源电"], [], 10),
        "光伏电站": (["光伏", "太阳能"], [], 10),
        "新能源车": (["新能源车", "新汽车", "智能汽车", "汽车", "汽车零部件", "自动驾驶"],
                     ["电池", "光伏"], 10),
    },
    "医药": {
        "医药综合": (["医药", "医疗", "生物", "疫苗", "器械", "医械", "健康", "CXO",
                      "科创板生物"],
                     ["创新药", "港股", "恒生", "中药", "科创板半导体", "科创板芯片",
                      "科创板新材料", "科创板新能源", "科创板工业", "科创板信息",
                      "科创板人工智能"], 9),
        "创新药": (["创新药"], ["港股", "恒生", "香港"], 11),
        "中药": (["中药"], [], 11),
    },
    "消费": {
        "消费服务": (["消费", "旅游", "家电", "零售", "教育", "服务", "电商", "免税",
                      "纺织", "服装", "家居", "养老", "物流", "交通运输", "交运"],
                     ["食品", "饮料", "酒", "消费电子", "金融科技", "港股通互联网"], 9),
        "食品饮料": (["食品", "饮料", "乳制品", "调味品"], [], 11),
        "白酒": (["酒"], [], 11),
        "农林牧渔": (["农业", "养殖", "畜牧", "种业", "农牧", "粮食", "渔业", "猪"],
                     [], 11),
    },
}

# 非港股 QDII 关键词：命中即强制归「宽基/海外」（优先级最高）
OVERSEAS_FORCE = ["标普", "纳斯达克", "纳指", "日经", "德国", "法国", "东南亚",
                  "亚太", "沙特", "道琼斯", "东证", "日本", "越南", "印度", "中韩",
                  "泛东南亚", "富时亚太", "新兴亚洲", "全球中国教育", "TOPIX", "顶峰", "东证指数"]
# 港股标识符：命中这些词的 QDII 属港股，不强制归海外
HK_MARK = ["恒生", "港股", "香港", "H股", "中概", "国企指数", "中国企业", "中华港股通"]

# 2026-11-08 手工裁决（「科技综合」主题取消）：
# PINNED —— 名称含「科技」会被「宽基/央企国企」的排除词踢掉，故显式固定归属。
PINNED = {
    "560170": ("宽基", "央企国企"),   # 中证国新央企科技引领
    "159335": ("宽基", "央企国企"),   # 中证诚通央企科技创新
    "563050": ("宽基", "央企国企"),   # 中证国新央企科技引领
}
# DROP_CODES —— 明确退出分类体系的标的：
# 1) 原「科技综合」剩余 20 只（泛科技宽基 / 创业板科技 / 沪港深科技 / 已停止披露）。
# 2) 原「医药/港医药」中名称只含「沪港深」不含「港股/恒生/香港」的 3 只 ——
#    删掉该主题后会被「医药/创新药」的「创新药」关键词收走，故显式排除。
DROP_CODES = {
    "515000", "159807", "159909", "515580", "515750", "516050", "515860",
    "562380", "563560", "560990", "159853",
    "159773", "159777", "159987",
    "517350", "517950", "159723", "517360", "517960", "517270",
    "159748", "517110", "517120",
    # 2026-11-22：医药板块剔除全部港股/沪港深标的，与「港股口径不入 A 股板块」
    # 的规则一致。前 3 只原本挂在「港医药」主题，主题删除后会被「医药」关键词
    # 收进「医药综合」；159622 的跟踪指数在同花顺缺失，只能按简称命中「创新药」。
    "513700",   # 香港医药
    "513620",   # 港医疗指
    "517990",   # 中证沪港深500医药卫生
    "159622",   # 创新药ETF沪港深
    # 2026-11-22：原「宽基/区域主题」取消后，这两只仍会被别的关键词误收，
    # 显式排除以保持地方主题 ETF 统一按未分类处理。
    "159809",   # 大湾区ETF基金（历史简称「恒生湾区」命中宽基/港股）
    "515760",   # 中证浙江国资创新发展（「国资」命中宽基/央企国企）
}


def load_names():
    """返回 {code: [候选名称...]}，同花顺指数名优先且独占（有效时）。"""
    ths = pd.read_csv(THS_INDEX, dtype=str)
    ths["code"] = ths["code"].str.zfill(6)
    idx = dict(zip(ths["code"], ths["index_name"].fillna("")))
    ftype = dict(zip(ths["code"], ths["fund_type"].fillna("")))
    qdii = dict(zip(ths["code"], ths["is_qdii"].fillna("False").astype(str)))

    em = pd.read_csv(EM_NAMES, dtype=str)
    em["基金代码"] = em["基金代码"].str.zfill(6)
    emname = dict(zip(em["基金代码"], em["基金简称"].fillna("")))

    # 交易所历史简称（全历史并集）
    hist = {}
    for f in sorted((ROOT / "data/daily").glob("share_*.parquet")):
        d = pd.read_parquet(f, columns=["code", "name"])
        for c, n in zip(d["code"].astype(str).str.zfill(6), d["name"]):
            hist.setdefault(c, set()).add(str(n))

    out = {}
    for c in set(idx) | set(emname) | set(hist):
        i = str(idx.get(c, "")).strip()
        if len(i) > 1:                       # 同花顺指数名有效 → 单独用，避免旧简称干扰
            out[c] = [i]
        else:
            alt = []
            if emname.get(c):
                alt.append(str(emname[c]))
            alt.extend(sorted(hist.get(c, set())))
            out[c] = alt
    return out, ftype, qdii


def match(names, include, exclude):
    for n in names:
        if any(k in n for k in exclude):
            continue
        if any(k in n for k in include):
            return True
    return False


def _theme_set(df):
    return {(str(c), str(t)) for c, t in zip(df["category"], df["theme"])}


def _compare(old, new):
    """返回 (新增主题, 消失主题, 行级差异说明列表)。"""
    added = sorted(_theme_set(new) - _theme_set(old))
    removed = sorted(_theme_set(old) - _theme_set(new))
    o = {(str(r.category), str(r.theme), str(r.code)) for r in old.itertuples()}
    n = {(str(r.category), str(r.theme), str(r.code)) for r in new.itertuples()}
    lines = []
    for c, t, code in sorted(n - o):
        lines.append("   + %s/%s %s" % (c, t, code))
    for c, t, code in sorted(o - n):
        lines.append("   - %s/%s %s" % (c, t, code))
    return added, removed, lines


def main():
    ap = argparse.ArgumentParser(description="按同花顺跟踪指数重建 src/etf_map.csv")
    ap.add_argument("--out", default=str(OUT), help="输出路径（默认 src/etf_map.csv）")
    ap.add_argument("--check", action="store_true",
                    help="只比对不写入；一致退出码 0，有差异退出码 1")
    ap.add_argument("--force", action="store_true",
                    help="主题集合或行数发生结构性变化时仍然写入")
    args = ap.parse_args()
    out_path = Path(args.out)

    names, ftype, qdii = load_names()
    df = B.load_share_df()
    all_codes = set(df["code"].astype(str).str.zfill(6))
    latest = df["trade_date"].max()
    s1 = (df[df["trade_date"] == latest]
          .assign(code=df["code"].astype(str).str.zfill(6))
          .set_index("code")["share"].to_dict())

    # 每主题命中集合
    hits = {}          # (cat, theme) -> [codes]
    for cat, themes in THEMES.items():
        for theme, (inc, exc, _rank) in themes.items():
            got = [c for c in all_codes
                   if c in names and match(names[c], inc, exc)]
            hits[(cat, theme)] = got

    # 强制海外：非港股 QDII / 含海外指数词
    forced = {}
    for c in all_codes:
        if c not in names:
            continue
        ns = names[c]
        if any(k in n for n in ns for k in HK_MARK):
            continue
        if any(k in n for n in ns for k in OVERSEAS_FORCE):
            forced[c] = ("宽基", "海外")

    # 归属：按 rank 取最细分主题
    best = {}
    for (cat, theme), codes in hits.items():
        rank = THEMES[cat][theme][2]
        for c in codes:
            prev = best.get(c)
            if prev is None or rank > prev[0]:
                best[c] = (rank, cat, theme)
    for c, v in forced.items():
        best[c] = (99, v[0], v[1])          # 海外强制优先级最高
    for c, (cat, theme) in PINNED.items():
        if c in all_codes:
            best[c] = (100, cat, theme)      # 手工裁决优先于关键词匹配
    for c in DROP_CODES:
        best.pop(c, None)                    # 明确退出分类体系

    # 输出（主题内按最新份额降序）
    rows = []
    for cat in THEMES:
        for theme in THEMES[cat]:
            codes = [c for c in best if best[c][1] == cat and best[c][2] == theme]
            codes.sort(key=lambda c: -s1.get(c, 0))
            for c in codes:
                rows.append({"category": cat, "theme": theme, "code": c,
                             "name": names[c][0] if names.get(c) else ""})
    new = pd.DataFrame(rows)

    # 统计
    print("新映射表：%d 行 / %d 主题" % (len(new), new.groupby(["category", "theme"]).ngroups))
    unmapped = sorted(all_codes - set(best))
    miss_v = sum(s1.get(c, 0) for c in unmapped)
    print("未入表：%d 只，合计 %.0f 亿份（最新 %s）" % (len(unmapped), miss_v, latest.date()))
    big = [(c, names.get(c, ["?"])[0], s1.get(c, 0)) for c in unmapped if s1.get(c, 0) > 5]
    big.sort(key=lambda x: -x[2])
    if big:
        print("未入表中 >5 亿份的：")
        for c, n, v in big[:15]:
            print("   %s %-26s %8.1f 亿份" % (c, str(n)[:26], v))

    # ---------- 写入前保护 ----------
    # 规则与人工调整长期并行演进，最容易出的事故是「重跑一次把手工裁决冲掉」。
    # 这里把现有 CSV 当作已批准的基线：主题集合变了、或行数大幅变动，默认拒绝写入。
    if out_path.exists():
        old = pd.read_csv(out_path, dtype=str).fillna("")
        old["code"] = old["code"].str.zfill(6)
        new_cmp = new.copy()
        new_cmp["code"] = new_cmp["code"].astype(str).str.zfill(6)
        added, removed, lines = _compare(old, new_cmp)
        delta = len(new_cmp) - len(old)
        n_add = sum(1 for s in lines if s.strip().startswith("+"))
        n_del = sum(1 for s in lines if s.strip().startswith("-"))
        print("\n与现有表比对：%d → %d 行（%+d，新增 %d / 移除 %d）"
              % (len(old), len(new_cmp), delta, n_add, n_del))
        if added or removed:
            print("主题集合变化：新增 %d 个、消失 %d 个" % (len(added), len(removed)))
            for c, t in added:
                print("   + %s/%s" % (c, t))
            for c, t in removed:
                print("   - %s/%s" % (c, t))
        if lines:
            print("成分变动 %d 处：" % len(lines))
            for s in lines[:40]:
                print(s)
            if len(lines) > 40:
                print("   ... 另有 %d 处" % (len(lines) - 40))
        structural = bool(added or removed) or n_del > 0 or abs(delta) > 40
        if not lines and not added and not removed:
            print("与现有表完全一致，无需写入。")
            return 0
        if args.check:
            print("--check：存在差异，未写入。")
            return 1
        if structural and not args.force:
            print("\n拒绝写入：这是结构性变更（主题增删 / 有成分被移除 / 行数大幅变动）。")
            print("成分被移除通常意味着手工裁决将被冲掉，请先核对上面的 - 行。")
            print("若确认无误，请加 --force 重跑：python src/tools/expand_map_ths.py --force")
            return 2
    elif args.check:
        print("--check：目标文件不存在。")
        return 1

    new.to_csv(out_path, index=False, encoding="utf-8")
    print("已写入", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
