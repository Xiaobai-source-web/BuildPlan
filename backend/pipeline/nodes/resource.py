"""节点4：资源定额计算 — T-12（纯 Python 确定性算法）

算法直接沿用 `资源定额.txt`（已核对与 Dify 导出 YAML 内嵌代码完全一致）：
关键词匹配（13 大类别约 110 项，词长降序）→ 物理产能 / 兜底定额 →
工程量校验与推算 → 资源削峰。

v1.1 契约调整：输出侧把动态扁平键（"推土机_per_day"）适配为嵌套结构
`resources: {"推土机": {"per_day": 8, "total_days": 40}}`，
算法内部逻辑不改动，仅包一层映射。

输入 ctx：wbs、extracted_params（可选）、boundary_conditions（可选）
输出 ctx：resource_demand（含嵌套 resources）
"""

import json
import math
import re
from typing import Dict, Any, Optional, List

from .. import kb
from .. import kb_units
from .. import org_defaults
from ..base import BaseNode
from .scheduler import (quantity_scale_bounds, scale_violation, units_compatible,
                        normalize_unit as _alias_normalize_unit)


def _unit_pair_reason(binding, leaf_unit):
    """叶子单位 vs 定额分母单位 → 不可用时返回中文原因；可用时返回 None。

    判据顺序（`scheduler._build_ledger_item` 就是这么读的）：
      ① 绑定层（WS3）判过并写下的 `unit_check.verdict` / `unit_verdict` —— **唯一真源**，
         same / convertible 都算可用（convertible 时 binding 里有 convert_factor）；
      ② 老计划里没有这两个键 → 现场用 `kb_units.check_unit_pair` + 绑定里的 ctx 复核；
      ③ 复核抛异常 → 退回旧的同族判据 `units_compatible`（纯兼容兜底）。

    ⚠️ 为什么不能只看 `units_compatible`：它只认同族，把「根 → m」（桩长 18 m/根，
    verdict=convertible、factor=18）判成不一致；它内部的 `_UNIT_ALIAS` 也认不出 "t"，
    于是「吨 vs 工日/t」同样被判不一致。实测 2.1.1 预应力管桩（120 根 × 18 m，
    binding=convertible/factor 18）与 1.5.3 钢筋运输（binding=verdict same，"吨 vs t"）
    就是这样被 resource **单方面**拦掉的 —— 同一条定额 scheduler 算得出工期、
    resource 却说单位不一致，两个节点自相矛盾。
    """
    b = binding if isinstance(binding, dict) else {}
    verdict = str((b.get("unit_check") or {}).get("verdict")
                  or b.get("unit_verdict") or "").strip().lower()
    if verdict in ("same", "convertible"):
        return None
    if verdict == "unusable":
        # 老计划自救（第 41 轮）：绑定层当时判的是 unusable，但这条定额本身是
        # "按厚度分层的墙体定额"，缺的只是项目墙厚 —— 用**具名假设**补上并留痕，
        # 而不是把整条工序的班组算空（实测 18 条 ALC 墙板）。补不了（合成绑定没有
        # `condition_text` / 不是这个组合）才照抄绑定层写下的原因。
        if _assumed_unit_ctx(b, leaf_unit)[0] is not None:
            return None
        # 绑定层显式判过 → 优先照抄它写的中文原因（本轮起带"可执行下一步"）
        return (str(b.get("not_usable_reason") or "").strip() or "单位不一致")
    # ---- 以下只服务"绑定层没判过"的老计划/合成绑定 ----
    # ⚠️ 说不清就不拦 —— 这条与旧 `units_compatible` 的契约逐字一致（"任一解析不出时
    # 返回 True，宁可不拦"），也是本函数唯一允许放行的地方：
    #   · `binding` 里根本没有 `unit` 键（合成/遗留绑定：实测 `test_crew_bind` 的
    #     `{'mode','norm_value','quantity_basis','source_code','match_type','task_id'}`
    #     这种）—— 拿不到定额分母单位**不等于**"单位不一致"，判不一致会让整条任务
    #     连资源都不给（12 个用例连带失败）；
    #   · 叶子单位是空/认不出的写法（`scheduler.normalize_unit()` 返回空）—— 同上。
    # 真实的 322/322 条计划绑定都带 `unit`，所以这里只兜底、不影响真实判定。
    # 注意：绑定层**显式**判过 `unusable`（如"台班"缺分母）在上面就返回了，
    # 不会被这条兜底放行 —— 契约 §5-WS4 ④ 的"默认拒绝"仍然生效。
    if not str(leaf_unit or "").strip() or not str(b.get("unit") or "").strip():
        return None
    if not _alias_normalize_unit(leaf_unit):
        return None
    ctx = None
    for key in ("convert_ctx", "convert_context", "ctx_value", "unit_ctx", "ctx"):
        val = b.get(key)
        if isinstance(val, dict) and val:
            ctx = val
            break
    try:
        check = kb_units.check_unit_pair(leaf_unit or "", b.get("unit") or "", ctx)
        if check.get("verdict") == "same" or check.get("verdict") == "convertible":
            return None
        if check.get("verdict") == "unusable":
            # 没有绑定层原因时，用真源算出来的 detail（含"补哪个参数"）当原因，
            # 别退回笼统的"单位不一致" —— 缺口明细要能直接照做。
            return (str(b.get("not_usable_reason") or "").strip()
                    or "单位不可用：%s" % (check.get("detail") or "单位不一致"))
    except Exception:
        pass
    if units_compatible(leaf_unit, b.get("unit")):
        return None
    return (str(b.get("not_usable_reason") or "").strip()
            or "单位不一致（任务 %s vs 定额 %s）" % (leaf_unit, b.get("unit")))


def _unit_pair_ok(binding, leaf_unit):
    """`_unit_pair_reason()` 的布尔形态（保留给调用方与探针脚本）。"""
    return _unit_pair_reason(binding, leaf_unit) is None


# ==================== 具名取值：面积↔体积缺墙厚 ====================
# 背景：真实计划里 18 条「N-N层 ALC墙板安装」按 m² 计量，而 `LDT724_砌块墙` 的 6 行定额
# 全是「工日/m³」（砌块墙按体积算），换算必须有墙厚；项目参数里**没有**厚度键
# （定额条件里的「>200mm」是**适用条件**、不是项目实测墙厚）。第 40 轮的处理是判
# unusable ——「默认拒绝」没错，但后果是这 18 条**一条班组都算不出来**（资源全空）。
# 第 41 轮改为**自处理 + 明确标注**（绝不静默、绝不 1:1 硬套、绝不改工程量）：
#   · **D5 改造后（P3 已交付）的取值来源**：厚度**从定额行的适用条件**（`≤200mm`
#     之类的**厚度档位**）解析出来，`kb_units.assumed_context()` 返回的 `source`
#     是 **`norm_condition`**（定额条件档位），**不再**用全局默认 0.2 兜底；
#   · 只在"这条定额本身就是按厚度分层的墙体定额"时生效（条件里有厚度档位）；
#   · 来源/说明逐处留痕（`unit_assumption` / `ctx_source` / `coverage_reason` /
#     `provenance.note`），并写进资源输出 `_unit_assumed`：
#     换算过程 + 结果 + 量级参考（供人工判断数量级，而不是替用户改数）。
#   · `ctx_source == "ai_estimate"` 的文案**保留**，供将来真有 AI 猜测路径时用。
def _assumed_unit_ctx(binding, leaf_unit):
    """老计划自救：具名取值能否补上这条绑定的换算参数。

    返回 `(ctx, note, source)` 或 `(None, "", "")`。`source` 由
    `kb_units.assumed_context()` 给出：**`norm_condition`** = 从定额行适用条件的
    厚度档位取值（当前唯一实际路径）；`ai_estimate` = 保留给将来真正的 AI 猜测路径。

    **纯函数**（不写 binding）。没有 `condition_text` 的合成/遗留绑定一律补不了 ——
    拿不到"这条定额是按厚度分层的"这个证据就不许编，
    `test_binding_with_two_units_still_blocks_when_unconvertible` 的合成绑定因此仍被拦。
    """
    b = binding if isinstance(binding, dict) else {}
    unit_text = str(b.get("unit") or "")
    if not str(leaf_unit or "").strip() or not unit_text:
        return None, "", ""
    ctx, src, note = kb_units.assumed_context(leaf_unit, unit_text,
                                              b.get("condition_text") or "")
    if not ctx:
        return None, "", ""
    return ctx, note, (str(src or "").strip().lower() or "ai_estimate")


def _materialize_unit_assumption(binding, leaf_unit):
    """把具名假设写回 binding（换算因子 + 留痕）→ True 表示这次自救了这条绑定。

    新计划的绑定层自己就算好了（`unit_check.verdict == 'convertible'`），这里不会动它；
    只有**老计划**里已被判 `unusable` 的绑定会走到这里。写回的东西：`unit_check`
    （改判 convertible）、`convert_factor`（= 假定墙厚）、`convert_ctx`、
    `unit_assumption`（假定值 + 来源 + 说明）、`coverage_reason`；`not_usable_reason`
    清空、`norm_is_evidence`/`usable` 置真（否则同一份计划里 `usable=False` 与
    `unit_check=convertible` 自相矛盾）。
    """
    if not isinstance(binding, dict):
        return False
    verdict = str((binding.get("unit_check") or {}).get("verdict") or "").strip().lower()
    if verdict == "convertible":
        return False
    ctx, note, ctx_src = _assumed_unit_ctx(binding, leaf_unit)
    if not ctx:
        return False
    check = kb_units.check_unit_pair(leaf_unit, binding.get("unit") or "", ctx)
    if check.get("verdict") != "convertible":
        return False
    binding["unit_check"] = check
    binding["convert_ctx"] = dict(ctx)
    binding["convert_factor"] = check.get("factor")
    binding["convert_denominator"] = check.get("denominator") or ""
    # D5（P3 已交付）：取值来源**按 `kb_units.assumed_context()` 给的 `source` 分流** ——
    # 当前实际路径是 `norm_condition`（厚度来自**定额行适用条件的档位**，不是 AI 估算、
    # 也不是全局默认 0.2）；`ai_estimate` 分支保留给将来真正的 AI 猜测路径。
    binding["unit_assumption"] = dict(ctx, source=ctx_src, note=note)
    binding["ctx_source"] = binding.get("ctx_source") or ctx_src
    binding["ctx_value"] = dict(ctx)
    binding["coverage_reason"] = ("定额条件档位换算参数（非 AI 估算）"
                                 if ctx_src == "norm_condition"
                                 else "AI估算换算参数")
    binding["not_usable_reason"] = ""
    binding["norm_is_evidence"] = True
    binding["usable"] = True
    prov = binding.get("provenance")
    if isinstance(prov, dict):
        old = str(prov.get("note") or "").strip()
        if note and note not in old:
            prov["note"] = (old + "；" + note) if old else note
    return True


#: 面积类系数对照（与 `beat_configs` 的既有系数对齐；**惰性 import**，避免
#: `beat_configs` → `resource` 的循环导入）。只用于"量级参考"那句话。
_AREA_FACTOR_HINTS = (
    ("ALC_AREA_FACTOR", ("ALC", "墙板", "砌块墙", "加气", "砌块")),
    ("PLASTER_AREA_FACTOR", ("抹灰", "涂料", "腻子")),
    ("FORMWORK_AREA_FACTOR", ("模板", "铝模")),
    ("WINDOW_AREA_FACTOR", ("门窗", "窗")),
    ("FACADE_AREA_FACTOR", ("外檐", "保温", "外墙")),
)


def _area_factor_reference(ratio, task_name):
    """量与项目规模的比值能否对上项目既有的面积系数 → `（与 … 一致）`，对不上返回 ""。"""
    try:
        from . import beat_configs as _bc        # 延迟导入：beat_configs 反向 import 本模块
    except Exception:
        return ""
    for attr, keywords in _AREA_FACTOR_HINTS:
        if not any(k in str(task_name or "") for k in keywords):
            continue
        val = _positive_float(getattr(_bc, attr, None))
        if val and abs(ratio - val) <= max(0.02, val * 0.02):
            return "（与项目既有系数 %s=%g 一致）" % (attr, val)
    return ""


def _magnitude_reference(total_qty, per_task_qty, group_n, unit, task_name, params):
    """量级参考（**只加说明，绝不改数**）：每层量 × 层数 / 与建筑面积的比值。"""
    bits = []
    total_area = _positive_float((params or {}).get("total_area"))
    floors = _positive_float((params or {}).get("floors"))
    if per_task_qty and floors and abs(float(group_n) - floors) <= 1e-9:
        bits.append("%g/层 × %d 层 = %g %s" % (per_task_qty, int(floors), total_qty, unit))
    if total_area and total_qty:
        ratio = total_qty / total_area
        # 契约 §5（全链路单位写法归一）：分母不再写 `㎡`(U+33A1)，直接写它的**计量对象**
        # —— `㎡` 在这里本来就是"建筑面积"（§1 词表），写"个/㎡ 建筑面积"既违规又啰嗦。
        bits.append("≈%g %s/建筑面积%s"
                    % (ratio, unit, _area_factor_reference(ratio, task_name)))
    return "；".join(bits)


def _unit_assumed_text(binding, quantity, norm_quantity, theoretical, result_amount,
                       result_label, per_day=None, days=None, task_unit=""):
    """具名假设的**完整换算过程与结果**（写进 `demand['_unit_assumed']`）。

    刻意是一段给用户看的字符串而不是结构化字典：判据在 `unit_assumption` /
    `ctx_source` / `coverage_reason` 上，这段负责让人一眼看出量级对不对 ——
      假定值 → 换算后的量 → 对应的定额行/档位（含"为什么选这一档"） →
      算出的工日（或台班） → 假定值同源说明。
    """
    asm = binding.get("unit_assumption") if isinstance(binding.get("unit_assumption"), dict) else {}
    ctx = dict((k, v) for k, v in asm.items() if k not in ("source", "note"))
    leaf_unit = kb_units.normalize_unit(binding.get("leaf_unit") or task_unit or "") or "?"
    den = kb_units.denominator_of(binding.get("unit") or "") or "定额单位"
    nv = _positive_float(binding.get("norm_value"))
    labor_unit = "台班" if str(binding.get("mode") or "").lower() == "machine" else "工日"
    # D5（P3 已交付）：取值来自**定额行适用条件的厚度档位**（`ctx_source ==
    # "norm_condition"`），不再对用户说"AI 假定"。`ai_estimate` 分支保留旧文案。
    _ctx_src = str(binding.get("ctx_source") or "").strip().lower()
    _by_norm_cond = (_ctx_src == "norm_condition")
    bits = []
    thickness = _positive_float(ctx.get("thickness_m"))
    if thickness:
        bits.append(("按**定额行适用条件的厚度档位** %dmm 换算：%g %s × %g m = %g %s"
                     if _by_norm_cond else
                     "按 AI 假定墙厚 %dmm 换算：%g %s × %g m = %g %s")
                    % (int(round(thickness * 1000)), quantity, leaf_unit,
                       thickness, norm_quantity, den))
    else:
        bits.append(("按**定额行适用条件的换算参数** %s 换算：%g %s → %g %s"
                     if _by_norm_cond else
                     "按 AI 假定参数 %s 换算：%g %s → %g %s")
                    % (json.dumps(ctx, ensure_ascii=False), quantity, leaf_unit,
                       norm_quantity, den))
    tier = str(binding.get("norm_tier_note") or "").strip()
    if tier:
        bits.append(tier)
    else:
        bits.append("定额行 %s「%s」（%s 工日/%s）"
                    % (binding.get("norm_id") or binding.get("source_code") or "未标行号",
                       binding.get("condition_text") or "", nv, den))
    if nv:
        bits.append("%g %s × %g = %g %s（理论需求）"
                    % (norm_quantity, den, nv, theoretical, labor_unit))
    if result_amount:
        tail = ("（排程 %s 天 → %s %s/天）" % (days, per_day, labor_unit)
                if per_day and days else "")
        bits.append("按定额算出 %g %s%s" % (result_amount, labor_unit, tail))
    bits.append(("厚度来源：**定额条件档位**（`ctx_source=norm_condition`，"
                 "非 AI 估算、非全局默认值 `BLOCK_WALL_THICKNESS=0.2 m`）"
                 if _by_norm_cond else
                 "假定值同源：项目既有常量 BLOCK_WALL_THICKNESS=0.2 m（内隔墙墙厚），"
                 "与同项目「砌块墙」口径一致") if thickness
                else ("以上参数来自**定额行适用条件**，用户给出实测参数后即以其为准"
                      if _by_norm_cond else
                      "以上为 AI 假定，用户给出实测参数后即以其为准"))
    return "；".join(bits)


#: 政策变更（用户 2026-09-20）：AI 经验估算的定额**允许用来算班组**，但必须逐条标注。
#: 文案与 `pipeline/norm_defaults.py` 的 `LABEL_AI_ESTIMATE` 保持一致（那里由另一个
#: 代理改闸门；本模块只做标注，不重复定义判据）。
LABEL_AI_ESTIMATE = "AI 经验估算定额（无规范依据，待审）"


def _ai_estimate_source(source_code):
    """来源代号是否**显式标成 AI 经验估算**（`AI_ESTIMATE_V1` / `AI_*`）。

    政策变更（2026-09-20）后本函数**只用于标注来源**（写进 `_norm_applied` /
    `_resource_source`），不再是"不许算班组"的否决开关。
    """
    src = str(source_code or "").upper()
    return ("AI_ESTIMATE" in src) or src.startswith("AI_")


def _norm_evidence_reason(binding, leaf_unit):
    """与 scheduler 同一判据：这条定额算不算"有据可查"。

    返回中文原因（不可用）或 None（可用）。

    **政策变更（用户 2026-09-20）**：AI 来源（`origin=ai` / `match_type=ai` /
    `source_code=AI_*`）**不再返回拦截原因** —— AI 估算定额与真人定额**同等参与**
    算班组；放行后必须在资源行上留下可追溯的来源标注（见 `compute_norm_resources`
    写出的 `_norm_applied.ai_estimate` / `_norm_applied.source_code` 与
    `_resource_source[工种]["origin"]="ai_estimate"`）。

    仍然拦截的只有两条（一条没松）：
      · **单位不可换算** —— `_unit_pair_reason`（= 绑定层 verdict）；
      · **定额口径与任务不符** —— `method_conflict`（如「机械挖基坑土方」绑到
        "人工挖小坑"定额，看着有来源、对这条任务却没有依据）。

    为什么两边必须一致：scheduler 不用这些定额算工期、resource 却拿它们算班组时，
    会出现"工期按 WBS、班组却按另一套口径"。实测自带样例：场地平整用的 AI 产能是
    **2 ㎡/工日**（真实机械约 200~500），于是 10000㎡ ÷ 5 天 ÷ 2 = **1000 人**；
    仓库样例峰值被吹到 **5000 人**。新政策下两边都按"定额（含 AI）"算，
    风险改由**逐条标注**承担，而不是靠静默丢弃。

    单位那一条**必须**走 `_unit_pair_reason`（= 绑定层 verdict）：只用同族的
    `units_compatible` 会把"能换算"的定额误标成"单位不一致"，缺口明细就会指向
    错误的修法（本该"补桩长/补厚度"，却写成"改绑定额"）。
    """
    b = binding if isinstance(binding, dict) else {}
    if not b:
        return "无定额绑定"
    if b.get("method_conflict"):
        return "定额口径与任务不符（%s）" % str(b.get("method_conflict"))[:60]
    unit_why = _unit_pair_reason(b, leaf_unit)
    if unit_why:
        return unit_why
    return None

# ==================== KB 机械台班定额（主体结构 equipment_driven 任务） ====================
# 现有产能路径里的机械名：KB 设备任务跳过这些，改用 KB 台班定额（防重复计算泵车等）
_MACHINERY_NAMES = {"推土机", "压路机", "挖掘机", "自卸汽车", "钻机", "注浆泵",
                    "静压桩机", "成槽机", "旋挖钻机", "搅拌桩机", "泵车", "塔吊",
                    "装载机", "履带吊", "吊车"}

# 辅助小型工具（主控机械筛除，不参与每日机械投入）
_AUX_MACHINE_SUBSTR = ("振捣", "抹光", "打夯", "翻斗车", "手推车", "砂浆搅拌")


def compute_kb_machinery(activity_id, quantity, planned_days):
    """按 KB 机械台班定额计算每日机械投入。

    对活动全部机械定额行，收集每台**主控机械**的最小台班定额（对应最常用现代化
    施工方法：商品混凝土泵送 / 一、二类土，作为 AI 默认假设），
    总台班 = quantity / quantity_basis × 台班定额（**固定，不随工期漂移**），
    每日 = ⌈总台班 / planned_days⌉。

    返回 {机械名: {"per_day": int, "total_days": float}}；无主控机械返回 None
    （调用方退回现有产能路径）。
    """
    if quantity <= 0 or planned_days <= 0:
        return None
    info = kb.activity_info(activity_id)
    if not info or info.get("recommended_production_mode") != "equipment_driven":
        return None
    rows = kb.equipment_norms(activity_id)
    if not rows:
        return None

    best = {}  # machine -> [min_norm, basis]
    for row in rows:
        try:
            machines = json.loads(row.get("machine_combination_json") or "[]") or []
            norms = json.loads(row.get("machine_shift_norm_json") or "[]") or []
        except (ValueError, TypeError):
            continue
        basis = row.get("quantity_basis") or 1
        for i, m in enumerate(machines):
            if not m or any(k in m for k in _AUX_MACHINE_SUBSTR):
                continue
            try:
                norm = float(norms[i]) if i < len(norms) else 0
            except (TypeError, ValueError):
                norm = 0
            if norm <= 0:
                continue
            if m not in best or norm < best[m][0]:
                best[m] = [norm, basis]

    if not best:
        return None
    result = {}
    for m, (norm, basis) in best.items():
        total_shifts = quantity / basis * norm
        per_day = math.ceil(total_shifts / planned_days) if planned_days > 0 else 0
        if per_day < 1:
            per_day = 1
        result[m] = {"per_day": per_day, "total_days": round(total_shifts, 2)}
    return result

# ==================== 关键词→资源映射 ====================
RESOURCE_MAPPING = {
    # ----- 施工准备 -----
    "场地平整": {"resources": ["推土机", "压路机", "普工"], "work_type": "土建临建"},
    "场地清理": {"resources": ["推土机", "压路机", "普工"], "work_type": "土建临建"},
    "临时道路": {"resources": ["普工"], "work_type": "土建临建"},
    "临时水电": {"resources": ["电工", "管道工"], "work_type": "机电安装"},  # 临时水电应匹配电工和管道工，而非“土建临建”
    "临时设施": {"resources": ["普工"], "work_type": "土建临建"},
    "办公临建": {"resources": ["普工"], "work_type": "土建临建"},
    "生活临建": {"resources": ["普工"], "work_type": "土建临建"},
    "控制网": {"resources": ["测量工"], "work_type": "测量工程"},
    "定位放线": {"resources": ["测量工"], "work_type": "测量工程"},
    "施工许可证": {"resources": [], "work_type": "行政管理"},
    "管线探测": {"resources": ["普工"], "work_type": "保护措施"},
    "管线保护": {"resources": ["普工"], "work_type": "保护措施"},

    # ----- 地基处理 -----
    "袖阀管钻孔": {"resources": ["注浆泵", "普工"], "work_type": "地基处理"},
    "注浆加固": {"resources": ["注浆泵", "普工"], "work_type": "地基处理"},
    "注浆效果": {"resources": ["普工"], "work_type": "检测工程"},

    # ----- 桩基工程 -----
    "PRC管桩": {"resources": ["静压桩机", "吊车", "桩机工"], "work_type": "桩基工程"},
    "管桩静压": {"resources": ["静压桩机", "吊车", "桩机工"], "work_type": "桩基工程"},
    "截桩": {"resources": ["普工", "桩机工"], "work_type": "桩基工程"},
    "接桩": {"resources": ["普工", "桩机工"], "work_type": "桩基工程"},
    "桩基检测": {"resources": ["普工"], "work_type": "检测工程"},
    "静载试验": {"resources": ["普工"], "work_type": "检测工程"},
    "低应变": {"resources": ["普工"], "work_type": "检测工程"},

    # ----- 基坑支护 -----
    "地下连续墙": {"resources": ["成槽机", "混凝土工", "钢筋工", "履带吊"], "work_type": "支护工程"},
    "咬合桩": {"resources": ["旋挖钻机", "混凝土工", "桩机工"], "work_type": "支护工程"},
    "灌注桩": {"resources": ["旋挖钻机", "混凝土工", "桩机工"], "work_type": "支护工程"},
    "内支撑": {"resources": ["钢筋工", "混凝土工", "普工"], "work_type": "钢筋混凝土工程"},
    "锚索": {"resources": ["普工"], "work_type": "支护工程"},
    "冠梁": {"resources": ["钢筋工", "混凝土工"], "work_type": "钢筋混凝土工程"},
    "腰梁": {"resources": ["钢筋工", "混凝土工"], "work_type": "钢筋混凝土工程"},
    "基坑监测": {"resources": ["普工"], "work_type": "监测工程"},
    "水位监测": {"resources": ["普工"], "work_type": "监测工程"},
    "降水运行": {"resources": ["普工"], "work_type": "监测工程"},

    # ----- 止水工程 -----
    "水泥搅拌桩": {"resources": ["搅拌桩机", "水泥工"], "work_type": "止水工程"},
    "搅拌桩": {"resources": ["搅拌桩机", "水泥工"], "work_type": "止水工程"},
    "止水帷幕": {"resources": ["搅拌桩机", "水泥工"], "work_type": "止水工程"},
    "三轴搅拌桩": {"resources": ["搅拌桩机", "水泥工"], "work_type": "止水工程"},

    # ----- 土方工程 -----
    "土方开挖": {"resources": ["挖掘机", "自卸汽车", "普工"], "work_type": "土方工程"},
    "土方外运": {"resources": ["自卸汽车", "普工"], "work_type": "土方工程"},
    "清槽": {"resources": ["普工"], "work_type": "土方工程"},
    "回填土": {"resources": ["装载机", "压路机", "普工"], "work_type": "土方工程"},

    # ----- 地下结构 -----
    "底板钢筋": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "墙柱钢筋": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "顶板钢筋": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "梁板钢筋": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "底板模板": {"resources": ["模板工"], "work_type": "模板工程"},
    "墙柱模板": {"resources": ["模板工"], "work_type": "模板工程"},
    "顶板模板": {"resources": ["模板工"], "work_type": "模板工程"},
    "梁板模板": {"resources": ["模板工"], "work_type": "模板工程"},
    "底板混凝土": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "墙柱混凝土": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "顶板混凝土": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "梁板混凝土": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "底板防水": {"resources": ["防水工"], "work_type": "防水工程"},
    "侧墙防水": {"resources": ["防水工"], "work_type": "防水工程"},
    "顶板防水": {"resources": ["防水工"], "work_type": "防水工程"},
    "后浇带": {"resources": ["混凝土工", "普工"], "work_type": "混凝土工程"},

    # ----- 主体结构 -----
    "铝模安装": {"resources": ["模板工"], "work_type": "模板工程"},
    "铝模拆除": {"resources": ["模板工"], "work_type": "模板工程"},
    "爬架提升": {"resources": ["架子工"], "work_type": "脚手架工程"},
    "爬架组装": {"resources": ["架子工"], "work_type": "脚手架工程"},
    "爬架拆除": {"resources": ["架子工"], "work_type": "脚手架工程"},
    # ⚠️ A7（2026-09-21）：本项目的「预制构件」/「套筒灌浆」两条**预制专属**映射
    # 已删除（本项目为现浇 + 叠合板吊装口径；这两条键在本项目任务名上 0 命中）。
    # `吊装工程` / `装配式安装工` 两处**保留** —— 它们是通用工作类型/工种表，
    # 本项目「叠合板吊装」仍走这条；如需彻底退场，见 `docs\资源分型_待审表.md`。
    "主体钢筋": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "主体混凝土": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "混凝土养护": {"resources": ["普工"], "work_type": "养护工程"},

    # ----- 二次结构（砌筑工程优先） -----
    "ALC墙板": {"resources": ["安装工"], "work_type": "砌筑工程"},
    "砌筑": {"resources": ["瓦工"], "work_type": "砌筑工程"},
    "砌块": {"resources": ["瓦工"], "work_type": "砌筑工程"},
    "拉结筋": {"resources": ["瓦工"], "work_type": "砌筑工程"},
    "植筋": {"resources": ["瓦工"], "work_type": "砌筑工程"},
    "构造柱": {"resources": ["混凝土工", "普工"], "work_type": "混凝土工程"},
    "圈梁": {"resources": ["混凝土工", "普工"], "work_type": "混凝土工程"},
    "过梁": {"resources": ["混凝土工", "普工"], "work_type": "混凝土工程"},

    # ----- 机电安装 -----
    "给水管道": {"resources": ["管道工"], "work_type": "机电安装"},
    "排水管道": {"resources": ["管道工"], "work_type": "机电安装"},
    "消防管道": {"resources": ["管道工"], "work_type": "机电安装"},
    "电气管线": {"resources": ["电工"], "work_type": "电气安装"},
    "配电箱": {"resources": ["电工"], "work_type": "电气安装"},
    "通风管道": {"resources": ["通风工"], "work_type": "暖通安装"},
    "风管": {"resources": ["通风工"], "work_type": "暖通安装"},
    "空调机组": {"resources": ["通风工"], "work_type": "暖通安装"},
    "设备调试": {"resources": ["普工"], "work_type": "清理调试"},

    # ----- 装饰装修 -----
    "墙面抹灰": {"resources": ["抹灰工"], "work_type": "粗装修"},
    "地面找平": {"resources": ["泥工"], "work_type": "粗装修"},
    "外墙保温": {"resources": ["保温工"], "work_type": "保温工程"},
    "外墙涂料": {"resources": ["油漆工"], "work_type": "饰面工程"},
    "门窗框": {"resources": ["安装工", "普工"], "work_type": "门窗工程"},
    "门窗扇": {"resources": ["安装工", "普工"], "work_type": "门窗工程"},
    "精装修": {"resources": ["装修工"], "work_type": "精装修"},
    "栏杆": {"resources": ["普工"], "work_type": "栏杆工程"},

    # ----- 室外工程 -----
    "室外管网": {"resources": ["管道工"], "work_type": "室外管网"},
    "道路铺装": {"resources": ["铺装工", "普工"], "work_type": "景观工程"},
    "绿化种植": {"resources": ["绿化工"], "work_type": "景观工程"},

    # ----- 验收/管理类 -----
    "专项验收": {"resources": [], "work_type": "验收管理"},
    "竣工清理": {"resources": ["普工"], "work_type": "清理调试"},
    "资料归档": {"resources": [], "work_type": "资料管理"},
    "档案移交": {"resources": [], "work_type": "资料管理"},
    "竣工备案": {"resources": [], "work_type": "验收管理"},
    "钥匙移交": {"resources": [], "work_type": "移交管理"},
    "实体移交": {"resources": [], "work_type": "移交管理"},

    # ----- 通用 work_type 兜底 -----
    "土建临建": {"resources": ["普工"], "work_type": "土建临建"},
    "测量工程": {"resources": ["测量工"], "work_type": "测量工程"},
    "行政管理": {"resources": [], "work_type": "行政管理"},
    "地基处理": {"resources": ["注浆泵", "普工"], "work_type": "地基处理"},
    "桩基工程": {"resources": ["静压桩机", "吊车", "桩机工"], "work_type": "桩基工程"},
    "检测工程": {"resources": ["普工"], "work_type": "检测工程"},
    "支护工程": {"resources": ["钢筋工", "混凝土工", "普工"], "work_type": "支护工程"},
    "止水工程": {"resources": ["搅拌桩机", "水泥工"], "work_type": "止水工程"},
    "土方工程": {"resources": ["挖掘机", "自卸汽车", "普工"], "work_type": "土方工程"},
    "钢筋工程": {"resources": ["钢筋工"], "work_type": "钢筋工程"},
    "模板工程": {"resources": ["模板工"], "work_type": "模板工程"},
    "混凝土工程": {"resources": ["混凝土工", "泵车"], "work_type": "混凝土工程"},
    "防水工程": {"resources": ["防水工"], "work_type": "防水工程"},
    "养护工程": {"resources": ["普工"], "work_type": "养护工程"},
    "脚手架工程": {"resources": ["架子工"], "work_type": "脚手架工程"},
    "吊装工程": {"resources": ["装配式安装工", "塔吊"], "work_type": "吊装工程"},
    "灌浆工程": {"resources": ["灌浆工"], "work_type": "灌浆工程"},
    "砌筑工程": {"resources": ["瓦工"], "work_type": "砌筑工程"},
    "钢筋混凝土工程": {"resources": ["钢筋工", "混凝土工"], "work_type": "钢筋混凝土工程"},
    "机电安装": {"resources": ["管道工", "电工"], "work_type": "机电安装"},
    "电气安装": {"resources": ["电工"], "work_type": "电气安装"},
    "暖通安装": {"resources": ["通风工"], "work_type": "暖通安装"},
    "粗装修": {"resources": ["抹灰工"], "work_type": "粗装修"},
    "精装修": {"resources": ["装修工"], "work_type": "精装修"},
    "保温工程": {"resources": ["保温工"], "work_type": "保温工程"},
    "饰面工程": {"resources": ["油漆工"], "work_type": "饰面工程"},
    "门窗工程": {"resources": ["安装工", "普工"], "work_type": "门窗工程"},
    "栏杆工程": {"resources": ["普工"], "work_type": "栏杆工程"},
    "室外管网": {"resources": ["管道工"], "work_type": "室外管网"},
    "景观工程": {"resources": ["绿化工", "铺装工"], "work_type": "景观工程"},
    "清理调试": {"resources": ["普工"], "work_type": "清理调试"},
    "验收管理": {"resources": [], "work_type": "验收管理"},
    "资料管理": {"resources": [], "work_type": "资料管理"},
    "移交管理": {"resources": [], "work_type": "移交管理"},
    "保护措施": {"resources": ["普工"], "work_type": "保护措施"},
    "监测工程": {"resources": ["普工"], "work_type": "监测工程"},
}

# ==================== 物理产能库 ====================
PRODUCTIVITY = {
    "推土机": 300, "压路机": 200, "挖掘机": 500, "自卸汽车": 120,
    "钻机": 150, "注浆泵": 120, "静压桩机": 200, "成槽机": 30,
    "旋挖钻机": 80, "搅拌桩机": 200, "泵车": 80, "塔吊": 50,
    "装载机": 300, "履带吊": 50, "吊车": 50,
    "钢筋工": 1.5, "模板工": 15, "混凝土工": 20, "抹灰工": 40,
    "泥工": 35, "油漆工": 50, "保温工": 40, "装修工": 20,
    "绿化工": 100, "防水工": 50, "安装工": 15, "管道工": 50,
    "电工": 100, "通风工": 50, "瓦工": 50,   # 拉结筋植筋：50根/人/天（修正）
    "架子工": 20, "灌浆工": 50, "装配式安装工": 5,
    "桩机工": 15, "铺装工": 40, "水泥工": 50,
    "测量工": 20, "普工": 50,
}

TOWER_CRANE_PRODUCTIVITY = {
    "预制构件": 50,
    "预制叠合板": 50,
    "预制楼梯": 50,
    "预制阳台": 50,
}


# ==================== 辅助函数（与 资源定额.txt 一致） ====================
def to_obj(value, default=None):
    if value is None or value == "":
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default if default is not None else {}
    return value


def collect_leaf_tasks(phases):
    leaf_tasks = []
    if not isinstance(phases, list):
        return leaf_tasks
    for phase in phases:
        for wp in phase.get("work_packages", []):
            sub = wp.get("sub_packages", [])
            if sub:
                leaf_tasks.extend(sub)
            else:
                leaf_tasks.append(wp)
    return leaf_tasks


def normalize_unit(quantity: float, unit: str, task_name: str = "") -> tuple:
    if unit == "kg" and quantity >= 1000:
        return quantity / 1000, "吨"
    if "注浆" in task_name and unit == "kg":
        return quantity / 1000, "吨"
    return quantity, unit


def compute_resource_by_productivity(quantity, planned_days, resource_name, task_name="", original_unit=""):
    if quantity <= 0 or planned_days <= 0:
        return None

    if quantity == 1 and original_unit in ["项", "个"]:
        return None

    # ===== 核心修正：混凝土养护任务特殊处理 =====
    if "养护" in task_name and resource_name == "普工":
        return {"per_day": 2, "total_days": planned_days * 2}

    if resource_name == "塔吊":
        for keyword, prod in TOWER_CRANE_PRODUCTIVITY.items():
            if keyword in task_name:
                daily_required = quantity / planned_days
                required_count = math.ceil(daily_required / prod)
                if required_count < 1:
                    required_count = 1
                total_days = required_count * planned_days
                return {"per_day": required_count, "total_days": round(total_days, 2)}
        productivity = 100
    else:
        productivity = PRODUCTIVITY.get(resource_name)
        if not productivity or productivity <= 0:
            return None

    daily_required = quantity / planned_days
    required_count = math.ceil(daily_required / productivity)
    if required_count < 1:
        required_count = 1

    total_days = required_count * planned_days
    return {"per_day": required_count, "total_days": round(total_days, 2)}


def compute_resource_by_quota(quantity, planned_days, resource_name, quota_dict):
    per_unit = quota_dict.get(resource_name)
    if not per_unit:
        return None
    total_resource_days = quantity * per_unit
    per_day = math.ceil(total_resource_days / planned_days) if planned_days > 0 else 0
    if per_day > 0:
        return {"per_day": per_day, "total_days": round(total_resource_days, 2)}
    return None


def parse_extracted_params(params_input: Any) -> Dict:
    result = {}
    if not params_input:
        return result
    if isinstance(params_input, str):
        try:
            params_input = json.loads(params_input)
        except Exception:
            return result
    if not isinstance(params_input, dict):
        return result
    return params_input


def validate_quantity(task: Dict, params: Dict) -> tuple:
    """工程量体检：**只报缺，不替用户编数**。

    ⚠️ **B6（2026-09-21）**：原先这里有一张写死的"部位比例"表 —— 按任务名关键词
    （混凝土 / 钢筋 / ALC / 抹灰 / 土方）与项目总量相乘反推工程量，比例是
    混凝土 0.3+0.3+0.3+0.6+0.1 = **1.6（未归一）**、钢筋 0.2+0.2+0.2+0.7+0.1 = **1.4**，
    且墙柱合并成一档、完全不看结构类型。**已整条删除**（3 份真实产物上命中数为 0）。

    现在走到这个分支（单元 `quantity == 1 且 unit == "项"`）时**明确报缺 + 逐条标注**：
    返回的说明里带「警告」二字，调用方按既有 `_demand_no_norm` 机制落一条
    `usable=False` 的需求行（`not_usable_reason` / `_warning`），**不再静默编数**。
    """
    task_name = task.get("name", "")
    work_type = task.get("work_type", "")
    quantity = task.get("quantity", 0.0)
    unit = task.get("unit", "")

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        quantity = 0.0

    construction_types = [
        "土方工程", "混凝土工程", "钢筋工程", "模板工程", "防水工程",
        "桩基工程", "支护工程", "止水工程", "砌筑工程", "吊装工程",
        "灌浆工程", "保温工程", "饰面工程", "门窗工程", "景观工程",
        "室外管网", "粗装修", "精装修", "脚手架工程", "钢筋混凝土工程",
        "地基处理", "养护工程"
    ]

    if quantity == 1 and unit == "项" and work_type in construction_types:
        return quantity, unit, (
            "警告:施工类任务「%s」的工程量为『1 项』（没有可用量），且 B6 已删除写死的"
            "部位比例表 —— **不再按「项目总量 × 部位比例」替用户编工程量**；"
            "请直接给出本工序的工程量与单位" % (task_name or work_type))

    return quantity, unit, None


def get_fallback_quantity(task_name: str, extracted_params: Dict) -> Optional[float]:
    """⚠️ **B6（2026-09-21）：原函数体已整条删除，本函数恒返回 `None`。**

    原先这张表是"工序名关键词 → (项目总量参数, 写死比例)" ——
    `底板/墙柱/顶板钢筋 = total_rebar × 0.2`、`主体钢筋 × 0.7`、`二次结构钢筋 × 0.1`
    （合计 **1.4**）；`底板/墙柱/顶板混凝土 = total_concrete × 0.3`、`主体混凝土 × 0.6`、
    `二次结构混凝土 × 0.1`（合计 **1.6，未归一**）；`ALC 墙板 × 0.6`、`墙面抹灰 × 2.5`。
    它把"墙/柱"合并成一档、不看结构类型，又用同一个总量反复拆分（比例和 > 1）。

    与 `validate_quantity` 里的那套是同值副本，二者都是**兜底路径**，且 3 份真实产物
    （503 / 322 / 304 叶）上**命中数都是 0**。按父代理裁定整条删除，**绝不静默编数**：
    调用方在 `quantity <= 0` 时必须**明确报缺 + 逐条标注**（见 `ResourceNode.run`），
    而不是拿一个写死比例凑出来的数继续算班组。

    签名保持不变，便于既有调用点 / 测试引用；行为恒为"没有兜底值"。
    """
    return None


#: `boundary_conditions._source` 的取值：`"model"` = 模型按"常见做法"补的、**不是用户申报**。
_SOURCE_MODEL = "model"


def _named_items(raw):
    """把 `{名: 数量}` 与 `[{name, quantity}]` 两种形态统一成 `[(名, 数量)]`。"""
    out = []
    if isinstance(raw, dict):
        out = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                out.append((str(item["name"]), item.get("quantity", 0)))
    return out


def _model_sourced(boundary_input, *keys):
    """`_source` 是否把这几条键标成了 `"model"`（模型补齐，非用户申报）。

    键名照抄边界节点写下的那套（`boundary.boundary_sources()`）：
    `"equipment"` / `"labor.by_trade"`。判据**严格**是这三分法：
      · 标了 `"model"` → True（下游不得当限额用）；
      · 标了 `"user"`   → False（照旧纳入）；
      · **没有 `_source`**（旧计划 / 既有测试直接传 dict）→ False（保持旧行为）。
    最后一条是硬要求：大量既有用例传 `{"equipment": {...}}` 并期望生效，把"无标注"
    一律当不可信会成片打死；而真实流水线里 boundary 节点现在**总会**产出 `_source`。
    """
    src = (boundary_input or {}).get("_source")
    if not isinstance(src, dict):
        return False
    for key in keys:
        if str(src.get(key) or "").strip().lower() == _SOURCE_MODEL:
            return True
    return False


def parse_boundary_conditions(boundary_input: Any) -> Dict:
    """把边界条件里的**用户限额**收成 `boundaries`（供 `apply_peak_shaving` 削峰用）。

    ⚠️ 只有"用户自己给的"才算限额。边界节点会在 `_source` 里逐项标注来源，被标成
    `"model"` 的是模型按常见做法补齐的（实测：原文一条资源数据都没有，却补出了
    总人工峰值 120 / 钢筋工 25 / 塔吊 1 台等），拿它们削峰 = 用模型编的数当甲方要求，
    会把工期和班组一起带偏。这些项**不纳入限额**，但在
    `boundaries["ignored_model_limits"]` 里逐条留痕，绝不静默丢弃。
    """
    boundaries = {"equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
    if not boundary_input:
        return boundaries

    if isinstance(boundary_input, str):
        try:
            boundary_input = json.loads(boundary_input)
        except json.JSONDecodeError:
            return boundaries

    if not isinstance(boundary_input, dict):
        return boundaries

    ignored = []
    eq = boundary_input.get("equipment_peak") or boundary_input.get("equipment")
    if _model_sourced(boundary_input, "equipment", "equipment_peak"):
        for name, val in _named_items(eq):
            ignored.append("equipment.%s=%s" % (name, val))
        eq = None
    if isinstance(eq, dict):
        for k, v in eq.items():
            try:
                boundaries["equipment_peak"][k] = int(v)
            except (ValueError, TypeError):
                pass
    elif isinstance(eq, list):
        for item in eq:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    try:
                        boundaries["equipment_peak"][name] = int(item.get("quantity", 0))
                    except (ValueError, TypeError):
                        pass

    labor = boundary_input.get("labor_peak") or boundary_input.get("peak_manpower")
    if labor is not None:
        try:
            boundaries["labor_peak"] = int(labor)
        except (ValueError, TypeError):
            pass

    trade_src = boundary_input.get("labor")
    trade_src = trade_src.get("by_trade") if isinstance(trade_src, dict) else None
    trades = boundary_input.get("trade_peak") or trade_src
    if _model_sourced(boundary_input, "labor.by_trade", "trade_peak"):
        for name, val in _named_items(trades):
            ignored.append("labor.by_trade.%s=%s" % (name, val))
        trades = None
    if isinstance(trades, dict):
        for k, v in trades.items():
            try:
                boundaries["trade_peak"][k] = int(v)
            except (ValueError, TypeError):
                pass
    elif isinstance(trades, list):
        for item in trades:
            if isinstance(item, dict):
                name = item.get("trade") or item.get("name")
                if name:
                    try:
                        boundaries["trade_peak"][name] = int(item.get("quantity", 0))
                    except (ValueError, TypeError):
                        pass

    if ignored:
        boundaries["ignored_model_limits"] = ignored
    # 【第 2 批 · 域 7.7】把边界节点冻结好的项目级常量透传给资源层。
    # 只在输入**确实含这个键**时才加键 —— 否则"老产物 / 既有用例直接传 dict"的
    # `parse_boundary_conditions(None / 坏 JSON / "[]")` 返回值形状会变（既有测试钉着）。
    _const = boundary_input.get(org_defaults.SITE_MACHINE_CONST_KEY)
    if isinstance(_const, dict) and _const:
        boundaries[org_defaults.SITE_MACHINE_CONST_KEY] = _const
    return boundaries


def apply_peak_shaving(demand: Dict, boundaries: Dict) -> Dict:
    if not boundaries:
        return demand
    if not boundaries.get("equipment_peak") and not boundaries.get("trade_peak"):
        return demand

    original_duration = demand.get("planned_duration_days", 1)
    new_duration = original_duration
    adjusted = False
    adjustment_details = []

    for key in list(demand.keys()):
        if not key.endswith("_per_day"):
            continue

        resource_name = key.replace("_per_day", "")
        per_day = demand.get(key, 0)
        limit = None

        for eq_name, eq_limit in boundaries.get("equipment_peak", {}).items():
            if eq_name in resource_name or resource_name in eq_name:
                limit = eq_limit
                break

        if limit is None:
            for trade_name, trade_limit in boundaries.get("trade_peak", {}).items():
                if trade_name in resource_name or resource_name in trade_name:
                    limit = trade_limit
                    break

        if limit is not None and per_day > limit:
            total_key = key.replace("_per_day", "_total_days")
            total_days = demand.get(total_key, per_day * original_duration)
            needed_days = math.ceil(total_days / limit)
            if needed_days > new_duration:
                new_duration = needed_days
            demand[key] = limit
            adjusted = True
            adjustment_details.append({
                "resource": resource_name,
                "original_per_day": per_day,
                "new_per_day": limit,
                "original_duration": original_duration,
                "new_duration": needed_days
            })

    if adjusted:
        demand["planned_duration_days"] = new_duration
        demand["_original_duration"] = original_duration
        demand["_adjusted"] = True
        demand["_adjustment_details"] = adjustment_details

    return demand


# ==================== v2.2 定额路径（仅当叶子带可用 norm_binding 时启用） ====================
# 设计约束（务必保持）：
#   没有 norm_binding 的叶子走**原有遗留逻辑**，一行都不改（test_algorithm_parity.py
#   断言其输出与 资源定额.txt 逐字段一致）。定额路径只在"有定额锚定结果"时生效。
_NORM_MACHINE_KEYS = ("machine_name", "machine", "main_machine", "equipment_name")


def _positive_float(value, default=None):
    """转成正数 float；非法 / <=0 返回 default。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num <= 0 or math.isnan(num) or math.isinf(num):
        return default
    return num


def _org_positive_int(value):
    """WS6：组织层字段里的正整数（`crew_total` / `n_faces` / `crew_per_face`）。

    非正 / 非法 / bool → None（调用方必须退回旧口径，不许把 0 当成"没有班组"用）。
    """
    if isinstance(value, bool):
        return None
    num = _positive_float(value)
    if num is None:
        return None
    try:
        out = int(math.ceil(num))
    except (TypeError, ValueError, OverflowError):
        return None
    return out if out > 0 else None


def norm_binding_usable(binding) -> bool:
    """定额锚定结果是否可用：norm_value>0，或 productivity 可推出（1/norm_value）。"""
    if not isinstance(binding, dict) or not binding:
        return False
    mode = str(binding.get("mode") or "labor").strip().lower()
    norm_value = _positive_float(binding.get("norm_value"))
    productivity = _positive_float(binding.get("productivity_value"))
    if mode == "machine":
        return norm_value is not None
    return productivity is not None or norm_value is not None


def _binding_machine_name(task, binding):
    """主控机械名：norm_binding 里已有就用它，否则查 KB 主控机械表。"""
    for key in _NORM_MACHINE_KEYS:
        val = binding.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    kid = task.get("kb_activity_id")
    if kid:
        rows = kb.main_machine(str(kid), binding.get("condition_text"))
        if rows and rows[0].get("machine_name"):
            return str(rows[0]["machine_name"])
    return None


def _resolve_workface(task):
    """工作面容量：**只取叶子自带的值**（域 1.6 已删 `Workface_Capacity_Rule`）。

    与 `scheduler.resolve_workface` 必须同口径：两边一旦分叉，就会出现
    "排程按公式算的 13 人、资源表按兼容键的 10 人"这种自相矛盾。
    域 1.6（第 6 批）删表后**不再回查 KB** —— 原先的 `kb.workface_capacity(kid)`
    查的是已删的表、恒定 None；`Resource_Workface_Index`（MWI，67 行）按资源名建键、
    量纲 m²/人，无法给出"每活动每班最多几人"，不存在等价迁移。
    """
    wf = task.get("workface_capacity")
    wf = dict(wf) if isinstance(wf, dict) else {}
    return wf or None


def _workface_caps(workface, leaf=None, quantity=None, unit=None):
    """工作面容量 → ``(max_labor, max_machine, note)``。

    第 37 轮（契约 §5-WS4 ①）起**删掉了"AI 估算 → 不参与封顶"的否决语义**：
    `source_type=ai_estimate` / `confidence=LOW` 只表示置信度，容量一样参与封顶。
    旧实现把整张容量表架空，出现「基坑临边防护栏杆搭设 架子工 921 人」这种数字
    —— 那不是容量表压出来的，是"不封顶"放出来的。

    ⚠️ **第 39 轮修正**：容量必须与排程节点**同源同式**（契约 §5-WS4 ⑤）——
    `cap = clamp(base + step_n × ⌊(Q_seg − q_ref)/step_q⌋, min, max)`，
    由 `scheduler.workface_limits_from_rule` 单点提供。

    为什么必须共用：本函数原来直接读 `workface["max_labor"]`，而那是 **crew_bind
    写进叶子的旧表常数**；`scheduler` 用的是 v2 标定公式 —— 实测同一任务两套数
    （1.5.1 混凝土运输：旧表 4 人 vs v2 公式 15 人；4.1.1.1 钢筋：旧表 14 vs v2 7）。
    资源曲线读的**是本函数**，所以 v2 公式那句"随工程量变化"从来没体现在交付物上。
    老 docstring 说"优先取 scheduler 已经按标定公式算好的 leaf.workface_capacity"
    —— 但没有**任何**代码把公式值回写进叶子，所以那句话一直是空头承诺。现在
    直接调用同一个函数，不再依赖"谁记得回写"。
    """
    if not isinstance(workface, dict) or not workface:
        return None, None, ""
    cap_labor = cap_machine = None
    if leaf is not None:
        try:
            from . import scheduler as _sched          # 延迟导入：避免节点间循环导入
            cap_labor, cap_machine = _sched.workface_limits_from_rule(
                leaf, quantity, unit)
        except Exception:
            cap_labor = cap_machine = None
    # 公式算不出来（KB 无 v2 标定行 / 量纲不可换算）→ 才退回旧表静态键
    if cap_labor is None:
        cap_labor = _positive_float(workface.get("max_labor"))
    if cap_machine is None:
        cap_machine = _positive_float(workface.get("max_machine"))
    basis = workface.get("unit_basis") or "每施工段"
    if leaf is not None and (cap_labor is not None or cap_machine is not None):
        note = ("（工作面容量：按工程量与%s算出 %s 人 / %s 台；"
                "source_type=%s / confidence=%s）"
                % (basis,
                   "—" if cap_labor is None else int(cap_labor),
                   "—" if cap_machine is None else int(cap_machine),
                   workface.get("source_type") or "未标注",
                   workface.get("confidence") or "未标注"))
    else:
        note = ("（工作面容量 source_type=%s / confidence=%s，未按段工程量标定）"
                % (workface.get("source_type") or "未标注",
                   workface.get("confidence") or "未标注"))
    return cap_labor, cap_machine, note


# ==================== 契约 §1/§5：demand 的 `unit` 与 `measure_scope` ====================
# 为什么要给 demand 补这两个键（`devtools/_dev-notes/终版修改_接口冻结.md` §5）：
# WBS 叶子 304/304 有 `unit`，而落盘的 `resource_demand.tasks[*]` **0/304 有 `unit` 键** ——
# 交付物里只剩一个裸数字（2000 是什么？m² 还是 t？），下游只能去猜。`measure_scope`
# 再进一步说明**这个 m² 是哪张面积**（§1 受控词表）：建筑面积 / 风管展开面积 / 天棚面积
# 量纲相同、数量级差好几倍，光看 `m²` 分不出来。
# ⚠️ 单位一律经 `kb_units.normalize_unit` 归一（`㎡`(U+33A1) → `m²`），且**只**放在独立键上。
# 塞进 `quantity_basis` / `raw_quantity_basis` 是历史坑（见 `devtools/fix_norm_basis.py`
# 开头注释）：那会把"量的口径"和"定额的分母"搅在一起，定额值随之被放大 10~1000 倍。


def _demand_unit(task):
    """demand 的 `unit`：任务单位经 `kb_units.normalize_unit` 归一；取不到 → `''`。"""
    try:
        return kb_units.normalize_unit((task or {}).get("unit") or "")
    except Exception:                                   # noqa: BLE001 — 归一绝不阻断算量
        return ""


def _known_measure_scope(value):
    """受控词表（§1）内的 `measure_scope`；表外写法 / 空 → `''`（当"未填"，不猜）。

    与 `norm_bind._known_scope()` 同一口径：表外的写法宁可当"未填"，也不当"口径不同"
    去拦一条工序 —— 那是可修的数据问题，代价不应由这道工序承担。
    """
    try:
        scope = kb_units.normalize_measure_scope(value)
    except Exception:                                   # noqa: BLE001
        scope = "" if value is None else str(value).strip()
    return scope if scope in kb_units.MEASURE_SCOPES else ""


def _task_measure_scope(task, binding=None):
    """任务的计量对象（§1/§5）：binding（WS1 口径关已算过的）→ 叶子 → 所绑活动。

    顺序刻意与 `norm_bind._task_measure_scope()` 一致：`binding["task_measure_scope"]`
    就是 WS1 按"叶子显式声明 → 任务文本 → 活动自身"三档算出来的那个值，**单一真源**，
    资源层不再自己重算一遍（重算就是第二套口径，早晚分叉）。
    """
    b = binding if isinstance(binding, dict) else {}
    for cand in (b.get("task_measure_scope"), (task or {}).get("measure_scope")):
        scope = _known_measure_scope(cand)
        if scope:
            return scope
    kid = (task or {}).get("kb_activity_id")
    if kid:
        try:
            return _known_measure_scope(kb.activity_measure_scope(str(kid)))
        except Exception:                               # noqa: BLE001
            return ""
    return ""


def _demand_meta(task, binding=None):
    """`(unit, measure_scope)` —— demand 的两个新键（§5），各条产出路径共用同一口径。"""
    return _demand_unit(task), _task_measure_scope(task, binding)


# ==================== 域 7.11：`L4_Activity_Dictionary` 的只读取数 ====================
# 判据（父代理 2026-09-21 冻结，**不许自创**）：
#     第一判据 = `L4_Activity_Dictionary.is_l5_expandable == 0`
#     第二判据 = 树内叶子没有 `segment_id`（见 `scheduler.plan_organization` 的调用点）
# `kb` 只暴露了"按活动取一行"（`kb.activity_info` / `kb.activity_measure_scope`），
# **没有** `is_l5_expandable` 的公开入口 —— `kb.py` 本批被冻结不许改，故这里直读表
# （只读 + 进程级缓存），写法与 `_crew_max_values` 同一条既有约定（见其 docstring 的
# 跨流请求：请 WS1 补公开入口）。DB 不可用 / 表缺列 / 任何异常 → `None`（**不猜**，
# 由调用方按"未知"处理），绝不阻断算量。
_L4_ACTIVITY_CACHE = {}


def _l4_activity_cell(activity_id, column):
    """`L4_Activity_Dictionary` 单格只读；取不到 → `None`（**不猜**，绝不抛异常）。"""
    key = (str(activity_id or ""), str(column or ""))
    if not key[0] or not key[1]:
        return None
    if key in _L4_ACTIVITY_CACHE:
        return _L4_ACTIVITY_CACHE[key]
    val = None
    try:
        rows = kb._query_all(                            # noqa: SLF001 — 见上方注释
            "SELECT %s FROM L4_Activity_Dictionary WHERE activity_id = ?" % key[1],
            (key[0],))
        if rows:
            row = rows[0]
            raw = row.get(key[1]) if isinstance(row, dict) else row[0]
            val = None if raw is None else raw
    except Exception:                                    # noqa: BLE001
        val = None
    _L4_ACTIVITY_CACHE[key] = val
    return val


def activity_l5_expandable(activity_id):
    """域 7.11 **第一判据**：该 L4 活动是否可展开（`is_l5_expandable`）。

    返回 `1` / `0` 的 `int`；取不到（无 id / DB 不可用 / 列缺失 / 值非数字）→ `None`
    —— `None` = **未知**，调用方**必须按"可展开"处理**（`org_plan.face_area_for_activity`
    的 `expandable is None` 分支：不判"不展开"、沿用可分层实体工程口径）。

    ⚠️ 实测全库 493 行**都是 `0`**（父代理裁决 C：域 3 落地后需回看 7.11）。
    """
    raw = _l4_activity_cell(activity_id, "is_l5_expandable")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def activity_measure_scope_of_l4(activity_id):
    """域 7.11 面积口径的**唯一真源**：该 L4 自己的 `measure_scope`（原样，未过词表）。

    ⚠️ **不要**用 `_task_measure_scope()` 代替它：那条链会经 `_known_measure_scope`
    过一遍 `kb_units.MEASURE_SCOPES` 词表、且优先取叶子/绑定的声明 —— 而 7.11 的
    面积口径裁决（§14.2 裁决 2）要求"依据该**活动**的 `measure_scope`"。
    取不到 → `''`（= "未填"，回退层面积合计并留痕）。
    """
    return kb.measure_scope_of_row([_l4_activity_cell(activity_id, "measure_scope")], 0)


# ============ 契约 §5 + 跨流 D4 的唯一出口：所有 demand 都从下面两个底板长出来 ============
# 教训（父代理的确定性重放，2026-09-20）：`compute_flat` 里原本有 **6 处**各写各的
# `demand = {...}` —— 我第一轮只补了其中 3 处，重组装出来的 304 行里就有 40 行漏掉
# `unit`/`measure_scope`（全是"带绑定但不可用"的 legacy 行）。所以现在收敛成一条路：
# 任何构造 per-task demand 的分支都必须调用 `_demand_base` / `_demand_no_norm`。
_DEMAND_UNSET = object()


def _has_pipeline_binding(task):
    """这条叶子是否带**绑定节点产出的** `norm_binding`（= 契约 §5/D4 的覆盖边界）。

    判据用 `leaf_unit` / `norm_is_evidence` 这两个绑定节点必写的留痕键，而不是"有没有
    `norm_binding`"：`docs/资源定额.txt` 是算法原始稿，`test_algorithm_parity.py` 与
    `test_crew_bind.py` 用**手写的畸形 binding**（只有 `mode`/`norm_value` 两三个键）
    锁死了 legacy 行的逐字段形状（含"不新增任何顶层键"）。手写畸形 binding 不是流水线
    产物，它的"没有定额"由绑定侧自己的用例负责；给它硬塞 D4 的键会毁掉那把锁。

    真实流水线里 304/304 叶子都带这两个留痕键（重放实测），所以产物侧覆盖率仍是 100%。

    ⚠️ 若 WS1 把 `leaf_unit` / `norm_is_evidence` 从 binding 里去掉，这条判据会失效
    （新键停止下发）—— 已在 `devtools/_dev-notes/终版修改_跨流请求.md` 记一行。
    """
    b = (task or {}).get("norm_binding")
    return isinstance(b, dict) and bool(b) and ("leaf_unit" in b or "norm_is_evidence" in b)


def _demand_base(task, quantity, planned_days, binding=None, *, force_meta=False, **extra):
    """**唯一**的 per-task demand 底板 —— 不许再有任何一处手写 demand dict。

    键序：`task_id` / `task_name` / `quantity` / [`unit` / `measure_scope`] /
    `planned_duration_days` / `extra...`。

    - `unit`/`measure_scope`（契约 §5）：`force_meta=True` 或 `_has_pipeline_binding()` 为真时写入；
      冻结的参考实现区域（无绑定节点产出的 binding）保持逐字段不变。
    - 定额路径自己稍后写完整的 `_norm_applied`，所以这里不管它（见 `_demand_no_norm` 的反面）。
    """
    tid = task.get("id") or task.get("task_id") or ""
    if binding is None and isinstance(task.get("norm_binding"), dict):
        # 口径（§1/§5）与叶子是同一个真源：调用方没显式传 binding 时也取叶子上的那份，
        # 保证 6 条分支拿到的 `measure_scope` 完全同源（`_task_measure_scope` 自己会兜底）。
        binding = task.get("norm_binding")
    base = {
        "task_id": tid,
        "task_name": task.get("name") or task.get("task_name") or tid,
        "quantity": quantity,
    }
    if force_meta or _has_pipeline_binding(task):
        base["unit"], base["measure_scope"] = _demand_meta(task, binding)
    base["planned_duration_days"] = planned_days
    base.update(extra)
    return base


def _demand_no_norm(task, quantity, planned_days, binding=None, *, force_meta=False, **extra):
    """**无定额**行（定额不可用 / 早退 / 遗留路径）的底板 = `_demand_base` + 显式 `_norm_applied: None`。

    跨流要求（WS3 的 D4「无定额暴露」按 `_norm_applied` 是否为空统计）：**键必须存在** ——
    "键不存在"与"键为 None"在这类统计里含义不同，容易埋坑。值写 `None` 而不是
    `{"state": "no_norm"}`：delivery 判据一律用真值（`rd.get("_norm_applied")`），
    dict 会被当成"有定额"，反而把 D4 的口径搞错。

    覆盖范围同 `_demand_base`（冻结的参考实现区域保持逐字段不变）。
    """
    gated = force_meta or _has_pipeline_binding(task)
    base = _demand_base(task, quantity, planned_days, binding, force_meta=gated, **extra)
    if gated:
        base["_norm_applied"] = None
    return base


# ==================== 契约 §7：无定额工序的"防荒谬"上限（域 1.6 起退役）====================
# 域 1.6（第 6 批）删除了 `Workface_Capacity_Rule` 表 —— 它是"同族 crew_max"的**唯一**
# 数据源，因此 `_crew_max_values` 恒返回空列表、`_family_crew_ceiling` 恒返回 None
# （= 不封顶）。**这是经用户裁定的能力退役，不是缺陷**：`Resource_Workface_Index`
# （MWI，67 行）按**资源名**建键、量纲是 m²/人，无法回答"某活动每班最多几人"，
# 不存在等价迁移，因此不再另立 MWI 版防荒谬上限。
_FAMILY_CREW_MAX_CACHE = {}


def _l3_of(task, binding=None):
    """本任务的 `work_type_l3`（§7 "同族"的键）。

    优先级与 `scheduler.plan_organization` 的取法保持一致（`rule.work_type_l3` →
    叶子 `work_type`），中间补两级 KB 查询：容量标定行不在时退 `L4_Activity_Dictionary`
    的 L3（`kb.l3_of_activity`），仍拿不到才用叶子上的中文 `work_type` 当候选键
    （它多半不是 L3 键，`_family_crew_ceiling` 会据此退到全表上限）。
    """
    t = task if isinstance(task, dict) else {}
    b = binding if isinstance(binding, dict) else {}
    for src in (_resolve_workface(t) or {}, b):
        cand = str(src.get("work_type_l3") or "").strip()
        if cand:
            return cand
    kid = t.get("kb_activity_id")
    if kid:
        try:
            l3 = kb.l3_of_activity(str(kid))
        except Exception:                               # noqa: BLE001
            l3 = None
        if l3:
            return str(l3).strip()
    return str(t.get("work_type") or "").strip()


def _crew_max_values(work_type_l3):
    """某 L3 的 `crew_max` 列表（域 1.6 已删 Workface_Capacity_Rule，返回空列表）。

    进程级缓存；表缺失 / DB 不可用 / 任何异常 → 空列表（= 不封顶，绝不阻断算量）。
    """
    key = str(work_type_l3 or "")
    if key in _FAMILY_CREW_MAX_CACHE:
        return _FAMILY_CREW_MAX_CACHE[key]
    # 域 1.6：表已删除，不再查询
    _FAMILY_CREW_MAX_CACHE[key] = []
    return []


def _family_crew_ceiling(task, binding=None):
    """同族 `crew_max` 的**偏大一档**（域 1.6 已删表，恒返回 None）。

    域 1.6 删除了 Workface_Capacity_Rule 表，`_crew_max_values` 恒返回空列表，
    因此本函数恒返回 None（= 不封顶）。
    """
    vals = _crew_max_values(_l3_of(task, binding))
    if not vals:
        vals = _crew_max_values("")
    return max(vals) if vals else None


def _is_no_norm_basis(binding):
    """§7 的"无定额依据"判据：`match_type in ('ai','unbound')`（binding 为空也算）。"""
    b = binding if isinstance(binding, dict) else {}
    if not b:
        return True
    return str(b.get("match_type") or "ai").strip().lower() in ("ai", "unbound")


def _is_known_worker(role):
    """role 是否是"工人/工种"名（产能表里有、且不是机械名）。

    用于兜底区分 norm_binding.crew 里没打标记的项：
    司机 / 泵工 / 操作工 / 信号工 这类不在产能表里 → 按机械配员算；
    钢筋工 / 普工 这类是工种 → 不当机械配员（避免覆盖按定额算出的人工数）。
    """
    return role in PRODUCTIVITY and role not in _MACHINERY_NAMES


def _labor_hint(task, binding):
    """该叶子已知的工人工种集合（crew 分类时的白名单）。"""
    names = set()
    for src in (binding, task):
        if not isinstance(src, dict):
            continue
        vals = src.get("labor_types")
        if isinstance(vals, list):
            for v in vals:
                if v:
                    names.add(str(v))
    kid = None
    for src in (task, binding):
        if isinstance(src, dict) and src.get("kb_activity_id"):
            kid = str(src["kb_activity_id"])
            break
    if kid:
        lab = kb.labor_type_for_activity(kid) or {}
        for v in (lab.get("labor_types") or []):
            if v:
                names.add(str(v))
    return names


def _split_crew(crew, kinds, labor_hint=None):
    """把 norm_binding 的 crew 拆成 (机械配员 {工种:人数}, 人工工种 [..])。

    判定顺序：
    1) crew_kind[role] 明确写了 "machine"/"labor" → 听它的（crew_bind 的约定）；
    2) 没标记：是已知工种（labor_types / KB 工种 / 产能表里的工人）→ 人工；
    3) 其余（司机 / 泵工 / 操作工…）→ 机械配员。
    人工项**不重复计入机械配员**。
    也兼容 {工种: {"count": 1, "kind": "labor"}} 的写法。
    """
    machine_roles = {}
    labor_roles = []
    if not isinstance(crew, dict):
        return machine_roles, labor_roles
    hint = labor_hint or set()
    for role, raw in crew.items():
        if not role:
            continue
        role = str(role)
        kind = kinds.get(role) if isinstance(kinds, dict) else None
        value = raw
        if isinstance(raw, dict):
            kind = raw.get("kind") or kind
            value = raw.get("count", raw.get("size", 0))
        if kind is None:
            kind = "labor" if (role in hint or _is_known_worker(role)) else "machine"
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        if str(kind).strip().lower() == "labor":
            labor_roles.append(role)
        else:
            machine_roles[role] = count
    return machine_roles, labor_roles


def _labor_resource_name(task, binding, labor_roles):
    """人工资源名：优先 crew 里标记为人工的工种，再 labor_types，再查 KB，最后普工。"""
    for role in labor_roles:
        if role:
            return role
    for key in ("labor_types",):
        for src in (binding, task):
            vals = src.get(key) if isinstance(src, dict) else None
            if isinstance(vals, list):
                for v in vals:
                    if v:
                        return str(v)
    kid = task.get("kb_activity_id")
    if kid:
        lab = kb.labor_type_for_activity(str(kid)) or {}
        for v in (lab.get("labor_types") or []):
            if v:
                return str(v)
    return "普工"


def _crew_provenance(task):
    """机械配员的来源（crew_bind 写在叶子的 crew_source 上）。"""
    cs = task.get("crew_source")
    if isinstance(cs, dict) and cs.get("ref"):
        origin = str(cs.get("origin") or "kb").strip().lower()
        if origin not in ("kb", "ai"):
            origin = "ai"
        return {"origin": origin, "ref": str(cs.get("ref"))}
    return {"origin": "ai", "ref": ""}


def compute_norm_resources(task, binding, quantity, planned_days, org=None):
    """定额路径：按 KB 定额算每日资源，不用任何硬编码产能表。

    - mode == "machine"：每日台数 = ⌈工程量 / quantity_basis × 台班定额 / 计划工期⌉
    - mode == "labor"  ：每人每天产量 P（productivity_value，缺则 1/norm_value），
                         每日人数 = max(1, ⌈(工程量 / 计划工期) / P⌉)
    - 机械配员（crew 里标记为机械的）按台数乘进去，一并进 resources
    - 工作面容量封顶：人数 ≤ max_labor、台数 ≤ max_machine，封顶写进 _workface_capped
    - 每个资源项标注来源：任务里的 _resource_source

    `org`（第 44 轮 WS6）：排程行上的 `_organization`（施工组织层结果，可缺省）。
    **给定且 `crew_total` 是正整数时，本工种的班组以组织层为唯一真源**：组织层规划的
    是**跨作业面的总班组**（N 面 × c 人/面），而旧表 `Workface_Capacity_Rule.max_labor`
    （域 1.6 已删除）的口径是**每施工段**（单面）—— 把单面上限套到总数上会把班组压小（实测钢筋绑扎
    38 人 → 8 人/天，并因此写出"仍缺 168 工日"这种不实自述：2 个面同时干，工日一点没缺）。
    故：有组织层时 `per_day = crew_total`；保险上限用 `单面上限 × n_faces`，并在
    `_workface_capped` 里把 `unit_basis` 如实写成"每施工段上限 × N 个作业面"。
    `org=None`（或没有 `crew_total`）→ 逐位走旧路径。

    定额不可用 / 工程量非法时返回 None（调用方退回遗留路径）。
    """
    if not norm_binding_usable(binding):
        return None
    if quantity <= 0 or planned_days <= 0:
        return None

    mode = str(binding.get("mode") or "labor").strip().lower()
    basis = _positive_float(binding.get("quantity_basis"), 1.0)
    norm_value = _positive_float(binding.get("norm_value"))
    productivity = _positive_float(binding.get("productivity_value"))
    # 跨族换算（契约 §5-WS4 ④）：`convert_factor` = 把**叶子单位的量**换成**定额分母
    # 单位的量**的乘数（叶子「根」、桩长 18 m/根 → factor=18）。定额值是对分母单位而言的
    # （如 0.53 台班/100m），不换算就直接拿 120 根去除 → 少算 18 倍台班。
    # `same` 时绑定层写的是 1.0；老计划没有这个键 → 同样按 1.0（等于不换算，与旧行为一致）。
    conv_factor = _positive_float(binding.get("convert_factor"), 1.0)
    norm_quantity = quantity * conv_factor
    if productivity is None and norm_value is not None:
        # 人工产能（单位/工日）= 1 ÷ 定额值 —— **不乘 basis**。
        # labor_norm_value 落库时已归一成「工日 / 1×quantity_unit」（留档不变式
        # raw_value / raw_quantity_basis == labor_norm_value），basis 只作溯源。
        # 旧实现写成 basis/norm_value：basis ∈ {10,100,1000} 时把工日放大 10~1000 倍、
        # 产能放大同样的倍数（实测 FORM_NEW_OTHER basis=10、定额 0.25 工日/10m²：
        # 真产能 40 m²/工日，旧口径算成 4 m²/工日）。
        # ⚠️ 机械侧口径**相反**：台班定额没有归一，下面 machine 分支的
        # `quantity / basis * norm_value` 必须乘 basis，别跟着这段一起改。
        productivity = 1.0 / norm_value

    source_code = str(binding.get("source_code") or "")
    match_type = str(binding.get("match_type") or "ai").strip().lower()
    kid = task.get("kb_activity_id")
    ref = source_code or (str(kid) if kid else "")
    origin = "kb" if (source_code and match_type != "ai") else "ai"
    # ---- 政策变更（用户 2026-09-20）：AI 经验估算定额放行，但必须逐条标注 ----
    # 旧口径：AI 来源在 `_norm_evidence_reason()` 里被整条拦下（`continue`，
    # 这条任务一个班组都不给）。新口径：照算，但资源行上必须留下可追溯的来源。
    # 标注全部落在**已有**的键上（`_resource_source` / `_norm_applied`），
    # 交付物层无需改代码即可显示（`delivery._source_refs()` 读 ref → "AI_ESTIMATE_V1"，
    # 那里 637 行已把它翻译成「AI 经验估算」）。
    ai_estimate = _ai_estimate_source(source_code)
    # `origin` 仍保留 kb/ai 两档（下游按它判断"是不是精确命中定额行"），
    # 但 AI 来源单列成 `ai_estimate`，让"这条班组是 AI 拍的"在产物里一眼可见。
    res_origin = "ai_estimate" if ai_estimate else origin

    task_id = task.get("id") or task.get("task_id") or ""
    task_name = task.get("name") or task.get("task_name") or task_id
    # 契约 §5：单位与计量对象随 demand 一起下发（原先只放 `quantity`，交付物里只剩裸数字）。
    # 唯一底板（§5 + D4）：这行的 `_norm_applied` 稍后由本函数自己写完整字典。
    demand = _demand_base(task, quantity, planned_days, binding, force_meta=True)

    workface = _resolve_workface(task) or {}
    unit_basis = workface.get("unit_basis") or "每施工段"
    # 工作面容量：与排程节点同一个公式（第 39 轮；见 _workface_caps 的说明）
    max_labor, max_machine, wf_note = _workface_caps(
        workface, leaf=task, quantity=quantity, unit=(task.get("unit") or ""))
    machine_roles, labor_roles = _split_crew(binding.get("crew"), binding.get("crew_kind"),
                                             _labor_hint(task, binding))
    source_map = {}
    capped = []
    # 实际投入的**人工**班组（不含机械本身）。下游 plan_assembler 会把它回写进
    # leaf.norm_binding["crew"]，让"计划里的班组"只剩一个真源 —— 否则 /sources 读
    # norm_binding.crew、交付物读 assigned_resources，两者会长期互相矛盾。
    labor_crew = {}
    # 具名假设（第 41 轮）的标注用中间量：理论需求 / 算出的量 / 每日量
    _theoretical = None
    _result_amount = None
    _result_per_day = None

    if mode == "machine" and norm_value is not None:
        # 总台班（固定，不随工期漂移）= 工程量 × 台班定额 ÷ 台班定额的基准量。
        # ⚠️ 与人工侧口径分岔：台班定额**没有归一**（still「台班 / basis×quantity_unit」），
        # 所以 basis 必须留在分母上；人工侧的产能是 1/norm_value、**不乘 basis**。
        total_shifts = (norm_quantity * norm_value) / basis
        per_day = int(math.ceil(total_shifts / planned_days))
        if per_day < 1:
            per_day = 1
        _theoretical = round(total_shifts, 2)
        _result_amount = round(total_shifts, 2)
        _result_per_day = per_day
        machine_name = _binding_machine_name(task, binding) or "主控机械"
        if max_machine is not None and per_day > max_machine:
            capped.append({
                "resource": machine_name,
                "kind": "machine",
                "original_per_day": per_day,
                "capped_per_day": int(max_machine),
                "unit_basis": unit_basis,
                # 文案必须与实际行为一致：本节点只调整"投入多少台"，
                # **不会**改 planned_days（工期由排程节点定）。
                "reason": "工作面容量封顶：%s最多 %d 台，已由 %d 台压到 %d 台；"
                          "总台班不变，故本任务需更多日历天完成"
                          "（本节点只调投入量，不改计划工期）"
                          % (unit_basis, int(max_machine), per_day, int(max_machine)),
            })
            per_day = int(max_machine)
        demand[machine_name + "_per_day"] = per_day
        demand[machine_name + "_total_days"] = round(total_shifts, 2)
        source_map[machine_name] = {"origin": res_origin, "ref": ref}
        for role, count in machine_roles.items():           # 配员随台数走
            demand[role + "_per_day"] = per_day * count
            demand[role + "_total_days"] = round(count * total_shifts, 2)
            source_map[role] = _crew_provenance(task)
            labor_crew[role] = per_day * count
    else:
        if productivity is None:
            return None
        needed_person_days = int(math.ceil((norm_quantity / planned_days) / productivity)) * planned_days
        daily = int(math.ceil((norm_quantity / planned_days) / productivity))
        if daily < 1:
            daily = 1
        _theoretical = round(norm_quantity * norm_value, 2) if norm_value else None
        _result_amount = needed_person_days
        _result_per_day = daily
        name = _labor_resource_name(task, binding, labor_roles)
        # ---- WS6 施工组织层：本工种的班组以组织层为唯一真源（见函数 docstring）----
        # 组织层规划的是**跨作业面的总班组**（N 面 × c 人/面）；`max_labor` 的口径是
        # **每施工段**（单面）。两者不是一回事：
        #   · `cap_total = 单面上限 × N 面` —— 这道保险的口径与总数一致，可用；
        #   · 直接把单面上限套到总数上 —— **不许**（实测把 38 人压成 8 人，并自述
        #     "仍缺 168 工日"，而 2 个面同时干、工日一点没缺，属于不实信息）。
        _org_faces = _org_positive_int((org or {}).get("n_faces")) or 1
        _org_crew = _org_positive_int((org or {}).get("crew_total"))
        _org_face_crew = _org_positive_int((org or {}).get("crew_per_face"))
        # ⚠️ 这里**不再**算 `max_labor × n_faces` 那套"资源层合计上限"（契约 §6 单源）：
        # 留着它就会在 `_organization_crew` 里出现第二个更小的上限，与组织层打架。
        # ---- 契约 §7：无定额 + 无组织层 → 同族 `crew_max` 偏大一档兜底（防荒谬）----
        # 只治"一个上限都没有"的那批（无 kb_activity_id → 无 capacity 标定行）；
        # 已有按段上限时取 min（只紧不松，绝不把既有封顶放松）。
        _ai_ceiling = None
        _ai_crew_capped = False
        _ai_crew_ref = ""
        if not _org_crew and _is_no_norm_basis(binding):
            _ai_ceiling = _family_crew_ceiling(task, binding)
        if _org_crew:
            # 组织层已按"节拍 + 作业面数"校验过每面人数（`scheduler.org_crew_ceiling`
            # = 组织口径上限 ∩ 用户工种限额），**资源层不再二次封顶**：
            # 两套"每面上限"本来就不同源（资源层是 `workface_limits_from_rule` 的按段
            # 公式，组织层是 `effective_crew_max`），实测 5.1.1.1：资源层单面 8 人 ×
            # 2 面 = 16 < 组织层的 38 人 —— 拿它当保险就等于继续用单面口径否掉组织层，
            # 并且会写出"仍缺 112 工日"这种与组织层自相矛盾的说明。
            daily = _org_crew
            _crew_src = str((org or {}).get("crew_source") or "").strip()
            # WS4 新增的留痕键（`org_plan._result()`）：曲线出处与**曲线本身**的单面上限。
            # 有就原样带上（产物可逐条对账），没有（老计划）不加键、不编值。
            _org_src_ref = str((org or {}).get("crew_source_ref") or "").strip()
            _org_curve_ceiling = (org or {}).get("curve_ceiling")
            # 留痕（绝不静默）：这个班组是组织层给的，不是工作面容量压出来的。
            source_map[name] = {
                "origin": "org_layer",
                "ref": "施工组织层：%s 面 × %s 人/面 = %d 人（每面上限由组织层工种曲线定%s）"
                       % (_org_faces, _org_face_crew or "?", daily,
                          "，crew_source=%s" % _crew_src if _crew_src else "（单源）"),
            }
            # 契约 §6（用户裁定：认大的那版 = 组织层工种曲线）：**每面人数上限单源**。
            # 旧实现把 `int(max_labor)` 放在这里（抹灰算出 20 与组织层 30 打架），
            # 并在 `_workface_note` 写"以组织层为准、资源层不再封顶"—— 一个班里两句话。
            # 现在：每面上限与总数都取组织层，资源层不再产出第二个更小的上限。
            demand["_organization_crew"] = {
                "trade": name,
                "crew_total": daily,
                "n_faces": _org_faces,
                "crew_per_face": _org_face_crew,
                # §6 单源：每面人数上限 = 组织层工种曲线（`org.crew_per_face`）。
                "cap_per_face": _org_face_crew,
                "cap_total": daily,
                "cap_source": _crew_src or "org_layer",
                # 保留键（§6）：资源层**没有**再按本段工程量封顶 → 正常恒为 False。
                "resource_cap_below_org": False,
                "capped": False,
                "basis": "总（跨 %d 个作业面）" % _org_faces,
            }
            if _org_src_ref:
                demand["_organization_crew"]["crew_source_ref"] = _org_src_ref
            if isinstance(_org_curve_ceiling, (int, float)) \
                    and not isinstance(_org_curve_ceiling, bool):
                demand["_organization_crew"]["curve_ceiling"] = int(_org_curve_ceiling)
            # 一致说明（不再自相矛盾）：上限从哪来、每面几个人、总数怎么来的。
            _trace = "；".join(x for x in (
                "crew_source=%s" % _crew_src if _crew_src else "",
                _org_src_ref) if x)
            wf_note = "；".join(p for p in (
                wf_note,
                "（每面人数上限单源（契约 §6）：以**施工组织层工种曲线**为准 —— "
                "%d 个作业面 × %s 人/面 = %d 人%s）"
                % (_org_faces, _org_face_crew or "?", daily,
                   "，%s" % _trace if _trace else "")) if p)
        else:
            _limit = max_labor
            _ai_is_limit = False
            if _ai_ceiling is not None and (_limit is None or _ai_ceiling < _limit):
                _limit = _ai_ceiling
                _ai_is_limit = True
            if _ai_ceiling is not None:
                _ai_crew_ref = ("ai_crew：模型自定班组（无定额依据），同族上限 %d 人"
                                "（work_type_l3=%s）"
                                % (_ai_ceiling, _l3_of(task, binding) or "（未知）"))
            # 只有**真的压了**才算"已封顶"（否则文案就是假的，见 _workface_capped 的旧教训）
            _ai_crew_capped = bool(_ai_is_limit and _limit is not None and daily > _limit)
            if _limit is not None and daily > _limit:
                _capped_kind = "ai_crew" if _ai_is_limit else "workface_capacity"
                if _ai_crew_capped:
                    _why = ("无定额依据（match_type=%s）的班组 %d 人超过同族上限 %d 人，"
                            "已封顶；本节点只调投入人数，不改计划工期"
                            % (match_type or "ai", daily, int(_limit)))
                else:
                    _why = ("工作面容量封顶：%s最多 %d 人，已由 %d 人压到 %d 人；"
                            "仍缺 %d 工日，故本任务需更多日历天完成"
                            "（本节点只调投入人数，不改计划工期）"
                            % (unit_basis, int(_limit), daily, int(_limit),
                               needed_person_days - int(_limit) * planned_days))
                capped.append({
                    "resource": name,
                    "kind": "labor",
                    "original_per_day": daily,
                    "capped_per_day": int(_limit),
                    "unit_basis": unit_basis if not _ai_crew_capped else "同族 crew_max（每施工段）",
                    "cap_source": _capped_kind,
                    # 文案必须与实际行为一致：本节点只调整"投入多少人"，
                    # **不会**改 planned_days（工期由排程节点定）。原来的"工期需相应延长"
                    # 让用户以为工期已经自动延长了，而实际没有 —— 属于误导。
                    "reason": _why,
                })
                daily = int(_limit)
        if _ai_ceiling is not None:
            # §7 打标：这条班组是模型自己定的（无定额依据），不是定额给的。
            # 文案与实际行为一致：真封了才写"已封顶"，没超上限就不许写"封顶"。
            _ai_sentence = ("班组由模型自定（无定额依据），%s（%s）"
                            % ("已按同族上限封顶" if _ai_crew_capped
                               else "未超过同族上限 %d 人" % _ai_ceiling,
                               _ai_crew_ref))
            wf_note = "；".join(p for p in (wf_note, _ai_sentence) if p)
        demand[name + "_per_day"] = daily
        demand[name + "_total_days"] = round(needed_person_days, 2)
        if not _org_crew:
            # 定额来源照旧；组织层接管时 `source_map[name]` 已在上面写成 org_layer
            # （这一行原样会把它覆盖掉，于是"班组从组织层来"这件事在产物里就消失了）。
            source_map[name] = {"origin": res_origin, "ref": ref}
        if _ai_ceiling is not None:
            # §7 要求的来源标记（必须在上面那行之后写，否则会被定额来源覆盖）
            source_map[name] = {"origin": "ai_crew", "ref": _ai_crew_ref}
        labor_crew[name] = daily
        for role, count in machine_roles.items():           # 混合活动：机械配员照记（按 1 台口径）
            demand[role + "_per_day"] = count
            demand[role + "_total_days"] = round(count * planned_days, 2)
            source_map[role] = _crew_provenance(task)
            labor_crew[role] = count

    demand["_crew"] = dict((k, v) for k, v in labor_crew.items() if v)
    if wf_note:
        # 工作面容量不可用时如实写出来（"没封顶"这件事本身也是信息）
        demand["_workface_note"] = wf_note
    demand["_resource_source"] = source_map
    demand["_norm_applied"] = {
        "mode": mode,
        "norm_value": norm_value,
        "productivity_value": productivity,
        "quantity_basis": basis,
        "source_code": source_code,
        "match_type": match_type,
        "origin": origin,
        # ---- 政策变更（用户 2026-09-20）：AI 经验估算定额的**逐条标注** ----
        # `source_code`（上面那行）本来就是可追溯的凭据（"AI_ESTIMATE_V1"），
        # 这里再给它一个显式布尔 + 中文文案，免得下游各自去猜"AI_ 前缀算不算 AI"。
        # 这两个键只为**显示与追溯**存在，不承载任何判据。
        "ai_estimate": ai_estimate,
        "source_label": LABEL_AI_ESTIMATE if ai_estimate else "",
    }
    # 具名假设（第 41 轮）：**算出来了**（不是"没算出来"），但换算用了一个写明的假定值，
    # 所以必须把"假定值 → 换算后的量 → 定额行/档位 → 算出的工日 → 量级参考"整条写出来，
    # 让人能自己判断量级。⚠️ 与 `_norm_flagged`/`_warning`（= 未计算班组）语义**不同**，
    # 刻意分开：算出来但带假定 ≠ 算不出来。`_unit_assumed_facts` 只给 compute_flat 的
    # 同口径汇总用，两遍之间 pop 掉，不进产物。
    if binding.get("unit_assumption"):
        _leaf_unit = kb_units.normalize_unit(
            binding.get("leaf_unit") or task.get("unit") or "")
        demand["_unit_assumed"] = _unit_assumed_text(
            binding, quantity, norm_quantity, _theoretical, _result_amount,
            "台班" if mode == "machine" else "工日",
            per_day=_result_per_day, days=planned_days,
            task_unit=task.get("unit") or "")
        demand["_unit_assumed_facts"] = {
            "group_key": "%s｜%s｜%s" % (
                binding.get("norm_id") or binding.get("source_code") or "",
                binding.get("condition_text") or "", _leaf_unit),
            "quantity": quantity,
            "unit": _leaf_unit,
            "amount": _result_amount,
            "label": "台班" if mode == "machine" else "工日",
        }
    if capped:
        demand["_workface_capped"] = capped
    return demand


# ==================== 主计算（与 资源定额.txt main 一致，返回扁平结构） ====================
# ==================== 场地级设备（塔吊 / 施工电梯） ====================
# 用户指令：「直接添加塔吊和施工电梯到机械表中，并记得添加 crew」。
# 这两个是**全场地常驻**的垂直运输设备 —— 一个工地各 1 台，服务所有楼层的所有任务，
# 所以曲线口径与泵车/挖掘机这类**任务级机械**不同（见 `plan_assembler._daily_peak` /
# `delivery._compute_view` 的 site-level 分支：逐日取 max，不按任务叠加）。
#
# ⚠️ 为什么不走定额路径：库内**没有**塔吊/施工电梯的台班定额行，`Activity_Main_Machine`
# 也**不许**加行（加一行会挤掉 CONC_NEW_* 的泵车定额，见
# tests/test_vertical_transport_crew.py 的两把锁）。台数只能来自"用户申报"或
# "标注清楚的 AI 默认值"，**绝不虚构台班定额数值挂到规范来源上**。
#
# 台数来源（**第 2 批 · 域 7.7 / 7.10 起有四个，按优先级**；改前恒为 1 台）：
#   ① `boundary_conditions.site_machine_const` —— **项目级常量**（边界节点一次定好并冻结，
#      逐台 `count_source`：user = 用户申报赢；ai_default = AI 按建筑参数估）；
#   ② 用户申报：`boundary_conditions.equipment` 且 `_source["equipment"] == "user"`
#      → 经 `parse_boundary_conditions` 归一后落在 `boundaries["equipment_peak"]`；
#   ③ 边界节点没落常量（老产物）→ 本层用**同一套明示规则**
#      （`org_defaults.estimate_site_machine_count`，只用栋数 / 面积 / 层数）补算，
#      标注里写明"未冻结"；
#   ④ 连建筑参数都没有 → **1 台**，并在每条命中任务的标注里写明"无可用建筑参数"。
SITE_LEVEL_EQUIPMENT = ("塔吊", "施工电梯")

# 场地级设备的台班来源标记：**无规范依据**，导入真实规范后应清退（写进任务标注）。
SITE_EQUIPMENT_NORM_SOURCE = "AI_ESTIMATE_V1"

# 垂直运输需求规则（**AI 默认口径，无规范依据**）：任务名命中关键词 → 该任务需要这台
# 场地级设备。词表就是规则真源；命中条数会在交付报告里给出，便于逐条核对。
SITE_EQUIPMENT_KEYWORDS = {
    # 塔吊：主体结构阶段的材料垂直运输（钢筋/模板/混凝土/预制构件/爬架）+ 砌体
    # ⚠️ A7（2026-09-21）：「叠合板」已从本词表移除（预制构件退场；删掉行为不变 ——
    # 同一元组已有「吊装」「模板」，叠合板吊装任务照样命中）。
    "塔吊": ("钢筋绑扎", "钢筋安装", "钢筋工程", "模板", "铝模", "混凝土浇筑",
             "吊装", "预制构件", "爬架", "钢结构", "ALC", "砌块",
             "构造柱", "屋面"),
    # 施工电梯：砌体 + 装饰装修 + 安装工程的**楼内**人员与材料垂直运输
    "施工电梯": ("ALC", "砌块", "构造柱", "勾缝", "抹灰", "找平", "涂料", "门窗",
                 "保温", "面砖", "吊顶", "栏杆", "预留预埋", "桥架", "配管配线",
                 "电缆", "管道", "风管", "喷淋", "消火栓", "防排烟", "配电箱",
                 "灯具", "防雷接地", "卫生器具", "空调设备", "火灾报警",
                 "气体灭火"),
}

# 室外/总平/临建类工序的垂直运输不靠塔吊/施工电梯（**排除规则同样是 AI 口径**）：
# 否则「路缘石安装」「室外照明系统安装」这种室外活也会被算进施工电梯。
# 「运输」也排除：实测冻结计划里 1.5.1 混凝土运输 / 1.5.2 砂运输 / 1.5.4 复合模板运输
# 会被"模板/混凝土"关键词命中，但它们是**开工前的材料运输工序**（D0 单日，塔吊尚未安装），
# 垂直运输那一段由它们所服务的安装/浇筑工序覆盖，重复计一次没有依据。
SITE_EQUIPMENT_EXCLUDE = ("室外", "管网", "道路", "路基", "绿化", "围墙", "围栏",
                          "大门", "停车位", "人行道", "路缘石", "检查井", "阀门井",
                          "标识标牌", "场地", "临时", "拆除", "退场", "移交",
                          "验收", "备案", "许可", "报建", "图纸", "档案", "资料",
                          "调试", "试运转", "运输")

# KB 取不到配员时的兜底（唯一真源是 `Equipment_Crew_Mapping`；这里只是不让流水线
# 因为 KB 缺行而静默丢掉配员，标注里会写明"代码兜底默认"）。
# 【第 2 批 · 域 7.9】取值真源搬到 `org_defaults.SITE_MACHINE_CREW_FALLBACK`：
# `boundary` 节点写 `site_machine_const` 时也要这份兜底，而 node 之间不许互相 import，
# 所以常量放纯默认值模块，这里只做**再导出**（值逐字未变，名字保留兼容既有引用）。
SITE_EQUIPMENT_CREW_FALLBACK = org_defaults.SITE_MACHINE_CREW_FALLBACK


def _site_equipment_const_block(boundaries):
    """边界节点冻结的 `site_machine_const` 块（没有 / 形状不对 → None）。"""
    block = (boundaries or {}).get(org_defaults.SITE_MACHINE_CONST_KEY)
    return block if isinstance(block, dict) else None


def _site_equipment_quantity(machine, boundaries, params=None):
    """场地级设备的台数 → ``(台数, 来源)``，来源是 ``"user"`` 或 ``"ai_default"``。

    【第 2 批 · 域 7.7 / 7.10】来源优先级（**不再写死 1 台**）：
      ① `boundary_conditions.site_machine_const`（**项目级常量**，边界节点一次定好并冻结）
         —— 逐台读 `count_source`：`user` → 用户申报赢；`ai_default` → AI 按建筑参数估的；
      ② 用户申报的老路径 `equipment_peak`（旧产物 / 直连用法的兼容路径，口径不变）；
      ③ 本层用**同一套明示规则**（`org_defaults.estimate_site_machine_count`，只用
         栋数 / 面积 / 层数）补算 —— 只在 boundary 没落常量时走到（老计划），
         标注里会写明"未冻结、由资源层按同一规则补算"；
      ④ 三者都取不到依据（连建筑参数都没有）→ **1 台 + `ai_default`**，标注如实写"无可用
         建筑参数"（"够用"口径：宁可常驻 1 台，也不判缺容量）。
    """
    block = _site_equipment_const_block(boundaries)
    machines = block.get("machines") if block else None
    entry = machines.get(machine) if isinstance(machines, dict) else None
    if isinstance(entry, dict):
        try:
            q = float(entry.get("count"))
        except (TypeError, ValueError):
            q = 0.0
        if q > 0:
            return q, ("user" if str(entry.get("count_source")) == "user"
                       else "ai_default")
    declared = (boundaries or {}).get("equipment_peak") or {}
    for key, val in declared.items():
        k = str(key)
        if k == machine or machine in k or k in machine:
            try:
                q = float(val)
            except (TypeError, ValueError):
                continue
            if q > 0:
                return q, "user"
    if isinstance(params, dict) and params:
        try:
            n, _rule, _basis = org_defaults.estimate_site_machine_count(machine, params)
        except Exception:
            n = None
        if n:
            return float(n), "ai_default"
    return 1.0, "ai_default"


def _site_equipment_const_meta(machine, boundaries, params=None):
    """台数的**可复核标注** → ``{rule, basis, frozen, const_source}``（不改变既有键）。

    `frozen=True` 只在读到边界节点冻结的常量时为真（域 7.7 的"冻结"口径）：
    资源层自己补算的值**不许**冒充"已冻结"，免得把"资源层临时估的"当成"项目级常量"。
    """
    block = _site_equipment_const_block(boundaries)
    machines = block.get("machines") if block else None
    entry = machines.get(machine) if isinstance(machines, dict) else None
    if isinstance(entry, dict) and entry.get("count"):
        return {"rule": str(entry.get("rule") or ""), "basis": entry.get("basis") or {},
                "frozen": True, "const_source": "site_machine_const",
                "count_source": str(entry.get("count_source") or "ai_default"),
                "estimate_note": str(block.get("estimate_note") or "")}
    # ⚠️ 判据顺序必须与 `_site_equipment_quantity` **逐条同序**（否则标注会指错来源）：
    # 常量 → 用户申报 equipment_peak → 本层按同一规则补算 → 1 台兜底。
    declared = (boundaries or {}).get("equipment_peak") or {}
    for key in declared:
        k = str(key)
        if k == machine or machine in k or k in machine:
            return {"rule": "用户申报（经 boundary.equipment 归一为限额）",
                    "basis": {"rule_id": "user.declared", "machine": machine},
                    "frozen": bool(block), "const_source": "equipment_peak",
                    "count_source": "user", "estimate_note": ""}
    if isinstance(params, dict) and params:
        try:
            _n, rule, basis = org_defaults.estimate_site_machine_count(machine, params)
            return {"rule": rule, "basis": basis, "frozen": False,
                    "const_source": "resource_estimate",
                    "count_source": "ai_default",
                    "estimate_note": org_defaults.SITE_MACHINE_ESTIMATE_NOTE}
        except Exception:
            pass
    return {"rule": "无可用建筑参数（栋数 / 建筑面积均未取得）：按最小常驻 1 台",
            "basis": {"rule_id": "no_input", "machine": machine, "inputs_missing": True},
            "frozen": bool(block), "const_source": "default_one",
            "count_source": "ai_default",
            "estimate_note": org_defaults.SITE_MACHINE_ESTIMATE_NOTE}


def _site_equipment_crew(machine):
    """场地级设备的配员（司机 / 信号工）与其来源。

    唯一真源是 KB 的 `Equipment_Crew_Mapping`（`kb.crew_for_machine` 自带规格后缀的
    模糊兜底）；KB 取不到才用 `SITE_EQUIPMENT_CREW_FALLBACK`，并把 source 标成
    ``"fallback"`` —— 调用方**必须**把它写进标注，不许静默降级。

    【第 2 批 · 域 7.9】解析逻辑收敛到 `org_defaults.resolve_site_machine_crew`
    （与 `boundary._site_machine_crew_of` 同源同口径，避免同一个 KB 两处各解析一遍）；
    本函数**保持返回形状逐字不变**（调用方 / 交付物读的是这几个键）。
    """
    row = None
    try:
        row = kb.crew_for_machine(machine)
    except Exception:                                     # pragma: no cover
        row = None
    return org_defaults.resolve_site_machine_crew(machine, row)


def _site_crew_provenance(crew_info):
    """配员来源 → `_resource_source` 风格的 {origin, ref, confidence}。"""
    if crew_info.get("source") == "kb":
        return {"origin": "kb", "ref": str(crew_info.get("ref") or ""),
                "confidence": crew_info.get("confidence") or ""}
    return {"origin": "ai_default",
            "ref": str(crew_info.get("ref") or "SITE_MACHINE_CREW_FALLBACK"),
            "confidence": "LOW"}


def _site_equipment_note(machine, per_day, quantity_source, crew_info,
                         rule=None, const_source=None, frozen=None):
    """场地级设备的**中文可人读**标注（口径、来源、置信度、清退条件，一个都不能少）。

    【第 2 批 · 域 7.7 / 7.10】新增 `rule` / `const_source` / `frozen` 三个可选入参：
    台数**不许只给一个数**，必须带上"这个数是怎么来的"（明示规则 + 是否冻结 + 是否由
    资源层补算）。缺省（不传）时文案与改前**逐字一致**，兼容既有调用与展示测试。
    """
    qtxt = ("用户申报" if quantity_source == "user"
            else "AI 默认口径：用户未申报台数")
    if crew_info.get("composition"):
        crew_txt = "%s（来源 %s%s）" % (
            crew_info["composition"],
            "kb:Equipment_Crew_Mapping" if crew_info.get("source") == "kb"
            else "代码兜底默认（KB 无该行）",
            ("，ref=%s，置信度=%s" % (crew_info["ref"], crew_info.get("confidence") or "—"))
            if crew_info.get("ref") else "")
    else:
        crew_txt = "无配员数据"
    note = ("垂直运输设备：%s %s 台（%s）；配员 %s；"
            "台班定额来源=%s（无规范依据，导入真实规范后应清退）；"
            "口径=场地级常驻设备（逐日曲线取 max，同一天多个任务需要也只算 %s 台，不按任务叠加）"
            % (machine, per_day, qtxt, crew_txt, SITE_EQUIPMENT_NORM_SOURCE,
               per_day))
    if rule:
        # 域 7.10：台数规则必须**逐条**落进产物（人可复核），不许只给一个数
        note += "；台数规则=%s（%s）" % (
            rule, org_defaults.SITE_MACHINE_ESTIMATE_NOTE)
        if const_source == "site_machine_const":
            note += "；来源=边界节点冻结的项目级常量 site_machine_const（全项目一次定好，不分到 L4 重估）"
        elif const_source == "resource_estimate":
            note += "；来源=资源层按同一套明示规则补算（**未冻结**：边界节点未落 site_machine_const）"
        if frozen:
            note += "；冻结=是（重跑逐位一致）"
    return note


def _site_machine_const_daily(site_registry):
    """【第 2 批 · 域 7.8】项目级常量的**日账本**（`{机械: {...}}`，逐日都要记这一次）。

    "默认够用"要能被逐日渲染看见（连续在场），所以除计划级登记外再给一份日账本：
    `per_day` = 台数、`present_all_days` = True、`const_period` = 全项目；
    并带上 7.10 的台数规则与冻结标记，以及"**不进超限清单**"的口径声明。

    键序 = `site_registry["machines"]` 的插入序（= `SITE_LEVEL_EQUIPMENT` 固定元组）→ 确定性。
    """
    return {
        m: {
            "per_day": int(rec.get("quantity") or 1),
            "unit": "台",
            "days": None,                     # 全项目天数在资源层未知 → 见 const_period
            "const_period": {"from_day": 0, "to_day": None},
            "present_all_days": True,         # 连续在场：逐日取 max，不按任务叠加
            "count_source": rec.get("quantity_source") or "ai_default",
            "rule": rec.get("rule") or "",
            "frozen": bool(rec.get("frozen")),
            "hit_tasks": rec.get("hit_tasks", 0),
            "note": ("项目级常量：全项目连续在场（逐日取 max，同一天多个任务需要"
                     "也只算这一次），不按任务叠加；默认够用、**不进超限清单**。"),
        }
        for m, rec in (site_registry.get("machines") or {}).items()
    }


def _inject_site_equipment(result_tasks, boundaries, params=None):
    """把场地级设备（塔吊 / 施工电梯）投到需要垂直运输的任务上（**就地**修改）。

    规则、台数来源、配员来源全部落进产物（任务级 `_site_equipment` /
    `_resource_source`，计划级 `_site_level_equipment`），**绝不静默**。

    返回 ``(计划级登记, {task_id: {资源名: 来源}})``；没有任何命中时返回 ``({}, {})``
    —— 调用方据此保证"没有新增命中时输出逐字段不变"（test_algorithm_parity 盯着它）。

    【第 2 批 · 域 7.7 / 7.9 / 7.10】
      · `params`（`ctx["extracted_params"]`）用于**边界节点没落常量时**补算台数，
        只用栋数 / 面积 / 层数（`org_defaults.estimate_site_machine_count`）；
      · 台数优先读 `site_machine_const`（项目级常量，冻结），逐台新增
        `rule` / `basis` / `frozen` / `const_source` 四个标注键（既有键一个不动）；
      · 配员仍是**每台**人数，逐日配员 = **台数 × 每台人数**（`crew_daily`），
        司机 / 信号工**不设限额**（本函数不写任何限额判据 —— 那是 `scheduler` 的事）。
    """
    plan_qty = {m: _site_equipment_quantity(m, boundaries, params)
                for m in SITE_LEVEL_EQUIPMENT}
    plan_meta = {m: _site_equipment_const_meta(m, boundaries, params)
                 for m in SITE_LEVEL_EQUIPMENT}
    plan_crew = {m: _site_equipment_crew(m) for m in SITE_LEVEL_EQUIPMENT}
    hit_tasks = []
    provenance = {}
    used = {}
    for demand in result_tasks:
        if not isinstance(demand, dict):
            continue
        task_name = str(demand.get("task_name") or "")
        if not task_name or any(x in task_name for x in SITE_EQUIPMENT_EXCLUDE):
            continue
        picked = [m for m in SITE_LEVEL_EQUIPMENT
                  if any(k in task_name for k in SITE_EQUIPMENT_KEYWORDS.get(m, ()))]
        if not picked:
            continue
        # 只有"真在干活"的工序才投入：行政/验收类任务在上游已被 continue 掉，
        # 落到这里时既没有资源、也没有任何定额/量级标记。
        # （标了 `_norm_flagged` / `_scale_flagged` 的任务**照投** —— 定额不可信是
        #  "这条任务的班组没算"，不等于"这条活不需要垂直运输"；场地级设备台数与
        #  该任务的工程量无关，不继承那个不可信的量。）
        if not (any(str(k).endswith("_per_day") for k in demand)
                or demand.get("_norm_flagged") or demand.get("_scale_flagged")):
            continue
        try:
            days = int(demand.get("planned_duration_days") or 1)
        except (TypeError, ValueError):
            days = 1
        days = max(1, days)
        src_map = demand.get("_resource_source")
        if not isinstance(src_map, dict):
            src_map = {}
        items = []
        for machine in picked:
            qty, qsrc = plan_qty[machine]
            per_day = int(math.ceil(qty)) or 1
            crew_info = plan_crew[machine]
            meta = plan_meta[machine]
            item = {"name": machine, "quantity": per_day, "unit": "台",
                    "quantity_source": qsrc,
                    "norm_source": SITE_EQUIPMENT_NORM_SOURCE,
                    "caliber": "site_level_max",
                    "crew": {}, "crew_composition": crew_info.get("composition"),
                    "crew_source": crew_info.get("source"),
                    "crew_ref": crew_info.get("ref"),
                    # 【第 2 批 · 域 7.7 / 7.10】台数的可复核标注（**新增键**，
                    # 既有键名与类型一个不动 —— 交付物 / 曲线读的是上面那几个）。
                    "rule": meta.get("rule") or "",
                    "basis": meta.get("basis") or {},
                    "const_source": meta.get("const_source") or "",
                    "frozen": bool(meta.get("frozen"))}
            for role, cnt in (crew_info.get("crew") or {}).items():
                # `Equipment_Crew_Mapping.crew_composition` 是**每台**的配员
                # （"司机1名+信号工1名" = 1 台塔吊配 1 名司机 + 1 名信号工），
                # 所以 `item["crew"]` 存**每台**人数，逐日配员 = 台数 × 每台人数
                # （`plan_assembler.site_equipment_contrib` / `delivery._site_equipment_map`
                #  就是按这个乘法还原的 —— 这里存总数会让台数被乘第二次）。
                c = int(math.ceil(float(cnt)))
                if c <= 0:
                    continue
                n = per_day * c
                item["crew"][role] = c
                # 配员是**人**，直接叠加进该任务的同名人员条目（如别的机械也有"司机"，
                # 两份就是两个人）；来源只在该角色还没有来源时补一条，
                # 逐项来源留在 `_site_equipment[*].crew_ref`（不覆盖别人的来源）。
                try:
                    base_pd = int(demand.get("%s_per_day" % role) or 0)
                except (TypeError, ValueError):
                    base_pd = 0
                try:
                    base_td = float(demand.get("%s_total_days" % role) or 0)
                except (TypeError, ValueError):
                    base_td = 0.0
                demand["%s_per_day" % role] = base_pd + n
                demand["%s_total_days" % role] = round(base_td + n * days, 2)
                src_map.setdefault(role, _site_crew_provenance(crew_info))
            # 场地级设备的台数**覆盖**该任务遗留路径算出的同名机械：一个是"按工程量
            # 算的台班"，一个是"全场地常驻台数"，叠加就是虚高（口径不同不许相加）。
            old = demand.get("%s_per_day" % machine)
            demand["%s_per_day" % machine] = per_day
            demand["%s_total_days" % machine] = round(per_day * days, 2)
            m_src = ({"origin": "user", "ref": "boundary.equipment"} if qsrc == "user"
                     else {"origin": "ai_estimate", "ref": SITE_EQUIPMENT_NORM_SOURCE})
            src_map[machine] = m_src
            item["note"] = _site_equipment_note(
                machine, per_day, qsrc, crew_info, rule=meta.get("rule"),
                const_source=meta.get("const_source"), frozen=meta.get("frozen"))
            try:
                if old and int(old) != per_day:
                    item["replaced_task_level_per_day"] = old
                    item["note"] += "；已覆盖该任务遗留路径算出的同名台数 %s 台" % old
            except (TypeError, ValueError):
                pass
            items.append(item)
            rec = used.setdefault(machine, {
                "quantity": per_day, "unit": "台", "quantity_source": qsrc,
                "crew": dict(item["crew"]),                     # **每台**配员
                "crew_daily": {r: per_day * c for r, c in item["crew"].items()},
                "crew_composition": crew_info.get("composition"),
                "crew_source": crew_info.get("source"),
                "crew_ref": crew_info.get("ref"),
                "norm_source": SITE_EQUIPMENT_NORM_SOURCE,
                "caliber": "site_level_max",
                # 【第 2 批 · 域 7.7 / 7.8 / 7.10】项目级常量的可复核标注（新增键）：
                # 台数规则 / 依据 / 是否冻结 / 台数从哪条路来的。
                "rule": meta.get("rule") or "",
                "basis": meta.get("basis") or {},
                "const_source": meta.get("const_source") or "",
                "frozen": bool(meta.get("frozen")),
                "note": item["note"], "hit_tasks": 0})
            rec["hit_tasks"] += 1
        demand["_site_equipment"] = items
        demand["_resource_source"] = src_map
        hit_tasks.append({"task_id": demand.get("task_id"), "task_name": task_name,
                          "equipment": [i["name"] for i in items]})
        provenance[str(demand.get("task_id"))] = {
            i["name"]: src_map.get(i["name"]) for i in items}
    if not hit_tasks:
        return {}, {}
    registry = {
        "machines": used,
        "tasks": hit_tasks,
        "count": len(hit_tasks),
        "norm_source": SITE_EQUIPMENT_NORM_SOURCE,
        "caliber": ("场地级常驻设备：逐日曲线取 max（同一天多个任务需要也只算申报台数），"
                    "不按任务叠加；任务级机械（泵车/挖掘机等）仍按日叠加。"),
        "note": ("塔吊/施工电梯的台数只有两个来源：用户申报（boundary.equipment 且来源为 "
                 "user）或 AI 默认 1 台；库内没有它们的台班定额行，故**不引用任何规范台班**，"
                 "导入真实规范后应清退。"),
    }
    return registry, provenance


def compute_flat(wbs: dict, extracted_params: dict = None, boundary_conditions: dict = None,
                 schedule_days: dict = None, schedule_org: dict = None) -> dict:
    """按工序算资源需求。

    `schedule_days`：``{task_id: 排程实际工期(天)}``（第 39 轮新增，可缺省）。
    `schedule_org` ：``{task_id: 排程行的 _organization}``（第 44 轮 WS6 新增，可缺省）。
    两者都从 `ctx["schedule"]["schedule"]` 的**同一行**取一次（同一份排程版本），
    缺省 = 完全旧行为。

    第 39 轮**工日守恒**修正：本函数原来一律用 `task["duration_days"]` —— 那是
    **WBS 里模型写的目标天数**，不是排程算出来的工期。实测 1.5.1 混凝土运输：
    排程按定额算出 148 天（2216 工日 ÷ 15 人），这里却按 WBS 的 **1 天** 算
    → 每天要 2216 人 → 被工作面容量压到 4 人 → 曲线上这条任务只剩 4 工日，
    自述"仍缺 2212 工日"。量级差 550 倍的根因就在这一行。
    传入排程工期后：15 人 × 148 天 ≈ 2216 工日（与定额需求守恒），
    且人数与排程的班组**同一个数**。
    """
    try:
        wbs_obj = to_obj(wbs, {})
        if not wbs_obj:
            return {"resource_demand": {"tasks": []}}

        schedule_days = schedule_days if isinstance(schedule_days, dict) else {}
        # WS6：施工组织层结果（`_organization`）与 schedule_days **同源同一次**取，
        # 免得"工期取排程版、组织层取另一版"。缺省 → {}（逐位旧行为）。
        schedule_org = schedule_org if isinstance(schedule_org, dict) else {}
        params = parse_extracted_params(extracted_params)
        # 量级上界：按项目规模（总面积/各类总量）推算，缺规模就不给界（不猜）
        scale_bounds = quantity_scale_bounds(params)
        boundaries = parse_boundary_conditions(boundary_conditions)
        phases = wbs_obj.get("phases", [])
        leaf_tasks = collect_leaf_tasks(phases)

        FALLBACK_QUOTAS = {
            "普工": 0.05, "钢筋工": 0.08, "模板工": 0.12, "混凝土工": 0.1,
            "抹灰工": 0.12, "泥工": 0.1, "油漆工": 0.1, "电工": 0.05,
            "管道工": 0.08, "防水工": 0.06, "瓦工": 0.15, "安装工": 0.1,
            "绿化工": 0.2, "保温工": 0.08, "通风工": 0.1, "架子工": 0.05,
            "灌浆工": 0.02, "装配式安装工": 0.15, "桩机工": 0.2,
            "铺装工": 0.1, "水泥工": 0.3, "测量工": 0.2,
            "钻机": 0.05, "注浆泵": 0.8, "静压桩机": 0.15, "成槽机": 0.1,
            "旋挖钻机": 0.4, "搅拌桩机": 0.2, "挖掘机": 0.012,
            "自卸汽车": 0.025, "泵车": 0.02, "装载机": 0.01,
            "吊车": 0.1, "履带吊": 0.08, "塔吊": 0.02,
        }

        result_tasks = []
        unmatched_tasks = []
        kb_machine_count = 0
        norm_task_count = 0
        resource_provenance = {}

        for task in leaf_tasks:
            task_id = task.get("id") or task.get("task_id") or ""
            task_name = task.get("name") or task.get("task_name") or task_id
            quantity = task.get("quantity", 0.0)
            try:
                quantity = float(quantity)
            except (TypeError, ValueError):
                quantity = 0.0

            # 工期真源（第 39 轮）：排程算出来的工期优先，缺了才退回 WBS 目标天数。
            # 用错这一个数，就会得到"4 人干 2216 工日的活、工期 1 天"这种曲线。
            planned_days = schedule_days.get(str(task_id)) or task.get("duration_days", 1)
            try:
                planned_days = int(math.ceil(float(planned_days)))
            except (TypeError, ValueError):
                planned_days = 1

            unit = task.get("unit", "")

            validated_qty, validated_unit, warning = validate_quantity(task, params)
            if warning:
                quantity = validated_qty
                unit = validated_unit
                if "警告" in warning:
                    result_tasks.append(_demand_no_norm(
                        task, quantity, planned_days, _warning=warning))
                    continue

            quantity, unit = normalize_unit(quantity, unit, task_name)

            # 工程量量级校验（与 scheduler 同一判据，见 scheduler.quantity_scale_bounds）：
            # 模板/AI 的默认值可能与项目规模完全脱钩 —— 实测自带住宅楼样例里
            # 「定位放线」被模板写死成 128000 ㎡，而项目总建筑面积只有 14200 ㎡，
            # 于是这条 2 天的任务被算成 **64000 人/天**，把峰值人力彻底污染。
            # 这类量**不许用来算班组**：只登记 + 标注，不给资源。
            _scale_why = scale_violation({"quantity": quantity, "unit": unit}, scale_bounds)
            if _scale_why:
                result_tasks.append(_demand_no_norm(
                    task, quantity, planned_days, force_meta=True,
                    _scale_flagged=_scale_why,
                    _warning="工程量量级不可信，未计算班组：" + _scale_why))
                continue

            if quantity <= 0:
                # B6（2026-09-21）：原先这里会从**写死的部位比例表**（混凝土合计 1.6
                # 未归一 / 钢筋合计 1.4）反推工程量 —— 已整条删除，`get_fallback_quantity`
                # 恒定返回 None。缺量时**明确报缺 + 逐条标注**，绝不静默编数。
                result_tasks.append(_demand_no_norm(
                    task, quantity, planned_days,
                    _warning="警告:工程量 <= 0（%s），且 B6 已删除写死的部位比例表 —— "
                             "不再按「项目总量 × 部位比例」替用户编工程量；"
                             "请给出本工序的工程量与单位" % (task_name or "未命名")))
                continue

            if planned_days <= 0:
                continue

            # 定额证据门（与 scheduler 同一判据）只对**有可用绑定**的任务生效：
            # 没绑定的任务要走下面的遗留路径，一行都不能改（见 test_crew_bind 的
            # legacy 回归与 test_kb 的机械路径回归）。
            binding = task.get("norm_binding")
            # 老计划自救（第 41 轮）：先把**具名假设**写回绑定，再进证据门 —— 否则
            # `_unit_pair_reason()` 只会看到当时写死的 "unusable"，这 18 条 ALC 墙板
            # 仍然一条班组都算不出来（假设的来龙去脉见 _materialize_unit_assumption）。
            _materialize_unit_assumption(binding, unit)
            norm_demand = None
            if norm_binding_usable(binding):
                _ev_why = _norm_evidence_reason(binding, unit)
                if _ev_why:
                    result_tasks.append(_demand_no_norm(
                        task, quantity, planned_days, binding, force_meta=True,
                        _norm_flagged=_ev_why,
                        _warning="定额不可作证据，未计算班组：" + _ev_why))
                    continue
                try:
                    norm_demand = compute_norm_resources(
                        task, binding, quantity, planned_days,
                        org=schedule_org.get(str(task_id)))
                except Exception:
                    # 定额路径出意外 → 退回遗留路径（宁可给旧口径的数，也不中断整条流水线）
                    norm_demand = None
            if norm_demand is not None:
                # WS6：班组由施工组织层给的（`_organization_crew`）时**不再削峰** ——
                # 组织层已按"节拍 + 作业面数"定过班组，且 `scheduler.org_crew_ceiling`
                # 已经用过用户工种限额（每面口径）；资源层的削峰是按**总数**再压一刀、
                # 并把 per_day 改小、工期拉长，与"组织层是本工种班组唯一真源"直接冲突
                # （实测 1.5.1：组织层 2 面 × 15 人 = 30 人，削峰按申报 普工 15 人压回 15，
                #  于是资源行与组织层对不上）。这里如实留痕，绝不静默。
                _org_crew_used = bool(norm_demand.get("_organization_crew"))
                if boundaries and not _org_crew_used:
                    norm_demand = apply_peak_shaving(norm_demand, boundaries)
                elif boundaries:
                    _limits = boundaries.get("trade_peak") or {}
                    _trade = (norm_demand.get("_organization_crew") or {}).get("trade")
                    if _trade in _limits:
                        norm_demand["_peak_shaving_skipped"] = {
                            "reason": "班组的唯一真源是施工组织层（crew_total=%s 人：%s 面 × %s 人/面）；"
                                      "申报的工种总上限 %s 人已在组织层按**每面**口径校验"
                                      "（scheduler.org_crew_ceiling），资源层不再按总数二次削峰"
                                      % (norm_demand["_organization_crew"].get("crew_total"),
                                         norm_demand["_organization_crew"].get("n_faces"),
                                         norm_demand["_organization_crew"].get("crew_per_face"),
                                         int(_limits[_trade])),
                            "declared_trade_limit": int(_limits[_trade]),
                            "crew_total": norm_demand["_organization_crew"].get("crew_total"),
                        }
                result_tasks.append(norm_demand)
                norm_task_count += 1
                src = norm_demand.get("_resource_source")
                if src:
                    resource_provenance[task_id] = src
                continue

            skip_keywords = ["验收", "备案", "许可", "报建", "图纸", "档案", "资料", "钥匙", "五方", "实体移交"]
            if quantity == 1 and any(kw in task_name for kw in skip_keywords):
                result_tasks.append(_demand_no_norm(task, quantity, planned_days))
                continue

            if quantity == 1 and unit == "项":
                result_tasks.append(_demand_no_norm(task, quantity, planned_days))
                continue

            # ---- KB 机械台班定额（主体结构 equipment_driven 任务）----
            kb_machinery = None
            kid = task.get("kb_activity_id")
            if kid and quantity > 0:
                kb_machinery = compute_kb_machinery(str(kid), quantity, planned_days)
                if kb_machinery:
                    kb_machine_count += 1

            # ---- 核心匹配逻辑 ----
            matched_resources = []
            work_type = None
            matched_keyword = None

            sorted_mappings = sorted(RESOURCE_MAPPING.items(), key=lambda x: len(x[0]), reverse=True)
            for keyword, mapping in sorted_mappings:
                if keyword in task_name:
                    matched_resources = mapping.get("resources", [])
                    work_type = mapping.get("work_type", task_name)
                    matched_keyword = keyword
                    break

            if not matched_resources:
                wt = task.get("work_type", "")
                if wt and wt in RESOURCE_MAPPING:
                    mapping = RESOURCE_MAPPING[wt]
                    matched_resources = mapping.get("resources", [])
                    work_type = mapping.get("work_type", wt)
                    matched_keyword = wt

            if not matched_resources:
                matched_resources = ["普工"]
                work_type = task_name
                unmatched_tasks.append({
                    "task_id": task_id,
                    "task_name": task_name,
                    "work_type": task.get("work_type", ""),
                    "keyword": "无匹配"
                })

            # 无可用定额 → 遗留产能表口径。单位/计量对象与显式 `_norm_applied: None`
            # （"这条没有定额依据"，跨流 D4 的判据）都由唯一底板给出。
            demand = _demand_no_norm(task, quantity, planned_days, binding)

            if matched_keyword:
                demand["_matched_keyword"] = matched_keyword

            for resource_name in matched_resources:
                result = compute_resource_by_productivity(quantity, planned_days, resource_name, task_name, unit)

                if result is None:
                    result = compute_resource_by_quota(quantity, planned_days, resource_name, FALLBACK_QUOTAS)

                if result is None:
                    if resource_name != "普工":
                        result = compute_resource_by_quota(quantity, planned_days, "普工", FALLBACK_QUOTAS)
                        if result:
                            demand["普工_per_day"] = result["per_day"]
                            demand["普工_total_days"] = result["total_days"]
                    continue

                demand[f"{resource_name}_per_day"] = result["per_day"]
                demand[f"{resource_name}_total_days"] = result["total_days"]

            # 机械改由 KB 台班定额：移除现有产能路径的机械，加入 KB 机械
            if kb_machinery:
                for mname in _MACHINERY_NAMES:
                    demand.pop(f"{mname}_per_day", None)
                    demand.pop(f"{mname}_total_days", None)
                for mname, q in kb_machinery.items():
                    demand[f"{mname}_per_day"] = q["per_day"]
                    demand[f"{mname}_total_days"] = q["total_days"]

            if boundaries:
                demand = apply_peak_shaving(demand, boundaries)

            result_tasks.append(demand)

        # ---- 场地级设备（塔吊 / 施工电梯）注入 ----
        # 放在**所有任务都算完之后**统一做：它不依赖定额路径/遗留路径哪一条走通
        # （两条路都要出垂直运输设备），也不必改 `norm_binding` / 台班定额表。
        site_registry, site_provenance = _inject_site_equipment(
            result_tasks, boundaries, params)
        for _tid, _src in site_provenance.items():
            resource_provenance.setdefault(_tid, {}).update(_src)

        result = {"resource_demand": {"tasks": result_tasks}}

        if unmatched_tasks:
            result["resource_demand"]["_warnings"] = {
                "unmatched_tasks": unmatched_tasks,
                "count": len(unmatched_tasks),
                "message": f"有 {len(unmatched_tasks)} 个任务未匹配到关键词，已使用普工作为兜底"
            }

        if kb_machine_count:
            result["resource_demand"]["_kb_machinery_count"] = kb_machine_count

        # 场地级设备登记（塔吊/施工电梯）：台数来源、配员来源、命中任务逐条留档。
        # 只在真有命中时加键 —— 保证"没有垂直运输设备可投"的输出逐字段不变。
        if site_registry:
            result["resource_demand"]["_site_level_equipment"] = site_registry
            # 【第 2 批 · 域 7.8】项目级常量的**日账本**：每天都要记这个常量一次
            # （= 连续在场），供 `plan_assembler` / `delivery` 逐日渲染"连续在场"，
            # 也让"默认够用、不判超限"这条口径在产物里可查。
            # 键只在真有命中时加 —— 保持"没有垂直运输设备可投"的输出逐字段不变。
            result["resource_demand"][
                org_defaults.SITE_MACHINE_CONST_DAILY_KEY] = \
                _site_machine_const_daily(site_registry)

        # v2.2：定额路径的计数与来源汇总（仅在定额路径被用过时才出现，
        # 保证"无 norm_binding"的遗留输出与改动前逐字段一致）
        if norm_task_count:
            result["resource_demand"]["_norm_path_count"] = norm_task_count
        if resource_provenance:
            result["resource_provenance"] = resource_provenance
        # 「模型补齐的限额被丢弃」必须能从产物里查到（绝不静默）；为空时**不加键**，
        # 沿用本文件既有约束：没有定额/没有标注的遗留输出逐字段不变
        # （test_algorithm_parity 盯着旧路径的输出）。
        if boundaries.get("ignored_model_limits"):
            result["resource_demand"]["_ignored_model_limits"] = list(
                boundaries["ignored_model_limits"])

        # 同口径具名假设的**量级参考汇总**（第 41 轮）：逐条只写"本条的换算过程"，
        # 合计与项目规模的关系在这里补一句（1420 ㎡/层 × 18 层 = 25560 ㎡、合计多少工日），
        # 让人能自己判断量级对不对。⚠️ 仍然只是标注：工程量、工日、班组一个都不改；
        # `_unit_assumed_facts` 是两遍之间的搬运工，汇总完即 pop，不进产物。
        _groups = {}
        for _t in result_tasks:
            _f = _t.pop("_unit_assumed_facts", None)
            if _f and _t.get("_unit_assumed"):
                _groups.setdefault(_f["group_key"], []).append((_t, _f))
        for _items in _groups.values():
            if len(_items) < 2:
                continue
            _qty = sum(f["quantity"] for _t, f in _items)
            _amt = sum((f["amount"] or 0) for _t, f in _items)
            _f0 = _items[0][1]
            _ref = _magnitude_reference(_qty, _f0["quantity"], len(_items), _f0["unit"],
                                        _items[0][0].get("task_name"), params)
            _sentence = ("量级参考：同口径 %d 条合计 %g %s，合计 %g %s%s"
                         % (len(_items), _qty, _f0["unit"], _amt, _f0["label"],
                            ("（%s）" % _ref) if _ref else ""))
            for _t, _f in _items:
                _t["_unit_assumed"] = "%s；%s" % (_t["_unit_assumed"], _sentence)

        return result

    except Exception as e:
        return {"resource_demand": {"tasks": [], "_error": str(e)}}


# ==================== v1.1 契约适配：扁平键 → 嵌套 resources ====================
_RES_DYN_KEY = re.compile(r"^(.*)_(per_day|total_days)$")


def to_nested_resources(resource_demand: Dict) -> Dict:
    """把 task 里的 '推土机_per_day' 扁平键整理为 resources: {推土机: {per_day, total_days}}。"""
    tasks_new = []
    for task in resource_demand.get("tasks", []):
        new_task = {}
        resources = {}
        for k, v in task.items():
            m = _RES_DYN_KEY.match(k)
            if m:
                resources.setdefault(m.group(1), {})[m.group(2)] = v
            else:
                new_task[k] = v
        if resources:
            new_task["resources"] = resources
        tasks_new.append(new_task)
    result = dict(resource_demand)
    result["tasks"] = tasks_new
    return result


def main(wbs: dict, extracted_params: dict = None, boundary_conditions: dict = None,
         schedule_days: dict = None, schedule_org: dict = None) -> dict:
    """独立运行入口：计算 + 嵌套适配。"""
    flat = compute_flat(wbs, extracted_params, boundary_conditions,
                        schedule_days=schedule_days, schedule_org=schedule_org)
    flat["resource_demand"] = to_nested_resources(flat.get("resource_demand", {"tasks": []}))
    return flat


class ResourceNode(BaseNode):
    name = "resource"
    title = "资源定额计算"

    def run(self, ctx):
        wbs = ctx.get("wbs") or {}
        params = ctx.get("extracted_params") or {}
        boundary = ctx.get("boundary_conditions") or {}
        # 排程实际工期（第 39 轮）：排程节点在 resource 之前跑完，`ctx["schedule"]`
        # 就是交付版（resource_ok）那一版，行里 `es/ef` 给出这条任务真实占用的天数。
        # 拿它当分母，资源曲线才与"人数 × 工期 = 工日"守恒。
        schedule_days = {}
        # WS6 施工组织层（第 44 轮）：排程行上的 `_organization`（节拍 + 作业面数 →
        # **跨作业面的总班组**）。与 schedule_days 同一次循环取，保证同一份排程版本。
        schedule_org = {}
        _sched = ctx.get("schedule")
        _sched = _sched if isinstance(_sched, dict) else {}
        for _row in (_sched.get("schedule") or []):
            if not isinstance(_row, dict):
                continue
            _tid = str(_row.get("task_id") or "")
            try:
                _span = int(_row.get("ef", 0)) - int(_row.get("es", 0))
            except (TypeError, ValueError):
                continue
            if _tid and _span > 0:
                schedule_days[_tid] = _span
            if _tid and isinstance(_row.get("_organization"), dict) and _row["_organization"]:
                schedule_org[_tid] = _row["_organization"]
        self.emit("node_progress", {"node": self.name, "progress": 40,
                                    "message": "按工序逐条匹配人工与机械消耗量"})
        flat = compute_flat(wbs, params, boundary, schedule_days=schedule_days,
                            schedule_org=schedule_org)
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "资源用量算好了，正在按可用人数削峰"})
        nested = to_nested_resources(flat.get("resource_demand", {"tasks": []}))
        n = len(nested.get("tasks", []))
        kb_n = flat.get("resource_demand", {}).get("_kb_machinery_count") or 0
        kb_txt = f"；机械台班来自KB定额 {kb_n} 项" if kb_n else ""
        norm_n = flat.get("resource_demand", {}).get("_norm_path_count") or 0
        norm_txt = f"；走定额锚定 {norm_n} 项" if norm_n else ""
        site = flat.get("resource_demand", {}).get("_site_level_equipment") or {}
        site_n = site.get("count") or 0
        site_txt = ""
        if site_n:
            names = "/".join(sorted(site.get("machines") or {}))
            site_txt = f"；场地级设备（{names}）投入 {site_n} 项（逐日取 max）"
        self.done_summary = (f"完成 {n} 个任务资源定额{kb_txt}{norm_txt}{site_txt}；削峰调整 "
                             f"{sum(1 for t in nested.get('tasks', []) if t.get('_adjusted'))} 项")
        out = {"resource_demand": nested}
        # v2.2：定额路径的资源来源汇总写回 ctx（无定额路径时不产生该键）
        if flat.get("resource_provenance"):
            out["resource_provenance"] = flat["resource_provenance"]
        return out
