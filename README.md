# ETF 份额每日自动追踪

每个交易日盘后自动采集全市场 ETF 份额，按主题聚合成 N-Tab HTML 看板，数据存档于 git，看板发布到 GitHub Pages。

## 工作流

```
每天 UTC 11:00（北京 19:00）
  → GitHub Actions 自动触发
  → collect.py  采集上交所/深交所/东财三源 ETF 份额（T+1）
  → build_report.py  按 etf_map.csv 聚合主题，生成 docs/index.html
  → git commit data/daily/ + docs/
  → 发布到 gh-pages 分支 → GitHub Pages 手机/电脑随时看
```

非交易日自动跳过（零提交）。也可在 Actions 页面点 "Run workflow" 手动补跑。

## 首次配置（4 步，约 2 分钟）

1. **建仓库**：在 GitHub 建一个空仓库（公开/私有均可），把本目录全部文件 push 上去：
   ```bash
   git init && git add -A && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```
2. **开权限**：仓库 Settings → Actions → General → Workflow permissions，选 **"Read and write contents"**。
3. **开 Pages**：仓库 Settings → Pages → Source 选 **"Deploy from a branch"** → Branch 选 `gh-pages` / `(root)` → Save。
4. **手动触发一次**：Actions → "etf-share-daily" → "Run workflow"，确认三个步骤全绿。之后每天自动跑，看板地址：`https://<你的用户名>.github.io/<仓库名>/`。

## 本地用法

```bash
pip install -r requirements.txt

python src/collect.py                          # 采集今天（非交易日自动退出）
python src/collect.py --date 20261016          # 指定日期
python src/collect.py --backfill 20250101:20261016   # 区间回填历史
python src/build_report.py                     # 生成看板（月频）
python src/build_report.py --freq daily        # 日频视图
python src/check_splits.py                     # 核对疑似份额折算（自动拉净值判定）
python src/check_splits.py --append            # 确认是折算的，写入 splits.csv
```

## 目录结构

```
├── .github/workflows/daily.yml   # 定时任务
├── src/
│   ├── collect.py                # 采集（三源：上交所/深交所/东财）+ 落库
│   ├── build_report.py           # parquet → HTML 看板（含份额折算复权）
│   ├── check_splits.py           # 折算核对：拉净值区分"拆分"与"真实申购"
│   ├── etf_map.csv               # ETF 代码 → 类别/主题 映射表
│   └── splits.csv                # 已确认的份额折算事件（白名单，需人工登记）
├── data/daily/share_YYYYMMDD.parquet   # 每日份额存档（一日一文件，git 友好）
├── docs/                         # 生成的看板（Pages 发布目录）
└── vendor/echarts.min.js         # ECharts 5.4.3（内联进单文件 HTML，离线可用）
```

## 维护映射表（重要）

看板按 `src/etf_map.csv` 把 ETF 归入 8 大类主题，**份额按主题内 ETF 求和**。
表中代码已逐个用交易所官方简称核对（2026-10-18）。细分主题（科技/资源/新能源/医药/消费）为**同题全量聚合**
（2025-01 前上市的全量标的，195 行映射），避免单只基金的渠道行为冒充主题资金流。

## 数据口径说明

- **T+1**：ETF 份额由交易所次日披露，19:00 跑任务取到的是当日收盘份额；未更新时自动回退上一交易日。
- **单位**：统一为“亿份”。
- **数据范围**：2021-01-04 起（两交易所官方源，1400 个交易日零缺口）。2021 年前 ETF 市场仅 160 余只，
  多数主题无标的，故不回填；映射标的 2021 年存活率约 1/8。
- **固定队列**：主题内只统计序列首日已上市的标的，之后上市的同题基金不进历史序列——
  否则它上市当天就会制造一次假跳升，破坏可比性（晚上市的主题起点自然晚，图上表现为断点后起线）。
- **三源优先级**：上交所 > 深交所 > 东财快照（兜底）；任一方失败不影响其他源。
- **长期排名按净增份额（亿份）**：5 年窗口下百分比会被小基数主题绑架
  （0.02 → 7.97 亿份 = +79600%），因此"长期吸金冠军/失血王"按绝对增量排序，百分比仅作辅助展示。
- **份额折算复权（重要）**：ETF 拆分/合并会让原始份额跳变，不复权就会被误读成巨额申购。
  但**真实大额申购同样会造成跳变**，仅凭"单日 >1.9 倍"无法区分，因此采用**白名单制**：
  只有 `src/splits.csv` 登记过的事件才复权，未登记的跳变一律不改数，只在构建日志告警。
  复权用**净值推导的精确折算因子**（累计净值比 ÷ 单位净值比，如 2.0000、3.0000、0.1000），
  而非观测倍率——折算日若同时有真实申赎，这样能把它保留下来。
  维护流程：看到 `[警告] 疑似折算未确认` → 跑 `python src/check_splits.py` → 确认后 `--append`。
  判定依据：真实申购的折算因子恒为 1.0000，拆分/合并则等于拆分倍数（盲测全对）。
  当前白名单 110 起（50 折算 + 60 申购），覆盖 2021-2026 全周期。原始 parquet 不修改，复权只发生在生成报告时。
- **映射已修正**：地方政府债原误用 511030（实为「公司债ETF平安」），已改为 **159816「0-4年地方债ETF鹏华」**。
- **akshare 版本**：接口随 akshare 版本变动，requirements 锁了 `1.19.1`；升级前先本地跑一次 `--diag` 确认列名。

## 常见问题

- **Actions 里采集失败**：查看 run 日志；交易所接口偶发 502 属正常，脚本内置重试，且非交易日零提交不会报错。
- **想改统计时间**：编辑 workflow 里 cron（注意是 UTC，+8 小时是北京时间）。
- **Pages 404**：确认 Settings → Pages 选了 gh-pages 分支，且 workflow 最后一次运行是绿的。
