"""本地存储模块（所有数据落盘本地，不上传任何服务器）。

- account.json：账户状态（现金 / 持仓 / 已实现盈亏 / 当前交易日）
- trades.jsonl：逐笔成交流水（追加写）
- snapshots.jsonl：每日资产快照（追加写）
"""
import json
from datetime import datetime
from pathlib import Path

from core.account import AccountManager


class LocalStore:
    def __init__(self, base_dir: str = "data"):
        self.base = Path(base_dir)
        if not self.base.is_absolute():
            # 锚定到项目根目录（storage/ 的上级），不依赖运行时的 cwd，
            # 保证引擎落盘位置与看板(dashboard.py)读取位置一致。
            root = Path(__file__).resolve().parent.parent
            self.base = (root / base_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.account_file = self.base / "account.json"
        self.trades_file = self.base / "trades.jsonl"
        self.snap_file = self.base / "snapshots.jsonl"
        self.strategy_file = self.base / "strategy_state.json"
        self.learned_file = self.base / "learned_params.json"
        self.progress_file = self.base / "learning_progress.jsonl"
        self.live_file = self.base / "live_prices.json"
        self.risk_log_file = self.base / "risk_log.jsonl"

    # ---------- 账户 ----------
    def save_account(self, account: AccountManager) -> None:
        self.account_file.write_text(
            json.dumps(account.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_account(self, config) -> AccountManager:
        if not self.account_file.exists():
            return None
        data = json.loads(self.account_file.read_text(encoding="utf-8"))
        return AccountManager.from_dict(data, config)

    def load_trades(self) -> list:
        """读取成交流水（每行一条 JSON）。"""
        if not self.trades_file.exists():
            return []
        out = []
        for line in self.trades_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # ---------- 成交流水 ----------
    def _existing_trade_ids(self) -> set:
        ids = set()
        if self.trades_file.exists():
            for line in self.trades_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        ids.add(json.loads(line)["trade_id"])
                    except (ValueError, KeyError):
                        pass
        return ids

    def append_trade(self, trade) -> None:
        with self.trades_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trade.__dict__, ensure_ascii=False, default=str) + "\n")

    def append_new_trades(self, trades) -> None:
        """仅追加尚未落盘的成交，避免重复写入。"""
        existing = self._existing_trade_ids()
        with self.trades_file.open("a", encoding="utf-8") as f:
            for t in trades:
                if t.trade_id not in existing:
                    f.write(json.dumps(t.__dict__, ensure_ascii=False, default=str) + "\n")
                    existing.add(t.trade_id)

    # ---------- 资产快照 ----------
    def save_snapshot(self, snapshot: dict) -> None:
        rec = dict(snapshot)
        rec["_ts"] = datetime.now().isoformat()
        with self.snap_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def reset(self) -> None:
        for f in (self.account_file, self.trades_file, self.snap_file,
                  self.strategy_file, self.learned_file, self.progress_file,
                  self.live_file, self.risk_log_file):
            if f.exists():
                f.unlink()

    # ---------- 风控日志（完整可追溯） ----------
    def append_risk_event(self, rec: dict) -> None:
        with self.risk_log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def load_risk_log(self) -> list:
        if not self.risk_log_file.exists():
            return []
        out = []
        for line in self.risk_log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # ---------- 实时行情快照（供看板读取，与智能体同数据源） ----------
    def save_live_prices(self, data: dict) -> None:
        """写入各持仓/全市场实时价 {code: {price, name, prev_close, industry, updated_at}}。"""
        self.live_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def load_live_prices(self) -> dict:
        if not self.live_file.exists():
            return {}
        return json.loads(self.live_file.read_text(encoding="utf-8"))

    # ---------- 自学习参数 / 进度 ----------
    def save_learned_params(self, data: dict) -> None:
        self.learned_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def load_learned_params(self) -> dict:
        if not self.learned_file.exists():
            return {}
        return json.loads(self.learned_file.read_text(encoding="utf-8"))

    def append_progress(self, rec: dict) -> None:
        with self.progress_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def load_progress(self) -> list:
        if not self.progress_file.exists():
            return []
        out = []
        for line in self.progress_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # ---------- 策略状态 ----------
    def save_strategy_state(self, state: dict) -> None:
        self.strategy_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def load_strategy_state(self) -> dict:
        if not self.strategy_file.exists():
            return {}
        return json.loads(self.strategy_file.read_text(encoding="utf-8"))
