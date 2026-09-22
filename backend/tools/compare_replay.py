"""逐任务对比：存档工期 vs 确定性重演工期（找差异在哪条、哪一类）。"""

from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_BACKEND, "tools")
for p in (_BACKEND, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_plan as rp                     # noqa: E402


def main():
    plan_id = sys.argv[1] if len(sys.argv) > 1 else "plan_run_1789827002"
    plan, _p = rp.load_plan(plan_id)
    res = rp.replay(plan, verbose=False)
    after = {str(t[0]): t[2] for t in res["durations"]}

    # 存档里的排程工期（docx/看板用的那一份）
    archived = {}
    for row in plan.get("all_tasks_schedule") or []:
        archived[str(row.get("task_id"))] = row.get("duration_days")

    name = {}
    for ph in (plan.get("wbs") or {}).get("phases", []):
        for wp in ph.get("work_packages", []):
            for sp in wp.get("sub_packages", []):
                name[str(sp.get("id"))] = sp.get("name")

    diffs = []
    for tid in sorted(set(archived) | set(after)):
        a, b = archived.get(tid), after.get(tid)
        if a is None or b is None:
            continue
        if int(a) != int(b):
            diffs.append((tid, name.get(tid, ""), int(a), int(b)))

    print("任务数 %d，工期有差异 %d 条" % (len(after), len(diffs)))
    print("总工期：存档 %s → 重演 %s（理论 %s）"
          % (res["before"]["total"], res["after"]["resource_ok_days"],
             res["after"]["theory_min_days"]))
    diffs.sort(key=lambda d: -(d[3] - d[2]))
    print("\n--- 工期变长的（前 25 条）---")
    for tid, nm, a, b in diffs[:25]:
        print("   %-10s %-24s %3d → %3d  (%+d)" % (tid, (nm or "")[:24], a, b, b - a))
    print("\n--- 工期变短的（前 15 条）---")
    for tid, nm, a, b in diffs[-15:]:
        print("   %-10s %-24s %3d → %3d  (%+d)" % (tid, (nm or "")[:24], a, b, b - a))
    grow = sum(b - a for _t, _n, a, b in diffs if b > a)
    shrink = sum(a - b for _t, _n, a, b in diffs if a > b)
    print("\n净变化：变长合计 +%d，变短合计 -%d" % (grow, shrink))


if __name__ == "__main__":
    raise SystemExit(main())
