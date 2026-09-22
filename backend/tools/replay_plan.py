"""对一份已存档的计划做**确定性重演**（不调 LLM），用于改代码后的 A/B 对比。

为什么需要它：整条链最贵、最不可复现的两步是 `wbs_agent` 与 `deps`（上一次运行
deps 一个节点就吃掉 296K token），而**工期口径的改动全在 `deps` 之后**。所以：

    WBS + dependencies 冻结（取自存档）
        ↓ 重新跑
    norm_bind → crew_bind → scheduler

这样"改的是代码还是模型随机性"再也说不清的问题就消失了 —— 同一份 WBS、同一份依赖，
只换代码，工期差值就是代码造成的。

用法::

    python backend/tools/replay_plan.py --plan plan_run_1789827002
    python backend/tools/replay_plan.py --plan plan_run_1789827002 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文/㎡³ 这类字符会直接把脚本打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline import plan_store as _ps              # noqa: E402
from pipeline.nodes import crew_bind as _crew       # noqa: E402
from pipeline.nodes import norm_bind as _nb         # noqa: E402
from pipeline.nodes import scheduler as _sched      # noqa: E402

PLANS_DIR = Path(_BACKEND) / "plans"


def _silence(node):
    node._emit = lambda event, data: None
    return node


def load_plan(plan_id):
    """先找 plans/档案/<id>/当前版本.json，再退回 plans/<id>.json。"""
    for p in (PLANS_DIR / "档案" / plan_id / "当前版本.json", PLANS_DIR / (plan_id + ".json")):
        if p.exists():
            with open(p, encoding="utf-8") as fh:
                return json.load(fh), p
    raise SystemExit("找不到计划：%s" % plan_id)


def replay(plan, verbose=True):
    meta = plan.get("meta") or {}
    wbs = plan.get("wbs") or {}
    deps = plan.get("dependencies") or []
    params = meta.get("extracted_params") or {}
    boundary = meta.get("boundary_conditions") or {}

    def durations():
        out = []
        for ph in wbs.get("phases", []):
            for wp in ph.get("work_packages", []):
                for sp in wp.get("sub_packages", []):
                    out.append((sp.get("id"), sp.get("name"), sp.get("duration_days")))
        return out

    before = {"total": (plan.get("overview") or {}).get("total_duration_days"),
              "versions": meta.get("schedule_versions")}

    ctx = {"wbs": wbs, "prompt": "", "extracted_params": params,
           "boundary_conditions": boundary, "dependencies": deps}
    if meta.get("kb_scope"):
        ctx["kb_scope"] = meta["kb_scope"]

    _silence(_nb.NormBindNode(llm=None)).run(ctx)
    _silence(_crew.CrewBindNode()).run(ctx)
    out = _silence(_sched.SchedulerNode()).run(ctx)

    ver = out.get("schedule_versions") or {}
    ok = ver.get("resource_ok") or {}
    th = ver.get("theory_min") or {}

    result = {
        "plan_id": plan.get("plan_id"),
        "before": before,
        "after": {
            "theory_min_days": th.get("total_duration_days"),
            "resource_ok_days": ok.get("total_duration_days"),
            "critical_path_len": len(ok.get("critical_path") or []),
            "peak_labor": ok.get("peak_labor"),
        },
        "norm_coverage": out.get("norm_coverage") or {},
        "schedule_warnings": (out.get("schedule_warnings") or [])[:12],
        "durations": durations(),
    }

    if verbose:
        print("== %s 确定性重演 ==" % result["plan_id"])
        print("  改前：%s 天（%s）" % (before["total"], before["versions"]))
        print("  改后：理论最短 %s 天 / 资源不超额 %s 天"
              % (result["after"]["theory_min_days"],
                 result["after"]["resource_ok_days"]))
        print("  关键路径条数 %s，峰值 %s 人"
              % (result["after"]["critical_path_len"], result["after"]["peak_labor"]))
        cov = result["norm_coverage"]
        if cov:
            print("  定额覆盖：%s/%s = %s%%（未绑 %s）"
                  % (cov.get("bound", "?"), cov.get("total", "?"),
                     cov.get("bound_pct", "?"), cov.get("unbound", "?")))
            for k, v in (cov.get("by_reason") or {}).items():
                print("      - %-46s %s" % (k, v))
        if result["schedule_warnings"]:
            print("  排程警告（前 12 条）：")
            for w in result["schedule_warnings"]:
                print("      * %s" % w)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    plan, path = load_plan(args.plan)
    print("（计划来源：%s）" % path)
    res = replay(plan)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print("  明细 → %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
