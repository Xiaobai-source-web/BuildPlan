"""节点：机械配员与工作面容量（crew_bind）— 纯查库，不调用任何 LLM。

解决的问题（两条硬约束）：
1. 「无人操纵的机械」：既有的机械定额只算"需要几台机械"，没人算"谁来开"。
   本节点给每条叶子补 `norm_binding["crew"]`（机械配员），来源为 KB
   配员表 Equipment_Crew_Mapping（只覆盖 20 余种机械，拿不到就报警告、
   **绝不瞎编**）。
2. 「工作面容量」：**域 1.6（第 6 批）已整体退役**。原先本节点把 KB
   `Workface_Capacity_Rule` 的值（每活动每施工段最多几人/几台）原样搬到叶子上。
   该表已删除（kb.db 21 → 20 表），本节点**不再从 KB 搬运容量值**。
   ⚠️ 不要以为 `Resource_Workface_Index`（MWI）能顶上：它按**资源名**建键、
   量纲是 m²/人（一人所需最小工位面积），回答不了"这条活动每班最多几人"。
   容量主口径 = `segment_capacity.segment_capacity`（段面积 ÷ MWI，域 7.1/7.11）。

输入 ctx：wbs（叶子可能带 norm_binding / kb_activity_id）
输出 ctx：
- wbs（原地补字段，并同步返回）
- crew_warnings：[{task_id, machine, reason}]
- crew_stats：{machines_total, machines_with_crew}
  （域 1.6 起不再有 workface_known / workface_missing —— 容量不再由本节点从 KB 取）

补的字段（只补在与 KB 有关的叶子上，完全没锚定的老叶子一个字段都不碰）：
- leaf["norm_binding"]["crew"]      机械配员 {工种: 人数}（仅在已有 norm_binding 时写）
- leaf["norm_binding"]["crew_kind"] 每个工种的归属："machine" | "labor"
                                    （下游据此只把 machine 计入机械配员，
                                      人工工种不重复计入）
- leaf["norm_binding"]["labor_types"] 人工工种列表
- leaf["machine_crew"]              机械配员的副本（无 norm_binding 的叶子也能取到）
- leaf["crew_source"]               {origin, ref, confidence, note}
- leaf["labor_source"]              {origin, ref, confidence, note}

（域 1.6 起**不再**写 `leaf["workface_capacity"]`；叶子自带的该键由上游/用户给出，
 本节点不碰，下游 `workface_limits_from_rule` 仍会读它。）

全部降级：KB 查不到不报错、不中断（kb.py 自身即"优雅降级"约定）。
"""

import re

from .. import kb
from ..base import BaseNode

# ==================== 配员文本解析 ====================
# "司机1名+信号工1名" / "泵工1人+辅助1人" / "振捣工1人" / "司机1名"
_CREW_TOKEN_SPLIT = re.compile(r"[+＋、,，;；/\s]+")
_CREW_ITEM = re.compile(r"^([^\d]+?)(\d+(?:\.\d+)?)\s*[名人台辆部组个]?$")


def parse_crew_composition(text):
    """把配员表 composition 文本解析成 {工种: 人数}。

    "司机1名"            -> {"司机": 1}
    "泵工1人+辅助1人"     -> {"泵工": 1, "辅助": 1}
    "信号工2名、司机1名"  -> {"信号工": 2, "司机": 1}
    "若干人" / "" / None -> {}（解析不出来，调用方须保留原文并留空 crew）

    只做纯文本解析，不猜任何数字；同名工种累加。
    """
    if not text or not isinstance(text, str):
        return {}
    out = {}
    for raw in _CREW_TOKEN_SPLIT.split(text.strip()):
        token = raw.strip()
        if not token:
            continue
        m = _CREW_ITEM.match(token)
        if not m:
            continue
        role = m.group(1).strip()
        if not role:
            continue
        try:
            count = int(float(m.group(2)))
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        out[role] = out.get(role, 0) + count
    return out


# ==================== 小工具 ====================
def collect_leaf_tasks(phases):
    """收集叶子任务（口径与 resource.collect_leaf_tasks 完全一致）。

    刻意在本地保留一份实现，避免与 resource.py 互相 import。
    """
    leaf_tasks = []
    if not isinstance(phases, list):
        return leaf_tasks
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        for wp in phase.get("work_packages", []) or []:
            if not isinstance(wp, dict):
                continue
            sub = wp.get("sub_packages") or []
            if sub:
                leaf_tasks.extend([x for x in sub if isinstance(x, dict)])
            else:
                leaf_tasks.append(wp)
    return leaf_tasks


def _as_dict(obj):
    """dict 直接用；pydantic 模型转 dict；其余返回 None。"""
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return None
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                data = fn()
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    return None


_BINDING_MACHINE_KEYS = ("machine_name", "machine", "main_machine", "equipment_name")


def _binding_machine_name(binding):
    """norm_binding 里若已带机械名，优先用它。"""
    for key in _BINDING_MACHINE_KEYS:
        val = binding.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _leaf_activity_id(leaf, binding):
    for src in (leaf, binding):
        if not isinstance(src, dict):
            continue
        val = src.get("kb_activity_id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# ⚠️ 域 1.6（第 6 批）：原先这里另有 `_workface_payload(wf)` —— 把 KB
# `Workface_Capacity_Rule` 的行整理成叶子字段（max_labor / max_machine / unit_basis /
# origin / confidence / note / source_type）。该表已删除、`kb.workface_capacity()`
# 已退役，本节点不再从 KB 搬运容量值，该 helper 随之删除。
# 叶子自带的 `workface_capacity`（用户/上游显式给的）不受影响，仍参与封顶。


class CrewBindNode(BaseNode):
    name = "crew_bind"
    title = "机械配员与人工工种"

    def run(self, ctx):
        wbs = ctx.get("wbs") or {}
        phases = wbs.get("phases") if isinstance(wbs, dict) else []
        leaves = collect_leaf_tasks(phases or [])

        warnings = []
        stats = {"machines_total": 0, "machines_with_crew": 0}

        self.emit("node_progress", {"node": self.name, "progress": 10,
                                    "message": "查知识库里的机械配员与工作面容量"})

        for leaf in leaves:
            if not isinstance(leaf, dict):
                continue
            try:
                counts = self._bind_leaf(leaf, warnings)
            except Exception as exc:
                # 铁律：KB 出任何意外都只是记一条警告，绝不中断流水线
                warnings.append({
                    "task_id": leaf.get("id") or leaf.get("task_id") or "",
                    "machine": "",
                    "reason": "配员/工作面容量处理异常（已跳过该叶子）：%s" % exc,
                })
                continue
            for key in stats:
                stats[key] += counts.get(key, 0)

        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "机械与班组都配好了，正在核对配员来源"})
        self.done_summary = ("机械配员：%d/%d 台已配班组；提示 %d 条"
                             % (stats["machines_with_crew"], stats["machines_total"],
                                len(warnings)))

        # 叶子是原地修改的，这里把 wbs 一并回传以保证"写回 ctx"
        return {"wbs": wbs, "crew_warnings": warnings, "crew_stats": stats}

    def _bind_leaf(self, leaf, warnings):
        """给一条叶子补配员 / 人工工种 / 工作面容量；返回本叶子贡献的统计量。"""
        counts = {"machines_total": 0, "machines_with_crew": 0}
        binding = _as_dict(leaf.get("norm_binding"))
        if binding is not None:
            leaf["norm_binding"] = binding          # pydantic 模型 -> dict
        activity_id = _leaf_activity_id(leaf, binding)

        # 与 KB 完全无关的叶子：一个字段都不补（遗留路径零影响）
        if not binding and not activity_id:
            return counts

        task_id = leaf.get("id") or leaf.get("task_id") or ""
        machine_name = None

        # ---------- 1) 机械配员 ----------
        mode = str((binding or {}).get("mode") or "").strip().lower()
        if not mode and activity_id:
            # 没有锚定结果时，用 KB 的推荐生产模式兜底判断
            info = kb.activity_info(activity_id)
            if info and info.get("recommended_production_mode") == "equipment_driven":
                mode = "machine"
        if mode == "machine":
            if not activity_id:
                if binding is not None:
                    machine_name = _binding_machine_name(binding)
            else:
                machine_name = _binding_machine_name(binding or {})
                if not machine_name:
                    rows = kb.main_machine(activity_id, (binding or {}).get("condition_text"))
                    if rows:
                        machine_name = rows[0].get("machine_name")

            if machine_name:
                counts["machines_total"] += 1
                crew_row = kb.crew_for_machine(machine_name)
                if crew_row is None:
                    # 拿不到配员：不瞎编，警告 + crew 留空
                    warnings.append({
                        "task_id": task_id,
                        "machine": machine_name,
                        "reason": "该机械无配员数据（配员表只覆盖 20 种机械）",
                    })
                    leaf["machine_crew"] = {}
                    leaf["crew_source"] = {
                        "origin": "none",
                        "ref": "",
                        "confidence": "",
                        "note": "机械 %s 无配员数据，未估算" % machine_name,
                    }
                else:
                    comp_text = crew_row.get("composition") or ""
                    crew = parse_crew_composition(comp_text)
                    ref = crew_row.get("source_type") or ""
                    note = comp_text.strip()
                    if crew:
                        counts["machines_with_crew"] += 1
                        if binding is not None:
                            crew_dict = binding.get("crew")
                            if not isinstance(crew_dict, dict):
                                crew_dict = {}
                            kind = binding.get("crew_kind")
                            if not isinstance(kind, dict):
                                kind = {}
                            for role, cnt in crew.items():
                                crew_dict[role] = int(cnt)
                                kind[role] = "machine"
                            binding["crew"] = crew_dict
                            binding["crew_kind"] = kind
                            binding["machine_name"] = machine_name
                        leaf["machine_crew"] = dict(crew)
                        leaf["crew_source"] = {
                            "origin": "kb", "ref": ref,
                            "confidence": crew_row.get("confidence") or "",
                            "note": note or ("配员 %d 人" % (crew_row.get("crew_size") or 0)),
                        }
                    else:
                        # 有配员行但文本解析不出来：保留原文，crew 留空
                        warnings.append({
                            "task_id": task_id,
                            "machine": machine_name,
                            "reason": "配员文本无法解析：%s" % (comp_text or "（空）"),
                        })
                        leaf["machine_crew"] = {}
                        leaf["crew_source"] = {
                            "origin": "kb", "ref": ref,
                            "confidence": crew_row.get("confidence") or "",
                            "note": "配员原文（未能解析成人数）：%s" % (comp_text or "（空）"),
                        }
            else:
                warnings.append({
                    "task_id": task_id,
                    "machine": "",
                    "reason": "KB 无主控机械数据（未计入配员）",
                })

        # ---------- 2) 人工工种（只标记，不重复计入机械配员）----------
        if activity_id:
            lab = kb.labor_type_for_activity(activity_id) or {}
            labor_types = [t for t in (lab.get("labor_types") or []) if t]
            if labor_types:
                leaf["labor_types"] = labor_types
                if binding is not None:
                    binding["labor_types"] = labor_types
                    crew_dict = binding.get("crew")
                    if not isinstance(crew_dict, dict):
                        crew_dict = {}
                    kind = binding.get("crew_kind")
                    if not isinstance(kind, dict):
                        kind = {}
                    for t in labor_types:
                        # ⚠️ C8 第 10 项（2026-09-21）：原先这里还有
                        # `crew_dict.setdefault(t, 1)` —— 给每个工种**凭空补 1 个人**，
                        # 那是"资源不是来自工作面容量"的最后一个后门（实测把铝模的
                        # 13 人压成 1 人）。已删除：只有真算出来的班组才写进 `crew`。
                        kind.setdefault(t, "labor")   # 已标记为机械的不覆盖
                    binding["crew"] = crew_dict
                    binding["crew_kind"] = kind
                leaf["labor_source"] = {
                    "origin": "kb", "ref": lab.get("ref") or "",
                    "confidence": lab.get("confidence") or "",
                    "note": "人工工种：" + "、".join(labor_types),
                }

        # ---------- 3) 工作面容量：域 1.6（第 6 批）已整体退役 ----------
        # 原先是 `wf = kb.workface_capacity(activity_id)`，从 `Workface_Capacity_Rule`
        # 取「每活动每施工段最多几人/几台」写进 `leaf["workface_capacity"]`。
        # 该表已删除（kb.db 21 → 20 表），`kb.workface_capacity()` 随之退役，本节点
        # **不再**从 KB 补容量 —— 否则只会写一个恒定 None（静默降级），下游
        # `workface_limits_from_rule` / `_workface_caps` 全部拿不到上限而无声失效。
        # 容量主口径改为 `segment_capacity.segment_capacity`（段面积 ÷ MWI，域 7.1/7.11）。
        # 叶子自带 `workface_capacity` 时仍原样保留、继续参与封顶（用户显式给的说话）。

        return counts
