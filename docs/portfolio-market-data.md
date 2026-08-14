# A 股持仓行情数据管道

`scripts/portfolio_market_data.py` 独立于主分析流程，为外部 Agent/GPT 生成以下稳定 JSON：

- `data/portfolio/premarket.json`
- `data/portfolio/midday.json`
- `data/portfolio/close.json`
- `data/portfolio/intraday.json`

四个阶段的语义互不替代：`premarket` 是盘前快照，`midday` 是午盘快照，`close` 是 15:00 后的正式收盘快照，`intraday` 是 09:30–11:30 或 13:00–15:00 交易时段内的任意手动快照。采集脚本可在盘中运行，但不会把盘中数据写入 `close.json`。

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

脚本进程每次运行都重新创建数据源对象，不读取落盘缓存。进程内的全市场快照缓存只用于同一批 7 个代码，避免重复请求；不会把既有 JSON 当成实时数据复用。

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
python scripts/portfolio_market_data.py --phase all
```

非交易日默认不写文件；仅诊断时可增加 `--force`。`close` 在 15:00 前执行会返回错误且不写文件，`intraday` 在非交易时段也会拒绝；`all` 只展开三个固定报告阶段，并在写文件前统一执行时间预检。`--allow-phase-time-override` 仅供测试/诊断，workflow 不会传入该参数。

独立 workflow `.github/workflows/portfolio-market-data.yml` 使用 UTC cron，对应北京时间：

- `00:48 UTC` = `08:48 Asia/Shanghai`：premarket
- `03:35 UTC` = `11:35 Asia/Shanghai`：midday
- `07:10 UTC` = `15:10 Asia/Shanghai`：close

`workflow_dispatch` 支持 `premarket`、`midday`、`close`、`intraday`、`all`，并提供非交易日强制生成开关。`intraday` 不增加 cron，只能手动运行。生成后 workflow 提交实际变化的 JSON；若交易日判断跳过或内容无变化，则不产生 commit。

## JSON 时间与量比示例

```json
{
  "generated_at": "2026-08-14T10:15:00+08:00",
  "market_phase": "intraday",
  "stocks": [{
    "code": "159567",
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
- workflow 向当前分支提交 JSON；若目标分支保护规则禁止 GitHub Actions 直接推送，需要仓库管理员允许该 bot，或改为由独立 PR 接收快照更新。
