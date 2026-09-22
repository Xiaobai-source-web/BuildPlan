"""施工组织层 · 缺省经验参数（**全部 ai_estimate，可覆盖**）。

⚠️ **2026-09-21（C 组「资源与工期计算收敛」）后的口径**（依据
`docs/资源与工期计算重构方案_v1.md`）：

    资源只来自**工作面容量**；工期**只有一个公式**：
        工期 = ceil(需求量 ÷ 有效容量)
    链路：【0】层面积表 →【1】按 MSSA=500 切施工段（`segment_plan`）
        →【2】段容量 = ceil(段面积 ÷ MWI)（`segment_capacity`）
        →【3】需求量 = 工程量 × 定额
        →【4】有效容量 = min(汇总容量, 用户同类限额)
        →【5】工期 = ceil(需求量 ÷ 有效容量)（`segment_capacity.duration_days`）
        →【6】投入资源 = 有效容量

**本文件已删除**（C8 删除清单 1/2/7，见 `docs/落地清单_Wave23.md` 清单 1）：
  · `eta(c)` / `ETA_FLOOR` —— 规模效率折减（无规范依据，已按裁定 3 整条删）；
  · `CONTINUOUS_POUR_KEYWORDS` / `CONTINUOUS_POUR_ACTIVITIES` / `is_continuous_pour()`
    —— 结构缝/连续浇筑分段推导（裁定 1/11：不考虑结构缝）；
  · `CREW_CEILING_BAND` / `CREW_CEILING_CAP` / `CREW_CURVE_REF` /
    `CREW_SOURCE_ORG_CURVE` / `crew_ceiling_from_curve()`
    —— 「max(crew_max, min(40, ceil(crew_base×2.5)))」那条 ×2.5 带（无规范依据）；
    每工人数上限改由 **MWI 表（`Resource_Workface_Index`）** 给出，见 §新链路常量。

本模块只放**常量与纯函数**（不碰 IO、不 import scheduler，避免循环依赖）。
所有取值都是缺省经验值：交付物必须标注"缺省经验参数（AI 估算）"。
"""

from __future__ import annotations

import math

# 机动性取值 —— **单一来源**是 `segment_capacity`（本模块只做再导出，不另立字面量）。
from .segment_capacity import FIXED as MOBILITY_FIXED
from .segment_capacity import MOBILE as MOBILITY_MOBILE
from .segment_capacity import SITE as MOBILITY_SITE

# ==================== 新链路：工作面容量（唯一资源来源）====================

#: MWI 表名（方案 §5 阶段 1）。**运行时只读这一张**取 `mwi` / `resource_mobility`；
#: 表结构由 P1 维护，本模块只登记表名，不写死任何"资源名 → 型别"的映射。
MWI_TABLE = "Resource_Workface_Index"

#: `mwi_unit` 的规范写法（面积/人、面积/台）。MWI 表里 ASCII 的 `m2/人` 由
#: `component_ratio.normalize_area_unit` / `kb_units.normalize_unit` 归一。
MWI_UNIT_AREA = "m²/人"

#: 三类机动性的判据出处（产物留痕用，人可读）。
MOBILITY_BASIS = (
    "方案 §3.2：里面干活→fixed（逐段取整相加）；外面服务→mobile（汇总取整一次）；"
    "塔吊/施工电梯→site（不进段容量，走 _site_equipment）。"
    "取值来自 Resource_Workface_Index.resource_mobility（P1 维护），本模块不另立映射。"
)

# ==================== 作业面与班次（保留项：新链路不再使用）====================

#: 同时作业面数上限（住宅标准层，可配置）。
#: ⚠️ 新链路按 MSSA=500 切施工段、段数不设上限（裁定 9），**不再使用本常量**；
#: 保留仅为兼容历史 import 点与旧计划回放。
N_CAP = 4

#: 班次上限。新链路不使用班次（工期只由需求量 ÷ 有效容量决定），保留仅为兼容。
S_MAX_DEFAULT = 1

#: 单个作业面的**合理面积**（㎡/面，AI 估算）。
#: 新链路用 MWI（`Resource_Workface_Index.mwi`）取代本常量；保留仅为兼容 `face_area_of`。
FACE_AREA_DEFAULT = 300.0

#: 按 `work_type_l3` 覆盖"单面合理面积"。
#: ⚠️ 新链路不使用（容量只来自 MWI）；保留仅为兼容 `org_plan.face_area_of` 的历史调用点。
FACE_AREA_BY_L3 = {
    "rebar": 300.0,          # 钢筋绑扎
    "steel": 300.0,
    "formwork": 400.0,       # 模板（含铝模）
    "concrete": 400.0,       # 混凝土浇筑
    "masonry": 150.0,        # 砌体（湿作业、材料周转慢）
    "plaster": 200.0,        # 抹灰
    "scaffolding": 300.0,    # 脚手架
    "waterproof": 300.0,
    "flooring": 300.0,
    "decoration": 200.0,
    "mep": 300.0,
    "installation": 300.0,
    "earthwork": 500.0,
    "piling": 400.0,
    # 中文别名：真实计划里叶子的 `work_type` 就是中文（实测 plan_run_1789827002.json：
    # 钢筋工程 22 条 / 模板工程 22 / 砌筑工程 56 / 混凝土工程 41…），
    # 按 `work_type_l3` 覆盖时两种写法都要认。
    "钢筋工程": 300.0,
    "模板工程": 400.0,
    "混凝土工程": 400.0,
    "钢筋混凝土工程": 400.0,
    "砌筑工程": 150.0,
    "抹灰工程": 200.0,
    "脚手架工程": 300.0,
    "架子工程": 300.0,
    "土方工程": 500.0,
    "桩基工程": 400.0,
    "防水工程": 300.0,
    "楼地面工程": 300.0,
    "涂饰工程": 200.0,
}

# ==================== 措施性 / 按次工序的固定操作时长 ====================
# ⚠️ 新链路的工期只有 `duration_days(需求量, 有效容量)` 一个入口，**本表不再参与
# 工期计算**（原先由已删除的 `org_plan.plan_workfaces` 消费）。保留仅为兼容历史
# import 点与 `tests/test_final_ws4_crew_source.py` 的数据自检。

#: 措施性、**按次**工序的固定操作时长（天）。键 = 工序/活动名（叶子工序名里包含即命中）。
MEASURE_ITEM_DURATION = {
    "爬架提升": 1.0,
    "爬架爬升": 1.0,
    "整体提升架提升": 1.0,
    "塔吊顶升": 1.0,
    "塔吊附着": 1.0,
    "顶升加节": 1.0,
    "附着安装": 1.0,
    "施工电梯安装": 1.0,
    "施工电梯顶升": 1.0,
}

#: 措施性但**没有独立工期**的按次工序（随主体周期/按次发生，命中也不改工期）。
MEASURE_ITEM_NO_DURATION = (
    "基坑监测",
    "沉降观测",
    "变形监测",
    "基坑降水",
)


# ======================================================================
# 【第 2 批 · 域 7.7 / 7.8 / 7.9 / 7.10】场地级垂直运输设备的**项目级常量**
# ======================================================================
# 病根（真实产物 `backend/plans/plan_test_full.json` 实测）：塔吊 / 施工电梯的台数
# **恒为 1 台**（`resource.py:_site_equipment_quantity` 末行 `return 1.0, "ai_default"`），
# 与项目规模（12 栋 / 38 层 / 21.5 万 m²）完全脱钩。
#
# 口径（用户裁定，域 7.7–7.10）：
#   · **项目级常量**：全项目一次性定好（在 `boundary` 节点），写成
#     `boundary_conditions["site_machine_const"]`，**不分到 L4 时重新估**；
#   · 用户申报 → **用户值赢**；没申报 → 按**已有建筑参数**（栋数 / 面积 / 层数）估；
#   · **默认够用、不判超限**（不进 `over_limit`）；每天的资源账本都要记这个常量
#     （= 连续在场）；
#   · 司机 / 信号工**跟台数走**（1 台塔吊 = 1 司机 + 1 信号工），**不设限额**；
#   · 估算规则**明示、可复核、冻结 + 标注**，**不引入新数据源、不编系数表**（无随机、
#     无时间戳、无字典序依赖 → 重跑逐位一致）。
#
# 为什么放在本模块（父代理裁定 1）：`boundary` 节点要算台数（它负责写常量），
# `resource` 节点要读常量；node 之间不许互相 import，所以纯估算函数放在这个
# **纯默认值模块**里，两边一起 import，没有反向依赖。
# ⚠️ 本节的规则表是**台数的唯一真源**：改阈值只改这里，产物里的 `rule` 文本会随之变。

#: `boundary_conditions` 上的键名（**唯一真源**，boundary / resource 都从这里取）。
#: ⚠️ **绝对不许**登记进 `boundary.MODEL_DECLARED_KEYS` —— 那是 `strip_model_declared()`
#: 的判据表，登记了常量会被 `bc.pop(...)` 清掉（域 7.7 铁律）。
SITE_MACHINE_CONST_KEY = "site_machine_const"

#: 7.8 的**日账本**键：`resource_demand` 上每天记一次项目级常量（= 连续在场），
#: 供 `plan_assembler` / `delivery` 逐日渲染，也让"默认够用、不判超限"在产物里可查。
SITE_MACHINE_CONST_DAILY_KEY = "_site_machine_const_daily"

#: 场地级（全场地常驻、逐日取 max 不按任务叠加）的机械——**顺序即产物里 `machines` 的键序**，
#: 固定元组保证逐位可复现（不许改成 set / dict 遍历）。
SITE_MACHINE_MACHINES = ("塔吊", "施工电梯")

#: 台数单位（产物的 `unit` 字段）。
SITE_MACHINE_UNIT = "台"

#: 「一栋的规模口径」= 18000 m²（AI 估算，无规范依据）。取值理由：真实高层住宅
#: 一栋的结构面积规模 ≈ 1.8 万 m²（本项目 215000 m² ÷ 12 栋 ≈ 17900 m²/栋），
#: 而一台塔吊 / 施工电梯的服务对象就是**一栋楼**。所以"s 栋 → s 台"与
#: "每 18000 m² 1 台"在典型高层项目上**给同一个数**（12 栋 → 12 台；
#: 215000 m² → ceil(12.0) = 12 台），两条规则可以互相复核。
#: 栋数取不到时才退回面积法（见 `estimate_site_machine_count`）。
SITE_MACHINE_BUILDING_AREA = 18000.0

#: 高层判据：层数 ≥ 10 → 每栋 1 台塔吊（一栋一个作业面，塔吊服务半径覆盖单栋平面）；
#: 层数 < 10（多层）→ 一台塔吊可覆盖相邻 **2** 栋（总高小、回转半径富余）。
SITE_MACHINE_HIGH_RISE_FLOORS = 10.0
SITE_MACHINE_LOW_RISE_UNITS_PER = 2

#: 施工电梯：**每栋 1 台**（双笼 SC200/200 单台即可服务常见高层住宅；施工电梯沿单栋
#: 竖向服务，**不能跨栋移动**，所以台数 = 栋数）。加台数须由用户申报，AI 不替用户加。
SITE_MACHINE_HOIST_PER_BUILDING = 1

#: 台数下限（"够用"口径：估不出来时至少 1 台常驻，且来源标 `ai_default`、理由如实写出）。
SITE_MACHINE_MIN_COUNT = 1

#: 常量块的标签文案（人可读，交付物直接可用）。
SITE_MACHINE_CONST_LABEL = ("塔吊 / 施工电梯为项目级常量（全项目统一，不分到 L4 重新估），"
                            "默认够用、不判超限")

#: AI 估算的口径声明（与口径总表 13 同形：无规范依据、待审）。
SITE_MACHINE_ESTIMATE_NOTE = "AI 经验估算规则（无规范依据，待审）"

#: KB `Equipment_Crew_Mapping` 取不到配员时的兜底（**唯一真源**）。取值与实测的 KB 两行
#: 逐字一致（塔吊：司机1名+信号工1名；施工电梯：司机1名），所以兜底不会改变配员口径。
SITE_MACHINE_CREW_FALLBACK = {
    "塔吊": {"composition": "司机1名+信号工1名", "crew_size": 2},
    "施工电梯": {"composition": "司机1名", "crew_size": 1},
}


def _machine_number(value):
    """参数值 → 正浮点数；取不到 / 非正 → None（**不猜**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return f


def _fmt_num(value):
    """数字 → 人能读的短文本（12.0 → "12"；17000.0 → "17000"；12.5 → "12.5"）。"""
    f = float(value)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else ("%g" % f)


def estimate_site_machine_count(machine, params):
    """7.10：只用**已有建筑参数**估一台场地级设备的台数 → ``(count, rule, basis)``。

    `params` = `ctx["extracted_params"]`（`total_area` / `building_count` / `floors` 三个键，
    与 `boundary.CORE_KEYS` 同源）。**绝不**读定额 / 施工量 / 节拍，**绝不**引入新数据源。

    规则（明示、可复核，阈值见本节常量；`rule` 文本会逐字落进产物）：

      塔吊：
        ① 有 `building_count`：层数 ≥ 10（或层数未知，按"够用"取高层口径）→ **每栋 1 台**；
           层数 < 10 → **每 2 栋 1 台**。`count = max(1, ceil(栋数 ÷ 每台栋数))`；
        ② 无栋数、有 `total_area`：**每 18000 m² 1 台**（≈ 一栋高层的规模，见常量说明）；
        ③ 两者都取不到：**1 台**（最小常驻），来源标 `ai_default`，rule 里写明"参数缺失"。

      施工电梯：每栋 1 台（`count = max(1, 栋数)`）；无栋数 → 每 18000 m² 1 台；都缺 → 1 台。

    返回的 `basis` 逐项记下**原始输入**与命中的规则号（`rule_id`），便于复核与冻结比对；
    `count` 一律 `int`（确定性：只有 `ceil` + 固定顺序，无随机、无时间戳）。
    """
    p = params if isinstance(params, dict) else {}
    buildings = _machine_number(p.get("building_count"))
    floors = _machine_number(p.get("floors"))
    area = _machine_number(p.get("total_area"))

    def basis(rule_id, per, extra=None):
        b = {"rule_id": rule_id, "machine": str(machine),
             "inputs": {"building_count": buildings, "floors": floors, "total_area": area},
             "per_unit": per}
        if extra:
            b.update(extra)
        return b

    if str(machine) == "塔吊":
        if buildings:
            if floors is None or floors >= SITE_MACHINE_HIGH_RISE_FLOORS:
                per = 1
                why = ("层数 %s ≥ %s，判为高层：每栋 1 台"
                       % (_fmt_num(floors) if floors is not None else "未取得（按高层口径，宁可够用）",
                          _fmt_num(SITE_MACHINE_HIGH_RISE_FLOORS)))
                rid = "tower_crane.per_building.high_rise"
            else:
                per = SITE_MACHINE_LOW_RISE_UNITS_PER
                why = ("层数 %s < %s，判为多层：每 %d 栋 1 台"
                       % (_fmt_num(floors), _fmt_num(SITE_MACHINE_HIGH_RISE_FLOORS), per))
                rid = "tower_crane.per_building.low_rise"
            n = int(math.ceil(buildings / float(per)))
            n = max(SITE_MACHINE_MIN_COUNT, n)
            rule = ("按栋数：%s —— ceil(栋数 %s ÷ %d 栋/台) = %d 台"
                    % (why, _fmt_num(buildings), per, n))
            return n, rule, basis(rid, per, {"units_per": per})
        if area:
            per = SITE_MACHINE_BUILDING_AREA
            n = max(SITE_MACHINE_MIN_COUNT, int(math.ceil(area / per)))
            rule = ("按面积：每 %s m² 1 台（≈ 一栋高层住宅的建筑面积规模）—— "
                    "ceil(%s m² ÷ %s) = %d 台（栋数未取得）"
                    % (_fmt_num(per), _fmt_num(area), _fmt_num(per), n))
            return n, rule, basis("tower_crane.per_area", per)
        rule = ("塔吊：无可用建筑参数（building_count / total_area 均未取得）—— "
                "按最小常驻 %d 台，来源 ai_default（待用户申报台数）" % SITE_MACHINE_MIN_COUNT)
        return SITE_MACHINE_MIN_COUNT, rule, basis("tower_crane.no_input", None,
                                                   {"inputs_missing": True})

    # 施工电梯（以及任何将来的同口径场地级设备）：每栋 1 台 → 面积法 → 1 台
    if buildings:
        per = SITE_MACHINE_HOIST_PER_BUILDING
        n = max(SITE_MACHINE_MIN_COUNT, int(math.ceil(buildings / float(per))))
        rule = ("按栋数：每栋 %d 台（施工电梯沿单栋竖向服务，不能跨栋移动）—— "
                "ceil(栋数 %s ÷ %d 栋/台) = %d 台"
                % (per, _fmt_num(buildings), per, n))
        return n, rule, basis("hoist.per_building", per, {"units_per": per})
    if area:
        per = SITE_MACHINE_BUILDING_AREA
        n = max(SITE_MACHINE_MIN_COUNT, int(math.ceil(area / per)))
        rule = ("按面积：每 %s m² 1 台（≈ 一栋高层住宅的建筑面积规模）—— "
                "ceil(%s m² ÷ %s) = %d 台（栋数未取得）"
                % (_fmt_num(per), _fmt_num(area), _fmt_num(per), n))
        return n, rule, basis("hoist.per_area", per)
    rule = ("%s：无可用建筑参数（building_count / total_area 均未取得）—— "
            "按最小常驻 %d 台，来源 ai_default（待用户申报台数）"
            % (str(machine), SITE_MACHINE_MIN_COUNT))
    return SITE_MACHINE_MIN_COUNT, rule, basis("hoist.no_input", None, {"inputs_missing": True})


def resolve_site_machine_crew(machine, kb_row=None, parser=None):
    """场地级设备的「每台配员」→ ``{"crew", "composition", "source", "ref", "confidence"}``。

    唯一真源是 KB `Equipment_Crew_Mapping`（`kb_row` 由调用方用 `kb.crew_for_machine()`
    读进来 —— 本模块**不碰 IO**，只做纯解析与兜底）；KB 取不到才用
    `SITE_MACHINE_CREW_FALLBACK`，并把 `source` 标成 ``"fallback"`` —— 调用方**必须**
    把它写进标注，不许静默降级。

    7.9：「司机 / 信号工跟台数走」= 这里给的是**每台**人数，逐日配员 = 台数 × 每台人数
    （乘法在 `resource._inject_site_equipment` 与 `plan_assembler.site_equipment_contrib`，
    本函数不做乘法，也不设任何限额）。
    """
    if parser is None:
        try:
            # ⚠️ 路径是 `.nodes.crew_bind`（本模块在 `pipeline/` 下，解析器在 `pipeline/nodes/` 下）；
            # 延迟 import 避免与 `nodes` 包形成模块级循环。
            from .nodes.crew_bind import parse_crew_composition as parser
        except Exception:                                     # pragma: no cover
            parser = None
    row = kb_row if isinstance(kb_row, dict) else None
    crew = {}
    if row and row.get("composition") and parser is not None:
        try:
            crew = parser(str(row["composition"])) or {}
        except Exception:
            crew = {}
    if crew:
        return {"crew": crew, "composition": str(row["composition"]),
                "source": "kb", "ref": str(row.get("source_type") or "kb"),
                "confidence": str(row.get("confidence") or "")}
    fb = SITE_MACHINE_CREW_FALLBACK.get(str(machine)) or {}
    crew = {}
    if parser is not None and fb.get("composition"):
        try:
            crew = parser(str(fb["composition"])) or {}
        except Exception:
            crew = {}
    return {"crew": crew, "composition": str(fb.get("composition") or ""),
            "source": "fallback", "ref": "SITE_MACHINE_CREW_FALLBACK",
            "confidence": "LOW"}


def build_site_machine_const(params, declared=None, crew_of=None, decided_by="boundary"):
    """7.7 / 7.10：**一次性**定好全项目的塔吊 / 施工电梯台数 → `site_machine_const` 块。

    `declared`：用户申报台数 ``{机械名: 台数}``（来自 `boundary_conditions.equipment`
    且来源标注为 `user`）——**用户值赢**，逐台采用；没申报的机械按 `estimate_site_machine_count`
    估。
    `crew_of` ：``callable(machine) -> {"crew","composition","source","ref"}``（boundary /
    resource 各传自己那份，读的是同一张 KB 表）；缺省 → 每台配员为空 dict。

    确定性：`machines` 的键序 = `SITE_MACHINE_MACHINES` 固定元组；台数只有 `ceil`；
    无随机数、无时间戳、无字典序依赖 ⇒ **重跑逐位一致**（冻结）。
    """
    decl = declared if isinstance(declared, dict) else {}
    machines = {}
    for machine in SITE_MACHINE_MACHINES:
        q = _machine_number(decl.get(machine))
        if q:
            count = max(SITE_MACHINE_MIN_COUNT, int(math.ceil(q)))
            rule = ("用户申报：%s 台（用户值优先，本常量照申报采用）" % _fmt_num(q))
            basis = {"rule_id": "user.declared", "machine": machine,
                     "declared": q, "inputs": {}}
            source, conf = "user", "HIGH"
        else:
            count, rule, basis = estimate_site_machine_count(machine, params)
            source, conf = "ai_default", "LOW"
        crew_info = {}
        if callable(crew_of):
            try:
                crew_info = crew_of(machine) or {}
            except Exception:
                crew_info = {}
        crew_src = ("kb:Equipment_Crew_Mapping" if crew_info.get("source") == "kb"
                    else "fallback:SITE_MACHINE_CREW_FALLBACK")
        machines[machine] = {
            "count": int(count),
            "unit": SITE_MACHINE_UNIT,
            "count_source": source,
            "confidence": conf,
            "rule": rule,
            "basis": basis,
            "inputs": dict(basis.get("inputs") or {}),
            "crew_per_unit": dict(crew_info.get("crew") or {}),      # **每台**人数（7.9）
            "crew_composition": crew_info.get("composition") or "",
            "crew_source": crew_src,
            "crew_ref": crew_info.get("ref") or "",
            "const_period": {"from_day": 0, "to_day": None},         # 7.8：连续在场
            "frozen": True,
            "note": ("项目级常量：整场只此 %d 台（%s）；**不分到 L4 时重新估**；"
                     "默认够用、**不进超限清单**；每天的资源账本按连续在场记这一次"
                     % (int(count), rule)),
        }
    sources = set(m["count_source"] for m in machines.values())
    return {
        "schema": 1,
        "caliber": "project_level_constant",
        "decided_at": str(decided_by),      # boundary = 边界节点一次定好
        "frozen": True,
        "machines": machines,
        "label": SITE_MACHINE_CONST_LABEL,
        "estimate_note": SITE_MACHINE_ESTIMATE_NOTE,
        "_source": "user" if sources == {"user"} else "model",
        "note": ("塔吊 / 施工电梯是**项目级常量**：在边界节点一次性定好，全项目统一口径，"
                 "**不分到 L4 时重新估**；用户申报（%s）优先，其余按已有建筑参数"
                 "（栋数 / 面积 / 层数）用明示规则估算并冻结；默认够用，不做超限判定。"
                 % "、".join("、".join(sorted(set(
                     "user" if m["count_source"] == "user" else "—"
                     for m in machines.values())) and ["（无）"]) or "（无）")),
    }


def measure_item_duration(*texts):
    """命中措施项 → ``(固定操作时长天数, 命中的键)``；未命中 → ``(None, None)``。

    命中 `MEASURE_ITEM_NO_DURATION` 时返回 ``(None, 命中的键)``：**按次但无独立工期**。
    """
    for text in texts:
        if not text:
            continue
        s = str(text)
        for key, days in MEASURE_ITEM_DURATION.items():
            if key in s:
                return float(days), key
        for key in MEASURE_ITEM_NO_DURATION:
            if key in s:
                return None, key
    return None, None

