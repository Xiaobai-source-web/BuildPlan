"""WBS 代码骨架 + 逐相 KB 注入 — 重构后的 1级 阶段定义（T-10')

每个 1级 节点（阶段）由代码定死「近乎必备」的集合，避免让 LLM 现场发挥 1级 造成丢项。
每项带：
  - `hint`：该节点**特化提示**（如主体结构注意层数/流水作业；安装估算门窗/机电规模）
  - `kb`：该相对应的 KB L3 work_type 键（注入给该相 LLM 的敞口活动 + 编码）
专项措施（溶洞/爬架/季节等）**不再独立成 1级 相**：由主体 LLM 在「跨相融合」
阶段（wbs_fusion.txt）开放 2/3级 修改权限，把专项并入宿主实体阶段（host_phase_map 兜底）。
"""

import re

from .. import kb

# ---------------- 必备 1级 阶段骨架 ----------------
DEFAULT_PHASES = [
    {
        "key": "prepare",
        "phase": "施工准备",
        "hint": "含场地平整、临时设施、测量放线、技术/材料/设备准备、手续办理；管理类工作按项。",
        "kb": ["site_prep", "temp_util", "tech_prep", "material_prep", "equip_prep"],
    },
    {
        "key": "piles",
        "phase": "地基处理与桩基",
        "hint": "按项目岩土条件展开桩型与地基处理；管桩/旋挖等按延米或立方米给真实量，桩基检测按项。",
        "kb": ["pile_foundation", "earthwork"],
    },
    {
        "key": "excavate",
        "phase": "基坑支护与土方",
        "hint": "支护（内支撑/锚索/地连墙等按项目）、土方开挖与回填给真实方量；基坑监测按项。",
        "kb": ["earthwork", "scaffolding", "steel_structure"],
    },
    {
        "key": "basement",
        "phase": "地下室结构",
        "hint": "层数=地下室层数；每层按 钢筋→模板→混凝土→防水 顺序展开到工序；底板/墙柱/顶板分构件。",
        "kb": ["rebar", "formwork", "concrete", "waterproofing"],
    },
    {
        "key": "super",
        "phase": "地上主体结构",
        "hint": "按地上层数×单层量推演总量（严禁只算一层）；流水作业要分流水段；注意铝模/爬架等工法；钢筋→混凝土拆开。",
        # 第 5 批（域 4.1b）：末尾补 "scaffolding" —— 本分部有「爬架提升」措施，
        # SCAFF0004 的 work_type_id 就是 scaffolding；不补则该叶子的 L4 所属 L3
        # 不属于本分部 kb 列表，违反域 4 编号链的硬约束（验收判据 5）。
        # 只加在**末尾**：L3 工种号 = 列表 1-based 顺序，前 4 个的序号不变。
        "kb": ["rebar", "formwork", "concrete", "steel_structure", "scaffolding"],
    },
    {
        "key": "masonry",
        "phase": "二次结构与砌体",
        "hint": "ALC墙板/砌体/构造柱/拉结筋植筋；植筋的 work_type 必须是「砌筑工程」而非钢筋工程。",
        "kb": ["masonry"],
    },
    {
        "key": "mep",
        "phase": "机电安装",
        "hint": "估算门窗/机电设备规模（电、给排水、暖通、消防）；按 预留预埋→管线安装→设备安装→调试 顺序，调试按项。",
        "kb": ["electrical", "plumbing", "hvac", "fire_protection"],
    },
    {
        "key": "finish",
        "phase": "装饰装修",
        "hint": "内装修（抹灰/楼地面/墙柱面/天棚/油漆）与外檐（保温/饰面/门窗）；外墙保温须在二次结构完成后；抹灰按面积给真实量。",
        "kb": ["wall_finish", "flooring", "ceiling", "painting", "door_window", "insulation"],
    },
    {
        "key": "site",
        "phase": "室外工程",
        "hint": "室外管网/道路铺装/绿化；按面积或延米给真实量。",
        "kb": ["earthwork", "site_prep"],
    },
    {
        "key": "accept",
        "phase": "竣工验收",
        "hint": "隐蔽/分项/分部/竣工验收与资料移交、调试；验收与资料类工作按项。",
        "kb": ["sub_accept", "div_accept", "hidden_accept", "final_accept"],
    },
]

# 专项 → 宿主实体阶段 映射（跨相融合的兜底表；不再新增 1级 相）
# 每个专项是一组 (正则, 宿主阶段名, 融合要点 hint)。LLM 走 wbs_fusion.txt；此处稳定兜底。
# A7（2026-09-21 裁定「移除预制相关内容」）：原「装配式|预制|灌浆|叠合 → 地上主体结构」
# 一条**已删除**（预制混凝土 PC 口径整体移出产品；本项目按现浇编排）。
SPECIALTY_HOST_MAP = [
    (re.compile(r"溶洞|岩溶|成孔"), "地基处理与桩基",
     "针对岩溶/溶洞的填充、成孔与处理措施，按项目处治工艺给真实量，并入本阶段。"),
    (re.compile(r"爬架|整体提升|附着式"), "地上主体结构",
     "附着式升降脚手架（爬架）提升与维护，随主体结构同步，作为本阶段措施按栋/层给量。"),
    (re.compile(r"冬雨|雨季|冬季|专项方案"), "施工准备",
     "冬雨季施工的防护与专项措施，结合项目所在地气候条件，并入本阶段。"),
]


def default_phases():
    """返回必备 1级 阶段 spec 列表（顶层骨架 1级，不含 2/3级）。"""
    return [dict(p) for p in DEFAULT_PHASES]


def host_phase_map(text):
    """按项目描述关键词返回「专项 → 宿主阶段 融合要点」列表（融合兜底，不产新相）。

    返回 [{"specialty":..., "target_phase":..., "hint":...}, ...]
    """
    out, seen = [], set()
    for rx, host, hint in SPECIALTY_HOST_MAP:
        if rx.search(text or "") and host not in seen:
            seen.add(host)
            out.append({"specialty": rx.pattern, "target_phase": host, "hint": hint})
    return out


def build_phase_kb_injection(keys, scope=None):
    """为某个 1级 阶段拼「KB 活动清单」文本（含 activity_id + 单位），供该相 LLM 挂 kb_activity_id。

    遍历该相 kb 键逐一取 l4_for；KB 缺失/无活动时不抛异常，返回 None。

    scope（v2.1 新增）：传 ctx["kb_scope"] 时，会按项目结构形式**过滤**候选工序——
      1. 只保留该结构体系下可用的 L4（`l4_candidates[key]`）；
      2. 把被结构剔除的工序汇成"严禁使用"清单附在后面。
    这样剪力墙项目里就不会再冒出"柱浇筑"。
    不传 scope 时行为与旧版完全一致（向后兼容）。
    """
    lines = []
    banned = []
    absent_keys = []
    for key in keys or []:
        acts = kb.l4_for(key)
        if not acts:
            continue

        allowed_ids = None
        absent = False
        if isinstance(scope, dict):
            cand = (scope.get("l4_candidates") or {}).get(key)
            if cand:
                allowed_ids = {c.get("activity_id") for c in cand if c.get("activity_id")}
                absent = any(c.get("structure_mapping_absent") for c in cand)
            # cand 为空列表 = 该 L3 在本结构下全部被剔除（如剪力墙下的钢结构）
            elif key in (scope.get("l4_candidates") or {}):
                allowed_ids = set()

        picked = acts
        if allowed_ids is not None:
            picked = [a for a in acts if a.get("activity_id") in allowed_ids]
            banned.extend(
                f"{a.get('activity_name')}({a.get('activity_id')})"
                for a in acts if a.get("activity_id") not in allowed_ids)
        if absent:
            absent_keys.append(key)
        if not picked:
            continue
        items = " ".join(
            f"{a['activity_name']}({a['activity_id']}/{a['unit']})" for a in picked)
        lines.append(f"  [{key}]: {items}")

    if not lines:
        return None
    out = ("该阶段可挂靠的 KB 工程类型/活动（叶子任务请从下列选择最贴切的并写 kb_activity_id）：\n"
           + "\n".join(lines)
           # 第 5 批（域 3.2）：这份清单同时是 `l4_order` 的**允许候选集**。
           # 三条要求缺一不可：① 按施工先后排序（下游编号的唯一顺序来源就是它）；
           # ② 量=0 的工序也必须列（否则下游按序编号会跳号）；③ LLM 只给顺序、不给编号。
           + "\n【l4_order 必读】上表方括号里的键（如 [rebar]）就是 work_type_id。"
             "请在本相 JSON 的 `l4_order` 里把本阶段**全部**要施工的 L4 工序"
             "按**施工先后顺序**列出来（先干的在前）：\n"
             "  · 只给顺序，**不要写任何工序号/序号/id 数字**，编号由系统按下标自动分配；\n"
             "  · 同一个 kb_activity_id 只列一次（重复的只算第一次）；\n"
             "  · **即使某道工序在本工程中工程量为 0（无此部位、无此做法）也必须列出它的位置，"
             "quantity 写 0** —— 漏掉量 0 的工序会让后面的编号跳号；\n"
             "  · 每一项形如 {\"kb_activity_id\":\"...\",\"work_type_id\":\"[方括号里的键]\","
             "\"activity_name\":\"...\",\"unit\":\"...\",\"quantity\":0}。")
    if banned:
        out += ("\n【本工程结构体系下不适用，严禁使用以下工序（违反即为错误）】"
                + "、".join(dict.fromkeys(banned)))
    if absent_keys:
        out += ("\n注：{} 在结构映射表中没有数据，已按“无结构约束”处理，请按项目实际情况判断。"
                .format("、".join(absent_keys)))
    return out



def resolve_prompt(spec):
    """返回该 1级 阶段的 user 提示 = 通用口令 + 特化 hint。"""
    base = "你负责把给定的 1级 施工阶段展开为 2级 工作包与 3级 叶子任务。"
    base += "只产出这一个阶段的子树，不要跨阶段、不要管其它阶段。"
    hint = (spec or {}).get("hint")
    return base + ("\n【本阶段要点（务必遵守）】" + hint if hint else "")