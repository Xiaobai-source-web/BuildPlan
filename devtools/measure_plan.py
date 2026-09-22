# -*- coding: utf-8 -*-
"""工期实测工具（离线 · 确定性 · 只读生产目录）— 开发用，不是交付物。

为什么要它：工期/叶子数/资源峰值这些问题，只有在**真实项目参数**下跑完整流水线
才看得见。本工具把"跑一次 + 出对比表"固定成一条命令，避免每次手敲一长串内联脚本。

特点：
  - **强制 LLM 不可用**（与 test_contracts 同款桩）→ 全走确定性兜底，结果可复现、不联网、不烧额度。
  - 输出目录指向 `backend/_measure_tmp/`，**绝不碰生产目录「输出结果/」**
    （否则会触发 delivery 的 MAX_KEEP=6 滚动淘汰，把真实交付物删掉）。

用法：
    python tools/measure_plan.py                 # 跑内置场景（潭村 12 栋 / 单栋对照）
    python tools/measure_plan.py --top 15        # 多列几条最长任务
"""

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import config                                    # noqa: E402
from pipeline.builder import build_pipeline                    # noqa: E402
from pipeline.llm import LLMError                              # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")                    # Windows 控制台默认 GBK
except Exception:
    pass

TMP = BACKEND / "_measure_tmp"


def _isolate():
    """把交付物/计划目录挪到临时目录，并强制"没有 API Key"。"""
    out = TMP / "outputs"
    plans = TMP / "plans"
    out.mkdir(parents=True, exist_ok=True)
    plans.mkdir(exist_ok=True)
    config.DELIVERABLES_DIR = out
    config.PLANS_DIR = plans
    config.LLM_API_KEY = ""


class _NoLLM(object):
    """强制 LLM 不可用 → 全确定性兜底。"""

    def chat_json(self, *a, **k):
        raise LLMError("measure: 强制无 LLM")

    def chat_text(self, *a, **k):
        raise LLMError("measure: 强制无 LLM")


def run(params_prompt, timeout=180):
    """跑完整流水线（自动应答所有人工门），返回 ctx。"""
    pipeline = build_pipeline(run_id="measure", llm=_NoLLM())
    events = []
    resolved = set()

    def emit(e, d):
        events.append((e, d))

    # ⚠️ `mode: "plan"` 必须给：第 34 轮取消意图识别后，router 只按终端手选的模式
    # 分流；缺 mode 会被当成 normal（纯聊天），流水线会**秒结束且一片空**。
    ctx = {"prompt": params_prompt, "_run_id": "measure", "mode": "plan"}
    t = threading.Thread(target=lambda: pipeline.run(ctx, emit=emit), daemon=True)
    t.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not t.is_alive():
            break
        for e, d in list(events):
            key = d.get("pause_id") or d.get("confirm_id") or d.get("review_id")
            if not key or key in resolved:
                continue
            resolved.add(key)
            try:
                if e == "node_paused":
                    pipeline.registry.resolve(key, {"action": "continue"})
                elif e == "confirm_required":
                    pipeline.registry.resolve(key, {"decision": True})
                elif e == "param_review":
                    pipeline.registry.resolve(key, {"passed": True})
            except Exception:
                pass
        time.sleep(0.05)
    t.join(timeout=5)
    return ctx, events, (not t.is_alive())


# ---------------- 统计 ----------------
def _leaves(wbs):
    return [l for ph in (wbs.get("phases") or [])
            for wp in ph.get("work_packages", [])
            for l in wp.get("sub_packages", [])]


def _phase_by_prefix(wbs):
    """task_id 首段 → 阶段名（节拍叶子 id = 阶段.分区.段.工序）。"""
    m = {}
    for ph in (wbs.get("phases") or []):
        for wp in ph.get("work_packages", []):
            for l in wp.get("sub_packages", []):
                m[str(l.get("id") or "").split(".")[0]] = ph.get("phase") or "?"
                break
            break
    return m


def summarize(ctx, label):
    wbs = ctx.get("wbs") or {}
    leaves = _leaves(wbs)
    sv = ctx.get("schedule_versions") or {}
    th = (sv.get("theory_min") or {}).get("total_duration_days")
    ro = (sv.get("resource_ok") or {}).get("total_duration_days")
    params = ctx.get("extracted_params") or {}
    meta = (ctx.get("plan_json") or {}).get("meta") or {}
    qty_src = {}
    for l in leaves:
        k = l.get("_qty_source") or "（无·非节拍）"
        qty_src[k] = qty_src.get(k, 0) + 1

    row = {
        "label": label,
        "buildings": params.get("building_count"),
        "floors": params.get("floors"),
        "area": params.get("total_area"),
        "leaves": len(leaves),
        "theory_min": th,
        "resource_ok": ro,
        "peak_labor": (sv.get("resource_ok") or {}).get("peak_labor"),
        "credibility": meta.get("credibility"),
        "qty_src": qty_src,
        "warnings": (sv.get("warnings") or [])[:6],
        "sv": sv,
        "wbs": wbs,
    }
    ld, _ = _labor_days(sv, "theory_min")
    row["labor_days"] = ld
    row["crew_design_top"] = _crew_top(sv, "theory_min", 3)
    row["key_warnings"] = [w for w in (sv.get("warnings") or [])
                           if any(k in str(w) for k in KEY_WARN)]
    return row


def _labor_days(sv, tag="theory_min"):
    """该版计划的**定额工日总量** = ∑(班组人数 × 工期)（人工主导任务）。

    这是"以知识库为准"之后最该看的数字：它由定额与工程量决定，**与工期排布无关**。
    拿它跟用户自己计划里的 total_manpower_days 一比，就能看出两边的产能口径差多少。

    只读 schedule 行（crew + es/ef）：`_planned` 这类下划线字段在经过完整流水线后
    会被剥掉（schemas/引擎不留内部字段），所以不能在工具里依赖它。
    """
    rows = (sv.get(tag) or {}).get("schedule") or []
    total = 0.0
    for row in rows:
        crew = row.get("crew") or {}
        d = max(1, int((row.get("ef") or 0) - (row.get("es") or 0)))
        total += sum(float(v) for k, v in crew.items() if not is_machine_like(k)) * d
    return total, {}


def _crew_top(sv, tag="theory_min", n=3):
    """编制规模最大的 n 条任务（人 × 天），用于快速看清"班组有多大"。"""
    rows = (sv.get(tag) or {}).get("schedule") or []
    out = []
    for row in rows:
        crew = row.get("crew") or {}
        people = sum(float(v) for v in crew.values())
        d = max(1, int((row.get("ef") or 0) - (row.get("es") or 0)))
        out.append((people, d, crew))
    out.sort(key=lambda t: -t[0])
    return "；".join("%s人 × %d 天" % ("+".join("%s%d" % (k, int(v)) for k, v in sorted(c.items())),
                                       int(d)) for p, d, c in out[:n])


# 这些是"口径类"警告：不看清它们就读不懂工期数字，所以不受"只显示前 6 条"的限制
KEY_WARN = ("设计班组按", "定额工日需求合计", "机械主导任务", "机械主导人工需求",
            "全项目共", "AI 经验估算", "单位不一致", "缺工作面容量数据")


_MACHINE_HINTS = ("车", "机", "泵", "吊", "塔", "夯", "钻", "锯", "焊", "搅")


def is_machine_like(name):
    """粗判资源名是不是机械（只用于统计工日，不影响排程）。"""
    s = str(name or "")
    return any(h in s for h in _MACHINE_HINTS) and "工" not in s


def print_row(r):
    print("=" * 78)
    print("【%s】 栋数=%s 层数=%s 总建筑面积=%s㎡" %
          (r["label"], r["buildings"], r["floors"], r["area"]))
    print("  叶子数 %-6s  理论最短 %-6s 天   资源不超额 %-6s 天   人工峰值 %s" %
          (r["leaves"], r["theory_min"], r["resource_ok"], r["peak_labor"]))
    print("  定额工日总量（理论版）：%s 人日" % _fmt_int(r.get("labor_days")))
    print("  单层量来源：%s" % r["qty_src"])
    print("  可信度：%s" % (r["credibility"],))
    if r.get("crew_design_top"):
        print("  设计班组（编制规模最大的几项）：%s" % r["crew_design_top"])
    if r["warnings"]:
        print("  提示：")
        for w in r["warnings"]:
            print("    - %s" % str(w)[:160])
    for w in r.get("key_warnings") or []:
        print("  ★ %s" % str(w)[:400])


def _fmt_int(v):
    try:
        return "{:,}".format(int(round(float(v))))
    except (TypeError, ValueError):
        return "-"


def print_phases(r, top=12):
    """按阶段看工期分布，再列最长的若干条任务。"""
    pmap = _phase_by_prefix(r["wbs"])
    nmap = {str(l.get("id")): l.get("name") for l in _leaves(r["wbs"])}
    sv = r["sv"]
    for tag in ("theory_min", "resource_ok"):
        rows = (sv.get(tag) or {}).get("schedule") or []
        if not rows:
            continue
        agg = {}
        for t in rows:
            p = pmap.get(str(t.get("task_id", "")).split(".")[0], "?")
            a = agg.setdefault(p, {"n": 0, "last": 0})
            a["n"] += 1
            a["last"] = max(a["last"], t.get("ef") or 0)
        print("  [%s] 各阶段收口天数（该阶段最后完成日）：" % tag)
        for p, a in sorted(agg.items(), key=lambda kv: -kv[1]["last"]):
            print("    %-16s %-5s 条  收口第 %s 天" % (p, a["n"], a["last"]))

    rows = (sv.get("resource_ok") or {}).get("schedule") or []
    if rows:
        # schedule 行只有 es/ef/crew（工期 = ef-es），任务名要去 WBS 里按 id 取
        longest = sorted(rows, key=lambda t: -((t.get("ef") or 0) - (t.get("es") or 0)))[:top]
        print("  [resource_ok] 最长的 %d 条任务：" % top)
        for t in longest:
            tid = str(t.get("task_id"))
            d = (t.get("ef") or 0) - (t.get("es") or 0)
            print("    %-44s %4s 天  (第%s→%s天)  班组=%s%s" %
                  (str(nmap.get(tid) or tid)[:44], d, t.get("es"), t.get("ef"),
                   {k: int(v) for k, v in (t.get("crew") or {}).items()},
                   "  [已封顶]" if t.get("capped") else ""))
        biggest = sorted(rows, key=lambda t: -sum((t.get("crew") or {}).values()))[:top]
        print("  [resource_ok] 班组人数最多的 %d 条任务（定额驱动的编制规模）：" % top)
        for t in biggest:
            tid = str(t.get("task_id"))
            print("    %-44s %4s 人  %s" %
                  (str(nmap.get(tid) or tid)[:44],
                   int(sum((t.get("crew") or {}).values())),
                   {k: int(v) for k, v in (t.get("crew") or {}).items()}))


def _tan(extra=""):
    # ⚠️ 「结构形式」「基础类型」必须写在提示词里：它们是 boundary 的硬必要键
    # （REQUIRED_KEYS + ABSOLUTE_KEYS），无 LLM 的确定性兜底路径下缺了会被参数门拦死，
    # 整条流水线 2 秒就停（叶子数 0）。本工具强制 LLM 不可用，所以只能靠提示词给全。
    return ("广州市白云区潭村城中村改造项目首开区安置地块，共 12 栋，地上 38 层，"
            "结构形式：框架-剪力墙结构，基础类型：筏板基础，"
            "总建筑面积 21.5万㎡，混凝土 8.2万m³，钢筋 1.28万吨，" + extra + "开工 2025-04-16")


SCENARIOS = [
    ("潭村12栋·自由编制（不给资源限额）", _tan()),
    ("潭村12栋·全场480人（40人/栋）", _tan("总劳动力峰值 480 人，塔吊 12 台，")),
    ("潭村12栋·全场240人（20人/栋）", _tan("总劳动力峰值 240 人，塔吊 12 台，")),
    ("潭村12栋·全场120人（10人/栋）", _tan("总劳动力峰值 120 人，塔吊 12 台，")),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--only", default="", help="只跑标签包含该子串的场景")
    args = ap.parse_args()

    _isolate()
    rows = []
    for label, prompt in SCENARIOS:
        if args.only and args.only not in label:
            continue
        t0 = time.time()
        ctx, events, finished = run(prompt)
        r = summarize(ctx, label)
        r["secs"] = round(time.time() - t0, 1)
        r["finished"] = finished
        rows.append(r)
        print_row(r)
        print("  耗时 %.1fs  流水线结束=%s" % (r["secs"], r["finished"]))
        print_phases(r, args.top)

    print("=" * 78)
    print("两版工期对比表")
    print("%-24s %8s %10s %12s %8s" % ("场景", "叶子数", "理论最短(天)", "资源不超额(天)", "人工峰值"))
    for r in rows:
        print("%-24s %8s %10s %12s %8s" %
              (r["label"], r["leaves"], r["theory_min"], r["resource_ok"], r["peak_labor"]))


if __name__ == "__main__":
    main()
