# A 股持仓行情数据管道

`scripts/portfolio_market_data.py` 独立于主分析流程，为外部 Agent/GPT 生成以下稳定 JSON：

- `data/portfolio/premarket.json`
- `data/portfolio/midday.json`
- `data/portfolio/close.json`
- `data/portfolio/intraday.json`

四个阶段的语义互不替代，正式文件只能在对应北京时间窗口写入：

- `premarket`：`00:00 <= time <= 08:50`；
- `midday`：`11:30 <= time < 13:00`；
- `close`：`time >= 15:00`；
- `intraday`：`09:30 <= time <= 11:30` 或 `13:00 <= time < 15:00`。

窗口外执行会返回 `PhaseTimeError`，不会新建或覆盖正式 JSON。采集脚本可在盘中运行，但不会把盘中数据写入 `close.json`。

## 持仓与关注配置

证券池只从 `config/portfolio.json` 读取，配置只保存六位证券代码及 `holdings` / `watchlist` 分类，不保存名称、成本、数量、金额、盈亏、账户、券商或交易历史。名称仍由行情源按代码动态解析。

```json
{
  "version": 1,
  "holdings": ["688825", "300442", "688012", "300604", "300274", "159567", "159967"],
  "watchlist": []
}
```

代码必须是六位数字；同一分类中的重复项会按首次出现去重，同一代码同时位于两个分类时直接失败。配置缺失、JSON 损坏、出现未支持字段或非法代码时不会回退到 Python 默认值。两个列表都为空时仍生成四个基准指数，并在顶层输出 `portfolio_status: "empty"`。

行情输出继续使用兼容的 `stocks` 数组，每项通过 `tracking_type: "holding" | "watchlist"` 标记分类；顶层同时提供 `holdings_codes` 和 `watchlist_codes` 供下游快速读取。两类证券使用完全相同的行情与指标字段。

可使用轻量管理脚本原子更新配置：

```bash
python scripts/manage_portfolio.py add-holding 600519
python scripts/manage_portfolio.py remove-holding 300442
python scripts/manage_portfolio.py add-watchlist 002594
python scripts/manage_portfolio.py remove-watchlist 002594
python scripts/manage_portfolio.py show
```

`add-holding` 会将同一代码从 watchlist 移除；`add-watchlist` 遇到已有 holding 会明确拒绝。删除 holding 不会自动转入 watchlist。脚本只执行明确命令，不解析或猜测自然语言交易意图。

## 数据源与降级

脚本只注入无需 Token 的现有数据源，历史日线依次使用 Efinance、AkShare、PyTDX、Baostock、Tencent；实时行情依次使用 Efinance、AkShare 东方财富、AkShare 新浪、AkShare 腾讯。指数由同一组免费 Fetcher 获取并逐项补齐。单源失败会继续尝试后续源；全部失败时证券仍保留在 JSON 中，`source` 为 `unavailable`、`status` 为 `error`，详细原因写入顶层 `errors`。

每条证券记录同时包含：

- `source`：当前价格字段实际采用的数据源；
- `source_details.history`：技术指标所用日线源；
- `source_details.realtime`：盘中/盘后覆盖当前 bar 的实时源；
- `source_details.volume_ratio`：只在实时源原生返回标准量比时记录该源，否则为 `null`；
- `fetched_at`：本程序抓取该条记录的北京时间，始终存在；
- `provider_timestamp`：上游明确返回的行情时间；上游未提供时为 `null`；
- `data_timestamp`：为兼容保留；有 `provider_timestamp` 时与其相同，否则为 `null`，不使用 `generated_at` 伪装行情时间；
- `freshness_status`：`fresh` / `stale` / `unknown`。无供应商时间时固定为 `unknown`；盘中/午盘供应商时间与抓取时间相差不超过 15 分钟时为 `fresh`，收盘数据需明确更新到当日 15:00 后才为 `fresh`；
- `data_date`：该证券实际最新 bar 日期，可能因停牌或源延迟早于顶层 `data_date`。

脚本进程每次运行都重新创建数据源对象，不读取落盘缓存。进程内的全市场快照缓存只用于同一批配置代码，避免重复请求；不会把既有 JSON 当成实时数据复用。

## 指标口径

- `MA5/10/20/60`：截至当前观测 bar 的最近 N 个交易日收盘价算术平均；不足 N 根返回 `null`。
- `return_Nd`：`(当前观测收盘价 / N 个交易日前收盘价 - 1) * 100`；因此 N 日收益至少需要 N+1 根 bar。
- `volume_ratio`：只使用上游实时行情源原生提供的标准量比；无可信原生字段时为 `null`。
- `volume_vs_5d_avg`：当前观测 bar 成交量 / 前 5 个完整交易日平均成交量；该指标不等同于行情软件的盘中量比。
- `volume_vs_20d_avg`：当前观测 bar 成交量 / 前 20 个完整交易日平均成交量。
- `amplitude`：优先使用实时源；缺失时为 `(最高价 - 最低价) / 昨收 * 100`。
- 盘前价格、涨跌幅和 OHLCV 固定取上一已完成交易日日线；实时查询仅可用于解析当前证券名称。

## 运行与调度

```bash
python scripts/portfolio_market_data.py --phase premarket
python scripts/portfolio_market_data.py --phase midday
python scripts/portfolio_market_data.py --phase close
python scripts/portfolio_market_data.py --phase intraday
python scripts/portfolio_market_data.py --phase intraday --config config/portfolio.json
```

非交易日默认不写文件。正常模式下 `all` 总是拒绝且不写任何文件；它只允许通过以下显式诊断命令展开 `premarket`、`midday`、`close`：

```bash
python scripts/portfolio_market_data.py --phase all --allow-phase-time-override
```

`--allow-phase-time-override` 或 `--force` 属于诊断运行，未指定 `--output-dir` 时写入被 git 忽略的 `data/portfolio/diagnostics/`。即使显式指定，诊断运行也拒绝以正式 `data/portfolio/` 为输出目录。其他独立临时目录可以通过 `--output-dir` 指定。

独立 workflow `.github/workflows/portfolio-market-data.yml` 使用 UTC cron，对应北京时间：

- `22:37 UTC` 周日至周四 = 次日 `06:37 Asia/Shanghai` 周一至周五：premarket（距 `08:50` 窗口上限 133 分钟，为 GitHub scheduled workflow 排队预留时间）
- `03:35 UTC` = `11:35 Asia/Shanghai`：midday
- `07:10 UTC` = `15:10 Asia/Shanghai`：close

`workflow_dispatch` 只支持 `premarket`、`midday`、`close`、`intraday`，不暴露 `all` 或诊断覆盖开关。`intraday` 不增加 cron，只能手动运行。生成后 workflow 提交实际变化的 JSON；若交易日判断跳过或内容无变化，则不产生 commit。

PR 中的 `Portfolio Market Data Smoke` 使用干净 Python 3.11，只安装 `.github/requirements-portfolio-pipeline.txt`，再执行 `pip check` 和 `python scripts/portfolio_market_data.py --help`，用于阻止轻量依赖清单与实际启动 import 链再次漂移。

## JSON 时间与量比示例

```json
{
  "generated_at": "2026-08-14T10:15:00+08:00",
  "market_phase": "intraday",
  "holdings_codes": ["159567"],
  "watchlist_codes": ["600519"],
  "stocks": [{
    "code": "159567",
    "tracking_type": "holding",
    "volume_ratio": 0.68,
    "volume_vs_5d_avg": 0.35,
    "volume_vs_20d_avg": 0.34,
    "source_details": {
      "history": "BaostockFetcher",
      "realtime": "akshare_em",
      "volume_ratio": "akshare_em"
    },
    "fetched_at": "2026-08-14T10:15:00+08:00",
    "provider_timestamp": null,
    "data_timestamp": null,
    "freshness_status": "unknown"
  }]
}
```

## 已知限制

- 免费网页接口可能被限流、调整字段或短时不可用；错误会显式进入 JSON，不承诺每个扩展字段始终有值。
- 多数免费实时源不提供供应商原始时间，此时 `provider_timestamp` 和 `data_timestamp` 为 `null`，`freshness_status` 为 `unknown`；只能确认 `fetched_at` 是抓取时间。
- 阶段窗口只保护报告语义，不判断上游免费源是否延迟；是否可用仍应结合 `provider_timestamp`、`freshness_status` 和 `status` 判断。
- workflow 向当前分支提交 JSON；若目标分支保护规则禁止 GitHub Actions 直接推送，需要仓库管理员允许该 bot，或改为由独立 PR 接收快照更新。
