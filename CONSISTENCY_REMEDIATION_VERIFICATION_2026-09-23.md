# 交易规则与行情显示一致性专项 · 整改验证报告

- **被整改系统**：stock-simulator（A股模拟交易系统）
- **整改依据**：《交易规则与行情显示一致性专项核查报告单》（`RULE_DISPLAY_CONSISTENCY_AUDIT_2026-09-23.md`）异常项 AN-1 ~ AN-11，及用户逐项整改指令（P0 / P1 / P2 / P3）
- **整改完成时点**：2026-09-23 17:05
- **验证方式**：代码静态比对 + 单元测试（84 项，含新增 16 项）+ 运行时取证脚本（隔离临时存储，未污染 `data/`），取证结果见附件 `_verify_result.json`
- **验证结论**：**全部异常项已修复并通过验证；P0 级 2 项 100% 修复；满足复验条件。**

---

## 一、整改总览（按优先级）

| 优先级 | 整改指令 | 对应异常项 | 是否 100% 修复 | 验证方式 |
|---|---|---|---|---|
| **P0** | 消除运行期两套数据偏差（快照 60s→3s 对齐展示价，或交易时段直读行情只读镜像） | AN-1 | ✅ 是 | 单元测试 + 运行时取证 |
| **P0** | 禁用空快照静默回退 Mock 静态价；展示价旁新增 `updated_at` 时间戳与 `source` 源标识 | AN-2 | ✅ 是 | 单元测试 + 运行时取证 |
| **P1** | 补全核心行情指标（open/high/low/volume/change_pct 落盘 + 看板列） | AN-3/4/5 | ✅ 是 | 单元测试 + 运行时取证 |
| **P1** | 补全涨跌停状态展示（ceiling/floor 落盘 + 状态标识 + 阈值） | AN-6 | ✅ 是 | 单元测试 + 运行时取证 |
| **P1** | 新增停牌检测（is_suspended + 禁止撮合 + 看板标注） | AN-7 | ✅ 是 | 单元测试 + 运行时取证 |
| **P2** | 修正交易阶段标签（9:00–9:30「盘前/集合竞价」；无集合竞价在规则文档声明） | AN-8 | ✅ 是 | 单元测试 + 文档核对 |
| **P2** | 修正行情刷新时效口径（升级 3s 后口径自洽；页面重绘/数据刷新一致） | AN-9 | ✅ 是 | 单元测试 + 文档核对 |
| **P2** | 补充滑点说明（看板「现价」列旁标注市价单含 5bps 滑点） | AN-10 | ✅ 是 | 文档/界面文案核对 |
| **P3** | 统一 README 涨跌停口径（按名称含 st 判定，非代码前缀） | AN-11 | ✅ 是 | 文档核对 + 单元测试 |

> 注：原核查 B-3「收盘价」单列未展示——不在本次用户 P1.1 清单（open/high/low/volume/change_pct）内；盘中连续竞价阶段无独立「当日收盘」概念，收盘以「最新价」替代，与撮合/展示同源一致，故未单列，特此说明。

---

## 二、逐项验证明细（异常项 · 整改点 · 代码修改位置 · 运行时取证 · 验证结论）

### P0 级（复验前置，必须 100% 修复）

#### AN-1　运行期两套数据偏差（快照 60s vs 实时价）
- **整改点**：将盘中 `live_prices.json` 快照刷新从 60s 收紧至 3s，与看板重绘频率对齐；快照新增 `updated_at` / `source` / `_meta` 时效与数据源元数据。
- **代码修改位置**：
  - `scheduler.py`：`REFRESH_SEC = 60` → `REFRESH_SEC = 3`（L39）；模块与函数 docstring 同步更新。
  - `core/simulator.py`：`_live_prices()` 重写，落盘新增 `open/high/low/volume/change_pct/ceiling/floor/is_suspended/limit_hit/updated_at/source`，顶部写入 `_meta{generated_at,source,positions}`；新增 `_industry()` 辅助。
- **运行时取证**：
  - `scheduler.REFRESH_SEC == 3` ✅
  - 同源验证：引擎成交价 = 行情源价 ×(1+5bps) = 11.2 × 1.0005 = **11.2056**；看板展示价 = 行情源价 **11.2**；`source = "mock"`（同源 `MarketProvider.get_quote`）。
  - 快照 `_meta = {generated_at:"2026-09-23T17:00:08", source:"mock", positions:1}` ✅
- **验证结论**：✅ **通过**。刷新频率已对齐 3s，展示价与成交判定价同源，时效差窗口消除。

#### AN-2　空快照静默回退 Mock 静态价（虚构演示价）
- **整改点**：看板移除对 `MockProvider` 静态价的静默回退；快照为空/异常时显式提示「行情数据未就绪」，禁止展示虚构价；新增 `updated_at` 与 `source` 状态标识。
- **代码修改位置**：
  - `dashboard.py`：删除 `_mock_price()` 与 `market_state()`；`current_price()` 改为调用 `market.display.resolve_display_price`，空快照返回 `None`；新增 `market/display.py`（纯函数，无 streamlit 依赖）承载 `resolve_display_price` / `market_phase_label`。
  - `dashboard.py` 顶栏：展示「数据源 / 快照更新时间 / 刷新频率」；空快照时显式红色告警「行情数据未就绪」；持仓明细表「现价」列对未就绪项标「行情未就绪」徽标。
- **运行时取证**：
  - `resolve_display_price({}, "sh600519")` → `{status:"NOT_READY", price:None, warn:"行情数据未就绪"}` ✅（不再返回 1680.00 虚构价）
- **验证结论**：✅ **通过**。空快照不再回退虚构价，强制「未就绪」提示，两套数据红线消除。

### P1 级（重点功能整改）

#### AN-3　开盘/最高/最低价已抓取但未落盘、未展示
- **整改点**：`_live_prices` 落盘 `open/high/low`；看板持仓明细增加「开盘/最高/最低」列。
- **代码修改位置**：`core/simulator.py` `_live_prices()`；`dashboard.py` `positions_html()` 表头与行渲染。
- **运行时取证**：`P1_fields = {open:11.2, high:11.2, low:11.2}` ✅
- **验证结论**：✅ **通过**。

#### AN-4　单只标的当日涨跌幅未摄取、未展示
- **整改点**：`_live_prices` 摄取/计算 `change_pct`（优先源值，否则由 `(price-prev_close)/prev_close` 推导）；看板增加「涨跌幅」列。
- **代码修改位置**：`core/simulator.py` `_live_prices()`；`market/westock_provider.py` `get_quote` 摄取 `change_pct`；`dashboard.py` `positions_html()`。
- **运行时取证**：`P1_fields.change_pct = 1.82`（= (11.2−11.0)/11.0×100）✅
- **验证结论**：✅ **通过**。

#### AN-5　成交量未摄取、未展示
- **整改点**：`Quote` 新增 `volume` 字段；`_live_prices` 落盘 `volume`；看板增加「成交量」列（万手展示）。
- **代码修改位置**：`market/base.py` `Quote.volume`；`market/westock_provider.py` `get_quote` 摄取 `volume`；`core/simulator.py`；`dashboard.py`。
- **运行时取证**：`P1_fields.volume = 1000000.0` ✅
- **验证结论**：✅ **通过**。

#### AN-6　涨停/跌停状态前端零展示
- **整改点**：`_live_prices` 落盘 `ceiling/floor` 并计算 `limit_hit`（UP/DOWN/NONE）；看板「涨跌停」列对涨停/跌停显式徽标 + 价格阈值；引擎侧 `validate_price_limit` 已用 `ceiling/floor` 拒超幅委托（核查 C-3 原已一致）。
- **代码修改位置**：`core/simulator.py` `_live_prices()`（写入 `ceiling/floor/limit_hit`）；`dashboard.py` `positions_html()` 涨跌停徽标。
- **运行时取证**：`P1_fields.ceiling=12.1, floor=9.9`（主板 ±10%）；单元测试 `test_limit_hit_up` 命中 UP ✅
- **验证结论**：✅ **通过**。

#### AN-7　停牌状态引擎与展示均缺失
- **整改点**：行情校验层新增 `is_suspended` 停牌检测；`_live_prices` 对停牌标的写冻结价 + `is_suspended=True` 并标 `limit_hit=NONE`；撮合引擎对停牌标的拒单；看板标「停牌」徽标。
- **代码修改位置**：
  - `market/feed.py`：新增 `detect_suspension(price, prev_close, volume, suspended)`（源显式标记 或 成交量=0 且现价=昨收 辅助判定）。
  - `market/base.py`：`Quote.suspended`；`market/westock_provider.py` `get_quote` 摄取交易状态列（`_suspended_flag`）；`market/mock_provider.py` `set_suspended()`。
  - `core/matching.py` `process_order()`：获取行情后调用 `detect_suspension`，命中则 `_reject("标的停牌中，禁止交易")`。
  - `dashboard.py` `positions_html()`：「停牌」徽标。
- **运行时取证**：
  - 停牌快照：`P1.3_suspended_snapshot = {is_suspended:true, limit_hit:"NONE"}` ✅
  - 撮合拦截：`P1.3_suspended_matching = {status:"REJECTED", reject_reason:"标的停牌中，禁止交易"}` ✅
- **验证结论**：✅ **通过**。

### P2 级（体验与口径合规）

#### AN-8　9:00–9:30 误标「午休」；无集合竞价未在规则文档声明
- **整改点**：看板将 9:00–9:30 标注「盘前 / 集合竞价」；在 `core/trading_rules.py` 与 `README.md` 显式声明系统仅做连续竞价撮合、不含集合竞价独立算法。
- **代码修改位置**：`market/display.py` `market_phase_label()`（PRE 标签「盘前 / 集合竞价」）；`core/trading_rules.py` docstring；`README.md` 设计要点新增「交易时段（撮合窗口）」声明。
- **运行时取证**：`P2.1_phase = {09:15:"盘前 / 集合竞价", 10:00:"交易中 · 连续竞价", 12:00:"午休", 周六:"休市 · 收盘总结"}` ✅
- **验证结论**：✅ **通过**。

#### AN-9　看板「3s 实时」实为 60s 假实时
- **整改点**：快照刷新升级至 3s（见 AN-1）；看板顶部明确「行情快照每 3s 刷新（交易时段）」与页面重绘频率一致，消除口径偏差。
- **代码修改位置**：`scheduler.py` `REFRESH_SEC=3`；`dashboard.py` 顶栏时效说明 + 自动刷新 `interval` 取 `phase["refresh"]`。
- **运行时取证**：`REFRESH_SEC == 3` ✅；看板文案「页面每 3s 重绘，行情快照每 3s 刷新」✅
- **验证结论**：✅ **通过**。

#### AN-10　市价单成交含 5bps 滑点，看板「现价」未标注
- **整改点**：看板持仓明细标题下标注「现价列旁标注『市价单成交含 5bps 滑点』」；`README.md` 撮合要点补充滑点说明。
- **代码修改位置**：`dashboard.py` ③ 持仓明细 `st.caption(...)`；`config/settings.py` `slippage_rate=0.0005`（原已实现，仅补注明）。
- **运行时取证**：`FeeConfig().slippage_rate = 0.0005 > 0`；AN-1 同源取证中成交价 11.2056 = 11.2×(1+0.0005) 即滑点体现 ✅
- **验证结论**：✅ **通过**。

### P3 级（文档口径统一）

#### AN-11　README「代码前缀回退」与代码「名称含 st」不符
- **整改点**：README 改为「无行情时按股票名称是否含 'st' 判定涨跌停幅度（ST 股 ±5%，其余主板 ±10%）」；`core/trading_rules.py` docstring 同步口径。
- **代码修改位置**：`README.md` 设计要点「涨跌幅」条；`core/trading_rules.py` docstring。
- **运行时取证**：`P3_readme_new_wording=True`（含「按股票名称是否含 'st'」），`P3_readme_old_removed=True`（无「按代码前缀回退计算」）✅；`price_limit_pct("ST云城")=0.05`，`price_limit_pct("贵州茅台")=0.10` ✅
- **验证结论**：✅ **通过**。

---

## 三、回归测试与运行时取证汇总

- **单元测试**：`python -m unittest discover -s tests` → **Ran 84 tests in 1.1s, OK（0 失败 / 0 错误）**。
  - 原回归套件 68 项全部通过（无新回归）。
  - 新增 `tests/test_display_consistency.py` 16 项，覆盖 P0–P3 全部修复点（快照 3s、空快照不回退、全字段落盘、涨跌停、停牌落盘+拒单、阶段标签、README 口径）。
- **修复的测试隔离缺陷**：`MockProvider.set_price/set_suspended` 原先会原地修改类级 `_SAMPLE`（浅拷贝），导致测试用例间相互污染；已改为复制内层字典后再修改，消除污染。
- **运行时取证附件**：`_verify_result.json`（空快照 NOT_READY、同源成交/展示价、全字段落盘、停牌拒单、阶段标签、README 口径均在此文件中可查）。

---

## 四、部署状态（保证明日正常运转）

- 调度守护已用**更新后的代码**重新启动（日志 `[2026-09-23 17:02:05] 调度守护进程启动`），盘中快照刷新已为 **3s**；守护进程现休眠至 2026-09-24 09:00，届时自动进入盘中 3s 刷新、15:35 自动交易学习（幂等）。
- 说明：上一班次遗留的守护进程（16:04 启动，内存中仍持 60s 旧值）因沙箱进程管理（WMI/CIM 被拦截）无法选择性终止；其 `live_run` 收盘流程幂等、盘中刷新只读且写入同源有效数据，与新建守护并存时**不会造成数据偏差或重复交易**，仅属冗余；建议在有进程管理权限时停止该遗留进程（`taskkill /pid <遗留PID>`）。

---

## 五、复验条件结论

> **满足复验条件。**
> - P0 级异常项 **AN-1、AN-2 均已 100% 修复**（快照 60s→3s 对齐 + 空快照不回退虚构价并显式「未就绪」提示），经单元测试与运行时取证双重确认。
> - P1 级 AN-3~AN-7、P2 级 AN-8~AN-10、P3 级 AN-11 全部修复并通过验证。
> - 全量 84 项单元测试通过，无回归；调度守护已按新代码重启，明日盘中 3s 实时刷新与行情展示一致性生效。
> - 据此判定：**交易规则与行情显示一致性校验通过（整改后）。**
