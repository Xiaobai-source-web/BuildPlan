"""按"影响面"排出待审的 L4 默认定额行 —— 人工审 10 条，胜过盲审 400 条。

用法::

    python backend/tools/review_queue.py --plan plan_run_1789827002
    python backend/tools/review_queue.py --plan plan_run_1789827002 --top 15 --json q.json

## 为什么需要它

`L4_Norm_Default` 有 424 行，全部 pending。但**决定总工期的是少数几条 L4**：
实测 322 条任务里，主体结构那 5 条 L4（钢筋绑扎 / 铝模安装 / 叠合板吊装 /
混凝土浇筑 / 爬架提升）各占 18 条，二次结构 4 条各占 18 条 —— 这些就是
"18 层 × 每层几天"的全部来源。花 10 分钟审这 10 条，比审完 424 条有效得多。

排序口径（可解释、不拍脑袋）：

    影响分 = 该 L4 的任务条数 × 该 L4 的定额反算工期与 WBS 目标工期之差（取绝对值）
             + 任务条数 × 10（条数本身就是权重）

即"这条 L4 一旦审错，会把总工期推歪多少天"的一阶估计。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_BACKEND, "tools")
for p in (_BACKEND, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_plan as rp                       # noqa: E402
from pipeline import kb                        # noqa: E402


def _defaults():
    """{activity_id: [ (unit, value, unit_text, confidence, state) ]}"""
    out = defaultdict(list)
    for r in kb._query_all(
            "SELECT activity_id, quantity_unit, norm_value, norm_unit, "
            "confidence, review_state FROM L4_Norm_Default WHERE norm_kind='labor'"):
        out[str(r[0])].append((r[1], r[2], r[3], r[4], r[5]))
    return out


def _name_of(activity_id):
    rows = kb._query_all(
        "SELECT activity_name FROM L4_Activity_Dictionary WHERE activity_id = ?",
        (activity_id,))
    return (rows[0][0] if rows else "") or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    plan, _path = rp.load_plan(args.plan)
    defaults = _defaults()

    # 逐叶子：L4、任务名、WBS 目标工期、以及"若定额放行会算成几天"
    agg = defaultdict(lambda: {"tasks": 0, "wbs_days": 0, "norm_days": 0.0,
                               "unit": "", "value": None, "confidence": "",
                               "state": "", "name": "", "phase": "",
                               "sample": ""})
    for ph in (plan.get("wbs") or {}).get("phases", []):
        for wp in ph.get("work_packages", []):
            for sp in wp.get("sub_packages", []):
                aid = str(sp.get("kb_activity_id") or "")
                b = sp.get("norm_binding") if isinstance(sp.get("norm_binding"), dict) else {}
                qty = sp.get("quantity")
                days = sp.get("duration_days")
                a = agg[aid]
                a["tasks"] += 1
                a["wbs_days"] += float(days or 0)
                a["name"] = a["name"] or _name_of(aid)
                a["phase"] = a["phase"] or ph.get("phase", "")
                a["sample"] = a["sample"] or (sp.get("name") or "")
                prod = b.get("productivity_value")
                if prod is None and b.get("norm_value"):
                    try:
                        prod = 1.0 / float(b["norm_value"])
                    except Exception:
                        prod = None
                # 工作面容量顶满口径下的反算工期（与 scheduler 同式）
                cap = ((sp.get("workface_capacity") or {}).get("max_labor")
                       or (b.get("crew") or {}).get("_cap") or None)
                try:
                    q = float(qty or 0)
                except Exception:
                    q = 0.0
                if prod and q > 0:
                    people = int(cap) if cap else int(b.get("crew_design") or 0) or 1
                    a["norm_days"] += max(1.0, q / (float(prod) * people))
                d = defaults.get(aid) or []
                if d and not a["value"]:
                    a["unit"], a["value"], a["confidence"], a["state"] = (
                        d[0][0], d[0][1], d[0][3], d[0][4])

    rows = []
    for aid, a in agg.items():
        if not aid:
            continue
        delta = abs(a["norm_days"] - a["wbs_days"])
        score = a["tasks"] * 10 + a["tasks"] * delta
        rows.append({
            "activity_id": aid,
            "activity_name": a["name"],
            "phase": a["phase"],
            "tasks": a["tasks"],
            "wbs_days": int(a["wbs_days"]),
            "norm_days": int(round(a["norm_days"])),
            "delta_days": int(round(a["norm_days"] - a["wbs_days"])),
            "score": int(score),
            "default_value": a["value"],
            "default_unit": a["unit"],
            "confidence": a["confidence"],
            "review_state": a["state"],
            "sample_task": a["sample"],
        })
    rows.sort(key=lambda r: -r["score"])

    print("== 待审优先级（前 %d 条，按影响分）==" % args.top)
    print("   影响分 = 任务条数×(10 + |定额反算工期 − WBS 目标工期|)   [天/层 × 层数放大]")
    print()
    print("   %-4s %-20s %-9s %-5s %-8s %-8s %-7s %-9s %s"
          % ("序", "L4 活动", "阶段", "条数", "WBS天", "定额天", "差", "状态", "默认值"))
    for i, r in enumerate(rows[: args.top], 1):
        print("   %-4d %-20s %-9s %-5d %-8d %-8d %+-7d %-9s %s %s"
              % (i, r["activity_id"][:20], (r["phase"] or "")[:9], r["tasks"],
                 r["wbs_days"], r["norm_days"], r["delta_days"],
                 r["review_state"] or "无行",
                 ("%.4g" % r["default_value"]) if r["default_value"] else "-",
                 r["default_unit"] or ""))
    print()
    print("   审定命令：")
    print("     python backend/tools/approve_norm_default.py --show <L4>")
    print("     python backend/tools/approve_norm_default.py --approve <L4> --by <你的名字>")
    print("     # 值不对就先改值：")
    print("     python backend/tools/approve_norm_default.py --set <L4> --value 0.9 "
          "--unit 工日/m2 --source <来源> --approve")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print("   完整清单 → %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
