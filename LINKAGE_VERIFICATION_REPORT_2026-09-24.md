# 全模块联动性核验及修复报告

- **系统**：stock-simulator 模拟股市交易系统
- **核验日期**：2026-09-24
- **覆盖模块**：交易撮合引擎（`core/matching.py`）、交易规则引擎（`core/trading_rules.py`）、行情接入层（`market/`）、实时看板（`dashboard.py`）、调度守护（`scheduler.py`）、模拟器落盘与盈亏核算（`core/simulator.py`、`core/pnl.py`）
- **修复规则**：联动性问题按影响等级自动分级——交易定价正确性 / 状态不一致归 P0 优先，功能展示类归 P1/P2，发现问题直接同步修复，无需二次指令。

---

## 一、核验结论

> **整体联动性达标 ✅**

本轮全模块联动核验发现联动异常点 **6 项**（含上一轮一致性整改遗留的联动薄弱点），按影响等级分级修复：

| 等级 | 数量 | 项 | 状态 |
|------|------|----|------|
| **P0** | 1 | D1 看板状态联动崩溃（未定义 `ms` NameError） | 100% 修复 |
| **P1** | 3 | D2 行业落盘耦合基本面网络、D3 盈亏核算静默写 0、脏价二级防御缺失 | 100% 修复 |
| **P2** | 2 | D4 收盘边界缺失末值刷新、D5 Mock 行情 OHLC 不可区分 | 100% 修复 |

- 新增联动专项单元测试 **16 项**（`tests/test_linkage.py`）。
- 全量回归 **100 项**单元测试全部通过（0 失败 / 0 错误）。
- 运行期取证（`_linkage_verify_result.json`，11 个证据段）全部通过。
- **满足复验条件**：P0 项（D1）已 100% 修复，全链路数据流转 / 状态 / 调度时序 / 异常容错四类联动均验证通过。

---

## 二、核验维度与发现的联动异常点

### 维度 1 · 数据流转联动（行情接入 → 撮合定价 → 落盘存储 → 看板展示）
- **L-D5（P2）**：`MockProvider.get_quote` 的 `open/high/low` 恒等于现价，全链路字段透传虽已实现，但看板「开盘/最高/最低」三列恒等于现价，无法验证字段是否真实透传，易误判为字段未落盘。
- **L-脏价（P1）**：`validate_quote` 仅拦截非正 / NaN 价，**未拦截超出 `[floor, ceiling]` 涨跌停区间**的异常行情价；此类脏价会被原样落盘，在撮合引擎与看板间扩散为两套数据。

### 维度 2 · 状态联动（盘前 / 连续竞价 / 午休 / 收盘 + 涨跌停 / 停牌 / 正常）
- **D1（P0）**：`dashboard.py` 休市分支引用未定义变量 `ms`（`if ms == "CLOSED"`），运行期触发 `NameError`，**整个看板崩溃**，状态联动在该分支完全断裂（撮合/规则侧状态正常，看板侧不展示）。

### 维度 3 · 调度时序联动（行情快照刷新 → 收盘收尾 → 持仓落盘 → 盈亏核算）
- **D4（P2）**：调度守护盘中循环在 `>=15:00` 退出后、15:35 收盘流程（`run_daily`）前，缺少一次边界快照刷新。15:00 最后一笔行情可能在 15:00–15:35 空窗丢失，导致末值未落盘即进入收盘核算。

### 维度 4 · 异常容错联动（行情源中断 / 撮合异常 / 落盘失败）
- **D2（P1）**：撮合引擎与看板每 3s 刷新快照时，`Simulator._industry` 对每只持仓调用 `get_fundamentals` 触达行情源基本面网络。网络抖动 / 限流会级联拖累看板依赖的实时价获取（同源耦合，违反「单模块异常全链路同步降级」）。
- **D3（P1）**：`PnLStatistics._current_prices` 在 `get_quote` 与快照回退均失败时静默返回 `0.0`，导致概览总资产悄悄少算，与持仓明细「行情未就绪」制造**跨模块数据割裂**。

---

## 三、逐项修复与运行时取证

### D1（P0 · 状态联动）看板崩溃修复
- **修复点**：`dashboard.py`
  - 顶栏阶段状态显式绑定：`phase_state = phase["state"]`（原 `phase = market_phase_label()` 后直接引用未定义的 `ms`）。
  - 休市分支改为 `if phase_state == "CLOSED" and mode == "实时看板"`。
- **运行时取证**（`_linkage_verify_result.json` → `dashboard_phase_binding`）：
  `binds_phase_state=true`、`no_undefined_ms=true`。
- **验证结论**：单元 `test_dashboard_phase_binding_no_nameerror` 通过；看板休市视图不再崩溃，状态联动恢复。

### D2（P1 · 异常容错 / 数据流转）行业落盘解耦基本面网络
- **修复点**：`core/simulator.py` 重写 `_industry(code, fallback=None)`：优先复用上一次有效快照中的 `industry`（静态元数据，首次成功后即稳定），仅当快照缺该字段时才回退 `get_fundamentals`。
- **运行时取证**（`D2_industry_decoupled`）：`returned="FALLBACK_IND"`、`fundamentals_called=false`、`decoupled_from_network=true`。
- **验证结论**：单元 `test_industry_refresh_decoupled_from_fundamentals` 通过；盘中刷新不再触达基本面网络，行情源抖动不会级联拖垮实时价获取。

### D3（P1 · 异常容错联动）盈亏核算不再静默写 0
- **修复点**：`core/pnl.py` 重构 `_current_prices_with_flags()` 返回 `(prices, unavailable)`；`snapshot() / reconcile() / position_details()` 显式携带 `price_ready` 与 `price_unavailable`，源与回退均失败时**标记不可用并交由看板联动告警**，绝不静默写 0。
- **看板联动**：`dashboard.py` `compute_overview` 透传 `price_ready`，概览与持仓明细同步告警，消除数据割裂。
- **运行时取证**（`D3_unavailable_signal` / `D3_fallback_to_snapshot`）：
  - 源与回退均失败：`price_ready=false`、`unavailable=["sh600519"]`（不再静默写 0）。
  - 源失败但快照可用：复用回退价 `1705.0`，`ok=true`（兜底生效）。
- **验证结论**：单元 `test_pnl_signals_unavailable_no_silent_zero`、`test_pnl_falls_back_to_live_snapshot` 通过。

### D4（P2 · 调度时序）收盘边界补刷新 + 串行化
- **修复点**：`scheduler.py` 盘中循环退出（`>=15:00`）后、15:35 收盘流程前，补一次 `refresh_intraday(market)`；并保留 `if datetime.now().time() >= CLOSE_RUN:` 门控，确保 **边界刷新 → 收盘流程** 按依赖顺序串行执行。
- **运行时取证**（`D4_scheduler_timing`）：`REFRESH_SEC=3`、`CLOSE_RUN=15:35:00`、`end_of_day_refresh_before_close_run=true`。
- **验证结论**：单元 `test_scheduler_constants`、`test_end_of_day_refresh_before_close_run`（静态断言刷新位于收盘流程之前）、`test_refresh_intraday_self_heals_on_source_failure`（源异常自愈不退出）通过。

### D5（P2 · 数据流转）Mock 行情 OHLC 可区分
- **修复点**：`market/mock_provider.py` `get_quote` 的 `_ohlc(prev, price)` 确定性生成可区分四价：开盘取昨收，最高/最低在开收区间外各扩 0.5%（同 code 每次一致、可断言、可透传验证）。
- **运行时取证**（`D5_ohlc_distinct`）：
  `sz000001: open=11.0/high=11.26/low=10.95/price=11.2`；`sh600519: 1700/1708.5/1671.6/1680`；`bj830799: 10.0/10.05/9.45/9.5`，三例 `distinct=true`。
- **验证结论**：单元 `test_ohlc_distinct_transmittable`、`test_full_fields_landed_and_displayed` 通过；字段透传可被端到端验证。

### 脏价二级防御（P1 · 异常容错）超出涨跌停区间不落盘
- **修复点**：`core/simulator.py` `_live_prices()` 在 `validate_quote` 通过后追加区间校验：`if ceiling>0 and floor>0 and (price>ceiling+1e-6 or price<floor-1e-6): ok=False`；与原「源失败 → 沿用回退 / 跳过」分支合并，确保脏价**不落盘**。
- **运行时取证**（`dirty_price_guard`）：异常价 `99999`（区间 `[90,110]`）→ `X_landed=false`、`positions=0`（stderr 日志确认 `校验未过(价格超出涨跌停区间)且无有效价可回退，跳过落库`）。
- **验证结论**：单元 `test_dirty_price_out_of_limit_not_landed`、`test_nonpositive_price_guard` 通过。

---

## 四、跨模块一致性复核（同源 / 同时效 / 同异常）

- **同源**：`data_flow_same_source` 取证——行情源价 `1705.0` = 看板展示价 `1705.0`；成交判定价 `1705.8525 = 1705.0 ×(1+5bps 滑点)`，证实撮合与展示共用同一行情源，无两套数据。
- **同时效**：调度守护 `REFRESH_SEC=3` 与看板重绘频率对齐（沿用上轮一致性整改结论），快照 `_meta.generated_at`/`source` 在看板展示旁显式标识。
- **同异常**：
  - 行情源中断 → 撮合拒单（`test_matching_rejects_on_quote_failure`：`行情获取失败`）、看板标「行情未就绪」、盈亏核算标 `price_ready=false`，三处同步。
  - 停牌 → 撮合拒单（`标的停牌中，禁止交易`）、落盘 `is_suspended=true`、看板标「停牌」，`suspension_detect` 全场景正确。
  - 脏价 → 不入链路（validate 拦截）、不落盘（二级防御）。
  - 落盘失败 → 调度守护 `refresh_intraday` 外层 try/except 自愈，主循环不退出。

---

## 五、状态机覆盖复核
`phase_labels` 取证：
- `09:15 → PRE（盘前 / 集合竞价）`
- `10:00 → LIVE（交易中 · 连续竞价）`
- `12:00 → PAUSE（午休）`
- `周六 10:00 → CLOSED（休市 · 收盘总结）`

四态在规则引擎、撮合引擎（交易时段校验）、看板（阶段徽标 + 休市视图切换）三处一致。

---

## 六、验证结果汇总

| 类别 | 数量 | 结果 |
|------|------|------|
| 既有单元测试（含一致性专项） | 84 | 全部通过 |
| 本轮新增联动专项测试 | 16 | 全部通过 |
| **全量回归** | **100** | **OK（0 失败 / 0 错误）** |
| 运行时取证证据段 | 11 | 全部通过 |

---

## 七、最终判定

**整体联动性达标。** 数据流转联动、状态联动、调度时序联动、异常容错联动四个维度全部修复并验证通过；P0 级异常项 D1 已 100% 修复（看板状态联动崩溃消除），满足复验条件。系统现有六大模块在字段透传、状态同步、调度串行、异常降级四个层面已实现**同数据源、同时效、同异常处理逻辑**的联动一致性。
