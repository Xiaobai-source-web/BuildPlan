"""节点：排程与两版工期（SchedulerNode）— 纯 Python 确定性算法，**绝不调用 LLM**。

一次算出**两版工期**，两版都是"满足依赖 + 满足工作面容量"的最短工期，
唯一的区别只在"资源上限取什么"：

  theory_min（理论最短工期）
      上限 = **工作面容量**（每个施工段顶满）。
      物理上最快能干完多久，**完全不看用户给的资源限额**。

  resource_ok（资源不超额工期）
      上限 = min(工作面容量, 用户资源限额)。
      在用户现有资源条件下最快能干完多久。

天然关系 **theory_min ≤ resource_ok** 必然成立：
resource_ok 的可用上限是 theory_min 上限的"子集"，班组只能更小或相等，
每条任务工期只能更长或相等；串行排程的可行解空间也只是被收窄、不会被放宽。
（test_scheduler.py 用断言守住这条不变量。）

用户提出的目标工期**只当参照**：本节点绝不为了凑目标去改定额、改工程量或改资源，
目标只影响 compare 里的中文结论（target_verdict / target_note）。

算法（力求简单、可解释、可复现）
------------------------------------------------
1. 每条叶子的班组规模与工期
   有可用定额（norm_value>0 或 productivity_value>0，且**有据可查**）：
       人工主导：P = productivity_value（缺则 1/norm_value）      [单位/工日]
                 人数 = **设计班组人数**（节拍配置声明；缺失才按目标工期反推）
                 工期 = max(1, ceil(工程量 / (P × 人数)))
                 ——定额是唯一产能真源，节拍产能表不再参与"排多少天"
                 再按"本版上限"封顶人数/台数（封顶一律记进 capped，绝不静默）。
       机械主导：台班定额来自 norm_binding，总台班 = 工程量/quantity_basis × norm_value
                 台数 = max(1, ceil(总台班 / 目标工期))
                 工期 = max(1, ceil(总台班 / 台数))
   无可用定额：沿用叶子自身 duration_days，并记一条"AI 假设"warning。
2. 串行排程（Serial SGS）
   按拓扑序（Kahn，队列按 task_id 排序保证确定性）逐条安排：
       earliest = max(前驱 ef + lag)   # FS；SS 用 es + lag
   从 earliest 起逐日检查"第 d..d+工期-1 天，所有相关资源的当日累计用量 + 本任务用量
   ≤ 上限"，找到最早的连续可排期起点；
   若永远排不下（上限太小），放到 earliest 并记一条 warning，不中断。
   用户给了**总人工上限**（labor.peak_total）时，同一循环里再复核"当天在岗人工之和
   ≤ 上限"，超了就把起点顺延（并发口径，**不是**把额度按任务数摊派）。

输入 ctx（全部可能缺失，全部容错）：
  wbs / dependencies / boundary_conditions / extracted_params / cpm_result
输出 ctx：
  schedule_versions = {theory_min, resource_ok, compare, warnings}
  schedule          = schedule_versions["resource_ok"]（默认交付版）
  schedule_warnings = 中文警告列表

任何异常都降级：不让流水线崩，能出多少出多少，并记中文说明。
Python 3.8 兼容；不新增第三方依赖。
"""

import math
import re

from .. import kb, kb_units, norm_defaults as _norm_defaults
from .. import org_defaults, org_plan
from .. import segment_capacity
from ..base import BaseNode

# ==================== 常量 ====================
# 缺工作面容量数据时用的"足够大"默认值：等于不封顶（会在 warning 里说明）
DEFAULT_CEILING = 10 ** 6

# 缺工作面容量标定行、用户也没给设备清单时的机械台数兜底（契约 §5-WS4 ③/上级口径 ③）。
# **不许**用 DEFAULT_CEILING 让台数无限：台数直接决定工期（台数翻倍工期减半），
# 没有依据的台数会把工期算成物理上做不到的短。宁保守取 1 台并记 warning。
DEFAULT_MACHINE_FALLBACK = 1

# ==================== 域 7（7.2 / 7.3）：三轮回压 ====================
#: 回压轮次上限（**常量**，裁定 #6：最多 3 轮，不许散落、不许 `while` 无限循环）。
#: 3 轮后仍超限 → **采用超限额值 + 如实标出**（`over_limit` 记一条 + warning），
#: **绝不抛异常**（口径 §3.2.5：总需求量有上界，回压只在 [硬下界, ∞) 里抬高工期）。
BACKPRESSURE_MAX_ROUNDS = 3

#: 回压分摊的权重口径留痕（域 7.3：按**需求量**分摊，**不按施工量** —— 施工量在
#: m²/m³/t 之间不可比）。落进 `_backpressure.share_source`，与 `segment_capacity
#: .largest_remainder_by_demand` 的 `trace["weight_kind"] == "demand"` 同义。
BACKPRESSURE_SHARE_SOURCE = "demand_ratio"

# ==================== C8-7 已删除：每施工段人数上限的 "×2.5 带" ====================
# 原第 39 轮的 `CREW_CEILING_BAND = 2.5` / `CREW_CEILING_CAP = 40` 与
# `effective_crew_max()`（`max(crew_max, min(40, ceil(crew_base × 2.5)))`）
# 属**无规范依据**的补丁，已按 C 组 C8 删除清单第 7 项整条删除（2026-09-21）。
# 每工（每台）能上多少，改由 **MWI 表 `Resource_Workface_Index`** 给出：
#     段容量 n_i = ceil(段面积 ÷ MWI)（`segment_capacity`），
#     有效容量 = min(汇总容量, 用户同类限额)（用户没给 → 不限）。
# 见 `plan_organization()` / `_mwi_rows_by_name()`。

# 机械名识别用词（只用于把资源归到"机械曲线"还是"人工曲线"）
MACHINE_KEYWORDS = (
    "塔吊", "吊车", "履带吊", "汽车吊", "挖掘机", "推土机", "压路机", "装载机",
    "自卸汽车", "泵车", "钻机", "桩机", "成槽机", "搅拌桩机", "注浆泵", "井架",
    "龙门架", "叉车", "发电机", "空压机", "振动锤", "夯机", "摊铺机", "罐车",
)

# norm_binding 里可能出现的机械名字段（与 resource.py / crew_bind.py 口径一致）
_BINDING_MACHINE_KEYS = ("machine_name", "machine", "main_machine", "equipment_name")

# 判定"没有定额锚定，工期沿用叶子原值"的 warning（AI 假设必须留痕）
_WARN_NO_NORM = "任务 %s（%s）无可用定额锚定，工期沿用叶子原值 %d 天（AI 假设，未按定额重算）"


# ==================== 小工具 ====================
def _num(value, default=None):
    """安全转 float；None/空/非法/NaN/Inf 一律返回 default（**0 是合法值**）。"""
    try:
        if value is None or value == "":
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _pos(value, default=None):
    """正数 float：<=0 视为没有（返回 default）。"""
    f = _num(value)
    if f is None or f <= 0:
        return default
    return f


def _pos_int(value, default=1):
    """正整数（向上取整）：非法或 <=0 → default。"""
    f = _num(value)
    if f is None or f <= 0:
        return default
    return int(math.ceil(f))


def _r2(value):
    """金额/工程量口径的两位小数（保证输出可逐字段比对）。"""
    return round(float(value) + 0.0, 2)


def _as_list(value):
    """dict/list → 原样；其余 → 空 list（容错各种畸形输入）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


# ==================== 输入解析 ====================
def collect_leaf_tasks(wbs):
    """收集叶子任务（口径与 resource.py / crew_bind.py 一致：无子包时工作包即叶子）。"""
    phases = (wbs or {}).get("phases") if isinstance(wbs, dict) else None
    leaves = []
    if not isinstance(phases, list):
        return leaves
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        for wp in phase.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            subs = wp.get("sub_packages") or []
            if subs:
                leaves.extend([s for s in subs if isinstance(s, dict)])
            else:
                leaves.append(wp)
    return leaves


def normalize_deps(dependencies):
    """依赖归一成 list（兼容 {"dependencies": [...]} / [...] / 空）。"""
    if isinstance(dependencies, dict):
        return _as_list(dependencies.get("dependencies"))
    return _as_list(dependencies)


def _limit_item_count(items):
    """边界条件列表里"有几项"（`{名: 值}` / `[{...}]` 都能数；看不懂 → 0）。"""
    if isinstance(items, dict):
        return len(items)
    return len(_as_list(items))


def _boundary_key_source(boundary, key):
    """`boundary_conditions["_source"]` 里某个限额键的来源（`"user"` / `"model"` / None）。

    第 40 轮：boundary 节点给 `boundary_conditions` 逐项打来源标注（`_source`）：
    `user` = 用户在自己提供的文件/参数里明确给出；`model` = 模型按"常见做法"补齐。
    **没有 `_source`（旧计划 / 调用方直接传 boundary dict）→ None**，调用方按旧行为采纳
    —— 既有用例里大量"直接传 `{"labor": {"peak_total": N}}` 期望生效"的写法都靠这条。
    """
    if not isinstance(boundary, dict):
        return None
    src = boundary.get("_source")
    if not isinstance(src, dict):
        return None
    val = src.get(key)
    return str(val).strip().lower() if val else None


def _parse_crew_text(text):
    """配员原文 → `{角色: 每台人数}`（只认"工种+数字"，其余**不猜**）。

    与 `machine_crew_of` 的解析**同一份实现**（域 7.9 起两处共用，避免两份口径漂移）。
    解析不出来（没有"角色+数字"对）→ 空字典。
    """
    out = {}
    token = ""
    idx = 0
    text = str(text or "")
    while idx < len(text):
        ch = text[idx]
        if ch.isdigit():
            digits = ""
            while idx < len(text) and (text[idx].isdigit() or text[idx] == "."):
                digits += text[idx]
                idx += 1
            role = token.strip(" +＋、,，;；/")
            num = _pos_int(digits, 0)
            if role and num > 0:
                out[role] = out.get(role, 0) + num
            token = ""
            continue
        if ch in "+＋、,，;；/ \t":
            token = ""
        else:
            token += ch
        idx += 1
    return out


#: 场地级机械登记表的缓存：`{机械名 tuple: 配员角色 tuple}`（KB 读一次就够，确定性）。
_SITE_CREW_ROLES_CACHE = {}


def _site_machine_roles_from_kb(machines):
    """域 7.9 兜底：常量块缺失时，从 KB `Equipment_Crew_Mapping` 反推配员角色名。

    **仍然不写死角色名**：读的是登记表里那几台场地级机械的 `composition` 原文，
    用与 `machine_crew_of` 同一份解析（`_parse_crew_text`）。读不到 → 空元组（不猜）。
    """
    key = tuple(sorted(str(m) for m in (machines or ())))
    if key in _SITE_CREW_ROLES_CACHE:
        return _SITE_CREW_ROLES_CACHE[key]
    roles = set()
    for machine in key:
        try:
            row = kb.crew_for_machine(machine)
        except Exception:
            row = None
        if isinstance(row, dict):
            roles.update(_parse_crew_text(row.get("composition")).keys())
    out = tuple(sorted(roles))
    _SITE_CREW_ROLES_CACHE[key] = out
    return out


def _site_machine_registry(site_const=None, limits=None):
    """域 7.8 / 7.9：**场地级机械名**与**跟台数走的机组配员角色**（判据来源）。

    ⚠️ **绝不写死资源名 / 角色名**。真源是 `boundary_conditions["site_machine_const"]`
    （`org_defaults.build_site_machine_const` 的产物）：
      · 机械名   = `site_machine_const["machines"]` 的**键集**（7.8：超限清单永不含它们）；
      · 配员角色 = 每台机械 `crew_per_unit` 的**键集**（7.9：司机 / 信号工跟台数走、不设限额）。
    取不到常量时依次退回：
      · `limits["site_machines"]` / `limits["site_crew_roles"]`（`parse_boundary_limits`
        落下的同一份投影，供只拿到 `limits` 的调用点用）；
      · `org_defaults.SITE_MACHINE_MACHINES`（**同一个登记表** —— `build_site_machine_const`
        就是按它逐台建的，不是本文件里的字面量）；
      · KB `Equipment_Crew_Mapping` 的 `composition` 解析（只为补角色名）。

    返回 `(机械名 tuple, 配员角色 tuple)`，一律 `sorted()`（确定性：无 set 迭代序）。
    """
    machines, roles = set(), set()
    blocks = [site_const]
    if isinstance(limits, dict):
        blocks.append(limits.get(org_defaults.SITE_MACHINE_CONST_KEY))
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw = block.get("machines")
        if not isinstance(raw, dict):
            continue
        for name in raw:
            text = str(name or "").strip()
            if not text:
                continue
            machines.add(text)
            entry = raw.get(name)
            if isinstance(entry, dict) and isinstance(entry.get("crew_per_unit"), dict):
                roles.update(str(r).strip() for r in entry["crew_per_unit"]
                             if str(r or "").strip())
    if isinstance(limits, dict):
        for name in (limits.get("site_machines") or ()):
            text = str(name or "").strip()
            if text:
                machines.add(text)
        for role in (limits.get("site_crew_roles") or ()):
            text = str(role or "").strip()
            if text:
                roles.add(text)
    machines.update(str(m) for m in org_defaults.SITE_MACHINE_MACHINES if str(m or "").strip())
    if not roles:
        roles.update(_site_machine_roles_from_kb(machines))
    return tuple(sorted(machines)), tuple(sorted(roles))


def parse_boundary_limits(boundary):
    """解析用户资源上限（兼容 resource.parse_boundary_conditions 的全部口径）。

    返回 {"labor_total": 总人工上限（没有则 None）, "by_trade": {工种: 上限},
          "equipment": {机械: 上限}, "user_target": 用户目标工期（没有则 None）,
          "ignored_model_values": [被来源闸门拦下、没当限额用的值（中文说明）]}

    识别的情形：
      labor.peak_total / labor_peak / peak_manpower / labor 直接给数字
      labor.by_trade / trade_peak（list[{trade|name, quantity}] 或 dict）
      equipment / equipment_peak（list[{name, quantity}] 或 dict）
      project_duration_days / user_target_days / target_duration_days（用户目标工期）

    **来源闸门（第 40 轮）**：用户实测「示例3_住宅楼.txt 原文一条资源数据都没有」，
    boundary 节点让 LLM 按"18 层住宅常见做法"补齐了 `labor.peak_total=120`，这个
    **模型补的值**却被下游当"用户限额"用来卡排程（两版工期因此失真）。判据：
      · `_source` 存在且该键 = `"model"` → **不采纳**为限额，并记进
        `ignored_model_values`（绝不静默丢弃）；
      · `_source` 存在且 = `"user"` → 照旧采纳；
      · `_source` 不存在 / 该键没标注 → 保持旧行为（照旧采纳）。
    别名（`labor_peak`/`peak_manpower`/`trade_peak`/`equipment_peak`/`user_target_days`…）
    与正键**是同一个语义**，共用同一个来源标注：只拦正键会留下"模型值换个键就进来了"
    的后门，所以整组一起判。注意 `_boundary_by_regex` 只写正键，别名是兼容旧计划用的。
    """
    result = {"labor_total": None, "by_trade": {}, "equipment": {}, "user_target": None,
              "ignored_model_values": []}
    if isinstance(boundary, str):
        import json
        try:
            boundary = json.loads(boundary)
        except Exception:
            return result
    if not isinstance(boundary, dict):
        return result

    def _model(key):
        return _boundary_key_source(boundary, key) == "model"

    def _ignore(key, value, via=None):
        result["ignored_model_values"].append(
            "%s=%s（%s来源=model，未采纳）"
            % (key, value, ("经 %s，" % via) if via and via != key else ""))

    labor = boundary.get("labor")

    # ---------- 总人工上限 ----------
    # 用户二次裁定（父代理 2026-09-21）：**申报峰值从源头删掉** —— 模型按"常见做法"
    # 补的 `labor.peak_total` 不再作为任何人数来源，只留痕（`_ignore` 必须保留）；
    # 用户明确给出的仍然采纳（`_source` 缺失 = 旧计划 / 调用方直传，沿用旧行为）。
    if _model("labor.peak_total"):
        # 来源=model → 不采纳，但**必须留痕**（模型值可能就在别名或 bare labor 上）
        for key in ("labor_peak", "peak_manpower", "labor_peak_total"):
            if boundary.get(key) is not None:
                _ignore("labor.peak_total", boundary.get(key), via=key)
                break
        else:
            if isinstance(labor, dict) and labor.get("peak_total") is not None:
                _ignore("labor.peak_total", labor.get("peak_total"))
            elif _num(labor) is not None:
                _ignore("labor.peak_total", labor)
    else:
        for key in ("labor_peak", "peak_manpower", "labor_peak_total"):
            val = boundary.get(key)
            if val is not None:
                result["labor_total"] = _pos_int(val, None)
                break
        if result["labor_total"] is None:
            if isinstance(labor, dict) and labor.get("peak_total") is not None:
                result["labor_total"] = _pos_int(labor.get("peak_total"), None)
            elif _num(labor) is not None:
                result["labor_total"] = _pos_int(labor, None)  # labor 直接给了个数字

    # ---------- 分工种上限 ----------
    trades = boundary.get("trade_peak")
    trades_via = "trade_peak"
    if not isinstance(trades, (list, dict)) and isinstance(labor, dict):
        trades = labor.get("by_trade")
        trades_via = "labor.by_trade"
    if _model("labor.by_trade"):
        # `_items_source` 是"全对才算 user"：列表里只要有一项对不上整体记 model →
        # 整张分工种限额表都不采纳（宁可少认，也不许把模型补的项冒充成用户申报）。
        n_items = _limit_item_count(trades)
        if n_items:
            _ignore("labor.by_trade", "共 %d 项" % n_items, via=trades_via)
        trades = None
    if isinstance(trades, dict):
        trades = [{"trade": k, "quantity": v} for k, v in trades.items()]
    for item in _as_list(trades):
        if not isinstance(item, dict):
            continue
        name = item.get("trade") or item.get("name") or item.get("工种")
        limit = _pos_int(item.get("quantity", item.get("limit")), None)
        if name and limit:
            # 工种名归一：边界常写"木工/砼工/杂工"，账上是"模板工/混凝土工/普工"。
            # 不归一这条上限就匹配不上任何任务，等于用户白给了限额。
            result["by_trade"][_normalize_trade(str(name))] = limit

    # ---------- 设备上限 ----------
    eq = boundary.get("equipment")
    eq_via = "equipment"
    if not isinstance(eq, (list, dict)):
        eq = boundary.get("equipment_peak")
        eq_via = "equipment_peak"
    if _model("equipment"):
        n_items = _limit_item_count(eq)
        if n_items:
            _ignore("equipment", "共 %d 项" % n_items, via=eq_via)
        eq = None
    if isinstance(eq, dict):
        eq = [{"name": k, "quantity": v} for k, v in eq.items()]
    for item in _as_list(eq):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("设备")
        limit = _pos_int(item.get("quantity", item.get("limit")), None)
        if name and limit:
            result["equipment"][str(name).strip()] = limit

    # ---------- 用户目标总工期（只当参照）----------
    for key in ("project_duration_days", "user_target_days", "target_duration_days",
                "total_duration_days", "duration_days", "工期", "总工期"):
        if boundary.get(key) is None:
            continue
        if _model("project_duration_days"):
            _ignore("project_duration_days", boundary.get(key), via=key)
            break
        result["user_target"] = _pos_int(boundary.get(key), None)
        break

    # ---------- 域 7.8 / 7.9：场地级机械常量的**判据投影**（键只增不改）----------
    # `site_machines` / `site_crew_roles` 只作判据来源（超限清单 + 配员不受限额），
    # 不参与任何数值计算；真源是 `boundary_conditions["site_machine_const"]`。
    result["site_machines"], result["site_crew_roles"] = _site_machine_registry(
        site_const=boundary.get(org_defaults.SITE_MACHINE_CONST_KEY), limits=None)
    return result



def scale_limits_per_building(limits, params):
    """把**全项目**的资源限额折算成**单栋**限额（多栋项目）。

    为什么必须折算：本计划按"标准栋"编制（层/段/工序的单层量都是单栋口径），
    模板/钢筋等工程量、工作面容量都是"一栋楼一个工作面"。而用户给的
    「总劳动力峰值 400 人」「塔吊 4 台」是**整个工地**的口径。若直接拿全场
    限额去卡单栋任务，等于假设这 400 人全挤在一栋楼里，两版工期的对比就失真了。
    折算规则：单栋限额 = 全场限额 ÷ 栋数（各栋平均分摊，下限 1）。

    返回 ``(新 limits, 中文说明列表)``；栋数 ≤1 时原样返回（不复制、不产生说明）。
    """
    from .beat_configs import building_count
    n = building_count(params)
    if n <= 1 or not isinstance(limits, dict):
        return limits, []

    def _div(v):
        try:
            return max(1, int(math.ceil(float(v) / n)))
        except (TypeError, ValueError):
            return v

    out = dict(limits)
    has_any = bool(out.get("labor_total") or out.get("by_trade") or out.get("equipment"))
    if not has_any:
        return limits, []           # 没给任何资源限额 → 没什么可折算的，别啰嗦
    if out.get("labor_total"):
        out["labor_total"] = _div(out["labor_total"])
    out["by_trade"] = dict((k, _div(v)) for k, v in (out.get("by_trade") or {}).items())
    out["equipment"] = dict((k, _div(v)) for k, v in (out.get("equipment") or {}).items())
    parts = []
    if out.get("labor_total"):
        parts.append("单栋人工上限 %d 人" % out["labor_total"])
    if out.get("equipment"):
        parts.append("单栋机械上限 " + "、".join("%s %d 台" % (k, v)
                                                for k, v in sorted(out["equipment"].items())))
    note = ("全项目共 %d 栋：用户给的是**全场**资源限额，已按各栋平均分摊折算成**单栋**限额"
            "（%s）——因为本计划按标准栋编制、各栋平行施工。"
            % (n, "；".join(parts) if parts else "无"))
    return out, [note]


def user_target_from_params(params):
    """用户目标工期也可能落在 extracted_params 里（可选输入）。"""
    if not isinstance(params, dict):
        return None
    for key in ("project_duration_days", "user_target_days", "target_duration_days",
                "total_duration_days"):
        val = _pos_int(params.get(key), None)
        if val:
            return val
    return None


# ==================== 上限匹配 ====================
def machine_type(name):
    """机械的"类型核心词"：`静力压桩机` / `静压桩机` → `桩机`；`履带式单斗液压挖掘机` → `挖掘机`。

    用于**同机异名**的绑定：用户清单写的是「静压桩机」，KB/叶子写的是「静力压桩机」，
    两串互不包含（多一个"力"），精确/包含匹配都会落空 —— 用户申报的硬约束
    被静默忽略（实测把 1 台桩机算成 2 台）。这里只取 `MACHINE_KEYWORDS` 里的
    类型词做归一，**不做宽松子串包含**（错绑比不绑更危险）。
    """
    if not name:
        return ""
    text = str(name)
    for kw in MACHINE_KEYWORDS:
        if kw in text:
            return kw
    return text.strip()


def _match_limit(name, limits):
    """用户限额落到具体资源名：精确 → 包含 → **机械类型归一**。

    返回 (限额, 匹配到的用户侧名字)；没匹配上返回 (None, None)。

    第三级是为真实计划里的同机异名准备的（见 `machine_type`）：用类型核心词
    把「静压桩机」绑到「静力压桩机」、「挖掘机」绑到「履带式单斗液压挖掘机」。
    为避免把不同设备错绑成一个上限，**只在类型词完全相同、且候选限额一致**时采用。
    """
    if not name or not isinstance(limits, dict):
        return None, None
    if name in limits:
        return limits[name], name
    for key, val in sorted(limits.items()):
        if key and (key in name or name in key):
            return val, key
    mtype = machine_type(name)
    if mtype:
        hits = [(k, v) for k, v in sorted(limits.items()) if machine_type(k) == mtype]
        if hits and len(set(v for _k, v in hits)) == 1:
            return hits[0][1], hits[0][0]
    # 兜底：两边都含同一个机械类型词（如「履带式单斗液压挖掘机」↔「挖掘机」）
    text = str(name)
    hits = [(key, val) for key, val in sorted(limits.items())
            if key and any(kw in text and kw in str(key) for kw in MACHINE_KEYWORDS)]
    if hits and len(set(v for _k, v in hits)) == 1:
        return hits[0][1], hits[0][0]
    return None, None


def equipment_binding_report(limits, resource_names):
    """用户申报的设备 → 绑到了计划里的哪个机械资源（逐项可对账）。

    返回 `{用户申报名: {"declared": 台数, "bound_to": 计划里的资源名或 None,
                        "effective": 是否生效, "note": 中文说明}}`。

    为什么要逐项报：用户清单里「静压桩机」和计划里的「静力压桩机」不是子串关系，
    旧实现静默丢弃了这条硬上限（实测 1 台被算成 2 台）。**匹配不上必须显式说明**，
    不允许再出现"用户申报了、排程当没看见"。
    """
    out = {}
    names = [str(n) for n in (resource_names or []) if n]
    for key, val in sorted((limits or {}).get("equipment", {}).items()):
        bound = None
        for rname in names:
            if rname == key or key in rname or machine_type(rname) == machine_type(key):
                bound = rname
                break
        if bound is not None:
            out[str(key)] = {
                "declared": val, "bound_to": bound, "effective": True,
                "note": "已绑定到计划资源「%s」，限额 %s 生效" % (bound, val)}
        else:
            out[str(key)] = {
                "declared": val, "bound_to": None, "effective": False,
                "note": "用户申报的「%s」未匹配到计划中的任何机械资源，该限额未生效"
                        % key}
    return out


def unmatched_equipment_warnings(report):
    """未匹配上的用户设备限额 → 中文告警（**绝不静默丢弃**）。"""
    out = []
    for key, rec in sorted((report or {}).items()):
        if not rec.get("effective"):
            out.append(
                "用户申报的设备「%s」（%s 台）未匹配到计划中的任何机械资源，"
                "该限额未生效 —— 请核对设备名称或补充对应的机械资源"
                % (key, rec.get("declared")))
    return out


def is_machine_name(name):
    """机械名判定（只在两处用到：是否按机械容差检查、归入机械曲线）。"""
    if not name:
        return False
    text = str(name)
    return any(kw in text for kw in MACHINE_KEYWORDS)


# ==================== 叶子台账（工作面容量 / 工种）====================
def resolve_workface(leaf):
    """工作面容量标定行：**只取叶子自带的值**（域 1.6 已删 `Workface_Capacity_Rule`）。

    叶子自带的就是唯一来源（用户/上游显式给的容量仍然说话）。域 1.6（第 6 批）
    删表后**不再从 KB 补齐** —— 原先的 `kb.workface_capacity(kid)` 查的是已删的表，
    恒定返回 None，只会制造"已经补齐过了"的假象；而 `Resource_Workface_Index`
    （MWI，67 行）按**资源名**建键、量纲是 m²/人，回答的是"一人要多大工位"，
    无法回答"这条活动每班最多几人"，两者不存在等价迁移。
    容量主口径见 `segment_capacity.segment_capacity`（段面积 ÷ MWI，域 7.1/7.11）。
    """
    wf = leaf.get("workface_capacity")
    wf = dict(wf) if isinstance(wf, dict) else {}
    return wf or None


# ==================== 工作面容量（契约 §5-WS4 ⑤）====================
def resolve_workface_rule(leaf):
    """一条叶子的工作面容量标定行（**只取叶子自带的**，不再回查 KB）。

    与 `resolve_workface` 同一口径，保留别名只是为了让"容量公式"这一段读起来
    自洽：容量不再是一张固定人数表，而是**随本施工段工程量变化**的标定行。
    """
    return resolve_workface(leaf)


# 上游**显式**声明"这条任务代表几个并行施工段"时用的键（顺序即优先级）。
_SEGMENT_KEYS = ("segments", "_segments", "segment_count", "workface_segments",
                 "segment_total", "_segment_total", "parallel_segments",
                 "_parallel_segments")


def _segment_count(leaf):
    """本叶子背后**并行施工段**数（取不到 → 1，即不放大）。

    实测事实（`terminal/plans/plan_run_1789827002.json`，322 条任务）：
    **没有任何一条叶子**带 `segments/_segments/segment_count/workface_segments` 之类的
    段数键 → 本函数对全部真实叶子都返回 1。这不是"漏了"，而是契约口径：

      · 节拍展开（`layer_engine._beat_leaf`）产出的叶子 id 是 `{node}.{z}.{s}.{k}`，
        每个 (分区 z, 施工段 s, 工序 k) **各成一条任务**；同段工序串行、跨段搭接由
        `structural_deps` 写成显式依赖，同一时刻并行几段由串行排程（Serial SGS）在
        资源池上决定。排程期再乘一遍段数 = 重复计数（同一份工程量被放大 N 次），
        所以**这里必须是 1**。
      · 契约 §5-WS4 ⑤ 的 `cap_total = cap_labor × 并行段数` 是"**整条活动跨段合计**"
        的口径；本函数的调用点是**单条任务（单段）**的人数，所以段数取 1、
        `cap_total == cap_labor`。既有断言 `test_segment_quantity_divides_by_parallel_segments`
        锚定的正是"单段口径"：段数 2 时按 Q/2 算出来的是**每段** 9 人，不是合计 18 人。
      · 只有上游显式给了段数（`_SEGMENT_KEYS`）才按它折减单段量 —— 那是"一条任务代表
        多段"的另一种建模，与节拍展开互斥；派生的 `_zone`/`_segment` **不算**段总数
        （它们是"本条是第几段"，段总数 = 层数 ÷ 每段层数，存在节拍配置
        `beat_configs.segment_floors` 里，从不回写叶子）。

    闸门：`segments_factor == 0` 时本函数的结果被 `parallel_segment_count` 压回 1
    （见 `segments_parallel_enabled` 的语义查证）。
    """
    if not isinstance(leaf, dict):
        return 1
    for key in _SEGMENT_KEYS:
        n = _pos_int(leaf.get(key), 0) or 0
        if n > 0:
            return n
    return 1


def segments_parallel_enabled(workface):
    """`segments_factor` 的语义（第 39 轮查证结论）——**0/1 闸门，不是折减系数**。

    结论与证据（可复核）：
      · `docs/修改契约_v1.md` §4 的列注释写 `segments_factor,  # 0/1`，DDL 是
        `segments_factor INTEGER DEFAULT 1`；§5-WS4 ⑤ 只定义了 `segments_factor == 1`
        时的放大（`cap_total = cap_labor × 并行段数`），**没有**任何"× segments_factor"
        的写法。若它真是折减系数，`0` 会把容量乘成 0（"这一段不许站人"）—— 说不通。
        → 它是"要不要按施工段并行放大"的**开关**，默认开。
      · `kb.workface_capacity()` 的 docstring 写"`segments_factor` 是施工段折减"，与上面
        的 0/1 定义域不一致 → **以实际取值分布为准**：全表 478 行**全部是 1**
        （只读统计见 `backend/tools/recalibrate_workface_ceiling.py --stats` 与
        `backend/tests/test_workface_segments.py`）。既不是折减、也不是段数，这一列
        目前不携带任何信息；本函数只是把契约语义**实现出来**，在当前库上零影响。
      · 闸门取假（0 / False / 空串）→ "本活动不按施工段并行" → 调用方把并行段数按 1 算。
      · 缺列 / 缺行 / 值非法（老库、旧计划里没有这一列）→ 按"真"处理（缺省就是 1）。
    """
    if not isinstance(workface, dict) or "segments_factor" not in workface:
        return True
    v = workface.get("segments_factor")
    if v is None or v == "":
        return True
    try:
        return float(v) != 0.0
    except (TypeError, ValueError):
        return True


def parallel_segment_count(leaf, workface=None):
    """本任务参与并行的施工段数（段数键 ∩ `segments_factor` 闸门）—— 单一口径入口。

    `Q_seg = 总量 ÷ 本函数`（契约 §5-WS4 ⑤）；返回 1 时 `Q_seg = 总量`，不折减也不放大。
    """
    if not segments_parallel_enabled(workface):
        return 1
    return max(1, _segment_count(leaf))


# C8-7 已删除：`effective_crew_max(crew_base, crew_min, crew_max)`
#   = max(crew_max, min(CREW_CEILING_CAP=40, ceil(crew_base × CREW_CEILING_BAND=2.5)))
# 这条"×2.5 带"**无规范依据**，连同两个常量一起删（2026-09-21）。
# 每工能上多少改由 MWI 表给出，见 `plan_organization()`。
# 原调用点 `workface_limits_from_rule` 的人工侧重定也一并去掉（见该函数）。


def _capacity_quantity(workface, binding=None, leaf=None):
    """容量公式用的工程量单位（标定行优先，其次定额行，最后叶子单位）。

    标定行的 `quantity_unit` 是**标定口径**（v2 按它标定 q_ref/step：m、m²、m³、t…），
    所以它排在最前：叶子的量纲若与标定口径不同（实测 2.1.1 叶子按「根」计量、
    标定行按「m」），公式必须按标定口径判可用性，不能拿叶子的单位去套。
    """
    for src in (workface, binding, leaf):
        if isinstance(src, dict):
            u = src.get("quantity_unit")
            if u:
                return str(u)
    return ""


def workface_capacity_for_qty(workface, quantity, quantity_unit, ctx=None,
                              kind="labor", factor=None, crew_ceiling=None):
    """工作面容量（一个施工段最多几个人 / 几台机）——随工程量大小变化。

    契约 §5-WS4 ⑤：`cap = clamp(base + step_n × ⌊(Q_seg − q_ref) / step_q⌋,
                                min, max)`，`Q_seg = 总量 ÷ 段数`。
    契约（§4 标定）列出的容差都在这条公式里；`crew_step_n == 0`
    （现库 95 行，单位是项/樘/块/台/座之类，容量与工程量无关）退化为常量
    `cap = clamp(base, min, max)` —— 不除零、不抛异常。

    **跨族换算**（P0-3）：标定行的 `quantity_unit` 是标定口径（如 `GD_A13_压管桩`
    按 m 标定），而叶子的量可能是「根」——`factor` = 绑定层给出的换算系数
    （根 → m，例：桩长 18 m/根）。先 `Q′ = Q × factor` 再用标定口径代入公式；
    factor 为 None 且量纲不符时返回 None（调用方回退旧键并**写明原因**，绝不静默）。

    **`crew_ceiling`（仅人工分支）**：夹取用的上限。默认 `None` =
    标定行的原 `crew_max`，即**契约 §5-WS4 ⑤ 的纯公式**（既有回归用例
    `test_workface_v2.py` 锚定的就是这个口径：`crew_max=15` 时 99999 夹到 15）。
    ⚠️ C8-7（2026-09-21）：原先共用入口会传入 `effective_crew_max(...)`（×2.5 带）
    的结果，**该口径已删除** → 现在一律是裸 `crew_max`。
    本函数的人工结果在新链路里只作**兜底**（MWI 表缺该资源/缺层面积时才用到），
    容量主口径见 `plan_organization()`。
    """
    kind = "machine" if str(kind).lower() == "machine" else "labor"
    if not isinstance(workface, dict) or not workface:
        return None
    # 工程量 0 是合法值（tail 项、纯措施项）：按公式走完再夹取，
    # 不能因为 _pos() 把 0 当"没有"就整条判不可用。
    q = _pos(quantity, 0.0)
    if q is None:
        return None
    # 量纲必须能对齐才允许套公式（m² 的工程量套 m 的标定行毫无意义）。
    # ⚠️ 比较基准是 `quantity_unit`（**叶子的工程量单位**），不是标定行的
    # `quantity_unit` —— `_capacity_quantity()` 返回的是后者，两者可能不一致
    # （实测 2.1.1：标定行按 m 标定、叶子按「根」计量），拿标定单位来自比会
    # 永远进不了换算分支（Q′ 没乘 factor → 台数永远按 1 算）。
    wu = str(workface.get("quantity_unit") or "")
    uu = str(quantity_unit or "")
    if wu and uu and kb_units.normalize_unit(wu) != kb_units.normalize_unit(uu):
        conv = _pos(factor)
        if conv is None:
            return None
        q = float(q) * conv          # 换算到标定口径（Q′）

    if kind == "machine":
        base = _pos(workface.get("machine_base"))
        cap_min = _pos(workface.get("machine_min"))
        cap_max = _pos(workface.get("machine_max"))
        step_q = _pos(workface.get("machine_step_q"))
        step_n = _pos(workface.get("machine_step_n"))
        q_ref = _pos(workface.get("machine_q_ref"))
        if base is None and cap_max is None and cap_min is None:
            return None
        if base is None:
            base = cap_max if cap_max is not None else cap_min
    else:
        base = _pos(workface.get("crew_base"))
        cap_min = _pos(workface.get("crew_min"))
        cap_max = _pos(workface.get("crew_max"))
        step_q = _pos(workface.get("crew_step_q"))
        step_n = _pos(workface.get("crew_step_n"))
        q_ref = _pos(workface.get("q_ref"))
        if base is None and cap_max is None and cap_min is None:
            # 兼容旧表：只有一张"每施工段最多几人"的定值表
            old = _pos(workface.get("max_labor" if kind == "labor" else "max_machine"))
            return old
        if base is None:
            base = cap_max if cap_max is not None else cap_min
        # 第 39 轮：裸 `crew_max` → 上限重定值（**只动人工分支**，机械台数不碰：
        # 台数直接决定工期，没有"一个施工段站得下几台"的经验依据，不许照搬）。
        if crew_ceiling is not None:
            cap_max = _pos(crew_ceiling)
    if base is None:
        return None

    value = float(base)
    if step_n and step_q and step_q > 0:
        anchor = float(q_ref) if q_ref is not None else 0.0
        value = float(base) + float(step_n) * math.floor((float(q) - anchor) / float(step_q))
    return int(_clamp_num(round(value), cap_min, cap_max))


def _clamp_num(value, lo, hi):
    """数值夹取；`lo`/`hi` 缺失时单边夹取，`hi` 小于 `lo` 时以 `hi` 为准。"""
    v = float(value)
    if lo is not None and hi is not None and float(hi) < float(lo):
        lo, hi = hi, lo
    if hi is not None:
        v = min(v, float(hi))
    if lo is not None:
        v = max(v, float(lo))
    return v


def workface_limits_from_rule(leaf, quantity, quantity_unit, ctx=None):
    """一条叶子的工作面容量 → `(cap_labor, cap_machine)`（都可能为 None）。

    `cap_labor`：本施工段最多几人（v2 标定公式；v2 缺项 → 旧表 `max_labor`）。
    `cap_machine`：本施工段最多几台主控机械（同口径，机械侧同样随工程量变化）。

    ⚠️ C8-7（2026-09-21）：原先人工侧的上限会被 `effective_crew_max(...)`（×2.5 带）
    抬高，**该口径已删除** → 现在一律是标定行的裸 `crew_max`。
    机械侧本来就不受那个口径影响（台数直接决定工期）。
    本函数的人工结果在新链路里只作**兜底**，容量主口径见 `plan_organization()`。

    标定行 = 叶子上已有的 `workface_capacity`（域 1.6 已删 Workface_Capacity_Rule，
    **不再从 KB 补齐**）。叶子上的键就是唯一来源（用户/上游显式给的容量仍然说话）。
    """
    rule = resolve_workface_rule(leaf)
    if not isinstance(rule, dict) or not rule:
        return None, None
    binding = leaf.get("norm_binding") if isinstance(leaf, dict) else None
    binding = binding if isinstance(binding, dict) else {}
    # `quantity_unit` 传**叶子自己的工程量单位**（如「根」），不是标定行的口径：
    # 公式内部要拿它和标定行 `quantity_unit` 比对，不一致时才用 factor 换算（P0-3）。
    unit = str((leaf or {}).get("unit") or quantity_unit or "")
    q_seg = _pos(quantity)
    if q_seg is None:
        return (_pos(rule.get("max_labor")), _pos(rule.get("max_machine")))
    # 段数走单一口径入口（`segments_factor` 闸门 ∩ 显式段数键）：
    # 全库 478 行 `segments_factor` 都是 1、且计划里 322/322 叶子没有段数键 → 现值为 1，
    # 即 Q_seg = 总量（不折减、不放大）。见 `_segment_count` / `segments_parallel_enabled`。
    n_seg = parallel_segment_count(leaf, rule)
    q_seg = float(q_seg) / float(max(1, n_seg))
    # 跨族换算系数（P0-3）：绑定层已经算好并写了 `convert_factor`（如 根→m 的桩长 18）。
    # 标定口径与叶子量纲不一致时就靠它把 Q_seg 换算到标定口径，机械侧同样要吃。
    factor = _pos(binding.get("convert_factor"))
    # 第 39 轮：人工上限重定（`effective_crew_max`）在**共用入口**传入 —— 排程、资源、
    # 修订三条路因此仍共用同一个上限（单一真源不破），而 `workface_capacity_for_qty`
    # 的默认口径仍是契约 §5-WS4 ⑤ 的纯公式（既有回归用例锚定的那个口径）。
    # ⚠️ C8-7（2026-09-21）：原先这里把上限重定为
    # `effective_crew_max(...)`（max(crew_max, min(40, ceil(crew_base×2.5)))），**已删**。
    # 现在一律用标定行的裸 `crew_max`（契约 §5-WS4 ⑤ 的纯公式）。
    # 本函数的人工结果在新链路里只作**兜底**：有 MWI 表行时容量来自
    # `segment_capacity.segment_capacity`，见 `plan_organization()`。
    cap_labor = workface_capacity_for_qty(
        rule, q_seg, unit, ctx, kind="labor", factor=factor)
    cap_machine = workface_capacity_for_qty(rule, q_seg, unit, ctx, kind="machine",
                                           factor=factor)
    # ⚠️ 旧键 (`max_labor`/`max_machine`) **只在 v2 标定算不出来时**才当兜底。
    # `kb.workface_capacity()` 为了向后兼容把旧键并进返回字典，但旧表的值是**同族
    # 中位**（标定 v2 时的输入），不是这条活动自己的上限：两者同取会把 v2 的
    # crew_max=15 悄悄换成旧表的 10（实测铝模 5.1.1.2：v2 算 13 人 → 旧键压回 10 人）。
    # 契约 §5-WS4 期望的就是 v2 公式值，所以这里**只做回退，不做 min**。
    if cap_labor is None:
        cap_labor = _pos(rule.get("max_labor"))
    if cap_machine is None:
        cap_machine = _pos(rule.get("max_machine"))
    return cap_labor, cap_machine


def resolve_labor_types(leaf, binding):
    """该叶子的人工工种：binding.labor_types → leaf.labor_types → 查 KB。"""
    for src in (binding, leaf):
        if not isinstance(src, dict):
            continue
        vals = src.get("labor_types")
        if isinstance(vals, list):
            got = [str(v) for v in vals if v]
            if got:
                return got
    kid = None
    for src in (leaf, binding):
        if isinstance(src, dict) and src.get("kb_activity_id"):
            kid = str(src["kb_activity_id"])
            break
    if kid:
        try:
            lab = kb.labor_type_for_activity(kid) or {}
        except Exception:
            lab = {}
        got = [str(v) for v in (lab.get("labor_types") or []) if v]
        if got:
            return got
    return []


def split_crew(crew, crew_kind, known_labor):
    """把 norm_binding 的 crew 拆成 (机械配员 {名: 数}, 人工工种 [名])。

    判定顺序：显式 crew_kind 标记 → 已在 labor_types 里 → 名字本身是机械名 → 人工。
    （与 resource._split_crew 的口径一致，人工不重复计入机械配员。）
    """
    machines = {}
    labors = []
    if not isinstance(crew, dict):
        return machines, labors
    kinds = crew_kind if isinstance(crew_kind, dict) else {}
    for role, raw in crew.items():
        if not role:
            continue
        role = str(role)
        kind = kinds.get(role)
        value = raw
        if isinstance(raw, dict):
            kind = raw.get("kind") or kind
            value = raw.get("count", raw.get("size", 0))
        count = _pos_int(value, 0)
        if count <= 0:
            continue
        if kind is not None:
            is_labor = str(kind).strip().lower() == "labor"
        elif role in known_labor:
            is_labor = True
        else:
            is_labor = not is_machine_name(role)
        if is_labor:
            if role not in labors:
                labors.append(role)
        else:
            machines[role] = machines.get(role, 0) + count
    return machines, labors


def binding_machine_name(leaf, binding):
    """主控机械名：norm_binding 里已有就用它，否则查 KB 主控机械表。"""
    for src in (binding, leaf):
        if not isinstance(src, dict):
            continue
        for key in _BINDING_MACHINE_KEYS:
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    kid = leaf.get("kb_activity_id")
    if kid:
        try:
            rows = kb.main_machine(str(kid))
        except Exception:
            rows = None
        if rows and rows[0].get("machine_name"):
            return str(rows[0]["machine_name"])
    return "主控机械"


def machine_crew_of(leaf, machine_name):
    """机械配员（每台机械的司机/信号工等）：优先用叶子上已有的配员数据。

    数据来源（按可靠度排序）：叶子 machine_crew（crew_bind 从 KB 配员表写入）→
    norm_binding.crew 里被标成机械的角色 → KB Equipment_Crew_Mapping。
    **不瞎编**：KB 拿不到就返回空，只让主控机械进资源曲线。
    """
    crew = leaf.get("machine_crew")
    out = {}
    if isinstance(crew, dict):
        for role, cnt in crew.items():
            n = _pos_int(cnt, 0)
            if role and n > 0:
                out[str(role)] = n
    if out:
        return out
    if not machine_name:
        return {}
    try:
        row = kb.crew_for_machine(str(machine_name))
    except Exception:
        row = None
    if not row:
        return {}
    text = str(row.get("composition") or "")
    # 只解析"工种+数字"这种最简单的情形；解析不出来返回空（不猜数字）。
    # 与 `_parse_crew_text` 是**同一份实现**（域 7.9 起两处共用，防止口径漂移）。
    return _parse_crew_text(text)


# ==================== 排程核心 ====================
def topo_order(task_ids, preds):
    """Kahn 拓扑排序；队列按 task_id 排序 → 顺序完全确定。

    有环时把剩下的任务按 id 排序补在末尾（记由调用方 warning），绝不丢任务。
    """
    ids = sorted(task_ids)
    id_set = set(ids)
    indeg = {}
    succs = dict((tid, []) for tid in ids)
    for tid in ids:
        plist = set(p["task_id"] for p in preds.get(tid, [])
                    if isinstance(p, dict) and p.get("task_id") in id_set)
        indeg[tid] = len(plist)
        for pred in plist:
            succs.setdefault(pred, []).append(tid)
    queue = sorted([tid for tid in ids if indeg.get(tid, 0) == 0])
    order = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in sorted(succs.get(cur, [])):
            if nxt not in indeg:
                continue
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    if len(order) < len(ids):
        rest = [tid for tid in ids if tid not in set(order)]
        order.extend(sorted(rest))
        return order, True
    return order, False


def earliest_day(tid, preds, es_map, ef_map):
    """按依赖算最早可开工日：FS 用 前驱 ef + lag，SS 用 前驱 es + lag。"""
    earliest = 0
    for dep in preds.get(tid, []):
        pid = dep.get("task_id")
        if pid not in ef_map:
            continue
        lag = _num(dep.get("lag"), 0) or 0
        if dep.get("type") == "SS":
            earliest = max(earliest, es_map.get(pid, 0) + lag)
        else:
            earliest = max(earliest, ef_map.get(pid, 0) + lag)
    return int(earliest)


def feasible_start(earliest, dur, spans, plan, horizon):
    """在 [earliest, ...] 里找最早的连续可排期起点。

    判定规则（与需求完全一致）：候选窗口 [d, d+dur-1] 里每一天，所有相关资源的
    当日累计用量 + 本任务用量 <= 上限。上限取 plan["limits"]（资源池上限，
    口径见 _run_one_version 的注释）。

    做法：把已排任务按资源整理成"并发冲突区间"（区间并集），同一区间内并发数已达
    池上限 → 该区间整段不可用；于是只需检查候选窗口是否与任何冲突区间重叠 ——
    这样既不用逐日回溯，也不受"总工期特别长"影响。
    候选起点只需取 earliest 与各冲突区间的右端点（那些位置冲突刚解除）。

    horizon 是"最多往后找多少天"（调用方按该资源的剩余总工程量给）；
    上界内排不下时返回能找到的最晚可用点（不硬塞到 earliest 去制造冲突），
    并由调用方记 warning 留痕。
    """
    if dur <= 0:
        return int(earliest)
    resources = plan.get("resources") or {}
    if not resources:
        return int(earliest)                 # 不占资源：最早日即可
    limits = plan.get("limits") or {}

    # 本任务自己就超上限的资源：单日需求 > 池上限时"每天都不合适"，
    # 但仍要给它一个确定的落点（否则排程会永远找不到位置）。
    blocked = set()
    for rname, need in resources.items():
        if need > limits.get(rname, DEFAULT_CEILING) + 1e-9:
            blocked.add(rname)
    if len(blocked) == len(resources):
        return int(earliest)

    span_limit = int(earliest) + max(2 * int(dur) + 20, int(horizon))

    # 可用区间 = [earliest, span_limit] 逐资源减去"已被占满"的区间
    avail = [(int(earliest), int(span_limit) + 1)]
    for rname in resources:
        if rname in blocked:
            continue                               # 该资源本任务独占即可，不算障碍
        for bs, be in spans.get(rname, ()):
            avail = _subtract(avail, (bs, be))
        if not avail:
            break

    # 找第一段长度 >= dur 的可用区间
    last_ok = int(earliest)
    for start, end in avail:
        if start <= span_limit:
            last_ok = max(last_ok, start)
        if end - start >= dur:
            return int(start)
        if end - 1 <= span_limit:
            last_ok = max(last_ok, end - 1)
    # 上界内确实排不下：落在最后一个可行的起点（不硬塞进冲突区间去制造超限），
    # 调用方会据此记一条中文 warning 留痕。
    return int(min(last_ok, span_limit))


def _subtract(avail, blocked):
    """从可用区间列表里减去一个被占满的区间（列表按升序、互不相交）。"""
    bs, be = blocked
    out = []
    for start, end in avail:
        if be <= start or bs >= end:               # 不相交
            out.append((start, end))
            continue
        if start < bs:
            out.append((start, min(bs, end)))
        if end > be:
            out.append((max(be, start), end))
    return out


def _merge_intervals(intervals):
    """区间排序合并（输入可为空）。"""
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _overlaps_window(spans, rname, day, dur):
    """某资源在 [day, day+dur-1] 里是否已被占满（用于排程 warning 的措辞）。"""
    for bs, be in spans.get(rname, ()):
        if bs < day + dur and be > day:
            return True
    return False


def resource_spans(rows, plan_data):
    """按资源整理"并发冲突区间"（区间并集）。

    对每种资源，把同时占用它的任务区间并起来；只有"并发数已达池上限"的区间才算冲突
    （例如钢筋工池上限 10 人、每条任务要 10 人 → 任意两条同时占用即冲突）。
    返回 {资源名: [(start, end), ...]}（升序、已合并）。
    """
    per_resource = {}
    for tid, row in rows.items():
        plan = plan_data.get(tid) or {}
        limits = plan.get("limits") or {}
        for rname, need in (plan.get("resources") or {}).items():
            cap = limits.get(rname, DEFAULT_CEILING)
            if need <= 0 or cap <= 0:
                continue
            capacity_slots = int(cap // need)
            if capacity_slots <= 0:
                capacity_slots = 1
            per_resource.setdefault(rname, []).append(
                (row["es"], row["ef"], capacity_slots))
    spans = {}
    for rname, items in per_resource.items():
        spans[rname] = _conflict_spans(items)
    return spans


def _conflict_spans(items):
    """把 (start, end, 可容纳并发数) 列表压成"并发达上限"的区间列表。

    做法：每条"槽位仅为 1"的区间都在时间轴上 +1（表示"它一个人就占满了池"），
    差分扫描出并发数 > 0 的连续区间 —— 这些区间里再塞一个新任务必然超池上限。
    可容纳 >= 2 条的区间不必登记：新任务照样能挤进去（当日余量检查在其他地方兜底）。
    """
    deltas = {}
    for start, end, cap in items:
        if cap > 1:
            continue
        deltas[start] = deltas.get(start, 0) + 1
        deltas[end] = deltas.get(end, 0) - 1
    out = []
    cur = 0
    open_start = None
    for day in sorted(deltas):
        cur += deltas[day]
        if cur > 0 and open_start is None:
            open_start = day
        elif cur <= 0 and open_start is not None:
            out.append((open_start, day))
            open_start = None
    if open_start is not None and deltas:
        out.append((open_start, max(deltas) + 1))
    return out


def _total_labor_of(plan):
    """该任务占用的**人工**人数 = 当天为这条任务出勤的**全部人工**（工种 + 机械配员）。

    口径必须与逐日曲线（`daily_curves`）和交付物（`delivery`：LABOR ∪ MACHINE_CREW = 人）
    **完全一致**，否则同一份计划会给出两个"人工峰值"：
      · 人工主导：`resources` 就是各工种人数，全算；
      · 机械主导：主资源是**机械**（塔吊 / 泵车…）不能算人，但 `machine_crew_of`
        写进 `resources` 的配员（司机 / 信号工 / 泵工 / 操作工…）是**人**，必须算进来。
    历史坑：旧实现只算人工主导任务，于是塔吊上的司机/信号工既不进曲线、也不受用户给的
    `labor.peak_total` 约束 —— 用户设"全项目同时在岗 8 人"，曲线却报 20 人还没有一条
    over_limit（用户看到的"资源少"就是这个口径差）。
    """
    if plan.get("resource_kind") != "labor":
        machines = plan.get("resources") or {}
        total = 0.0
        for role, cnt in machines.items():
            if is_machine_name(role):
                continue                      # 机械本身不是人
            try:
                total += float(cnt)
            except (TypeError, ValueError):
                continue
        return total
    try:
        return float(sum(float(v) for v in (plan.get("resources") or {}).values()))
    except (TypeError, ValueError):
        return 0.0


def earliest_window_within_total(start, dur, need, labor_day, limit, max_scan):
    """把起点往后推，直到窗口 [d, d+dur-1] 内**每一天**的在岗人工 + need ≤ 上限。

    返回 ``(d, ok)``；ok=False 表示在 max_scan 内排不下（调用方按"最早可行日"
    落地并记 warning，绝不静默超限）。

    复杂度：内层扫到第一个"当天会超"的日就停，然后跳到它之后一天 —— 每天最多
    被扫常数次，因此与工期长短无关，不会因为计划有几千天而变慢。
    """
    d = int(start)
    limit = float(limit)
    bound = int(start) + max(1, int(max_scan))
    while d <= bound:
        bad = None
        for x in range(d, d + int(dur)):
            if labor_day.get(x, 0.0) + need > limit + 1e-9:
                bad = x
                break
        if bad is None:
            return d, True
        d = bad + 1
    return int(start), False


def serial_sgs(order, plan_data, preds, warnings, tag, total_labor_limit=None):
    """串行排程（Serial SGS）：返回 {task_id: 行}、逐日用量、总工期、排不下的任务。

    逐条按拓扑序安排：earliest 由依赖决定，之后在"当天资源有余量"的前提下
    找最早的连续可排期起点（见 feasible_start / resource_spans）。
    每条任务的"放弃上界"由它占用资源的**剩余总工程量**决定：
    剩余工程量 ÷ 每日上限 = 同一工种排完还要多少天，不可能比这更晚才轮到它。

    total_labor_limit（可选）＝用户给的**全项目同时在岗人工上限**（labor.peak_total）。
    它是"逐日并发"口径的天花板，不是"把额度按任务数平均分掉"——后者会让 400 条
    任务每条只分到 1 人，把工期从 600 天抬到 7000 天（实测踩过这个坑）。
    """
    es_map, ef_map, rows = {}, {}, {}
    infeasible = []
    labor_day = {}                    # 日 → 当天在岗人工（仅人工主导任务）

    # 各资源剩余总工程量（工日 / 台班）：本任务自己也算在内，作为排队上界
    remaining = {}
    for tid in order:
        plan = plan_data.get(tid) or {}
        for rname, need in (plan.get("resources") or {}).items():
            remaining[rname] = remaining.get(rname, 0.0) + float(need) * \
                max(1, int(plan.get("duration") or 1))

    for tid in order:
        plan = plan_data.get(tid)
        if plan is None:
            continue
        dur = max(1, int(plan.get("duration") or 1))
        limits = plan.get("limits") or {}
        earliest = earliest_day(tid, preds, es_map, ef_map)
        horizon = 0
        for rname, need in (plan.get("resources") or {}).items():
            cap = limits.get(rname, DEFAULT_CEILING)
            total = remaining.get(rname, 0.0)
            if cap > 0:
                horizon = max(horizon, int(math.ceil(total / cap)) + dur)
        spans = resource_spans(rows, plan_data)
        start = feasible_start(earliest, dur, spans, plan, horizon)
        # ---- 总人工上限：逐日复核 + 顺延（并发口径，不摊派）----
        need_labor = _total_labor_of(plan)
        if total_labor_limit and need_labor > 0:
            max_scan = max(2 * dur + 30, min(int(horizon) + dur, 5000))
            new_start, ok = earliest_window_within_total(
                start, dur, need_labor, labor_day, total_labor_limit, max_scan)
            if new_start != start or not ok:
                plan.setdefault("capped", []).append({
                    "task_id": tid, "resource": "总人工",
                    "want": need_labor, "got": need_labor,
                    "reason": ("总人工限额（%d 人）：该任务需 %g 人，与已在施任务争用后"
                               "已顺延到第 %d 天起（原第 %d 天）"
                               % (int(total_labor_limit), need_labor, new_start, start))
                    if ok else
                    ("总人工限额（%d 人）：该任务需 %g 人，向后 %d 天内找不到可容下它的"
                     "窗口，已按最早可行日安排，当日将超过上限（请放宽限额或拆分任务）"
                     % (int(total_labor_limit), need_labor, max_scan)),
                })
                if not ok:
                    warnings.append(
                        "%s：任务 %s 需 %g 人，在总人工限额 %d 人下向后 %d 天内无可行窗口，"
                        "已按第 %d 天安排并记入 over_limit（不静默超限）"
                        % (tag, tid, need_labor, int(total_labor_limit), max_scan, new_start))
            start = new_start
        if start > earliest and plan.get("resources"):
            # 起点 earliest 排不下 → 被迫顺延（同一工种/机械当天名额已满），必须留痕
            blocked = [r for r, need in plan["resources"].items()
                       if _overlaps_window(spans, r, earliest, dur)]
            if blocked:
                rname = sorted(blocked)[0]
                infeasible.append({
                    "task_id": tid,
                    "resource": rname,
                    "want": _r2(plan["resources"].get(rname, 0)),
                    "limit": _r2(limits.get(rname, DEFAULT_CEILING)),
                })
                warnings.append(
                    "%s：任务 %s（%s）在依赖允许的最早日期（第 %d 天）与已在施任务争用 %s，"
                    "已顺延到第 %d 天起（%s 当日余量为 0）"
                    % (tag, tid, plan.get("name") or tid, earliest, rname, start, rname))
        es_map[tid] = start
        ef_map[tid] = start + dur
        rows[tid] = {
            "task_id": tid,
            "es": start,
            "ef": start + dur,
            "crew": dict(plan.get("crew") or {}),
            "capped": bool(plan.get("capped")),
        }
        if plan.get("organization"):
            # 施工组织层结果（契约 §2）：**只在取到节拍时**写这一行键，
            # 无节拍路径的排程行与改动前逐位相同。
            rows[tid]["_organization"] = plan["organization"]
        # 裁定 B（2026-09-21）：容量来源**逐行可追溯**，两态之一 ——
        # `"mwi"` / `"reported_missing"`（域 1.6 收敛，绝不静默）。
        if plan.get("capacity_source"):
            rows[tid]["capacity_source"] = plan["capacity_source"]
        if plan.get("capacity_basis"):
            rows[tid]["capacity_basis"] = plan["capacity_basis"]
        if total_labor_limit and need_labor > 0:      # 记入当天在岗人工台账
            for day in range(start, start + dur):
                labor_day[day] = labor_day.get(day, 0.0) + need_labor
        for rname, need in (plan.get("resources") or {}).items():
            remaining[rname] = max(0.0, remaining.get(rname, 0.0) - need * dur)

    total = max([r["ef"] for r in rows.values()] or [0])
    return {
        "rows": rows,
        "usage": day_usage(rows, total),
        "total_duration_days": int(total),
        "infeasible": infeasible,
    }


def day_usage(rows, total):
    """day -> {资源名: 当日用量, task_id: 1}（从最终排程一次性算出来）。"""
    usage = {}
    for tid in sorted(rows):
        row = rows[tid]
        for day in range(row["es"], row["ef"]):
            slot = usage.setdefault(day, {})
            slot[tid] = 1
            for rname, need in (row.get("resources") or {}).items():
                slot[rname] = _r2(slot.get(rname, 0) + need)
    return usage


def longest_chain(rows, preds):
    """用 ef 最长链推关键路径（确定性：并列时取 task_id 最小的分支）。"""
    if not rows:
        return []
    ends = sorted(rows.values(), key=lambda r: (r["ef"], r["task_id"]))
    last = ends[-1]["task_id"]
    chain = []
    cur = last
    guard = 0
    while cur is not None and guard <= len(rows):
        guard += 1
        chain.append(cur)
        best = None
        for dep in sorted(preds.get(cur, []), key=lambda d: str(d.get("task_id"))):
            pid = dep.get("task_id")
            if pid not in rows or dep.get("type") == "SS":
                continue
            if rows[pid]["ef"] <= rows[cur]["es"]:
                if best is None or rows[pid]["ef"] > rows[best]["ef"]:
                    best = pid
        cur = best
    chain.reverse()
    return chain


def daily_curves(rows, total, plan_data):
    """逐日人工 / 机械曲线（从 0 到总工期，每天都有条目，保证曲线连续）。

    **人工口径 = 当天在岗的全部人工**：工种 + 机械配员（司机 / 信号工 / 泵工 /
    操作工…）。机械主导任务原先只统计主资源（机械本身），于是 `machine_crew_of`
    写进 `plan['resources']` 的配员（见 `_plan_task` 机械分支）一个都不进曲线 ——
    实测塔吊（crew 司机1+信号工1，20 天）的 2 个人**完全不出现在任何逐日人工**里，
    `peak_labor` 因此低于交付物（`delivery` 从 `assigned_resources` 算的每日用工峰值，
    **含**机械配员）报出的同一个量。这里按任务逐条把配员补进 `trades`，
    与交付物口径对齐（`delivery.LABOR ∪ MACHINE_CREW = 人`）。

    机械名（`is_machine_name`）永远归 `items`（设备曲线），绝不混进人工。
    """
    total = max(int(total), 0)
    series = []
    for day in range(total + 1):
        trades = {}
        items = {}
        for tid in sorted(rows):
            row = rows[tid]
            if not (row["es"] <= day < row["ef"]):
                continue
            plan = plan_data.get(tid) or {}
            resources = plan.get("resources") or {}
            name = plan.get("resource_id")
            if plan.get("resource_kind") == "machine":
                # 主资源 = 机械本身 → 设备曲线；其余资源 = 随机台数走的配员 → 人工曲线
                if not name:
                    continue
                items[name] = _r2(items.get(name, 0) + (resources.get(name) or 0))
                for role, cnt in resources.items():
                    if role == name or is_machine_name(role):
                        continue
                    trades[role] = _r2(trades.get(role, 0) + (cnt or 0))
                continue
            need = resources.get(name)
            need = need or 0
            if not name:
                continue
            trades[name] = _r2(trades.get(name, 0) + need)
        series.append({
            "day": day,
            "labor": _r2(sum(trades.values())),
            "equipment": _r2(sum(items.values())),
            "trades": dict(sorted(trades.items())),
            "items": dict(sorted(items.items())),
        })
    return series


def user_target_of(boundary, params):
    """用户目标工期（只当参照）：boundary_conditions 优先，其次 extracted_params。"""
    limits = parse_boundary_limits(boundary)
    target = limits.get("user_target")
    if target is None:
        target = user_target_from_params(params)
    return target


# ==================== 主算法 ====================
def compute_schedules(wbs, dependencies, boundary, params=None, cpm_result=None,
                      reuse_declared_crews=False, frozen_ids=None, face_area=None):
    """两版排程主入口（纯函数，确定性）。返回 ctx 片段。

    `reuse_declared_crews`（第 36 轮，**修订路径专用**）：历史开关。
    ⚠️ C8-5/6（2026-09-21）后**已不影响人数与工期** —— 人数只来自工作面容量
    （段面积 ÷ MWI，见 `plan_organization`），"叶子上写明的投入人工
    `norm_binding.crew`"与"节拍设计班组 `_crew_design`"两个来源都已删除。
    保留参数只为兼容既有调用点。

    `frozen_ids`（**修订路径专用**）：集合里的叶子工期一律保持存档原值，不参与
    重算 —— 用户改一条工序，别的工序不该跟着动。修订路径**必须**继续用它明确冻结
    未被波及的任务。

    注意：即使 `reuse_declared_crews=True` 也**不足以**完全复现存档 —— 存档里的
    `norm_binding.crew` 是 `ResourceNode` 回填的，与排程器当时实际用的班组不是同一个
    量（实测 209 条里仍有 36 条会变，例如模板安装 14 天→5 天）。所以修订必须以
    `frozen_ids` 明确冻结未被波及的任务，而不是指望"重排一般能算出一样的结果"。

    `face_area`（第 41 轮，施工组织层）：该层**可施工面积**（㎡），只用来推"同时能开
    几个作业面"；缺省 None = 面积口径不参与（M_max 只由结构缝与组织上限决定）。

    施工组织层（第 41 轮）**只在 `boundary_conditions.cadence_days` 为正数时生效**：
    工期 = 工日 ÷ (作业面数 × 每面人数 × 班次 × 效率折减)；取不到节拍 → 旧口径
    （工日 ÷ 固定小班组）**一字不动**，输出与改动前逐位相同。
    """
    warnings = []
    leaves = collect_leaf_tasks(wbs)
    if not leaves:
        warnings.append("WBS 中没有可用叶子任务，两版工期均为 0 天（请检查 WBS）")
        return _empty_result(warnings, boundary, params)

    cadence_days, cadence_scope, cadence_source = organization_of(boundary)
    # 裁定 E：用户显式分段规则（`boundary_conditions.segment_rule`）**优先于 MSSA**。
    user_rule = segment_rule_of(boundary)
    if face_area is None:
        # 裁定 G（2026-09-21）：`recompute.py` / 其余调用方不传 `face_area` 时，
        # **在这里自己取一次**（与 `SchedulerNode.run` 完全同一口径：
        # `beat_configs.standard_floor_area(params)` = 总面积 ÷ 栋数 ÷ 层数）。
        # 不这样做，修订重排就只能走 `cap_labor` 兜底、与首版口径不一致。
        try:
            from .beat_configs import standard_floor_area
            face_area = standard_floor_area(params)
        except Exception:
            face_area = None
    if cadence_days:
        warnings.append(
            "施工组织层已生效：标准层主体节拍 %.1f 天/层（范围：%s，来源：%s）—— 班组与"
            "工期按「工日 ÷ (作业面数 × 每面人数 × 班次 × 效率折减)」组织，"
            "做不到的工序进 `organization_gaps`（组织缺口，绝不静默拉长节拍）。"
            % (cadence_days, cadence_scope, cadence_source or "未标注"))
    limits = parse_boundary_limits(boundary)
    # 第 40 轮：来源=model 的边界值不当限额用 —— 必须让用户看见。否则他会问
    # "我明明给了 120 人，为什么排程不理它"，而真相是那个 120 是模型按常见做法补的。
    if limits.get("ignored_model_values"):
        warnings.append(
            "边界条件里以下值由模型按常见做法补齐（`_source`=model），"
            "**没有当作资源限额使用**：%s。若确为你的要求，请在项目文件里写明，"
            "或直接给出这些参数。" % "；".join(limits["ignored_model_values"]))
    limits, caliber_warnings = scale_limits_per_building(limits, params)
    warnings.extend(caliber_warnings)
    preds = {}
    id_set = set()
    for leaf in leaves:
        tid = leaf.get("id") or leaf.get("task_id") or ""
        if tid:
            id_set.add(str(tid))
    for dep in normalize_deps(dependencies):
        if not isinstance(dep, dict):
            continue
        pred = dep.get("predecessor")
        succ = dep.get("successor")
        if pred is None or succ is None:
            continue
        pred, succ = str(pred), str(succ)
        if pred not in id_set or succ not in id_set:
            continue
        dtype = str(dep.get("type") or "FS").strip().upper()
        if dtype not in ("FS", "SS"):
            dtype = "FS"
        preds.setdefault(succ, []).append({
            "task_id": pred,
            "type": dtype,
            "lag": _num(dep.get("lag_days"), 0) or 0,
        })

    # 台账：每条叶子的基础量（与版本无关，保证两版只差"上限"）
    ledger = {}
    no_norm = []
    no_capacity = []
    ai_capacity = []
    scale_bad = []
    scale_bounds = quantity_scale_bounds(params)
    for leaf in leaves:
        tid = leaf.get("id") or leaf.get("task_id") or ""
        if not tid:
            continue
        tid = str(tid)
        name = str(leaf.get("name") or leaf.get("task_name") or tid)
        item = _build_ledger_item(leaf, tid, name)
        # 工程量量级校验（见 scale_violation 的说明）：超界的量不许用来算班组，
        # 直接判为"无可用定额锚定"→ 走既有的"沿用 WBS 工期"路径并计入覆盖率缺口。
        _why = scale_violation(item, scale_bounds)
        if _why:
            item["usable"] = False
            item["norm_is_evidence"] = False
            item["not_usable_reason"] = "工程量量级不可信"
            item["scale_reason"] = _why
            scale_bad.append((tid, name, _why))
        ledger[tid] = item
        if frozen_ids and tid in frozen_ids:
            # 修订路径：这条任务没有被本次修订波及 → 工期冻结为存档原值
            item["frozen"] = True
        if not item["usable"]:
            no_norm.append(tid)
        # 注意区分两种情况（第 37 轮起两者都不再"放行不封顶"）：
        #   ① 库里**根本没有**这条活动的工作面数据 → 记"缺数据"，按物理兜底上限处理
        #   ② 库里有数据 → 一律参与封顶（LOW 只是置信度标注，见 workface_is_evidence）
        has_advisory = bool(item.get("workface_advisory"))
        if item["cap_labor"] is None and item["cap_machine"] is None and not has_advisory:
            no_capacity.append(tid)
        if has_advisory and (item["cap_labor"] is None or item["cap_machine"] is None):
            ai_capacity.append(tid)

    # 警告按类型聚合（数百条叶子时逐条刷屏没有意义，但信息一条都不能少）
    cov = norm_coverage_report(ledger)
    if cov["unbound"]:
        warnings.append(_coverage_warning(cov))
    if no_norm:
        warnings.append(
            "共 %d 条任务无可用定额锚定（norm_value/productivity_value 均缺失），"
            "工期沿用各自 duration_days，班组记为未知（AI 假设）" % len(no_norm))
        for tid in sorted(no_norm):
            item = ledger[tid]
            warnings.append(_WARN_NO_NORM % (tid, item["name"], item["own_duration"]))
    if no_capacity:
        warnings.append(
            "共 %d 条任务缺工作面容量数据（域 1.6 已删 Workface_Capacity_Rule 表，"
            "容量唯一来源改为 MWI 表），已按物理兜底上限处理"
            "（人工 %d 人 / 机械 %d 台，**不是**不封顶），这些任务的工期可能偏乐观"
            % (len(no_capacity), DEFAULT_CEILING, DEFAULT_MACHINE_FALLBACK))
        if len(no_capacity) <= 50:      # 少量时才逐条列出，避免刷屏
            for tid in sorted(no_capacity):
                warnings.append("任务 %s（%s）缺工作面容量数据，已按兜底上限（人工 %d 人 / "
                                "机械 %d 台）计"
                                % (tid, ledger[tid]["name"], DEFAULT_CEILING,
                                   DEFAULT_MACHINE_FALLBACK))
    if ai_capacity:
        sample = sorted(ai_capacity)[:5]
        warnings.append(
            "共 %d 条任务的工作面容量来自 AI 经验估算（source_type=ai_estimate / "
            "confidence=LOW），**已参与封顶计算**（LOW 只表示置信度，不再等于禁用）："
            "班组人数 = clamp(crew_base + crew_step_n × ⌊(段工程量 − q_ref) / crew_step_q⌋, "
            "crew_min, crew_max)，机械台数同口径。若你手上有可靠的施工段容纳人数/机械台数，"
            "请直接给出，本节点会用你的值封顶。"
            % len(ai_capacity))
        warnings.append("（封顶值样例：%s）"
                        % "；".join(
                            "%s=%s人/%s台" % (t,
                                            _r2n((ledger[t].get("workface_capacity") or {}).get("max_labor")),
                                            _r2n((ledger[t].get("workface_capacity") or {}).get("max_machine")))
                            for t in sample))

    order_base = sorted(ledger)
    order, has_cycle = topo_order(order_base, preds)
    if has_cycle:
        warnings.append("依赖关系存在环路，已按任务 ID 顺序兜底排程（请检查 dependencies）")

    # 设计班组：**C8 第 5 项已删除 `resolve_design_crews`**（"96 人预算 ÷ 定额工日
    # 需求比例"的摊派）。班组不再由人力预算决定 —— 它只来自**工作面容量**
    # （段容量 = ceil(段面积 ÷ MWI)，见 `plan_organization`）。
    # 用户**明确指定**的 `crew_design` 按 C9 只作**同类限额**保留。
    crew_plan = {}
    _declared_crews = user_declared_crews(params, boundary)
    if _declared_crews:
        limits = dict(limits or {})
        limits["crew_design"] = _declared_crews
        warnings.append(
            "用户指定的设计班组（%s）只作**同类限额**参与 `min(汇总容量, 用户限额)`，"
            "不再按人力预算摊派（C8：96 人预算摊派已删除）"
            % "、".join("%s %d 人" % (k, v) for k, v in sorted(_declared_crews.items())))
    demand = design_crew_demand(ledger, order)
    if demand:
        warnings.append(
            "定额工日需求合计 %s 人日（按工种：%s）——这是定额算出来的客观量，"
            "工期 = 工日需求 ÷ 班组人数，**不可通过调整工期来压缩**。"
            % ("{:,.0f}".format(sum(demand.values())),
               "、".join("%s %s" % (k, "{:,.0f}".format(v)) for k, v in sorted(demand.items()))))

    # 机械主导任务的人工需求：单独口径、只报不算（见 machine_labor_demand 的说明）
    machine_labor, machine_detail = machine_labor_demand(ledger, order)
    if machine_labor:
        ml_total = sum(machine_labor.values())
        warnings.append(
            "另有**机械主导任务**的人工需求 %s 人日（按工种：%s）——"
            "这些活动的工期由知识库**台班定额**决定（KB 标为 equipment_driven），"
            "上面那句'定额工日需求'不含它们，所以**实际总用工是 %s 人日**。"
            "本版不用该需求延长工期（改工期口径要单独评估）；"
            "产能取该活动人工定额的**中位数**（AI 经验估算行按政策变更 2026-09-20 "
            "同样计入样本，来源由台账逐条标注，样本仍须 ≥3 行）。"
            % ("{:,.0f}".format(ml_total),
               "、".join("%s %s" % (k, "{:,.0f}".format(v)) for k, v in sorted(machine_labor.items())),
               "{:,.0f}".format(sum(demand.values()) + ml_total)))
        for trade, d in sorted(machine_detail.items()):
            warnings.append(
                "机械主导人工需求 · %s：%s 人日 / %d 条任务（样例活动 %s，人工定额 %d 行，"
                "中位产能 %s/工日）"
                % (trade, "{:,.0f}".format(d["days"]), d["tasks"], d["activity"],
                   d["norm_rows"], _r2(d["median_productivity"])))
    ctx_machine_labor = {"demand": machine_labor, "detail": machine_detail}

    if scale_bad:
        warnings.append(
            "**工程量量级不可信** %d 条：这些任务的工程量超过按项目规模推算的合理上界，"
            "疑为模板/默认值与项目规模脱钩，已**不用它们算班组**（沿用 WBS 工期）并计入缺口。"
            "样例：%s"
            % (len(scale_bad),
               "；".join("%s %s（%s）" % (t, n[:14], w[:38]) for t, n, w in scale_bad[:3])))

    # 两版的唯一差异是"受不受用户资源约束"：
    #   理论最短   —— 只受工作面容量约束
    #   资源不超额 —— min(工作面, 用户限额)
    # 域 7.2：**三轮回压**只在 `honor_user_limits=True` 那一版发生（理论版第 1 轮即收敛）。
    _site_const = boundary.get(org_defaults.SITE_MACHINE_CONST_KEY) \
        if isinstance(boundary, dict) else None
    _area_params = _area_params_of(params)
    _common_kw = dict(crew_plan=crew_plan,
                      reuse_declared_crews=reuse_declared_crews,
                      cadence_days=cadence_days, cadence_scope=cadence_scope,
                      face_area=face_area, cadence_source=cadence_source,
                      user_rule=user_rule, site_const=_site_const,
                      area_params=_area_params)
    theory = _run_version_with_backpressure(ledger, preds, order, limits, warnings,
                                            "理论最短工期", False, **_common_kw)
    resource_ok = _run_version_with_backpressure(ledger, preds, order, limits, warnings,
                                                 "资源不超额工期", True, **_common_kw)

    # 复用上游 CPM 的关键路径（仅在其任务 id 能对上时），否则用 ef 最长链推导
    reused = _reuse_cpm_path(cpm_result, _rows_map(theory))
    if reused is not None:
        theory["critical_path"] = reused
        resource_ok["critical_path"] = list(reused)
    else:
        theory["critical_path"] = longest_chain(_rows_map(theory), preds)
        resource_ok["critical_path"] = longest_chain(_rows_map(resource_ok), preds)

    versions = {"theory_min": theory, "resource_ok": resource_ok}
    compare = build_compare(theory["total_duration_days"],
                            resource_ok["total_duration_days"],
                            user_target_of(boundary, params))
    # 用户申报的设备限额 → 逐项对账（绑到了哪个计划资源、是否生效）。
    # 必须显式交付：用户写「静压桩机」、计划里叫「静力压桩机」时，
    # 旧实现会静默丢弃这条硬上限（实测 1 台被算成 2 台、工期 11 天变 6 天）。
    _res_names = set()
    for _v in (theory, resource_ok):
        for _row in _v.get("schedule") or []:
            _res_names.update((_row.get("crew") or {}).keys())
        for _rec in _v.get("daily_equipment") or []:
            _res_names.update((_rec.get("items") or {}).keys())
    equipment_binding = equipment_binding_report(limits, _res_names)
    # 只有"没用上/对不上"才进 warning 刷屏；逐项对账走 equipment_binding 结构化字段。
    warnings.extend(unmatched_equipment_warnings(equipment_binding))
    # 施工组织缺口（契约 §3）：两版合并、按任务去重取更严重的一条。
    # 无节拍时两版都没有 `organization`，这里必然是 `[]`。
    organization_gaps = merge_org_gaps(theory.get("organization_gaps"),
                                       resource_ok.get("organization_gaps"))
    for _gap in organization_gaps:
        warnings.append(
            "施工组织缺口：任务 %s（%s）按节拍 %s 天/层需 %d 个作业面，结构/组织上限只有"
            "%d 个 —— 当前最快 %s 天/层（%s 工日）。可选措施：%s"
            % (_gap.get("task_id"), _gap.get("task_name"), _gap.get("cadence_days"),
               _gap.get("n_needed"), _gap.get("n_max"), _gap.get("t_min_days"),
               _gap.get("person_days"), "；".join(_gap.get("levers") or [])))
    return {
        "schedule_versions": {
            "theory_min": theory,
            "resource_ok": resource_ok,
            "compare": compare,
            "warnings": warnings,
        },
        "schedule": resource_ok,
        "schedule_warnings": warnings,
        "norm_coverage": cov,
        "machine_labor_demand": ctx_machine_labor,
        "equipment_binding": equipment_binding,
        "organization_gaps": organization_gaps,
    }


def _empty_result(warnings, boundary, params):
    total = 0
    empty = {"total_duration_days": total, "schedule": [], "critical_path": [],
             "daily_labor": [], "daily_equipment": [], "peak_labor": 0,
             "peak_equipment": 0, "over_limit": [], "capped": []}
    compare = build_compare(total, total, user_target_of(boundary, params))
    return {
        "schedule_versions": {"theory_min": dict(empty), "resource_ok": dict(empty),
                              "compare": compare, "warnings": warnings},
        "schedule": dict(empty),
        "schedule_warnings": warnings,
        "equipment_binding": {},
        "organization_gaps": [],
    }


def _rows_map(version):
    """版本的 schedule 列表 → {task_id: 行}（关键路径推导用）。"""
    return dict((row["task_id"], row) for row in (version.get("schedule") or []))


def _reuse_cpm_path(cpm_result, rows):
    """上游 CPM 关键路径：能对上本节点任务 id 才复用，否则 None（自己推）。"""
    if not isinstance(cpm_result, dict):
        return None
    path = cpm_result.get("critical_path")
    if not isinstance(path, list) or not path:
        return None
    ids = [str(t) for t in path]
    if all(t in rows for t in ids):
        return ids
    return None


def build_compare(theory_total, resource_total, target):
    """对比块（中文结论）。**只解释，绝不为了凑目标改任何数据。**"""
    compare = {
        "theory_min_total": int(theory_total),
        "resource_ok_total": int(resource_total),
        "delta_days": int(resource_total) - int(theory_total),
        "user_target": None,
        "target_verdict": None,
        "target_note": "",
    }
    if target is None:
        compare["target_note"] = ("用户未提出总工期，已按客观计算给出两版："
                                  "顶满工作面最快 %d 天，按现有资源最快 %d 天。"
                                  % (compare["theory_min_total"], compare["resource_ok_total"]))
        return compare

    target = int(target)
    compare["user_target"] = target
    if target < compare["theory_min_total"]:
        compare["target_verdict"] = "物理上做不到"
        compare["target_note"] = (
            "最快 %d 天（顶满工作面），目标 %d 天，差 %d 天；建议放宽目标工期或增加工作面。"
            % (compare["theory_min_total"], target,
               compare["theory_min_total"] - target))
    elif target <= compare["resource_ok_total"]:
        compare["target_verdict"] = "需放宽资源"
        compare["target_note"] = (
            "顶满工作面 %d 天可完成；按你现有资源最快 %d 天，目标 %d 天需要放宽资源限制 %d 天。"
            % (compare["theory_min_total"], compare["resource_ok_total"], target,
               compare["resource_ok_total"] - target))
    else:
        compare["target_verdict"] = "宽松"
        compare["target_note"] = (
            "用户目标 %d 天 > 资源可行 %d 天，有余量 %d 天，可考虑减少投入。"
            % (target, compare["resource_ok_total"],
               target - compare["resource_ok_total"]))
    return compare


# ==================== 单位一致性（防止拿面积除以体积）====================
_UNIT_ALIAS = (
    ("平方米", "m2"), ("㎡", "m2"), ("m²", "m2"), ("m^2", "m2"), ("m2", "m2"),
    ("立方米", "m3"), ("m³", "m3"), ("m^3", "m3"), ("m3", "m3"),
    ("延米", "m"), ("米", "m"), ("吨", "t"), ("千克", "kg"), ("公斤", "kg"),
    ("工日", "wd"), ("台班", "shift"), ("樘", "set"), ("块", "pc"),
    ("根", "pc"), ("个", "pc"), ("台", "pc"), ("套", "pc"), ("座", "pc"),
    ("项", "item"), ("处", "pc"), ("组", "pc"), ("批", "pc"),
)


def normalize_unit(text):
    """把各种写法归一成规范单位串（m2 / m3 / t / m / pc / item / wd …）。

    无法识别时返回空串（调用方据此判定"说不清"，不做单位校验）。
    """
    t = str(text or "").strip().lower()
    if not t:
        return ""
    for src, dst in _UNIT_ALIAS:
        if src in t:
            return dst
    return ""


def units_compatible(leaf_unit, norm_unit_text):
    """叶子的工程量单位与定额的计量单位是否一致。

    定额单位串形如 "工日/m³"（人工，分母是工程量单位）或 "m²/工日"（产能，分子是
    工程量单位）。两边都解析出来取"工程量那一个"再比。

    返回 True/False；任一解析不出（说不清）时返回 True —— 宁可不拦，
    也不要因为格式不认识就把有据可查的定额浪费掉。
    """
    a = normalize_unit(leaf_unit)
    if not a:
        return True
    parts = [p for p in str(norm_unit_text or "").replace("／", "/").split("/") if p.strip()]
    if not parts:
        return True
    cands = [normalize_unit(p) for p in parts]
    cands = [c for c in cands if c]
    if not cands:
        return True
    # 工程量单位一定不是"工日/台班"
    cands = [c for c in cands if c not in ("wd", "shift")] or cands
    return a in cands


def _r2n(value):
    """展示用的"整数优先"数字：4.0 → "4"，4.5 → "4.5"，None → "?"。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "?"
    return str(int(f)) if abs(f - round(f)) < 1e-9 else str(round(f, 2))


# ==================== 单条叶子的台账（与版本无关）====================
def workface_is_evidence(workface):
    """兼容垫片：**否决语义已删除**（第 37 轮，契约 §5-WS4 ①）。

    旧语义要求工作面容量"有据可查"（非 ai_estimate/LOW）才允许参与封顶；
    知识库旧表 `Workface_Capacity_Rule`（域 1.6 已删除）的 478 行**全部**是
    `source_type='ai_estimate'`、`confidence='LOW'`，于是旧实现把容量表整表架空，
    人数改由「工程量 ÷ (定额产能 × 目标工期)」反推 —— 那正是"按目标工期反推班组"
    的旧口径，用户改工程量时工期原地不动，也看不到任何工作面约束。

    新语义（契约 §4）：`source_type=ai_estimate` / `confidence=LOW` 只是一条**置信度
    标注**，容量一样参与计算（LOW ≠ 禁用）。因此本函数对任何非空容量字典一律返回
    True，只为 `resource.py` 等既有调用方保留入口；新代码请直接用
    `workface_capacity_for_qty` / `workface_limits_from_rule`。
    """
    return bool(isinstance(workface, dict) and workface)


def _median(values):
    """中位数（不改动入参）。空 → None。"""
    vs = sorted(float(v) for v in values)
    n = len(vs)
    if not n:
        return None
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2.0


# ---- 政策变更（用户 2026-09-20 亲自决定）--------------------------------------
# 旧政策：AI 凭经验编的定额（`sources` 表 `AI_ESTIMATE_V1`）"只作参考，不参与算工期"，
# `norm_is_evidence` 里 `_origin=="ai" / _match=="ai" / _ai_source` 任一命中即判 False。
# 新政策：**允许使用 AI 估算的定额**，但必须**逐条标注**来源（下面 LABEL_AI_ESTIMATE）。
# 因此本段只剩两件事：① 认出来源（`_ai_source`）；② 把它标进台账与覆盖率账本。
# ⚠️ 拦截判据一条没松 —— 单位不可换算、定额口径不符、绑定层判不可用、定额值取不到，
# 仍然一律不可用（见 `_build_ledger_item` 里 `not_usable_reason` 的优先级）。
LABEL_AI_ESTIMATE = "AI 经验估算定额（无规范依据，待审）"


def _ai_source(source_code):
    """该定额行是否**显式标成非规范来源**（`AI_ESTIMATE_V1` / `AI_*` / `SCAFFOLD*`）。

    政策变更（2026-09-20）后本函数**只用于标注来源**，不再是否决开关：
    这类行现在与真人定额**同等参与工期计算**，但必须逐条标出"这是 AI 凭经验编的、
    没有规范依据"。函数名保留（既有调用方与 `tests/test_machine_labor_demand.py`
    直接 import 它）；新语义名见 `_is_ai_estimate_source`。

    `SCAFFOLD*`（`SCAFFOLD_V1` = 类别占位定额）于 2026-09-20 用户裁定
    「保留占位但必须全面如实标注」后并入本判据 —— 它与交付层
    `delivery._ai_norm_source_code_ai` 必须**同口径**，否则会出现
    "看板标非规范、排程台账 `released_ai` 却漏计"的静默分叉。
    """
    src = str(source_code or "").upper()
    return ("AI_ESTIMATE" in src) or src.startswith("AI_") or src.startswith("SCAFFOLD")


# 新语义名（与旧名等价）：本函数现在只用来"标注"，不参与"能不能用"的判定。
_is_ai_estimate_source = _ai_source


def _item_ai_source(item):
    """台账条目是否绑定了 AI 来源定额（先看绑定层标记，再退回来源标注）。

    `norm_coverage_report` 的 `released_ai` 用它统计。三路任一命中即算（政策变更
    2026-09-20；闸门层与绑定层的 AI 信号不同源，必须都认）：
      · `ai_norm_source` —— 绑定自身被标成 AI（`provenance.origin=ai` /
        `match_type=ai` / `source_code=AI_*`）；
      · 来源标注 `state == "released_ai"` —— 闸门按**默认行的来源**放行的 AI 档；
      · 来源标注 `confidence == "estimated"` —— 实测 `L4_Norm_Default` 全表的
        `source_code` 一律是 `KB_Norm_Labor_Table`，闸门侧唯一的 AI 信号就是
        `confidence`，不认它就会漏报（而这次政策变更最不能错的就是这个条数）。
    三者都拿不到就**不当 AI**（不猜）。
    """
    if not isinstance(item, dict):
        return False
    if item.get("ai_norm_source"):
        return True
    label = item.get("norm_evidence_label")
    if not isinstance(label, dict):
        return False
    if str(label.get("state") or "") == "released_ai":
        return True
    if str(label.get("confidence") or "") == "estimated":
        return True
    return _ai_source(label.get("source_code"))


def _normalize_bound_reason(reason):
    """把绑定层留下的**旧 AI 拦截文案**改写成如实描述（政策变更 2026-09-20）。

    `norm_bind` 在"KB 里一行定额都没有、只能拿经验产能顶上"时写的是
    `"AI估算定额：KB 无定额行，只作参考"`。新政策下 AI 来源本身不再是拦截理由，
    剩下的真实原因是**KB 里确实没有定额行**；"只作参考"描述的是已废除的旧政策，
    不该再出现在覆盖率缺口明细里（否则用户会按错误的修法去改）。
    ⚠️ `norm_bind.py` 本轮不动（另有代理在改其它文件），所以在消费端改写。
    """
    r = str(reason or "").strip()
    if not r:
        return ""
    if ("只作参考" in r) or ("AI" in r.upper() and "定额行" in r):
        return "KB无定额行"
    return r


def quantity_in_norm_unit(item):
    """把叶子工程量换算到**定额分母单位**（人工口径）—— 工期与工日需求的唯一入口。

    为什么必须换算（第 39 轮实测，`devtools/replay_full.py` 抓到的最大缺陷）：
    ALC 墙板 18 条各自 `quantity=1420`（**m²**），绑定的定额是 **0.943 工日/m³**。
    按原始工程量直接除（1420 ÷ 0.943 = 1506 工日）→ 每条 150 天，18 条合计把总工期
    从 1488 天吹到 3993 天；换成体积 1420 × 0.2 = 284 m³ → 284 ÷ 0.943 ≈ 301 工日，
    ÷9 人 ≈ 34 天。机械分支一直在换算（`machine_total_shifts` /
    `_machine_labor_days`），人工分支漏了，同一份计划两套量纲。

    **单一真源**：换算系数只从 `item["labor_norm_unit_pair"]`（= `kb_units.check_unit_pair`
    的结果，`_build_ledger_item` 写入）取，**不另造系数、不硬编码 0.2**。
    pair 缺失 / verdict 不是 same|convertible / factor 取不到 → **原样返回工程量**
    （保持旧行为；宁可少换算，也不猜一个系数）。
    """
    qty = _pos(item.get("quantity"))
    if qty is None:
        return None
    pair = item.get("labor_norm_unit_pair")
    if not isinstance(pair, dict) or pair.get("verdict") not in ("same", "convertible"):
        return qty
    conv = _pos(pair.get("factor"))
    return qty * conv if conv else qty


def typical_labor_productivity(activity_id):
    """某活动人工定额的**中位产能**（单位/工日）。返回 ``(产能 or None, 用了几行)``。

    为什么要中位数、而不是"取第一行"：一个活动下常挂几十上百条定额行（按构件类型、
    施工方式、机械配置分条件），第一行往往是**预制**或某种特殊工艺，拿来代表现浇
    会差一个量级。中位数是"这个活动典型一天能干多少"的稳健估计。

    为什么要至少 3 行：只有 1~2 行时中位数就是那一两行本身，等于"拿单条定额代表全部"，
    不可信 —— 宁可不报，也不报一个假数。

    **政策变更（2026-09-20）**：`AI_ESTIMATE_V1` 这类 AI 经验估算行**与真人定额同一
    口径**计入样本（旧口径整行剔除）。样本门槛不变（仍要 ≥3 行），来源标注由台账与
    交付物承担（见 `_build_ledger_item` 的 `norm_evidence_label`）。
    """
    if not activity_id:
        return None, 0
    try:
        rows = kb.labor_norms(str(activity_id)) or []
    except Exception:
        return None, 0
    vals = []
    for r in rows:
        p = _pos(r.get("productivity_value"))
        if p:
            vals.append(p)
    if len(vals) < 3:
        return None, len(vals)
    return _median(vals), len(vals)


def machine_labor_demand(ledger, order):
    """**机械主导**任务的人工需求（工日）—— 单独口径，**不与机械工期取大值**。

    为什么必须单独报这个数：知识库把现浇混凝土这类活动标成 `equipment_driven`，
    于是它们的工期由**台班定额**决定，而 `design_crew_demand()` 只统计 labor 主导的
    任务 —— 这些活动的**人工工日**就完全没进"本工程要多少工日"。
    可混凝土是要人的（KB 里 CONC_NEW_SLAB 挂着 360 行人工定额），
    不计进来，用户拿来自查用工总量的那个数就是**偏小的**，而且**看不出来偏**。

    本版**不**用这个需求去延长工期：那等于改掉"机械主导任务工期由台班决定"的口径，
    属于口径变更，必须单独评估、单独让用户拍。这里只把它如实报出来，消灭"看不见的缺口"。

    返回 ``(需求 dict, 明细 dict)``。单位：人日。
    """
    demand, detail = {}, {}
    for tid in order:
        item = ledger.get(tid)
        if not isinstance(item, dict):
            continue
        if str(item.get("mode") or "") != "machine" or not item.get("usable"):
            continue
        qty = _pos(item.get("quantity"))
        aid = item.get("kb_activity_id")
        if not qty or not aid:
            continue
        prod, n_rows = typical_labor_productivity(aid)
        if not prod:
            continue
        trade = str(item.get("labor_name") or "普工")
        # ⚠️ 这里**故意不**做单位换算，与 `design_crew_demand`（labor 主导）不同：
        # `prod` 来自 `typical_labor_productivity()`，即该活动**人工定额行**的中位产能，
        # 它的分母单位是那些定额行自己的 `quantity_unit`（如 工日/m³）；而机械任务的
        # `item["labor_norm_unit_pair"]` 是由 **机械定额**的分母算出来的
        # （2.1.1 实测：leaf「根」 vs 机械「台班/m」→ factor=18）。拿机械分母去换算
        # 人工需求就是另一套量纲 —— 宁可保持原样，也不猜系数（本函数只报表、不参与工期）。
        # 要做对需要先拿到"人工定额行的单位"这个真源，见交付报告里的待定项。
        days = qty / prod
        demand[trade] = demand.get(trade, 0.0) + days
        d = detail.setdefault(trade, {"days": 0.0, "tasks": 0, "activity": str(aid),
                                      "median_productivity": prod, "norm_rows": n_rows})
        d["days"] += days
        d["tasks"] += 1
    return demand, detail


def design_crew_demand(ledger, order):
    """各工种的**定额总工日需求** = ∑ 工程量 ÷ 定额产能（只有有据可查的定额才算）。

    这是"以知识库为准"的核心数字：它由定额与工程量唯一决定，**与怎么排工期无关**。
    有了它，人力与工期的关系就是一句人话：
        工期 = 工日需求 ÷ 能投入的人数
    """
    demand = {}
    for tid in order:
        item = ledger.get(tid) or {}
        # 台账里的模式字段叫 mode（labor/machine），不是 resource_kind（那是 _plan_task 的返回值）
        if str(item.get("mode") or "labor") != "labor" or not item.get("usable"):
            continue
        p = _pos(item.get("productivity"))
        # 台账里的工种字段叫 labor_name（resource_id 是 _plan_task 的返回值，台账里没有）
        trade = item.get("labor_name")
        if not p or not trade:
            continue
        demand[str(trade)] = (demand.get(str(trade), 0.0)
                              + (quantity_in_norm_unit(item) or 0.0) / p)
    return demand


# 工种别名 → 规范名。用户/资料里的叫法（木工、砼工、杂工…）与知识库的工种名
# （模板工、混凝土工、普工…）常常不一致；不归一就会"给了人数却匹配不上"。
_TRADE_ALIAS = {
    "木工": "模板工", "模板": "模板工", "支模工": "模板工", "木模板工": "模板工",
    "砼工": "混凝土工", "混凝土": "混凝土工", "搅拌工": "混凝土工",
    "扎筋工": "钢筋工", "钢筋": "钢筋工",
    "砌筑工": "瓦工", "砖工": "瓦工", "泥水工": "瓦工",
    "杂工": "普工", "力工": "普工", "普通工": "普工",
    "脚手架工": "架子工", "架工": "架子工",
    "粉刷工": "抹灰工", "油漆": "油漆工", "涂料工": "油漆工",
    "水暖工": "管道工", "管工": "管道工", "水电工": "电工",
    "焊工": "安装工", "起重工": "司机", "机械操作工": "操作工",
}


def _normalize_trade(name):
    """把工种别名归一成知识库里的规范名；未知的叫法原样返回。"""
    s = str(name or "").strip()
    if not s:
        return s
    return _TRADE_ALIAS.get(s, s)


# C8 第 5 项已删除（2026-09-21）：`resolve_design_crews(ledger, order, limits, params,
# boundary, reuse_declared_crews=False)` —— 「96 人预算 ÷ 定额工日需求比例」的摊派。
# 那段代码用 `limits["labor_total"]` 或「节拍配置各工种班组之和」当预算，再按
# `budget × d / Σd` 向下取整摊到每个工种（实测把模板工压到 1 人）。
# 新口径下班组**只来自工作面容量**：段容量 = ceil(段面积 ÷ MWI)，
# 有效容量 = min(汇总容量, 用户同类限额)，见 `plan_organization()`。
# 用户直接指定的 `crew_design` 按 C9 保留，走 `user_declared_crews()` → 同类限额。


    """C8 已删除：本函数体不再存在（保留占位见上方注释）。"""


def norm_coverage_report(ledger, total=None):
    """定额口径覆盖率报告 —— 「哪些工程量进了定额、哪些没进、占多少」一眼可查。

    为什么必须报：计划里 415 条任务只有一部分有**有据可查**的定额。没进定额的那部分
    沿用 WBS 原工期，它们既不在"定额工日需求"里，也不受"定额产能"约束。
    不显式报出来，用户就会把"定额口径的工期"误当成"全口径的工期"。
    """
    total = total if total else len(ledger)
    # 政策变更（2026-09-20）：`"AI估算定额"` 这个**不可用原因**桶已废除 —— AI 来源
    # 不再产生"不可用"，它只进下面的 `released_ai`（已放行并标注的条数）。
    # 未知原因仍会被 `buckets.get(key, 0)` 动态收下，不丢信息。
    buckets = {"bound": 0, "单位不一致": 0, "KB无定额行": 0,
               "定额口径不符": 0, "工程量量级不可信": 0, "定额值为空": 0, "其他": 0}
    by_reason_activity = {}
    # 第 39 轮：进了定额口径的任务里，有多少条用的是**未经人工审定**的真人定额
    # （闸门按来源放行的那一档）。不报出来，用户就看不出"这些数还没人点过通过"。
    released_unapproved = 0
    released_activities = {}
    by_confidence = {}
    # 政策变更（2026-09-20）：**绑定了 AI 来源定额、且已按新政策放行**的任务数。
    # 用户的原话是"不得不用 AI 就用，最后标出来"—— 这个数就是"标出来"的账。
    released_ai = 0
    released_ai_activities = {}
    # 第 39 轮：可用任务里的人工容量**饱和**情况 —— 容量公式值顶到上限的条数，
    # 以及因为上限重定（`effective_crew_max`）而上限被抬高的条数。
    # 为什么必须报：上限重定的目的是"大工程量任务还能加人"，不报出来就看不出还剩
    # 多少条顶在天花板上（顶到上限 = 工程量再大也不加人 = 工期被上限锁死）。
    workface_saturated = 0
    workface_ceiling_raised = 0
    for item in ledger.values():
        key = "bound" if item.get("usable") else (item.get("not_usable_reason") or "其他")
        buckets[key] = buckets.get(key, 0) + 1
        if key != "bound":
            aid = item.get("kb_activity_id") or "（未锚定活动编号）"
            by_reason_activity.setdefault(key, {})
            by_reason_activity[key][aid] = by_reason_activity[key].get(aid, 0) + 1
            continue
        # ---- 人工容量饱和 / 上限抬高（只数**可用**任务；缺数据记 0，绝不抛异常）----
        # 本轮之前建的台账 item 没有这两个键 → 从 `workface_capacity` 现算；
        # 那里也没有 crew_base/crew_max（老 item 只存 max_labor/max_machine）→ 算不出就跳过。
        wf = item.get("workface_capacity")
        wf = wf if isinstance(wf, dict) else {}
        eff = item.get("crew_max_effective")
        if eff is None:
            # C8-7（2026-09-21）：`effective_crew_max`（×2.5 带）已删 → 容量上限就是
            # 标定行的裸 `crew_max`，不再有"上限被抬高"这回事
            # （`workface_ceiling_raised` 因此恒为 0）。
            eff = _pos(wf.get("crew_max"))
        raw = item.get("crew_max_raw")
        if raw is None:
            raw = _pos(wf.get("crew_max"))
        cap_labor = _pos(item.get("cap_labor"))
        if eff is not None and cap_labor is not None \
                and float(cap_labor) >= float(eff) - 1e-9:
            workface_saturated += 1
        if eff is not None and raw is not None and float(eff) > float(raw) + 1e-9:
            workface_ceiling_raised += 1
        label = item.get("norm_evidence_label")
        label = label if isinstance(label, dict) else {}
        conf = str(label.get("confidence") or "")
        if conf:
            by_confidence[conf] = by_confidence.get(conf, 0) + 1
        if str(label.get("state") or "") == "released_unapproved":
            released_unapproved += 1
            aid = item.get("kb_activity_id") or "（未锚定活动编号）"
            released_activities[aid] = released_activities.get(aid, 0) + 1
        # 政策变更（2026-09-20）：AI 来源定额放行后单独成一档（口径 = 可用 + AI 来源）。
        if _item_ai_source(item):
            released_ai += 1
            aid = item.get("kb_activity_id") or "（未锚定活动编号）"
            released_ai_activities[aid] = released_ai_activities.get(aid, 0) + 1

    def _pct(n):
        return round(100.0 * n / total, 1) if total else 0.0

    return {
        "total": total,
        "bound": buckets["bound"],
        "bound_pct": _pct(buckets["bound"]),
        "unbound": total - buckets["bound"],
        "unbound_pct": _pct(total - buckets["bound"]),
        "by_reason": dict((k, v) for k, v in buckets.items() if k != "bound" and v),
        "by_reason_pct": dict((k, _pct(v)) for k, v in buckets.items() if k != "bound" and v),
        "top_activities": dict(
            (k, sorted(v.items(), key=lambda kv: -kv[1])[:6])
            for k, v in by_reason_activity.items()),
        # ---- 第 39 轮：进了定额口径的那部分的**来源**（谁审过、谁没审过）----
        "released_unapproved": released_unapproved,
        "released_unapproved_pct": _pct(released_unapproved),
        "by_confidence": by_confidence,
        "released_activities": sorted(released_activities.items(), key=lambda kv: -kv[1])[:6],
        # 政策变更（2026-09-20）：AI 来源定额已放行的条数/占比（交付物要逐条标出来）
        "released_ai": released_ai,
        "released_ai_pct": _pct(released_ai),
        "released_ai_activities": sorted(released_ai_activities.items(),
                                         key=lambda kv: -kv[1])[:6],
        # ---- 第 39 轮：人工容量上限重定的实际效果（交付物要展示这两个数）----
        # `workface_saturated`：可用任务里容量公式值顶到 `effective_crew_max` 的条数；
        # `workface_ceiling_raised`：因新口径 `effective > 原 crew_max` 的条数。
        "workface_saturated": workface_saturated,
        "workface_ceiling_raised": workface_ceiling_raised,
    }


def _coverage_warning(cov):
    """把覆盖率报告压成一条中文警告（用户看的是这句话）。"""
    if not cov or not cov.get("total"):
        return ""
    parts = ["%s %d 条（%.1f%%）" % (k, v, cov["by_reason_pct"].get(k, 0.0))
             for k, v in sorted(cov["by_reason"].items(), key=lambda kv: -kv[1])]
    detail = []
    for k, items in sorted((cov.get("top_activities") or {}).items()):
        detail.append("%s：%s" % (k, "、".join("%s×%d" % (a, n) for a, n in items[:4])))
    # 第 39 轮：进了定额口径的那部分，也要说清楚**谁审过**。真人 parsed/verified 的
    # 行没有人工审定也放行（按来源分档），但必须让用户看见"这些数还没人点头"。
    extra = ""
    if cov.get("released_unapproved"):
        conf_txt = "、".join("%s %d 条" % (k, v) for k, v in
                            sorted((cov.get("by_confidence") or {}).items()))
        acts = "、".join("%s×%d" % (a, n) for a, n in (cov.get("released_activities") or [])[:4])
        extra = ("⚠️ 其中 %d 条（%.1f%%）用的是**未经人工审定的真人定额**（%s）——"
                 "已按来源放行并逐条标注；如认为某条不适用，请把该行标为 rejected。"
                 "缺明细：%s。"
                 % (cov["released_unapproved"], cov.get("released_unapproved_pct") or 0.0,
                    conf_txt or "来源见计划 meta", acts or "—"))
    # 政策变更（2026-09-20）：AI 经验估算的定额**已按来源放行**（不再"只作参考"），
    # 但必须让用户看见这个数 —— 它是"允许用 AI 定额"的代价，交付物会逐条标注。
    if cov.get("released_ai"):
        ai_acts = "、".join("%s×%d" % (a, n)
                           for a, n in (cov.get("released_ai_activities") or [])[:4])
        extra += ("⚠️ 其中 %d 条（%.1f%%）依据 AI 经验估算定额（无规范依据，已按来源放行"
                  "并逐条标注）—— 缺明细：%s。"
                  % (cov["released_ai"], cov.get("released_ai_pct") or 0.0,
                     ai_acts or "—"))
    return ("定额口径覆盖率：共 %d 条任务，**只有 %d 条（%.1f%%）有据可查并计入定额工日需求**；"
            "未纳入定额口径的 %d 条（%.1f%%）沿用 WBS 原工期、不受定额产能约束 —— %s。"
            "缺口明细：%s。这些缺口不会自己消失，要么补定额，要么在交付物里如实标注。%s"
            % (cov["total"], cov["bound"], cov["bound_pct"], cov["unbound"],
               cov["unbound_pct"], "；".join(parts) if parts else "无", "；".join(detail),
               extra))


def unit_family(unit):
    """把单位归到三类量纲之一（m2/m3/t），认不出返回空串。"""
    u = str(unit or "").strip().lower()
    if u in ("㎡", "m2", "m²", "m^2", "平方米"):
        return "m2"
    if u in ("m³", "m3", "m^3", "立方米"):
        return "m3"
    if u in ("t", "吨"):
        return "t"
    return ""


def quantity_scale_bounds(params):
    """按量纲给出"单条任务的量"的**宽松上界**；缺项目规模就不给界（不猜）。

    为什么上界以 total_area 为基（不再乘层数）：**total_area 本身就是各层面积之和**，
    再乘层数等于把层数算两遍。面积类工序的合法倍数来自"接触面积系数"：
    模板 ≈ 2.5×、抹灰/涂料 ≈ 2~3×，所以取 5 倍已经很宽松。
    """
    p = params if isinstance(params, dict) else {}

    def _p(k):
        try:
            f = float(p.get(k))
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    area = _p("total_area")
    # 【第 2 批 · 域 2 / 2.3】`total_wall` 已删除 → 换成 `total_infill_wall`（填充墙，m³）。
    # 为什么**不是**换成 `total_pile`：这里算的是**m³ 量纲**的合理上界（`b["m3"]`），
    # 而 `total_pile`（桩）**不预设单位**（可能是根 / 米 / m³），拿它当 m³ 基数会
    # 悄悄给出一根错误的"合理上界"。`total_infill_wall` 的口径就是 m³，语义与
    # 原来的 total_wall（体积类总量）一致。
    vols = [x for x in (_p("total_concrete"), _p("total_earthwork"),
                        _p("total_infill_wall")) if x]
    rebar = _p("total_rebar")
    b = {}
    if area:
        b["m2"] = area * 5.0
    if vols:
        b["m3"] = max(vols) * 3.0
    if rebar:
        b["t"] = rebar * 2.0
    return b


def scale_violation(item, bounds):
    """工程量超出"项目规模推算的合理上界"时返回中文原因，否则 None。

    实测（自带住宅楼样例）：residential 模板把「定位放线」写死成 128000 ㎡，
    而该项目总建筑面积只有 14200 ㎡ —— 一条放线的量是整栋楼的 9 倍；定额又是
    AI 经验估算 1.0 工日/㎡，于是一条 2 天的放线任务被算成 **64000 人/天**。
    这类"模板/AI 默认值与项目规模脱钩"的量不许用来算班组。
    """
    fam = unit_family(item.get("unit"))
    lim = bounds.get(fam) if fam else None
    q = float(item.get("quantity") or 0)
    if not lim or lim <= 0 or q <= 0:
        return None
    if q > lim:
        return ("单条任务工程量 %s %s 超过按项目规模推算的合理上界 %s（%.1f 倍），"
                "疑为模板/默认值与项目规模脱钩"
                % (("%g" % q), item.get("unit"), ("%g" % round(lim, 1)), q / lim))
    return None


def _build_ledger_item(leaf, tid, name):
    """算出与版本无关的部分：量、定额产能、目标工期、工作面容量、工种、机械。

    这里**不写 warning**（同一句话会重复几百遍）：警告由 compute_schedules
    按类型聚合后统一写入，见 no_norm / no_capacity 两处。
    """
    quantity = _pos(leaf.get("quantity"), 0.0) or 0.0
    unit = str(leaf.get("unit") or "")
    raw_binding = leaf.get("norm_binding")
    binding = raw_binding if isinstance(raw_binding, dict) else {}
    mode = str(binding.get("mode") or "labor").strip().lower()
    if mode not in ("labor", "machine"):
        mode = "labor"

    basis = _pos(binding.get("quantity_basis"), 1.0)
    norm_value = _pos(binding.get("norm_value"))
    productivity = _pos(binding.get("productivity_value"))
    if productivity is None and norm_value is not None and mode != "machine":
        # 人工产能（单位/工日）= 1 ÷ 定额值 —— **不乘 basis**。
        # 库里 labor_norm_value 落库时已归一成「工日 / 1×quantity_unit」，
        # 书页上的批量分母（basis=1/10/100/1000）只作溯源
        # （留档不变式 raw_value / raw_quantity_basis == labor_norm_value）。
        # 旧实现写成 basis/norm_value —— 与上面的不变式直接冲突：工日被放大
        # 10~1000 倍、班组与工期缩小同样的倍数（实测 FORM_NEW_OTHER basis=10、
        # 定额 0.25 工日/10m²：真产能 40 m²/工日，旧口径算成 4 m²/工日）。
        #
        # ⚠️ 口径分岔（下一个人务必看清，别再改错）：
        #   人工：norm_value 已归一 → 产能 = 1/norm_value，**不乘 basis**；
        #   机械：台班定额**没有归一**，仍是「台班 / (basis × quantity_unit)」
        #        （例：静压桩机 0.49 台班/100m，basis=100）→ 总台班必须乘 basis。
        #        **机械侧的 norm_value 不能拿来当人工产能**（它不是工日，是台班；
        #        1/台班数 既不是 m³/工日 也不是任何有量纲的产量）。机器任务的
        #        "人工配合"另有口径，见 _machine_labor_days。
        productivity = kb_units.productivity_of(norm_value)
    if mode == "machine" and norm_value is None:
        # 机械主导但没有台班定额 → 退回人工口径（有产能就用，没有就沿用原工期）
        mode = "labor" if productivity is not None else "machine"

    target_days = _pos_int(leaf.get("duration_days"), 1)
    own_duration = _pos_int(leaf.get("duration_days"), 1)

    workface = resolve_workface(leaf) or {}
    # 工作面容量（契约 §5-WS4 ①⑤）：**不再有"AI 估算 → 不参与封顶"的否决语义**。
    # LOW / ai_estimate 只作置信度标注，容量一律按标定行随工程量算出来参与计算。
    cap_labor, cap_machine = workface_limits_from_rule(leaf, quantity, unit) \
        if workface else (None, None)
    unit_basis = str(workface.get("unit_basis") or "每施工段")
    # 第 39 轮：把"上限重定"的输入/输出留在台账上 —— `norm_coverage_report` 要数
    # `workface_saturated`（顶到新上限的条数）与 `workface_ceiling_raised`（上限被抬高的
    # 条数）。不在这里记，报告只能拿台账里没有的 `crew_max` 去猜（`item["workface_capacity"]`
    # 只保留了 max_labor/max_machine/置信度，没有 crew_base/crew_max）。
    crew_max_raw = _pos(workface.get("crew_max"))
    # C8-7（2026-09-21）：上限重定（`effective_crew_max` = ×2.5 带）**已删** →
    # "有效上限"就是裸 `crew_max`。`norm_coverage_report` 仍读这两个键。
    crew_max_effective = crew_max_raw

    labor_types = resolve_labor_types(leaf, binding)
    # ⚠️ C8 第 6 项（2026-09-21）：`leaf["_crew_design"]`（节拍配置写入的设计班组，
    # 96 人摊派的输入）**不再读**。只保留**用户明确给出**的 `crew_design`
    # （按 C9 作同类限额，见 `user_declared_crews`）。0/缺失 = 没有设计班组。
    crew_design = _pos_int(leaf.get("crew_design"), 0) or 0
    crew_machines, crew_labors = split_crew(binding.get("crew"), binding.get("crew_kind"),
                                            set(labor_types))
    # 定额 / 上游已经写明的投入人工（crew_bind 给的班组）：它是"工期 = 工日 ÷ 人数"
    # 里那个"人数"的另一个来源。有它时工程量变化会如实传导到工期；没有时人数只能
    # 从目标工期反推，工期会原地不动 —— 用户"把工程量改成 300"就看不到任何变化。
    crew_declared = 0
    for role in (crew_labors or []):
        crew_declared += _pos_int((binding.get("crew") or {}).get(role), 0) or 0
    labor_name = (crew_labors[0] if crew_labors
                  else (labor_types[0] if labor_types else "普工"))
    machine_name = binding_machine_name(leaf, binding)

    # 定额是否"有据可查"：**政策变更（用户 2026-09-20）**后，显式标记为 AI 估算的行
    # **也可以**用来重算工期（与真人定额同等），但必须逐条标注来源。
    # 旧口径（第 37 轮）：AI 假设的条目一律不许重算工期 —— 理由是 AI 经验产能的量级
    # 不可靠（实测把电气配管算成 0.12 m/工日 → 单条 1512 天、整版 7651 天）。
    # 新口径下这个风险由**标注 + 闸门 + 单位/口径判据**承担：AI 来源进
    # `norm_evidence_label.state="released_ai"`，交付物逐条标出"无规范依据、待审"。
    # 判据仍用"显式 AI 标记"而不是"必须有 kb 标记"：历史/合成数据往往没有 provenance。
    _prov = binding.get("provenance")
    _prov = _prov if isinstance(_prov, dict) else {}
    _origin = str(_prov.get("origin") or "").strip().lower()
    _match = str(binding.get("match_type") or "").strip().lower()
    # 知识库里有一部分活动/定额本身就是 AI 经验估算入库的（source_code =
    # AI_ESTIMATE_V1，如装饰/机电/临建类）。它们"精确命中"，新政策下照用，但要标出来。
    _src = str(binding.get("source_code") or "").upper()
    _ai_source = ("AI_ESTIMATE" in _src) or _src.startswith("AI_")
    # 单位一致性：叶子的量纲必须与定额的计量单位一致。实测知识库里同一活动下
    # 混有不同量纲的行（如 CONC_NEW_SLAB 既有 m³/工日 也有 m/工日），
    # 而节拍叶子的单位又可能与所绑活动不同（ALC 墙板按 m² 计量、砌块墙定额按 m³）
    # —— 拿面积除以体积会得到毫无意义的天数（实测单条 57 天/层）。
    _unit_ok = units_compatible(leaf.get("unit"), binding.get("unit"))
    # 跨族换算（契约 §5-WS4 ④）：`units_compatible` 只认同族，把「根 → m」（桩长
    # 18 m/根）判成不一致 —— 绑定层（WS3）已经用 kb_units.check_unit_pair **带 ctx**
    # 判过并写了 `unit_check`：same / convertible 都是**可用**。有绑定层结论时以它为准，
    # 否则退回旧的同族判据（兼容老计划里没有 unit_check 的绑定）。
    _unit_verdict = str((binding.get("unit_check") or {}).get("verdict")
                        or binding.get("unit_verdict") or "").strip().lower()
    if _unit_verdict == "same":
        _unit_ok = True
    elif _unit_verdict == "convertible":
        _unit_ok = True
    elif _unit_verdict == "unusable":
        _unit_ok = False
    _convert_ctx = None
    for _key in ("convert_ctx", "convert_context", "ctx_value", "unit_ctx", "ctx"):
        _v = binding.get(_key)
        if isinstance(_v, dict) and _v:
            _convert_ctx = _v
            break
    # 机械侧单位换算（契约 §5-WS4 ④）：把工程量换算到**台班定额的分母单位**
    # （例：叶子「根」、定额「台班/m」、桩长 18 m/根 → factor=18，120 根 → 2160 m）。
    # ⚠️ 必须把 ctx 传进去：不传就永远判 unusable（根 ↔ m 是跨族换算，只认工程参数）。
    _machine_pair = None
    if mode == "machine":
        _bound_factor = _pos(binding.get("convert_factor"))
        _bound_denom = str(binding.get("convert_denominator")
                           or binding.get("norm_denominator") or "")
        if _bound_factor is not None and _bound_denom:
            _machine_pair = {"verdict": "convertible", "factor": _bound_factor,
                             "denominator": _bound_denom, "scale": 1.0,
                             "detail": "绑定层给出的换算系数"}
        else:
            try:
                _machine_pair = kb_units.check_unit_pair(
                    leaf.get("unit") or "", binding.get("unit") or "", _convert_ctx)
            except Exception:
                _machine_pair = None
    # 人工口径的量纲校验（机械任务算"人工配合下限"时要用；量纲不符就不加下限）
    _labor_pair = None
    try:
        _labor_pair = kb_units.check_unit_pair(leaf.get("unit") or "",
                                               binding.get("unit") or "", _convert_ctx)
    except Exception:
        _labor_pair = None
    # 主导方式冲突（norm_bind 写入）：任务描述与所绑定额的口径不是一回事
    # （实测「机械挖基坑土方」绑到"人工挖小坑"定额）。这种定额看着 origin=kb、
    # 置信度"中"，对这条任务却没有依据，必须与 AI 估算同等对待。
    _method_conflict = bool(binding.get("method_conflict"))
    # ---- 第 38 轮：定额能否当"工期证据"，改由**人工预置的默认定额行**决定 ----
    # 旧口径只看"这次临时匹配成功了没有"（origin/match/ai_source + 单位 + 口径冲突），
    # 于是三个洞同时开着：
    #   ① `AI_ESTIMATE_V1` 的 75 条在旧口径里**全部** norm_is_evidence=True
    #      （eg. 测量放线 0.012 工日/m²）→ duration = by_norm 直接覆盖目标工期；
    #   ② 同 L4 下的错误行照样放行（铝模挂到 FORM_NEW_OTHER 的"垫层/带形/木模板"
    #      0.025 工日/m²，量级差 30~50 倍）；
    #   ③ 匹配到哪一行完全由运行时条件消歧决定 —— 同一个 L4 换个描述就可能换一行。
    # 新口径：`norm_bind` 的判定**保留**（它仍决定"绑哪一行、单位对不对"），但再加一道
    # 硬门 `pre_approved`：L4_Norm_Default 里没有一行 review_state='approved' 的，
    # 一律不许当证据 → 退回 `duration = max(target_days, by_norm)`（目标工期当地板）。
    # 这道门**只收紧、不放松**，且不删任何既有判据；上游没建表时行为与旧版一致
    # （表不存在 → pre_approved 全 False → 全体退回目标工期，即"更保守"而非更激进）。
    # ⚠️ 闸门必须按**这条任务实际走的口径**去查（人工 or 机械台班）。用错了口径会
    # 查到"另一半"的 pending 行而误拦 —— 实测机械任务（CONC_NEW_FOUND 等）被人工
    # 口径的行挡住，test_machine_labor_demand 4 个用例连带失败。
    _gate_kind = (_norm_defaults.KIND_MACHINE if mode == "machine"
                  else _norm_defaults.KIND_LABOR)
    _gate_ok, _gate_reason = _norm_defaults.gate_open(
        leaf.get("kb_activity_id"), unit, _gate_kind)
    # 第 39 轮：闸门放行的**来源标注**（state / confidence / source_code）。
    # 真人 parsed/verified 的行没有人工审定也放行，但必须逐条标出来 ——
    # 用户的原话是"不得不用 AI 就用，最后标出来"；这条通路就是"标出来"的入口。
    # `gate_label` 是第 39 轮新增的 API，用 getattr 兜底：测试里给 `_norm_defaults`
    # 装过替身模块，缺这个函数时不能把整条排程打崩。
    _gate_fn = getattr(_norm_defaults, "gate_label", None)
    _gate_label = {}
    if callable(_gate_fn):
        try:
            _gate_label = _gate_fn(leaf.get("kb_activity_id"), unit, _gate_kind) or {}
        except Exception:
            _gate_label = {}
    if not isinstance(_gate_label, dict):
        _gate_label = {}
    # ---- 政策变更（用户 2026-09-20）：AI 经验估算定额**放行**，但必须逐条标注 ----
    # 旧口径：`_origin=="ai"` / `_match=="ai"` / `_ai_source` 任一命中 → norm_is_evidence
    # 判 False（"AI 估算定额只作参考"）。新口径：AI 定额与真人定额**同等**参与工期计算，
    # 只是来源必须标出来。保留的拦截判据一条没少：`_method_conflict`（定额口径与任务
    # 不符）、`_unit_ok`（单位不一致/不可换算）、`_gate_ok`（闸门待审/被否决/定额值缺失）、
    # 以及下面 `binding["usable"] is False` 与"定额值取不到"。
    norm_is_evidence = (not _method_conflict) and _unit_ok and _gate_ok
    # AI 来源有**两条**独立通路，标注时都要认（覆盖率统计同口径，见 `_item_ai_source`）：
    #   ① 绑定自身被标 AI（origin / match_type / source_code 三处任一命中）；
    #   ② 闸门的来源分档说这是 AI 档（`state="released_ai"`，或默认行
    #      `confidence="estimated"` —— 实测 L4_Norm_Default 的 source_code 一律是
    #      `KB_Norm_Labor_Table`，只有 confidence 能区分）。
    # 二者现在都**只是标注用途**，不再是否决开关。
    _gate_state = str(_gate_label.get("state") or "")
    _gate_conf = str(_gate_label.get("confidence") or "")
    _ai_binding = bool(_origin == "ai" or _match == "ai" or _ai_source)
    _ai_norm = bool(_ai_binding or _gate_state == "released_ai"
                    or _gate_conf == "estimated")
    if _ai_norm and norm_is_evidence:
        _ai_label_text = str(getattr(_norm_defaults, "LABEL_AI_ESTIMATE",
                                     LABEL_AI_ESTIMATE) or LABEL_AI_ESTIMATE)
        _gate_label = dict(_gate_label)
        # 闸门已经给了更具体的状态就保留（`released_ai` 同义；`approved` 是人工审定过
        # 的 AI 行，改写成 released_ai 会把"人点过头"这件事抹掉）。
        if _gate_state not in ("released_ai", "approved"):
            _gate_label["state"] = "released_ai"
        _gate_label["label"] = _ai_label_text
        _gate_label["ai_estimate"] = True
        if not str(_gate_label.get("source_code") or ""):
            _gate_label["source_code"] = str(binding.get("source_code") or "")
        _gate_label.setdefault("note", _ai_label_text)

    usable = bool(quantity > 0 and (productivity is not None or norm_value is not None)
                  and norm_is_evidence)
    # 绑定层显式判过"不可用"（如单位不一致且不可换算）→ 一律不参与计算。
    # 这是契约 §5-WS4 ④ 的"verdict == unusable 的定额直接降级"在台账层的落点。
    if binding.get("usable") is False:
        usable = False
    # 旧名保留（有定额值但未被采信）。政策变更（2026-09-20）后它**不再**等于"是 AI"，
    # 只表示"有定额值、但因为口径/单位/闸门/绑定层判不可用而没被采信"。
    has_norm_but_ai = bool(quantity > 0 and (productivity is not None or norm_value is not None)
                           and not norm_is_evidence)
    # 「为什么不可用」要说清楚——这是定额覆盖率口径的唯一真源，别让调用方各自猜。
    # 顺序有讲究：**单位不一致排在口径之前**。ALC 墙板安装这种"绑到了真实定额、
    # 但量纲对不上"的（m² vs 工日/m³）属于可修的数据/配置问题，不能被笼统归成
    # "AI 估算"，否则缺口明细会指向错误的修法。
    # 绑定层（WS3）已经把原因判过一遍（如"缺计量单位""单位不一致且不可换算"）→ 优先读它。
    # 政策变更（2026-09-20）：绑定层可能仍写着旧的 AI 拦截文案（`norm_bind` 本轮不改），
    # 在消费端改写成如实描述 —— AI 来源已不是拦截理由，剩下的是真实的缺行/口径问题。
    _bound_reason = _normalize_bound_reason(binding.get("not_usable_reason"))
    if usable:
        not_usable_reason = ""
    elif not _unit_ok:
        not_usable_reason = _bound_reason or "单位不一致"
    elif not _gate_ok and _gate_reason:
        # 定额本身绑得上、单位也对，但闸门拦下 → 如实报闸门给的原因。第 41 轮
        # （2026-09-20 政策变更）起闸门的拦法只剩两种：
        #   ① `STATE_NO_VALUE` / `REASON_NO_VALUE`：该 L4 的行定额值缺失或非正（<=0）
        #      —— 覆盖率口径里这就是"定额值为空"那一档，归一到既有桶名，不造新桶名；
        #   ② `REASON_REJECTED`：那行被人工显式否决过。
        # （`REASON_AI_ONLY` 已不再返回：AI 经验估算现在放行 + 标注，见 `norm_defaults`。）
        # ⚠️ 闸门的原因必须优先于 `binding["usable"] is False` 的兜底原因：闸门拦下的
        # 条目里绝大多数同时"有定额值但非证据"，若让绑定层那条先命中，缺口明细会全被
        # 写成"定额口径不符"，用户就看不到"这条其实是**定额值缺失**"这个可执行的下一步。
        _no_value_state = str(getattr(_norm_defaults, "STATE_NO_VALUE", "") or "no_value")
        not_usable_reason = ("定额值为空" if _gate_state == _no_value_state
                             else _gate_reason)
    elif binding.get("usable") is False:
        not_usable_reason = _bound_reason or "定额口径不符"
    elif _method_conflict:
        not_usable_reason = "定额口径不符"
    # ⚠️ 政策变更（2026-09-20）删掉了 `elif has_norm_but_ai: "AI估算定额"` 这一支：
    # AI 来源的定额现在会被放行（usable=True），不再产生"不可用原因"；真的没有定额行的
    # 仍落在下面的 `KB无定额行`（那类是真的缺行，不是"AI 所以不用"）。
    elif not binding:
        not_usable_reason = "KB无定额行"
    elif productivity is None and norm_value is None:
        not_usable_reason = "定额值为空"
    else:
        not_usable_reason = "其他"

    return {
        "task_id": tid,
        "name": name,
        "_leaf": leaf,                 # 原始叶子（取机械配员等已由上游补好的字段）
        "quantity": quantity,
        "unit": unit,
        "mode": mode,
        "usable": usable,
        "norm_is_evidence": norm_is_evidence,
        "has_norm_but_ai": has_norm_but_ai,
        # 政策变更（2026-09-20）：这条任务的定额是不是 AI 来源（供覆盖率 `released_ai`）
        "ai_norm_source": _ai_norm,
        "not_usable_reason": not_usable_reason,
        # 第 38 轮：L4 默认定额行的放行状态（供覆盖率报告与"为什么没算定额"追溯）
        "norm_pre_approved": bool(_gate_ok),
        "norm_gate_reason": _gate_reason,
        # 第 39 轮：放行/拦下的**来源**（真人 parsed/verified / AI estimated / 否决）
        "norm_evidence_label": _gate_label,
        "kb_activity_id": str(leaf.get("kb_activity_id") or ""),
        # ---- 域 7.11 判据（**只读 KB；取不到就 None / ""，绝不猜**）----
        # 第一判据 `is_l5_expandable`（0 = 不展开）；面积口径唯一真源 `measure_scope`；
        # 第二判据 = 树内叶子是否已经分过段（`segment_id`）。
        "is_l5_expandable": _l5_expandable_of_activity(leaf.get("kb_activity_id")),
        "measure_scope": _measure_scope_of_activity(leaf.get("kb_activity_id")),
        "segment_id": str(leaf.get("segment_id") or ""),
        "target_days": target_days,
        "own_duration": own_duration,
        "crew_design": crew_design,
        "crew_declared": crew_declared,
        "basis": basis,
        "norm_value": norm_value,
        "machine_norm_unit_pair": _machine_pair,
        "labor_norm_unit_pair": _labor_pair,
        "productivity": productivity,
        "cap_labor": cap_labor,
        "cap_machine": cap_machine,
        # ---- 第 39 轮：上限重定（人工侧）的台账留痕，供 norm_coverage_report 统计 ----
        "crew_max_raw": crew_max_raw,                 # 标定行的原 crew_max（可能 None）
        "crew_max_effective": crew_max_effective,     # 重定后实际上限（可能 None）
        "crew_ceiling_raised": bool(
            crew_max_raw is not None and crew_max_effective is not None
            and float(crew_max_effective) > float(crew_max_raw)),
        "segments_factor": workface.get("segments_factor") if workface else None,
        "parallel_segments": parallel_segment_count(leaf, workface),
        # 保留旧键：语义已改成"容量行是标定数据、不是证据门槛"（恒为 True/False 仅为兼容）
        "workface_is_evidence": bool(cap_labor is not None or cap_machine is not None
                                     or workface),
        "workface_capacity": {
            "max_labor": cap_labor,
            "max_machine": cap_machine,
            "unit_basis": unit_basis,
            "source_type": workface.get("source_type") or workface.get("origin"),
            "confidence": workface.get("confidence"),
            "model_version": workface.get("model_version"),
        } if workface else {},
        "workface_advisory": {
            "max_labor": _pos(workface.get("max_labor")),
            "max_machine": _pos(workface.get("max_machine")),
            "source_type": workface.get("source_type") or workface.get("origin"),
            "confidence": workface.get("confidence"),
        } if workface else {},
        "unit_basis": unit_basis,
        "labor_name": labor_name,
        "machine_name": machine_name,
        "machine_crew": crew_machines,
        "has_crew": bool(crew_machines) or bool(crew_labors),
    }


# ==================== 单版计算 ====================
def _apply_ceiling(tid, name, raw, ceiling, reason, kind, unit_text):
    """按上限压人数/台数；被压则返回 (压后值, capped 记录)，否则 (原值, None)。

    封顶一律留痕（want=原值 / got=压后值 / reason=中文原因），绝不静默。
    """
    raw = int(max(1, raw))
    if ceiling is None or raw <= ceiling:
        return raw, None
    got = int(max(1, ceiling))
    rec = {
        "task_id": tid,
        "resource": name,
        "want": raw,
        "got": got,
        "reason": "%s：%s 由 %d %s 压到 %d %s（总工程量不变，工期相应延长）"
                  % (reason, name, raw, unit_text, got, unit_text),
    }
    return got, rec


def _cap_record(tid, name, want, got, reason, unit_text, detail=""):
    """一条"班组/台数为什么是这个数"的留痕（契约：封顶一律记进 capped，绝不静默）。

    `detail` 为空时用默认句式；`want == got` 表示**没有被压**，只是说明取值的来源
    （工作面容量顶满 / 缺数据兜底 / 上游已指定班组），调用方据此区分两种留痕。
    """
    if not detail:
        detail = "%s：%s 由 %d %s 压到 %d %s（总工程量不变，工期相应延长）" \
                 % (reason, name, int(want), unit_text, int(got), unit_text)
    return {
        "task_id": tid,
        "resource": name,
        "want": int(want),
        "got": int(got),
        "reason": "%s：%s" % (reason, detail),
    }


def _user_limit_of(resource_name, limits, honor_user_limits):
    """用户在**本版**给出的该资源限额（不生效时返回 None）。"""
    if not honor_user_limits or not isinstance(limits, dict):
        return None
    for key in ("equipment", "by_trade"):
        limit, _k = _match_limit(resource_name, limits.get(key) or {})
        if limit is not None:
            return limit
    return None


def machine_total_shifts(item):
    """机械主导的总台班（**口径与人工正式分岔**，见 _build_ledger_item 的注释）。

    台班定额没有归一：`norm_value` 仍是「台班 / (quantity_basis × 定额分母单位)」。
      ① 定额分母单位与叶子单位**可换算**（kb_units.check_unit_pair 给得出 factor）
         → 先把工程量换算到定额分母单位，再用 `basis=1` 的等效口径算总台班；
      ② 分母缺失 / 不可换算（如叶子「根」vs 定额「台班/m」且工程量单位没写）
         → 退回基准口径 `quantity ÷ quantity_basis × norm_value`
           （旧实现就是这么算的，测试与实物口径都按它标定）。
    人工侧**永远不乘 basis**（那里的定额值已归一），千万别把两段合起来改。
    """
    qty = item["quantity"]
    norm = item["norm_value"]
    basis = _pos(item.get("basis"), 1.0) or 1.0
    unit = item.get("unit") or ""
    pair = item.get("machine_norm_unit_pair")
    # 机器可读的形式统一写成「(量 × 台班定额) ÷ 基准量」：与 `q ÷ basis × norm` 等价，
    # 但不会与人工侧那条已废弃的 `basis/norm` 镜像写法混淆（含 AST 守卫）。
    base_default = (qty * norm) / basis
    # ⚠️ `kb_units.check_unit_pair` 的 verdict 只有 same | convertible | unusable，
    # **没有 "usable"** —— 旧写法 `!= "usable"` 让换算分支永不触发，跨族换算
    # （根 → m，桩长 18 m/根）永远算不出来：120 根 × 0.49 ÷ 100 = 0.588 台班（应为 10.58）。
    if not isinstance(pair, dict) or pair.get("verdict") not in ("same", "convertible"):
        return base_default
    conv = _pos(pair.get("factor"))
    denom = str(pair.get("denominator") or "")
    if conv is None or not denom:
        return base_default
    # 换算后的工程量（定额分母单位）÷ 基准量 × 台班定额（例：2160 m ÷ 100 × 0.49）
    return (qty * conv * norm) / basis


# 【已删除】_rebalance_total_labor（按任务数摊派总人工额度的旧实现）
# 旧实现把"全项目同时在岗上限"当成"把额度平均分给计划里的每一条任务"：
# quota = 本班组人数 × 总额度 ÷ **所有任务**人数之和。415 条任务的计划里，
# 每条任务只能分到 1 人 → 工期从 600 天飙到 7734 天（实测）。
# 正确口径是**逐日并发**：当天所有在施任务的人数之和 ≤ 上限。现在由
# serial_sgs(total_labor_limit=...) + earliest_window_within_total() 逐日复核，
# 超了就顺延起点，顺延也放不下时记 warning 并落进 over_limit（绝不静默超限）。


# ==================== 域 7.2（T2-2.2）：三轮回压的三个纯函数 ====================
# 与上面那个 `_rebalance_total_labor` 墓碑**无关**：它按"任务数"把全项目人工额度摊派给
# 每条任务（实测把 600 天抬到 7734 天）；域 7.2 **只收窄"当天能同时用多少"这一维**，
# 权重是各任务在该资源上的**需求量**（7.3），工期公式、段容量、定额一个字都不动。


def _demand_weight(item, plan, rname):
    """域 7.3 / §3.3.2：该任务在**这个资源**上的需求量（工日 / 台班）—— 分摊权重。

    口径（设计 §3.3.2，**不许自创**）：
      · 机械主导 → `machine_total_shifts(item)`（总台班）；
      · 人工主导 → `person_days_of(item)`（定额分母单位的工程量 ÷ 每人每天产量）；
      · 取不到   → `_organization["demand"]`（`plan_capacity_chain` 算好的同一个量）；
      · 仍取不到 → **0**：该条**不参与分摊**（调用方进 warning，**绝不猜**）。
    三者**同一量纲**（工日或台班），所以同一资源内部可比 —— 这正是"不按施工量"的原因
    （施工量在 m²/m³/t 之间不可比）。任何异常都返回 0（回压绝不让流水线崩）。
    """
    try:
        if str((plan or {}).get("resource_kind") or "") == "machine":
            w = _pos(machine_total_shifts(item), None)
        else:
            w = _pos(person_days_of(item), None)
        if not w:
            org = (plan or {}).get("organization") or {}
            w = _pos(org.get("demand"), None) or _pos(org.get("person_days"), None)
    except Exception:
        w = None
    return float(w) if w and w > 0 else 0.0


def _usage_by_day(curves, rname):
    """某资源在逐日曲线里的**当日用量**：`{day(int): 用量}`（机械在 `items`、人工在 `trades`）。"""
    out = {}
    for c in curves or []:
        v = (c.get("items") or {}).get(rname)
        if v is None:
            v = (c.get("trades") or {}).get(rname)
        if v:
            out[int(c.get("day") or 0)] = v
    return out


def resource_backpressure(ledger, planned, rows, curves, limits, round_no, *,
                          honor_user_limits=True, site_const=None):
    """域 7.2 / 7.3：**一轮回压** —— 拿本轮的逐日曲线找出超限资源，按**需求量**重新分摊
    当天的日池上限，得到下一轮各资源的逐日份额。

    返回 `(next_share, over_records, notes)`：
      · `next_share`   : `{资源名: {task_id(str): {day(int): 份额(int ≥ 1)}}}`；
                        份额 0 的条目**不写进去**（`effective_capacity_daily` 对 ≤0 抛异常，
                        绝不能把"算错"变成"工期无穷"）。
      · `over_records` : `[{resource, limit, peak, note, round, breached}]`
        —— 域 7.2 裁决 #8：**复用既有 `over_limit` 的 `{resource, limit, peak, note}`**，
        键只增不改（`round` / `breached` 是新增键）。
      · `notes`        : 人可读中文轮次说明（进 `_backpressure.trace`，裁定 #8 的"轮次说明"）。

    纪律：
      · 用户没给该资源的限额 → **不回压**（`_user_limit_of` 返回 None 即跳过）；
      · 需求量权重取不到（= 0）→ 该条**不参与分摊**并留痕，**绝不猜**；
      · 场地级机械与跟台数走的配员 → **不判超限、不参与回压**（7.8 / 7.9）；
      · 分摊一律交给 `segment_capacity.largest_remainder_by_demand`（**不自己写分摊**）；
        限额 < 当天参与分摊条数 → 该函数按 7.6 突破限额，这里把 `breached` 记进记录；
      · 全部遍历 `sorted()`、权重走 `Fraction`（在 `segment_capacity` 内）→ 逐位可复现。
    """
    next_share, over, notes = {}, [], []
    if not honor_user_limits:
        return next_share, over, notes
    site_machines, site_roles = _site_machine_registry(site_const=site_const, limits=limits)

    # ---- ① 按资源汇总"当天需要它的任务集合"（权重 = 该资源上的需求量）----
    active = {}
    weights = {}
    zero_weight = []
    for tid in sorted(planned):
        plan = planned[tid] or {}
        res = plan.get("resources") or {}
        if not res:
            continue
        row = rows.get(tid) or {}
        es, ef = int(row.get("es") or 0), int(row.get("ef") or 0)
        if ef <= es:
            continue
        for rname in sorted(res):
            if rname in site_machines or rname in site_roles:
                continue                    # 域 7.8 / 7.9：不判超限
            w = _demand_weight(ledger.get(tid) or {}, plan, rname)
            if w <= 0:
                zero_weight.append((str(tid), rname))
                continue                    # 需求量取不到 → 不参与分摊（不猜）
            weights.setdefault(rname, {})[str(tid)] = w
            for day in range(es, ef):
                active.setdefault(rname, {}).setdefault(day, []).append(str(tid))

    # ---- ② 逐资源逐日：实际用量 vs 限额 ----
    for rname in sorted(active):
        limit = _user_limit_of(rname, limits, honor_user_limits)
        if limit is None:
            continue                        # 用户没给 → 不限（7.8 / 口径总表 3）
        peaks = _usage_by_day(curves, rname)
        if not peaks:
            continue
        worst = max(peaks.values())
        if worst <= limit + 1e-9:
            continue                        # 本资源本轮收敛
        # ---- ③ 7.3 分摊：`alloc_i = 限额 × 各自需求量 ÷ 当天合计需求` ----
        share_of_day = {}
        breached_any = False
        for day in sorted(active[rname]):
            pairs = sorted(active[rname][day])          # (tid 升序 → 平局裁决可复现)
            dem = [weights[rname][t] for t in pairs if t in weights[rname]]
            pairs = [t for t in pairs if t in weights[rname]]
            if not pairs:
                continue
            alloc, _trace = segment_capacity.largest_remainder_by_demand(
                int(limit), dem, pairs)
            if alloc.breached:
                breached_any = True
            for tid, n in zip(pairs, alloc.allocated):
                n = int(n)
                if n > 0:                   # 7.5/7.6：份额恒 ≥ 1；0 不写进份额表
                    share_of_day.setdefault(tid, {})[int(day)] = n
        if share_of_day:
            next_share[rname] = share_of_day
        note = ("第 %d 轮：%s 峰值 %s > 用户限额 %d → 按**需求量**重新分摊当天日池上限"
                "（%d 天有在施任务）"
                % (round_no, rname, _r2(worst), int(limit), len(active[rname])))
        if breached_any:
            note += ("；且**用户限额 %d < 当天参与分摊的任务数** → 按域 7.6 突破限额"
                     "（每条至少 1），如实标出" % int(limit))
        if not share_of_day:
            note += "；**分摊不出份额**（需求量权重取不到）→ 不猜，本轮按超限额值保留"
        notes.append(note)
        over.append({
            "resource": rname,
            "limit": int(limit),
            "peak": _r2(worst),
            "note": ("第 %d 轮回压后仍超限：当日峰值 %s > 限额 %d 人/台%s"
                     % (round_no, _r2(worst), int(limit),
                        "；限额 < 当天参与分摊的任务数 → 按域 7.6 突破限额"
                        if breached_any else "")),
            "round": int(round_no),
            "breached": bool(breached_any),
        })
    if zero_weight:
        # 只对**用户真的给了限额**的资源留痕（用户没给限额的资源本来就不回压，
        # 报"权重取不到"是噪声）—— 绝不猜需求量，也绝不静默。
        _limited = sorted("%s/%s" % (tid, rname) for tid, rname in zero_weight
                          if _user_limit_of(rname, limits, honor_user_limits) is not None)
        if _limited:
            notes.append("需求量权重取不到（不参与分摊、绝不猜）：%s"
                         % "、".join(_limited[:8]))
    return next_share, over, notes


def _merge_share(old, new):
    """域 7.2：把本轮算出的份额并进累计份额 —— **只收窄，不放大**（逐格取 `min`）。

    只收窄的理由（设计 §3.2.5）：每轮的实际用量因此单调不增，回压不会发散；
    旧份额里出现、新份额里没有的天**原样保留**（收敛过的天不能被后面几轮放大回去）。
    """
    out = {}
    for rname in sorted(set(old or {}) | set(new or {})):
        a = (old or {}).get(rname) or {}
        b = (new or {}).get(rname) or {}
        per_task = {}
        for tid in sorted(set(a) | set(b)):
            merged = {}
            for day, val in sorted((a.get(tid) or {}).items()):
                if int(val) > 0:
                    merged[int(day)] = int(val)
            for day, val in sorted((b.get(tid) or {}).items()):
                if int(val) <= 0:
                    continue
                day = int(day)
                merged[day] = int(val) if day not in merged else min(merged[day], int(val))
            if merged:
                per_task[str(tid)] = merged
        if per_task:
            out[str(rname)] = per_task
    return out


def _share_of(share, task_id, window=None):
    """域 7.2 / 裁决 #7：该任务本轮应拿到的**逐日份额最小值**（正整数），取不到 → `None`。

    `share`  : `{资源名: {task_id: {day: 份额}}}`（`resource_backpressure` 的产物，
               已被 `_merge_share` 收窄）。
    `window` : 该任务**上一轮排期窗口** `(es, ef)`；给了就只在 `[es, ef)` 天内取最小
               —— 裁决 #7 的"**该任务窗口内份额的最小值**"。不给 → 它全部有份额的天里取最小。
    跨资源取**最小**：`_plan_task` 的 `有效容量` 只能是**一个标量**（口径总表唯一公式
    `工期 = ⌈需求量 ÷ 有效容量⌉`），所以取该任务所有资源里最紧的那一条。
    """
    best = None
    for rname in sorted(share or {}):
        days = (share[rname] or {}).get(str(task_id))
        if not isinstance(days, dict) or not days:
            continue
        vals = []
        if window:
            es, ef = int(window[0]), int(window[1])
            vals = [days[d] for d in sorted(days) if es <= int(d) < ef]
        if not vals:
            vals = [days[d] for d in sorted(days)]
        if not vals:
            continue
        val = min(int(v) for v in vals)
        if val > 0 and (best is None or val < best):
            best = val
    return best


def _daily_share_snapshot(share):
    """域 7.12：把内部份额定型成**可序列化快照** `_daily_share`（设计 §4.4）。

    形状（一字不差）：
      `{"资源名(str)": {"task_id(str)": {"day(str)": 份额(int ≥ 1)}}}`
    遍历一律 `sorted()`；**返回全新 dict（不是 generator / 不是视图）** ——
    `_run_one_version` 返回前必须定型，否则第二次读取结果不同（7.12 冻结要求）。
    """
    out = {}
    for rname in sorted(share or {}):
        block = {}
        for tid in sorted((share[rname] or {})):
            days = (share[rname] or {}).get(tid) or {}
            block[str(tid)] = dict((str(int(d)), int(days[d])) for d in sorted(days))
        if block:
            out[str(rname)] = block
    return out


def _rebalance_removed_tombstone():
    """（占位：原 `_rebalance_total_labor` 墓碑见上方注释，域 7.2 **不复活**它。）"""
    return None



def _run_one_version(ledger, preds, order, limits, warnings, tag, honor_user_limits,
                     crew_plan=None, reuse_declared_crews=False, cadence_days=None,
                     cadence_scope=None, face_area=None, cadence_source=None,
                     user_rule=None, *, site_const=None, area_params=None,
                     daily_share_map=None, prev_rows=None, round_no=1):
    """跑**一轮**：先定班组/工期，再串行排程，最后出曲线、统计与本轮的超限结论。

    **域 7.2（T2-2.3）**：本函数只跑一轮，三轮回压的外层循环是
    `_run_version_with_backpressure()`（它按 `BACKPRESSURE_MAX_ROUNDS` 常量调用本函数
    1–3 次）。这样"一轮"的语义可以单独测，而热路径不必为一层循环重排缩进。

    新增的 keyword-only 形参（**键只增不改**，缺省 `None` = 老行为逐字段不变）：
      · `site_const`      : `boundary_conditions["site_machine_const"]`（7.8 / 7.9 判据来源）；
      · `area_params`     : 域 7.11 的面积证据入参（`total_area` / `floor_areas` / 栋数 / 层数）；
      · `daily_share_map` : 上一轮回压出的逐日份额 `{资源: {task_id: {day: 份额}}}`；
      · `prev_rows`       : 上一轮的排期行（裁决 #7：份额取"该任务窗口内"的最小值）；
      · `round_no`        : 轮次（只进留痕，不参与计算）。

    本轮新增返回键（**键只增不改**）：`_backpressure`（本轮 trace / next_share / over）与
    `_daily_share`（本轮**实际采用**的份额快照，域 7.12 冻结）。
    """
    planned = {}          # task_id -> {duration, resources, resource_id, kind, crew, capped}
    capped_all = []
    over_limit = []
    organization_gaps = []
    _nocap = []           # 缺工作面容量（没编人数）的任务
    crew_plan = crew_plan or {}
    # 域 7.8 / 7.9：场地级机械名 + 跟台数走的配员角色（判据来自 `site_machine_const` 键集）
    _site_machines, _site_roles = _site_machine_registry(site_const=site_const, limits=limits)
    # 本轮的 warning 先进暂存表：`_run_version_with_backpressure` 会调用本函数多轮，
    # 直接用共享的 `warnings` 会把同一条文案重复 3 遍（= 回归）。
    round_warnings = []
    _daily_share_map = daily_share_map or {}
    _prev_rows = prev_rows or {}

    # 两种"上限"必须分开（这是本节点最容易出错的地方）：
    #   ① 班组上限（逐条任务）：决定人数/台数与工期，见 _plan_task；
    #   ② 资源池上限（逐资源，取所有任务里的最小值）：
    #      同一个工种/机械在**同一时刻**能同时投入多少，是排程期并发检查的口径。
    #      池上限不随任务变（否则各任务各按各的，和 8+8=16 超 8 也发现不了）。
    # ② 里还要并上用户限额：用户给的"钢筋工只能 8 人"本来就是同时段总量上限。
    #      它只影响"谁先谁后"，不改变班组规模与定额。
    pool = {}
    for tid in order:
        item = ledger[tid]
        plan = _plan_task(item, limits, honor_user_limits, crew_plan,
                          reuse_declared_crews=reuse_declared_crews,
                          cadence_days=cadence_days, cadence_scope=cadence_scope,
                          face_area=face_area, cadence_source=cadence_source,
                          user_rule=user_rule,
                          daily_share=_share_of(
                              _daily_share_map, tid,
                              (_prev_rows[tid]["es"], _prev_rows[tid]["ef"])
                              if tid in _prev_rows else None))
        planned[tid] = plan
        capped_all.extend(plan["capped"])
        if (not plan.get("resources")
                and str(item.get("mode") or "labor") == "labor"
                and item.get("usable")):
            # C8-8：缺工作面容量 → **如实报缺**（不按 AI 估算补班组）。逐条进 warning。
            _nocap.append(str(tid))
        if plan.get("organization") and not plan["organization"].get("feasible"):
            organization_gaps.append(org_gap_record(item, plan))
        for rname in (plan.get("resources") or {}):
            cap = workface_cap_of(item, rname)
            org = plan.get("organization")
            if org and rname == plan.get("resource_id"):
                # 组织层把"每面人数 × 作业面数"当成该工种**同时投入**的总量
                # （实测 5.1.1.1：3 面 × 17 人 = 51 人）。池上限必须跟着走，
                # 否则 51 人永远"超过每面容量 16 人"，并发检查会静默失效。
                cap = max(cap, int(org.get("crew_total") or 0) or cap)
            if honor_user_limits and rname not in _site_machines and rname not in _site_roles:
                # 域 7.8 / 7.9：场地级机械（塔吊 / 施工电梯）与**跟台数走的机组配员**
                # （司机 / 信号工…）**不受用户限额夹** —— 判据来自 `site_machine_const`
                # 的键集（`_site_machine_registry`），**不写死资源名 / 角色名**。
                # "人不够"的唯一物理办法是**少上机器**（见 `_plan_task` 的 `_max_by_crew`），
                # 不是给司机单独发限额。
                user_cap, _key = _match_limit(rname, limits.get("equipment") or {})
                if user_cap is None:
                    user_cap, _key = _match_limit(rname, limits.get("by_trade") or {})
                if user_cap is not None:
                    cap = min(cap, user_cap)
            pool[rname] = min(pool.get(rname, cap), cap)
    for tid in order:
        planned[tid]["limits"] = dict(pool)
    if _nocap:
        round_warnings.append(
            "缺工作面容量数据 %d 条（%s）：MWI 表（`Resource_Workface_Index`）里没有"
            "该资源、或 `resource_mobility` 缺列、或缺层面积 —— 这些任务**不按 AI 估算"
            "补班组**，资源与班组留空、工期沿用叶子原值；**它们的工期不随工程量变化"
            "（改工程量也不会变长/变短）**，请补 MWI 行或给出层面积/用户同类限额"
            % (len(_nocap), "、".join(sorted(_nocap)[:8])
               + ("…" if len(_nocap) > 8 else "")))

    # ---- 总人工上限（labor.peak_total）----
    # 它是"全项目同时在岗人数的天花板"——**逐日并发**口径，必须在排程期按天检查，
    # 不能预先"把额度按任务数平均分掉"：415 条任务每条只分到 1 人，工期会从 600 天
    # 抬到 7734 天（实测踩过这个坑）。所以这里交给 serial_sgs 逐日复核 + 顺延。
    total_limit = limits.get("labor_total") if honor_user_limits else None

    result = serial_sgs(order, planned, preds, round_warnings, tag,
                        total_labor_limit=total_limit)
    for plan in planned.values():
        for rec in plan.get("capped") or []:
            if isinstance(rec, dict) and rec not in capped_all:
                capped_all.append(rec)
    rows = result["rows"]

    curves = daily_curves(rows, result["total_duration_days"], planned)
    peak_labor = max([c["labor"] for c in curves] or [0])
    peak_equipment = max([c["equipment"] for c in curves] or [0])

    if honor_user_limits:
        _next_share, _over_round, _bp_notes = resource_backpressure(
            ledger, planned, rows, curves, limits, int(round_no),
            honor_user_limits=honor_user_limits, site_const=site_const)
        _bp = {
            "cap": int(BACKPRESSURE_MAX_ROUNDS),
            "rounds_used": int(round_no),
            "converged": not _over_round,
            "share_source": BACKPRESSURE_SHARE_SOURCE,
            "trace": [{"round": int(round_no),
                       "over": [{"resource": r["resource"], "limit": r["limit"],
                                 "peak": r["peak"]} for r in _over_round],
                       "notes": list(_bp_notes)}],
            "note": "",
            "next_share": _next_share,
            "over": _over_round,
        }
        # 裁决 #8：严格超限的记录（若还有）**复用既有 `over_limit` 形状**（键只增不改）；
        # 收敛 / 无严格超限时走原来的"已达上限"记录 → 老场景逐字段不变。
        over_limit = _over_round or _over_limit_records(
            curves, peaks_from_curves(curves), limits, site_const=site_const)
    else:
        _bp = {"cap": int(BACKPRESSURE_MAX_ROUNDS), "rounds_used": int(round_no),
               "converged": True, "share_source": BACKPRESSURE_SHARE_SOURCE,
               "trace": [{"round": int(round_no), "over": [], "notes": []}],
               "note": "", "next_share": {}, "over": []}
    # 本轮的 warning 一次性并进共享表（多轮调用时不会重复刷屏）
    warnings.extend(round_warnings)

    schedule = [rows[tid] for tid in sorted(rows)]
    return {
        "total_duration_days": result["total_duration_days"],
        "schedule": schedule,
        "critical_path": [],
        "daily_labor": [{"day": c["day"], "total": c["labor"], "trades": c["trades"]}
                        for c in curves],
        "daily_equipment": [{"day": c["day"], "total": c["equipment"], "items": c["items"]}
                            for c in curves],
        "peak_labor": _r2(peak_labor),
        "peak_equipment": _r2(peak_equipment),
        "over_limit": over_limit,
        "capped": _dedup_capped(capped_all),
        "organization_gaps": organization_gaps,
        "_planned": planned,
        "_curves": curves,
        # ---- 域 7.2 / 7.12 新增（键只增不改；`_` 前缀 → 不进 `_public_version`）----
        "_backpressure": _bp,
        "_daily_share": _daily_share_snapshot(_daily_share_map),
        "_rows": rows,
    }


def _run_version_with_backpressure(ledger, preds, order, limits, warnings, tag,
                                   honor_user_limits, **kw):
    """域 7.2（T2-2.3）：把一个版本跑 **最多 `BACKPRESSURE_MAX_ROUNDS` 轮**（常量，不是 while）。

    第 1 轮没有份额（与改造前逐字段相同）；后续轮拿上一轮的超限结论
    （`resource_backpressure`）按**需求量**重新分摊当天的日池上限，**只收窄不放大**
    （`_merge_share`），份额经 `_plan_task(daily_share=…)` 只作用在 `eff` 上（拉长工期）。

    · `honor_user_limits=False` → 第 1 轮即收敛（不做回压，`rounds_used=1`）；
    · 用户没给任何限额 → `resource_backpressure` 找不出超限资源 → 第 1 轮收敛；
    · 3 轮后仍超限 → **采用超限额值 + 如实标出**（note 追加"已取满 3 轮…采用超限额值"
      + 一条中文 warning），**绝不抛异常、绝不无限循环**。
    """
    prev_share, prev_rows = {}, {}
    used_share = {}
    version = None
    trace, over_last, note = [], [], ""
    rounds = 0
    for round_no in range(1, int(BACKPRESSURE_MAX_ROUNDS) + 1):
        rounds = int(round_no)
        used_share = prev_share
        version = _run_one_version(ledger, preds, order, limits, warnings, tag,
                                   honor_user_limits, daily_share_map=prev_share,
                                   prev_rows=prev_rows, round_no=round_no, **kw)
        _bp = version.get("_backpressure") or {}
        trace.extend(list(_bp.get("trace") or []))
        over_last = list(_bp.get("over") or [])
        if _bp.get("converged") or round_no >= int(BACKPRESSURE_MAX_ROUNDS):
            break
        prev_share = _merge_share(prev_share, _bp.get("next_share") or {})
        prev_rows = version.get("_rows") or {}
    converged = not over_last
    if honor_user_limits and not converged:
        for rec in over_last:
            rec["note"] = ("%s；已取满 %d 轮回压上限（不是收敛为止）→ "
                           "**采用超限额值**并如实标出"
                           % (rec["note"], int(BACKPRESSURE_MAX_ROUNDS)))
        note = ("经 %d 轮回压后仍有 %d 项资源超用户限额（%s）：按口径"
                "**采用超限额值**并如实标出；若要真正不超额，请放宽限额或延长工期。"
                % (int(BACKPRESSURE_MAX_ROUNDS), len(over_last),
                   "、".join("%s 限额 %s→峰值 %s" % (r["resource"], r["limit"], r["peak"])
                             for r in sorted(over_last,
                                             key=lambda x: str(x["resource"])))))
        warnings.append("资源不超额工期（%s）：%s" % (tag, note))
    _bp = {"cap": int(BACKPRESSURE_MAX_ROUNDS), "rounds_used": int(rounds),
           "converged": bool(converged), "share_source": BACKPRESSURE_SHARE_SOURCE,
           "trace": trace, "note": note}
    version["_backpressure"] = _bp
    # 域 7.12 冻结：`_daily_share` = **最后一轮实际采用**的份额（不是 generator、不是视图）
    version["_daily_share"] = _daily_share_snapshot(used_share)
    return version


def _is_crew_bound_machine(name):
    """机种是否是"整台设备占一个机组全程盯守"（桩机 / 吊装 / 钻机）。

    只有这类机种的工期才需要过 `_machine_labor_days` 的人工配合下限：
    它们的台班定额只数机械占用时间，人工配合（对位、吊装、接桩、清孔）量级完全不同。
    土方 / 夯实 / 泵送 / 运输这类"人只是配合"的机种不加下限（见调用处注释）。
    """
    text = str(name or "")
    return any(k in text for k in ("桩机", "打桩", "压桩", "吊", "钻机", "成槽机"))


def _machine_labor_days(item, machines):
    """机械主导任务的"人工配合下限"天数（只用来给工期设下限，不改台数）。

    口径：总工日 = 工程量 ÷ 人工产能，再 ÷ 工作面能容纳的人数（班组）。
    量纲不一致时用 `check_unit_pair` 换算；换算不出来（返回 None）就**不加下限**，
    绝不拿量纲不明的数去拉长工期。返回 None 表示这条任务没有可用的人工口径。
    """
    productivity = _pos(item.get("productivity"))
    if productivity is None or productivity <= 0 or machines <= 0:
        # ⚠️ 这里**不再**回退到"台班定额取倒数"当人工产能：台班定额不是工日定额，
        # 它只数了机械占用时间（没有人工配合系数），拿它当人日需求会算得离谱地长。
        # 只有绑定层**真给了人工产能**（binding["productivity_value"]）的机械任务
        # 才允许设人工配合下限。
        return None
    qty = item["quantity"]
    mpair = item.get("machine_norm_unit_pair")
    lpair = item.get("labor_norm_unit_pair")
    if not isinstance(mpair, dict) or not isinstance(lpair, dict):
        return None
    if mpair.get("verdict") not in ("same", "convertible"):
        return None
    if lpair.get("verdict") not in ("same", "convertible"):
        return None
    conv = _pos(lpair.get("factor"))
    if conv is None:
        return None
    # ⚠️ 台数 > 工作面人工容量时**不加下限**：说明这条任务的瓶颈是机械，人只是配合
    #    （实测 3.2.3 土方外运：机械 3 台 vs 人工容量 15 人 → 机械口径 2 天是对的）。
    #   反过来（桩基：机械 2 台 < 人工容量 8 人）说明"人多机器少"，工期必须长到
    #   容得下这些人：2.1.1 实测台班 0.59 台班 → 1 天，而 120 根桩要 59 人日 ÷ 8 人 = 8 天。
    cap_machine = _pos(item.get("cap_machine"))
    cap_labor = _pos(item.get("cap_labor"))
    if cap_machine is None or cap_labor is None or cap_machine > cap_labor:
        return None
    # 两侧定额分母必须是**同一个工程量单位**（台班「台班/根」vs 人工「工日/根」），
    # 否则是两套工艺的定额在互相比，没有可比性。
    m_den = str(mpair.get("denominator") or "")
    l_den = str(lpair.get("denominator") or "")
    if kb_units.normalize_unit(m_den) != kb_units.normalize_unit(l_den):
        return None
    person_days = (qty * conv) / productivity
    crew_cap = int(max(1, cap_labor or DEFAULT_MACHINE_FALLBACK))
    return int(max(1, math.ceil(person_days / crew_cap)))


def workface_cap_of(item, resource_name):
    """该资源在**工作面**口径下的上限（与是否看用户限额无关）。"""
    if item["mode"] == "machine" or is_machine_name(resource_name):
        return item["cap_machine"] if item["cap_machine"] is not None else DEFAULT_CEILING
    if resource_name == item["labor_name"]:
        return item["cap_labor"] if item["cap_labor"] is not None else DEFAULT_CEILING
    return DEFAULT_CEILING          # 机械配员这类不单独封顶（随台数走）


# ==================== 施工组织层（2026-09-21 C 组：唯一路径）====================
# 资源只来自**工作面容量**；工期只有一个公式 `ceil(需求量 ÷ 有效容量)`。
# 链路：【0】层面积 →【1】MSSA=500 切段 →【2】段容量 = ceil(段面积 ÷ MWI)
#       →【3】需求量 = 工程量 × 定额 →【4】有效容量 = min(汇总容量, 用户同类限额)
#       →【5】工期 = ceil(需求量 ÷ 有效容量) →【6】投入资源 = 有效容量
# 节拍（`cadence_days`）只写进产物作**对比展示**，不参与任何计算（裁定 8 / C11）。
# 已删除：节拍驱动作业面规划、「四支人数来源」、96 人预算摊派、η 效率折减、
#        ×2.5 带（C8 删除清单 1/3/4/5/6/7）。


def _l5_expandable_of_activity(activity_id):
    """域 7.11 **第一判据**：该 L4 的 `is_l5_expandable`（1 / 0）；取不到 → `None`（不猜）。

    只读 KB、异常 → `None`（懒 import `resource` 破包级循环依赖）。`None` = **未知**，
    调用方（`_segment_table_for_item`）必须按"可展开"处理 —— 即**不切**施工面积口径。
    """
    aid = str(activity_id or "").strip()
    if not aid:
        return None
    try:
        from . import resource as _resource
        return _resource.activity_l5_expandable(aid)
    except Exception:
        return None


def _measure_scope_of_activity(activity_id):
    """域 7.11 **面积口径的唯一真源**：该 L4 自己的 `measure_scope`。

    取不到 → `""`（= "未填"，由 `face_area_for_activity` 回退 Σ floor_areas 并留痕）。
    只读 KB、异常 → `""`（不猜）。**不用** `_task_measure_scope`（那会过词表并优先取叶子声明）。
    """
    aid = str(activity_id or "").strip()
    if not aid:
        return ""
    try:
        from . import resource as _resource
        return str(_resource.activity_measure_scope_of_l4(aid) or "")
    except Exception:
        return ""


def _area_params_of(params):
    """域 7.11 的面积证据入参：**只用已有建筑参数**（不发明数据源）。

    取 `total_area` / `floor_areas` / `building_count` / `floors`（缺的不塞空值），
    `params` 非 dict → 空字典（`face_area_for_activity` 会据此报缺，绝不编面积）。
    """
    if not isinstance(params, dict):
        return {}
    out = {}
    for key in ("total_area", "floor_areas", "building_count", "floors"):
        if params.get(key) is not None:
            out[key] = params[key]
    return out


def segment_rule_of(boundary):
    """用户显式分段规则（裁定 E，键名 `boundary_conditions.segment_rule`）→ 原样返回。

    **只消费，不发明**：取出 `boundary_conditions["segment_rule"]`（或 `params` 里同名键
    由抽取侧 W3-C 落进去），形状由 `segment_plan.compute_segment_areas(user_rule=...)`
    定义（int 段数 / 面积序列 / dict），本函数不解析、不改写。
    取不到 → `None` → 走 MSSA=500（裁定 2/5）。**用户规则优先于一切**（裁定 11）。
    """
    if not isinstance(boundary, dict):
        return None
    rule = boundary.get("segment_rule")
    if rule is None:
        return None
    if isinstance(rule, dict) and not rule:
        return None
    if isinstance(rule, (list, tuple)) and not rule:
        return None
    return rule


def organization_of(boundary):
    """边界条件里的**标准层主体节拍**（天/层）＋作用范围＋来源（user/model）。

    取不到（缺失 / 非正数 / 非数）→ ``(None, "标准层", None)``：组织层不生效。
    来源取自 `boundary_conditions._source.cadence_days`（提取层写的 user/model），
    只作**留痕**：模型按常见做法补的节拍照样生效，但必须在产物里标出来
    （`_organization.cadence_source`），不许让用户以为那是他自己写的。
    """
    bc = boundary if isinstance(boundary, dict) else {}
    cad = _pos(bc.get("cadence_days"))
    scope = bc.get("cadence_scope")
    scope = str(scope).strip() if isinstance(scope, str) and scope.strip() else "标准层"
    src = None
    sources = bc.get("_source")
    if isinstance(sources, dict):
        raw = sources.get("cadence_days")
        if raw:
            src = str(raw)
    return (float(cad) if cad else None), scope, src


def person_days_of(item):
    """该工序**自己工种**的工日 = 定额分母单位的工程量 ÷ 每人每天产量。

    ⚠️ 只算人工口径：**机械配员（司机 / 信号工 / 泵工 / 操作工 / 辅助）与机械台日
    不进这里**（它们随台数走，见机械分支）。把机组配员折成工日会把主体钢筋的
    306 工日虚增到 408，工期跟着虚长。

    与 `by_norm` 的口径同一个量（`quantity_in_norm_unit / productivity`），
    只是把"每人每天产量"换成"总工日"。
    """
    productivity = _pos(item.get("productivity"))
    if productivity is None:
        return None
    qty = quantity_in_norm_unit(item)
    if qty is None or qty <= 0:
        return None
    return float(qty) / float(productivity)


# ==================== MWI 表（新链路唯一的容量来源）====================

#: MWI 表读取缓存（进程级）。表 / 列缺失 → 空字典（调用方**报缺**，不写死型别映射）。
_MWI_CACHE = {}


def clear_mwi_cache():
    """清空 MWI 缓存（KB 变更后 / 测试用）。"""
    _MWI_CACHE.clear()


def _mwi_rows_by_name():
    """读 `Resource_Workface_Index` → `{资源名: MWIRow}`；缺表 / 缺列 → `{}`。

    ⚠️ 必须带 `resource_mobility` 列（方案 §3.2 / §4.2）。缺列时 SQL 直接报错，
    `kb._query_all` 返回空 → 本函数返回 `{}`，**绝不**在这里写"资源名→型别"的
    映射表（那正是 D5 禁止的写死常量）。缺数据由调用方如实报缺。
    """
    if "rows" in _MWI_CACHE:
        return _MWI_CACHE["rows"]
    cols = ("resource_name", "resource_kind", "mwi", "mwi_unit",
            "resource_mobility", "capacity_mode", "notes")
    seed = _MWI_CACHE.get("raw")
    if seed is not None:
        # 测试注入口径：调用方直接给行字典（不读库），走下面同一条清洗/校验路径。
        rows = [dict(r) for r in seed]
    else:
        raw = kb._query_all("SELECT %s FROM %s"
                            % (", ".join(cols), org_defaults.MWI_TABLE))
        rows = [dict(zip(cols, r)) for r in (raw or [])]
    # ⚠️ `segment_capacity.build_mwi_index` 把缺失/空串的 `resource_mobility` 落到默认值
    # `fixed`（那是它的 dataclass 缺省）。**容量型别是三选一的判据，不许默认** ——
    # 所以这里先把型别非法的行**剔出去**（`_skipped` 留痕），让调用方如实报缺。
    good, skipped = [], []
    for row in rows:
        mob = str(row.get("resource_mobility") or "").strip().lower()
        if mob in segment_capacity.MOBILITY_VALUES:
            row["resource_mobility"] = mob
            good.append(row)
        else:
            skipped.append(str(row.get("resource_name") or "?"))
    try:
        index = segment_capacity.build_mwi_index(good)
    except Exception:
        index = {}
        skipped.extend(str(r.get("resource_name") or "?") for r in good)
    _MWI_CACHE["rows"] = index
    _MWI_CACHE["skipped"] = skipped
    return index


def _mwi_row_of(resource_name):
    """取该资源在 MWI 表里的行（原名 → 工种别名归一兜底）；取不到 → `None`。"""
    index = _mwi_rows_by_name()
    if not index:
        return None
    for key in (str(resource_name or ""), _normalize_trade(resource_name),
                segment_capacity.normalize_trade(resource_name)):
        if key and key in index:
            return index[key]
    return None


def user_cap_for_task(primary, limits):
    """该任务工种的**用户同类限额**（C9：只有用户明确给出的才生效）。

    合成三处用户口径，取**最小**（同类取最小）：
      · `limits["by_trade"][工种]` —— 用户申报的分工种限额；
      · `limits["labor_total"]`   —— 用户申报的全项目同时在岗上限（本工序班组也在其中）；
      · `limits["crew_design"][工种]` —— 用户直接指定的班组（`params`/`boundary` 的
        `crew_design`，已在 `user_declared_crews()` 里筛过来源）。
    都没有 → `None` = **不限**（绝不用 KB `crew_max` 或 96 人预算去封顶）。
    """
    if not isinstance(limits, dict):
        return None
    # 域 7.9：**机组配员角色**（司机 / 信号工…）**跟台数走、不设限额**。
    # 判据 = 场地级机械常量每台 `crew_per_unit` 的**键集**（`_site_machine_registry`），
    # **不写死角色名**。"人不够"的唯一物理办法是**少上机器**（见 `_plan_task` 机械分支
    # 的 `_max_by_crew`：总人工限额把台数压小），而不是给司机单独发限额。
    if str(primary or "").strip() in _site_machine_registry(limits=limits)[1]:
        return None
    cands = []
    trade_limit, _key = _match_limit(primary, limits.get("by_trade") or {})
    if trade_limit:
        cands.append(int(trade_limit))
    total = _pos_int(limits.get("labor_total"), 0)
    if total:
        cands.append(int(total))
    design = limits.get("crew_design")
    if isinstance(design, dict):
        d = _pos_int(design.get(primary), 0)
        if d:
            cands.append(int(d))
    return min(cands) if cands else None


def user_declared_crews(params, boundary):
    """用户**明确指定**的各工种班组人数（`crew_design`）；模型补的一律丢弃（C9）。

    来源：`extracted_params.crew_design` / `boundary_conditions.crew_design`。
    `boundary_conditions._source["crew_design"] == "model"` → 不采纳（留痕在调用方）。
    """
    out = {}
    for src in (params, boundary):
        if not isinstance(src, dict):
            continue
        raw = src.get("crew_design")
        if not isinstance(raw, dict):
            continue
        if src is boundary and _boundary_key_source(boundary, "crew_design") == "model":
            continue
        for k, v in raw.items():
            n = _pos_int(v, 0)
            if k and n:
                out[_normalize_trade(str(k).strip())] = int(n)
    return out



def plan_organization(item, primary, limits, honor_user_limits, cadence_days=None,
                      cadence_scope=None, face_area=None, cadence_source=None,
                      user_rule=None, machine=False, *, daily_share=None,
                      area_params=None):
    """施工组织层主入口（**新链路唯一入口**）：返回 `_organization` 字典。

    【0】层面积 →【1】分段（**用户显式规则优先**，否则 MSSA=500）
    →【2】段容量 = ceil(段面积 ÷ MWI)
    →【3】需求量（人工=本工种工日 / 机械=总台班）
    →【4】有效容量 = min(汇总容量, 用户同类限额)
    →【5】工期 = `duration_days(需求量, 有效容量)` →【6】投入资源 = 有效容量

    **取不到任何一环 → 返回 None**（调用方必须**如实报缺**，不许编人数）：
      · 需求量算不出（无定额锚定 / 工程量 <= 0）；
      · 层面积取不到（切不出施工段）；
      · MWI 表里没有该资源 / `resource_mobility` 缺列 → 无从判断 fixed/mobile/site；
      · `machine=True` 且该资源的 `capacity_mode != "area"`（裁定 C：position /
        auxiliary / transport 共 37 行没有 MWI 口径 → **不发明**，保留机台容量路径）。

    `machine=True`（裁定 C，2026-09-21）：`capacity_mode == "area"` 的机械行
    （履带式单斗液压挖掘机 / 推土机 / 抓铲挖掘机 / 拖曳铲运机 / 轮胎式装载机 /
    钢轮压路机 / 电动夯实机 / 混凝土输送泵车）**与人工同一套公式**，需求量为总台班。

    节拍（`cadence_days`）**只写进产物作对比展示**，不参与任何计算（裁定 8 / C11）。
    原先"只有取到节拍才生效、取不到就走旧口径"的分岔**已删除**（C8 第 4/6 项）。
    """
    row = _mwi_row_of(primary)
    if row is None:
        return None
    if org_plan.mobility_of(row) is None:
        # `resource_mobility` 是 C4「三种型别分开算」的判据，缺了就**不猜**（报缺）。
        return None
    if machine:
        if str(getattr(row, "capacity_mode", "") or "").strip().lower() != "area":
            # 裁定 C：非 area 型机械（position/auxiliary/transport，mwi 为 NULL）
            return None
        pd = machine_total_shifts(item)
        cap = _user_limit_of(primary, limits, honor_user_limits)
    else:
        pd = person_days_of(item)
        cap = user_cap_for_task(primary, limits) if honor_user_limits else None
    if pd is None or pd <= 0:
        return None
    # ---- 域 7.11：**不展开的活动按施工面积开段**（段数 = 1，不需要楼层范围）----
    # 判据（父代理冻结）：`is_l5_expandable == 0`（第一）+ 树内叶子无 `segment_id`（第二）。
    # 取不到 `is_l5_expandable`（KB 没给）→ **不判"不展开"**，沿用既有 `build_segment_table`
    # （于是 `segment_areas` / `segment_ids` 与改造前逐字段相同）。
    # 用户显式分段规则（裁定 11「用户规则优先于一切」）在 `segment_plan` 那条路上消费，
    # 只有它不在时才按 7.11 判据走（见 `_segment_table_for_item`）。
    table = _segment_table_for_item(item, row, face_area, user_rule, area_params, primary)
    if not table["ok"]:
        return None

    org = org_plan.plan_capacity_chain(
        pd, table["segment_areas"], table["segment_ids"], row,
        user_cap=cap,
        user_cap_source=("用户申报同类限额" if cap is not None else ""),
        aliases=_TRADE_ALIAS, resource_name=primary,
        cadence_days=cadence_days, cadence_scope=cadence_scope,
        cadence_source=cadence_source,
        # 域 7.1 / 7.2：逐日份额取小**只在这里发生一次**（`effective_capacity_daily`）
        daily_share=daily_share)
    org["capacity_source"] = "mwi"
    org["floor_area"] = table["floor_area"]
    org["segment_rule"] = table["rule"]
    org["segment_rule_note"] = table["note"]
    org["cadence_scope"] = cadence_scope or "标准层"
    org["cadence_source"] = cadence_source or "unknown"
    # 域 7.11：段数与面积口径的留痕（可溯源；旧键一个不动）
    org["segment_area_caliber"] = str(table.get("caliber") or "")
    if item.get("kb_activity_id"):
        org["activity_id"] = str(item["kb_activity_id"])
    return org


def _segment_table_for_item(item, mwi_row, face_area, user_rule, area_params, primary):
    """域 7.11：该活动的**施工段表** —— 不展开的走 `face_area_for_activity`（段数 = 1）。

    · 不展开（`is_l5_expandable == 0` ∧ 树内叶子无 `segment_id`）→ 按**施工面积**开段，
      面积口径的唯一真源是 `L4_Activity_Dictionary.measure_scope`（空值回退 Σ floor_areas
      并留痕；取不到面积 → 报缺）；
    · 其余（未知 / 可展开 / 叶子已分段 / **用户显式分段规则**）→ 沿用既有
      `org_plan.build_segment_table(face_area, user_rule)`（**逐字段不变**）。
    返回与 `build_segment_table` 同形的 `{ok, floor_area, segment_areas, segment_ids,
    rule, note}`（外加 `caliber`）；`ok=False` → 调用方报缺（不编面积、不编人数）。
    """
    expandable = item.get("is_l5_expandable")
    leaf_seg = str(item.get("segment_id") or "").strip()
    if expandable is None or leaf_seg or user_rule:
        return org_plan.build_segment_table(face_area, user_rule)
    params = dict(area_params or {})
    scope = str(item.get("measure_scope") or "")
    res = org_plan.face_area_for_activity(
        params, is_l5_expandable=expandable, leaf_segment_id=None,
        scope=(scope or None), mwi_row=mwi_row, floor_area=face_area,
        trade=primary, activity_id=str(item.get("kb_activity_id") or ""),
        activity_name=str(item.get("name") or ""))
    if not res.get("ok") or not res.get("segment_areas"):
        return {"ok": False, "floor_area": None, "segment_areas": [], "segment_ids": [],
                "rule": str(res.get("caliber") or ""), "note": str(res.get("basis") or ""),
                "caliber": str(res.get("caliber") or "")}
    return {"ok": True, "floor_area": res.get("face_area"),
            "segment_areas": [float(a) for a in res["segment_areas"]],
            "segment_ids": [str(s) for s in res["segment_ids"]],
            "rule": str(res.get("caliber") or "construction_area"),
            "note": str(res.get("basis") or res.get("caliber_note") or ""),
            "caliber": str(res.get("caliber") or "")}


def org_capped_records(tid, primary, org):
    """组织层留痕（契约：绝不静默）—— **为什么是 N 人 / N 台、为什么是 D 天**。

    新链路（C 组）只输出两条：
      · 主条：完整依据串 `org["basis"]`（逐段 `面积 ÷ MWI` → 汇总 → 工期）；
      · 用户限额生效时：**取小过程**（`min(汇总容量, 用户同类限额)`）。
    旧口径的「节拍组织 / 效率折减 / 组织缺口 / 措施项固定时长」留痕随 C8 一并删除。
    """
    recs = []
    eff = _pos_int(org.get("crew_total"), 0)
    if not eff:
        return recs
    unit = "台" if str(org.get("resource_kind") or "").lower() == "machine" else "人"
    basis = str(org.get("basis") or "按工作面容量（MWI 段容量）计")
    recs.append(_cap_record(tid, primary, int(eff), int(eff),
                            "工作面容量（MWI 段容量）", unit, basis))
    if org.get("user_cap") is not None:
        rollup = _pos_int(org.get("capacity_rollup"), 0) or int(eff)
        recs.append(_cap_record(
            tid, primary, int(rollup), int(eff), "用户同类限额", unit,
            "有效容量 = min(汇总容量 %d %s, 用户同类限额 %d %s) = %d %s（取小）"
            % (int(rollup), unit, int(org["user_cap"]), unit, int(eff), unit)))
    return recs


def org_gap_record(item, plan):
    """组织缺口报告的一条（契约 §3；`plan_assembler` 会搬进 `meta.organization_gaps`）。"""
    org = plan.get("organization") or {}
    return {
        "task_id": item.get("task_id"),
        "task_name": item.get("name") or item.get("task_id"),
        "trade": plan.get("resource_id"),
        "person_days": _r2(org.get("person_days")),
        "cadence_days": org.get("cadence_days"),
        "n_needed": int(org.get("n_needed") or 1),
        "n_max": int(org.get("n_max") or 1),
        "c_max": org.get("c_max"),
        "t_min_days": org.get("t_min_days"),
        "levers": list(org.get("levers") or []),
        # 诊断（只增不改）：工期、当量人数、缺省参数说明
        "duration_days": int(org.get("duration_days") or 1),
        "effective_crew_total": org.get("effective_crew_total"),
        "assumption": org.get("assumption") or "",
    }


def merge_org_gaps(*gap_lists):
    """两版的缺口合并：同一条任务取"更严重"的那条（n_needed 更大）。"""
    merged = {}
    for gaps in gap_lists:
        for gap in (gaps or []):
            tid = gap.get("task_id")
            old = merged.get(tid)
            if old is None or int(gap.get("n_needed") or 0) > int(old.get("n_needed") or 0):
                merged[tid] = gap
    return [merged[tid] for tid in sorted(merged, key=lambda x: str(x))]


def _plan_task(item, limits, honor_user_limits, crew_plan=None,
               reuse_declared_crews=False, cadence_days=None, cadence_scope=None,
               face_area=None, cadence_source=None, user_rule=None, *,
               daily_share=None, site_const=None, area_params=None):
    """算一条任务在本版上限下的班组、工期、资源用量（**唯一实现，两版共用**）。

    返回 {"duration", "resources", "resource_id", "resource_kind", "crew", "capped",
          "organization", "capacity_source", "capacity_basis"}。
    两版的唯一差异：honor_user_limits=False 时上限只取工作面容量；
    True 时取 min(工作面容量, 用户限额)。

    域 7.1 / 7.2 **新增 keyword-only 形参** `daily_share=None`（**键只增不改**）：
    该任务**窗口内逐日份额的最小值**（裁决 #7）。`None` = 没有份额 → 逐字段退回旧行为
    （`effective_capacity_daily(N, None) == N`）。**不在这里做取小** —— 取小只在
    `org_plan.plan_capacity_chain` 里发生一次（域 7.1 的唯一实现）。

    人数/台数**唯一来源** = 工作面容量（段面积 ÷ MWI，见 `plan_organization`）；
    取不到时按 `capacity_source` 如实分流：`"mwi"` / `"reported_missing"`
    （域 1.6 收敛为两态，裁定 B，绝不静默）。
    """
    tid = item["task_id"]
    capped = []
    resources = {}
    primary = None
    kind = "labor"
    crew = {}
    cap_src = "reported_missing"
    cap_basis = ""
    organization = None          # 施工组织层结果（无节拍 → None，绝不写进产物）
    duration = int(max(1, item["own_duration"]))
    crew_plan = crew_plan or {}

    if not (item["usable"] and item["quantity"] > 0):
        # 无可用定额锚定：沿用叶子自身工期，班组未知（warning 已在台账阶段记过）。
        #
        # ⚠️ 第 38 轮试过在这里补一个"按工作面容量给班组"的兜底，**已撤回**：
        # 契约 §5-WS4 ④ 明确"verdict == unusable 的定额直接降级、不进工日需求"，
        # 补上去会让 23 个既有用例（工作面容量封顶、总人工限额、覆盖率分母、
        # 无定额即无工日需求…）全部失效。副作用是：当**所有** L4 都还没人工审定
        # 时，人力曲线会是 0 人、总工期退化成纯依赖链长度（实测 675 天）——
        # 这个数字**不能当方案用**，只是"没有定额、也没有班组"的退化解。
        # 正确做法是把默认定额行审出来（见 tools/approve_norm_default.py），
        # 而不是在排程器里给未审定数据补一个假班组。
        # 第 7 批（2026-09-21）：**无可用定额锚定也必须写出容量来源**。
        # 裁定 B 要求"容量来源逐行可追溯、两态之一（mwi / reported_missing），绝不静默"，
        # 而这条早退路径漏了这两个键 ⇒ 实测 127 行里 **20 行**在交付侧读不到
        # `capacity_source`（正是那 9 条工作包级占位叶子 + 11 条 ALC），
        # `delivery.capacity_caliber_model()` 因此既不算 mwi 也不算 missing，
        # 那段"工期不随工程量变化"的提示里**永远缺这 20 条**。
        # 语义有据：下面 :3904 的 `reported_missing` 文案自己就写着缺容量的成因包括
        # "…/ **无定额锚定** / 用户也没给限额" —— 所以这里写 `reported_missing` 是
        # 照设计口径落键，不是新造第三态。
        return {"duration": duration, "resources": resources, "resource_id": None,
                "resource_kind": kind, "crew": crew, "capped": capped,
                "capacity_source": "reported_missing",
                "capacity_basis": (
                    "本任务没有可用的定额锚定（无 KB 定额 / 定额单位不可用），"
                    "未进入容量计算：工期沿用叶子原值 %d 天，**不随工程量变化**。"
                    "改工程量不会让这条任务变长或变短。" % int(duration))}

    if item["mode"] == "machine" and item["norm_value"] is not None:
        # ---------- 机械主导：总台班固定，台数决定工期 ----------
        # 台数上限优先级（契约 §5-WS4 ②③ + 上级口径 ③）：
        #   ① 用户设备清单（meta.boundary_conditions.equipment：{name, quantity}）
        #   ② v2 标定 machine_max（随工程量算出来的工作面容量）
        #   ③ 旧键 max_machine
        #   ④ 都没有 → **1 台 + warning**（不许用 DEFAULT_CEILING 让台数无限）
        # 绝对不许"按目标工期反推台数"。
        kind = "machine"
        primary = item["machine_name"]
        total_shifts = machine_total_shifts(item)
        target = max(1, int(item["target_days"]))
        # ---- 裁定 C（2026-09-21）：area 型机械也走 MWI 新链路 ----
        # `capacity_mode == "area"` 的机械行与人工**同一套公式**（fixed 逐段相加 /
        # mobile 汇总一次 / site 独立）。剩下 37 行非 area 型（position 21 /
        # auxiliary 11 / transport 5，mwi 为 NULL）**不发明口径** → 如实报缺
        # （`capacity_source="reported_missing"`，域 1.6 收敛为两态）。
        _morg = plan_organization(item, primary, limits, honor_user_limits,
                                  face_area=face_area, user_rule=user_rule,
                                  machine=True, daily_share=daily_share,
                                  area_params=area_params)
        if _morg is not None:
            machines = int(max(1, _morg["crew_total"]))
            for _mrec in org_capped_records(tid, primary, _morg):
                capped.append(_mrec)
            organization = _morg
            cap_src = str(_morg.get("capacity_source") or "mwi")
            cap_basis = str(_morg.get("basis") or "")
        else:
            # 工作面容量口径的台数（不是"凑目标工期"算出来的）
            want = int(max(1, item["cap_machine"] or DEFAULT_MACHINE_FALLBACK))
            user_m = _user_limit_of(primary, limits, honor_user_limits)
            ceiling = want if user_m is None else int(max(1, min(want, user_m)))
            machines = int(max(1, min(want, ceiling)))
            _mrow = _mwi_row_of(primary)
            cap_src = "reported_missing"
            cap_basis = ("该资源 MWI 口径未定义（capacity_mode=%s），"
                         "无工作面容量规则可回退（域 1.6 已删 Workface_Capacity_Rule），"
                         "如实报缺。"
                         % (str(getattr(_mrow, "capacity_mode", "") or "unknown")
                            if _mrow is not None else "MWI表无该资源"))
        # 封顶一律留痕（契约：绝不静默）。用户限额与"缺容量数据"各记一条。
        # ⚠️ `_morg`（MWI 新链路）已记过依据，这里只在兜底路径记。裁定 C。
        if _morg is not None:
            pass
        elif user_m is not None and user_m < want:
            capped.append(_cap_record(tid, primary, want, machines, "用户资源限额", "台",
                                      "台数由 %d 台压到 %d 台" % (want, machines)))
        elif item["cap_machine"] is None:
            capped.append(_cap_record(
                tid, primary, machines, machines, "缺工作面容量数据", "台",
                "KB 无该活动的机械工作面容量标定行、你也没给设备清单，"
                "按 %d 台（保守默认）计；若现场实际可上更多台，请在边界条件里给出设备数量"
                % DEFAULT_MACHINE_FALLBACK))
        else:
            capped.append(_cap_record(
                tid, primary, machines, machines, "工作面容量", "台",
                "台数按工作面容量定为 %d 台（%s）；台数翻倍工期减半，"
                "若现场可上更多台请给出设备数量"
                % (machines, item.get("unit_basis") or "每施工段")))
        # ---- 总人工上限（labor.peak_total）：机组配员也算"全项目同时在岗"人数 ----
        # 口径必须与 `_total_labor_of` / `daily_curves` / 交付物一致：司机、信号工是**人**。
        # 机组按台数配备，所以"人不够"的唯一物理办法是**少上机器**（总台班守恒 →
        # 工期按同一比例延长），绝不静默超限（与人工分支的"缩编"同一办法）。
        _per_machine = machine_crew_of(item.get("_leaf") or {}, primary)
        _crew_per_machine = sum(_pos_int(c, 0) for c in _per_machine.values())
        _total_limit = limits.get("labor_total") if honor_user_limits else None
        if _total_limit and _crew_per_machine > 0:
            _max_by_crew = int(max(1, float(_total_limit) // _crew_per_machine))
            if machines > _max_by_crew:
                _old_machines = machines
                _old_duration = (int(max(1, math.ceil(total_shifts / machines)))
                                 if total_shifts > 0 else target)
                machines = _max_by_crew
                capped.append(_cap_record(
                    tid, primary, _old_machines, machines, "总人工限额", "台",
                    "总人工限额（%d 人）：机组配员 %d 人/台，最多同时上 %d 台（原 %d 台）；"
                    "工期按同一比例延长（%d→%d 天），总台班不变"
                    % (int(_total_limit), _crew_per_machine, machines, _old_machines,
                       _old_duration,
                       int(max(1, math.ceil(total_shifts / machines)))
                       if total_shifts > 0 else target)))
        # ══════════════════════════════════════════════════════════════════════════
        # 第 44 轮补（用户 2026-09-21 实测）：**无容量依据时，机械定额反算工期不许膨胀**
        # ---------------------------------------------------------------------------
        # 反例（实测）：`9.2.5 管沟回填夯实` 工程量 2900 m³、台班定额 5.53 台班/100m³
        # → 总台班 160.37；该资源的 MWI 口径未定义（`capacity_mode=area`，域 1.6 已删
        # `Workface_Capacity_Rule`），台数落进"④ 都没有 → 1 台"的兜底 → 工期 161 天。
        # 而本任务的**既有排期是 12 天**（WBS provenance 原文：「工期来自 WBS 既有排期；
        # 本次只锚定定额，不重排工期」）—— 一条 12 天的工序被定额反算成 161 天。
        #
        # 判据**只认"有没有容量依据"**，不认任务名：
        #   · `_morg is not None`（MWI 链路有成）→ 台数有据，**本规则不介入**；
        #   · 用户给了设备清单（`user_m`）/ v2 标定 `cap_machine` → 同上，不介入；
        #   · 三者**全无** → 台数本来就没有任何依据（原来固定取 1 台），此时改按
        #     **本任务自己的既有排期**反推台数：`台数 = ⌈总台班 ÷ 叶子工期⌉`
        #     （总台班守恒，不编造定额、不改工程量），工期落回叶子排期。
        #     ⚠️ 这不是给上面「绝对不许按目标工期反推台数」开通用口子：那条禁令管的是
        #     **有容量依据时的通用路径**；这里是**无依据的兜底路径**，且锚点不是
        #     "用户目标工期"，而是**本任务自己的既有排期**（与人工侧"无定额即沿用
        #     叶子原值"同一口径）。
        # 只**抬高**台数、绝不压低（`need > machines` 才动），也不会缩短本就短于叶子
        # 工期的任务；每次调整都写进 `capped` 留痕，绝不静默。
        # ══════════════════════════════════════════════════════════════════════════
        if (_morg is None and user_m is None and item.get("cap_machine") is None
                and total_shifts > 0):
            _own = int(max(1, item["own_duration"]))
            _derived = int(max(1, math.ceil(total_shifts / machines)))
            if _derived > _own:
                _need = int(max(1, math.ceil(total_shifts / _own)))
                if _need > machines:
                    capped.append(_cap_record(
                        tid, primary, machines, _need, "无容量依据按叶子排期定台数", "台",
                        "该活动既无 MWI 工作面容量、也无设备清单，台数原本按 1 台保守计；"
                        "按台班定额反算得 %d 天，而叶子既有排期是 %d 天 → 按"
                        "「总台班 ÷ 叶子工期」反推台数为 %d 台，"
                        "工期 = ⌈%.2f ÷ %d⌉ = %d 天"
                        "（总台班 %.2f 不变；若现场上不了这么多台，请在边界条件里给出"
                        "设备数量，或在本任务写明天数）"
                        % (_derived, _own, _need, total_shifts, _need,
                           int(max(1, math.ceil(total_shifts / _need))), total_shifts)))
                    machines = _need
        # C10 唯一公式：工期 = ceil(需求量 ÷ 有效容量) —— 机械侧"需求量" = 总台班、
        # "有效容量" = 台数，走**同一个** `duration_days` 入口（不再各写一份 ceil 除法）。
        duration = int(max(1, segment_capacity.duration_days(total_shifts, machines) or 1)) \
            if total_shifts > 0 else target
        if _morg is not None:
            # 总人工限额可能把台数压小 → 把 `_organization` 的容量/工期同步成**实际生效值**，
            # 否则产物里会出现"组织层说 5 台、排程行用 2 台"的自相矛盾。
            if machines != int(_morg.get("crew_total") or 0):
                _morg["capacity_effective"] = machines
                _morg["crew_total"] = machines
                _morg["effective_crew_total"] = machines
                _morg["duration_days"] = duration
                _morg["basis"] = ("%s；总人工限额（%d 人，机组配员 %d 人/台）"
                                  "把台数压到 %d 台 → 工期 ceil(总台班 ÷ %d 台) = %d 天"
                                  % (_morg.get("basis") or "", int(_total_limit or 0),
                                     _crew_per_machine, machines, machines, duration))
                cap_basis = str(_morg["basis"])
        resources[primary] = float(machines)
        # 机械配员（司机 / 信号工…）随台数走；模板没带配员就只记主控机械。
        # `crew` 与 `resources` 必须**同一个量**（当天在场的总人数）：原先 `crew` 留的是
        # **每台**配员（1 台机 1 个司机 → {'司机': 1}），而 `resources` 是总人数
        # （5 台机 → 司机 5 人）。两处不一致时，逐日曲线的"总人工"（按 resources 算）
        # 与排程行的班组（按 crew 算）永远对不上——实测 six_leaf_wbs 的塔吊 5 台：
        # resources 司机5+信号工5，row.crew 却是 司机1+信号工1。
        crew = {}
        for role, cnt in sorted(_per_machine.items()):
            n = _pos_int(cnt, 0)
            if n > 0:
                resources[role] = float(machines * n)
                crew[role] = float(machines * n)
        # ---- 机械台班的"人手下限"（第 37 轮，实测踩到的坑）----
        # 只对**机上一台设备 + 一个机组全程盯守**的机种生效（桩机 / 吊装 / 钻机）：
        # 这类任务的台班定额只数了机械占用时间，人工配合量级完全不同 ——
        # 实测 2.1.1 预应力管桩：台班口径 120 ÷ 100 × 0.49 = 0.59 台班 → 2 台机 = 1 天，
        # 而 120 根桩的人日需求 ÷ 工作面 8 人 = 8 天，1 天物理上做不到（要 8 分钟一根）。
        # 土方 / 夯实 / 泵送这类**人只是配合**的机种（挖掘机、自卸汽车、夯实机、泵车、
        # 塔吊）不加：实测 3.2.3 土方外运的"人工定额"是 0.714 m³/工日（人挖人运口径），
        # 拿它卡机械外运会把 2 天算成 398 天。加下限**只加长不缩短**。
        labor_days = None
        if _is_crew_bound_machine(primary):
            labor_days = _machine_labor_days(item, machines)
        if labor_days and labor_days > duration:
            capped.append(_cap_record(
                tid, primary, duration, labor_days, "人工配合下限", "天",
                "台班口径 %d 天短于人工配合口径 %d 天（人日需求 ÷ 工作面 %s 人），"
                "工期取人工口径" % (duration, labor_days, item.get("labor_name") or "普工")))
            duration = int(labor_days)
    else:
        # ---------- 人工主导：每人每天产量 P → 人数 → 工期 ----------
        kind = "labor"
        primary = item["labor_name"]
        productivity = item["productivity"]
        target = max(1, int(item["target_days"]))
        # 人数来源（契约 §5-WS4 ①：**删掉"按目标工期反推班组"**）：
        #   ① 用户明确指定的班组（`crew_plan`，来自人工预算分摊 / 用户指定）
        #      与上游写明的投入人工（crew_bind 的 crew，重排时它是权威）
        #      —— 这是"用户限额"之外**唯一**允许覆盖工作面容量的来源；
        #   ② 都没有 → **工作面容量顶满**：
        #      班组 := cap = clamp(crew_base + crew_step_n × ⌊(段量 − q_ref)/crew_step_q⌋,
        #                          crew_min, crew_max)。
        # ⚠️ 节拍配置的设计班组（`_crew_design`）**不再**决定人数：它来自旧产能表
        # 口径，实测把铝模顶满的 13 人压成 10 人（见 test_workface_v2）。它仍然参与
        # `resolve_design_crews` 的人力预算，但不进这条"多少天干完"的链路。
        # 产能永远是定额（productivity_value，缺则 1/norm_value）；节拍产能表不参与
        # "排多少天"，目标工期也不再能反推出人数。
        # ---- 施工组织层（2026-09-21 C 组）：**新链路唯一入口** ----
        # 段容量 = ceil(段面积 ÷ MWI) → 有效容量 = min(汇总容量, 用户同类限额)
        # → 工期 = ceil(需求量 ÷ 有效容量)（C10 唯一公式）。
        # 原先"取到节拍才生效 / 取不到走四支人数来源（crew_plan / crew_design /
        # crew_declared / cap_labor）"的分岔**已整条删除**（C8 第 5/6 项）。
        _org = plan_organization(item, primary, limits, honor_user_limits,
                                 cadence_days=cadence_days,
                                 cadence_scope=cadence_scope, face_area=face_area,
                                 cadence_source=cadence_source,
                                 user_rule=user_rule, daily_share=daily_share,
                                 area_params=area_params)
        if _org is not None:
            # 新链路：投入资源 = 有效容量（人数），工期 = 唯一公式的产物
            people = int(max(1, _org["crew_total"]))
            duration = int(max(1, _org["duration_days"]))
            for _orec in org_capped_records(tid, primary, _org):
                capped.append(_orec)
            organization = _org
            cap_src = str(_org.get("capacity_source") or "mwi")
            cap_basis = str(_org.get("basis") or "")
        else:
            # ---- 报缺 + **不猜人数** ----
            # 缺层面积 / MWI 表里没有该资源 / `resource_mobility` 缺列 → 退回**既有的
            # 工作面容量** `cap_labor`（每施工段人数，仍属工作面容量口径），工期照样走
            # 唯一公式 `duration_days(需求量, 有效容量)`。绝不按 AI 估算补班组。
            # 只有连工作面容量都没有时，才允许**用户明确给出的同类限额**单独当容量
            # （C9：用户给的就是资源条件）；两者都没有 → 如实报缺，不编人数。
            _pd = _pos(person_days_of(item), 0.0) or 0.0
            _fb_raw = _pos_int(item.get("cap_labor"), 0)      # KB 工作面容量（裸公式）
            _ucap = user_cap_for_task(primary, limits) if honor_user_limits else None
            if _fb_raw and _ucap:
                _fb = min(int(_fb_raw), int(_ucap))
            else:
                _fb = int(_fb_raw or _ucap or 0)
            if not _fb:
                # 连工作面容量都没有：如实报缺，不写人数、不用叶子工期凑数
                _miss = ("本任务没有可用的工作面容量（缺层面积 / MWI 表缺该资源 / "
                         "resource_mobility 缺列 / 无定额锚定 / 用户也没给限额），"
                         "**不按 AI 估算补班组**；资源与班组留空，工期沿用叶子原值 %d 天。"
                         "本次未取到工作面容量，工期沿用原值，**未参与容量计算**。"
                         "⚠️ **本次工程量变化未反映到工期（缺容量数据）** —— "
                         "改工程量不会让这条任务的工期变长/变短，"
                         "请补 MWI 行、层面积或用户同类限额后再看工期。"
                         % int(max(1, item["own_duration"])))
                capped.append(_cap_record(
                    tid, primary, 1, 1, "缺工作面容量数据", "人", _miss))
                return {"duration": int(max(1, item["own_duration"])), "resources": {},
                        "resource_id": None, "resource_kind": kind, "crew": {},
                        "capped": capped, "organization": None,
                        "capacity_source": "reported_missing",
                        "capacity_basis": _miss}
            people = int(max(1, _fb))
            duration = int(max(1, segment_capacity.duration_days(_pd, people) or 1))
            cap_src = "reported_missing"
            _capped_by_user = bool(_fb_raw and _ucap and int(_ucap) < int(_fb_raw))
            cap_basis = ("本次未取到 MWI 段容量（缺层面积 / MWI 表缺该资源 / "
                         "resource_mobility 缺列）且无工作面容量规则可回退"
                         "（域 1.6 已删 Workface_Capacity_Rule），如实报缺；"
                         "工期 = ceil(需求量 %.2f ÷ %d 人) = %d 天（仍走唯一公式）"
                         % (_pd, int(people), int(duration)))
            capped.append(_cap_record(
                tid, primary, int(_fb_raw or _fb), people,
                ("用户同类限额（工作面容量兜底）" if _capped_by_user
                 else "工作面容量兜底（非 MWI）" if _fb_raw
                 else "用户同类限额（无 MWI 容量）"),
                "人", cap_basis))
        # 投入资源 = 有效容量（新链路）或兜底工作面容量 —— 两条路都必须写进资源与班组
        resources[primary] = float(people)
        crew = {primary: float(people)}

    # 冻结（第 36 轮，修订路径）：这条任务没有被本次修订波及 → 工期保持存档原值。
    # 班组/资源照上面的结果留着（它们要进资源曲线与 assigned_resources），只把工期
    # 钉回去。不能反过来"先算再比"：存档里的 norm_binding.crew 由 ResourceNode 回填，
    # 与排程器当时用的班组不是同一个量，重排结果本来就可能与存档不同。
    if item.get("frozen"):
        duration = int(max(1, item["own_duration"]))

    resources = dict((k, float(v)) for k, v in resources.items() if v)
    return {"duration": duration, "resources": resources, "resource_id": primary,
            "resource_kind": kind, "crew": crew, "capped": capped,
            "organization": organization,
            "capacity_source": cap_src, "capacity_basis": cap_basis}


def _ceiling_for(resource_name, item, limits, honor_user_limits):
    """本版该资源的计数上限 + 中文原因（工作面容量 / 用户资源限额）。"""
    workface = workface_cap_of(item, resource_name)
    if not honor_user_limits:
        return workface, "工作面容量"
    user_limit, user_key = _match_limit(resource_name, limits.get("equipment") or {})
    if user_limit is None:
        user_limit, user_key = _match_limit(resource_name, limits.get("by_trade") or {})
    if user_limit is not None and user_limit < workface:
        return user_limit, "用户资源限额（%s）" % (user_key or resource_name)
    return workface, "工作面容量"


def peaks_from_curves(curves):
    """逐日曲线 → 峰值（人工 / 机械各自的最大值）。"""
    return {
        "labor": max([c["labor"] for c in curves] or [0]),
        "equipment": max([c["equipment"] for c in curves] or [0]),
        "trades": _peak_map(curves, "trades"),
        "items": _peak_map(curves, "items"),
    }


def _peak_map(curves, field):
    out = {}
    for c in curves:
        for name, val in (c.get(field) or {}).items():
            out[name] = max(out.get(name, 0), val)
    return out


def _over_limit_records(curves, peaks, limits, *, site_const=None):
    """resource_ok 版里"已达/超过用户上限"的资源（理论上不该超，超了就是真问题）。

    域 7.8（**结构性排除**）：**超限清单永不含场地级机械**（塔吊 / 施工电梯）——
    它们是项目级常量、默认够用，用户申报的台数已经由常量采用（`site_machine_const`），
    再报一条"已达上限"纯属噪声。判据来自 `_site_machine_registry` 的**键集**
    （`site_machine_const.machines` / 登记表），**不写死资源名**。
    """
    records = []
    _site_machines, _site_roles = _site_machine_registry(site_const=site_const, limits=limits)
    for trade, peak in sorted(peaks["trades"].items()):
        if trade in _site_roles:
            # 域 7.9：机组配员（司机 / 信号工…）**跟台数走、不设限额** ——
            # 给用户申报的限额报"已达上限"是噪声；真正该报的是"台数被限额压小"
            # （`_plan_task` 的 `_max_by_crew`，已进 `capped`）。
            continue
        limit, key = _match_limit(trade, limits.get("by_trade") or {})
        if limit is None:
            continue
        if peak >= limit - 1e-9:
            records.append({
                "resource": trade,
                "limit": limit,
                "peak": _r2(peak),
                "note": "已达上限（%s 限额 %d 人）" % (key or trade, limit),
            })
    for name, peak in sorted(peaks["items"].items()):
        if name in _site_machines:
            continue                      # 域 7.8：场地级常量不进超限清单
        limit, key = _match_limit(name, limits.get("equipment") or {})
        if limit is None:
            continue
        if peak >= limit - 1e-9:
            records.append({
                "resource": name,
                "limit": limit,
                "peak": _r2(peak),
                "note": "已达上限（%s 限额 %d 台）" % (key or name, limit),
            })
    total_limit = limits.get("labor_total")
    # 与分资源同口径：`>=` 表示"已达上限"（顶满即报，属提示而非违规）。
    if total_limit is not None and peaks["labor"] >= total_limit - 1e-9:
        records.append({
            "resource": "总人工",
            "limit": int(total_limit),
            "peak": _r2(peaks["labor"]),
            "note": "已达上限（总人工限额 %d 人）" % int(total_limit),
        })
    return records


def _dedup_capped(records):
    """封顶记录去重（同一任务同一资源只留一条），并固定排序。"""
    seen = set()
    out = []
    for rec in records:
        key = (rec.get("task_id"), rec.get("resource"), rec.get("want"), rec.get("got"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return sorted(out, key=lambda r: (str(r.get("task_id")), str(r.get("resource"))))


# ==================== 节点 ====================
class SchedulerNode(BaseNode):
    name = "scheduler"
    title = "排程与两版工期"

    def run(self, ctx):
        ctx = ctx or {}
        wbs = ctx.get("wbs") or {}
        dependencies = ctx.get("dependencies")
        boundary = ctx.get("boundary_conditions") or {}
        params = ctx.get("extracted_params") or {}
        cpm_result = ctx.get("cpm_result")

        self.emit("node_progress", {"node": self.name, "progress": 15,
                                    "message": "读取 WBS、工序先后与资源上限"})
        # 施工组织层：该层**可施工面积**（单栋标准层面积，㎡）只用来推"同时能开几个
        # 作业面"；取不到就 None（面积口径不参与，不许猜）。延迟 import 避免与
        # `resource`/`beat_configs` 形成循环导入。
        face_area = None
        try:
            from .beat_configs import standard_floor_area
            face_area = standard_floor_area(params)
        except Exception:
            face_area = None
        try:
            out = compute_schedules(wbs, dependencies, boundary, params, cpm_result,
                                    face_area=face_area)
        except Exception as exc:                       # 任何异常都降级，绝不让流水线崩
            warnings = ["排程节点异常，已降级为空排程（不影响后续节点的数据校验）：%s" % exc]
            out = _empty_result(warnings, boundary, params)
            out["schedule_warnings"] = warnings

        versions = out.get("schedule_versions") or {}
        theory = versions.get("theory_min") or {}
        ok = versions.get("resource_ok") or {}
        compare = versions.get("compare") or {}
        self.emit("node_progress", {"node": self.name, "progress": 70,
                                    "message": "两版工期已算出，正在整理逐日人力曲线"})

        delta = compare.get("delta_days")
        delta = 0 if delta is None else delta
        if compare.get("target_verdict"):
            verdict = "；目标 %s 天判定为「%s」" % (compare.get("user_target"),
                                                  compare.get("target_verdict"))
        else:
            verdict = "；用户未提出总工期"
        self.done_summary = ("理论最短 %s 天、资源不超额 %s 天（相差 %s 天）%s；"
                             "警告 %d 条"
                             % (theory.get("total_duration_days", 0),
                                ok.get("total_duration_days", 0), delta, verdict,
                                len(versions.get("warnings") or [])))
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "两版工期都排完了（详细对比在下面的复审门里）"})

        # 对外输出去掉内部辅助键（_planned / _curves 只供自查，不进 ctx）
        clean_versions = {
            "theory_min": _public_version(theory),
            "resource_ok": _public_version(ok),
            "compare": compare,
            "warnings": list(versions.get("warnings") or []),
        }
        return {
            "schedule_versions": clean_versions,
            "schedule": clean_versions["resource_ok"],
            "schedule_warnings": list(out.get("schedule_warnings") or []),
            "norm_coverage": out.get("norm_coverage") or {},
            "machine_labor_demand": out.get("machine_labor_demand") or {},
            # 用户申报设备 → 计划资源的逐项对账（异名绑定 / 未匹配告警）
            "equipment_binding": out.get("equipment_binding") or {},
            # 施工组织缺口（契约 §3；plan_assembler 会搬进 meta.organization_gaps）。
            # 没有取到节拍 / 全部可行 → 空列表，绝不编造缺口。
            "organization_gaps": list(out.get("organization_gaps") or []),
            # ---- 域 8.3 搬运：日级资源账单数据 ----
            # `ok` 是裸版本 dict（在 `_public_version` 剥 `_` 前缀之前），
            # `_daily_share` / `_backpressure` 只存在于裸版本上。
            # 这里把它们抬成独立 ctx 键，供 `plan_assembler.build_meta` 白名单搬运。
            # ⚠️ 不解除 `_` 前缀（会打破 `test_algorithm_parity.py` 一类守护）。
            "daily_resource_share": ok.get("_daily_share") or {},
            "resource_backpressure": ok.get("_backpressure") or {},
        }


def _public_version(version):
    """对外版本：只保留契约里写明的字段，顺序固定（便于逐字段比对）。"""
    return {
        "total_duration_days": version.get("total_duration_days", 0),
        "schedule": version.get("schedule", []),
        "critical_path": version.get("critical_path", []),
        "daily_labor": version.get("daily_labor", []),
        "daily_equipment": version.get("daily_equipment", []),
        "peak_labor": version.get("peak_labor", 0),
        "peak_equipment": version.get("peak_equipment", 0),
        "over_limit": version.get("over_limit", []),
        "capped": version.get("capped", []),
    }
