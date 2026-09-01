# A 股持仓行情数据管道

`scripts/portfolio_market_data.py` 独立于主分析流程，为外部 Agent/GPT 生成以下稳定 JSON：

- `data/portfolio/premarket.json`
- `data/portfolio/midday.json`
- `data/portfolio/close.json`
- `data/portfolio/intraday.json`

四个阶段的语义互不替代，正式文件只能在对应北京时间窗口写入：

- `premarket`：`00:00 <= time <= 08:50`；
- `midday`：`11:30 <= time < 13:00`；
- `close` live：`15:00 <= time < 18:00`；若 scheduled run 严重迟到，允许以 `recovery` 模式在窗口后补齐最近一个已完成交易日；
- `intraday`：`09:30 <= time <= 11:30` 或 `13:00 <= time < 15:00`。

premarket/midday 的 scheduled run 在窗口外会返回 `PhaseTimeError`，不会新建或覆盖正式 JSON。两个受控例外都只能读取完整日线：`workflow_dispatch phase=premarket` 在快照 stale/missing/invalid 时以 `recovery` 模式补齐目标日的最近已完成交易日；close recovery 补齐目标收盘日。两者的 `generated_at` 均保留真实补齐时间，不读取实时行情，也不伪造生成时间。采集脚本可在盘中运行，但不会把盘中数据写入 `close.json`。

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

脚本只注入无需 Token 的现有数据源，历史日线依次使用 Efinance、AkShare、PyTDX、Baostock、Tencent；实时行情依次使用 Efinance、AkShare 东方财富、AkShare 新浪、AkShare 腾讯。live 指数由同一组免费 Fetcher 获取并逐项补齐；close recovery 的指数改用 AkShare 东方财富历史指数日线，并以 AkShare 新浪历史指数日线备用。单源失败会继续尝试后续源；全部失败时证券仍保留在 JSON 中，`source` 为 `unavailable`、`status` 为 `error`，详细原因写入顶层 `errors`。

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
- 成交量优先统一换算为“股”。当免费源缺少成交量单位元数据时，管道将原值、100 倍和 1/100 与前 5 日/20 日历史分布比较，仅在 100 倍候选具有明显置信优势时归一化；真实 5～10 倍放量保留原值。无法可靠判断时，两个相对成交量指标为 `null`，证券标记 `partial` 并注明 `suspected volume unit mismatch`。
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

premarket/midday/intraday 在非交易日默认不写文件；close recovery 可在非交易日补齐最近缺失的已完成交易日。正常模式下 `all` 总是拒绝且不写任何文件；它只允许通过以下显式诊断命令展开 `premarket`、`midday`、`close`：

```bash
python scripts/portfolio_market_data.py --phase all --allow-phase-time-override
```

`--allow-phase-time-override` 或 `--force` 属于诊断运行，未指定 `--output-dir` 时写入被 git 忽略的 `data/portfolio/diagnostics/`。即使显式指定，诊断运行也拒绝以正式 `data/portfolio/` 为输出目录。其他独立临时目录可以通过 `--output-dir` 指定。

独立 workflow `.github/workflows/portfolio-market-data.yml` 使用 UTC cron，对应北京时间：

- `22:37 / 23:07 / 23:37 UTC` 周日至周四 = 次日 `06:37 / 07:07 / 07:37 Asia/Shanghai` 周一至周五：premarket 主任务与两次独立补偿（均在 `08:50` 窗口内）
- `02:53 / 03:07 / 03:21 UTC` = `10:53 / 11:07 / 11:21 Asia/Shanghai`：midday 主任务与两次独立补偿；任一任务在 `11:32` 前启动时等待，`11:32 <= time < 13:00` 立即生成，已有 fresh snapshot 时幂等退出，`13:00` 后输出 `SCHEDULE_MISSED_PHASE_WINDOW` 并失败
- `06:23 UTC` = `14:23 Asia/Shanghai`：close 主任务，在 `15:05` 前启动时等待至 `15:05`
- `07:43 / 08:43 / 09:03 UTC` = `15:43 / 16:43 / 17:03 Asia/Shanghai`：close 三次自动补偿

独立 workflow `.github/workflows/portfolio-close-watchdog.yml` 不依赖上述 close run 是否曾被 GitHub 创建。它在北京时间 `16:17 / 17:17 / 18:17 / 19:17 / 20:17`（UTC `08:17 / 09:17 / 10:17 / 11:17 / 12:17`）执行状态驱动检查：目标日期不是交易日时输出 `NON_TRADING_DAY`；远端 close 已通过正式契约时输出 `CLOSE_ALREADY_FRESH` 且不调用生成器；missing/stale/invalid 时使用完整日线 recovery，验证、提交并重新读取远端，成功输出 `CLOSE_RECOVERED`，否则以 `CLOSE_RECOVERY_FAILED` 结束。多个 watchdog 是独立恢复机会，不是无条件重复生成任务。

三个正式阶段统一使用 `scripts/portfolio_snapshot_readiness.py`。workflow 先由 nominal cron slot 解析目标业务日期，再计算期望 `data_date`，不会再用 runner 实际启动日期代替任务日期。fresh 时三阶段都跳过 generator 和 commit；missing/stale/invalid 时才进入阶段 gate、生成、contract validator 和提交。premarket/midday 严重迟到会明确失败，绝不使用下午或收盘行情补上午快照。

close 的期望 `data_date` 是 nominal slot 对应时点“最近一个已经完成收盘的 A 股交易日”。例如周五任务延迟到周六 02:00，仍补周五：顶层 `generation_mode: "recovery"`，所有证券和基准只来自周五完整日线，`generated_at` 是真实周六时间。周一早上若 close 仍停留在上周四，则相对最近完成的上周五为 stale，允许 recovery；当前自然日非交易日不再直接阻止补齐。

新生成的快照仍必须通过“本次执行 30 分钟内”的严格 validator：顶层不得有未解决 `errors`；每只持仓/关注证券的 `latest_price`、`prev_close`、`open`、`high`、`low`、`volume`、`amount` 必须为有限数值，并满足正价格、`high >= low`、成交量/成交额非负。历史不足导致技术指标为 `null` 仍允许以 `partial` 通过，这与核心行情缺失是两个不同契约。recovery 额外要求证券/基准 `data_date` 等于目标交易日，且证券 `source_details.realtime` 必须为 `null`。生成器执行后若文件无 diff，系统重新检查 freshness；仍不 fresh 时输出 `SNAPSHOT_NOT_UPDATED` 并失败，三个正式 phase 不再有绿色特判。

`workflow_dispatch` 只支持 `premarket`、`midday`、`close`、`intraday`，不暴露 `all` 或诊断覆盖开关。手动 premarket 先做 readiness：fresh 时 no-op；stale/missing/invalid 时进入 `recovery`，即使已过 08:50 也可用目标日前一个已完成交易日的完整日线补齐。scheduled premarket 仍严格受 08:50 截止保护；midday/intraday 的原有时间语义不变，`intraday` 不增加 cron。

concurrency 按 phase 隔离：同 phase 的 primary/fallback 串行，不同 phase 互不阻塞。每个 job 在 readiness 前先 fast-forward 到远端最新分支，commit 前再次检查远端同 phase freshness；提交只 stage 当前 phase JSON，并在 `pull --rebase` 后最多尝试 push 三次。远端已由另一个任务写入合法 snapshot 时 no-op，不覆盖或重复提交。

正常 close 与 close watchdog 使用同一个 `portfolio-market-data-${ref}-close` concurrency group，因此即使正常任务和 watchdog 同时获得 runner，也会串行执行；后获得执行权的一方会再次检查远端 freshness。watchdog 的 nominal cron 日期决定目标交易日，所以周一的 20:17 任务即使延迟到周二凌晨，仍只会尝试补周一完整日线，不会因 runner 的自然日期改变目标。

每次 workflow 都在 Step Summary 记录 nominal schedule slot、目标业务日期、期望 `data_date`、cutoff、实际北京时间、lateness minutes、当前 snapshot、readiness、generation mode、validator、git diff、commit 及最终远端 freshness。这可以区分“GitHub 未创建 run”、“scheduler 严重迟到错过窗口”、“run 触发但快照已新鲜”、“生成失败”、“契约失败”和“push 失败”。

新 JSON 在 commit 前必须通过正式契约校验：`market_phase`、`timezone`、当日 `generated_at`、阶段时间窗口、`data_date`、持仓/关注列表与 `config/portfolio.json` 及 `tracking_type` 都必须一致。配置证券池非空时 `stocks` 不得整体缺失。交易日判断跳过、未生成新文件或契约失败时，日志输出 `current snapshot unavailable`，不会把旧 JSON commit 成当日成功。

下游不应将“文件存在”视为“今日可用”，必须同时校验 `generated_at + data_date + market_phase`。根据当前 GitHub Actions 已观测的延迟与约 4–5 分钟数据生成耗时，建议午盘报告约 `11:45` 读取，收盘报告约 `15:25` 读取；仍需先做上述契约判断，不应假设 Actions 绝对准时。

### 消费前 readiness 检查

外部 GPT/Agent 应先刷新或重新读取 GitHub `main`，再对对应正式文件执行只读检查：

```bash
python scripts/check_portfolio_snapshot_ready.py --phase premarket
python scripts/check_portfolio_snapshot_ready.py --phase midday
python scripts/check_portfolio_snapshot_ready.py --phase close
```

脚本只读取 `data/portfolio/{phase}.json` 和 `config/portfolio.json`，不会抓取行情、修改或删除 snapshot，也不会创建另一套行情管道。`ready=true` 仅在文件存在，阶段、时区、目标业务日期、预期 `data_date`、配置证券池和完整正式 snapshot contract 全部通过时返回；close recovery 的 `generated_at` 可以晚于 `data_date`，但必须是真实生成时间且不得早于目标交易日收盘。消费者以 JSON 中的 `ready` 为判断依据；旧于期望交易日的文件返回 `stale_snapshot`，文件不存在返回 `missing_snapshot`，其余契约错误统一返回 `invalid_snapshot`。旧文件不会被删除或冒充新数据。

推荐读取与重试策略：

- premarket：08:00 后开始读取；未 ready 时每 5 分钟重试，最晚到 08:50；
- midday：建议 11:45 开始读取；未 ready 时每 5 分钟重试，最晚到 12:15；
- close：建议 15:25 开始读取；未 ready 时每 5 分钟重试，最晚到 16:00。

readiness 检查复用正式 validator，但不使用“生成后 30 分钟内”这一 commit 阶段限制，因此合法 midday 快照在 12:15 仍可判定 ready；日期、阶段窗口、证券池、核心行情字段和基准契约不会放宽。close readiness 按最近已完成交易日判断，允许识别晚生成但契约合法的 recovery 文件。

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
- 多 cron 能降低单次 scheduled event 被 dropped 的风险，但 GitHub Scheduler 仍可能同时延迟或丢弃多个 event；仓库代码无法承诺绝对准时。premarket/midday 一旦所有 fallback 都晚于业务 cutoff，只能明确失败，不能事后伪造时点快照。
- close watchdog 将恢复判断从“某个 close cron 是否准时”改成“目标交易日的合法 close 是否已存在”，但 GitHub 仍可能同时丢弃主 workflow 和全部 watchdog event；此时只能通过 `workflow_dispatch phase=close` 或手动触发 watchdog 恢复。
