"""节点5：方案汇总 + 交付（纯确定性，Dify 3.2 进度方案的 Python 化替代）— T-13

从 wbs / dependencies / cpm_result / resource_demand / extracted_params 确定性汇总：
overview / key_milestones / critical_path_tasks / all_tasks_schedule /
resource_plan / risks → 组装 plan_json（§5.4 契约），并在 deliver 节点校验落盘。

原则：数值全部取自确定性算法结果，不让 LLM 参与，保证契约不漂移。
"""

import datetime
import json
import os
import re
import sys

from .. import branding, config, quantity as quantity_mod
from .. import schemas, usage as usage_mod
from .. import audit_scope
from ..base import BaseNode
from ..events import EV_PLAN_FINAL
from .beat_configs import building_count, building_count_note

LABOR_NAMES = {
    "普工", "钢筋工", "模板工", "混凝土工", "瓦工", "抹灰工", "泥工", "油漆工",
    "保温工", "装修工", "绿化工", "防水工", "安装工", "管道工", "电工", "通风工",
    "架子工", "灌浆工", "装配式安装工", "桩机工", "铺装工", "水泥工", "测量工",
    # 「木工」「砌筑工」：用户申报的 labor.by_trade 里就有这两个工种（实测真实计划
    # `meta.boundary_conditions.labor.by_trade` = 钢筋工/木工/混凝土工/架子工/砌筑工/
    # 安装工/普工），但分类表里缺它们 —— 一旦任务资源里出现同名工种，就会被当成**机械**
    # 写进 equipment_peak（"木工 20 台"）。同类历史缺陷，一并补全。
    "木工", "砌筑工",
}

# 机械配员：随机械台数配置的操作人员，属于"人"，但不属于"工种人工需求"。
# 它们出现在 tasks[*].assigned_resources 里（如 混凝土浇筑 挂 混凝土输送泵车17/泵工17/辅助17），
# 历史实现把它们当成设备写进 equip_peak，导致看板「设备资源荷载」里出现人。
# 因此**不并入 LABOR_NAMES**：两者的语义不同 —— LABOR_NAMES 是「工种人工」账本
# （total_manpower_days / labor_demand 的口径），MACHINE_CREW 是「随机械配置的人」
# （只进人数峰值 + 单列 machine_crew_peak 留档）。
# ⚠️ delivery.py 的 LABOR / MACHINE_CREW 与本表同源（`_is_labor` = LABOR ∪ MACHINE_CREW），
# 改这里必须同步改那份，否则看板与 resource_plan 会再次分叉。
# 「信号工」：塔吊/施工电梯的配员（`Equipment_Crew_Mapping` 写的是"司机1名+信号工1名"）。
# 缺了它，信号工会被 `_is_labor` 判成**机械**，在机械清单里冒出一个叫"信号工"的"设备"。
# 「振捣工」：混凝土振捣器的配员（`Equipment_Crew_Mapping` 写的是"振捣工1人"）。
# 实测计划 `plan_sample3_after_fix` 的 `resource_plan.equipment_peak` 里就有
# `"振捣工": 1` —— 同一类缺陷，按同一条路补。全表解析 26 行配员原文后，
# 它是唯一漏网的角色。
MACHINE_CREW = {"泵工", "辅助", "操作工", "司机", "信号工", "振捣工"}


def _leaf_tasks(wbs):
    return [sub for ph in wbs.get("phases", [])
            for wp in ph.get("work_packages", [])
            for sub in wp.get("sub_packages", [])]


# ============================================================
# G5 · 产物单位清零：CJK 兼容方块平米符号（U+33A1）
# ------------------------------------------------------------
# 背景：U+33A1 与 `m²`（`m` + U+00B2）**不是同一个字符**。`kb_units.normalize_unit`
# 已做**输入侧**归一（用户文档里写它照样认），所以它不影响任何数字，只影响显示一致性
# 与验收判据（重构方案 §6 验收 #5「无 U+33A1 残留」）。
#
# 本段是**输出侧收口**（生成期断言），两条硬要求：
#   ① 源码里不出现该字符本身（一律用 `chr(0x33A1)` 构造），免得它又被复制进产物串；
#   ② 命中即**报出具体路径 + 原文**并抛错 —— **绝不静默 replace**：静默 replace 会把
#      上游缺陷（谁在拼这个单位串）藏起来，下一次还会冒出来。
#
# 归口说明（谁负责清零哪一处）：
#   · `plan_assembler.py` 的 `material_summary[*].unit` —— **【第 2 批 · 域 2 / 2.6】已删除**
#     （`material_summary` 这个"主要材料"汇总整体不再产出；改为在交付物上声明
#     「本计划不含材料计划。材料按"管够"处理…」）；
#   · `beat_configs.py` 的公式模板（238 处 `_qty_formula` 的主要来源）—— W3-A；
#   · `resource.py` 的 `new_unit = ... else "U+33A1"` —— W2-C；
#   · `kb_units.py` 别名表 / `scope_inputs.py` / `extractor.py` 的**输入**正则 ——
#     **绝不清零**（必须继续认得出用户写的 `U+33A1`）。
# ============================================================
CJK_COMPAT_SQUARE_METRE = chr(0x33A1)          # 即 CJK 兼容方块平米符号
CJK_COMPAT_SQUARE_METRE_NAME = "U+33A1（CJK 兼容方块平米符号）"


class CjkCompatSquareMetreError(ValueError):
    """产物里残留 U+33A1 方块平米符号（G5 / 验收 §6#5）。"""


def find_cjk_compat_square_metre(obj, path="产物"):
    """递归找 `obj` 里所有 U+33A1 出现处 → ``[(路径, 原文), ...]``（只读，不改入参）。

    路径用点号 + 下标定位到具体字段（键名本身残留也会被报出来），
    原文原样带出（便于 grep 回上游拼串处）。
    """
    hits = []
    if obj is None or isinstance(obj, bool):
        return hits
    if isinstance(obj, str):
        if CJK_COMPAT_SQUARE_METRE in obj:
            hits.append((path, obj))
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(find_cjk_compat_square_metre(k, "%s[%r]（键名）" % (path, k)))
            hits.extend(find_cjk_compat_square_metre(v, "%s[%r]" % (path, k)))
        return hits
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(find_cjk_compat_square_metre(v, "%s[%d]" % (path, i)))
        return hits
    return hits


def assert_no_cjk_compat_square_metre(obj, where="plan_json"):
    """产物里不得残留 U+33A1 —— 命中即抛 `CjkCompatSquareMetreError`（报路径 + 原文）。

    返回 ``[]``（无残留）或抛错；**不做任何替换**。
    """
    hits = find_cjk_compat_square_metre(obj, path=str(where))
    if not hits:
        return []
    lines = ["%s 里残留 %d 处 %s（应写 `m²`，G5 / 验收 §6#5）："
             % (where, len(hits), CJK_COMPAT_SQUARE_METRE_NAME)]
    for p, t in hits[:20]:
        lines.append("  · %s → %s" % (p, _hit_excerpt(t)))
    if len(hits) > 20:
        lines.append("  · …（共 %d 处，其余见上游单位串拼接处）" % len(hits))
    lines.append("修法：写 `m²`（U+00B2），不要写 CJK 兼容方块字；"
                 "输入侧宽容由 `kb_units.normalize_unit` 负责，产物侧一律不出现。")
    raise CjkCompatSquareMetreError("\n".join(lines))


def _hit_excerpt(text, width=40):
    """命中处**上下文窗口**（长串整段打出来等于没定位，短串原样返回）。"""
    s = str(text)
    if len(s) <= 2 * width:
        return s
    i = s.find(CJK_COMPAT_SQUARE_METRE)
    if i < 0:
        return s[:2 * width] + "…"
    lo, hi = max(0, i - width), min(len(s), i + 1 + width)
    return ("…" if lo else "") + s[lo:hi] + ("…" if hi < len(s) else "")


def normalize_cjk_compat_square_metre(obj, path="产物"):
    """G5 **归一收口**：把 U+33A1「U+33A1」就地归一为 `m²`，**逐处留痕**（不是静默 replace）。

    为什么需要归一（而不只有断言）：`U+33A1`(U+33A1) 与 `m²`（`m` + U+00B2）是同一个量纲的
    两种写法，§5「单位贯通」定的规范写法就是 `m²`（唯一真源 `kb_units.normalize_unit`）。
    上游仍有几处未收口的产出点（实测：WBS 叶子的 `unit`、KB 定额单位串 `工日/U+33A1`），
    产物侧在这里统一收口，并**把每一处的路径与归一前原文写进 stderr** —— 这样既不中断
    流水线（"保持仓库可运行"），也不把上游缺陷藏起来（有据可查）。

    只改这一个字符，**不改任何数字、不动别的字段**。
    返回 ``[(路径, 归一前原文), ...]``；`dict` / `list` 就地修改。
    """
    fixed = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kid = "%s[%r]" % (path, k)
            if isinstance(v, str) and CJK_COMPAT_SQUARE_METRE in v:
                obj[k] = v.replace(CJK_COMPAT_SQUARE_METRE, "m²")
                fixed.append((kid, v))
            else:
                fixed.extend(normalize_cjk_compat_square_metre(v, kid))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            kid = "%s[%d]" % (path, i)
            if isinstance(v, str) and CJK_COMPAT_SQUARE_METRE in v:
                obj[i] = v.replace(CJK_COMPAT_SQUARE_METRE, "m²")
                fixed.append((kid, v))
            else:
                fixed.extend(normalize_cjk_compat_square_metre(v, kid))
    return fixed


def normalize_cjk_compat_square_metre_in_text(text):
    """字符串形态的同一归口（HTML / XML 这类成品串）：``(新串, [(路径, 原文), ...])``。"""
    s = text if isinstance(text, str) else str(text or "")
    if CJK_COMPAT_SQUARE_METRE not in s:
        return s, []
    fixed = []
    start = 0
    while True:
        i = s.find(CJK_COMPAT_SQUARE_METRE, start)
        if i < 0:
            break
        fixed.append(("文本偏移 %d" % i, s[max(0, i - 40):i + 41]))
        start = i + 1
    return s.replace(CJK_COMPAT_SQUARE_METRE, "m²"), fixed


def _schedule_map(cpm):
    return {s["task_id"]: s for s in (cpm.get("schedule") or [])}


def _resources_map(resource_demand):
    return {t["task_id"]: (t.get("resources") or {}) for t in (resource_demand.get("tasks") or [])}


def _labor_crew_map(resource_demand):
    """task_id → 实际投入的**人工**班组（不含机械本身）。没有该键的行不进表。"""
    out = {}
    for t in ((resource_demand or {}).get("tasks") or []):
        crew = t.get("_crew")
        if isinstance(crew, dict) and crew:
            out[str(t.get("task_id"))] = crew
    return out


def _as_crew_int(value):
    """班组人数：整数取整，非整数保留原值，非正数丢弃。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return int(f) if abs(f - round(f)) < 1e-6 else f


def _add_days(start: datetime.date, days: int) -> str:
    return (start + datetime.timedelta(days=days)).isoformat()


def _closed_last_day(row) -> int:
    """排程行 → **闭区间末日**（天下标）：`max(es, ef - 1)`。

    为什么必须减一：`scheduler.py` 的 `ef` 是**半开上界**（`ef = es + 工期`，7 天任务占
    `es..es+6`）。交付物给人看的是**日期**，日期就是闭区间（首尾两天都算）——
    `finish_date = 开工 + (ef - 1)`。旧实现用 `开工 + ef`，于是每条任务的日期跨度
    都比排程跨度多一天（实测 5.1.1.1 组织层 7 天、日期却跨 8 天；全项目 689 天 vs
    总工期 688 天）。零工期（`ef <= es`）按一天算，与 `delivery._compute_view` 的
    `d1 = max(d0, …)` 兜底一致。
    """
    try:
        es = int(row.get("es") or 0)
    except (TypeError, ValueError):
        es = 0
    try:
        ef = int(row.get("ef") or 0)
    except (TypeError, ValueError):
        ef = es
    return max(es, ef - 1)


def _as_number(value):
    """数值化：拿不到 / 非数 / NaN → None（**不编数**，也不把 0 当成"有值"）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def site_equipment_contrib(task):
    """任务级的**场地级设备**投入（含其配员）→ ``{资源名: 逐日台/人数}``。

    场地级设备（塔吊 / 施工电梯）是**全场地常驻**的：一个工地各 1 台，服务所有楼层的
    所有任务。所以同一天有 5 条任务需要塔吊，也**只能算 1 台**（不是 5 台）——
    曲线口径必须是逐日 **max**；而泵车/挖掘机这类**任务级机械**仍按日 **+=**。

    真源是 `resource._inject_site_equipment()` 写进任务的 `_site_equipment`：
    每项给出 `quantity`（台数）与 `crew`（每台配几个人），逐日贡献 = 台数、配员 = 台数×人数。
    没有该键（旧计划 / 没有场地级设备）→ 返回 {}，聚合行为与改动前**逐字一致**。
    """
    out = {}
    for item in ((task or {}).get("_site_equipment") or []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        try:
            q = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            q = 0.0
        if not name or q <= 0:
            continue
        out[name] = out.get(name, 0.0) + q
        for role, cnt in (item.get("crew") or {}).items():
            try:
                c = float(cnt)
            except (TypeError, ValueError):
                continue
            if c > 0:
                out[str(role)] = out.get(str(role), 0.0) + q * c
    return out


def _daily_peak(rd, sched, predicate):
    """资源量的**按天叠加峰值** → (``{资源名: 单日最大在场量}``, 未落排程行的任务数)。

    口径与交付物的逐日视图**逐字一致**（`delivery.py:_compute_view()`）：
      · **任务级**资源：任务按**闭区间** `es..ef-1` 占用（`ef` 半开上界，含首尾两天等价于
        `ef-es` 天），`per_day` 取整后逐日 **累加**，峰值取当日总和的最大值；
      · **场地级**设备（塔吊/施工电梯，见 `site_equipment_contrib`）：逐日 **max**，
        不跨任务叠加 —— 一个工地 1 台塔吊服务所有楼层的所有任务，按任务累加就是虚高。

    同名资源可能同时来自两类（如"司机"既是挖掘机的任务级配员、又是塔吊的场地级配员）：
    该名字的逐日量 = 任务级之和 + 场地级 max，两部分分开记账再相加，互不污染。

    ⚠️ 为什么不能再沿用 `max(单任务 per_day)`（第 40 轮实测缺陷）：那种算法**永远算不出
    同机多任务并行** —— 每种机械恒为 1 台；而交付物逐日铺开算出「履带式单斗液压挖掘机」
    单日峰值 **2 台**。同一份计划两个数打架，"设备资源荷载"就是错的。
    """
    flat_daily = {}          # 任务级：逐任务累加
    site_daily = {}          # 场地级：逐任务取 max
    orphans = 0
    for idx, task in enumerate((rd or {}).get("tasks") or []):
        if not isinstance(task, dict):
            continue
        tid = str(task.get("task_id") or "")
        row = sched.get(tid) if isinstance(sched, dict) else None
        d0 = d1 = None
        if isinstance(row, dict):
            try:
                d0 = int(row.get("es", 0))
                d1 = _closed_last_day(row)          # ef 是半开上界：末日在 ef-1
            except (TypeError, ValueError):
                d0 = d1 = None
        if d0 is None:
            # 没有排程行（未排上 / 旧产物）：**不许静默丢掉** —— 按"各自单独一天"计入，
            # 它自己的 per_day 仍是一个候选峰值，但不会与别的任务叠在一起虚高。
            d0 = d1 = -1 - idx
            orphans += 1
        if d1 < d0:
            d1 = d0
        site_contrib = site_equipment_contrib(task)
        for name, q in (task.get("resources") or {}).items():
            if not predicate(name):
                continue
            per_day = (q or {}).get("per_day") if isinstance(q, dict) else q
            try:
                per_day = int(per_day)          # 与 delivery 一致：先取整再累加
            except (TypeError, ValueError):
                continue
            # 场地级那一份要从任务级计数里扣掉，否则同一条任务里 1 台塔吊会被算两次
            # （一次按任务级累加、一次按场地级 max）。
            site_part = 0
            if name in site_contrib:
                try:
                    site_part = int(site_contrib[name])
                except (TypeError, ValueError):
                    site_part = 0
            flat_part = max(0, per_day - site_part)
            if flat_part > 0:
                bucket = flat_daily.setdefault(name, {})
                for d in range(d0, d1 + 1):
                    bucket[d] = bucket.get(d, 0) + flat_part
            if site_part > 0:
                bucket = site_daily.setdefault(name, {})
                for d in range(d0, d1 + 1):
                    if site_part > bucket.get(d, 0):
                        bucket[d] = site_part
    daily = flat_daily
    for name, bucket in site_daily.items():
        tgt = daily.setdefault(name, {})
        for d, v in bucket.items():
            tgt[d] = tgt.get(d, 0) + v
    return {name: max(b.values()) for name, b in daily.items() if b}, orphans


def _equipment_binding_items(raw):
    """用户申报设备 → 计划资源的逐项对账 → 固定形状的**列表**。

    形状：``[{name, quantity, bound_to, effective, note}]``。
    scheduler 侧（`scheduler.equipment_binding_report`）是
    ``{用户申报名: {declared, bound_to, effective, note}}`` 的字典，这里归一成
    交付物/看板好读的列表（`quantity` = 申报台数）。
    拿不到就返回**空列表** —— 绝不编一条"已绑定"出来。
    """
    def _one(name, rec):
        rec = rec if isinstance(rec, dict) else {}
        return {
            "name": str(name or rec.get("name") or ""),
            "quantity": rec.get("quantity", rec.get("declared")),
            "bound_to": rec.get("bound_to"),
            "effective": bool(rec.get("effective")),
            "note": str(rec.get("note") or ""),
        }

    if isinstance(raw, dict):
        return [_one(k, v) for k, v in sorted(raw.items(), key=lambda kv: str(kv[0]))]
    if isinstance(raw, list):
        return [_one((it or {}).get("name") if isinstance(it, dict) else it, it)
                for it in raw if isinstance(it, dict)]
    return []


def _organization_gaps_items(raw):
    """WS6 组织层"按节拍做不到"的工序清单 → 固定形状的**列表**。

    形状：``[{task_id, task_name, trade, person_days, cadence_days, n_needed, n_max,
    c_max, t_min_days, levers}]``（scheduler 侧就是这个形状，这里只拷贝 + 过滤掉
    非字典项，**不改任何数值、不做任何推断**）。
    拿不到 / 不是列表 → **空列表**（不编造缺口）。
    注意"空列表"与"键缺失"对交付物是两件不同的事：`delivery` 对缺键会如实写
    "计划 meta 未带 organization_gaps 字段"（`delivery.py:2975`）。本函数只负责把 ctx
    里的东西原样搬出来，不替它下结论。
    """
    if not isinstance(raw, list):
        return []
    return [dict(it) for it in raw if isinstance(it, dict)]


def _numbering_projection(beat_subtrees):
    """第 5 批（域 4）编号留痕的**投影** → `{阶段名: {...}}`。

    为什么需要这一行搬运（与 `organization_gaps` / `quantity_coverage` **完全同一类事故**）：
    `build_meta` 是**显式白名单字典**，没列进去的 ctx 键永远进不了 meta。
    `beat_node` 把"id 是按什么口径编的、LLM 给的工序清单是什么、哪些 L4 没解析出来、
    有没有叶子违反候选集硬约束"写在 `ctx["beat_subtrees"][阶段]["numbering"]`，
    而整个 `beat_subtrees` 从来没进过 meta ⇒ **用户在交付物里看不到编号溯源**。

    这里只搬**投影**（不搬 `cfg` 全文）：计划 JSON 已经 6 MB 量级，`cfg` 里有整份
    cycle/分区配置，搬进去会让体积翻倍；而"口径 + 违规 + LLM 顺序"这几项才是要看的。
    形状：``{阶段名: {scheme, l3_order, candidates_bad, l4_order, l4_unresolved}}``。
    `beat_subtrees` 拿不到 / 形状不认识 → **空 dict**（不编造）。
    """
    if not isinstance(beat_subtrees, dict):
        return {}
    out = {}
    for name, item in beat_subtrees.items():
        if not isinstance(item, dict):
            continue
        num = item.get("numbering")
        if not isinstance(num, dict):
            continue
        out[str(name)] = {
            "scheme": num.get("scheme"),
            "l3_order": list(num.get("l3_order") or []),
            "candidates_bad": [dict(b) for b in (num.get("candidates_bad") or [])
                               if isinstance(b, dict)],
            "l4_order": num.get("l4_order"),
            "l4_unresolved": [dict(u) for u in (num.get("l4_unresolved") or [])
                              if isinstance(u, dict)],
        }
    return out


def _ratio_notes_projection(beat_subtrees):
    """节拍展开时**占比表降级/量0出局**的留痕 → `[{phase, code, step, activity_id, message}]`。

    为什么需要这一行搬运（与 `organization_gaps` / `quantity_coverage` / `numbering`
    **完全同一类**"算了但没送到"）：`build_meta` 是显式白名单，没列进去的 ctx 键进不了 meta。
    `layer_engine.expand_node` 把占比表降级写在 `phase_dict["ratio_degradations"]`，
    `beat_node` 搬到 `beat_subtrees[阶段]["degradations"]`，但**从来没进过 meta** ⇒
    交付物里看不到"用户给的分项总量到底用上了没有"。

    典型必须被看见的一条（第 5 批实测）：`Component_Ratio` 只覆盖
    concrete/rebar/formwork，而 `GROUP_TOTAL_PARAMS` 里还有 masonry/earthwork ——
    用户写「砌体：约3000立方米」时，`total_masonry` **无法按占比拆到 L4**，
    工序回退系数推算。这条必须让用户看见，**不能静默**。

    拿不到 → **空列表**（不编造）。按阶段名排序，保证重跑逐位一致。
    """
    if not isinstance(beat_subtrees, dict):
        return []
    out = []
    for name in sorted(beat_subtrees):
        item = beat_subtrees.get(name)
        if not isinstance(item, dict):
            continue
        for d in item.get("degradations") or []:
            if not isinstance(d, dict):
                continue
            out.append({
                "phase": str(d.get("phase") or name),
                "code": d.get("code"),
                "step": d.get("step"),
                "activity_id": d.get("activity_id"),
                "message": d.get("message"),
            })
    return out


def _bind_actual_resources(items, rd):
    """把"用户申报设备 → 是否生效"的对账结果与**资源层实际投入**的机械对齐。

    背景（真实缺陷，第 40 轮记录 + 本轮的塔吊/施工电梯）：`scheduler.equipment_binding_report()`
    在 **resource 节点之前**跑，只认排程自己那条机械路径。塔吊/施工电梯是**场地级常驻设备**，
    由资源层（`resource._inject_site_equipment`）单独注入、**故意不经过定额路径**（库内没有
    它们的台班定额行）—— 于是排程侧永远说"未匹配到计划中的任何机械资源"。资源层已经真投入了
    还印"⚠ 未生效"，交付物就自相矛盾。

    这里**只补能证实的绑定**：资源名在计划里真的作为场地级设备登记过（`_site_level_equipment`），
    才把 effective 改成 True 并写明"由资源层注入、台数=申报台数、不参与排程限额约束"；
    证不实的**维持原样**（判 unsupported 的照旧判 unsupported），绝不替它编一条"已绑定"。
    """
    site = ((rd or {}).get("_site_level_equipment") or {}).get("machines") or {}
    if not site or not items:
        return items
    out = []
    for it in items:
        name = str(it.get("name") or "")
        hit = None
        for mname in site:
            if mname == name or (name and (name in mname or mname in name)):
                hit = mname
                break
        if hit and not it.get("effective"):
            it = dict(it)
            it["effective"] = True
            it["bound_to"] = hit
            it["note"] = ("已由资源层按**场地级常驻设备**投入计划（%s，逐日取 max，"
                          "台数取用户申报值）；该设备不参与排程期的机械限额约束"
                          "（排程早于资源层，且场地级设备不按任务叠加）。原对账说明：%s"
                          % (hit, it.get("note") or ""))
        out.append(it)
    return out


def build_parts(ctx) -> dict:
    """确定性汇总所有中间结果 → plan 各部分。"""
    wbs = ctx.get("wbs") or {}
    cpm = dict(ctx.get("cpm_result") or {})
    rd = ctx.get("resource_demand") or {}
    params = ctx.get("extracted_params") or {}
    boundary = ctx.get("boundary_conditions") or {}

    # 排程结果优先：CPM 是"无资源约束的理想工期"，而 scheduler 给出的是
    # "满足工作面容量（+ 用户资源限额）的真实工期"。交付物应当用后者。
    # 为了不破坏契约（overview.total_duration_days == cpm_result.total_duration_days），
    # 这里把排程结果**并回 cpm_result**，下游一律读同一份。
    sched_ver = ctx.get("schedule") or {}
    if sched_ver.get("total_duration_days") and sched_ver.get("schedule"):
        cpm["cpm_total_duration_days"] = cpm.get("total_duration_days")   # 留个对照值
        cpm["total_duration_days"] = sched_ver["total_duration_days"]
        cpm["schedule"] = [
            {"task_id": t.get("task_id"),
             "es": t.get("es", 0), "ef": t.get("ef", 0),
             "ls": t.get("ls", t.get("es", 0)), "lf": t.get("lf", t.get("ef", 0))}
            for t in (sched_ver.get("schedule") or [])
        ]
        if sched_ver.get("critical_path"):
            cpm["critical_path"] = list(sched_ver["critical_path"])
        ctx["cpm_result"] = cpm
    elif cpm:
        ctx["cpm_result"] = cpm

    # ---- 口径必须随数字落盘（P0-B 的 644 vs 608）----
    # `cpm_total_duration_days` 与排程版的 `total_duration_days` **不同源**，因此它
    # **可以比排程版更长**（真计划实测 644 > 608）。原因不是算错：`cpm.py` 用的是 WBS
    # 叶子上的 `duration_days`（模型估的目标天数），而 scheduler 的逐任务工期是
    # "工日 ÷ 班组 × 工作面"反算出来的 —— 真计划 310 行里 144 行两者不同
    # （`2.1.1` WBS 15 天 / 排程 6 天）。"无资源约束的理想值 ≤ 资源约束版"这条定律
    # 只在两边用同一套逐任务工期时成立，**在这里不成立**。
    # 所以口径要跟数字一起存：下游（交付物、复检脚本、模型页面）才有依据决定该不该比大小。
    if cpm.get("cpm_total_duration_days") is not None:
        cpm.setdefault("cpm_duration_basis", "wbs_target_days+dependencies")
        cpm.setdefault("cpm_duration_note", (
            "按 WBS 目标天数 + 依赖关系正推的无资源约束工期；与 "
            "cpm_result.total_duration_days（排程版，逐任务工期由资源/工作面反算）"
            "不同源，因此可以更长，不能当排程版的理想下界。"))
        ctx["cpm_result"] = cpm

    total_days = cpm.get("total_duration_days", 0)
    critical_path = cpm.get("critical_path", [])
    sched = _schedule_map(cpm)
    # WS6 施工组织层：排程行上的 `_organization` 只能从 `sched_ver` 单独取一次。
    # 原因：上面并回 `cpm_result` 时**只搬了 task_id/es/ef/ls/lf**（保持
    # `cpm_result.schedule` 的既有形状与键集，不为了新字段去动它），所以
    # `_schedule_map(cpm)` 出来的行里没有 `_organization`。而交付物
    # （`delivery._org_map`）按 resource_demand.tasks → all_tasks_schedule →
    # critical_path_tasks → schedule 的顺序找这个键 —— 不在这里透传，
    # 看板/Word 的施工组织层段落就会整段显示"来源未记录"（组织层白算）。
    org_map = {t.get("task_id"): t["_organization"]
               for t in (sched_ver.get("schedule") or [])
               if isinstance(t, dict) and isinstance(t.get("_organization"), dict)}
    # ---- 容量口径两态（域 1.6 裁定 B）：**逐行可追溯**，同一处透传 ----
    # 缺陷（第 7 批实测定位）：排程器把两态写在**它自己的排程行**上
    # （`scheduler._run_one_version` → `rows[tid]["capacity_source"]` /
    # `["capacity_basis"]`，值 ∈ {`mwi`, `reported_missing`}），而下面组装
    # `all_tasks_schedule` 时用的是**显式白名单**（只搬 6 个键 + `_organization`）
    # ⇒ 这两个键被丢掉。交付侧 `delivery._capacity_source_rows()` 读的正是
    # `all_tasks_schedule[*].capacity_source`，于是实测**读到 0 条**：
    # `capacity_caliber_model(plan)["present"] == False`，那段"下列 N 条任务的工期
    # 不随工程量变化（缺容量数据）"的提示**永远不会出现**，用户看到的是"什么都没发生"。
    # 这正是本仓反复出现的"**算了但没送到**"：每加一个产物键，必须问
    # 「谁把它搬到交付物读的那一层？」—— 答案就是这里。
    # `sched_ver["schedule"]` 是**真源**（`_public_version` 保留了它，见
    # `scheduler._public_version` 的 `"schedule": version.get("schedule", [])`）。
    cap_map = {t.get("task_id"): t
               for t in (sched_ver.get("schedule") or [])
               if isinstance(t, dict) and t.get("capacity_source")}
    resources = _resources_map(rd)
    labor_crews = _labor_crew_map(rd)
    leaves = _leaf_tasks(wbs)

    # 计划起止
    try:
        start = datetime.date.fromisoformat(params.get("planned_start_date")
                                            or datetime.date.today().isoformat())
    except ValueError:
        start = datetime.date.today()
    # 闭区间末日：`planned_end_date` = 开工 + (总工期 - 1) —— 首尾两天都算，
    # 否则「2026-06-01 → 2028-04-19 跨 689 天」与「总工期 688 天」自相矛盾。
    try:
        _total_int = int(total_days or 0)
    except (TypeError, ValueError):
        _total_int = 0
    end = start + datetime.timedelta(days=max(0, _total_int - 1))

    # overview
    # ⚠️ 给模型看的 parts 里**不出现** `critical_path_length` 这个歧义键名：它装的是
    # 关键路径任务的**条数（个）**，而"长度"这个词与同一个 overview 里的
    # `total_duration_days`（天数）摆在一起，模型实测读成了天数 —— 真计划
    # `plans/plan_run_1789895021.json` 的交付物正文写着「当前计划总工期为604天，
    # 关键路径长度为81天」，81 其实是条数。这里改名并附一句 note（详见
    # `CRITICAL_PATH_TERM` 上方那段说明）；落进 plan_json 时由 `_overview_contract`
    # 改回契约键名（`schemas.Overview.critical_path_length` 是硬契约，不能少）。
    overview = {
        "project_name": params.get("project_name") or "未命名施工项目",
        "total_duration_days": total_days,          # = cpm_result.total_duration_days
        "planned_start_date": start.isoformat(),
        "planned_end_date": end.isoformat(),
        "critical_path_task_count": len(critical_path),   # 条数（个），不是天数
        "critical_path_task_count_note": (
            CRITICAL_PATH_COUNT_NOTE + "；关键路径的**天数**见 critical_path_caliber"),
    }

    # all_tasks_schedule / critical_path_tasks
    all_tasks_schedule = []
    critical_path_tasks = []
    # ---- 排程覆盖对账：**WBS 叶子集合 − 排程行集合**（差集才是真源）----
    # 老实现是 `s = sched.get(tid); if not s: continue` —— 一旦某个 WBS 叶子没有排程行，
    # 它就从 `all_tasks_schedule` 里**无声消失**：交付物不显示、也不报缺，用户完全看不出来
    # （与"数据里没有就不许印"是同一类病的镜像面：**数据里该有的也不许静默丢**）。
    # 现在改成"不丢记录、但**不编日期**"：缺 es/ef 就排不出起止日期，绝不瞎写一个；
    # 差集如实落进 `ctx["unscheduled_tasks"]`（→ `meta["unscheduled_tasks"]`）并升为告警。
    unscheduled = []
    for leaf in leaves:
        tid = leaf["id"]
        s = sched.get(tid)
        if not isinstance(s, dict) or s.get("es") is None or s.get("ef") is None:
            if tid not in sched:
                _why = "排程结果里没有这条工序的行（排序/依赖求值没有产出它）"
            elif not isinstance(s, dict):
                _why = "排程行不是字典（数据形状不对）"
            else:
                _why = "排程行存在但缺少 es/ef，无法换算起止日期"
            unscheduled.append({
                "task_id": str(tid),
                "task_name": leaf.get("name") or str(tid),
                "wbs_target_days": leaf.get("duration_days"),
                "reason": _why,
                "effect": "该任务未进入 all_tasks_schedule —— 交付物里看不到它，"
                          "也不占用工期",
            })
            continue
        res = {k: v.get("per_day", 0) for k, v in (resources.get(tid) or {}).items()}
        # ⚠️ 两个「天数」必须**分开存**（用户审计 P0-B）：
        #   · `duration_days`   = **排程跨度**（含首尾，= `ef - es`）—— 与同一行的起止
        #     日期**同源**，`(finish_date - start_date).days + 1 == duration_days` 恒成立
        #     （`finish_date` 由 `_closed_last_day` 给出，见那里的推导）；
        #   · `wbs_target_days` = WBS 叶子上模型写的**目标天数**（原值，不折算、不合并）。
        # 旧实现把 WBS 目标天数塞进 `duration_days`，而日期来自排程器 —— 实测真计划
        # 310 行里 **144 行**「字段与自己的日期对不上」（`2.1.1` 字段 15 天 / 日期 6 天；
        # `3.2.1` 字段 25 天 / 日期 3 天），交付物同一行印出两个互相矛盾的工期。
        try:
            _span = int(s["ef"]) - int(s["es"])
        except (KeyError, TypeError, ValueError):
            _span = 0
        item = {
            "task_id": tid,
            "task_name": leaf.get("name", tid),
            "start_date": _add_days(start, s["es"]),
            # 闭区间首尾：末日 = 开工 + (ef - 1)，见 `_closed_last_day` 的说明
            "finish_date": _add_days(start, _closed_last_day(s)),
            "duration_days": max(1, _span),
            "wbs_target_days": leaf.get("duration_days"),
            "assigned_resources": res,
        }
        if tid in org_map:
            item["_organization"] = org_map[tid]     # WS6：组织层结果透传给交付物
        # ---- 容量口径两态（域 1.6 裁定 B）：交付侧 `delivery._capacity_source_rows()`
        # 读的就是 `all_tasks_schedule[*]` 的这两个键。不搬 ⇒ 实测 0 条 ⇒ 缺容量提示永不出现。
        # 只写**排程器真的给了值**的行（没给就不写键，绝不编一个 `mwi`/`reported_missing`）。
        _cap = cap_map.get(tid)
        if _cap:
            item["capacity_source"] = _cap.get("capacity_source")
            if _cap.get("capacity_basis"):
                item["capacity_basis"] = _cap.get("capacity_basis")
        all_tasks_schedule.append(item)
        if tid in critical_path:
            critical_path_tasks.append(item)

    # ---- 班组只留一个真源：把实际投入的人工班组回写进 leaf.norm_binding.crew ----
    # 背景：crew_bind 只填**机械配员**，人工班组是**故意留空**的（见 test_norm_bind
    # 里 `b["crew"] == {}` 的断言，注释写着"配员由别的节点填，这里必须留空"），
    # 等实际资源算出来后再回填。此前初次排程这条路漏了这一步，于是计划里长期存在
    # 两个互相矛盾的班组：norm_binding.crew（/sources 读它）与 assigned_resources
    # （交付物读它）。潭村实测 415 条里有 377 条（90.8%）对不上。
    # 这里独立成环，不与"有没有排程行"耦合 —— 班组来自 resource_demand。
    crew_written = 0
    for leaf in leaves:
        crew = labor_crews.get(str(leaf.get("id")))
        if not crew:
            continue
        fixed = {}
        for role, cnt in crew.items():
            n = _as_crew_int(cnt)
            if n is not None:
                fixed[str(role)] = n
        if not fixed:
            continue
        binding = leaf.get("norm_binding")
        if not isinstance(binding, dict):
            binding = {}
        binding["crew"] = fixed
        binding["crew_source"] = "排程实算（定额产能 ÷ 设计班组）"
        leaf["norm_binding"] = binding
        crew_written += 1

    # 关键里程碑（确定性：开工 + 关键路径前 3 项 + 竣工）
    key_milestones = [{"name": "开工", "date": start.isoformat(),
                       "task_id": critical_path[0] if critical_path else "",
                       "description": "项目开工"}]
    for t in critical_path_tasks[:3]:
        key_milestones.append({"name": t["task_name"], "date": t["start_date"],
                               "task_id": t["task_id"], "description": f"{t['task_name']}完成"})
    key_milestones.append({"name": "竣工", "date": end.isoformat(),
                           "task_id": critical_path[-1] if critical_path else "",
                           "description": "项目竣工"})

    # resource_plan
    # 资源分三类（口径真源就在这里，delivery.py 的 LABOR/MACHINE_CREW 与 _is_labor 同源）：
    #   1) LABOR_NAMES  → 工种人工：进 total_manpower_days（工日账本）与人数峰值；
    #   2) MACHINE_CREW → 机械配员（泵工/辅助/操作工/司机）：**是人**，所以计入
    #      「每日在场人数峰值」；但**不是设备**，绝不进 equipment_peak，改为单列
    #      machine_crew_peak 留档；也**不计入** total_manpower_days —— 那是工种人工
    #      工日账本（与 labor_demand 同口径），配员随机械台数走、已含在机械台班里，
    #      混进去会让两个账本互相污染（口径保持不变是硬约束）。
    #   3) 其余         → 真设备：进 equipment_peak。
    total_manpower_days = 0.0
    peak = 0
    for task in rd.get("tasks", []):
        day_sum = 0
        for name, q in (task.get("resources") or {}).items():
            if name in LABOR_NAMES:
                total_manpower_days += q.get("total_days", 0)
                day_sum += q.get("per_day", 0)
            elif name in MACHINE_CREW:
                day_sum += q.get("per_day", 0)
        peak = max(peak, day_sum)
    # ---- 机械 / 配员的峰值改为**按天叠加**（第 40 轮，问题 B）----
    # 旧口径 `equip_peak[name] = max(旧值, 单任务 per_day)`：只取单任务最大值、**不跨任务叠加**
    # → 每种机械恒为 1 台；而交付物逐日铺开算出「履带式单斗液压挖掘机」单日峰值 2 台，
    # 同一份计划两个数打架。现口径与 delivery._compute_view() 完全一致（逐日 bucket 求和取 max）。
    equip_peak, orphan_tasks = _daily_peak(
        rd, sched, lambda n: n not in LABOR_NAMES and n not in MACHINE_CREW)
    machine_crew_peak, _orphan_crew = _daily_peak(rd, sched, lambda n: n in MACHINE_CREW)
    # ---- 峰值人数：来源分档，**模型补的数一律不采纳**（E1 / 2026-09-21 裁定）----
    # 旧口径 `peak_total = boundary.labor.peak_total or boundary.labor_peak`：
    # **申报值一旦存在就顶掉算出来的曲线峰值**（实测：示例3 原文一条资源数据都没有，
    # 120 是 boundary 节点按"18 层住宅常见做法"补的 → 看板印"峰值人数 120 人"，
    # 而逐日曲线实算只有 38 人）。四级口径里还有一支 ③「没有曲线时显示申报值、
    # 标 model_estimate」—— 那同样是**把 AI 补的数印给用户看**，同属病根 3。
    # 用户 2026-09-21 裁定：申报峰值**连源头一起删**，交付物不再有「申报峰值」展示项。
    # 现口径（只剩三档，且第一档必须 `_source == "user"`）：
    #   ① 用户**明确给**的限额（boundary._source["labor.peak_total"] == "user"）
    #      → 用申报值，source="user"（C9：用户限额保留、用于限制峰值）；
    #   ② 否则排程 resource_ok 版逐日曲线峰值 > 0 → **用曲线实算峰值**，source="resource_curve"；
    #   ③ 连曲线都没有 → 退回逐任务口径（曲线实算的保守下界），source="resource_curve"。
    # `declared_peak_manpower` / `declared_peak_manpower_source` 两个键**整体删除**
    # （全仓 grep：不是 `schemas.py` 的硬契约键，可以删；同 `critical_path_length` 那种
    #  被 schema 钉死的键的处理方式不同 —— 那个必须保留）。
    labor_bc = boundary.get("labor") if isinstance(boundary.get("labor"), dict) else {}
    src_map = boundary.get("_source") if isinstance(boundary.get("_source"), dict) else {}
    user_peak = None
    if src_map.get("labor.peak_total") == "user":
        user_peak = _as_number(labor_bc.get("peak_total") or boundary.get("labor_peak"))
        user_peak = int(user_peak) if user_peak and user_peak > 0 else None
    curve_peak = _as_number(sched_ver.get("peak_labor"))
    curve_peak = int(curve_peak) if curve_peak and curve_peak > 0 else None
    task_peak = int(peak) if peak else 0
    if user_peak:
        peak_manpower, peak_source = user_peak, "user"
    elif curve_peak:
        peak_manpower, peak_source = curve_peak, "resource_curve"
    else:
        peak_manpower, peak_source = task_peak, "resource_curve"
    # 曲线峰值字段：有排程曲线就用它，否则退回逐任务口径（不编数）
    curve_peak_manpower = curve_peak if curve_peak else task_peak
    # ---- 【第 2 批 · 域 2 / 2.6】`resource_plan.material_summary` **整体不再产出** ----
    # 删掉的是「主要材料」这张表的构建（原映射表六项：total_concrete /
    # total_rebar / total_area / total_earthwork / total_formwork / total_masonry）。
    # 三条理由，缺一条都不该删：
    #   ① 它就是**展示给用户的"材料清单"**（看板印成「主要材料：混凝土 8000m³…」），
    #      而本批的交付物口径是「本计划不含材料计划。材料按"管够"处理，不参与工期与
    #      资源计算」—— 再摆一张材料表就是自相矛盾；
    #   ② 它给的是 `total_*` 的**裸总量**（既无损耗、无分区段、无时间分布），
    #      标成"主要材料"会让人误以为这是材料需求量；
    #   ③ 这些量本身**没有消失**：仍在 `extracted_params` / `meta.extracted_params` 里
    #      （工程量口径的唯一真源），交付物上的清单不是它们的必要出口。
    # ⚠️ 只删这一处构建 + `schemas.ResourcePlan` 的**字段声明**（两处都已删）：
    # 字段能安全删掉是因为 `schemas._Base` 是 `extra="allow"` —— 已落盘的历史计划 JSON
    # 里那个键会被当**额外字段原样保留**，老计划的重新校验 / 重新出交付物不受影响
    # （计划是不可变档案，同 G5 那条纪律）。
    # ---- 机械主导任务的人工需求（并入 resource_plan，见下方说明）----
    # 顺序确认：`resource_plan`（本函数）与 `meta`（build_meta）都从**同一个 ctx** 取数
    # （assemble_plan_json 里先取 parts["resource_plan"]、再调 build_meta(ctx)），而
    # ctx["machine_labor_demand"] 由 scheduler 节点在 assembler **之前**写入，所以这里
    # 与 build_meta 读的是同一份数据 —— 不存在"组装 resource_plan 时拿不到它"的顺序问题，
    # 不需要在函数收尾回填。
    # 为什么必须并进来：过去它只落在 meta 里，而交付物（Word / 看板）只拿得到 resource_plan
    # → 「混凝土工 2633.6 工日 / 40 个任务」在交付物里彻底消失（用户明确反馈的缺陷）。
    # 字段含义（新增，不改动任何既有字段）：{工种: 工日}，来自 machine_labor_demand.demand。
    mld = ctx.get("machine_labor_demand")
    mld = mld if isinstance(mld, dict) else {}
    labor_demand = dict(mld.get("demand") or {}) if isinstance(mld.get("demand"), dict) else {}
    labor_demand_detail = (dict(mld.get("detail") or {})
                           if isinstance(mld.get("detail"), dict) else {})
    resource_plan = {
        "total_manpower_days": round(total_manpower_days, 1),
        # peak_manpower 语义（第 40 轮修正）：只有用户明确给过才等于"申报限额"，
        # 其余情况一律等于**实算曲线峰值**（见上方四级口径）。配套来源键见下。
        "peak_manpower": peak_manpower,
        "peak_manpower_source": peak_source,
        # E1（2026-09-21 裁定）：`declared_peak_manpower` / `declared_peak_manpower_source`
        # 两个键**已整体删除** —— 它们承载的正是模型替用户补的那个 120。
        # 旧注释（保留作历史依据）：
        #   declared_peak_manpower_source —— 申报的 120 本身是谁给的（用户写的 / 模型补的）
        #   为什么当时要分开：交付物只写"申报峰值 120 人"时，用户会反问"我没申报过 120"。
        #   现在的处置不是"标注来源"，而是**不再产生、不再展示**（重构方案 §0.3 病根 3）。
        "curve_peak_manpower": curve_peak_manpower,
        "equipment_peak": equip_peak,
        "machine_crew_peak": machine_crew_peak,
        # 【第 2 批 · 域 2 / 2.6】`material_summary` **已删除**（见上方说明）。
        # schema 里的同名字段保留为空默认值，只为兼容已落盘的历史计划。
        "labor_demand": labor_demand,
        "labor_demand_detail": labor_demand_detail,
    }
    if orphan_tasks:
        # 失败绝不静默：有任务没落进排程行，它的量只能按"单独一天"计入，必须留痕。
        resource_plan["peak_caliber_note"] = (
            f"有 {orphan_tasks} 项任务没有排程行（es/ef），其资源量按各自单日计入峰值、"
            f"未与其他任务叠加 —— 该机械/配员峰值可能偏低。")

    # risks（确定性：按开工季节 + 工期）
    risks = []
    if 4 <= start.month <= 9:
        risks.append({"risk_name": "雨季施工", "mitigation": "提前搭设排水系统，混凝土配合比按雨季调整"})
    if 6 <= start.month <= 8:
        risks.append({"risk_name": "高温施工", "mitigation": "调整作业时间避开高温时段，加强养护"})
    if 7 <= start.month <= 9:
        risks.append({"risk_name": "台风天气", "mitigation": "塔吊与脚手架加固，制定应急预案"})
    if critical_path:
        risks.append({"risk_name": "关键路径工期", "mitigation": "关键任务优先配置资源，密切跟踪"})
    risks = risks[:3]

    # ---- 审计层：三处"可疑却被静默使用"的数据事实（第 41 轮，只读不改值）----
    # 在这里算（本函数已经拿到 `leaves = _leaf_tasks(wbs)`），并回写进 ctx，
    # 让 build_meta 直接取这一份 —— 同一份计划不重算两遍（①②③都是纯函数/只读查询，
    # 但 ① 要读 kb.db 全表 478 行，没必要算两次）。`pipeline/audit_scope.py`。
    scope_audit_report = audit_scope.scope_audit(leaves)
    ctx["scope_audit"] = scope_audit_report
    # 排程覆盖差集：**写进 ctx**，`build_meta` 才能把它落进 `meta["unscheduled_tasks"]`
    # （PlanAssemblerNode 在 build_parts 之后才调 assemble_plan_json → build_meta）。
    ctx["unscheduled_tasks"] = list(unscheduled)

    return {
        "overview": overview,
        # 关键路径口径（**条数 ≠ 天数**）——与 overview 里那个改名后的条数同源，
        # 这里再把「两个数各自叫什么、单位是什么、来自哪个键」明写一份，见
        # `CRITICAL_PATH_TERM` 上方的事故说明。报告提示词按名字写就不会串。
        "critical_path_caliber": _critical_path_caliber_of(overview, cpm),
        "key_milestones": key_milestones,
        "critical_path_tasks": critical_path_tasks,
        "all_tasks_schedule": all_tasks_schedule,
        "resource_plan": resource_plan,
        "risks": risks,
        # 展示口径（监督报告必须自证"表格是多少行"）——见 display_caliber。
        "display_granularity": display_caliber(ctx, len(all_tasks_schedule)),
        # 审计层（build_meta 会原样取 ctx 这一份落进 plan.meta）。
        "scope_audit": scope_audit_report,
        # 排程覆盖差集（空列表 = WBS 叶子全都排上了）
        "unscheduled_tasks": list(unscheduled),
    }


def unscheduled_effect_text(items) -> str:
    """差集 → 一句给用户看的话（ID + 任务名 + 原因 + 后果）。没有差集返回空串。"""
    rows = [it for it in (items or []) if isinstance(it, dict)]
    if not rows:
        return ""
    lines = ["有 %d 条 WBS 工序没有排程结果，**没有进入交付物**（不显示、不占工期）："
             % len(rows)]
    for it in rows[:20]:
        lines.append("  · %s %s —— %s；%s"
                     % (it.get("task_id"), it.get("task_name"),
                        it.get("reason"), it.get("effect")))
    if len(rows) > 20:
        lines.append("  · …（共 %d 条，其余见 meta.unscheduled_tasks）" % len(rows))
    return "\n".join(lines)


def display_caliber(source, leaf_count=None) -> dict:
    """一句话展示口径（监督报告 / 交付物共用）。

    为什么要有它：用户在计划细度门选了"每 5 层一组"之后，报告里的行数到底指什么
    必须能自证 —— 实测踩过：交付物合并成 164 行、报告一个字不提粒度，读者拿着
    307 项的数字对不上表格。

    ``source`` 既可以是**流水线 ctx**（``display_granularity`` + ``wbs``），
    也可以是**计划 JSON**（``meta.display_granularity`` + ``wbs`` +
    ``all_tasks_schedule``）—— 交付节点手里只有后者（重导出老计划时也要补口径）。

    返回 ``{depth, floor_grouping, depth_label, floor_label, rows, leaves,
    merged, note}``；``note`` 可直接当作报告里的一条要点。未选粒度时按
    工序级 × 按层（逐任务、未合并）说明，同样给出真实行数。
    """
    src = source or {}
    g = src.get("display_granularity")
    if not isinstance(g, dict) or not g:
        meta = src.get("meta")
        g = (meta or {}).get("display_granularity") if isinstance(meta, dict) else None
    if not isinstance(g, dict):
        g = {}
    wbs = src.get("wbs") or {}
    depth = g.get("depth") or quantity_mod.DEPTH_COMPONENT
    grouping = g.get("floor_grouping") or quantity_mod.FLOOR_PER_FLOOR
    if depth not in quantity_mod.DEPTHS:
        depth = quantity_mod.DEPTH_COMPONENT
    if grouping not in quantity_mod.FLOOR_GROUPINGS:
        grouping = quantity_mod.FLOOR_PER_FLOOR
    merged = (depth, grouping) != (quantity_mod.DEPTH_COMPONENT,
                                   quantity_mod.FLOOR_PER_FLOOR)
    if leaf_count is None:
        tasks = src.get("all_tasks_schedule")
        if isinstance(tasks, list) and tasks:
            leaf_count = len(tasks)
    if leaf_count is None:
        try:
            leaf_count = quantity_mod.count_rows(wbs).get("rows")
        except Exception:
            leaf_count = None
    rows = g.get("rows")
    if not rows:
        # 没记下行数就现算一份（同 estimate_rows_for 的纯函数口径），
        # 绝不在报告里留一个"多少行"的空位。
        try:
            rows = quantity_mod.estimate_rows_for(wbs, depth, grouping)
        except Exception:
            rows = None
    depth_label = quantity_mod.DEPTH_LABELS.get(depth, depth)
    floor_label = quantity_mod.FLOOR_LABELS.get(grouping, grouping)
    head = "「%s × %s」→ " % (depth_label, floor_label)
    if merged:
        cnt = "共 %s 行" % (rows if rows else "已合并")
        if rows and leaf_count and leaf_count != rows:
            cnt += "（逐叶子 %d 项）" % leaf_count
        note = (head + cnt + "；仅合并展示行，工程量、工期与资源均未改动"
                "（合并行工期取组内排程时间跨度，总工期与逐任务口径一致）。")
    else:
        cnt = "逐任务 %s 行" % (rows if rows else (leaf_count or "?"))
        note = head + cnt + "，未做任何合并。"
    return {"depth": depth, "floor_grouping": grouping,
            "depth_label": depth_label, "floor_label": floor_label,
            "rows": rows, "leaves": leaf_count, "merged": bool(merged),
            "note": note}


def with_caliber(report, note):
    """把"展示口径"钉进报告正文 —— 不靠 LLM 自觉，也不靠调用方记得写。

    模型一句不提粒度，报告里的行数就没人能对上（实测：交付物合并成 164 行、
    报告 0 处提粒度）。插成"一、总体情况"的第一条要点，找不到该节就退到首个
    标题之后；已提到则原样返回，不重复。
    """
    text = (report or "").strip()
    if not text or not note or "展示口径" in text:
        return text
    bullet = "- **展示口径**：%s" % note
    lines = text.splitlines()
    at = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#") and "总体情况" in ln:
            at = i + 1
            break
    if at is None:
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#"):
                at = i + 1
                break
    if at is None:
        at = 0
    return "\n".join(lines[:at] + [bullet] + lines[at:]).strip() + "\n"


# ══════════════ 关键路径口径：**条数（个）** 与 **天数（天）** 是两回事 ══════════════
# 真实缺陷（真计划 `plans/plan_run_1789895021.json`：overview.critical_path_length=81、
# total_duration_days=604、cpm_result.total_duration_days=604）：交付物正文写着
#   「**当前计划总工期为604天，关键路径长度为81天**，表明非关键路径任务具有一定的浮动时间。」
# —— 81 是**关键路径任务的条数（个）**，被写成了天数；而同一份交付物的表格里明明写着
# 「关键路径任务数 81 个」「关键路径工期·排程版 604 天」。歧义源就是那个老键名里的
# "长度"（它和 overview.total_duration_days 摆在一起，读成天数太自然了）。
#
# 三道防线（一条都不靠模型自觉）：
#   ① 喂给模型的输入里，这个数**带名字、带单位**：报告 parts 的 overview 里歧义键名换成
#      `critical_path_task_count`（+ note），并另给一份 `critical_path_caliber`；交付侧
#      facts 同样处理（`delivery._facts_bundle`）；
#   ② 提示词写死铁律（`prompts/report.txt`、`prompts/deliver_html.txt`）；
#   ③ 渲染 / 落盘前的**确定性改写** —— `fix_critical_path_wording`（下面）。
#      为什么必须有③：`plan["report"]` 是**上一次运行落盘**的 Markdown，改提示词管不到
#      历史计划（`plans/` 里已经躺着的那些）。docx / 看板 / 交付 facts 三条出口都在
#      印出来之前过这一道，`PlanDeliverNode` 落盘前也过一道。
CRITICAL_PATH_TERM = "关键路径长度"
CRITICAL_PATH_COUNT_LABEL = "关键路径任务数"
CRITICAL_PATH_DAYS_LABEL = "关键路径工期·排程版"
CRITICAL_PATH_COUNT_UNIT = "个"
CRITICAL_PATH_DAYS_UNIT = "天"
CRITICAL_PATH_COUNT_NOTE = "条数（个），不是天数"
CRITICAL_PATH_WORDING_RULE = (
    "「%s」这个词禁用（既可能被读成条数、也可能被读成天数）：条数写"
    "「%s N %s」，天数写「%s N %s」。`overview.critical_path_length` 装的是**条数**"
    "（单位：%s），不是天数；关键路径的天数取 `cpm_result.total_duration_days`"
    "（与总工期同源）。"
    % (CRITICAL_PATH_TERM, CRITICAL_PATH_COUNT_LABEL, CRITICAL_PATH_COUNT_UNIT,
       CRITICAL_PATH_DAYS_LABEL, CRITICAL_PATH_DAYS_UNIT, CRITICAL_PATH_COUNT_UNIT))

# 「关键路径长度[为是:：]? 81 天」——把**条数**当天数写出来的句子（本次缺陷的原句）。
_CP_TERM_RE = re.compile(
    r"关键路径长度(?P<sep>\s*(?:为|是|＝|=|:|：)?\s*)"
    r"(?P<num>\d+(?:\.\d+)?)?(?P<day>\s*天)?")
# 「关键路径由 81 项任务组成，总时长 81 天」——同一段里的第二处（同一条数的另一种写法）。
_CP_CHAIN_SUM_RE = re.compile(
    r"关键路径由\s*(?P<n1>\d+)\s*项任务组成[，,、]?\s*(?:总时长|总工期|工期为|工期)\s*"
    r"(?P<n2>\d+)\s*天")


def _num_text(value) -> str:
    """数字进正文时不留 `.0`（604.0 → 604）；非数字原样返回（绝不抛）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


def _critical_path_caliber_of(overview, cpm) -> dict:
    """关键路径两个数的**带标签**版本（条数 / 天数）；取不到的键不出现。"""
    overview = overview if isinstance(overview, dict) else {}
    cpm = cpm if isinstance(cpm, dict) else {}
    out = {"rule": CRITICAL_PATH_WORDING_RULE}
    n = overview.get("critical_path_length")
    if n is None:
        n = overview.get("critical_path_task_count")
    if n is not None:
        out["critical_path_task_count"] = {
            "label": CRITICAL_PATH_COUNT_LABEL, "value": n,
            "unit": CRITICAL_PATH_COUNT_UNIT, "meaning": CRITICAL_PATH_COUNT_NOTE,
            "source": "overview.critical_path_length",
        }
    v = cpm.get("total_duration_days")
    if v is not None:
        out["critical_path_duration_days"] = {
            "label": CRITICAL_PATH_DAYS_LABEL, "value": v,
            "unit": CRITICAL_PATH_DAYS_UNIT,
            "source": "cpm_result.total_duration_days",
        }
    return out


def critical_path_caliber(source) -> dict:
    """`source`（plan_json 或 parts，含 overview / cpm_result）→ 两个数的带标签版本。"""
    if not isinstance(source, dict):
        return {}
    return _critical_path_caliber_of(source.get("overview"), source.get("cpm_result"))


def _overview_contract(overview) -> dict:
    """把 parts 的 overview 还原成**契约形状**（`critical_path_length` 必须有）。

    `build_parts` 交给报告模型的 overview 去掉了歧义键名（→
    `critical_path_task_count` + note，见 `CRITICAL_PATH_TERM` 上方说明），而
    `schemas.Overview` 与看板 / 契约测试读的都是 `critical_path_length` —— 落盘前在这里
    改回去，并清掉那两个"只给模型看"的键。已经有 `critical_path_length` 的原样保留
    （老调用方 / 手工 parts 不受影响）。
    """
    out = dict(overview or {})
    if out.get("critical_path_length") is None:
        n = out.get("critical_path_task_count")
        if n is not None:
            out["critical_path_length"] = n
    out.pop("critical_path_task_count", None)
    out.pop("critical_path_task_count_note", None)
    return out


def fix_critical_path_wording(text, source=None):
    """把「关键路径长度为 N 天」这类**把条数当天数**的句子改写成明确口径。

    返回 ``(新文本, 改写说明列表)``。判据只有一条：**数字必须与计划真值对得上**
    （条数 = `overview.critical_path_length`，天数 = `cpm_result.total_duration_days`）——
    对不上、或真值取不到，那一处**一个字都不动**（宁缺勿造：绝不猜、绝不拿别的数顶上）。

    四类改写：
      · 术语：`关键路径长度`（后面没数字）→ `关键路径任务数`
      · 条数当天数：`关键路径长度为 81 天`（81 == 条数）→ `关键路径任务数为81 个（条数，不是天数）`
      · 术语 + 真天数：`关键路径长度为 604 天`（604 == 天数）→ `关键路径工期·排程版为 604 天`
      · 链式句：`关键路径由 81 项任务组成，总时长 81 天`（两个数都是条数）→ 后半句改成真天数
    """
    text = text or ""
    if not text or CRITICAL_PATH_TERM not in text:
        return text, []
    truth = critical_path_caliber(source)
    count = (truth.get("critical_path_task_count") or {}).get("value")
    days = (truth.get("critical_path_duration_days") or {}).get("value")
    fixes = []

    def _eq(a, b):
        if a is None or b is None:
            return False
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False

    def _chain(m):
        n1, n2 = m.group("n1"), m.group("n2")
        if _eq(n1, count) and _eq(n2, count) and days is not None and not _eq(count, days):
            fixes.append("链式句：关键路径由%s项任务组成、总时长写%s天 → 天数改为 %s 天"
                         % (n1, n2, _num_text(days)))
            return ("关键路径由 %s 项任务组成，%s为 %s 天"
                    % (n1, CRITICAL_PATH_DAYS_LABEL, _num_text(days)))
        return m.group(0)

    text, _n_chain = _CP_CHAIN_SUM_RE.subn(_chain, text)

    def _term(m):
        sep, num, day = m.group("sep") or "", m.group("num"), m.group("day")
        if num is None:                      # 光是一个术语，没有数字可判
            fixes.append("术语「%s」→「%s」" % (CRITICAL_PATH_TERM, CRITICAL_PATH_COUNT_LABEL))
            return CRITICAL_PATH_COUNT_LABEL + sep
        if _eq(num, count) and not _eq(num, days):
            fixes.append("条数当天数：「%s%s%s%s」→「%s%s%s %s（条数，不是天数）」"
                         % (CRITICAL_PATH_TERM, sep, _num_text(num), day or "",
                            CRITICAL_PATH_COUNT_LABEL, sep, _num_text(num),
                            CRITICAL_PATH_COUNT_UNIT))
            return ("%s%s%s %s（条数，不是天数）"
                    % (CRITICAL_PATH_COUNT_LABEL, sep, _num_text(num),
                       CRITICAL_PATH_COUNT_UNIT))
        if day and _eq(num, days) and not _eq(num, count):
            fixes.append("术语改名（数字不动）：「%s%s%s天」→「%s%s%s 天」"
                         % (CRITICAL_PATH_TERM, sep, _num_text(num),
                            CRITICAL_PATH_DAYS_LABEL, sep, _num_text(num)))
            return ("%s%s%s %s" % (CRITICAL_PATH_DAYS_LABEL, sep, _num_text(num),
                                   CRITICAL_PATH_DAYS_UNIT))
        return m.group(0)                    # 认不出来 → 原样（绝不猜）

    text, _n_term = _CP_TERM_RE.subn(_term, text)
    return text, fixes


# ══════════════════════ 模型参与度：**恒存在**的结论字段 ══════════════════════
# 为什么必须加（真实事故）：
#   计划 `plan_sample3_after_org` 的 `meta["usage"]` 是
#   `{"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
#     "cost_cny": 0.0, "by_node": {}, "model": ""}` —— **一次模型都没调用**。
#   **整条主链**全部静默走确定性兜底，流水线照样报成功：209 条任务、门都答了、
#   `audit_status=未审计`，**没有任何地方说明"模型没有参与"**。
#   后果是硬的：`meta["boundary_conditions"]` 里 labor/equipment/materials/工期全无值
#   （只剩从用户原文正则抽到的节拍）、`meta["equipment_binding"]` 为空、
#   `extracted_params.total_concrete / total_rebar` 为 `null` —— 而用户会拿它当方案用。
#   光有 `usage.calls` 不够：它只是用量账本里的一个数字，**没有任何地方会为它报警**，
#   交付侧也无从知道"0 次"意味着什么。所以这里把判断钉成一个恒存在的字段，并给出
#   可直接渲染的中文 note（交付层在 `level != "ok"` 时显著渲染，见 delivery.py）。
MODEL_NONE_NOTE = (
    "本次运行没有任何模型调用：所有内容由确定性兜底生成（模型未参与），"
    "参数抽取、边界条件（劳动力/设备/材料）、定额匹配可能缺失，请勿据此施工。")
MODEL_UNKNOWN_NOTE = "本次运行未记录模型用量，无法判断模型是否参与。"


# ══════════════ 节点级告警 / 模型调用失败：**恒存在**的留档字段 ══════════════
# 为什么必须加（与 model_participation 同一起因，第 42 轮的真实事故）：
#   流水线跑完 `plan_sample3_after_org_v2`（本次运行 32 次模型调用、¥0.8）后，
#   `meta["boundary_conditions"]` 里 labor/equipment/materials **全是空的**
#   （`peak_total: null`、`[]`、`[]`）；而同一份输入手工复现那次模型调用是成功的
#   （`peak_total=120` + 8 个工种 + 4 台设备），事后逐条核对也证明**不是抛异常**
#   （抛异常走的是 `_boundary_by_regex(prompt)` 兜底，而那一支根本不会造出
#    labor.by_trade / equipment / materials / project_duration_days 这几个键，
#    见 boundary.py:851-863；产物里这四个键都在 ⇒ 走的是 `if llm_out:` 分支）。
#   真相是：**模型答了，只是答了一个"结构正确、值全空"的 JSON**，而那条路径
#   原本一句 warning 都不发 —— 于是"模型这次没把资源清单填出来"对用户完全不可见，
#   产物看起来完全正常。为此补了两处：boundary 侧新增"三类全空"告警
#   （`EMPTY_RESOURCES_NOTE_KEY` / `resources_all_empty()`），以及这里的留档。
#   这是产品级的诚实性缺陷，不修就是拿兜底产物冒充方案。
#   留档点：`engine.collect_node_warning`（所有节点 `emit("warning")` 的唯一必经之路）
#   把告警收进 `ctx["node_warnings"]`，这里再**原样透传**进 meta。
NODE_WARNINGS_CTX_KEY = "node_warnings"

#: 「模型这条路没走通」的**内容**判据。
#: 为什么按内容匹配、而不是靠节点名：
#:   · 同一个节点两种告警并存 —— boundary 既会因**模型调用失败**发告警
#:     （`模型调用失败，已退回关键词兜底`），也会因**节拍超区间**发告警（如"9 天/层
#:     超出常见区间"）。按节点名判会把后者也误报成模型失败；
#:   · 反过来，任何节点（既有的 extractor/reporter/revise/wbs_agent/kb_scope，以及
#:     以后新加的）第一次调模型失败都会发同一条告警 —— 按节点名判必漏新节点；
#:   · 告警的 `message` / `detail` 里写的就是失败原话（`detail` 直接来自 LLMError），
#:     内容是最稳的判据。
#: 下面每条都取自**本仓真实文案**（出处写在行尾），不是凭空猜的词。
MODEL_FAILURE_MARKERS = (
    "模型调用失败",           # nodes/boundary.py:923（本次事故的原文措辞）
    "模型不可用",             # nodes/boundary.py:919 的 node_progress 文案
    "模型没答上来",           # nodes/extractor.py:291（同义措辞，一并认）
    "LLM 不可用",             # nodes/reporter.py:38、nodes/revise.py:1390/1392
    "LLM 调用失败",           # llm.py:118 的 LLMError 原文
    "LLM HTTP",               # llm.py:86 的 LLMError 原文（HTTP 500 那一类）
    "LLM 响应结构异常",       # llm.py:106/152
    "LLM 输出不是合法 JSON",  # llm.py:126
    "未配置 QWEN_API_KEY",    # llm.py:54（没配 key 同样是"模型这条路走不通"）
    "退回关键词兜底",         # nodes/boundary.py:923 的降级动作
    "已用规则解析",           # nodes/revise.py:1388/1390/1392 的降级动作
)


def _as_warning_list(source) -> list:
    """从 ctx 或**老计划 meta** 里取告警列表；取不到/类型不对 → `[]`（绝不抛）。

    老计划（如 `plans/plan_sample3_after_org_v2.json`）的 `meta` 里根本没有这两个键，
    而交付层 / 修订链 / `/sources` 会在**老计划上**调这里 —— 缺键必须等于"没有告警"，
    不许抛异常、更不许编造。
    """
    if not isinstance(source, dict):
        return []
    raw = source.get(NODE_WARNINGS_CTX_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for it in raw:
        if isinstance(it, dict):
            out.append(dict(it))          # 浅拷贝：不把 meta 里的字典暴露给调用方乱改
    return out


def _is_model_call_failure(warn) -> bool:
    """一条告警是否属于"模型调用失败 / 退回规则兜底"（按 message + detail 的内容判）。"""
    if not isinstance(warn, dict):
        return False
    text = "%s %s" % (warn.get("message") or "", warn.get("detail") or "")
    return any(m in text for m in MODEL_FAILURE_MARKERS)


def model_call_failures_of(warns) -> list:
    """从告警列表里挑出模型调用失败的那些，并**投影成固定三键**。

    固定 `{node, message, detail}`（丢掉 `at`/`count`）：这是给展示层/机读用的汇总，
    键固定才不会每处各写一套判据（与 `meta["kb_warnings"]` 同一条理由）。
    """
    out = []
    for w in (warns or []):
        if _is_model_call_failure(w):
            out.append({"node": str(w.get("node") or ""),
                        "message": str(w.get("message") or ""),
                        "detail": str(w.get("detail") or "")})
    return out


def model_participation(usage) -> dict:
    """usage 快照 → 参与度结论（恒存在；判据真源 = `meta["usage"]["calls"]`）。

    三态（**"未知"绝不等于"合格"**）：
      · calls > 0  → participated=True  / level="ok"      / 带上模型名
      · calls == 0 → participated=False / level="none"    / note 里写明"请勿据此施工"
      · 缺失/非字典/calls 取不到数 → participated=None / level="unknown"
    """
    if not isinstance(usage, dict):
        return {"participated": None, "calls": None, "level": "unknown",
                "note": MODEL_UNKNOWN_NOTE}
    try:
        calls = int(usage.get("calls"))
    except (TypeError, ValueError):
        return {"participated": None, "calls": None, "level": "unknown",
                "note": MODEL_UNKNOWN_NOTE}
    if calls > 0:
        return {"participated": True, "calls": calls, "level": "ok",
                "model": str(usage.get("model") or ""), "note": ""}
    if calls == 0:
        return {"participated": False, "calls": 0, "level": "none",
                "note": MODEL_NONE_NOTE}
    # 负数等异常值：当"未知"处理，不许折算成"合格"。
    return {"participated": None, "calls": None, "level": "unknown",
            "note": MODEL_UNKNOWN_NOTE}


def build_meta(ctx) -> dict:
    """组装计划元数据（审计状态 / 细度 / 模式 / 品牌 / 用量 / 参与度 / 可信度）。

    这些字段让"这份计划是谁生成的、数据多可信、模型有没有参与、花了多少钱、审没审"
    一眼可查。
    """
    sv = ctx.get("schedule_versions") or {}
    chosen = ctx.get("schedule_chosen") or "resource_ok"
    ver = sv.get(chosen) or {}
    try:
        usage_snap = usage_mod.meter().snapshot()
    except Exception:
        usage_snap = None
    # ---- 模型参与度：必须在 `usage` 快照**就位之后**才算（判据就是它的 calls）----
    # 快照在这里取（`usage_mod.meter().snapshot()`，唯一写 `meta["usage"]` 的地方就是
    # 本函数；`engine.py` 只在 node_done 事件里另发一份，不写 meta），计划一旦落盘，
    # `meta["usage"]` 就是唯一可复核的用量记录 —— 所以结论也在同一处算。
    participation = model_participation(usage_snap)
    # ---- 节点级告警 / 模型调用失败（第 42 轮，恒存在）----
    # 来源是 `engine.collect_node_warning` 落下的 `ctx["node_warnings"]`（去重 + count）。
    # 取不到就是 `[]`（老 ctx / 直接调 build_meta 的契约测试）——**绝不编造**。
    node_warns = _as_warning_list(ctx)
    model_fails = model_call_failures_of(node_warns)
    # 编制口径：本计划按**标准栋**编制，全项目 N 栋平行施工（单栋工期 ≈ 项目工期）。
    # 用户必须一眼看到这件事，否则会以为计划漏算了其他栋。
    params = ctx.get("extracted_params") or {}
    n_build = building_count(params)
    floors = params.get("floors")
    try:
        floors = int(floors) if floors not in (None, "") else None
    except (TypeError, ValueError):
        floors = None
    caliber_note = building_count_note(params) or "单栋项目，按整栋口径编制"
    if floors is None:
        caliber_note += "；层数未从资料中取得，暂用配置默认层数（待用户确认）"
    # 审计状态：**不采信 `ctx["audited"]`** —— 它只表示"三轮都 passed"，脚本代答同样为真
    # （见 `audit_gate.answered_by_of`）。一律走 `audit_honesty`：每轮都要
    # `answered_by == "human"` 才配印「已审计」。延迟导入避免模块级循环导入。
    from .audit_gate import audit_honesty
    _audit_rounds = list(ctx.get("audit_rounds") or [])
    _audit_ok = audit_honesty({"audit_rounds": _audit_rounds})["confirmed"]
    return {
        "audit_status": "已审计" if _audit_ok else "未审计",
        "audit_rounds": _audit_rounds,
        "audit_comments": list(ctx.get("audit_comments") or []),
        # WBS 叶子 − 排程行 的差集（无则 `[]`）。**必须落进 meta**：这些工序没进
        # `all_tasks_schedule`，交付物里看不到它们，只有这份账能证明"少了几条、是哪几条"。
        "unscheduled_tasks": list(ctx.get("unscheduled_tasks") or []),
        "plan_level": ctx.get("plan_level") or "L4",
        "plan_mode": chosen if sv else "",
        "building_count": n_build,
        "floors": floors,
        "caliber_note": caliber_note,
        "version": branding.VERSION,
        "brand": {
            "product": branding.PRODUCT,
            "team": branding.TEAM,
            "school": branding.SCHOOL,
            "slogan": branding.SLOGAN,
            "subtitle": branding.SUBTITLE,
            "sign": branding.SIGN,
        },
        "usage": usage_snap,
        # ---- 模型参与度（恒存在）：与 `usage` 紧邻落档 ----
        # 上面这段 note 会**原样**渲染进看板与 Word（delivery.model_participation_notice）；
        # level == "ok" 时交付侧一个字都不加，保证既有逐字回归门不受影响。
        "model_participation": participation,
        # ---- 节点级告警（恒存在）：**本次运行有哪些节点报过事** ----
        # 为什么必须有这个键（真实事故，见本文件上方 MODEL_FAILURE_MARKERS 的说明）：
        #   一次模型调用失败只发了一条 warning 事件，事件飘到终端就没了，计划 JSON 与
        #   交付物里一个字都不留 —— 用户拿到的产物看起来完全正常。落进 meta 之后，
        #   交付物 / 修订链 / `/sources` 都拿得到"这次模型没帮上忙"这件事。
        # 逐条原样透传 `{node, message, detail, at, count}`（交付侧要原文，不要摘要）。
        "node_warnings": node_warns,
        # 显式条数：**不让调用方自己去 len()** —— 展示层拿到的可能是老计划（键缺失）或
        # 类型被写坏的数据，`len()` 会抛；而 `node_warning_count` 与 `node_warnings` 在
        # 同一处、同一份数据上算出来，永远自洽（0 与"取不到"都写 0，不写 null）。
        "node_warning_count": len(node_warns),
        # ---- "模型调用失败"的**可机读**汇总（恒存在）----
        # 判据真源 = 上面告警的 message/detail 内容（字面量见 MODEL_FAILURE_MARKERS），
        # 所以它只是 node_warnings 的一个**投影**，不会与告警列表分叉。
        "model_call_failures": model_fails,
        "data_sources": ctx.get("data_sources") or [],
        "credibility": ctx.get("credibility") or {},
        "total_duration_days": ver.get("total_duration_days"),
        # ---- 让计划**自包含**：修订（自然语言改计划）时要按同样的口径重排，
        # 就必须拿得到原始项目参数与边界条件。少了它们，recompute 只能"凭猜"
        # 或干脆不重排，"改得动"就变成半截功能。
        "extracted_params": dict(params),
        "boundary_conditions": dict(ctx.get("boundary_conditions") or {}),
        "schedule_versions": {
            "theory_min_days": (sv.get("theory_min") or {}).get("total_duration_days"),
            "resource_ok_days": ver.get("total_duration_days"),
            "delta_days": (sv.get("compare") or {}).get("delta_days"),
            # 口径随数字落盘（P0-B）：`theory_min_days` 是**排程器自己的**理论最短
            # （逐任务工期与实排同源），与 `cpm_result.cpm_total_duration_days`
            # （按 WBS 目标天数正推）**不同源**，两者直接比大小没有意义。
            "theory_min_basis": "scheduler_task_durations",
            "comparable_to_cpm": False,
        },
        # ---- 两个"口径账本"，交付物里要能核对 ----
        # norm_coverage：哪些工程量进了定额、哪些没进、占多少
        # machine_labor_demand：机械主导任务的人工需求（单独口径，只报不算工期）
        "norm_coverage": ctx.get("norm_coverage") or {},
        "machine_labor_demand": ctx.get("machine_labor_demand") or {},
        # ---- 展示粒度必须落进计划本身 ----
        # 交付物（Word / HTML）只拿得到 plan，拿不到 ctx。少了这一段，用户在粒度门
        # 里选"工种级 × 整栋"就**只在对话里生效**，导出的文件仍是全部叶子
        # —— 选择被问了、被记了、被显示了，却没被执行。
        "display_granularity": dict(ctx.get("display_granularity") or {}),
        # ---- 知识库范围装配的警告（"算完了要能用"）----
        # kb_scope 只把**归并后**的一句摘要放进 done_summary（同类不刷屏）；
        # 逐条原文在这里留档 —— 交付物 / /sources / 修订链都拿得到，
        # 不会再出现"终端只说警告 N 条，用户永远看不到内容"。
        "kb_warnings": list(ctx.get("kb_warnings") or []),
        # ---- 知识库范围一致性核对的结果（"范围装好了，计划真的照着做了吗"）----
        # kb_scope 只决定"允不允许"；这个块是**事后核对**：树里有没有出现范围外的
        # kb_activity_id（实测踩到过：节拍配置硬编码的"柱浇筑"在剪力墙项目里照进计划）。
        # 留档进 meta —— 交付物 / 修订链 / /sources 都拿得到，用户能自己核。
        "kb_scope_conformance": dict(ctx.get("kb_scope_conformance") or {}),
        # ---- 必含工程类型校验（"该有的工程类型到底有没有"）----
        # 必须连同 `checked`/`reason` 一起留档：校验没跑成（如没解析到建筑类型）时
        # `missing` 恒为空，只看 missing 会把"没校验"读成"不缺" —— 实测踩过这个坑。
        "kb_essentials": dict(ctx.get("kb_essentials") or {}),
        # ---- 参数完备性 / 试算标记 ----
        # 用户若在参数门上选择"用默认值试算"，交付物必须显著标注"不可用于施工"。
        # 只存在 ctx 里就等于没标 —— 交付物只拿得到 plan。
        "params_completeness": dict(ctx.get("params_completeness") or {}),
        "trial_mode": bool(ctx.get("trial_mode")),
        # ---- 用户申报设备的**对账结果**（第 40 轮，问题 B2）----
        # 背景（实测）：用户申报的塔吊/施工电梯压根没匹配到计划里的机械资源，
        # scheduler 的 equipment_binding_report() 早就算出了这件事，但它只活在 ctx 里，
        # `plan.meta` **没有这个键** → 用户完全看不到"塔吊 1 台未生效"。这里透传进 meta：
        # 每项 {name, quantity, bound_to, effective, note}；拿不到就是空列表（不编）。
        # 再与**资源层实际投入**的场地级设备（塔吊/施工电梯）对齐：排程那份对账早于资源节点，
        # 认不出资源层注入的设备，见 `_bind_actual_resources`。
        "equipment_binding": _bind_actual_resources(
            _equipment_binding_items(ctx.get("equipment_binding")),
            ctx.get("resource_demand") or {}),
        # ---- 审计层：三处"可疑却被静默使用"的数据事实（第 41 轮）----
        # 只读，**不改任何值**（`pipeline/audit_scope.py`）：
        #   ① 工作面人数上限：表里旧 max_labor / 新 crew_max / 实际生效 effective_crew_max
        #      三套并存（实测 487 行里 387 行新旧不一致、385 行再被公式抬高），
        #      计划印的是旧上限、排程用的是抬高后的上限，中间没有任何提示；
        #   ② 同一 source_code 给出差 1.79 倍的定额值（实测 4.43 vs 7.91 工日/t）；
        #   ③ 同一活动 + 同一楼层同时按面积与体积各排一条（实测 18 组，换算厚度 0.2 m）。
        # 落进 meta 而不是只留在 ctx 里：交付物 / /sources / 修订链都拿得到，
        # 用户能自己核（与 kb_warnings / kb_scope_conformance 同一条理由）。
        # `build_parts` 已经算过的直接复用（那次是针对**同一个 wbs** 算的），避免同一份
        # 计划算两遍；ctx 里没有（如只调 build_meta 的契约测试）才算一份。
        "scope_audit": (ctx.get("scope_audit")
                        or audit_scope.scope_audit(ctx.get("wbs") or {})),
        # ---- WS6 施工组织层：**按节拍做不到**的工序清单（第 41 轮）----
        # 调度器 `compute_schedules` 早就算好了（`out["organization_gaps"]`，无节拍时恒为
        # `[]`），交付物 `delivery.organization_section_model()` 也**只从 meta 读**这个键
        # （`delivery.py:2021` 的 facts / `:2975` 的缺口段）—— 但两边中间**没人搬**，
        # 于是"组织缺口报告"永远是空的：实测注入 7 天节拍后，`1.5.1 混凝土运输` 需要
        # 19 个作业面（上限 4）这个结论到不了用户眼前（这正是"算了但没送到"那一类）。
        # 与 equipment_binding / scope_audit 同一条理由：落进 meta，交付物与修订链都拿得到。
        "organization_gaps": _organization_gaps_items(ctx.get("organization_gaps")),
        # ---- 域 5 工程量覆盖账（第 3 批）----
        # ⚠️ 这一行是**必须的搬运**，不是可选项：`build_meta` 是**显式白名单字典**，
        # 没列在这里的 ctx 键**永远进不了 meta**。域 5 的新节点
        # （`QuantityAgentNode`，name="quantity_fill"）把逐 L4 的表态与「未入树清单」
        # 写在 `ctx["quantity_coverage"]`，而 `delivery.quantity_coverage_blocks()`
        # **只从 meta 读**这个键 —— 两边中间没人搬，第 8 节「工程量来源与未入树清单」
        # 就永远是空的（与上面 `organization_gaps` 那条**完全同一类**事故：
        # "算了但没送到"）。设计文档亦点名此处：`域5_补量与冻结_实现设计.md:450-451`。
        "quantity_coverage": dict(ctx.get("quantity_coverage") or {}),
        # ---- 第 5 批 编号留痕（域 4）----
        # 同一类"算了但没送到"：编号口径/候选集违规/LLM 工序清单写在
        # `ctx["beat_subtrees"][阶段]["numbering"]`，而本字典是显式白名单 ⇒
        # 不搬这一行，交付物里就看不到"id 是按什么口径编的"。见 `_numbering_projection`。
        "numbering": _numbering_projection(ctx.get("beat_subtrees")),
        # ---- 第 5 批 占比表降级留痕（域 3 收口）----
        # 同一类"算了但没送到"：`step_not_ratio_driven` 说明"用户给了分项总量、但占比表
        # 不覆盖该工种 ⇒ 没法按占比拆"，必须进交付物，绝不静默。见 `_ratio_notes_projection`。
        "ratio_notes": _ratio_notes_projection(ctx.get("beat_subtrees")),
        # ---- 域 8.3 搬运：日级资源账单 ----
        # `_daily_share` / `_backpressure` 只存在于裸版本 dict 上（`_` 前缀被
        # `_public_version` 剥掉），`schedule_versions` 里拿不到。
        # scheduler 节点在 `_public_version` 之前把它们抬成独立 ctx 键
        # `daily_resource_share` / `resource_backpressure`，这里搬运进 meta。
        "daily_resource_share": dict(ctx.get("daily_resource_share") or {}),
        "resource_backpressure": dict(ctx.get("resource_backpressure") or {}),
        # ---- 域 8.1：AI 补的限额被丢弃 ----
        # `_ignored_model_limits` 在 `resource.py` 里写入（带 `_` 前缀），
        # 被 `_public_version` 剥掉。scheduler 节点不搬运它（它在 resource_demand 子键上），
        # 这里从 ctx["resource_demand"] 读取并搬进 meta。
        "_ignored_model_limits": list(
            (ctx.get("resource_demand") or {}).get("_ignored_model_limits") or []),
    }


def assemble_plan_json(ctx, parts, report="") -> dict:
    """把 parts + report 组装为 plan_json（不含 plan_id，deliver 节点补充）。

    `parts["overview"]` 里那个歧义键名已经在 `build_parts` 换成
    `critical_path_task_count`（给模型看的形状），落盘必须还原成契约键名
    `critical_path_length` —— 见 `_overview_contract`。
    `report` 也在这里过一道关键路径口径的确定性改写（老计划 / 手工传进来的报告都覆盖）。
    """
    _ov = _overview_contract(parts["overview"])
    _cpm = ctx.get("cpm_result") or {}
    return {
        "plan_id": "",
        "overview": _ov,
        "wbs": ctx.get("wbs") or {},
        "dependencies": (ctx.get("dependencies") or {}).get("dependencies", []),
        "cpm_result": ctx.get("cpm_result") or {},
        "resource_demand": ctx.get("resource_demand") or {},
        "key_milestones": parts["key_milestones"],
        "critical_path_tasks": parts["critical_path_tasks"],
        "all_tasks_schedule": parts["all_tasks_schedule"],
        "resource_plan": parts["resource_plan"],
        "risks": parts["risks"],
        "report": fix_critical_path_wording(
            report or "", {"overview": _ov, "cpm_result": _cpm})[0],
        "meta": build_meta(ctx),
    }


def template_report(parts) -> str:
    """LLM 不可用时的确定性报告模板。"""
    ov = parts["overview"]
    ms = parts["key_milestones"]
    cp = parts["critical_path_tasks"]
    rp = parts["resource_plan"]
    rs = parts["risks"]
    # 条数：`build_parts` 给模型的键名是 `critical_path_task_count`（无歧义），
    # 手工/老调用方传进来的 parts 仍用契约键 `critical_path_length` —— 两个都认。
    _cp_count = ov.get("critical_path_length")
    if _cp_count is None:
        _cp_count = ov.get("critical_path_task_count")
    lines = [
        "# 监督报告",
        "",
        "## 一、总体情况",
        f"- 项目：{ov['project_name']}",
        f"- 总工期：{ov['total_duration_days']} 天（{ov['planned_start_date']} → {ov['planned_end_date']}）",
        # ⚠️ 这不是"长度/天数"，是**条数**（P0-B）。老写法「关键路径长度：89」被用户
        # 读成"关键路径 89 天"，而同一份计划里 89 条关键任务只占 608 天。
        f"- 关键路径任务数：{_cp_count} 个（条数，不是天数）",
    ]
    caliber = (parts.get("display_granularity") or {}).get("note")
    if caliber:
        lines.append(f"- **展示口径**：{caliber}")
    lines += [
        "",
        "## 二、关键里程碑",
    ]
    for m in ms:
        lines.append(f"- **{m['name']}**（{m['date']}）— {m['description']}")
    lines += ["", "## 三、关键路径分析"]
    chain = " → ".join(f"{t['task_name']}({t['task_id']})" for t in cp[:8])
    lines.append(f"- {chain}" if chain else "- 无")
    lines += ["", "## 四、资源投入分析",
              f"- 峰值人数：{rp['peak_manpower']} 人",
              f"- 总人·日：{rp['total_manpower_days']}",
              f"- 设备峰值：{json.dumps(rp.get('equipment_peak', {}), ensure_ascii=False)}",
              "", "## 五、主要风险与应对"]
    for r in rs:
        lines.append(f"- **{r['risk_name']}**：{r['mitigation']}")
    lines += ["", "## 六、监理建议",
              "1. 关键路径任务优先配置资源，避免工期延误；",
              "2. 雨季/高温/台风季节提前落实专项措施；",
              "3. 加强资源高峰期的现场协调与安全交底。"]
    # 「该有的也不许静默丢」：WBS 里有、排程里没有的工序**必须写进报告**，
    # 否则读者会以为"表格里没有 = 这活不用干"。
    uns = parts.get("unscheduled_tasks") or []
    if uns:
        lines += ["", "## 七、未排上日程的工序（**未进入交付物**）",
                  "以下工序在 WBS 里有，但没有排程结果，因此不在进度表里、也不占工期："]
        for it in uns[:20]:
            lines.append("- %s %s：%s" % (it.get("task_id"), it.get("task_name"),
                                          it.get("reason")))
        if len(uns) > 20:
            lines.append("- …（共 %d 条，其余见 meta.unscheduled_tasks）" % len(uns))
    return "\n".join(lines)


class PlanAssemblerNode(BaseNode):
    name = "assembler"
    title = "方案汇总"

    def run(self, ctx):
        parts = build_parts(ctx)
        ctx["plan_parts"] = parts
        # 「数据里该有的也不许静默丢」：WBS 叶子缺排程行时，除了落进
        # `meta["unscheduled_tasks"]`，还要走**引擎的告警通道**（`emit("warning")` →
        # `engine.collect_node_warning` → `meta["node_warnings"]` → 交付物的告警卡片），
        # 否则这份账只躺在 JSON 里，终端与用户界面一个字都不说。
        uns = parts.get("unscheduled_tasks") or []
        if uns:
            self.emit("warning", {
                "node": self.name,
                "message": "有 %d 条 WBS 工序没有排程结果，未进入交付物" % len(uns),
                "detail": unscheduled_effect_text(uns),
            })
        ctx["plan_json"] = assemble_plan_json(ctx, parts, report=ctx.get("report") or "")
        ov = parts["overview"]
        self.done_summary = (f"计划数据已组装：总工期 {ov['total_duration_days']} 天，"
                             f"工序 {len(parts['all_tasks_schedule'])} 项")
        if uns:
            self.done_summary += "；⚠ 另有 %d 条 WBS 工序没有排程结果（未进入交付物）" % len(uns)
        return {"plan_parts": parts, "plan_json": ctx["plan_json"]}


class PlanDeliverNode(BaseNode):
    name = "deliver"
    title = "方案交付"

    def run(self, ctx):
        plan = ctx.get("plan_json") or assemble_plan_json(
            ctx, ctx.get("plan_parts") or build_parts(ctx), ctx.get("report") or "")
        plan["plan_id"] = f"plan_{getattr(self, '_run_id', 'run')}"
        # 关键路径口径的**最后一道**确定性改写：`report` 可能来自任何上游（老存档 /
        # /revise / 手工 ctx），落盘前统一过一遍 —— 判据与 `fix_critical_path_wording`
        # 相同：数字必须与计划真值（overview 的条数 / cpm_result 的天数）对得上，
        # 否则一个字都不动。交付物侧还有一道（`delivery._report_text`），
        # 因为**已经落盘的老计划**不会再经过这里。
        plan["report"] = fix_critical_path_wording(plan.get("report") or "", plan)[0]
        # 契约校验（唯一真源）
        plan = schemas.PlanJson.model_validate(plan).model_dump()
        ctx["plan_json"] = plan
        # ---- G5：产物单位清零（归一收口 + 生成期断言）----
        # ① 归一收口：把 U+33A1「U+33A1」统一成 `m²`（§5 单位贯通；只改这一个字符），
        #    并**逐处留痕**到 stderr（路径 + 归一前原文）—— 不是静默 replace。
        # ② 断言：归一之后仍不得有残留（真正用来兜住"归一漏掉的分支"）。
        # 归口：`plan_assembler.material_summary` 已于第 2 批删除（不再产出该表）；
        # 旧产物里那 238 处 `_qty_formula` 来自 `beat_configs`（W3-A）；WBS 叶子 `unit` /
        # KB 定额单位串 `工日/U+33A1` 目前仍会带出 U+33A1 —— 由这一步收口并留痕。
        _g5_fixed = normalize_cjk_compat_square_metre(plan, "plan_json")
        if _g5_fixed:
            sys.stderr.write(
                "[G5] plan_json 有 %d 处 %s 已按 §5 单位贯通归一为 m²"
                "（上游产出点如下，非静默）：\n%s\n"
                % (len(_g5_fixed), CJK_COMPAT_SQUARE_METRE_NAME,
                   "\n".join("  · %s ← %s" % (p, _hit_excerpt(t))
                             for p, t in _g5_fixed[:10])))
        assert_no_cjk_compat_square_metre(plan, "plan_json")
        path = self._save(plan)
        self.emit(EV_PLAN_FINAL, {"plan_id": plan["plan_id"], "plan": plan, "saved_path": path})
        # ⚠️ 不许写「已交付」：这一刻三轮回审门一道都还没走，定稿 Word 与看板都还没产出
        # （见 renderer.py 落盘文案处的同一条禁令）。落盘的只是**可复核的计划数据**。
        self.done_summary = f"计划数据已落盘（尚未审计）：{plan['plan_id']}"
        return {"plan_json": plan}

    @staticmethod
    def _save(plan):
        config.PLANS_DIR.mkdir(exist_ok=True)
        path = config.PLANS_DIR / f"{plan['plan_id']}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
