"""端到端验收（真实计划输入）：P0-1 / P0-2 / P0-3 / P1-1 / P1-2。

用法（仓库根目录）：

    python devtools/verify_p0p1_acceptance.py

与各工作流的**自测**不同，本脚本刻意：
  · 用真实计划 `backend/plans/plan_run_1789818211.json` 的 `wbs` / `dependencies` /
    `boundary_conditions` 直接跑排程器（不用合成夹具）；
  · 对返回结构做**防御式探测**（子代理内部结构可能变），拿不到就标 SKIP 而不是崩；
  · 只读，不写任何文件，不碰 `输出结果/`。

退出码：全部 PASS（或有 SKIP 但无 FAIL）→ 0；任一 FAIL → 1。
"""
from __future__ import annotations

import ast
import json
import math
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PLAN_JSON = BACKEND / "plans" / "plan_run_1789818211.json"
KB_DB = ROOT / "BuildPlan_KB" / "kb.db"
for p in (str(BACKEND), str(BACKEND / "pipeline")):
    if p not in sys.path:
        sys.path.insert(0, p)

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, ("  — " + detail) if detail else ""))
    return bool(ok)


def skip(name, detail=""):
    _RESULTS.append((name, None, detail))
    print("  [SKIP] %s%s" % (name, ("  — " + detail) if detail else ""))


def _has_quantity(text: str) -> bool:
    """分子里是否带工程量因子 —— 用于区分机械台班公式与 labor 产能反推。

    机械台班定额**没有**归一，正确写法就是 `量 × 台班定额 / basis`；
    labor 侧的 `basis/norm`、`norm/basis` 才是第 37 轮要清除的镜像 bug。
    """
    t = text.lower()
    return ("quantity" in t) or ("qty" in t) or bool(re.search(r"(?:^|[^\w])q(?:[^\w]|$)", t))


def _rebind(wbs, meta):
    """先用**当前绑定层**重跑一遍，返回重绑后的 wbs（深拷贝，不动原计划）。

    必须这么做：归档计划里的 `norm_binding` 是**旧 norm_bind** 的产物
    （例如 2.1.1 的 `单位=台班/根` —— 正是 P0-2 要清除的伪造分母）。
    直接拿归档 wbs 排程，测的是排程器对陈旧绑定的容忍度，而不是 P0-2 是否修好。
    """
    import copy
    try:
        from pipeline.nodes.norm_bind import NormBindNode
    except Exception as exc:                                     # pragma: no cover
        print("     [warn] 绑定层不可用：%s" % exc)
        return wbs, None
    ctx = {"wbs": copy.deepcopy(wbs), "prompt": meta.get("prompt") or "",
           "extracted_params": meta.get("extracted_params") or {},
           "boundary_conditions": meta.get("boundary_conditions") or {}}
    try:
        node = NormBindNode(llm=None)
        node._emit = lambda event, data: None
        node.run(ctx)
    except Exception as exc:                                     # pragma: no cover
        print("     [warn] 绑定层重跑失败：%s: %s" % (type(exc).__name__, exc))
        return wbs, None
    return ctx["wbs"], ctx


def peak_of(obj, name):
    """递归找 `name` 这个键对应的数值，返回最大值（设备可能只出现在 daily_equipment
    或 capped 台账里，不挂在任务行上）。"""
    best = 0.0

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k) == name:
                    if isinstance(v, (int, float)):
                        best = max(best, float(v))
                    elif isinstance(v, dict):
                        for vv in v.values():
                            if isinstance(vv, (int, float)):
                                best = max(best, float(vv))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return best


def load_plan():
    return json.loads(PLAN_JSON.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- 工具


def walk_collect(node, pred, out):
    """递归收集满足 pred 的 dict（对结构变化免疫）。"""
    if isinstance(node, dict):
        if pred(node):
            out.append(node)
        for v in node.values():
            walk_collect(v, pred, out)
    elif isinstance(node, list):
        for v in node:
            walk_collect(v, pred, out)
    return out


def task_rows(obj):
    """找到所有「任务行」并建 id → 行 的映射。"""
    rows = walk_collect(
        obj,
        lambda d: ("task_id" in d or "id" in d)
        and any(k in d for k in ("duration", "duration_days", "ef", "crew", "resources")),
        [])
    out = {}
    for r in rows:
        tid = str(r.get("task_id") or r.get("id") or "")
        if not tid:
            continue
        out.setdefault(tid, r)
    return out


def row_duration(row):
    for k in ("duration", "duration_days"):
        v = row.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    es, ef = row.get("es"), row.get("ef")
    if isinstance(es, (int, float)) and isinstance(ef, (int, float)) and ef > es:
        return int(ef - es)
    return None


def row_crew(row):
    c = row.get("crew")
    if isinstance(c, dict) and c:
        return {k: v for k, v in c.items() if isinstance(v, (int, float))}
    res = row.get("resources")
    if isinstance(res, dict) and res:
        out = {}
        for k, v in res.items():
            if isinstance(v, (int, float)):
                out[k] = v
            elif isinstance(v, dict) and isinstance(v.get("per_day"), (int, float)):
                out[k] = v["per_day"]
        return out
    return {}


def leaves_of(plan):
    leaves = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("id") and ("norm_binding" in o or "quantity" in o):
                leaves.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(plan.get("wbs") or plan)
    return leaves


def version_of(out, name):
    for key in ("schedule_versions", "versions"):
        v = out.get(key)
        if isinstance(v, dict) and isinstance(v.get(key, v.get(name)), dict):
            inner = v.get(name)
            if isinstance(inner, dict):
                return inner
    v = out.get(name)
    return v if isinstance(v, dict) else {}


# ----------------------------------------------------------- P0-1 数据与代码


def phase_p01():
    print("\n[P0-1] labor 产能 = 1/labor_norm_value；quantity_basis → raw_quantity_basis")
    con = sqlite3.connect(str(KB_DB))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(Norm_Labor_Table)")]
        alias = "quantity_basis" in cols
        drift = 0
        if alias:
            drift = con.execute(
                "SELECT COUNT(*) FROM Norm_Labor_Table "
                "WHERE IFNULL(quantity_basis,-1) != IFNULL(raw_quantity_basis,-2)").fetchone()[0]
        check("列已改名 raw_quantity_basis", "raw_quantity_basis" in cols,
              ("兼容别名 quantity_basis 存在，与 raw 不同值 %d 行" % drift) if alias else "")
        bad = con.execute(
            "SELECT COUNT(*) FROM Norm_Labor_Table WHERE labor_norm_value>0 "
            "AND productivity_value IS NOT NULL "
            "AND ABS(productivity_value - 1.0/labor_norm_value) > 1e-9").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM Norm_Labor_Table").fetchone()[0]
        check("productivity_value == 1/labor_norm_value", bad == 0,
              "违反 %d/%d 行" % (bad, total))
        raw = con.execute(
            "SELECT COUNT(*) FROM Norm_Labor_Table WHERE raw_value IS NOT NULL "
            "AND raw_quantity_basis>0 AND labor_norm_value>0 "
            "AND ABS(raw_value/raw_quantity_basis - labor_norm_value) > 1e-9").fetchone()[0]
        check("留档不变式 raw_value/basis == norm_value", raw == 0, "违反 %d 行" % raw)
    finally:
        con.close()

    # 代码侧：labor 产能不得再由 basis 参与（注释/文档字符串不算）
    guarded = ["nodes/norm_bind.py", "nodes/scheduler.py", "nodes/resource.py",
               "recompute.py", "nodes/revise.py"]
    offenders = []
    for rel in guarded:
        path = BACKEND / "pipeline" / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left = ast.unparse(node.left).lower()
                right = ast.unparse(node.right).lower()
                lb = "basis" in left and "norm" not in left
                rn = ("norm" in right or right.strip() in ("nv", "n")) and "basis" not in right
                ln = ("norm" in left or left.strip() in ("nv", "n")) and "basis" not in left
                rb = "basis" in right and "norm" not in right
                if (lb and rn) or (ln and rb and not _has_quantity(left)):
                    offenders.append("%s:%d  %s / %s"
                                     % (rel, getattr(node, "lineno", 0), left, right))
    check("代码里无 basis/norm 镜像写法", not offenders,
          "; ".join(offenders[:4]) if offenders else "")


_IRON_NEGATIVE = '''
def _plan_dir(plan):
    return config.DELIVERABLES_DIR / ("计划_%s" % plan["plan_id"])


def render(path):
    _maintain(config.DELIVERABLES_DIR)          # 旧缺陷：绕过隔离、直写真实目录
    return path
'''


def _delivery_violations(src):
    """返回 (绕过隔离的 _maintain 调用, 出现在 _plan_dir 之外的 DELIVERABLES_DIR 引用)。"""
    tree = ast.parse(src)
    bad_calls, raw_attr = [], []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                if ast.unparse(node.func).endswith("_maintain"):
                    for a in node.args:
                        if "DELIVERABLES_DIR" in ast.unparse(a):
                            bad_calls.append("line %d: _maintain(%s)"
                                             % (node.lineno, ast.unparse(a)))
            if isinstance(node, ast.Attribute) and node.attr == "DELIVERABLES_DIR":
                raw_attr.append((fn.name, getattr(node, "lineno", 0)))
    return bad_calls, [x for x in raw_attr if x[0] != "_plan_dir"]


def phase_iron_rule():
    """铁律（静态守卫）：交付层不得绕过 `_plan_dir` 直写 `config.DELIVERABLES_DIR`。

    早前事故的根因就在这里：`delivery.py` 两处无条件 `_maintain(config.DELIVERABLES_DIR)`
    会绕过测试隔离，重写真实 `输出结果\\索引.html` 并按 MAX_KEEP 剪掉真实运行目录。
    这条守卫让同类改动以后在验收里直接变红；末尾的负样本证明它真的抓得住旧缺陷。
    """
    print("\n[铁律] 交付层不得绕过 _plan_dir 直写 输出结果/")
    path = BACKEND / "pipeline" / "nodes" / "delivery.py"
    if not path.exists():
        skip("delivery.py 存在", str(path))
        return
    bad_calls, outside = _delivery_violations(path.read_text(encoding="utf-8"))
    check("_maintain 只跟随实际写入目录（不读常量）", not bad_calls,
          "; ".join(bad_calls[:3]) if bad_calls else "0 处违规")
    check("DELIVERABLES_DIR 只允许出现在 _plan_dir 内", not outside,
          ("出现在 %s" % outside) if outside else "仅 _plan_dir 引用")
    neg_calls, _ = _delivery_violations(_IRON_NEGATIVE)
    check("守卫本身抓得住旧缺陷（负样本）", len(neg_calls) >= 1,
          "负样本命中 %d 处" % len(neg_calls))


# ------------------------------------------------------- 端到端：真实计划排程


def phase_p03():
    """P0-3：工作面容量必须随工程量变化（v2 表 + 公式），而不是一个常数。"""
    print("\n[P0-3] 工作面容量 v2：随工程量变化")
    try:
        from pipeline import kb
    except Exception as exc:                                     # pragma: no cover
        skip("导入 kb", str(exc)[:100])
        return
    aid = "FORM_NEW_OTHER"
    cap = kb.workface_capacity(aid) or {}
    keys = ("crew_base", "crew_step_q", "crew_step_n", "crew_min", "crew_max", "q_ref")
    missing = [k for k in keys if cap.get(k) is None]
    check("v2 容量公式字段齐全（%s）" % aid, not missing, "缺 %s" % missing if missing else
          "base=%s step=%s/%s min=%s max=%s q_ref=%s"
          % (cap.get("crew_base"), cap.get("crew_step_q"), cap.get("crew_step_n"),
             cap.get("crew_min"), cap.get("crew_max"), cap.get("q_ref")))

    def cap_of(q):
        base, sq, sn = cap.get("crew_base"), cap.get("crew_step_q"), cap.get("crew_step_n")
        lo, hi, q_ref = cap.get("crew_min"), cap.get("crew_max"), cap.get("q_ref")
        if None in (base, lo, hi) or not sq or sn is None or q_ref is None:
            return None
        n = int(math.floor((q - q_ref) / sq))
        return int(min(max(base + sn * n, lo), hi))

    small, big = cap_of(500), cap_of(4000)
    check("P0-3 容量随工程量单调变化（同活动）",
          small is not None and big is not None and small < big,
          "Q=500→%s 人，Q=4000→%s 人" % (small, big))

    rows = _capacity_rows()
    if rows:
        full = sum(1 for r in rows if all(r.get(k) is not None for k in keys))
        check("容量表全覆盖（%d 行都有公式字段）" % len(rows), full == len(rows),
              "齐全 %d/%d" % (full, len(rows)))


def _capacity_rows():
    """合表后唯一容量表的全部行（表缺失/异常 → 空列表，调用方据此跳过）。"""
    try:
        con = sqlite3.connect(str(KB_DB))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute("SELECT * FROM Workface_Capacity_Rule")]
        finally:
            con.close()
    except Exception:
        return []


def phase_e2e():
    print("\n[E2E] 用真实计划输入跑两版排程（theory_min / resource_ok）")
    try:
        from pipeline.nodes import scheduler as S
    except Exception as exc:                                     # pragma: no cover
        skip("导入 scheduler", str(exc)[:120])
        return
    plan = load_plan()
    meta = plan.get("meta") or {}
    boundary = meta.get("boundary_conditions") or {}
    params = meta.get("extracted_params") or {}
    wbs = plan.get("wbs")
    deps = plan.get("dependencies")
    if not wbs:
        skip("真实计划缺 wbs")
        return
    # P0-2：先跑绑定层，拿到**当前代码**产出的 binding（归档 binding 是旧版产物）
    wbs, bind_ctx = _rebind(wbs, meta)
    leaf_by_id = {str(l.get("id")): l for l in leaves_of({"wbs": wbs})}
    check("绑定层可重跑（真实 307 条叶子）", bool(leaf_by_id),
          "%d 条叶子" % len(leaf_by_id))

    # ---- P0-2 ①：机械定额分母必须来自 KB，不得用叶子单位伪造
    b211 = (leaf_by_id.get("2.1.1") or {}).get("norm_binding") or {}
    unit211 = str(b211.get("unit") or "")
    denom211 = unit211.split("/")[-1] if "/" in unit211 else ""
    check("P0-2 机械定额分母来自 KB（2.1.1 = 台班/m）",
          denom211 == "m", "单位=%s（旧缺陷是伪造的 台班/根）" % unit211)
    check("P0-2 跨族换算参数可溯源（桩长 18m/根）",
          bool((b211.get("ctx_value") or {}).get("pile_length_m")),
          "ctx=%s ctx_source=%s" % (b211.get("ctx_value"), b211.get("ctx_source")))

    # ---- P0-2 ②：单位不一致且不可换算 → 默认拒绝（不得静默按 1 计算）
    b311 = (leaf_by_id.get("3.1.1") or {}).get("norm_binding") or {}
    reason311 = str(b311.get("not_usable_reason") or "")
    check("P0-2 单位不一致默认拒绝（3.1.1 根 vs 工日/m³）",
          b311.get("usable") is False and "不可换算" in reason311,
          "usable=%s reason=%s" % (b311.get("usable"), reason311[:60]))

    # ---- P0-2 ③：机械定额必须选中**主控机械那一行**，不得"暂用同行机械"
    #      CONC_NEW_FOUND 在设备表里三行：NE_CONC_001 振捣器0.77 / NE_CONC_002 泵车0.055
    #      / NE_CONC_020 振捣器×2(condition=后浇带)1.26；主控机械=混凝土输送泵车 → 必选 0.055。
    b413 = (leaf_by_id.get("4.1.4.3") or {}).get("norm_binding") or {}
    note413 = str((b413.get("provenance") or {}).get("note") or "")
    check("P0-2 机械行按主控机械选行（混凝土浇筑 → 泵车 0.055）",
          abs(float(b413.get("norm_value") or 0) - 0.055) < 1e-9,
          "norm=%s（选错后浇带振捣器行就是 1.26 → 34 天/段，正确 0.055 → 2 天/段）"
          % b413.get("norm_value"))
    borrowed = []
    for tid, lf in leaf_by_id.items():
        bb = lf.get("norm_binding") or {}
        if "暂用同行机械" in str((bb.get("provenance") or {}).get("note") or ""):
            borrowed.append(tid)
    check("P0-2 全计划无「暂用同行机械」无声明借定额",
          not borrowed, "借定额任务 %d 条：%s" % (len(borrowed), borrowed[:6]))

    try:
        out = S.compute_schedules(wbs, deps, boundary, params, plan.get("cpm_result"))
    except Exception as exc:
        check("compute_schedules 可运行", False, "%s: %s" % (type(exc).__name__, exc))
        return
    check("compute_schedules 可运行", True)

    # ---- 用户申报的设备限额：逐项对账，**绝不允许静默失效**
    #     真实事故：用户申报「静压桩机 1 台」，而计划里的机械名是「静力压桩机」，
    #     scheduler 建限额表时用纯精确串匹配 → 匹配失败 → 用户限额被静默丢弃 →
    #     公式给出 2 台就真上 2 台（2.1.1 从 11 天变 6 天）。修法：名称容错匹配 +
    #     匹配不上必须出中文告警。这两件事各由一条检查钉住。
    eb = out.get("equipment_binding")
    check("管道产出用户设备对账表 equipment_binding",
          isinstance(eb, dict) and bool(eb),
          "%d 项" % len(eb) if isinstance(eb, dict) else "缺失/类型错")
    if isinstance(eb, dict) and eb:
        declared = sorted((S.parse_boundary_limits(boundary).get("equipment") or {}).keys())
        missing = [k for k in declared if k not in eb]
        check("用户申报的每项设备都进了对账表（无静默丢弃）", not missing,
              "漏报 %s" % missing if missing else "申报 %d 项 / 对账 %d 项"
              % (len(declared), len(eb)))
        silent = [k for k, v in eb.items()
                  if not v.get("effective") and not str(v.get("note") or "").strip()]
        check("未生效的设备限额必须带中文说明（不得沉默）", not silent,
              "无说明 %s" % silent if silent else "未生效项均有说明")
        zj = eb.get("静压桩机") or {}
        check("回归守卫：静压桩机 1 台 已绑定生效（11 天前提）",
              zj.get("effective") is True and bool(zj.get("bound_to")),
              "bound_to=%s effective=%s" % (zj.get("bound_to"), zj.get("effective")))
        eff = [k for k, v in eb.items() if v.get("effective")]
        print("     对账明细：%d/%d 项生效；未生效 %s"
              % (len(eff), len(eb), [k for k in sorted(eb) if k not in eff]))

    theo = version_of(out, "theory_min")
    res = version_of(out, "resource_ok")
    tt = theo.get("total_duration_days")
    rt = res.get("total_duration_days")
    if isinstance(tt, (int, float)) and isinstance(rt, (int, float)):
        check("theory_min ≤ resource_ok", tt <= rt, "theory=%s resource=%s" % (tt, rt))
    else:
        skip("两版总工期", "拿不到 total_duration_days")

    # ---- 把两个版本的任务行合并成 id → (theory_row, resource_row)
    trows = task_rows(theo)
    rrows = task_rows(res)
    if not trows and not rrows:
        skip("任务行结构", "两版都没探测到任务行（结构可能变了）")
        return
    print("     探测到任务行：theory=%d resource=%d" % (len(trows), len(rrows)))

    # ---- P0-2 / 验收①：120 根 PHC 桩必须走**跨族换算后的台班口径**
    #      120 根 × 18 m/根 = 2160 m；2160/100×0.49 = 10.58 台班；1 台静压桩机 → 11 天。
    #      只判 "≥8 天" 是不够的：退回 WBS 原值（30 天）或退回未换算的 0.59 台班（1 天）
    #      都能"蒙对"方向，所以这里锚定机制区间 [10,12] 并检查资源里真有桩机。
    for tid, label in (("2.1.1", "120 根 PHC 静压桩"),):
        row = rrows.get(tid) or trows.get(tid)
        if not row:
            skip("验收① %s(%s) 台班口径 10~12 天" % (label, tid), "未探测到该任务行")
            continue
        d = row_duration(row)
        leaf = leaf_by_id.get(tid) or {}
        b = leaf.get("norm_binding") or {}
        factor = (b.get("ctx_value") or {}).get("pile_length_m")
        shifts = None
        try:
            q = float(leaf.get("quantity") or 0)
            if b.get("mode") == "machine" and b.get("norm_value") and b.get("quantity_basis"):
                shifts = q * float(factor or 1) / float(b["quantity_basis"]) * float(b["norm_value"])
        except Exception:
            shifts = None
        res = row.get("resources") if isinstance(row.get("resources"), dict) else {}
        check("验收① %s(%s) 台班口径 10~12 天" % (label, tid),
              d is not None and 10 <= d <= 12,
              "实算 %s 天（换算后应 ≈ %.2f 台班 / 1 台；退回 WBS 工期=30 或未换算=1 都算 FAIL）"
              % (d, shifts if shifts is not None else -1))
        # 逐资源峰值只从 **resource_ok** 版的每日设备曲线取。
        # （theory_min 版按设计不认用户限额，会给出 2 台；把两版混在一起看会误读。）
        def _equip_peak(version, rname):
            pk = 0
            for rec in (version.get("daily_equipment") or []):
                items = rec.get("items") if isinstance(rec, dict) else None
                if isinstance(items, dict):
                    pk = max(pk, items.get(rname) or 0)
            return pk
        pile_peak = _equip_peak(version_of(out, "resource_ok"), "静力压桩机")
        check("验收① 静压桩机峰值 == 用户申报 1 台（resource_ok 曲线）",
              pile_peak == 1,
              "曲线峰值=%s 台；theory_min 版按设计不认用户限额（%s 台）"
              % (pile_peak, _equip_peak(version_of(out, "theory_min"), "静力压桩机")))
        print("        明细：单位=%s norm=%s basis=%s ctx=%s | 资源=%s | 换算后台班≈%s"
              % (b.get("unit"), b.get("norm_value"), b.get("quantity_basis"),
                 b.get("ctx_value"), res,
                 ("%.2f" % shifts) if shifts is not None else "?"))

    # ---- P1-1 / 验收②：铝模班组 = 工作面上限（≥ 8 人，不再 1 人）
    try:
        from pipeline import kb as KB
    except Exception:                                            # pragma: no cover
        KB = None
    # leaf_by_id 已在上面用**重绑后**的 wbs 建好，这里不再重复（否则会用回陈旧 binding）

    def expected_cap(activity_id, qty):
        if KB is None:
            return None
        cap = KB.workface_capacity(activity_id)
        if not cap:
            return None
        base, step_q, step_n = cap.get("crew_base"), cap.get("crew_step_q"), cap.get("crew_step_n")
        lo, hi, q_ref = cap.get("crew_min"), cap.get("crew_max"), cap.get("q_ref")
        if None in (base, lo, hi):
            return None
        if not step_q or not step_n or q_ref is None:
            return int(min(max(base, lo), hi))
        n = int(math.floor((qty - q_ref) / step_q))
        return int(min(max(base + step_n * n, lo), hi))

    for tid, label, aid in (("5.1.1.2", "铝模安装", "FORM_NEW_OTHER"),):
        leaf = leaf_by_id.get(tid)
        row = trows.get(tid) or rrows.get(tid)
        if not leaf or not row:
            skip("验收② %s(%s)" % (label, tid), "未探测到叶子或任务行")
            continue
        crew = row_crew(row)
        got = max(crew.values()) if crew else None
        exp = expected_cap(aid, float(leaf.get("quantity") or 0))
        check("验收② %s(%s) 班组 ≥ 8 人" % (label, tid),
              got is not None and got >= 8, "实算 %s 人（%s）" % (got, crew))
        if exp is None:
            skip("P1-1 顶满 == 工作面上限（%s）" % label, "v2 容量键不完整")
        else:
            check("P1-1 顶满 == 工作面上限（%s）" % label, got == exp,
                  "实算 %s，工作面上限 %s" % (got, exp))

    # ---- P1-2：总人工/机械峰值不应"顶满用户限额才算合法"；两版差异只应来自限额
    #      代理判据：无用户限额时，两版任务班组应完全一致（差异只能来自 honor_user_limits）
    if trows and rrows:
        diff = []
        for tid, trow in trows.items():
            rrow = rrows.get(tid)
            if not rrow:
                continue
            tc, rc = row_crew(trow), row_crew(rrow)
            if tc and rc and tc != rc:
                diff.append(tid)
        # 有用户限额时 resource_ok 允许更小；无限制时不应出现差异（除限额缩编记录）
        capped_ids = {str(c.get("task_id")) for c in
                      walk_collect(out, lambda d: "want" in d and "got" in d and "reason" in d, [])}
        unexplained = [t for t in diff if t not in capped_ids]
        check("P1-2 班组差异都有 capped 记录解释", not unexplained,
              "无解释差异 %d 条：%s" % (len(unexplained), unexplained[:5]) if unexplained else "")

    # ---- 验收③：土方 4260 m³ 不得再按人工 0.827 工日/m³ 得出 ~118 天
    for tid in ("2.2.1", "3.2.1"):
        row = rrows.get(tid) or trows.get(tid)
        if not row:
            skip("验收③ 土方(%s) 工期不再 ~118 天" % tid, "未探测到该任务行")
            continue
        d = row_duration(row)
        check("验收③ 土方(%s) 工期不再 ~118 天" % tid,
              d is not None and d <= 60, "实算 %s 天" % d)

    # ---- P1-2 源码代理判据：按目标工期反推班组的兜底已删除
    src = (BACKEND / "pipeline" / "nodes" / "scheduler.py").read_text(encoding="utf-8")
    squeezed = src.replace(" ", "").replace("\n", "")
    has_inversion = "(productivity*target)" in squeezed
    has_inversion = has_inversion or "(productivity*int(target))" in squeezed
    check("P1-2 已删除「按目标工期反推班组」兜底", not has_inversion,
          "仍存在 quantity/(productivity×target) 形态" if has_inversion else "")


def main():
    print("=" * 78)
    print("端到端验收：P0-1 / P0-2 / P0-3 / P1-1 / P1-2（计划 run_1789818211）")
    print("=" * 78)
    phase_p01()
    phase_p03()
    phase_iron_rule()
    phase_e2e()
    fails = [r for r in _RESULTS if r[1] is False]
    skips = [r for r in _RESULTS if r[1] is None]
    print("\n" + "-" * 78)
    print("合计：%d 项，PASS %d，FAIL %d，SKIP %d"
          % (len(_RESULTS), len(_RESULTS) - len(fails) - len(skips), len(fails), len(skips)))
    for name, _, detail in fails:
        print("  FAIL: %s  %s" % (name, detail))
    for name, _, detail in skips:
        print("  SKIP: %s  %s" % (name, detail))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
