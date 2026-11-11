"""选股池过滤（量化交易专家主导）。

规则（要求 1）：
1) 仅保留沪深主板 A股；彻底剔除科创板(688) / 创业板(300/301) / 北交所(bj)。
2) 基础剔除：ST/*ST、近一年净利润亏损、股权质押率>50%、近30日日均成交额<500万元。
3) 优先从沪深300、中证500 的主板成分股中选股（成分内二次剔除科创/创业）。
"""
from typing import List, Tuple

from config.strategy import StrategyConfig
from market.base import MarketProvider, Fundamentals


class StockPool:
    def __init__(self, cfg: StrategyConfig, market: MarketProvider):
        self.cfg = cfg
        self.market = market

    # ---------- 板块判定 ----------
    def is_main_board(self, code: str) -> bool:
        c = (code or "").lower()
        for ex in self.cfg.exclude_boards:
            if c.startswith(ex):
                return False
        return any(c.startswith(b) for b in self.cfg.main_boards)

    def is_etf(self, code: str) -> bool:
        """ETF 识别：仅在 include_etfs 开启且代码前缀匹配 ETF 板块时成立。"""
        if not self.cfg.include_etfs:
            return False
        c = (code or "").lower()
        return any(c.startswith(b) for b in self.cfg.etf_boards)

    def is_st(self, code: str, fund: Fundamentals) -> bool:
        if self.cfg.exclude_st and fund.is_st:
            return True
        name = (fund.name or "").lower()
        return "st" in name

    # ---------- 基本面剔除 ----------
    def check_fundamentals(self, code: str, fund: Fundamentals) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        if self.is_etf(code):
            # ETF 为基金，无个股财报：跳过 ST/净利/质押，仅做流动性筛选
            if fund.avg_turnover_30d < self.cfg.min_avg_turnover_30d:
                reasons.append(f"近30日日均成交额 {fund.avg_turnover_30d/1e4:.0f}万 < "
                               f"{self.cfg.min_avg_turnover_30d/1e4:.0f}万")
            return (len(reasons) == 0), reasons
        if self.is_st(code, fund):
            reasons.append("ST/*ST 股剔除")
        if self.cfg.exclude_loss_net_profit and fund.net_profit_ttm <= 0:
            reasons.append("近一年净利润亏损")
        if fund.pledge_ratio > self.cfg.max_pledge_ratio:
            reasons.append(f"股权质押率 {fund.pledge_ratio:.0%} > {self.cfg.max_pledge_ratio:.0%}")
        if fund.avg_turnover_30d < self.cfg.min_avg_turnover_30d:
            reasons.append(f"近30日日均成交额 {fund.avg_turnover_30d/1e4:.0f}万 < "
                           f"{self.cfg.min_avg_turnover_30d/1e4:.0f}万")
        return (len(reasons) == 0), reasons

    # ---------- 单码筛选 ----------
    def screen_code(self, code: str) -> dict:
        """返回 {code, name, passed, reasons}。"""
        if not (self.is_main_board(code) or self.is_etf(code)):
            return {"code": code, "name": code, "passed": False,
                    "reasons": ["非沪深主板/ETF（科创/创业/北交所/其他剔除"]}
        try:
            fund = self.market.get_fundamentals(code)
        except Exception as e:
            return {"code": code, "name": code, "passed": False,
                    "reasons": [f"基本面获取失败: {e}"]}
        if self.is_etf(code):
            fund.industry = self.cfg.etf_industry   # ETF 归入独立行业桶，便于仓位约束
        ok, reasons = self.check_fundamentals(code, fund)
        return {"code": code, "name": fund.name, "passed": ok,
                "reasons": reasons, "fundamentals": fund}

    # ---------- 优先指数池 → 候选宇宙 ----------
    def candidate_universe(self) -> List[str]:
        """优先取沪深300/中证500 主板成分；若均为空则回退全市场主板。
        include_etfs 开启时额外并入种子 ETF。"""
        codes: List[str] = []
        for idx in self.cfg.priority_indexes:
            try:
                codes.extend(self.market.get_index_constituents(idx))
            except Exception:
                pass
        codes = [c for c in dict.fromkeys(codes) if self.is_main_board(c)]
        if self.cfg.include_etfs:
            seen = set(codes)
            for etf in self.cfg.etf_seeds:
                if self.is_etf(etf) and etf not in seen:
                    codes.append(etf)
                    seen.add(etf)
        if not codes:
            try:
                codes = [c for c in self.market.get_universe() if self.is_main_board(c)]
            except Exception:
                codes = []
        return codes

    def screen(self) -> List[dict]:
        """对候选宇宙逐个过滤，返回通过标的的明细列表。"""
        out = []
        for code in self.candidate_universe():
            res = self.screen_code(code)
            if res["passed"]:
                out.append(res)
        return out
