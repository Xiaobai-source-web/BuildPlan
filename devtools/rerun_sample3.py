# -*- coding: utf-8 -*-
"""复跑「示例3_住宅楼」完整 26 节点流水线（真实 LLM），用于改动前后的 A/B 对比。

与 devtools/smoke_real_llm.py 的区别：
  · 输入固定为 `项目样例/示例3_住宅楼.txt` 的**原文**（与上一次 plan_run_1789827002 同源）；
  · **不做目录隔离** —— 产物直接落到真实的 `backend/plans` 与 `输出结果/`，
    这样终端界面能直接看到这次复跑；plan_id 由流水线自动生成（plan_run_<ts>），
    不会覆盖上一次的产物。
  · 自动应答全部人工门；每个门的事件载荷都记进日志（便于诊断卡门）。
  · 跑完把关键指标（resource_plan / 机械名 / ALC 18 条 / 总工日）落一份 JSON 摘要到
    `backend/_probe_tmp/rerun_<tag>.json`，供对比脚本消费。

用法：
    python devtools/rerun_sample3.py                # 默认 tag=rerun
    python devtools/rerun_sample3.py --tag after_fix --timeout 2700
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pipeline import config                      # noqa: E402
from pipeline import usage as usage_mod          # noqa: E402
from pipeline.builder import build_pipeline      # noqa: E402
from pipeline.llm import LLMClient               # noqa: E402

# 分类口径**必须与交付物一致**，否则"机械"里会混进工种、对比就成了噪音。
# 直接借 delivery 的现成集合（LABOR / MACHINE_CREW / _is_labor），不再自己抄一份。
try:
    from pipeline.nodes import delivery as _dlv      # noqa: E402
    _LABOR_SET = set(getattr(_dlv, "LABOR", ()))
    _CREW_SET = set(getattr(_dlv, "MACHINE_CREW", ()))
except Exception as _exc:                            # pragma: no cover
    print("（警告：无法从 delivery 借分类集合，退回内置表：%s）" % str(_exc)[:80])
    _LABOR_SET = {"普工", "钢筋工", "模板工", "混凝土工", "瓦工", "抹灰工", "油漆工",
                  "防水工", "电工", "管工", "架子工", "砌筑工", "木工", "测量工"}
    _CREW_SET = {"泵工", "辅助", "操作工", "司机", "信号工"}

SAMPLE = ROOT / "项目样例" / "示例3_住宅楼.txt"
OUTDIR = ROOT / "backend" / "_probe_tmp"


def summarise(plan):
    """从跑完的 plan_json 里抠出对比要用的指标（只读，不改计划）。"""
    rp = plan.get("resource_plan") or {}
    meta = plan.get("meta") or {}
    tasks = ((plan.get("resource_demand") or {}).get("tasks")) or []

    machines = {}
    crews = {}
    trades = {}
    alcs = []
    no_res = 0
    flagged = {}
    for t in tasks:
        res = t.get("resources") or {}
        if not res:
            no_res += 1
            why = t.get("_norm_flagged") or t.get("_warning") or "(无字段)"
            flagged[str(why)[:40]] = flagged.get(str(why)[:40], 0) + 1
        for name, q in res.items():
            per_day = (q or {}).get("per_day")
            if isinstance(per_day, (int, float)) and per_day == int(per_day):
                per_day = int(per_day)
            if name in _CREW_SET:
                crews.setdefault(name, []).append(per_day)
            elif name in _LABOR_SET:
                trades.setdefault(name, []).append(per_day)
            else:
                machines.setdefault(name, []).append(per_day)
        nm = str(t.get("task_name") or "")
        if "ALC" in nm.upper():
            alcs.append({
                "task_id": t.get("task_id"), "task_name": nm,
                "quantity": t.get("quantity"), "duration": t.get("planned_duration_days"),
                "resources": res,
                "assumed": t.get("_unit_assumed") or t.get("_assumption_note"),
            })

    schedule = plan.get("all_tasks_schedule") or []
    return {
        "plan_id": plan.get("plan_id"),
        "project": (plan.get("overview") or {}).get("project_name"),
        "total_duration_days": (plan.get("overview") or {}).get("total_duration_days"),
        "task_count": len(tasks),
        "tasks_without_resource": no_res,
        "unbound_reasons": flagged,
        "resource_plan": {
            "total_manpower_days": rp.get("total_manpower_days"),
            "peak_manpower": rp.get("peak_manpower"),
            "peak_manpower_source": rp.get("peak_manpower_source"),
            "curve_peak_manpower": rp.get("curve_peak_manpower"),
            "declared_peak_manpower": rp.get("declared_peak_manpower"),
            "equipment_peak": rp.get("equipment_peak"),
            "machine_crew_peak": rp.get("machine_crew_peak"),
        },
        "machine_names": {k: {"tasks": len(v), "max_per_day": max(v) if v else None}
                          for k, v in sorted(machines.items())},
        "crew_names": {k: {"tasks": len(v), "max_per_day": max(v) if v else None}
                       for k, v in sorted(crews.items())},
        "trade_names": {k: {"tasks": len(v), "max_per_day": max(v) if v else None}
                        for k, v in sorted(trades.items())},
        "schedule_rows": len(schedule),
        "equipment_binding": meta.get("equipment_binding"),
        "norm_coverage": meta.get("norm_coverage"),
        "credibility": meta.get("credibility"),
        "schedule_versions": meta.get("schedule_versions"),
        "alcs": alcs,
        "usage": (meta.get("usage") or {}),
        "paths": plan.get("_paths"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=2700)
    ap.add_argument("--tag", default="rerun")
    ap.add_argument("--input", default="",
                    help="把该文件的**内容**当正文喂进去（仅在要复现「贴全文」形态时用）")
    ap.add_argument("--mode", default="plan", choices=["plan", "normal", "revise", "import"],
                    help="终端模式；上一次运行是 plan（router.py:165-190 只看这个，不看意图）")
    ap.add_argument("--run-id", default="sample3_rerun",
                    help="流水线 run_id（决定 plan_id，例如 plan_<run_id>；换一个就不会覆盖上一次产物）")
    args = ap.parse_args()

    # ⚠️ 输入形态必须与上一次一致，否则连流水线都进不去：
    # 上一次的输入档（`backend/plans/输入/in_20260919_221002_f91bb0.json`，
    # run_id=run_1789827002）是 **类型=文件、文本=""、文件路径=项目样例\示例3_住宅楼.txt**，
    # 即 ctx["prompt"] 就是那条**路径**（doc_load 用 extractor._detect_local_paths 去读），
    # 而不是文件正文；模式由终端选为 plan。实测：把正文当 prompt、且不带 mode
    # → router 按 normal 处理，回一句聊天就收工（0 token、0 门、无 plan_json）。
    if args.input:
        prompt = Path(args.input).read_text(encoding="utf-8")
        input_form = "正文（%d 字，%s）" % (len(prompt), Path(args.input).name)
    else:
        prompt = str(SAMPLE)
        input_form = "附件路径（与上一次输入档一致：类型=文件、文本为空）"

    # ⚠️ 必须在 `build_pipeline()` 之前把「当前档」刷到 config 上。
    # 为什么：`refresh_active_profile()` 只在 `backend/main.py`（Web 服务启动）里调用，
    # `backend/pipeline/` 自己**不读档位文件**；本脚本是在进程内直接跑流水线，
    # 不刷新就会退回 `backend/.env` 的出厂档。实测（2026-09-20）：.env 那条是
    # `token-plan-cn.xiaomimimo.com / mimo-v2.5`，配额已耗尽（HTTP 429），
    # 于是**每个节点都静默走确定性兜底**，照样产出一份看着完整的 plan_json ——
    # 但 `meta.usage.calls == 0`、没有边界条件、任务数也不对。
    src = config.refresh_active_profile()

    print("=" * 74)
    print("复跑示例3（真实 LLM）")
    print("  当前档       : %s" % (src or "（无档位 —— 用 backend/.env 的出厂档）"))
    print("  Key 是否就位 : %s" % ("是（%d 字符）" % len(config.LLM_API_KEY)
                                  if config.LLM_API_KEY else "否 —— 会全部走兜底！"))
    print("  base_url     : %s" % config.LLM_BASE_URL)
    print("  model        : %s" % config.LLM_MODEL)
    print("  输入形态     : %s" % input_form)
    print("  终端模式     : %s" % args.mode)
    print("  plans_dir    : %s" % config.PLANS_DIR)
    print("  产物目录     : %s" % config.DELIVERABLES_DIR)
    print("=" * 74)
    if not config.LLM_API_KEY:
        print("没有 Key，复跑无意义，退出。")
        return 1

    # 预检：先打一发最小对话。调不通就**当场中止**，绝不再跑出一份"0 次调用"的假完整产物
    # （那是上一轮的教训：235 秒跑完、门都答了、交付物齐全，但一次模型都没调用）。
    print("  预检最小对话 : ", end="", flush=True)
    try:
        _probe = LLMClient(timeout=60).chat_text("你是测试助手，只回两个字。", "回复：可用")
        print("✅ %r" % (_probe or "")[:40])
    except Exception as exc:
        print("❌ %s: %s" % (type(exc).__name__, str(exc)[:240]))
        print("-" * 74)
        print("模型调用不通 —— 跑下去只会得到一份「0 次调用」的假完整产物，已中止。")
        print("请先修端点，或在 backend/llm_profiles.json 里换一个能用的 active 档位。")
        return 1

    pipeline = build_pipeline(run_id=args.run_id, llm=LLMClient())
    events = []
    resolved = set()
    gate_log = []
    logf = OUTDIR / ("rerun_%s.log" % args.tag)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    _logh = logf.open("w", encoding="utf-8")

    def log(msg):
        """同时写屏幕与日志文件，并**立刻 flush** —— 管道里的 stdout 是块缓冲的，
        不 flush 就完全看不到进度（上一版就是这样盲跑了 14 分钟）。"""
        print(msg, flush=True)
        _logh.write(msg + "\n")
        _logh.flush()

    def emit(e, d):
        events.append((e, d))

    log("输入形态：%s | 模式：%s | tag：%s | 日志：%s" % (input_form, args.mode, args.tag, logf))

    def answer_gate(key, d, how):
        """应答一个人工门。`how` 只用于日志（事件 vs 登记表轮询）。"""
        if not key or key in resolved:
            return
        resolved.add(key)
        payload_keys = sorted(d.keys()) if isinstance(d, dict) else []
        gate_log.append({"event": (d or {}).get("_event", ""), "node": (d or {}).get("node"),
                         "keys": payload_keys, "how": how})
        # 参数门（param_review）**在必要参数不齐时不接受"按通过"**（会一直追问到超时中止，
        # 见 param_review.py:153-155）。所以这里按**输入原文里的事实**补参数，
        # 不编任何原文没有的数字。
        payload = {"action": "continue", "decision": True, "passed": True,
                   "choice": "continue", "approved": True}
        comp = (d or {}).get("completeness") or {}
        missing = [str(x) for x in (comp.get("missing_required") or [])]
        if missing:
            facts = []
            if any(("栋" in m) or ("building" in m) for m in missing):
                facts.append("栋数：1栋")
            if any(("层" in m) or ("floor" in m) for m in missing):
                facts.append("地上18层，地下1层")
            if any(("面积" in m) or ("area" in m) for m in missing):
                facts.append("总建筑面积：14200平方米")
            facts += ["计划开工日期：2026年6月1日", "工期要求：420日历天"]
            payload["manual_input"] = "；".join(facts)
            log("    （参数门缺 %s → 按输入原文补：%s）" % (missing, payload["manual_input"]))
        log("  · 应答人工门 #%d [%s] event=%s node=%s keys=%s"
            % (len(resolved), how, (d or {}).get("_event"), (d or {}).get("node"), payload_keys))
        try:
            pipeline.registry.resolve(key, payload)
        except Exception as exc:
            log("    （应答失败，忽略：%s）" % str(exc)[:120])

    ctx = {"prompt": prompt, "_run_id": args.run_id, "mode": args.mode}
    t0 = time.time()
    th = threading.Thread(target=lambda: pipeline.run(ctx, emit=emit), daemon=True)
    th.start()

    deadline = t0 + args.timeout
    last = t0
    pending = []
    while time.time() < deadline:
        if not th.is_alive():
            break
        for e, d in list(events):
            if not isinstance(d, dict):
                continue
            d = dict(d, _event=e)
            answer_gate(d.get("pause_id") or d.get("confirm_id") or d.get("review_id"), d, "事件")
        # 兜底：有些节点只 register + wait（事件没到我们手上就永远答不上），
        # 直接轮询登记表里"已登记但还没被 resolve"的键。这是上一版盲跑 14 分钟的教训。
        try:
            reg = pipeline.registry
            with reg._lock:                                     # noqa: SLF001（本项目自用脚本）
                pending = [k for k, ev in reg._events.items() if not ev.is_set()]
            for key in pending:
                answer_gate(key, {"node": "(登记表轮询)", "_event": "poll"}, "轮询")
        except Exception:
            pass
        if time.time() - last > 30:
            last = time.time()
            log("    …已跑 %.0fs，事件 %d 条，门 %d 个，ctx 键 %d 个，登记表待答 %d"
                % (time.time() - t0, len(events), len(resolved), len(ctx), len(pending or [])))
        time.sleep(0.05)

    alive = th.is_alive()
    th.join(timeout=15)
    log("\n" + "=" * 74)
    log("  跑完       : %s（耗时 %.0fs）" % ("是" if not alive else "否（超时）", time.time() - t0))
    log("  事件/门    : %d / %d" % (len(events), len(resolved)))
    try:
        snap = usage_mod.meter().snapshot() or {}
    except Exception:
        snap = {}
    log("  token 用量 : %s" % (snap or "（空 —— 一次真实 LLM 都没调用）"))

    plan = ctx.get("plan_json") or {}
    if not plan:
        log("  未产出 plan_json；ctx 键 = %s" % sorted(ctx.keys())[:24])
        log("  门事件：%s" % json.dumps(gate_log, ensure_ascii=False))
        _logh.close()
        return

    summary = summarise(plan)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    dest = OUTDIR / ("rerun_%s.json" % args.tag)
    dest.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("  plan_id    : %s" % summary["plan_id"])
    log("  总工期     : %s 天" % summary["total_duration_days"])
    log("  任务数     : %s（无资源 %s）" % (summary["task_count"], summary["tasks_without_resource"]))
    rp = summary["resource_plan"]
    log("  资源计划   : 总人工日=%s 峰值人数=%s（来源=%s） 曲线峰值=%s 申报=%s"
        % (rp["total_manpower_days"], rp["peak_manpower"], rp["peak_manpower_source"],
           rp["curve_peak_manpower"], rp["declared_peak_manpower"]))
    log("  机械峰值   : %s" % rp["equipment_peak"])
    log("  配员峰值   : %s" % rp["machine_crew_peak"])
    log("  机械名清单 : %s" % json.dumps(summary["machine_names"], ensure_ascii=False))
    log("  配员名清单 : %s" % json.dumps(summary["crew_names"], ensure_ascii=False))
    log("  设备对账   : %s" % json.dumps(summary["equipment_binding"], ensure_ascii=False)[:400])
    log("  ALC 任务   : %d 条" % len(summary["alcs"]))
    for a in summary["alcs"][:3]:
        log("     - %s %s 资源=%s 假定=%s"
            % (a["task_id"], a["task_name"], a["resources"], a["assumed"]))
    log("  摘要落盘   : %s" % dest)
    log("=" * 74)
    _logh.close()


if __name__ == "__main__":
    sys.exit(main())
