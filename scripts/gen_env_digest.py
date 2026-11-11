"""每日政策/消息资讯摘要生成器（盘前检索任务的落盘工具）。

用法（stdin 传入 JSON）：
    echo '{"policy":[...],"news":[...]}' | python scripts/gen_env_digest.py --day 2026-10-13
    python scripts/gen_env_digest.py --day 2026-10-13 --input digest_draft.json

字段约定（与 strategy/market_env.py 中 FileEnvProvider 的注释一致）：
  policy: [{id, level: national|ministry|industry, strength: -1~1,
            window_days: int, tag, summary, industries: [行业关键词]}]
  news:   [{id, nature: bullish|bearish|neutral, magnitude: 0~1,
            half_life_days: float, tag, summary, industries: [行业关键词]}]
  industries（可选）：该条政策/消息直接点名的行业关键词列表（如 ["证券","银行"]、
  ["光伏","储能"]）。选股时按个股所属行业命中加成——命中才有区分度，
  未命中不加分；无行业指向的政策（如全局性货币宽松）可留空或不写。
判定口径建议：
  strength/magnitude 由检索时人工判定；>=0.8 的 magnitude 视为重磅消息；
  window_days/half_life_days 按事件影响力估计（宏观政策 10~20 日、部委 5~10 日、
  行业事件 3~8 日；消息半衰期通常 1~4 日）。
输出：data/env_digest_<day>.json（与 account.json 同目录，引擎自动读取）。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

LEVELS = {"national", "ministry", "industry"}
NATURES = {"bullish", "bearish", "neutral"}


def _industries(ev: dict) -> list:
    inds = ev.get("industries") or []
    if not isinstance(inds, list):
        raise ValueError("industries 必须是字符串列表")
    return [str(x)[:20] for x in inds][:6]


def _validate_policy(items: list, day: str) -> list:
    out = []
    for i, ev in enumerate(items or []):
        level = str(ev.get("level", "industry")).lower()
        if level not in LEVELS:
            raise ValueError(f"policy[{i}].level 必须是 {sorted(LEVELS)}，得到 {level!r}")
        strength = float(ev.get("strength", 0.0))
        if not -1.0 <= strength <= 1.0:
            raise ValueError(f"policy[{i}].strength 必须在 [-1,1]，得到 {strength}")
        wd = int(ev.get("window_days", 10))
        if not 1 <= wd <= 60:
            raise ValueError(f"policy[{i}].window_days 应在 [1,60]，得到 {wd}")
        out.append({
            "id": str(ev.get("id") or f"d-{day}-p{i+1}"),
            "level": level,
            "strength": round(strength, 3),
            "window_days": wd,
            "tag": str(ev.get("tag", ""))[:40],
            "summary": str(ev.get("summary", ""))[:300],
            "industries": _industries(ev),
        })
    return out


def _validate_news(items: list, day: str) -> list:
    out = []
    for i, nv in enumerate(items or []):
        nature = str(nv.get("nature", "neutral")).lower()
        if nature not in NATURES:
            raise ValueError(f"news[{i}].nature 必须是 {sorted(NATURES)}，得到 {nature!r}")
        mag = float(nv.get("magnitude", 0.5))
        if not 0.0 <= mag <= 1.0:
            raise ValueError(f"news[{i}].magnitude 必须在 [0,1]，得到 {mag}")
        hl = float(nv.get("half_life_days", 3))
        if not 0.5 <= hl <= 30:
            raise ValueError(f"news[{i}].half_life_days 应在 [0.5,30]，得到 {hl}")
        out.append({
            "id": str(nv.get("id") or f"d-{day}-n{i+1}"),
            "nature": nature,
            "magnitude": round(mag, 3),
            "half_life_days": round(hl, 1),
            "tag": str(nv.get("tag", ""))[:40],
            "summary": str(nv.get("summary", ""))[:300],
            "industries": _industries(nv),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成每日政策/消息资讯摘要")
    ap.add_argument("--day", required=True, help="摘要日期 YYYY-MM-DD")
    ap.add_argument("--input", help="JSON 草稿文件（缺省从 stdin 读）")
    args = ap.parse_args()

    try:
        date.fromisoformat(args.day)
    except ValueError:
        print(f"[FAIL] --day 格式错误: {args.day}", file=sys.stderr)
        return 2

    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[FAIL] JSON 解析失败: {e}", file=sys.stderr)
        return 2
    if not isinstance(draft, dict):
        print("[FAIL] 顶层必须是 JSON 对象", file=sys.stderr)
        return 2

    try:
        digest = {
            "day": args.day,
            "generated_by": "workbuddy-agent",
            "policy": _validate_policy(draft.get("policy"), args.day),
            "news": _validate_news(draft.get("news"), args.day),
        }
    except ValueError as e:
        print(f"[FAIL] 字段校验失败: {e}", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_fp = DATA_DIR / f"env_digest_{args.day}.json"
    out_fp.write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    print(f"[OK] 已写入 {out_fp}")
    print(f"  政策 {len(digest['policy'])} 条 / 消息 {len(digest['news'])} 条")
    for ev in digest["policy"]:
        sign = "利好" if ev["strength"] > 0 else ("利空" if ev["strength"] < 0 else "中性")
        inds = ("→ " + "/".join(ev["industries"])) if ev["industries"] else ""
        print(f"  [政策][{ev['level']}][{sign}{abs(ev['strength']):.2f}]"
              f" {ev['tag']}（窗口{ev['window_days']}日）{inds}")
    for nv in digest["news"]:
        mark = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}[nv["nature"]]
        heavy = " ★重磅" if nv["magnitude"] >= 0.8 else ""
        inds = ("→ " + "/".join(nv["industries"])) if nv["industries"] else ""
        print(f"  [消息][{mark}{nv['magnitude']:.2f}]{heavy} {nv['tag']}"
              f"（半衰{nv['half_life_days']}日）{inds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
