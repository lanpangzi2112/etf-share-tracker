# -*- coding: utf-8 -*-
"""从同花顺 ETF 全量分类中提取「跟踪指数名」。

同花顺基金全称格式：基金公司 + 跟踪指数全称 + ETF[(QDII)]
  例：588170 → "华夏上证科创板半导体材料设备主题ETF" → "上证科创板半导体材料设备主题"
部分标的同花顺只给了交易所简称（无公司前缀），形如 "通用航空ETF永赢"，
需再剥离尾部基金公司名与 ETF 后缀。

输出 data/meta/ths_index.csv：code, full_name, index_name, fund_type, is_qdii
"""
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "etf-share-daily"
SRC = ROOT / "data/meta/etf_category_ths.csv"
OUT = ROOT / "data/meta/ths_index.csv"

# 基金公司名（用于剥离全称前缀 / 简称后缀）
COMPANIES = [
    "工银瑞信", "华泰柏瑞", "景顺长城", "前海开源", "西部利得", "国投瑞银", "创金合信",
    "中信保诚", "国寿安保", "新疆前海", "民生加银", "浦银安盛", "西藏东财", "信达澳亚",
    "恒生前海", "合煦智远", "渤海汇金", "上投摩根", "华夏基金", "易方达基金", "南方基金",
    "招商基金", "中邮创业", "中科沃土", "金元顺安", "富安达", "华润元大", "方正富邦",
    "国海富兰", "弘毅远方", "兴证全球", "浙商资管",
    "华夏", "易方达", "华宝", "国泰", "广发", "南方", "嘉实", "汇添富", "富国", "招商",
    "博时", "天弘", "工银", "银华", "鹏华", "建信", "中银", "平安", "大成", "华安",
    "万家", "浦银", "兴业", "国联安", "国联", "海富通", "华富", "永赢", "东财", "摩根",
    "路博迈", "贝莱德", "汇安", "泰康", "中金", "诺安", "长盛", "融通", "申万菱信",
    "中加", "德邦", "人保", "中欧", "兴银", "光大", "长信", "中海", "东方", "北信瑞丰",
    "先锋", "淳厚", "湘财", "格林", "达诚", "尚正", "百嘉", "泉果", "易米", "瑞达",
    "东海", "国融", "南华", "中邮", "中航", "华融", "江信", "金鹰", "信达", "宝盈",
    "诺德", "华商", "财通", "安信", "长城", "东方红", "九泰", "凯石", "明亚", "恒越",
    "同泰", "蜂巢", "惠升", "中融", "新沃", "兴华", "瑞和", "西藏", "兴证", "新华",
    "天治", "益民", "银河", "华泰", "中泰", "浙商", "山西", "东兴", "红塔", "英大",
    "华福", "财达", "世纪", "鑫元", "弘毅", "恒生", "南方东英",
    "中银证券", "国泰海通", "海通", "华泰证券", "中信证券", "国信证券", "招商证券",
    "广发证券", "中信建投", "银河证券", "申万宏源", "东方证券", "光大证券", "方正证券",
]
# 「恒生」「南方东英」等虽是合资机构名，但更常作为指数名的开头
# （恒生科技 / 恒生互联网 / 南方东英XX），剥离会丢失港股属性，故排除
_SKIP = {"恒生", "南方东英", "恒生前海", "东方", "南方基金", "华夏基金",
         "易方达基金", "招商基金"}
COMPANIES = [c for c in COMPANIES if c not in _SKIP]
COMPANIES = sorted(set(COMPANIES), key=len, reverse=True)
COMPANIES = sorted(set(COMPANIES), key=len, reverse=True)


def strip_prefix(name: str):
    """剥离开头的基金公司名，返回 (剩余, 公司名)。"""
    for c in COMPANIES:
        if name.startswith(c):
            return name[len(c):], c
    return name, ""


def strip_suffix(name: str) -> str:
    """剥离结尾的基金公司名（交易所简称格式：「主题ETF公司名」）。"""
    for c in COMPANIES:
        if name.endswith(c) and len(name) > len(c):
            return name[: -len(c)]
    return name


def to_index(full: str) -> str:
    """全称/简称 → 跟踪指数名。"""
    n = str(full).strip()
    n = re.sub(r"\(QDII-ETF\)|\(QDII\)", "", n)
    rest = n
    for _ in range(2):                 # 循环剥离：如「国泰海通中证…」叠了两家公司
        stripped, _ = strip_prefix(rest)
        if stripped == rest:
            break
        rest = stripped
    if rest == n:                      # 无公司前缀（交易所简称），尝试剥尾部公司名
        rest = strip_suffix(rest)
    rest = re.sub(r"ETF基金$|ETF$", "", rest).strip()
    return rest


def main():
    ths = pd.read_csv(SRC, dtype=str)
    ths["基金代码"] = ths["基金代码"].str.zfill(6)
    ths["index_name"] = ths["基金名称"].map(to_index)
    ths["is_qdii"] = ths["基金名称"].str.contains("QDII", na=False)
    out = ths[["基金代码", "基金名称", "index_name", "基金类型", "is_qdii"]].rename(
        columns={"基金代码": "code", "基金名称": "full_name", "基金类型": "fund_type"})
    out.to_csv(OUT, index=False, encoding="utf-8")

    print("提取完成：%d 行 → %s" % (len(out), OUT))
    empty = out[out["index_name"].str.len() <= 1]
    print("指数名为空或过短：%d 只" % len(empty))
    if len(empty):
        print(empty.head(10).to_string(index=False))
    print("\n样本（指数名最长的 10 只，信息量最丰富）：")
    sample = out[out["index_name"].str.len() >= 12].head(10)
    print(sample.to_string(index=False))
    print("\nQDII 数量：%d" % int(out["is_qdii"].sum()))


if __name__ == "__main__":
    main()
