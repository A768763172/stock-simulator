import os
d = r"C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\.workbuddy\memory"
os.makedirs(d, exist_ok=True)
p = os.path.join(d, "2026-09-23.md")
note = """
## 初始资金调整与系统修复（stock-simulator, 下午）
- 初始资金50000：config/settings.py INITIAL_CAPITAL=50000（唯一真值）；live_run.py 改为 from config.settings import INITIAL_CAPITAL，删除本地10000常量。main.py/main_strategy.py/README 显示同步。
- 修Bug1 累计收益率+441%：根因 live_run.py 旧本地常量10000，现引用settings真值，重算+8.34%正确。
- 修Bug2 调度盘前漏刷新：scheduler.main 加 盘前(<09:30) sleep_until(09:30)+continue 分支。
- 清脏数据：cli.py strategy --days 20 回填把 account.current_day 写成2026-10-02、snapshots 写9-24~10-16未来日期。已校正 current_day=2026-09-23、0持仓、清空快照/成交/价格/风控日志，保留 learned_params(四维学习权重)。
- 验证：68测试全绿；调度守护(scheduler.py)已后台常驻，明09:30刷新+15:35交易学习。
- 环境坑：bash 无 coreutils/head、WMIC 被沙箱拦、命令壳不回 stdout 时改用 python 脚本写文件再 Read 最稳。cli.py strategy/learn 是手动回填工具会污染自动运行，勿用。
"""
with open(p, "a", encoding="utf-8") as f:
    f.write(note)
print("mem appended", p)
