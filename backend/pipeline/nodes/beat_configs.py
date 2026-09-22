"""节拍型节点配置 — 4 个分层分段流水的「代码节拍引擎」配置真源（T-12）

覆盖 4 个节拍型实体阶段：地下室结构 / 地上主体结构 / 二次结构与砌体 / 装饰装修。

由逐相 LLM 一次性产出「总量+拍工期」的扁平叶子，改为**代码节拍引擎**：
  - 叶子 id 用纯数字点分 `p.z.s.k`（阶段.分区.段.工序）——全程兼容 normalize_wbs（强制
    \\d+(\\.\\d+)+）；区位（Ⅰ区 3层）放 name + location，中文 id 会被 normalize 重编号故不用。
  - 节拍 = 单段量 / 日产能，代码算 `duration_days`，不靠 LLM 拍工期。
  - 结构搭接（同段串行 / 跨段 / 跨相）由 layer_engine 代码产出，deps 对节拍叶子跳过 LLM。

竖向分段口径（v2.2 修正，务必区分「竖向层数」与「平面分区」两个维度）：
  - **竖向施工层 ≥ 楼层**：结构类分部（地上主体结构 / 二次结构与砌体）必须**一层一段**
    （floors_per_segment=1）。旧的「5 层一段」会产出「1-5层钢筋全绑完 → 1-5层模板 → …」，
    而模板未支、上层楼面无作业面，钢筋工上不去层，实际做不到。
  - **装饰装修**是连续上移分部（同一空间可自上而下连续推进），允许 **3 层一组**（floors_per_segment=3），
    成组比一层一段更贴近现场班组安排。
  - **地下室结构**保持 0.5 层一段：底板 / 墙柱 / 顶板本就分层浇筑，4 段 × 0.5 层覆盖全部 2 层。
  - 竖向段数不写死：`segment_floors()` 按 ceil(总层数 / 每段层数) 推导（38 层 × 1 层/段 = 38 段）。
  - **总层数以项目参数 `params["floors"]` 为准**（见 layer_engine._eff_floors）；配置里的
    `floors`（38）仅是取不到参数时的兜底默认，不是写死的 38 拍。
  - 平面分段（分区数）按**单栋**标准层面积建议（`suggest_zones()`）：面积取不到时沿用配置
    `zones` ——属 AI 默认值，须让用户可改。
  - **多栋项目（`building_count`>1）**：分区建议与单层工程量都按**单栋**口径
    （`per_building_params()` 先把总量类参数折算成单栋），全项目 N 栋平行施工。
    详见 `derive_beat_quantities` 规则 9。
"""

import copy
import math

from .resource import PRODUCTIVITY  # 复用现有产能基线（钢筋工 1.5 t/d、模板工 15 m²/d…）

# 按名匹配的 4 个节拍阶段（勿用 index：装饰装修 是 DEFAULT_PHASES 8，非 0-based index）
BEAT_PHASE_NAMES = {"地下室结构", "地上主体结构", "二次结构与砌体", "装饰装修"}

# 竖向必须「一层一段」的结构类分部（竖向施工层 = 楼层，不允许跨层成组）
ONE_FLOOR_PER_SEGMENT_PHASES = {"地上主体结构", "二次结构与砌体"}

# 允许「N 层一组」的连续上移分部（装饰装修：同一空间自上而下连续推进）
GROUPED_SEGMENT_PHASES = {"装饰装修": 3}

# ---- 节拍产能表（复用 resource PRODUCTIVITY 作基数，单位 = 量/人/天）----
# count=班组人数，efficiency=效率系数；daily = BEAT_PRODUCTIVITY[res].unit_output × count × efficiency
BEAT_PRODUCTIVITY = {
    "钢筋工": {"unit_output": 1.5, "count": 12, "efficiency": 1.0},   # t/d
    "模板工": {"unit_output": 15.0, "count": 10, "efficiency": 1.0},  # m²/d
    "混凝土工": {"unit_output": 20.0, "count": 12, "efficiency": 1.0},  # m³/d
    "泵车": {"unit_output": 80.0, "count": 1, "efficiency": 1.0},     # m³/d
    "吊装工": {"unit_output": 50.0, "count": 4, "efficiency": 1.0},   # m²/d
    "瓦工": {"unit_output": 50.0, "count": 10, "efficiency": 1.0},    # m²/d
    "抹灰工": {"unit_output": 40.0, "count": 12, "efficiency": 1.0},  # m²/d
    "油漆工": {"unit_output": 50.0, "count": 10, "efficiency": 1.0},  # m²/d
    "门窗工": {"unit_output": 20.0, "count": 8, "efficiency": 1.0},   # m²/d（含制作安装）
    "防水工": {"unit_output": 50.0, "count": 8, "efficiency": 1.0},   # m²/d
}


def beat_productivity(resource_name):
    """按资源名取节拍产能；无 → 用 resource.PRODUCTIVITY 原始值兜底。返回 daily 单日产量。

    ⚠️ 产品口径已改为**以知识库定额为准**（见 `beat_crew_count` 的说明）。
    本函数现在只承担两件事：
      ① 没有可用定额时算一个兜底工期（沿用 WBS 原值）；
      ② 算节拍叶子的 `duration_days`，它退化为**节奏下限**（不得快于节拍），
         不再用来反推"需要多少人"。
    真正决定工期的算式是「工程量 ÷ 定额产能 ÷ 设计班组人数」。
    """
    cfg = BEAT_PRODUCTIVITY.get(resource_name)
    if cfg:
        return max(0.1, cfg["unit_output"] * cfg["count"] * cfg["efficiency"])
    base = PRODUCTIVITY.get(resource_name)
    return max(0.1, (base or 10.0) * 1.0)


def beat_crew_count(resource_name):
    """节拍配置里该资源的**设计班组人数**（**施工组织决策参数**，不是 AI 估算数据）。

    以知识库为准之后，一个工序的工期只有两个输入，职责分得很干净：

        工日数 = 工程量 ÷ 定额产能          ← **知识库定额（唯一产能真源，不可动摇）**
        工期   = 工日数 ÷ 设计班组人数       ← **本函数（施工组织决策，用户可直接改）**

    这样就不再出现"目标工期用乐观节拍表、需要人数用定额表"这种两套口径打架的
    情况（那会把主体一层钢筋的编制规模推到 88 人）。班组人数是**可以谈的**，
    定额产能是**不能动的**——这正是产品要表达的边界。

    返回 0 表示该资源不在节拍表里（没有设计班组），调用方按目标工期反推人数。
    """
    cfg = BEAT_PRODUCTIVITY.get(resource_name)
    if not cfg:
        return 0
    try:
        return max(1, int(cfg.get("count") or 1))
    except (TypeError, ValueError):
        return 0


def segment_floors(floors, segments, per=None):
    """把总层数切成段，返回每段 (start_floor, end_floor_excl) 浮点区间。

    - per（floors_per_segment，如 1、3、0.5）优先：按每段固定层数切，末段取余，
      得到干净桶如 38/1→[(1,2),(2,3),…,(38,39)]、38/3→[…,(37,39)]、2/0.5→[(1,1.5),…]。
      段总数 = ceil(floors/per) —— **段数由层数推导，不写死**。
    - 无 per 时退回均分 segments 段（同样半开）。
    保证覆盖全部层数：∑(end−start) == floors（一层一段时同样成立）。
    """
    if floors <= 0 or (per is None and segments <= 0):
        return [(0.0, 0.0)]
    if per and per > 0:
        n_full = int(floors // per)          # 取整层的完整段
        rem = floors - n_full * per
        sizes = [per] * n_full
        if rem > 1e-9:
            sizes.append(rem)                # 末段取余
        if not sizes:
            sizes = [floors]
    else:
        n = max(1, int(segments))
        base = floors / n
        sizes = [base] * (n - 1) + [floors - (n - 1) * base]
    out = []
    cum = 0.0
    for size in sizes:
        out.append((cum + 1.0, cum + size + 1.0))   # 层标签 = 自首层偏移 + 1
        cum += size
    return out


# ---------------- 平面分段（分区数）：按标准层面积建议 ----------------
# ⚠️ 旧的四档经验阈值（<800 → 1 段；800~1500 → 2；1500~2500 → 3；>2500 → 4）**已废止**
# （B2 接口委派）：分区数统一走《资源与工期计算重构方案 v1》§4.1 的 MSSA = 500 m² 规则
# （`n = ceil(层面积 ÷ 500)` + 余量判定；段数不设上限），实现真源在 `pipeline/segment_plan.py`。
# 旧常量 `ZONE_AREA_MIN / ZONE_AREA_MID / ZONE_AREA_MAX / ZONE_COUNT_CAP` 随之删除
# （已无任何调用方；`grep ZONE_AREA_ backend` 为空）。


# ---------------- 栋数（多栋平行）----------------
# 本项目最大的口径坑：`total_area` 是**全项目**（多栋合计）建筑面积。若直接拿它
# 除以层数，会得到"N 栋加起来那么大的标准层"——分区数被推高、单层量被放大 N 倍，
# 而工作面容量（每施工段多少人）本来只是"一栋楼一个工作面"的口径，两边对不上，
# 工期就会被放大。所以：**分区建议用单栋标准层面积；单层工程量也按单栋口径算**，
# 全项目 N 栋平行施工（单栋工期 ≈ 项目工期），项目总量仍在 overview 里报全量。
DEFAULT_BUILDING_COUNT = 1


def building_count(params):
    """项目栋数（多栋平行施工的栋数）。

    取不到 / 空 / 非数 / <1 → ``DEFAULT_BUILDING_COUNT``（1）——**默认单栋，不瞎猜**；
    是"用户可改"的参数，不是 AI 假设。
    """
    if not isinstance(params, dict):
        return DEFAULT_BUILDING_COUNT
    v = params.get("building_count")
    if v is None or v == "":
        return DEFAULT_BUILDING_COUNT
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_BUILDING_COUNT
    return n if n >= 1 else DEFAULT_BUILDING_COUNT


def per_building_params(params):
    """把「全项目总量」参数折算成「单栋」口径；栋数 ≤1 → 原样返回（不复制）。

    折算的键＝总量类参数（面积/混凝土/钢筋/土方/预制/砌体），并且把 `building_count`
    置 1 —— 返回的就是"这个项目如果只有一栋"的那套参数。**置 1 很关键**：
    下游 `standard_floor_area()` 自己会再除以栋数，若不置 1 就会**重复除以栋数**
    （实测把单层模板量算成 98 ㎡ 而不是 1178 ㎡）。层数、分区数不折算。
    """
    if not isinstance(params, dict):
        return params
    n = building_count(params)
    if n <= 1:
        return params
    out = dict(params)
    # 【第 2 批 · 域 2】删 `total_precast` / `total_wall`；增 `total_infill_wall` / `total_pile`
    # （这两个新键也是"全项目总量"口径，多栋时必须同样折成单栋，否则量会放大 N 倍）。
    for k in ("total_area", "total_concrete", "total_rebar", "total_earthwork",
              "total_infill_wall", "total_pile", "total_formwork", "total_masonry"):
        v = out.get(k)
        if v is None or v == "":
            continue
        try:
            out[k] = float(v) / n
        except (TypeError, ValueError):
            continue
    out["building_count"] = 1        # 已折算过，别让下游再除一次
    return out


def standard_floor_area(params):
    """从项目参数推「标准层面积」= total_area ÷ 栋数 ÷ floors。

    **按单栋口径**：分区（施工段）是平面上"一栋楼怎么切"的问题，多栋项目下
    必须先把总面积摊到单栋，否则 12 栋的合计面积会被当成一栋的超大平层。

    参数可能缺失 → 返回 None（**不瞎猜**，由调用方沿用配置默认 zones）。
    非正数 / 非法值同样返回 None。
    """
    if not isinstance(params, dict):
        return None
    try:
        total = float(params.get("total_area"))
        floors = float(params.get("floors"))
    except (TypeError, ValueError):
        return None
    if total <= 0 or floors <= 0:
        return None
    return total / building_count(params) / floors


def suggest_zones(standard_floor_area):
    """按标准层面积建议**平面施工段数**（zones）—— 委派 `segment_plan`（B2）。

    ⚠️ **行为替换**（方案 §4.1 / 裁定 5）：口径不再是旧四档经验值
    （<800→1 / 800~1500→2 / 1500~2500→3 / >2500→4），而是**单一 MSSA = 500 m²** 规则：

        n = ceil(层面积 ÷ 500)；先满后余；余量 < 500/3 时弃用 MSSA、段数减一后均匀切

    两套口径在若干面积上给出**不同段数**（如 1200 m²：旧 2 / 新 3；4000 m²：旧 4 / 新 8），
    所以调用方的分区数、叶子数、工期都会随之变化。**段数不设上限**（裁定 9）。

    签名保持 `(standard_floor_area)`（旧调用点无需改动，不需要位置参数以外的能力）；
    取不到 / 非法 / <= 0 → `None`（沿用既有约定：**不瞎猜**，调用方回落配置 `zones`）。
    """
    from ..segment_plan import suggest_zones as _impl     # 惰性导入，破包级循环依赖
    return _impl(standard_floor_area)


def suggest_zones_from_params(params):
    """params → 建议平面段数（委派 `segment_plan`）；面积推不出来 → `None`。

    面积口径不变：**单栋**标准层面积 = 总建筑面积 ÷ 栋数 ÷ 地上层数
    （多栋合计面积不得被当成一栋的超大平层，见 `standard_floor_area`）。
    """
    from ..segment_plan import suggest_zones_from_params as _impl
    return _impl(params)


def normalize_vertical_split(phase, cfg, floors=None):
    """把竖向分段归一到产品口径；LLM 细化后仍要拦，防退回「5 层一段」。

    - 主体结构 / 二次结构与砌体：一层一段（floors_per_segment=1），段数 = 总层数
    - 装饰装修：3 层一组（连续上移分部，见 GROUPED_SEGMENT_PHASES）
    - 其余（地下室结构）：保留配置（0.5 层一段的分层浇筑）
    floors 传有效层数（项目参数优先）；不传则用配置里的兜底层数。
    只改竖向口径，不动 cycle / 产能表 / lead_in。返回同一个 cfg（原地改，便于链式调用）。
    """
    if not isinstance(cfg, dict):
        return cfg
    try:
        n_floors = float(floors) if floors is not None else float(cfg.get("floors") or 1)
    except (TypeError, ValueError):
        n_floors = float(cfg.get("floors") or 1)
    if n_floors <= 0:
        n_floors = 1.0
    if phase in ONE_FLOOR_PER_SEGMENT_PHASES:
        cfg["floors_per_segment"] = 1
        # segments 仅作兜底展示；实际段数由 segment_floors 按项目层数推导
        cfg["segments"] = max(1, int(math.ceil(n_floors)))
    elif phase in GROUPED_SEGMENT_PHASES:
        per = float(GROUPED_SEGMENT_PHASES[phase])
        cfg["floors_per_segment"] = per
        cfg["segments"] = max(1, int(math.ceil(n_floors / per)))
    return cfg


# ---------------- 4 节点基线配置 ----------------
# cycle: 每工序 qty_per_floor=单层量, unit, work_type(对齐 resource 匹配), resource
# attach_measures: 措施项挂某工序后
# lead_in: 与前节点的搭接 from_node + predecessor_task + floors_ahead(→lag 天)
# floors: **兜底默认层数**——有效层数优先取 params["floors"]（见 layer_engine._eff_floors），
#         配置里的 38 只在取不到项目参数时生效，不是写死的 38 拍。
# zones: 兜底平面分区；有标准层面积时由 suggest_zones() 建议（AI 默认，用户可改）。
# ================================================================================
# 【1】参数缺失时的兜底基线值：按工程经验单位率推导，不拍脑袋
# ================================================================================
# 为什么需要：v2.3 的规则是「参数缺失绝不猜 → 原样保留基线写死值」。但旧的地下室写死值
# （钢筋 2100 t / 模板 18000 ㎡ / 混凝土 8200 m³ 每层）**本身完全不可用**——它是把"整个
# 地下室的总量"误当"每层量"。真实计划 `plan_sample3_after_org_v2`（total_rebar/total_concrete
# 全为 null，整份计划退回基线）里，叶子 4.1.1.1「1-0.5层 钢筋绑扎」= 1050 t，比全项目钢筋
# 总量还多，组织层据此报「需要 39 个作业面」、单段排程 135 天。
#
# 口径（原 `REBAR_RATIO` 注释里的工程规则，常量已退役、规则仍然成立）：
#   「同一栋楼、同样的平面尺寸下，每层钢筋量应当是同量级的；地下室因底板/墙更厚，
#     约为地上标准层的 2~3 倍。」
#   「同一栋楼、同样的平面尺寸下，每层钢筋量应当是同量级的；地下室因底板/墙更厚，
#     约为地上标准层的 2~3 倍。」
# 于是兜底基线**不另起一套数**，而是以 `BASE_BEAT_CONFIGS["地上主体结构"]` 那组
# （钢筋 22 t / 模板 1900 ㎡ / 混凝土 180 m³）为"地上标准层"锚点，乘一个部位系数得到。
BASEMENT_VS_STANDARD_FLOOR = {
    # 部位系数 = 地下室单层量 ÷ 地上标准层单层量。逐量给值（不共用一个数）：
    # 每个量都单独和"独立工程单位率"对过账，说明见下方 BASEMENT_BASELINE_*。
    "rebar": 2.5,       # 55 t ÷ 22 t = 2.5
    "concrete": 2.0,    # 360 m³ ÷ 180 m³ = 2.0
    "formwork": 2.5,    # 4750 ㎡ ÷ 1900 ㎡ = 2.5
}

# ---- 地下室的兜底值（按 1900 ㎡ 标准层面积推导）----
# **适用前提**：典型剪力墙住宅，地下室楼板面积 ≈ 塔楼标准层面积（本文件面积类计算器的
# 口径：「地下室楼板 ≈ 塔楼标准层（可取等）」），即每层地下室 ≈ 1900 ㎡。
# **不确定性**：地下室层数多、带人防/多层地下室时，每层面积与墙厚都会变；带人防、抗浮
# 锚杆、厚筏板的项目实际含钢量可能更高。这里给的是**「典型住宅」的量级，不是精确值**，
# 只用于参数缺失时的兜底；参数齐全时一律走参数推算，用不到这组数。
#
# ① 钢筋：1900 ㎡ × 28.9 kg/㎡ ≈ 55 t/层（2.5 倍地上标准层）。
#    推导依据：地下室含钢量（底板+墙柱+顶板）约 25~30 kg/㎡ 建筑面积（取 28.9），
#    是常规住宅地上标准层（22 t ÷ 1900 ㎡ ≈ 11.6 kg/㎡）的 2~3 倍。
#    与混凝土对账：55 t ÷ 360 m³ ≈ 153 kg/m³，落在地下室底板/墙体 120~180 kg/m³ 区间内。
# ② 混凝土：180 m³ × 2.0 = 360 m³/层（= 1900 ㎡ × 0.19 m³/㎡）。
#    推导依据：地下每层混凝土 ≈ 楼面面积 × 0.18~0.2 m³/㎡（顶板 0.15 + 底板/墙柱分摊）。
#    系数取 2.0（不是 2.5）的理由：在 1900 ㎡ 楼面不变的前提下，360 m³ 已相当于平均
#    0.19 m 厚，再往上就与上面的含钢量（153 kg/m³）打架了。
# ③ 模板：1900 ㎡ × 2.5 = 4750 ㎡/层，与参数齐全时的口径**完全同源**
#    （模板接触面积系数 FORMWORK_AREA_FACTOR = 2.5，只由面积定、与混凝土量无关）。
#    与混凝土对账：4750 ㎡ ÷ 360 m³ ≈ 13.2 ㎡/m³，属地下室厚板+墙体的正常量级。
BASEMENT_BASELINE_REBAR_T = 55.0        # = 1900 ㎡ × 28.9 kg/㎡  ≈ 2.5 × 22 t
BASEMENT_BASELINE_CONCRETE_M3 = 360.0   # = 1900 ㎡ × 0.19 m³/㎡ ≈ 2.0 × 180 m³
BASEMENT_BASELINE_FORMWORK_M2 = 4750.0  # = 1900 ㎡ × 2.5 倍      = 2.5 × 1900 ㎡

# 「地上标准层」锚点——必须与 BASE_BEAT_CONFIGS["地上主体结构"]["cycle"] 保持一致。
# 单独提出来是为了**可校验**：自测会把两者逐值比对，防止有人改了主体配置却忘了
# 动兜底值（那会让上面「地下室 = 地上标准层 × 系数」的推导依据失效）。
STANDARD_FLOOR_REBAR_T = 22.0
STANDARD_FLOOR_FORMWORK_M2 = 1900.0
STANDARD_FLOOR_CONCRETE_M3 = 180.0


# ================================================================================
# L3 工种号 / L4 工序号 —— 第 5 批（域 4.1a / 4.1b / 4.2 / 4.2a）
# ================================================================================
# 编号口径（**冻结，不许自行更改**）：叶子 id = `分部号 . L3工种号 . L4工序号 . 分区 . 层段`（5 位）。
#   ① 分部号   = `wbs_phases.DEFAULT_PHASES` 的 **1-based 位置**（1..10）；
#   ② L3工种号 = 该分部 `DEFAULT_PHASES[].kb` **列表内顺序**（该分部内从 1 起，**允许跳号**）；
#   ③ L4工序号 = **LLM 给出的工序清单顺序**，在所属 L3 内从 1 重编。
# 三级**各自可溯源到代码或库**，不许来自 LLM 的直接数字输出（那是"让 LLM 编号"，已禁）。
#
# ★ 域 4.2a（真缺陷）：编号必须对「量变 0」免疫。
#   旧实现（layer_engine.py:167/183）先在 `active_steps` 里剔掉"量0出局"的工序，
#   再 `enumerate(steps, 1)` 从**过滤后**的列表编工序号 ⇒ 任一道工序的量变 0，
#   其后所有工序编号全体前移，而依赖边按 id 引用 ⇒ **依赖边全断**。
#   修法：`assign_step_numbers` 在**完整工序清单**（含将被过滤掉的）上编号；
#   过滤**只决定"进不进树"**，不参与编号。旧编号就此作废（域 4.4 代价已知）。

_L4_INDEX_CACHE = {}
_L3_ORDER_CACHE = {}


def _default_phases():
    """惰性取 `DEFAULT_PHASES`（模块级导入会与 wbs_phases→kb 的导入链成环的潜在风险点，
    统一走这里；返回的是**副本**的浅表，调用方只读）。"""
    from .wbs_phases import DEFAULT_PHASES
    return DEFAULT_PHASES


def div_no_of_phase(phase_name):
    """① 分部号 = 该分部名在 `DEFAULT_PHASES` 里的 1-based 位置。取不到 → None。"""
    for i, spec in enumerate(_default_phases(), 1):
        if spec.get("phase") == phase_name:
            return i
    return None


def candidate_work_types(phase_name):
    """该分部允许的 L3 工种键列表（= `DEFAULT_PHASES[].kb`，**就是 4.1b 的候选集硬约束**）。

    依赖方向是**单向**的：叶子 L4 的 `work_type_id` 必须 ∈ 本列表。这个列表同时给出
    ② 的取值（下标 +1 即 L3工种号），一举两得 —— 候选集与编号同源，不可能漂移。
    """
    for spec in _default_phases():
        if spec.get("phase") == phase_name:
            return list(spec.get("kb") or [])
    return []


def l3_index(phase_name, work_type_id):
    """② L3工种号 = `DEFAULT_PHASES[].kb` 列表内顺序（1 起）。不合法 → None。"""
    keys = candidate_work_types(phase_name)
    try:
        return keys.index(work_type_id) + 1
    except ValueError:
        return None


def resolve_l4_id(work_type_id, l4_name, phase_name=None):
    """按 `(L3键, L4中文名)` 从知识库 `L4_Activity_Dictionary` 反查 L4 编号。

    第 5 批把 `BASE_BEAT_CONFIGS` 里写死的 6 个「AI 命名」活动编号**全部删掉**，改成
    "写 L3 键 + L4 中文名，编号运行时从库里查"。好处有三：
      ① 代码里不再出现任何硬写死的活动编号（域 3.1 验收）；
      ② 编号永远与库一致（库改名 → 这里立刻查得到/查不到，不会悄悄用旧名）；
      ③ 查不到时**如实返回 None**（叶子就不挂 kb_activity_id），绝不静默编一个。
    返回 `activity_id` 或 `None`。
    """
    wid = str(work_type_id or "").strip()
    name = str(l4_name or "").strip()
    if not wid:
        return None
    key = (wid, name)
    if key in _L4_INDEX_CACHE:
        return _L4_INDEX_CACHE[key]
    try:
        from .. import kb as _kb
        acts = _kb.l4_for(wid) or []
    except Exception:
        acts = []
    found = None
    for a in acts:
        if str(a.get("activity_name") or "").strip() == name:
            found = a.get("activity_id")
            break
    if found is None and not name:
        found = acts[0].get("activity_id") if acts else None
    _L4_INDEX_CACHE[key] = found
    return found


def stamp_kb_activities(cfg):
    """把 `cycle` / `attach_measures` / `parallel_work` 里"L3键 + L4名"解析成 `kb_activity_id`。

    **不改传入的 cfg**：返回深拷贝。已显式带 `kb_activity_id` 的项原样保留（LLM 直接给了
    编号时以 LLM 为准 —— 那是"从候选里挑一个"，不是"自己编号"）。
    解析失败（库里没有该 L3 键/该 L4 名）→ 该项不挂 `kb_activity_id`，并记进
    `cfg["_l4_unresolved"]`（**绝不静默**：调用方会把它带进留痕）。
    """
    out = copy.deepcopy(cfg)
    unresolved = []
    for key in ("cycle", "attach_measures", "parallel_work"):
        items = out.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("kb_activity_id"):
                continue
            wid = it.get("work_type_id")
            aid = resolve_l4_id(wid, it.get("l4_name"), out.get("node_name"))
            if aid:
                it["kb_activity_id"] = aid
            elif wid:
                unresolved.append({"step": it.get("name"), "work_type_id": wid,
                                   "l4_name": it.get("l4_name")})
    if unresolved:
        out["_l4_unresolved"] = unresolved
    return out


def assign_step_numbers(phase_name, steps):
    """③ L4工序号 + ② L3工种号：**在完整工序清单上**编号（域 4.2 / 4.2a 的唯一实现）。

    `steps` = 该分部的**完整**工序顺序（`cycle + attach_measures`，**含量=0 的**）。
    就地给每个 step 打 `_l3_no`（该分部 kb 列表内序）与 `_l4_no`（**在所属 L3 内**从 1 重编，
    按 `steps` 的先后顺序 —— 连续性由"完整清单"保证）。

    ★ 关键：**过滤发生在编号之后**。调用方必须先 `assign_step_numbers(全部)`、
    再决定哪几道进树 —— 这样某道工序量变 0 时，其余工序的 `_l4_no` **逐位不变**。
    L3 键不在候选集里（违反 4.1b）→ 该步 `_l3_no = None`（调用方据此报违规），
    但仍参与编号，不影响其它工序。

    返回 `(l3_no, l4_no)` 的有序列表，与 `steps` 一一对应。
    """
    out = []
    counter = {}
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        wid = s.get("work_type_id")
        l3 = l3_index(phase_name, wid) if wid else None
        s["_l3_no"] = l3
        if l3 is None:
            s["_l4_no"] = None
            out.append((None, None))
            continue
        counter[l3] = counter.get(l3, 0) + 1
        s["_l4_no"] = counter[l3]
        out.append((l3, s["_l4_no"]))
    return out


def _step_kb_id(step):
    """取一道工序的 KB 编号：已挂的优先；没挂就按 `(L3键, L4名)` 现查（`resolve_l4_id` 有缓存）。

    为什么不直接用 `step["kb_activity_id"]`：`apply_l4_order` 是**公开函数**，
    调用方可能在 `stamp_kb_activities` 之前就调它（那样每道工序都"没挂编号"，
    `ignored` 会把整个清单误报成"库里没有"）。这里自己兜一次，函数自洽。
    """
    if not isinstance(step, dict):
        return ""
    aid = str(step.get("kb_activity_id") or "").strip()
    if aid:
        return aid
    return resolve_l4_id(step.get("work_type_id"), step.get("l4_name")) or ""


def apply_l4_order(cfg, l4_order):
    """域 3.2：用 LLM 给出的**有序 L4 清单**决定工序先后（③ L4工序号的唯一顺序来源）。

    `l4_order` 形状（由 `wbs_agent` 落库，见本批派工单）：
        [{"kb_activity_id": "REBAR_NEW_SLAB", "work_type_id": "rebar", ...}, ...]
    也接受纯字符串元素（直接给 activity_id）。**保序、去重**。

    规则（确定、可复现，全部可解释）：
      · 声明清单里有的工序 → 按 LLM 给的顺序排；
      · LLM **没列到**的工序 → 排在其后，保持它们之间的**声明相对顺序**
        （LLM 漏列既不改变这些工序的先后，也不改变任何编号 —— 编号在完整清单上编）；
      · **只重排，不新增**：LLM 列了但声明清单里没有的 L4 **不会被凭空造出来**
        （没有量算器/定额输入，造出来只会挂 0 量、无定额的假工序）；
        这一类别记进 `cfg["_l4_order"]["ignored"]`，**不静默**；
      · `l4_order` 为空/非法 → 原样返回（退回"声明顺序即施工先后"）。

    就地改 `cycle` / `attach_measures` / `parallel_work` 的**列表内顺序**并返回 cfg。
    注意：`cycle` 与 `attach_measures` 各自内部排序（全局顺序仍是 cycle→attach），
    与既有口径一致 —— 措施项本来就在主循环工序之后。
    """
    ids = []
    for it in l4_order or []:
        aid = it if isinstance(it, str) else (it or {}).get("kb_activity_id")
        aid = str(aid or "").strip()
        if aid and aid not in ids:
            ids.append(aid)
    if not ids:
        return cfg
    rank = {a: i for i, a in enumerate(ids)}
    declared = set()
    for key in ("cycle", "attach_measures", "parallel_work"):
        for it in cfg.get(key) or []:
            aid = _step_kb_id(it)
            if aid:
                declared.add(aid)
    ignored = [a for a in ids if a not in declared]
    for key in ("cycle", "attach_measures", "parallel_work"):
        items = cfg.get(key)
        if not isinstance(items, list) or not items:
            continue
        decorated = []
        for i, it in enumerate(items):
            aid = _step_kb_id(it)
            decorated.append((rank.get(aid, len(ids) + i), i, it))
        decorated.sort(key=lambda t: (t[0], t[1]))
        cfg[key] = [t[2] for t in decorated]
    cfg["_l4_order"] = {"source": "llm", "order": list(ids), "ignored": ignored}
    return cfg


def validate_l4_candidates(phase_name, leaves):
    """4.1b 硬约束：每个叶子的 L4 所属 L3 必须 ∈ 该分部的候选集。返回违规描述列表。

    空列表 = 合规。**这条是"验收判据 5"的代码化**，放在展开处调用，
    而不是只写在文档里靠人记得扫。
    """
    allowed = candidate_work_types(phase_name)
    if not allowed:
        return []
    bad, seen = [], set()
    for leaf in leaves or []:
        wid = leaf.get("l3_work_type_id")
        if not wid or wid in allowed or wid in seen:
            continue
        seen.add(wid)
        bad.append({"phase": phase_name, "work_type_id": wid,
                    "allowed": list(allowed), "leaf_id": leaf.get("id")})
    return bad


BASE_BEAT_CONFIGS = {
    "地下室结构": {
        "node_id": "4", "node_name": "地下室结构", "org_type": "layer",
        # 保持现状：底板 / 墙柱 / 顶板分层浇筑 → 4 段 × 0.5 层，∑=2 层覆盖全部（floors_locked）
        "zones": ["Ⅰ区", "Ⅱ区"], "floors": 2, "floors_locked": True,
        "segments": 4, "floors_per_segment": 0.5,
        "cycle": [
            # 参数缺失时的兜底值 = BASEMENT_BASELINE_*（推导见文件上方「【1】兜底基线值」）：
            # 55 t / 4750 ㎡ / 360 m³ 每层，即地上标准层（22 t / 1900 ㎡ / 180 m³）的 2~2.5 倍。
            # 旧值 2100 / 18000 / 8200 是「整栋/整个地下室总量」误当「每层量」，差 10~100 倍。
            {"name": "钢筋绑扎", "unit": "t",  "qty_per_floor": BASEMENT_BASELINE_REBAR_T, "work_type": "钢筋工程", "resource": "钢筋工",
             "work_type_id": "rebar", "l4_name": "基础钢筋"},
            {"name": "模板安装", "unit": "m²", "qty_per_floor": BASEMENT_BASELINE_FORMWORK_M2, "work_type": "模板工程", "resource": "模板工",
             "work_type_id": "formwork", "l4_name": "基础模板"},
            {"name": "混凝土浇筑", "unit": "m³", "qty_per_floor": BASEMENT_BASELINE_CONCRETE_M3, "work_type": "混凝土工程", "resource": "混凝土工",
             "work_type_id": "concrete", "l4_name": "基础浇筑"},
        ],
        "lead_in": {"from_node": "3"},
    },
    "地上主体结构": {
        "node_id": "5", "node_name": "地上主体结构", "org_type": "layer",
        # 一层一段：38 层 = 38 段 × 1 层（段数由层数推导，不写死）。
        # 旧的 8 段 × 5 层会产生「1-5层钢筋→1-5层模板」，实际做不到：模板未支、上层无作业面。
        "zones": ["Ⅰ区", "Ⅱ区"], "floors": 38, "segments": 38, "floors_per_segment": 1,
        # A7（2026-09-21 裁定「移除预制相关内容」）：原第 3 道工序「叠合板吊装」
        # （预制叠合板，unit=m²，resource=吊装工）**已删除**。它写死 800 m²/层、
        # 既无 `kb_activity_id`、又忽略结构类型（现浇项目里也照样铺出来），是"叠合板
        # bug"的根因。本项目按现浇口径编排：钢筋绑扎 → 铝模安装 → 混凝土浇筑 → 爬架提升。
        "cycle": [
            {"name": "钢筋绑扎", "unit": "t",  "qty_per_floor": 22,  "work_type": "钢筋工程", "resource": "钢筋工",
             "work_type_id": "rebar", "l4_name": "板钢筋"},     # 单层钢筋的聚合量，取占比最大的板钢筋为代表
            {"name": "铝模安装", "unit": "m²", "qty_per_floor": 1900, "work_type": "模板工程", "resource": "模板工",
             "work_type_id": "formwork", "l4_name": "其他模板"},     # 铝模无专属活动，归入"其他模板"
            {"name": "混凝土浇筑", "unit": "m³", "qty_per_floor": 180, "work_type": "混凝土工程", "resource": "混凝土工",
             "work_type_id": "concrete", "l4_name": "板浇筑"},
        ],
        "attach_measures": [
            # 单位必须是 m²：`_calc_scaffold_lift` 算出来的是**外架面积**
            # （单栋标准层面积 × 0.6）。原来写 "项" 会让 KB 的 SCAFF0004
            # （工日/m²）被判"单位不一致"，38 条措施项的定额白白浪费。
            {"name": "爬架提升", "unit": "m²", "after": "混凝土浇筑", "work_type": "脚手架工程", "resource": "架子工",
             "duration_days": 3, "work_type_id": "scaffolding", "l4_name": "整体提升架搭拆"},
        ],
        "lead_in": {"from_node": "4"},
    },
    "二次结构与砌体": {
        "node_id": "6", "node_name": "二次结构与砌体", "org_type": "layer",
        # 一层一段：二次结构随主体逐层插入，同样不允许跨层成组（38 层 = 38 段 × 1 层）
        "zones": ["Ⅰ区", "Ⅱ区"], "floors": 38, "segments": 38, "floors_per_segment": 1,
        "cycle": [
            # 第 7 批（2026-09-21，用户已认可修法 C）：**这里是 ALC 绑错的真正源头**。
            # 原写 `"l4_name": "砌块墙"` ⇒ `resolve_l4_id("masonry","砌块墙")` =
            # `LDT724_砌块墙`（分母 **m³**，0.887 工日/m³），而本工序按 **m²** 计量
            # （`_calc_alc` 返回的就是 m²）⇒ `check_unit_pair` 判"单位不一致且不可换算"
            # ⇒ 定额丢弃、工期退回 WBS 3 天/层（实测 11 条 6.1.1.1.1~.11 全 unusable，
            # 见 `resource.py:102` 与 `norm_bind.py:2809` 两处留痕）。
            # 而 masonry 里有**单位一致**的 `MASON_ALC_PANEL`「ALC墙板安装」（m²，
            # `L4_Norm_Default` 0.095 工日/m²，default_crew 12，measure_scope 墙板安装面积）
            # —— 名字逐字相同，只是原先没被写上。改过来即命中：
            #   1145 m² × 0.095 工日/m² ≈ 109 工日/层 ≈ 9 天/层（12 人）。
            # ⚠️ 不引入新数据源、不编系数：活动与定额行都已在 kb.db 里（`labor_norms`
            # 空、走 `_labor_candidates` 的 `L4_Norm_Default` 回退，契约 §5-WS3 认可）。
            {"name": "ALC墙板安装", "unit": "m²", "qty_per_floor": 800, "work_type": "砌筑工程", "resource": "瓦工",
             "work_type_id": "masonry", "l4_name": "ALC墙板安装"},
            # 4.1b（本批修正）：原来挂 `CONC_NEW_COLUMN`（柱浇筑，work_type_id=concrete），
            # 而本分部的候选集只有 masonry ⇒ **38 条叶子违反硬约束**（实测缺陷）。
            # 二次结构的柱是**构造柱**，属砌筑工程：改挂 masonry 的「方柱-混水」。
            {"name": "构造柱浇筑", "unit": "m³", "qty_per_floor": 32, "work_type": "砌筑工程", "resource": "瓦工",
             "work_type_id": "masonry", "l4_name": "方柱-混水"},
            {"name": "砌块墙", "unit": "m³", "qty_per_floor": 85, "work_type": "砌筑工程", "resource": "瓦工",
             "work_type_id": "masonry", "l4_name": "砌块墙"},
            {"name": "勾缝", "unit": "m²", "qty_per_floor": 800, "work_type": "砌筑工程", "resource": "瓦工",
             "work_type_id": "masonry", "l4_name": "砌块墙勾缝"},
        ],
        "lead_in": {"from_node": "5", "floors_ahead": 3},
    },
    "装饰装修": {
        "node_id": "8", "node_name": "装饰装修", "org_type": "layer",
        # 3 层一组（与结构类的一层一段不同）：装饰装修是连续上移分部——同一空间可自上而下
        # 连续推进，班组按 3 层一组流水比一层一段更贴近现场排班（38 层 → 13 段：12×3+2）。
        "zones": ["Ⅰ区", "Ⅱ区"], "floors": 38, "segments": 13, "floors_per_segment": 3,
        "cycle": [
            {"name": "内墙抹灰", "unit": "m²", "qty_per_floor": 8600, "work_type": "抹灰工程", "resource": "抹灰工",
             "work_type_id": "wall_finish", "l4_name": "内墙抹灰"},
            {"name": "地面找平", "unit": "m²", "qty_per_floor": 7500, "work_type": "楼地面工程", "resource": "瓦工",
             "work_type_id": "flooring", "l4_name": "找平层"},
            {"name": "内墙涂料", "unit": "m²", "qty_per_floor": 8600, "work_type": "涂饰工程", "resource": "油漆工",
             "work_type_id": "painting", "l4_name": "内墙涂料"},
            {"name": "门窗安装", "unit": "m²", "qty_per_floor": 1020, "work_type": "门窗工程", "resource": "门窗工",
             "work_type_id": "door_window", "l4_name": "铝合金窗安装"},
        ],
        "parallel_work": [
            {"name": "外檐保温", "unit": "m²", "qty_total": 120000, "work_type": "保温工程", "resource": "防水工",
             "work_type_id": "insulation", "l4_name": "外墙外保温"},
            {"name": "外檐涂料", "unit": "m²", "qty_total": 120000, "work_type": "涂饰工程", "resource": "油漆工",
             "work_type_id": "painting", "l4_name": "外墙涂料"},
        ],
        "lead_in": {"from_node": "6", "floors_ahead": 3},
    },
}


# ================================================================================
# 单层量推算（v2.3）—— 按项目参数算「每层工程量」，只在参数缺失时才退回兜底基线值
# ================================================================================
# 背景（真实缺陷）：旧配置给 4 个按层重复的阶段写死 qty_per_floor，数字自相矛盾——
# 地下室只有 2 层却写钢筋 2100 t/层（合计 4200 t，占项目钢筋 33%），而 38 层主体才
# 22 t/层（合计 836 t，占 6.5%），"2 层的钢筋比 38 层还多 5 倍"，显然是把两个不同规模
# 项目的数字混在了一起；装饰装修写死内墙抹灰 8600 ㎡/层，而主体铝模才 1900 ㎡/层
# （抹灰是楼面面积的 4.5 倍，偏高）。用真实定额算工期时，地下室钢筋绑扎单条 330 天、
# 总工期飙到 3738 天，完全不可用。
#
# 修正：单层量 = 项目总量 × 部位占比 ÷ 该部位层数 ÷ 分区数。
# **2026-09-21 更新（B3/B4/B5）**：上面这条"部位占比"曾是本文件的 CONCRETE_RATIO /
# REBAR_RATIO 常量（按**施工阶段**分部位），现已被 `Component_Ratio`（按**结构类型 ×
# 工种 L3** 分构件，组内 ∑=100，知识库表）取代 —— 见文件中部「部位分配比例：已整体退役」。
# 「÷该部位层数÷分区数」也一并被 B4 取代（改为按**层面积 / 段面积**加权）。
# **参数缺失绝不猜**：某个量缺了它需要的输入参数，就退回**按工程单位率推导的兜底基线值**
# （见上方「【1】参数缺失时的兜底基线值」），而不是保留随手写死的数字。
#
# ⚠️ 第二处缺陷（本次修正）：原来「不猜」= 保留的写死值本身就不可用（地下室 2100 t/层）。
# 现在兜底值改成推导值，并且再加一道量级自检 `SUSPECT_*`（见下方【2】）：
# 单位面积指标超物理上限就**如实标记、原样保留**，绝不静默改数。

# ---- 部位分配比例：**已整体退役**（B3/B4/B5 接线，2026-09-21）----
# 用户裁定（`docs/修改项总清单_20260921.md:272` 第 15 条）：
#   「肯定是新表做唯一真源啊…旧的方法不采纳。如果让你做了又不用，我干嘛要做？」
# 于是本文件原有的三张**按施工阶段**的比例表被删除，不再参与任何计算：
#   · `CONCRETE_RATIO`          —— 地下室 0.25 / 主体 0.60 / 二次 0.05 / 装饰 0.10
#                                  （原注释自承四者之和 = 95%，本就不闭合）
#   · `REBAR_RATIO`             —— 地下室 0.10 / 主体 0.80 / 二次 0.10 / 装饰 0.0
#   · `SECONDARY_CONCRETE_RATIO`—— = CONCRETE_RATIO["二次结构与砌体"] / 2.0
# 取而代之的是**唯一真源** `Component_Ratio`（知识库表，分组 = 结构类型 × 工种(L3)，
# 组内 ∑ = 100），由 `pipeline/ratio_scope.py` 读取，链路：
#   ① L4 总量 = params["total_<工种>"] × ratio_percent ÷ 100
#   ② L4 层量 = L4 总量 × (该层面积 ÷ Σ各层面积)
#   ③ L4 段量 = L4 层量 × (该段面积 ÷ 该层面积)
# 该表的两套口径是**正交的**（阶段 vs 结构×工种），不可能同时成立 —— 保留旧的等于
# 把同一个总量按两套比例各摊一遍（X-1）。所以是**删除**，不是"并存"。

# ---- 面积类系数（工程经验值，可调）----
FORMWORK_AREA_FACTOR = 2.5      # 模板接触面积 ≈ 2~3 倍建筑面积，取 2.5（铝模同口径）
ALC_AREA_FACTOR = 1.8           # 内隔墙（ALC 墙板）面积 ≈ 建筑面积 × 1.8
BLOCK_WALL_THICKNESS = 0.2      # 内隔墙墙厚 0.2 m：砌块墙（m³）= ALC 面积（㎡）× 0.2
PLASTER_AREA_FACTOR = 2.2       # 内墙抹灰/涂料展开面积 ≈ 建筑面积 × 2.2
WINDOW_AREA_FACTOR = 0.15       # 窗地比 0.15：门窗面积 = 建筑面积 × 0.15
FACADE_AREA_FACTOR = 0.6        # 外檐（保温/涂料/外架）面积 = 建筑面积 × 0.6


def _fmt_num(v):
    """公式展示用数字：整数不带小数点，否则保留 2 位（1.8 → "1.8"，0.25 → "0.25"）。"""
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return str(round(f, 2))


def _param_num(params, key):
    """取正的项目参数；缺失 / 空 / 非数 / <=0 → None（**不猜，交给基线兜底**）。"""
    if not isinstance(params, dict):
        return None
    v = params.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f


#: 占比表拆分的**唯一来源标记**（`_qty_source` 的第四态）。
#: 「单一真源」要求：只要工序的量来自 `Component_Ratio`，来源就必须是这一态，
#: 而不是被合并进「参数推算」（后者指"按项目参数 × 部位系数"的旧口径）。
SOURCE_RATIO = "占比表拆分"


# ================================================================================
# 【2】单层量级自检（物理上限）——走「基线默认/混合」的叶子才查；命中**只标记不改数**
# ================================================================================
# 为什么要有这一道：参数缺失时保留的基线值**可能本来就是错的**（旧地下室 2100 t/层）。
# 底座（v2.3）只标「来源=基线默认」，但「基线默认」四个字不表达"这个数量级是不是人话"，
# 于是 1050 t 钢筋被一路铺成叶子、组织层报「需要 39 个作业面」。所以按**单位面积指标**
# 再拦一道：把「每层量 ÷ 单栋标准层面积」和物理上限比（面积取不到就用本文件的
# 典型标准层 1900 ㎡，理由见 `_calc_suspect_reason`）。
#
# 三条阈值都取"物理上不可能"的量级（而不是"常见的/规范的"量级）——**宁可漏报，不可误报**：
# 参数齐全路径的推算结果绝不能因为阈值取紧而被误标。判定口径见 `_calc_suspect_reason`。
SUSPECT_MAX_REBAR_PER_M2 = 0.15      # t/㎡：150 kg/㎡ 建筑面积。
#   依据：含钢量最大的部位是筏板/厚底板，通常 100~150 kg/㎡；住宅地上标准层仅
#   10~20 kg/㎡，地下室底板+墙柱也就 25~30 kg/㎡。取 150 kg/㎡ 留足余量：
#   实测旧基线 2100 t/层 ÷ 1900 ㎡ ≈ 1105 kg/㎡ = 7.4 倍上限 → 必然命中。
SUSPECT_MAX_CONCRETE_PER_M2 = 2.0    # m³/㎡：2 m³/㎡ 建筑面积。
#   依据：常规结构折合厚度 0.15~0.4 m/㎡，很厚的筏板也就 ~1.0 m³/㎡。取 2.0 意味着
#   "平均 2 米厚的实心楼板"——物理上不可能。实测旧基线 8200 m³/层 ÷ 1900 ㎡ ≈ 4.3 m³/㎡ 命中。
SUSPECT_MAX_FORMWORK_PER_M2 = 4.0    # ㎡/㎡：4 倍建筑面积的模板接触面积。
#   依据：本文件的模板接触面积系数取 2.5（FORMWORK_AREA_FACTOR，模板 ≈ 2~3 倍建筑面积），
#   地下室墙多也不会超 4 倍。实测旧基线 18000 ㎡/层 ÷ 1900 ㎡ ≈ 9.5 倍 → 命中。
SUSPECT_STEP_METRIC = {
    # 工序名 → (单位面积指标名, 物理上限, 单位, 中文口径)
    # 单位串会出现在 `_qty_suspect_reason`（**产物**）里，一律写 `m²`（G5：产物清零 U+33A1）。
    "钢筋绑扎": ("rebar", SUSPECT_MAX_REBAR_PER_M2, "t/m²", "每层钢筋"),
    "混凝土浇筑": ("concrete", SUSPECT_MAX_CONCRETE_PER_M2, "m³/m²", "每层混凝土"),
    "模板安装": ("formwork", SUSPECT_MAX_FORMWORK_PER_M2, "m²/m²", "每层模板"),
    "铝模安装": ("formwork", SUSPECT_MAX_FORMWORK_PER_M2, "m²/m²", "每层模板"),
}
# 各工序的**文档化基线量**（取自 `BASE_BEAT_CONFIGS`，与「【1】兜底基线值」同源），
# 用于「宽容闸」：量不超过 基线 × SUSPECT_BASELINE_TOLERANCE 时视为文档化良好值，不判。
# 用一句话概括取值口径：**配置里允许出现的最大良好值**（地下室兜底 > 地上标准层，
# 所以三项都取地下室兜底值）。这样兜底基线全部落在闸门内不误报，而任何"量级错误值"
# （旧值 2100/8200/18000）都远在闸门外、继续走物理上限判定。
SUSPECT_DOC_BASELINE = {
    "钢筋绑扎": 55.0,      # 地下室兜底 55 t vs 主体 22 t → 取 55（配置内最大良好值）
    "混凝土浇筑": 360.0,   # 地下室兜底 360 m³ vs 主体 180 m³ → 取 360
    "模板安装": 4750.0,    # 地下室兜底 4750 ㎡ vs 主体 1900 ㎡ → 取 4750
    "铝模安装": 4750.0,
}
# 「宽容闸」倍数：文档化基线本身是推导出来的、不需要自证，故给它 1.5 倍余量。
# 为什么 1.5 仍然安全：旧写死值 2100 / 8200 / 18000 分别是基线 55 / 360 / 4750 的
# 38 / 23 / 3.8 倍，全部在闸门外 → 仍会被物理上限判定拦下（实测三条都命中）。
# 真正会从闸门缝隙里溜走的，是"跑到兜底值 1.5 倍以内、但相对建筑面积已经不合理"的量；
# 这个缝隙是有意留的：兜底值本身不需要自证，而把闸门收紧会让"正常配置"被误标。
SUSPECT_BASELINE_TOLERANCE = 1.5


def _over_doc_baseline(step):
    """该工序量是否已越过「宽容闸」（= 文档化基线 × SUSPECT_BASELINE_TOLERANCE）。

    False → 视为文档化的兜底值/良好值，自检放行（见 `_calc_suspect_reason` 的理由）。
    未被登记的工序 → True（不因此放行，交由 `_calc_suspect_reason` 按指标表决定）。
    """
    name = step.get("name") if isinstance(step, dict) else None
    doc_base = SUSPECT_DOC_BASELINE.get(name)
    if not doc_base:
        return True
    try:
        qty = float(step.get("qty_per_floor") or 0)
    except (TypeError, ValueError):
        return True
    return qty > float(doc_base) * SUSPECT_BASELINE_TOLERANCE


def _strip_area_denominator(unit):
    """把单位串里的"每平米"分母去掉，只留分子（`t/m²` → `t`、`m³/㎡` → `m³`）。

    **输入侧宽容**：`㎡`（U+33A1）与 `m²` / `m2` 是同一量纲的三种写法，声明方怎么写都认
    （与 `quantity`/`kb_units` 的归一口径一致）。**输出侧**一律用 `m²`
    （方案 §6 验收 #5：产物里不得残留 U+33A1）。
    """
    text = str(unit or "")
    # 「㎡」用转义构造：源码里不留 U+33A1 字面量，免得它再被复制进产物串（G5）。
    for token in ("/\u33a1", "/m²", "/m2"):
        text = text.replace(token, "")
    return text.strip()


def _calc_suspect_reason(step, params=None):
    """单位面积指标超物理上限 → 返回中文原因；否则 None（**只判定，不改数**）。
    判定用**单栋标准层面积**做量纲归一（`_floor_area(params)` = 总建筑面积÷栋数÷层数）。
    取不到面积（没 total_area / floors）→ 退到本文件的口径锚点
    `STANDARD_FLOOR_FORMWORK_M2`（= 1900 ㎡，见「【1】兜底基线值」的适用前提）。

    ⚠️ **为什么面积缺失时也敢判**：因为阈值是按"单栋标准层"的绝对量级定的
    （0.15 t/㎡、2 m³/㎡、4 ㎡/㎡ 都远高于任何正常结构），所以拿 1900 ㎡ 这个
    "典型住宅标准层"当归一基数，误差不会超过一个数量级的量级门槛。若改成拿
    `面积 ÷ 分区数` 去判，一个标准层被切成 4 个区时基数只剩 475 ㎡，模板 2.5 倍
    这一**良好**比例会被算成 10 倍而误报——所以**故意不除以分区数**。

    ⚠️ **已知良好值不判**：兜底基线本身就是按"1900 ㎡ × 单位率"推导出来的，
    不需要再自证一遍。所以先设一道"宽容闸"：量不超过**本工序文档化基线 ×
    SUSPECT_BASELINE_TOLERANCE** 时，视为"文档化的兜底值/良好值"，直接跳过。
    闸门很宽（基线 ×1.5），而真正的量级错误值（旧值 2100 / 8200 / 18000）是基线的
    38 / 23 / 3.8 倍，仍在闸门外 → 照样会被物理上限拦下。
    """
    spec = SUSPECT_STEP_METRIC.get(step.get("name")) if isinstance(step, dict) else None
    if not spec:
        return None
    _, limit, unit, what = spec
    try:
        qty = float(step.get("qty_per_floor") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    # 宽容闸：不超过「本工序文档化基线 × 1.5」→ 视为文档化兜底值/良好值，放行
    if not _over_doc_baseline(step):
        return None
    # 量纲归一：单栋标准层面积（取不到 → 用本文件的典型标准层 1900 ㎡）
    area = _floor_area(params)
    basis = float(area) if area else float(STANDARD_FLOOR_FORMWORK_M2)
    per_m2 = qty / basis
    if per_m2 <= float(limit) * 1.0000001:       # 等于上限不算超
        return None
    raw_unit = _strip_area_denominator(step.get("unit") or "")
    return ("%s %s %s 相当于 %s %s/m²（按%sm²标准层折算），超出物理合理上限 %s %s/m²"
            % (what, _fmt_num(qty), raw_unit, _fmt_num(round(per_m2, 2)),
               _strip_area_denominator(unit), _fmt_num(round(basis, 0)),
               _fmt_num(limit), _strip_area_denominator(unit)))


def building_count_note(params):
    """单栋口径的中文说明（拼在公式前面，让用户看得见"为什么总量变小了"）。

    单栋 / 取不到栋数 → 返回 ""（不啰嗦）。
    """
    n = building_count(params)
    if n <= 1:
        return ""
    total = _param_num(params, "total_area")
    if total is None:
        return "全项目共 %d 栋，按单栋口径编制（各栋平行施工）" % n
    return ("全项目共 %d 栋（总建筑面积%sm²÷%d栋 = %sm²/栋），按单栋口径编制、各栋平行施工"
            % (n, _fmt_num(total), n, _fmt_num(total / n)))


def _with_build_note(formula, note):
    """把单栋口径说明拼到公式前；任一为空则原样返回。"""
    if not formula or not note:
        return formula
    return "%s；%s" % (note, formula)


def _ratio_status(params, step, phase_steps):
    """该工序在 `Component_Ratio`（唯一真源）里的处境。

    转发 `pipeline/ratio_scope.py:step_ratio_status`（判据的**唯一实现**，
    节拍配置层与 `layer_engine` 展开层共用同一函数，不许各写一套）。
    """
    from ..ratio_scope import step_ratio_status   # 惰性：破循环导入
    return step_ratio_status(params, step, phase_steps)


def _norm_formula(floor_area, factor, zones, unit, what):
    """面积类公式：「单栋标准层面积×系数÷分区数」，what 说明系数含义。"""
    return ("单栋标准层%sm²×%s（%s）÷%s区 = %s %s/层"
            % (_fmt_num(floor_area), _fmt_num(factor), what,
               _fmt_num(zones), _fmt_num(floor_area * factor / zones), unit))


# ---- 面积类工序的**统一基数**：单栋标准层面积 ----
# 为什么不用 `total_area ÷ 阶段层数`（旧口径，已废止）：
#   ① 地下室只有 2 层，拿"整栋楼的面积"去除以 2，单层量被放大 (总层数÷2) 倍
#      ——实测地下室模板因此要 286 天；
#   ② 每个阶段都把**整栋建筑面积**算一遍 → 模板等面积类工序被各阶段重复计入，
#      全楼模板量凭空翻倍，工期成倍拉长。
# 单栋标准层面积 = 总建筑面积 ÷ 栋数 ÷ 地上层数，是与阶段无关的**部位面积基数**：
#   地下室楼板 ≈ 塔楼标准层（可取等，地下室的放大/缩小留给用户改系数）。
def _floor_area(params):
    return standard_floor_area(params)


# ---- 各工序的计算器：返回 {"qty": 单层量, "formula": 中文公式}；参数缺失返回 None ----
# 签名统一为 fn(params, phases, zones)：phases 是"该阶段层数"，zones 是分区数。
#
# ⚠️ **2026-09-21 接线说明（B3/B4/B5）**：
#   · 混凝土 / 钢筋 / 构造柱 / 模板(有用户总量时) / 砌块墙(有用户总量时) 的**量**已不再由
#     本文件的「阶段占比 × 总量 ÷ 层数」产生，而是由**占比表**（`Component_Ratio`）产生：
#       ① L4 总量 = params["total_<工种>"] × ratio_percent ÷ 100
#       ②③ 层量 / 段量由 `layer_engine.expand_node` 调用
#           `ratio_scope.B4Distribution` 按**层面积 / 段面积**分解（真正的量在叶子上）。
#   · 本文件此处只负责**认领与标源**：命中占比表 → `SOURCE_RATIO` 并记 `_ratio_l4`；
#     工种有用户总量但表里缺该 L4 → 标 `_ratio_excluded`（量0出局，展开时不进树）；
#     工种没有用户总量 → 继续走 `STEP_SPECS` 的既有系数路径。
#   · `_calc_concrete` / `_calc_rebar` / `REBAR_RATIO` / `CONCRETE_RATIO` /
#     `SECONDARY_CONCRETE_RATIO` / `_floors_total_same_scope` **已整体删除**（旧表退役）。


def _calc_formwork(params, floors, zones):
    """模板（含铝模，只是材料不同）的**系数路径**。

    ⚠️ 用户给了 `total_formwork` 时**不再走这里**：那条路已由占比表接管
    （①总量 × ratio → ②层量 → ③段量，见 `ratio_scope`），本函数只负责
    「用户没给模板总量」时的面积系数推算（数字与接线前逐位相同）。
    """
    fa = _floor_area(params)
    if fa is None:
        return None
    return {"qty": fa * FORMWORK_AREA_FACTOR / zones,
            "formula": _norm_formula(fa, FORMWORK_AREA_FACTOR, zones, "m²",
                                     "模板接触面积系数")}


def _calc_alc(params, floors, zones):
    """ALC 墙板安装（㎡）：单栋标准层面积 × 内隔墙面积系数 ÷ 分区数。"""
    fa = _floor_area(params)
    if fa is None:
        return None
    return {"qty": fa * ALC_AREA_FACTOR / zones,
            "formula": _norm_formula(fa, ALC_AREA_FACTOR, zones, "m²",
                                     "内隔墙面积系数")}


def _block_wall_by_coef(params, floors, zones):
    """砌块墙的**系数路径**（勾缝也走这条，见 `_calc_joint_fill`）。

    `单栋标准层面积 × 内隔墙面积系数 × 墙厚 0.2 m ÷ 分区数`。
    ⚠️ `BLOCK_WALL_THICKNESS = 0.2` 是**硬编码厚度**（D5 口径下属"写死常量"，待审，
    不属本次范围）——要接定额厚度档位应走 `kb_units.assumed_context(...)`。
    """
    fa = _floor_area(params)
    if fa is None:
        return None
    qty = fa * ALC_AREA_FACTOR * BLOCK_WALL_THICKNESS / zones
    return {"qty": qty,
            "formula": ("单栋标准层%sm²×%s（内隔墙面积系数）×%s m（墙厚）÷%s区 = %s m³/层"
                        % (_fmt_num(fa), _fmt_num(ALC_AREA_FACTOR),
                           _fmt_num(BLOCK_WALL_THICKNESS), _fmt_num(zones), _fmt_num(qty)))}


def _calc_block_wall(params, floors, zones):
    """砌块墙（m³）的**系数路径**。

    ⚠️ 用户给了 `total_masonry` 时**不再走这里**：那条路已由占比表接管
    （`LDT724_砌块墙` 在 `frame_shear|masonry` 组内占 35.4% → ①总量 × 35.4% →
    ②层量 → ③段量）。本函数只负责「用户没给砌体总量」时的系数推算。
    同量纲说明保留：`masonry` 里墙体主项都是 m³，那 9 条 m² 是勾缝/地胎膜/ALC/阳台栏板
    之类**附属项**（`LDT724_砌块墙勾缝` 在 `Component_Ratio` 里根本没有行 →
    按「表里没有该 L4 → 量 0 出局」处理，**不**从砌体总量硬拆）。
    """
    return _block_wall_by_coef(params, floors, zones)


def _calc_joint_fill(params, floors, zones):
    """勾缝（m²）：**恒走系数路径**——与砌块墙同口径（同一道墙）。

    裁定 I1-3：勾缝 **不**从 `total_masonry` 拆分。它与 ALC 一样属于**附属项**
    （P2 实测 masonry 的 m² 类 L4），量纲与"砌体总量（m³）"不同；
    若将来要接，必须走 `kb_units.assumed_context(from_unit, to_unit, condition_text)`
    取定额行厚度档位，**不许硬编码厚度**。
    """
    return _block_wall_by_coef(params, floors, zones)


def _calc_plaster(params, floors, zones):
    """内墙抹灰（水泥砂浆）：单栋标准层面积 × 内墙展开系数 ÷ 分区数。"""
    fa = _floor_area(params)
    if fa is None:
        return None
    return {"qty": fa * PLASTER_AREA_FACTOR / zones,
            "formula": _norm_formula(fa, PLASTER_AREA_FACTOR, zones, "m²",
                                     "内墙展开面积系数")}


def _calc_paint(params, floors, zones):
    """内墙涂料：与内墙抹灰同口径（同一展开面积）。"""
    got = _calc_plaster(params, floors, zones)
    if got is None:
        return None
    got["formula"] = got["formula"].replace("内墙展开面积系数", "内墙展开面积系数，涂料同抹灰口径")
    return got


def _calc_floor_screed(params, floors, zones):
    """地面找平：单栋标准层面积 ÷ 分区数（即楼面面积，不再乘系数）。"""
    fa = _floor_area(params)
    if fa is None:
        return None
    qty = fa / zones
    return {"qty": qty,
            "formula": ("单栋标准层%sm²÷%s区 = %s m²/层（即楼面面积）"
                        % (_fmt_num(fa), _fmt_num(zones), _fmt_num(qty)))}


def _calc_doors_windows(params, floors, zones):
    """门窗安装：单栋标准层面积 × 窗地比 0.15 ÷ 分区数。"""
    fa = _floor_area(params)
    if fa is None:
        return None
    return {"qty": fa * WINDOW_AREA_FACTOR / zones,
            "formula": _norm_formula(fa, WINDOW_AREA_FACTOR, zones, "m²", "窗地比")}


def _calc_scaffold_lift(params, floors, zones):
    """爬架提升（措施）：随层提升的外架面积 = 单栋标准层面积 × 0.6 ÷ 分区数。"""
    fa = _floor_area(params)
    if fa is None:
        return None
    return {"qty": fa * FACADE_AREA_FACTOR / zones,
            "formula": _norm_formula(fa, FACADE_AREA_FACTOR, zones, "m²",
                                     "外墙面积系数")}


# ---- 工序 → 计算器 规格表（按部位分组；未列入的工序一律保留基线写死值）----
# 每项 = (工序名, 计算器)；计算器签名统一为 fn(params, floors, zones) -> {"qty", "formula"} | None
#
# ⚠️ 表里**故意没有**「钢筋绑扎 / 混凝土浇筑 / 构造柱浇筑」：
#   这三道的量由**占比表**（`Component_Ratio`）产生（见 `_apply_step` 的 `SOURCE_RATIO`
#   分支），不再有任何"阶段占比"系数可写。工种没给用户总量时才退回基线兜底值
#   （`BASE_BEAT_CONFIGS` 里的 `qty_per_floor`），并标 `SOURCE_BASE`。
#   「模板安装 / 铝模安装 / 砌块墙」在这里保留的是**系数路径**（用户没给总量时用）。
STEP_SPECS = {
    "地下室结构": [
        ("模板安装", _calc_formwork),
    ],
    "地上主体结构": [
        ("铝模安装", _calc_formwork),     # 铝模就是模板，只是材料不同 → 同模板口径
    ],
    "二次结构与砌体": [
        ("ALC墙板安装", _calc_alc),
        ("砌块墙", _calc_block_wall),
        ("勾缝", _calc_joint_fill),
    ],
    "装饰装修": [
        ("内墙抹灰", _calc_plaster),
        ("地面找平", _calc_floor_screed),
        ("内墙涂料", _calc_paint),
        ("门窗安装", _calc_doors_windows),
    ],
}

# 措施项（attach_measures）规格：工序名 → 计算器
MEASURE_SPECS = {
    "爬架提升": _calc_scaffold_lift,
}

# 外檐平行专项（parallel_work）：全楼总量口径（不除以层数/分区数）
PARALLEL_SPECS = {
    "外檐保温": (FACADE_AREA_FACTOR, "外墙面积系数"),
    "外檐涂料": (FACADE_AREA_FACTOR, "外墙面积系数"),
}

SOURCE_PARAM = "参数推算"
SOURCE_BASE = "基线默认"
SOURCE_MIXED = "混合"


def _phase_floors_and_zones(cfg, params):
    """该部位的有效层数与分区数（复用 layer_engine 的既有口径，**不重复实现**）。

    - 地下室 floors_locked=True → 恒 2 层；主体/二次/装修 → 项目参数 floors，
      缺省用配置层数（38），并在 detail 里标注"层数取配置默认"。
    - 分区数用 layer_engine._eff_zones（惰性导入：beat_configs 被 layer_engine 导入，
      模块级互相导入会成环）。
    """
    from .. import layer_engine as _LE          # 惰性导入，破循环依赖
    zones = _LE._eff_zones(cfg, params)
    floors = _LE._eff_floors(cfg, params)
    if cfg.get("floors_locked"):
        return floors, zones, False
    if _param_num(params, "floors") is not None:
        return floors, zones, False
    return floors, zones, True                  # 层数取配置默认


def _spec_of(phase, name):
    """按 (阶段, 工序名) 取计算器；未登记 → None（该量保留基线，不硬凑）。"""
    for step_name, fn in STEP_SPECS.get(phase) or []:
        if step_name == name:
            return fn
    return None


def _apply_step(phase, step, floors, zones, params, floors_defaulted, phase_steps=()):
    """算一道工序的单层量。返回 `(新 step, 来源, 公式)`。

    三条互斥路径（**顺序即优先级**）：
      1. **占比表**（`SOURCE_RATIO`）：L4 在 `Component_Ratio` 里有 > 容差的占比
         → 标 `_ratio_l4`；`qty_per_floor` 只写一个"阶段平均层量"供展示/自检，
         **真量**由 `layer_engine.expand_node` 用 B4 公式（层面积→段面积）逐叶算。
      2. **量0出局**：工种有用户总量但表里缺该 L4 / 占比≈0 → 标 `_ratio_excluded`，
         叶子上**不进树**（展开时过滤），**绝不退回旧阶段比例表补数**。
      3. **既有系数/基线**：工种没有用户总量（占比表无发言权）→ 走 `STEP_SPECS`；
         算不出 → 保留基线兜底值并标 `SOURCE_BASE`。
    """
    st = _ratio_status(params, step, phase_steps)
    if st["status"] == "missing":
        new_step = dict(step)
        new_step["_ratio_excluded"] = {
            "activity_id": st["activity_id"],
            "work_type_id": st["work_type_id"],
            "reason": st["reason"],
        }
        if floors_defaulted:
            pass
        return new_step, SOURCE_BASE, None
    if st["status"] == "ratio":
        got = _ratio_floor_average(params, st, step, floors, zones)
        if got:
            new_step = dict(step)
            new_step["qty_per_floor"] = round(float(got["qty"]), 2)
            new_step["_ratio_l4"] = {
                "activity_id": st["activity_id"],
                "work_type_id": st["work_type_id"],
                "ratio_percent": (st.get("info") or {}).get("ratio_percent"),
                "l4_total": (st.get("info") or {}).get("quantity"),
            }
            formula = got["formula"]
            if floors_defaulted:
                formula += "（层数取配置默认 %s 层）" % _fmt_num(floors)
            return new_step, SOURCE_RATIO, formula
    fn = _spec_of(phase, step.get("name"))
    if fn is not None:
        got = fn(params, floors, zones)
        if got:
            new_step = dict(step)
            new_step["qty_per_floor"] = round(float(got["qty"]), 2)
            formula = got["formula"]
            if floors_defaulted:
                formula += "（层数取配置默认 %s 层）" % _fmt_num(floors)
            return new_step, SOURCE_PARAM, formula
    return dict(step), SOURCE_BASE, None


def _ratio_floor_average(params, status, step, floors, zones):
    """占比表路径的「阶段平均单层量」——只供展示与量级自检。

    真正的量在 `layer_engine.expand_node`（B4：层面积 → 段面积）。
    这里给的是 `L4 总量 ÷ 该阶段各层面积合计 × 单栋标准层面积 ÷ 分区数`。

    ⚠️ **算不出标准层面积时返回 None**：B4 的两步都需要层面积，
    没有它就等于"命中占比表却分解不了"。此时**不允许**标成占比表来源
    （那会让叶子挂着"占比表拆分"的来源、量却来自别处 —— 虚假溯源）。
    调用方会自然落到既有系数/基线路径，并如实标 `基线默认`。
    """
    from .. import segment_plan                    # 惰性：破循环导入
    info = status.get("info") or {}
    total = float(info.get("quantity") or 0.0)
    area = segment_plan.standard_floor_area_from_params(params)
    if total <= 0 or not area:
        return None
    span = float(area) * max(1.0, float(floors or 1))
    per_floor = total * float(area) / span if span > 0 else total / max(1.0, float(floors or 1))
    qty = per_floor / max(1, zones)
    unit = step.get("unit") or info.get("unit") or ""
    formula = ("占比表拆分：%s×%s%%（Component_Ratio %s|%s，%s）= %s%s（全楼）"
               "；按各层面积分解到层、再按段面积分解到段（见叶子的逐段算式）"
               % (_param_key_of(status), _fmt_num(info.get("ratio_percent")),
                  info.get("structure_type_id"), status["activity_id"],
                  _review_bits(info), _fmt_num(total), unit))
    return {"qty": qty, "formula": formula}


def _param_key_of(status):
    """该 L4 所属工种的用户总量参数键（展示用）。"""
    from ..ratio_scope import GROUP_TOTAL_PARAMS
    return GROUP_TOTAL_PARAMS.get(status.get("work_type_id"), "total_*")


def _review_bits(info):
    conf = str(info.get("confidence") or "").strip()
    state = str(info.get("review_state") or "").strip()
    bits = []
    if conf:
        bits.append("confidence=%s" % conf)
    if state:
        bits.append("review_state=%s" % state)
    return ",".join(bits) or "confidence=-,review_state=-"


def derive_beat_quantities(cfg, params):
    """按项目参数推算该阶段的单层工程量（v2.3）。

    返回 ``(新 cfg, 说明)``：
      - 新 cfg 的 ``cycle`` / ``attach_measures`` / ``parallel_work`` 里的量已按项目换算；
        **原 cfg 不被改动**（内部深拷贝）。
      - 说明 = ``{"source": "参数推算" | "基线默认" | "混合",
                  "detail": {工序名: {"qty_per_floor": 值, "source": "参数推算"|"基线默认",
                                      "formula": "人话公式，如 total_concrete×25%÷2层÷2区"}}}``

    规则（每条量都必须能说出怎么来的——这是本产品的核心价值「可溯源」）：
      1. 混凝土（m³）：单栋混凝土总量 × 部位占比 ÷ 部位层数 ÷ 分区数
      1. **占比表类（钢筋 / 混凝土 / 构造柱 / 有用户总量的模板与砌块墙）**：
         `Component_Ratio`（唯一真源）→ ①`L4 总量 = 单栋 total_<工种> × ratio%`，
         ②③再由 `layer_engine.expand_node` 按**层面积 / 段面积**分解到「层 × 平面段」。
         本函数只产出标源与展示用的"阶段平均层量"（`SOURCE_RATIO`），真量在叶子上。
         工种有用户总量但表里缺该 L4 → 标 `_ratio_excluded`（量0出局，不进树）。
      3. 模板（m²）    ：用户**没给** `total_formwork` 时走系数路径 ——
         **单栋标准层面积** × 2.5（模板接触系数）÷ 分区数（铝模同口径）。
      4. ALC 墙板（m²）：单栋标准层面积 × 1.8（内隔墙系数）÷ 分区数；
         砌块墙（m³）= 上述面积 × 墙厚 0.2 m（用户**没给** `total_masonry` 时的系数路径）。
         勾缝（m²）与 ALC（m²）都是**附属项**，**不接**砌体总量，恒走系数路径（裁定 I1-3）；
         砌体总量存在时 `LDT724_砌块墙勾缝` 因表里无行 → 量0出局。
      5. 装饰（㎡）    ：内墙抹灰/涂料 = 单栋标准层面积 × 2.2 ÷ 分区数；
         地面找平 = 单栋标准层面积 ÷ 分区数（楼面面积）；
         门窗 = 单栋标准层面积 × 0.15 ÷ 分区数
      6. 外檐（全楼平行，单栋总量）：单栋建筑面积 × 0.6（外墙面积系数），不除以层数/分区数
      7. 措施项：爬架提升 = 单栋标准层面积 × 0.6 ÷ 分区数；勾缝与砌块墙同口径

    ⚠️ 面积类工序 3~7 的基数必须是**单栋标准层面积**（= 总建筑面积 ÷ 栋数 ÷ 地上层数），
    不是 `总建筑面积 ÷ 该阶段层数`。旧口径有两个致命错：① 地下室只有 2 层，拿整栋面积
    除以 2，单层量被放大 (总层数÷2) 倍（实测地下室模板要 286 天）；② 每个阶段都把整栋
    建筑面积算一遍，模板等面积类工序被各阶段重复计入，全楼模板量凭空翻倍。
    ⚠️ 旧口径里"体积类工序要除以该阶段层数"的说法**已废止**：占比表驱动的那几道
    不再按阶段层数均摊，改按**层面积**加权（B4 公式一）。
      8. **参数缺失绝不猜**：某工序缺它需要的输入参数 → 保留基线兜底值并标「基线默认」
         （例：没有 total_rebar 时钢筋不会拿别的参数硬凑）。兜底值本身按工程单位率推导
         （见「【1】参数缺失时的兜底基线值」），不再是随手写死的数字。
      8b. **量级自检**：走「基线默认」的工序若单位面积指标超物理上限（见「【2】单层量级
          自检」），在 step 上加 ``_qty_suspect=True`` + ``_qty_suspect_reason``（中文原因）。
          **如实标记、原样保留**——可疑不等于改数。      9. **多栋项目按单栋口径**：`building_count`（栋数）>1 时，先用
         `per_building_params()` 把总量类参数（面积/混凝土/钢筋…）折算成单栋，
         再套上面 1~7 的公式。单层量因此是"一栋楼一层的量"，与工作面容量
         （每施工段多少人）口径一致；N 栋平行施工，单栋工期 ≈ 项目工期。
         公式前缀会写明「全项目共 N 栋（总建筑面积…÷N栋 = …㎡/栋）」。

    同时给 parallel_work 项挂 ``_qty_source`` / ``_qty_formula``，供 layer_engine 铺到叶子上；
    cycle 里被标记的 ``_qty_suspect`` / ``_qty_suspect_reason`` 同样由 layer_engine 透传。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    # 第 5 批（域 3.1）：先把"L3键 + L4名"补成 `kb_activity_id` 再做任何量化。
    # **必须在这里、而不是只在 layer_engine**：本函数是被直接调用的公开入口
    # （测试与展示都直接调它），少了这一步就会把本该走占比表的工序算成"基线默认"
    # —— 实测踩过：`test_full_params_ratio_table_drives_qty` 直接调本函数，
    # 钢筋绑扎的来源从「占比表拆分」掉成「基线默认」。
    out = stamp_kb_activities(cfg)
    phase = str(out.get("node_name") or out.get("phase") or "")
    floors, zones, floors_defaulted = _phase_floors_and_zones(out, params)
    zones_n = max(1, len(zones or []))
    # 多栋项目：所有"总量类"参数先折算成**单栋**口径，再套既有的
    # 「总量 ÷ 层数 ÷ 分区数」公式 → 单层量就是"一栋楼一层的量"，
    # 与工作面容量（每施工段多少人）口径一致。栋数 ≤1 时是同一对象，不影响行为。
    pb = per_building_params(params)
    build_note = building_count_note(params)

    detail = {}
    n_param = 0
    n_base = 0

    def _record(name, value, source, formula, suspect=False, suspect_reason=""):
        nonlocal n_param, n_base
        detail[name] = {"qty_per_floor": value, "source": source, "formula": formula or "",
                        "suspect": bool(suspect), "suspect_reason": suspect_reason or ""}
        # 「占比表拆分」与「参数推算」同属**按项目参数算出来的**（不是基线兜底），
        # 所以在来源计数里归并到同一档；两种来源在 detail/叶子上仍各自可辨。
        if source in (SOURCE_PARAM, SOURCE_RATIO):
            n_param += 1
        else:
            n_base += 1

    # ---- 主循环工序 + 挂靠措施 ----
    # `phase_steps` = 本阶段全部主循环工序：占比表的「一个 L4 只被一道工序认领」
    # 判据需要看到同阶段的兄弟工序（防两道工序共用同一 L4 被算两遍）。
    phase_steps = [s for s in (out.get("cycle") or []) if isinstance(s, dict)]
    new_cycle = []
    for step in (out.get("cycle") or []):
        new_step, source, formula = _apply_step(phase, step, floors, zones_n, pb,
                                                floors_defaulted, phase_steps)
        # 【2】量级自检：只有走基线的量才可能"来源本身不可用"，所以只查基线；命中
        # **如实标记、原样保留**，绝不在这里改数（交上游/用户决定）。
        suspect, reason = False, ""
        if source == SOURCE_BASE:
            reason = _calc_suspect_reason(new_step, pb) or ""
            suspect = bool(reason)
            if suspect:
                new_step["_qty_suspect"] = True
                new_step["_qty_suspect_reason"] = reason
        new_cycle.append(new_step)
        _record(new_step.get("name"), float(new_step.get("qty_per_floor") or 0),
                source, _with_build_note(formula, build_note), suspect, reason)
    out["cycle"] = new_cycle

    new_attach = []
    for step in (out.get("attach_measures") or []):
        fn = MEASURE_SPECS.get(step.get("name"))
        got = fn(pb, floors, zones_n) if fn else None
        if got:
            new_step = dict(step)
            new_step["qty_per_floor"] = round(float(got["qty"]), 2)
            formula = got["formula"]
            if floors_defaulted:
                formula += "（层数取配置默认 %s 层）" % _fmt_num(floors)
            formula = _with_build_note(formula, build_note)
            _record(step.get("name"), new_step["qty_per_floor"], SOURCE_PARAM, formula)
            new_attach.append(new_step)
        else:
            new_attach.append(dict(step))
            # 措施项自带固定节拍（duration_days）时量本身不参与工期，不记为"来源"
    out["attach_measures"] = new_attach

    # ---- 外檐平行专项（单栋全楼总量）----
    new_parallel = []
    for item in (out.get("parallel_work") or []):
        spec = PARALLEL_SPECS.get(item.get("name"))
        total_area = _param_num(pb, "total_area")
        new_item = dict(item)
        if spec and total_area is not None:
            factor, what = spec
            qty_total = total_area * factor
            new_item["qty_total"] = round(qty_total, 2)
            new_item["_qty_source"] = SOURCE_PARAM
            new_item["_qty_formula"] = _with_build_note(
                "建筑面积%sm²×%s（%s） = %s m²（单栋全楼总量）"
                % (_fmt_num(total_area), _fmt_num(factor), what, _fmt_num(qty_total)),
                build_note)
            _record(item.get("name"), new_item["qty_total"], SOURCE_PARAM,
                    new_item["_qty_formula"])
        else:
            new_item["_qty_source"] = SOURCE_BASE
            new_item["_qty_formula"] = ""
            _record(item.get("name"), float(item.get("qty_total") or 0), SOURCE_BASE, "")
        new_parallel.append(new_item)
    if out.get("parallel_work") is not None:
        out["parallel_work"] = new_parallel

    if n_param and n_base:
        source = SOURCE_MIXED
    elif n_param:
        source = SOURCE_PARAM
    else:
        source = SOURCE_BASE

    return out, {"source": source, "detail": detail}


# ---------------- 专属校验规则（返回 True 通过 / 字符串描述失败原因） ----------------
def _per_floors(cfg):
    """安全取 floors_per_segment（缺失/非法 → 0）。"""
    try:
        return float(cfg.get("floors_per_segment") or 0)
    except (TypeError, ValueError):
        return 0.0


SPECIFIC_RULES = {
    "地下室结构": lambda cfg: None if (int(cfg.get("floors") or 0) == 2 and int(cfg.get("segments") or 0) == 4)
    else "地下室须 floors=2 且 segments=4（底板/墙柱/顶板分层浇筑）",
    # v2.2：主体结构必须一层一段（旧的 segments=8 是「5 层一段」口径，已废止）
    "地上主体结构": lambda cfg: None if _per_floors(cfg) == 1.0
    else "主体结构须一层一段（floors_per_segment=1）",
    "二次结构与砌体": lambda cfg: None if (_per_floors(cfg) == 1.0
                                    and int((cfg.get("lead_in") or {}).get("floors_ahead") or 0) >= 3)
    else "二次结构与砌体须一层一段（floors_per_segment=1）且 lead_in.floors_ahead ≥3",
    "装饰装修": lambda cfg: None if (cfg.get("parallel_work") or [])
    else "装饰装修须含外檐 parallel_work（与内装平行）",
}


def specific_error(phase, cfg):
    """返回专属校验失败原因；通过返回 None。"""
    fn = SPECIFIC_RULES.get(phase)
    return fn(cfg) if fn else None


# ---------------- 复评占位提示（防逐相 LLM 铺全楼层 / 复评误报"只算一层"） ----------------
def is_beat_phase(phase):
    return phase in BEAT_PHASE_NAMES


#: 节拍分部的**结构标识**（`DEFAULT_PHASES[].key`）→ 规范阶段名。
#: 第 5 批（域 3.4）：4 个阶段名只是与 10 个一级分部中的 4 个**同名**（历史名称复用），
#: 不该再是"查节拍节点"的唯一途径。`beat_phase_name()` 先按**分部 key**（结构标识）认，
#: 认不出再退回阶段名 —— 名字改了、语言换了，key 仍在。
BEAT_PHASE_KEYS = {
    "basement": "地下室结构",
    "super": "地上主体结构",
    "masonry": "二次结构与砌体",
    "finish": "装饰装修",
}


def beat_phase_name(ph):
    """这是哪个节拍分部？按 `key` 优先、阶段名兜底。不是节拍分部 → None。

    返回的是**规范名**（`BASE_BEAT_CONFIGS` 的键）。
    """
    if not isinstance(ph, dict):
        return None
    k = str(ph.get("key") or "").strip()
    if k in BEAT_PHASE_KEYS:
        return BEAT_PHASE_KEYS[k]
    nm = str(ph.get("phase") or "").strip()
    return nm if nm in BEAT_PHASE_NAMES else None


def resolve_beat_phase_prompt(spec):
    """节拍阶段的逐相 LLM 口令：**只产占位子树**，但**必须给出有序的 L4 工序清单**。

    第 5 批（域 3.2）改动：以前这里只让模型"产个占位子树"，于是「有哪些工序、什么顺序」
    这条信息在节拍分部上是**断的** —— 顺序只能来自代码里写死的 `BASE_BEAT_CONFIGS.cycle`。
    现在占位子树照旧（楼层/段/工序仍由节拍引擎按「层×段×分区」代码展开，**不让模型铺全楼层**），
    但额外要求模型给出 `l4_order`：本分部**有序**的 L4 清单，**只排序、不编号**，
    **工程量为 0 的工序也要列出来**（域 4.2a 的编号免疫依赖完整清单）。
    """
    base = ("你负责把给定的 1级 施工阶段处理为一个占位子树。本阶段属于【节拍流水节点】，"
            "楼层/段/工序 将由节拍引擎按「层×段×工序」自动代码展开。")
    hint = (spec or {}).get("hint")
    base += "\n你只需输出 1 个工作包 + 1 个占位叶子（duration_days 取估算值 5，无需铺全楼层/全段）。"
    base += ("\n**但必须同时给出 `l4_order`**：本分部要做的 L4 工序的**有序**清单"
             "（先施工的排前面）。每一项含 kb_activity_id / work_type_id / activity_name / "
             "unit / quantity。**只给顺序，不要输出任何工序号、序号或 id 数字**（编号由代码分配）。"
             "**即使某道工序在本工程中工程量为 0，也必须列出它的位置，quantity 写 0** —— "
             "漏掉量 0 的工序会让下游按序编号跳号。")
    return base + ("\n【本阶段要点】" + hint if hint else "")