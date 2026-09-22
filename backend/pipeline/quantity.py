"""量级上卷 / 下拆 —— 纯函数，供"计划细度"与"排程"共用。

背景（与产品约定一致）：
  用户提供的参数可能落在 L4（具体工序）级，也可能只到 L3（工种/分部）级；
  而他想要的**计划细度**又是另一件事。四种组合都要能处理：

    参数 L4 → 计划 L4 ：最直接
    参数 L4 → 计划 L3 ：**自下而上汇总**（先按 L4 锚定额算工期，再汇总成 L3 行）
    参数 L3 → 计划 L4 ：**自上而下分解**（AI 按经验把 L3 的量拆到 L4，属 AI 假设）
    参数 L3 → 计划 L3 ：最简

  本模块只做"量的搬运与合并/拆分"，**不碰定额、不碰排程**；
  工期一律由调用方（排程器）给出，或按叶子工期聚合，并在 note 里写明算法。

术语：
  L3 行 = 一个 work_package 下、同一 L3（work_type / kb_activity_id 所属工种）的叶子合并而成
  L4 行 = 具体工序（现有的叶子）
"""

import math
import re

# 计划细度取值
LEVEL_L3 = "L3"
LEVEL_L4 = "L4"

# 由"计划细度"决定每行代表什么，写进叶子的 plan_level 字段
def leaf_l3_name(leaf):
    """取一条叶子所属的 L3 名（用于合并分组）。

    优先用 work_type（生成 WBS 时按 KB 工种写入），没有就退回空串。
    L3 的 **id** 需要查库，这里只做纯字符串层面的分组，不引入知识库依赖。
    """
    if not isinstance(leaf, dict):
        return ""
    return str(leaf.get("work_type") or "").strip()


def _num(v, default=0.0):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _schedule_span(ids, schedule):
    """一组任务在排程里的时间跨度（最晚完成 - 最早开始），即"这批活的持续时间"。

    schedule: {task_id: {"es":..,"ef":..}}；缺失时返回 None。
    """
    if not schedule:
        return None
    es = [schedule[i]["es"] for i in ids
          if i in schedule and schedule[i].get("es") is not None]
    ef = [schedule[i]["ef"] for i in ids
          if i in schedule and schedule[i].get("ef") is not None]
    if not es or not ef:
        return None
    return max(1, int(max(ef) - min(es)))


def rollup_to_l3(wbs, schedule=None, sum_unit_durations=False):
    """把 WBS 自下而上汇总到 L3 级：每个 work_package 下按 L3 名合并叶子。

    参数
    ----
    wbs      : 三层 WBS（phases → work_packages → sub_packages）
    schedule : 可选，{task_id: {"es","ef"}}；给了就用"时间跨度"当 L3 行的工期
               （这才是"L3 工期包含 L4 内容"的正确口径）
    sum_unit_durations : schedule 缺失时的退路。True=把叶子工期相加；
               False=取最大值。两者都不理想，因此会在 note 里写明用了哪种。

    返回
    ----
    (新 wbs, 汇总记录列表)
      - 每个合并行的 id 用 `父wp.id + ".L3." + 序号`，避免与 L4 行混淆
      - 行上带 `_rolled_up_from`: [原叶子 id...]，便于溯源与展开
      - 行上带 `_rollup`: {"quantity": 合计, "duration_rule": "时间跨度"|"求和"|"取最大"}
      - 汇总记录 [{l3, task_id, from: [...], quantity, duration, duration_rule}]
    """
    out_phases = []
    records = []

    for phase in (wbs or {}).get("phases", []) or []:
        new_wps = []
        for wp in phase.get("work_packages", []) or []:
            leaves = [l for l in (wp.get("sub_packages") or []) if isinstance(l, dict)]
            if not leaves:
                new_wps.append(dict(wp))
                continue

            # 按 L3 名分组（保持首次出现顺序，保证结果可复现）
            order = []
            groups = {}
            for leaf in leaves:
                key = leaf_l3_name(leaf) or "未分类"
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(leaf)

            if len(order) == 1 and len(groups[order[0]]) == len(leaves) and not leaves[0].get("_rollup"):
                # 该工作包本来就只有一类 L3 → 合并等价于原样，但仍是 L3 行
                pass

            merged = []
            for idx, key in enumerate(order, 1):
                members = groups[key]
                ids = [m.get("id") for m in members if m.get("id")]
                qty = round(sum(_num(m.get("quantity")) for m in members), 2)

                span = _schedule_span(ids, schedule)
                if span is not None:
                    dur, rule = span, "时间跨度"
                elif sum_unit_durations:
                    dur, rule = max(1, int(sum(_num(m.get("duration_days"), 1) for m in members))), "求和"
                else:
                    dur, rule = max(1, int(max(_num(m.get("duration_days"), 1) for m in members))), "取最大"

                units = [str(m.get("unit") or "") for m in members if m.get("unit")]
                unit = units[0] if units and len(set(units)) == 1 else "项"

                row = {
                    "id": "{}.L3.{}".format(wp.get("id") or "1.1", idx),
                    "name": "{}（{} 项工序汇总）".format(key, len(members)),
                    "duration_days": dur,
                    "quantity": qty,
                    "unit": unit,
                    "work_type": key,
                    "plan_level": LEVEL_L3,
                    "_rolled_up_from": ids,
                    "_rollup": {"quantity": qty, "duration_rule": rule,
                                "member_count": len(members)},
                }
                # 合并行的溯源：取成员里最保守的一份（有 ai 就标 ai）
                origins = [((m.get("provenance") or {}).get("norm") or {}).get("origin")
                           for m in members]
                if origins:
                    row.setdefault("provenance", {})["norm"] = {
                        "value": None, "origin": "ai" if "ai" in origins else (
                            "kb" if "kb" in origins else (origins[0] or "unknown")),
                        "ref": "由 {} 项 L4 汇总".format(len(members)),
                        "confidence": "低" if "ai" in origins else "中",
                        "note": "L3 汇总行的来源 = 成员中最保守者",
                    }
                merged.append(row)
                records.append({"l3": key, "task_id": row["id"], "members": ids,
                                "quantity": qty, "duration": dur, "duration_rule": rule})

            new_wps.append(dict(wp, sub_packages=merged))
        out_phases.append(dict(phase, work_packages=new_wps))

    return {"phases": out_phases}, records


def explode_to_l4(wbs, ratios=None):
    """把 L3 行下拆成 L4 行（自上而下）。

    ratios: 可选，{l3名: [(l4名, 占比, 单位), ...]}。
            未提供时**不臆造**：只把 L3 行原样标成 L3，并记一条 warning，
            由调用方决定是否让 AI 补比例（AI 补的比例属 AI 假设，须标注）。

    返回 (新 wbs, 分解记录, warnings)
    """
    out_phases = []
    records = []
    warnings = []

    for phase in (wbs or {}).get("phases", []) or []:
        new_wps = []
        for wp in phase.get("work_packages", []) or []:
            leaves = [l for l in (wp.get("sub_packages") or []) if isinstance(l, dict)]
            new_leaves = []
            for leaf in leaves:
                l3 = leaf_l3_name(leaf)
                spec = (ratios or {}).get(l3)
                if not spec:
                    new_leaves.append(dict(leaf))
                    continue
                total_qty = _num(leaf.get("quantity"))
                total_dur = max(1, int(_num(leaf.get("duration_days"), 1)))
                base_id = leaf.get("id") or "1.1.1"
                for j, item in enumerate(spec, 1):
                    name, share, unit = (list(item) + [None, None, None])[:3]
                    share = _num(share)
                    sub = {
                        "id": "{}.{}".format(base_id, j),
                        "name": "{} {}".format(leaf.get("name") or l3, name),
                        "duration_days": max(1, int(round(total_dur * share)) or 1),
                        "quantity": round(total_qty * share, 2),
                        "unit": unit or leaf.get("unit") or "项",
                        "work_type": l3,
                        "plan_level": LEVEL_L4,
                        "_exploded_from": base_id,
                        "provenance": {
                            "quantity": {"value": round(total_qty * share, 2), "origin": "ai",
                                         "ref": "按经验比例分解 L3 总量",
                                         "confidence": "低",
                                         "note": "用户只给了 L3 级参数，L4 量由 AI 按比例拆分"},
                        },
                    }
                    new_leaves.append(sub)
                    records.append({"l3": l3, "from": base_id, "to": sub["id"],
                                    "share": share, "quantity": sub["quantity"]})
                warnings.append("L3 行 {} 已按经验比例拆为 {} 条 L4（AI 假设）".format(
                    leaf.get("id"), len(spec)))
            new_wps.append(dict(wp, sub_packages=new_leaves))
        out_phases.append(dict(phase, work_packages=new_wps))

    if not ratios:
        warnings.append("未提供 L3→L4 分解比例，已保持原样（不臆造工序）")
    return {"phases": out_phases}, records, warnings


def count_rows(wbs):
    """统计计划行数（用于给用户"选细度"前的预估）。"""
    leaves = [l for ph in (wbs or {}).get("phases", []) or []
              for wp in ph.get("work_packages", []) or []
              for l in (wp.get("sub_packages") or [])]
    return {"rows": len(leaves),
            "phases": len((wbs or {}).get("phases", []) or []),
            "work_packages": sum(len(ph.get("work_packages", []) or [])
                                 for ph in (wbs or {}).get("phases", []) or [])}


def estimate_row_counts(wbs):
    """给"计划细度"门用的预估：(L3 行数, L4 行数)。

    L4 行数 = 现有叶子数；L3 行数 = 按 (work_package, L3) 去重后的组数。

    ⚠️ 这只是**旧的单维口径**，保留是为了向后兼容。真实产品口径是**两个独立维度**
    （见 `estimate_row_matrix`）：工序拆解深度 × 楼层分组。
    旧口径的毛病：它把"同一层内按工种合并"和"跨楼层合并"混成一个开关，
    而每道节拍工序的 work_type 都不同 → 层内其实合并不了任何东西，
    它只是在**合并楼层**（一栋 38 层楼：L4 190 行 vs L3 10 行）。
    """
    l4 = 0
    l3_groups = set()
    for ph in (wbs or {}).get("phases", []) or []:
        for wp in ph.get("work_packages", []) or []:
            for leaf in (wp.get("sub_packages") or []):
                l4 += 1
                l3_groups.add((wp.get("id"), leaf_l3_name(leaf) or "未分类"))
    return len(l3_groups), l4


# ==================== 展示粒度：两个**互相独立**的维度 ====================
# ① 工序拆解深度：这一行代表"一道工序"还是"一个工种的活"
# ② 楼层分组  ：这一行代表"一层"、"五层"还是"整栋"
# 二者正交：可以"工序级 × 每5层"，也可以"工种级 × 整栋"。
# **只影响展示分组，绝不改 WBS 树、不改排程、不改定额。**
DEPTH_COARSE = "coarse"          # 工种级（粗）
DEPTH_COMPONENT = "component"    # 工序级（细）
DEPTHS = (DEPTH_COMPONENT, DEPTH_COARSE)      # 细的在前（可上卷成粗的，反之不行）

FLOOR_PER_FLOOR = "per_floor"    # 按层
FLOOR_PER_5 = "per_5"            # 每 5 层一组
FLOOR_WHOLE = "whole"            # 整栋
FLOOR_GROUPINGS = (FLOOR_PER_FLOOR, FLOOR_PER_5, FLOOR_WHOLE)

DEPTH_LABELS = {DEPTH_COMPONENT: "工序级（细）", DEPTH_COARSE: "工种级（粗）"}
FLOOR_LABELS = {FLOOR_PER_FLOOR: "按层", FLOOR_PER_5: "每 5 层一组", FLOOR_WHOLE: "整栋"}

# 楼层区间写法由 layer_engine._fmt_range 产出：如 "1-1层"、"16-20层"、"1-0.5层"
# 真实数据里**两种写法都有**：节拍叶子是区间（"16-20层"），非节拍任务可能只写单层
# （"3层"）或"全楼"。所以区间与单层都要认 —— 只认区间会把单层任务误判成"分层外"。
_RE_FLOOR_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*层")
_RE_FLOOR_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*层")


def floor_bucket(leaf, grouping=FLOOR_PER_FLOOR):
    """该叶子落在哪个楼层组（返回可哈希的组名）。

    楼层信息从 `location` / `name` 里的区间或单层字样取（"Ⅰ区 16-20层" / "3层"）。
    取不到楼层（全楼平行、场地/准备/验收类）→ 归入"分层外"，**不参与**楼层分组。
    """
    if not isinstance(leaf, dict):
        return "分层外"
    text = "%s %s" % (leaf.get("location") or "", leaf.get("name") or "")
    m = _RE_FLOOR_RANGE.search(text) or _RE_FLOOR_SINGLE.search(text)
    if not m:
        return "分层外"
    if grouping == FLOOR_WHOLE:
        return "整栋"
    lo = _num(m.group(1), 1.0)
    if grouping == FLOOR_PER_5:
        idx = int((max(lo, 1.0) - 1) // 5)          # 0 基组号
        return "第 %d-%d 层" % (idx * 5 + 1, idx * 5 + 5)
    return "第 %s 层" % (_fmt_floor(lo))


def _fmt_floor(v):
    f = _num(v, 0.0)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else str(round(f, 1))


def step_of(leaf):
    """该叶子代表的那道**工序**名（节拍叶子带 `_step_name`；其余退回整名）。"""
    if not isinstance(leaf, dict):
        return ""
    return str(leaf.get("_step_name") or leaf.get("name") or leaf.get("id") or "").strip()


def group_key(leaf, depth=DEPTH_COMPONENT, grouping=FLOOR_PER_FLOOR, wp_id=None):
    """一个展示行的分组键 =（工作包, 工序或工种, 楼层组）。

    两个维度都只参与**分组**：depth 决定第二个分量是"一道工序"还是"一个工种"，
    grouping 决定第三个分量是"一层"、"五层"还是"整栋"。
    """
    if depth == DEPTH_COARSE:
        second = leaf_l3_name(leaf) or "未分类"
    else:
        second = step_of(leaf) or "未分类"
    return (wp_id, second, floor_bucket(leaf, grouping))


def estimate_rows_for(wbs, depth=DEPTH_COMPONENT, grouping=FLOOR_PER_FLOOR):
    """给定"深度 × 楼层分组"下，计划表会有多少行。"""
    seen = set()
    for ph in (wbs or {}).get("phases", []) or []:
        for wp in ph.get("work_packages", []) or []:
            for leaf in (wp.get("sub_packages") or []):
                if isinstance(leaf, dict):
                    seen.add(group_key(leaf, depth, grouping, wp.get("id")))
    return len(seen)


def estimate_row_matrix(wbs):
    """六种组合的行数矩阵：``{depth: {grouping: 行数}}``，供"计划细度"门直接展示。

    为什么要把六个数字都摆给用户：粒度是**两个正交维度**，只看一个数字
    （"选 L3 还是 L4"）根本表达不了"我要工序级、但楼层按五层归组"这种诉求。
    """
    return dict((depth, dict((g, estimate_rows_for(wbs, depth, g)) for g in FLOOR_GROUPINGS))
                for depth in DEPTHS)


def group_rows(wbs, depth=DEPTH_COMPONENT, grouping=FLOOR_PER_FLOOR, schedule=None):
    """按两个维度把叶子合并成展示行（纯函数，不改原树）。

    返回 ``[{key, work_package, 工序/工种, 楼层组, 工序数, 工程量, 单位, 工期}]``。
    工期：给了 schedule（{task_id: {es, ef}}）就用该组内任务的**时间跨度**，
    否则取组内叶子工期之和（并在 note 里写明口径 —— 不假装是排程结果）。
    """
    buckets = {}
    for ph in (wbs or {}).get("phases", []) or []:
        for wp in ph.get("work_packages", []) or []:
            for leaf in (wp.get("sub_packages") or []):
                if not isinstance(leaf, dict):
                    continue
                key = group_key(leaf, depth, grouping, wp.get("id"))
                b = buckets.setdefault(key, {
                    "key": key, "work_package": wp.get("name") or wp.get("id"),
                    "phase": ph.get("phase"),
                    "工序/工种": key[1], "楼层组": key[2],
                    "工序数": 0, "ids": [], "units": {},
                })
                b["工序数"] += 1
                b["ids"].append(str(leaf.get("id")))
                unit = str(leaf.get("unit") or "项")
                b["units"][unit] = b["units"].get(unit, 0.0) + _num(leaf.get("quantity"))
    rows = []
    for key in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        b = buckets[key]
        ids = b.pop("ids")
        units = b.pop("units")
        # 单位不一致时不硬加（别把 m³ 和 t 加成"总量"）
        unit = list(units)[0] if len(units) == 1 else "（单位不一）"
        span = _schedule_span(ids, schedule) if schedule else None
        # 这一行覆盖了哪些叶子 —— 交付物要靠它算"组内时间跨度"，
        # 用户也要能顺着这串 id 回溯到具体任务（"审得了"）。
        b["ids"] = ids
        if span is not None:
            b["工期"] = span
            b["工期口径"] = "排程时间跨度"
        else:
            b["工期"] = None
            b["工期口径"] = "未排程（无跨度可算）"
        b["工程量"] = round(sum(units.values()), 2) if len(units) == 1 else None
        b["单位"] = unit
        rows.append(b)
    return rows
