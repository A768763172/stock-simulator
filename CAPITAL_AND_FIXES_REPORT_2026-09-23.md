# 初始资金调整与系统修复报告（2026-09-23）

## 一、初始资金 10000 → 50000（全项目同步）

| 文件 | 改动 |
|---|---|
| `config/settings.py` | `INITIAL_CAPITAL = 50000.0`（**唯一真值源**） |
| `live_run.py` | 删除本地陈旧常量，改为 `from config.settings import SimulatorConfig, INITIAL_CAPITAL`（引用唯一真值，杜绝两处漂移） |
| `main.py` / `main_strategy.py` | 演示入口显示文案改为「5.00 万元 (50000 元)」（实际资本由 `SimulatorConfig` 统一读取，已一致） |
| `README.md` | 初始本金说明同步 |

> 其余出现 `10000`/`100000` 处均为**测试夹具**（如 `test_e2e_closed_loop.py` 用 100000 校验撮合算术）和**历史审计报告文档**，非实时初始资金逻辑，无需改动。

## 二、修复的两个真实 Bug

### Bug 1：累计收益率显示 +441.70%（严重失真）
- **根因**：`live_run.py` 第 40 行残留陈旧本地常量 `INITIAL_CAPITAL = 10000.0`，而 `account.json`/快照已是 50000。累计收益率按 `(54169.96 − 10000)/10000 = +441.70%` 计算，虚高 50 倍。
- **修复**：`live_run.py` 改为引用 `config/settings` 的唯一真值。重算后今日累计收益率正确显示 **+8.34%**（与快照层 `pnl.py` 的 `4195/50000` 口径一致）。

### Bug 2：调度守护在 09:30 前启动会漏掉整段盘中刷新
- **根因**：`scheduler.main()` 在交易日的 `now < 09:30` 分支直接 `sleep_until(15:35)`，跳过 09:30–15:00 的实时价刷新循环 → 盘中"实时采集行情"失效。
- **修复**：新增盘前分支——`if now < 09:30: log("盘前休眠至 09:30"); sleep_until(09:30); continue`。守护在 09:30 起每 60s 刷新持仓实时真价，15:35 触发当日交易+学习。

## 三、运行时脏数据清理（"从今日起模拟"）

发现一次手动 `cli.py strategy --days 20` 回填把状态污染：
- `account.json.current_day` 被改写为未来日期 **2026-10-02**；
- `snapshots.jsonl` 写入 9-24→10-16 共 30 行未来日期快照（全 0 持仓 / 50000），会污染明日的累计收益基线。

已校正为干净起步状态：
- `account.json`：`current_day=2026-09-23`、现金 50000、**0 持仓**、realized 0（契合"初始化、从今日模拟"）；
- 清空 `snapshots.jsonl` 后回填 1 条今日正确快照（day=2026-09-23, 0 持仓, 50000）；
- 清空历史 `trades.jsonl` / `risk_log.jsonl` / `learning_progress.jsonl` / `live_prices.json`；
- **保留 `learned_params.json`**（四维学习因子权重，作为学习系统的"大脑"延续）；
- `last_run_day.txt = 2026-09-23`（今日已跑，明日 09-24 自动新跑）。

## 四、实跑验证（今日完整流程）

- 用 **50000 元 + 真实 westock 行情**跑通：基于行情建仓、模拟撮合、四维学习复盘；
- 持仓现价落真实价（4.320 / 4.280 / 26.110），**不再是 0.000 脏值**（脏价防护闸门已接入）；
- 学习产出 `learning_progress.html` / `learned_params.json` 正常生成；
- **全套 68 项测试通过（0 失败 0 错误）**，含脏价防护用例（日志：`价格非正: 0.0 … 跳过落库`）。

## 五、调度守护运行状态

- 已后台常驻（进程 `scheduler.py`，启动于 2026-09-23 16:04，日志见 `data/scheduler.log` 末尾）；
- 当前休眠至 **2026-09-24 09:00**，届时：09:30 起盘中实时刷新 → 15:35 自动执行当日「行情→决策→撮合→复盘→学习」；
- 调度内部调用 `live_run.py` 使用正确 Python 路径（`.workbuddy/binaries/python/versions/3.13.12/python.exe`），且 `live_run` 自带幂等标记，同日不会重复交易。

## 六、注意事项

- **请勿运行 `cli.py strategy` / `cli.py learn`**：它们是手动多日回填演示工具，会写未来日期快照、重置账户，污染自动运行。正常自动交易/学习由调度守护完成，无需手动干预。
- 调度守护是常驻系统进程；若机器/会话重启，需用正确 Python 路径重启 `scheduler.py`（见上）。
- 看板 `dashboard.py`（streamlit :8501）独立运行，读取 `data/` 文件实时展示。
