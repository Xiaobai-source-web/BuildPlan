# -*- coding: utf-8 -*-
"""冻结 WBS + 依赖的确定性重演（**不调 LLM**），跑到资源与交付装配为止。

与 backend/tools/replay_plan.py 的区别：它只跑到 scheduler（工期口径），
本脚本继续跑 **resource → plan_assembler**，所以能看到"机械种类/台数、配员、
总工日、ALC 班组"这些**交付物上的数字**，并且**与模型的随机性无关**（同一份 WBS、
同一份依赖、只换代码）—— 这正是"改代码造成了什么"的干净答案。

用法：
    python devtools/replay_full.py --plan plan_run_1789827002 --tag after_fix
    python devtools/replay_full.py --plan plan_run_1789827002 --tag after_fix --json out.json
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "devtools"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pipeline import plan_store as _ps                      # noqa: E402
from pipeline.nodes import crew_bind as _crew                # noqa: E402
from pipeline.nodes import norm_bind as _nb                  # noqa: E402
from pipeline.nodes import plan_assembler as _pa             # noqa: E402
from pipeline.nodes import resource as _res                  # noqa: E402
from pipeline.nodes import scheduler as _sched               # noqa: E402

_spec = importlib.util.spec_from_file_location("rerun_sample3", ROOT / "devtools" / "rerun_sample3.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
summarise = _mod.summarise

PLANS = [ROOT / "backend" / "plans", ROOT / "terminal" / "plans"]


def load_plan(plan_id):
    """与 backend/tools/replay_plan.py 同序：**先档案版本**（流水线真实写出的那份），
    再退回 plans/<id>.json。两者形状不同，读错那个会让排程节点一条叶子都找不到。"""
    for base in PLANS:
        for p in (base / "档案" / plan_id / "当前版本.json", base / (plan_id + ".json")):
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")), p
    raise SystemExit("找不到计划 %s" % plan_id)


def _silence(node):
    node._emit = lambda event, data: None
    return node


def replay(plan, cadence=None, apply_deps=False):
    meta = plan.get("meta") or {}
    # ⚠️ 计划 JSON 里存的是**列表**，而流水线里 `deps_gen` 写的是
    # `{"dependencies": [...]}`（`deps_gen.py:124`），`plan_assembler.py:595` 与
    # `cpm.py:213` 都按**字典**取。喂列表会在装配节点报
    # `'list' object has no attribute 'get'`。这里统一成字典。
    deps = plan.get("dependencies") or []
    if isinstance(deps, list):
        deps = {"dependencies": deps}
    ctx = {
        "wbs": plan.get("wbs") or {},
        "prompt": "",
        "extracted_params": meta.get("extracted_params") or {},
        "boundary_conditions": meta.get("boundary_conditions") or {},
        "dependencies": deps,
    }
    if meta.get("kb_scope"):
        ctx["kb_scope"] = meta["kb_scope"]
    for key in ("extracted_params", "params"):
        if meta.get(key):
            ctx[key] = meta[key]

    # --cadence：注入"标准层节拍（天/层）"以打开施工组织层（WS6 的生效条件）。
    # 契约落点：boundary_conditions["cadence_days"] + cadence_scope + _source。
    # 不加这个参数 → 组织层不生效 → 用于验证"无节拍路径与旧代码逐位一致"。
    if cadence:
        bc = dict(ctx["boundary_conditions"])
        bc["cadence_days"] = float(cadence)
        bc.setdefault("cadence_scope", "标准层")
        src = dict(bc.get("_source") or {})
        src["cadence_days"] = "user"
        src.setdefault("cadence_scope", "user")
        bc["_source"] = src
        ctx["boundary_conditions"] = bc
        print("已注入节拍：cadence_days=%s 天/层（_source=user）" % bc["cadence_days"])

    # ⚠️ 节点**不直接改 ctx**：`engine.py:186-196` 是 `result = node.run(ctx)` 然后
    # `ctx.update(result)`。所以这里必须自己把返回值并回 ctx，否则后面的节点什么都读不到
    # （表现：任务数 0、工期 0、机械为空 —— 我第一版就是这个坑）。
    # ⚠️ 默认**不**跑依赖生成节点：`deps` 直接取计划里冻结的那份。
    # 后果（实测）：`parallelize_companion_deps()`（WS5「伴随型工序不锁主体」）
    # 在这条重放里**从未被执行** —— 曾因此误把 665 天当成"含该修复"的结果。
    # 加 `--apply-deps` 才会在这里跑一遍 `deps_gen.ensure_dependencies`。
    if apply_deps:
        from pipeline.nodes import deps_gen as _dg
        _deps_list = deps.get("dependencies") if isinstance(deps, dict) else deps
        _fixed, _dwarns, _dapplied = _dg.ensure_dependencies(
            list(_deps_list or []), ctx["wbs"])
        deps = {"dependencies": _fixed}
        print("已跑依赖规则 ensure_dependencies：改动 %s 条" % (_dapplied or 0))
        for _w in (_dwarns or []):
            print("   [deps] %s" % (_w.get("message") if isinstance(_w, dict) else _w))

    ctx["dependencies"] = deps

    for node in (_nb.NormBindNode(llm=None), _crew.CrewBindNode(), _sched.SchedulerNode(),
                 _res.ResourceNode()):
        ctx.update(_silence(node).run(ctx) or {})

    parts = _pa.build_parts(ctx)
    out = _pa.assemble_plan_json(ctx, parts, report="")
    return ctx, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--tag", default="replay")
    ap.add_argument("--json", default="")
    ap.add_argument("--cadence", type=float, default=None,
                    help="注入标准层节拍（天/层）以打开组织层；不填 = 旧行为对照")
    ap.add_argument("--apply-deps", action="store_true",
                    help="跑一遍 deps_gen.ensure_dependencies（含 WS5 伴随型工序并行化）。"
                         "默认关：deps 直接取计划里冻结的那份，"
                         "此时伴随型工序的 FS→SS 改型不会发生")
    args = ap.parse_args()

    plan, path = load_plan(args.plan)
    print("计划来源：%s" % path)
    ctx, out = replay(plan, cadence=args.cadence, apply_deps=args.apply_deps)

    # 交付物形状的摘要（与 compare_plans / rerun_sample3 同一口径）
    out2 = dict(out)
    out2["resource_demand"] = ctx.get("resource_demand") or out.get("resource_demand")
    summary = summarise(out2)

    dest = ROOT / "backend" / "_probe_tmp" / ("replay_%s.json" % args.tag)
    dest.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    rp = summary["resource_plan"]
    print("=" * 78)
    print("冻结 WBS 重演结果（tag=%s）" % args.tag)
    print("  任务数            : %s（无资源 %s）" % (summary["task_count"],
                                                   summary["tasks_without_resource"]))
    print("  总工期            : %s 天" % summary["total_duration_days"])
    print("  总人工日          : %s" % rp["total_manpower_days"])
    print("  峰值人数          : %s（来源 %s）" % (rp["peak_manpower"], rp["peak_manpower_source"]))
    print("  曲线峰值          : %s   申报峰值：%s" % (rp["curve_peak_manpower"],
                                                     rp["declared_peak_manpower"]))
    print("  机械峰值          : %s" % json.dumps(rp["equipment_peak"], ensure_ascii=False))
    print("  配员峰值          : %s" % json.dumps(rp["machine_crew_peak"], ensure_ascii=False))
    print("  机械名清单        : %s" % json.dumps(summary["machine_names"], ensure_ascii=False))
    print("  配员名清单        : %s" % json.dumps(summary["crew_names"], ensure_ascii=False))
    print("  无资源原因        :")
    for k, v in sorted((summary["unbound_reasons"] or {}).items(), key=lambda kv: -kv[1]):
        print("      %-46s %s" % (k[:46], v))
    alcs = [a for a in summary["alcs"] if a["resources"]]
    print("  ALC 有资源的条目  : %d / %d" % (len(alcs), len(summary["alcs"])))
    if alcs:
        a = alcs[0]
        print("      例：%s %s → %s" % (a["task_id"], a["task_name"],
                                      json.dumps(a["resources"], ensure_ascii=False)))
        print("      假定：%s" % str(a["assumed"])[:200])
    print("  摘要落盘          : %s" % dest)

    # 整份重演计划落盘（交付物形状）——用于逐任务对照"有/无节拍"的工期差。
    full = ROOT / "backend" / "_probe_tmp" / ("replay_%s_full.json" % args.tag)
    full.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print("  整份计划落盘      : %s" % full)

    # 标准层与二次结构的逐条对照（组织层效果的直接证据）
    rows = out.get("all_tasks_schedule") or []
    picks = [r for r in rows if str(r.get("task_id", "")).startswith(("5.1.1.", "6.1.1."))]
    if picks:
        print("  标准层/二次结构逐条（工期 / 组织层）：")
        for r in sorted(picks, key=lambda x: str(x.get("task_id"))):
            org = r.get("_organization") or {}
            if org:
                tail = ("节拍 %s 面 %s 人/面 %s η %s 有效 %s → 工期 %s%s"
                        % (org.get("cadence_days"), org.get("n_faces"),
                           org.get("crew_per_face"), org.get("eta"),
                           org.get("effective_crew_total"), org.get("duration_days"),
                           "" if org.get("feasible", True) else "  ⚠不可行 t_min=%s" % org.get("t_min_days")))
            else:
                tail = "（无 _organization —— 组织层未生效）"
            print("      %-11s %-24s 工期 %-4s %s" % (
                r.get("task_id"), str(r.get("task_name"))[:24],
                r.get("duration_days"), tail))
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("（同一份内容 → %s）" % args.json)


if __name__ == "__main__":
    main()
