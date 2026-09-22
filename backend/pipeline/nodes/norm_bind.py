"""节点：定额锚定（NormBindNode）—— 给每条 L4 叶子锚定一条定额并记录来源。

设计原则：**每个数都能溯源**。锚定结果只做两件事：
  ① 在叶子上挂 `norm_binding`（定额本体）与 `provenance`（quantity / norm / duration 三条溯源）；
  ② 把汇总写回 ctx（norm_bindings / norm_warnings / credibility / data_sources）。

锚定优先级（严格按序，匹配不上**绝不中断流程**，只"估算 + 报警"）：
  1. 有 kb_activity_id 且能从 prompt/extracted_params 提取到条件关键字
     → kb.labor_norm_match(activity_id, keywords)
        · 命中恰好 1 行 → match_type=exact，origin=kb
        · 命中多行     → 取 productivity_value 中位数那行，match_type=default
        · 命中 0 行    → 落到第 2 步
  2. kb.typical_labor_norm(activity_id) 拿到典型值 → match_type=default，origin=kb
  3. 没有 kb_activity_id 或 KB 里完全没有定额 → 用所属 L3 工种 + 经验产能估算
     → match_type=ai，origin=ai，confidence=低，并追加一条 norm_warnings
  4. 机械主导（kb.activity_info 的 recommended_production_mode == equipment_driven）
     → mode=machine，取 kb.equipment_norms 的台班定额；主控机械只认
       kb.main_machine（未标注就写"未标注主控机械"，不瞎猜）

LLM 只用在"候选多行且关键字判不了"的场合，且一次性只喂一个 L4 的候选；
LLM 不可用 / 抛异常 / 返回候选外的 id → 静默退回代码策略并记一条警告。
`self.llm is None` 时全代码路径，绝不联网（LLMClient 惰性创建，仅 LLM 分支用到）。

第 37 轮（单位 / 定额口径）三条硬口径：
  1. **单位校验只有唯一真源** `kb_units.check_unit_pair`（默认拒绝：解析不出 = unusable），
     本文件不再自带任何单位判据；
  2. **机械台班的分母只能来自 KB 行的 `quantity_unit`**。KB 缺分母 → binding 上写
     `not_usable_reason`（含"缺计量单位"）+ `norm_is_evidence=False`，降级为"仅参考"，
     **绝不**拿叶子单位把分母补出来（旧实现 `台班`→`台班/根` 自证永远通过）；
  3. **labor 产能 = 1 / labor_norm_value**（`labor_norm_value` 已归一，
     `raw_value / raw_quantity_basis == norm_value`）。`quantity_basis` 仅溯源，
     不参与任何乘法；机械台班**未归一**，`quantity_basis` 由下游照旧参与乘法；
  4. **机械台班选行按主控机械名**（在 `machine_combination_json` 里找
     `Activity_Main_Machine.machine_name`）：只有含这台机械的行才有资格提供它的台班定额；
     同名多行（夯实机 平地/槽坑、压桩机 φ300/φ400…）才用 `condition_text` 消歧，
     拿不到条件信号就保持 KB 行序；主控机械**不在任何行** → 降级"仅参考"
     （`not_usable_reason="主控机械缺台班定额（<机名>）"`），**绝不**借同行机械的定额
     （旧实现借 `rows[0]` 首台机械：CONC_NEW_FOUND 选中"后浇带／振捣器 1.26 台班"，
     266 m³ 被算成 34 天/段，正确行是泵车 0.055 台班/10m³ → 2 天/段）。

第 41 轮（口径关 / 绑定一致性 / 机械优先，契约 §2/§3/§4）：
  1. **口径关**：`measure_scope`（计量对象，如"建筑面积"vs"风管展开面积"）在绑定活动
     之后、算工日之前比较；**同量纲不同对象必须判不一致**，能换算就以定额口径为准并留痕
     `basis_adjust`，换算不了就 `usable=False` + `not_usable_reason="口径无法对齐"`；
     **禁止 1:1 硬乘、禁止静默丢弃**。口径未知（`''`）只打 `basis_unconfirmed`，不阻断。
  2. **绑定一致性校验**：工序名与所选活动明显不符 → 先在同 L3 内改绑，改绑不到才降级
     `match_type="unbound"` + `not_usable_reason="活动绑定不一致"`。
  3. **机械优先**：`recommended_production_mode='equipment_driven'` 且有台班行的活动
     必须走 `mode="machine"`，**不退回人工定额**；机械算出来比人工更长也照用。

量级基线（`Production_Method_Baseline`，契约 §5-WS3②）优先于任务名关键词：
  `> qty_threshold_high` → 强制机械（改绑同 L3 的 `machine_activity_hint`）；
  `< qty_threshold_low`  → 人工；中间 → 机械 + 人工两行都绑（人工只算修边量）；
  改绑不到 → 该定额降级"仅参考"，绝不进入工期计算。
`_expects_machine` 的关键词检测降级为**辅助信号**（只在没有量级基线时兜底）。

D 组（定额条件锁定，本轮）：
  1. **D1 选定 L4 时锁条件**：`lock_leaf_conditions()` 把该 L4 的
     `condition_combination` 维度收敛成 `leaf["condition_key"]`（用户输入优先，
     用户没给的维度取典型 = KB `L4_Norm_Default` 同口径的"中位行"条件）；
  2. **D2 按条件精确查**：`_matches_condition_rows()` 逐维度**逐值相等**（含
     `A|B` 别名与 `≤25`/`≤25mm` 写法变体），**禁止**旧 `labor_norm_match` 的子串
     模糊匹配、**禁止**匹配不上退默认行 —— 匹配不上 → `usable=False` +
     `not_usable_reason` 写"缺定额…"，逐条留痕；
  3. **D3 来源标注**：`leaf["condition_source"]` 逐维度给出 `user` / `l4` / `typical`，
     `provenance.note` 里写成人话（`条件锁定：构件类型=框架梁[用户输入]、…`）；
  4. **D4 「构件做法」硬定现浇**：`_CONSTRUCTION_METHOD = "现浇"`，恒定不参与猜测；
     KB 里写 `预制` 等其它做法的定额行在**候选入口**（`_labor_rows`）就被摘掉；
  5. **D5 以数据库分母口径为准、由 AI 换算工程量**：跨族换算的换算参数只从
     文本/项目参数/材料/**定额行条件**取（`_resolve_convert_ctx(..., row_condition=)`），
     落盘 `binding["basis_adjust"]` 六字段（调整后的工程量 + 换算方法与依据）；
     **参数只是中间物，不落盘、不建表、不设全局默认值** —— 原来的写死常量
     `_AI_PILE_LENGTH_M = 18.0` 已删除，桩长改由「桩长18m以内」这条定额条件推定并标
     `ctx_source='ai_estimate'`（限值型；厚度档位那条见下，标 `norm_condition`）；
     换算参数/换算后的量都要过 `_MAGNITUDE_BANDS` 的
     **量级校验**，离谱即拒（`usable=False` + `basis_adjust_rejected` 留痕），
     推定不出 → 报缺（不退回默认值硬算）。
     来源标注（裁定-1）：`ctx_source ∈ {text, materials, norm_condition, ai_estimate}`
     —— `norm_condition` = 取值来自**定额行适用条件**（如「≤200mm」→0.2 m），
     **不是 AI 估算**，绝不许标成 `ai_estimate`（假溯源与漏标一样违规）；
     `ai_estimate` 只留给"型号可推断的行业默认"（目前只有桩长限值那一条）。
  6. **D1/D2 也覆盖机械**（裁定-3）：`Norm_Equipment_Table` 精筛只做**有数据支撑的
     部分**（`_equip_candidates` / `_equip_condition_map`），实测依据与边界见那两个
     函数的 docstring；没有可用维度时不硬筛，保留第 37 轮的主控机械名选行口径并写审计键。

G1（本轮）：删掉路径③ 里那条 A 类判据
`if binding.get("norm_is_evidence"): _set_usable(binding, False, "AI估算定额：KB 无定额行，只作参考")`
—— 它把 62 条 `match_type='ai'` 的叶子全部堵死。政策见 `norm_defaults`
（`STATE_RELEASED_AI` / `LABEL_AI_ESTIMATE` / `STATE_NO_VALUE`）：AI 经验估算定额
**放行 + 逐行标注**。`usable=False` 的**其它正当来源**（缺计量单位 / 不可换算 /
`口径无法对齐` / 活动绑定不一致 / D5 的推定不出与换算不合理）全部保留。
"""

import json
import re

from .. import kb
from .. import kb_units
from ..base import BaseNode
from ..llm import LLMClient, LLMError
from ..prompts_loader import load

# ==================== 常量 ====================
# 条件组合里参与"关键字提取"的维度；定额表/未分类 这类元字段刻意不列入
CONDITION_KEYS = ('构件类型', '钢筋直径', '直径', '体积', '长度', '高度', '周长',
                  '施工方法', '运输方式', '预制场所', '混凝土类型', '土壤类别')

# 关键字打分：施工方法/运输方式最能定生死，其次构件类型，再次规格尺寸
_KEY_WEIGHT = {"施工方法": 90, "运输方式": 85, "构件类型": 80, "预制场所": 70,
               "混凝土类型": 60}
_DEFAULT_WEIGHT = 50

# 经验产能（人工兜底，单位/工日）：值越小越保守（工期越长），宁可偏保守
_EXPERIENCE_PRODUCTIVITY = {
    "钢筋工": 0.30, "混凝土工": 3.00, "模板工": 8.00, "砌筑工": 1.50,
    "抹灰工": 12.00, "防水工": 15.00, "架子工": 20.00, "电工": 20.00,
    "管道工": 8.00, "通风工": 8.00, "油漆工": 30.00, "测量工": 1.00,
    "桩机工": 5.00, "普工": 2.00,
}
_EXPERIENCE_DEFAULT = 2.00     # 未识别工种时的通用经验产能（单位/工日）
_AI_NOTE = 'KB 无定额，按 L3 工种经验产能估算（AI 假设）'

# ==================== D4：「构件做法」硬定 ====================
# 「构件做法」不再参与猜测：本产品的口径一律 **现浇**。不是"猜出来的现浇"，
# 而是**硬定的口径** —— 所以：
#   ① 锁条件时它恒等于 `_CONSTRUCTION_METHOD`，来源固定标注为 `l4`（不是 user / typical）；
#   ② KB 里条件写着其它做法（实测 `构件做法=预制` 共 96 行）的定额行**一律不匹配**，
#      由 D2 的"匹配不上就报缺"路径处理，绝不退回预制行充数。
_CONSTRUCTION_METHOD_KEY = "构件做法"
_CONSTRUCTION_METHOD = "现浇"
_CONSTRUCTION_METHOD_FIXED = frozenset((_CONSTRUCTION_METHOD, "现场预制", "加工厂预制",
                                        "预制", "预制混凝土", "现浇混凝土", "现浇构件"))

# ==================== D3：条件来源标注 ====================
# 每条模板绑定都要能说出"条件是哪来的"；三个来源与文档 D3 一一对应：
CONDITION_SOURCE_LABELS = {
    "user": "用户输入",              # ② 用户明确给的（叶子条件字段 / 用户原话 / 项目参数）
    "l4": "L4自身可推断",            # ① 该 L4 自己的取值即可推断（构件类型…含 D4 硬定的构件做法）
    "typical": "取典型",             # ③ 用户没给 → 取 KB 典型条件（L4_Norm_Default 同口径的收敛行）
}
#: 条件里恒定的维度（D4）：来源永远算 `l4`，不参与"用户有没有给"的推断
_FIXED_CONDITION_SOURCES = {_CONSTRUCTION_METHOD_KEY: "l4"}
#: 定额表自带的元字段，不参与条件比对（不是**构件**条件）
_META_CONDITION_KEYS = frozenset(("定额表", "未分类", "条件", "子目名称"))

# ==================== D5：换算参数的来源标注（裁定-1：不许谎报） ====================
# `ctx_source` 是**换算参数**（桩长/厚度/容重/单根体积/单件重量）的来源代号，
# 与 D3 的"条件来源"是两码事。取值与含义（kb_units 侧写、本文件照实落盘）：
#   text          → 用户在任务名/条件文本/项目参数里**明写**的数字（最高优先级）
#   materials     → boundary_conditions.materials（用户材料清单里明写）
#   norm_condition→ **定额行适用条件**里的档位值（如「≤200mm」→ 0.2 m）：
#                   来自数据库口径，**不是 AI 猜的**，不许标成 ai_estimate
#   ai_estimate   → 型号可推断的行业默认（目前无此路径；将来真有才是它）
CONVERT_SOURCE_LABELS = {
    "text": "用户明写的换算参数",
    "materials": "材料清单里的换算参数",
    "norm_condition": "定额行适用条件的厚度档位（数据库口径，非 AI 估算）",
    "ai_estimate": "AI 估算的换算参数",
}

# ==================== D5：换算的量级校验 ====================
# 「换算后的量 / 换算系数必须落在合理带内，离谱即拒」—— 单位换算本身没错，
# 错的是拿一个荒谬的物理量去换（例：把薄墙按 3 m 厚折、把桩长按 300 m 折）。
# 形态：族对 → (参数名, 下界, 上界, 单位, 说明)。只拦"离谱"，不做业务判断。
# ⚠️ 与 D5 禁止的"全局默认参数值"不是一回事：这里**不提供任何取值**，只判合理性。
_MAGNITUDE_BANDS = {
    ("count:根", "length"): ("pile_length_m", 0.3, 200.0, "m", "单根桩长"),
    ("length", "count:根"): ("pile_length_m", 0.3, 200.0, "m", "单根桩长"),
    ("count:根", "volume"): ("volume_per_pile_m3", 0.001, 500.0, "m³", "单根体积"),
    ("volume", "count:根"): ("volume_per_pile_m3", 0.001, 500.0, "m³", "单根体积"),
    ("mass", "volume"): ("density_t_per_m3", 0.1, 25.0, "t/m³", "材料容重"),
    ("volume", "mass"): ("density_t_per_m3", 0.1, 25.0, "t/m³", "材料容重"),
    ("area", "volume"): ("thickness_m", 0.005, 1.5, "m", "构件厚度"),
    ("volume", "area"): ("thickness_m", 0.005, 1.5, "m", "构件厚度"),
    ("count:件", "mass"): ("unit_weight_kg_per_piece", 0.01, 200000.0, "kg", "单件重量"),
    ("mass", "count:件"): ("unit_weight_kg_per_piece", 0.01, 200000.0, "kg", "单件重量"),
}


# ==================== 小工具 ====================
def _to_float(v, default=0.0):
    """安全转 float；None / 空串 / 非法值 → default。"""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=0):
    """安全转 int。"""
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _json_list(raw):
    """把 KB 里的 JSON 数组字段解析成 list（解析失败返回 []）。"""
    if isinstance(raw, list):
        return raw
    try:
        out = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return out if isinstance(out, list) else []


def _append_note(old, extra):
    """拼接 note，避免重复。"""
    old = (old or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return old
    if not old:
        return extra
    if extra in old:
        return old
    return old + "；" + extra


def _is_finite(v):
    """float 且不是 nan/inf（量级校验要拦"离谱"，nan/inf 也算离谱）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def _param_phrase(param, value, unit):
    """换算参数的**可读写法**（进 `basis_adjust.method`，例：「按 200 mm 板厚」）。

    参数只是换算过程的中间物：这里把它写成一句人话，不落盘成独立对象、不建表。
    """
    label = {"thickness_m": "板厚", "pile_length_m": "单根桩长",
             "volume_per_pile_m3": "单根体积", "density_t_per_m3": "容重",
             "unit_weight_kg_per_piece": "单件重量"}.get(param, param)
    if not _is_finite(value) or value <= 0:
        return "%s（未取值）%s" % (label, unit or "")
    if param == "thickness_m":
        # 例：「按 200 mm 板厚，由 750 ㎡ 折 150 m³」（D5 的 method 写法）
        mm = value * 1000.0
        if abs(mm - round(mm)) < 1e-6:
            return "%d mm %s" % (int(round(mm)), label)
        return "%g %s %s" % (value, unit or "", label)
    return "%s %g %s" % (label, value, unit or "")


# ==================== WBS 遍历 ====================
def iter_leaves(wbs):
    """按 阶段 → 工作包 → 子包 三层顺序产出 (leaf, 阶段名, 工作包名)。"""
    for phase in (wbs or {}).get("phases") or []:
        pname = phase.get("phase") or ""
        for wp in phase.get("work_packages") or []:
            for sub in wp.get("sub_packages") or []:
                yield sub, pname, (wp.get("name") or "")


# ==================== 条件关键字提取 ====================
def _text_pool(ctx, leaf, wp_name):
    """把可能含条件线索的文本汇成一池：叶子名 + 工作包名 + 用户原话 + 项目参数。

    只有出现在这个池子里的条件值才算"用户提供"，凭空补条件等于编造。
    """
    parts = [str((leaf or {}).get("name") or ""), str(wp_name or "")]
    ctx = ctx if isinstance(ctx, dict) else {}
    parts.append(str(ctx.get("prompt") or ""))
    params = ctx.get("extracted_params")
    if isinstance(params, dict):
        for k, v in params.items():
            parts.append("%s %s" % (k, v))
    return " ".join(p for p in parts if p)


_DIGIT_RE = re.compile(r"\d")


def _condition_variants(value):
    """一个条件值的写法变体。

    KB 里规格常带单位/小数（"≤16mm"、">25.01mm"），而用户原话往往写 "≤16"。
    这里对每个数字片段额外产出"砍掉尾数/去掉单位"的写法，扩大召回；
    变体同样必须逐字出现在用户文本里才会被采纳，所以不会凭空造条件。
    """
    out = []

    def _add(v):
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)

    _add(value)
    text = str(value or "")
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        token = text[:m.end()]
        _add(token)
        num = m.group(0)
        if "." not in num:
            continue
        _add(text[:m.start()] + num.split(".")[0])
    return out


def _anchors_from_row(row, cond_comb):
    """从一行的 condition_combination 里抽出「维度=值」的锚点。

    value 里的 "A|B" 是别名写法（如 ">25.01mm|>25"），拆成多个候选值；
    每个值再展开成写法变体（见 _condition_variants）。
    """
    out = []
    comb = cond_comb
    if isinstance(comb, str):
        try:
            comb = json.loads(comb)
        except (ValueError, TypeError):
            comb = None
    if not isinstance(comb, dict):
        return out
    for key in CONDITION_KEYS:
        val = comb.get(key)
        if not val:
            continue
        for one in (val if isinstance(val, (list, tuple)) else [val]):
            for alias in re.split(r"[|｜]", str(one)):
                for variant in _condition_variants(alias):
                    out.append((key, variant))
    return out


def extract_condition_keywords(rows, text_pool):
    """在候选定额行里，挑出"用户真的说了"的条件值作为匹配关键字。

    做法：遍历候选行的 condition_combination（取全部候选，去重后通常只有几十个锚点），
    取每个条件值，只有它**逐字出现在 text_pool**（叶子名 / 用户原话 / 项目参数）里才采纳。

    排序刻意不只看权重：先看"这个词在候选里还剩几行"（越少越具体，越可能一箭命中），
    再按维度权重、再按字长。否则 "≤16mm"（文本里带单位）会排在 "≤16"（KB 里不带）
    前面，反而把候选筛不干净。

    返回 (keywords, 命中维度描述)；一条都提不出来时返回 ([], "")。
    """
    cand = []
    seen = set()
    for row in rows or []:
        cond_comb = row.get("condition_combination")
        if isinstance(cond_comb, str):
            try:
                cond_comb = json.loads(cond_comb or "{}")
            except (ValueError, TypeError):
                cond_comb = {}
        if not isinstance(cond_comb, dict):
            continue
        for key, val in _anchors_from_row(row, cond_comb):
            if (key, val) in seen:
                continue
            seen.add((key, val))
            if val not in (text_pool or ""):
                continue
            cand.append((_KEY_WEIGHT.get(key, _DEFAULT_WEIGHT), key, val))

    def _hits(val):
        n = 0
        for r in rows or []:
            hay = "%s %s" % (r.get("condition_text") or "",
                             r.get("condition_combination") or "")
            if str(val) in hay:
                n += 1
        return n

    cand.sort(key=lambda x: _hits(x[2]))
    used, picked = [], set()
    for _weight, key, val in cand:
        if val in used:
            continue
        used.append(val)
        picked.add(key)
        if len(picked) >= 2:
            break
    desc = "、".join(str(v) for v in used)
    return used, desc


# ==================== D1/D2/D3：条件锁定 + 按条件精确查定额 ====================
def _condition_values(value):
    """一个条件格子的取值集合（KB 用 `A|B` 写别名，如 `>25.01mm|>25`）。"""
    out = []
    for one in (value if isinstance(value, (list, tuple)) else [value]):
        for alias in re.split(r"[|｜]", str(one or "")):
            alias = alias.strip()
            if alias and alias not in out:
                out.append(alias)
    return out


def _condition_combination_of(row):
    """定额行的条件组合 → dict（解析不出来返回 {}）。"""
    comb = (row or {}).get("condition_combination")
    if isinstance(comb, str):
        try:
            comb = json.loads(comb or "{}")
        except (ValueError, TypeError):
            return {}
    return comb if isinstance(comb, dict) else {}


def _cond_cell_matches(row_value, want_value):
    """条件格子是否匹配：两边的**写法变体**集合有交集（精确相等，不做包含/模糊匹配）。

    这是 D2「按条件精确查」的唯一判据。刻意**不**用 `in`/子串：子串匹配正是旧
    `labor_norm_match` 的关键字模糊匹配（`≤16` 命中 9 行、`框架梁` 命中 3 行）。

    但"写法不同"必须能对上：库写 `≤25mm`、用户写 `≤25`（`_condition_variants` 的
    既有口径）—— 这是同一规格的两种写法，不是模糊匹配。
    """
    rv = set()
    for one in _condition_values(row_value):
        rv.update(_condition_variants(one))
    wv = set()
    for one in _condition_values(want_value):
        wv.update(_condition_variants(one))
    return bool(rv & wv)


def _cast_in_place_rows(rows):
    """D4 硬过滤的独立入口：只做「构件做法 ≠ 非现浇」这一件事（忽略其它维度）。

    给"典型的候选集 / 锁定前的候选集"用 —— D4 是恒定口径，跟锁没锁条件无关，
    所以每一条选行路径都必须过这道滤网，否则预制行会在别的分支里漏回来。
    行**没写**该维度时保留（没写就没有把做法限定成别的）。
    """
    return [r for r in (rows or []) if isinstance(r, dict)
            and _matches_construction_method(
                _CONSTRUCTION_METHOD,
                _condition_combination_of(r).get(_CONSTRUCTION_METHOD_KEY))]


def _matches_condition_rows(rows, lock):
    """按锁定的条件**精确**筛定额行：每个锁定维度都必须与行逐值相等。

    - 行里没有该维度 → 不匹配（条件维度不是"可选提示"，是绑定依据）；
    - `构件做法`（D4）**不是锁定维度**，而是恒定的硬过滤：任务侧永远是 `现浇`，
      KB 里写着其它做法（实测 `构件做法=预制` 共 96 行）的定额行**一律出局**，
      由 D2 的"匹配不上就报缺"路径处理，绝不退回预制行充数 —— 这样 D4 只是
      **过滤掉不该用的行**，不会挤掉本该参与比较的现浇候选；
    - 空条件行（`condition_combination` 为 `{}`）只在**没有锁定维度**时通过，
      否则不予匹配 —— 这就是 D2「禁止匹配不上就退默认行」。

    返回筛出的行列表（可能为空；调用方必须按"匹配不上就报缺"处理）。
    """
    lock = lock or {}
    out = []
    for row in rows or []:
        comb = _condition_combination_of(row)
        if not _matches_construction_method(_CONSTRUCTION_METHOD, comb.get(
                _CONSTRUCTION_METHOD_KEY)):
            continue                                    # D4：非现浇做法一律出局
        if lock:
            if not all(_cond_cell_matches(comb.get(dim), want)
                       for dim, want in lock.items()
                       if dim not in _META_CONDITION_KEYS and dim in comb):
                continue
            if any(dim not in comb for dim in lock if dim not in _META_CONDITION_KEYS):
                continue
        out.append(row)
    return out


def _matches_construction_method(want, row_value):
    """D4：`构件做法` 硬定比对。

    任务侧恒为 `现浇`；定额行**没写**该维度时视为可在现浇条件下适用（它的条件里
    没有把做法限定成别的），行里写了就只认同一做法。
    """
    want = str(want or _CONSTRUCTION_METHOD).strip() or _CONSTRUCTION_METHOD
    val = str(row_value or "").strip()
    if not val:
        return True
    if want == val:
        return True
    return val not in _CONSTRUCTION_METHOD_FIXED and want == _CONSTRUCTION_METHOD


def _user_condition_lock(leaf, rows, text_pool):
    """从**用户侧**取条件锁定：叶子条件字段 + 用户原话/项目参数里明写的条件值。

    只采纳两种逐字可见的条件值（`extract_condition_keywords` 的既有口径）：
      ① `leaf["condition_text"]` 里以「维度=值 / 维度:值」或直接出现的条件值；
      ② 出现在 `text_pool`（叶子名 / 工作包名 / 用户原话 / 项目参数）里的条件值。
    """
    lock, src = {}, {}
    user_text = ""
    if isinstance(leaf, dict):
        for key in ("condition_key", "condition_text", "condition"):
            val = leaf.get(key)
            if isinstance(val, str) and val.strip():
                user_text = _append_note(user_text, val)
            elif isinstance(val, dict):
                for dim, v in val.items():
                    for one in _condition_values(v):
                        lock[str(dim)] = one
                        src[str(dim)] = "user"
    # 自由文本形态的条件字段：`构件类型=悬挑板, 钢筋直径=≤16` / `构件类型:悬挑板`。
    # 这是上游（extractor / 用户输入）最可能给出的写法，必须能直接解析成「维度=值」，
    # 否则锁条件会拿典型值把它顶掉（实测：写了"悬挑板"却锁成"拱形梁"）。
    for dim, val in re.findall(
            r"([\u4e00-\u9fff]{2,8})\s*[=＝:：]\s*([^,，;；、|]+)", user_text):
        dim, val = dim.strip(), val.strip()
        if not val or dim in _META_CONDITION_KEYS:
            continue
        lock.setdefault(dim, val)
        src.setdefault(dim, "user")
    if lock:
        # 已经显式写明的维度不再靠文本二次推断（用户给的优先，逐条可溯源）
        return lock, src
    # 用锚点表把"用户文本里出现的条件值"翻回「维度=值」（只认逐字出现，绝不凭空造条件）
    hay = _append_note(user_text, text_pool)
    for dim, val in _condition_anchors(rows):
        if val and val in hay:
            lock.setdefault(dim, val)
            src.setdefault(dim, "user")
    return lock, src


def _condition_anchors(rows):
    """候选行里全部「维度=值」锚点（含 `_condition_variants` 展开的写法变体）。

    排序与 `extract_condition_keywords` 同口径：越能在候选里筛掉更多行的越靠前
    （`_hits` 小者优先），保证"用户明写的条件值"优先落进锁定。
    """
    seen, anchors = set(), []
    for row in rows or []:
        for key, val in _anchors_from_row(row, _condition_combination_of(row)):
            if (key, val) in seen or key in _META_CONDITION_KEYS:
                continue
            seen.add((key, val))
            anchors.append((key, val))

    def _hits(val):
        n = 0
        for r in rows or []:
            hay = "%s %s" % (r.get("condition_text") or "",
                             r.get("condition_combination") or "")
            if str(val) in hay:
                n += 1
        return n

    anchors.sort(key=lambda kv: _hits(kv[1]))
    return anchors


def _typical_condition_lock(rows):
    """用户没给条件时的**典型条件**（D1：取典型）。

    取值口径与 KB `L4_Norm_Default` 的"自动收敛"完全一致：在该 L4 的定额行里取
    `productivity_value` 的**中位数那一行**，把它的条件组合当作典型条件锁定。
    返回 `(lock, 典型行)`；没有候选行 → `({}, None)`。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {}, None
    srt = sorted(rows, key=lambda r: _to_float(r.get("productivity_value"), 0.0))
    row = srt[len(srt) // 2]
    lock = {}
    for dim, val in _condition_combination_of(row).items():
        if dim in _META_CONDITION_KEYS or dim == _CONSTRUCTION_METHOD_KEY:
            continue
        vals = _condition_values(val)
        if vals:
            lock[dim] = vals[0]
    return lock, row


def _constant_condition_note():
    """D4 恒定的那一条（`构件做法=现浇`）：不是锁出来的条件，是**硬定的口径**。

    单独成句，不混进 `condition_key`：否则它会被当成"用户给/取典型"的维度，
    既污染 `condition_source` 的语义，又会在精确筛里把没有该维度的行整片筛掉。
    """
    return "%s=%s[%s（硬定，不参与猜测）]" % (
        _CONSTRUCTION_METHOD_KEY, _CONSTRUCTION_METHOD,
        CONDITION_SOURCE_LABELS["l4"])


def _record_row_conditions(leaf, row):
    """把**实际选中的定额行**的条件补进 `leaf` 的条件留痕（来源标 `typical`）。

    已由用户输入锁定的维度不覆盖（用户明确给的值优先），只补 KB 典型条件。
    """
    if not isinstance(leaf, dict) or not isinstance(row, dict):
        return
    lock = dict(leaf.get("condition_key") or {})
    src = dict(leaf.get("condition_source") or {})
    for dim, val in _condition_combination_of(row).items():
        if dim in _META_CONDITION_KEYS or dim == _CONSTRUCTION_METHOD_KEY:
            continue
        if dim in lock:
            continue
        vals = _condition_values(val)
        if vals:
            lock[dim] = vals[0]
            src[dim] = "typical"
    leaf["condition_key"] = lock
    leaf["condition_source"] = src


def lock_leaf_conditions(leaf, rows, text_pool):
    """D1：锁定该 L4 的条件并写进 `leaf["condition_key"]`；返回 `(lock, sources, row)`。

    顺序：用户输入 → 用户没给的维度取**典型条件**。`构件做法` **不**进 lock（D4 是恒定
    过滤，见 `_matches_condition_rows` 与 `_constant_condition_note`）。
    返回值同时是 D3 的留痕：`sources` 逐维度给出 `user` / `typical`。
    """
    lock, src = _user_condition_lock(leaf, rows, text_pool)
    typ_lock, typ_row = _typical_condition_lock(rows)
    if typ_lock:
        for dim, val in typ_lock.items():
            if dim not in lock:
                lock[dim] = val
                src[dim] = "typical"
    # 用户没给条件的维度，来源一律 `l4`（该 L4 自身可推断：构件类型/钢筋直径…）
    for dim in lock:
        if dim not in src:
            src[dim] = "l4"
    if isinstance(leaf, dict):
        leaf["condition_key"] = dict(lock)
        leaf["condition_source"] = dict(src)
        leaf["condition_locked"] = True
    return lock, src, typ_row


def _condition_lock_note(lock, sources):
    """条件的可读留痕（进 `provenance.note`，供"逐条标注来源"）。

    末尾恒定带 D4 的 `构件做法=现浇`（来源 `L4自身可推断`），让每条绑定都能一句话
    说清"这个值是哪来的"。
    """
    parts = []
    for dim, val in (lock or {}).items():
        label = CONDITION_SOURCE_LABELS.get((sources or {}).get(dim, ""), "取典型")
        parts.append("%s=%s[%s]" % (dim, val, label))
    parts.append(_constant_condition_note())
    return "条件锁定：" + "、".join(parts)


def _condition_lock_match_type(sources):
    """匹配类型是否算"精确命中"。

    只有**全部**锁定维度都来自用户输入 / L4 自身可推断（含 D4 硬定的构件做法）时才是
    `exact`；只要有一个维度是"取典型"补的，就按 `default`（与既有 match_type 语义一致：
    exact = 用户给的条件对上了，default = 系统补的条件对上了）。
    """
    for dim, s in (sources or {}).items():
        if s == "typical":
            return "default"
    return "exact"


_MACHINE_HINTS = ('机械', '台班', '泵车', '输送泵', '塔吊', '起重机', '挖掘机',
                  '推土机', '装载机', '压路机', '旋挖', '成槽', '钻孔', '打桩',
                  '桩机', '摊铺机', '罐车', '自卸汽车')
_LABOR_HINTS = ('人工', '手工', '人工作业')


def _expects_machine(task_name, work_type=""):
    """任务描述是否像"机械作业"。只看**明确**的机械词；出现"人工"则以人工为准。"""
    text = "%s %s" % (task_name or "", work_type or "")
    if any(h in text for h in _LABOR_HINTS):
        return False
    return any(h in text for h in _MACHINE_HINTS)


def _bigrams(text):
    """中文二元组（用于任务名与活动名的相似度打分）。

    不用关键词白名单：白名单永远不全（"成槽""旋挖""打桩"…），
    二元组对中文短语的匹配既通用又便宜。
    """
    s = re.sub(r"[（）()【】\[\]、，,。·\s/／\-—_]+", "", str(text or ""))
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _method_conflict_note(task_name, leaf, info):
    """任务描述与所绑活动的主导方式矛盾时返回中文说明，否则 None。

    为什么会矛盾：`kb_activity_id` 是模型给的，它可能把"机械挖基坑土方"绑到
    KB 里的人工挖土方活动上。活动本身标得很清楚，但没有环节去比对"任务说的"
    和"活动标的"是否一致 —— 于是人工定额被当成这条机械任务的依据。
    """
    if not info:
        return None
    act_mode = str(info.get("recommended_production_mode") or "").strip().lower()
    method = str(info.get("construction_method") or "").strip()
    activity = str(info.get("activity_id")
                   or (leaf or {}).get("kb_activity_id") or "")
    if _expects_machine(task_name, (leaf or {}).get("work_type") or "") \
            and act_mode != "equipment_driven":
        return ("任务描述像机械作业，但绑定的活动 %s（%s）标注为**非机械主导**"
                "（recommended_production_mode=%s、施工方法=%s）—— 定额口径与任务不符，"
                "已按「非有据可查」处理、沿用 WBS 工期。"
                % (activity, info.get("activity_name") or "",
                   act_mode or "未标注", method or "未标注"))
    return None


# ==================== 绑定一致性校验（契约 §3-D2） ====================
#: **改绑候选**（`_resolve_activity_conflict` / `_reanchor_machine`）要求任务名与活动名
#: 至少共有几个二元组。取 2 与既有 `_reanchor_machine` 同源：那一条已经用 2 证明过
#: "能认出对得上"。注意这不是"明显不符"的判据 —— 那条只看**零重合**（见下）。
_REBIND_OVERLAP_MIN = 2
#: 名字一致性校验里"同 L3 有更像的替代"的门槛，**比 `_REBIND_OVERLAP_MIN` 更严**。
#: 为什么必须更严：2 字重合只是"半像"（`1-1层 铝模安装` vs `铝合金模板安装` 只有「铝模」），
#: 拿这种候选当"更好的替代"会把本来有定额的绑定打成未绑定（真实重放实测 +285 天）。
#: 3 字重合才说明"任务名基本就是那条活动的名字"（如 `暖通预留预埋` vs 预留预埋类活动）。
_BETTER_SIBLING_MIN = 3
#: 名字一致性校验的判据线（契约 §3）：连续共同片段 < 该值才算"明显不符"。
_NAME_OVERLAP_MIN = 2


def _name_chars(text):
    """名字的规范字符集（去标点/去空白/去楼层前缀）——专供一致性校验。"""
    return re.sub(r"[（）()【】\[\]、，,。·\s/／\-—_]+", "", str(text or ""))


def _name_overlap(task_name, activity_name):
    """任务名与活动名的**最长连续共同片段**长度（单位：字）。

    用连续片段而不是"二元组交集大小"：中文工序名的同义/简写差别很大，而且两个名字
    偶然共用一个字也很常见。实测（`backend/_probe_tmp/ws1_calibrate.py` 扫出测试里
    全部 34 对真实 `(name, kb_activity_id)`）：

      · `预应力管桩（PHC-A400-95）施工` vs `压预制管桩`：共同片段「管桩」= 2；
      · `室外给排水管网开挖` vs `挖地槽（沟）`：共同片段「挖」= 1 → **不足 2**
        —— 但两者同属 earthwork、语义都是土方开挖，按注释里的二元组判法会把这条
        **合法**绑定打成不符（实测 `test_中间量级机械人工两行都绑` 因此变红）；
      · `Ⅰ区 1-1层 钢筋绑扎` vs `板钢筋`：共同片段「钢筋」= 2。

    而契约 §3 两处确证错绑的连续共同片段都 **< 2**：
      · `场地平整` vs `场地硬化`：只有孤立的「地」（1，注意「场地」≠「平/硬」不连续）；
      · `暖通预留预埋` vs `风管制作安装`：0。

    所以判据线取"连续共同片段 < 2"。这是**刻意的偏保守选择**：宁可不拦（继续算，
    但下游仍受口径关与 `usable` 约束）也不错拦（错拦会让整条工序一个班组都算不出来）。
    """
    a = _name_chars(task_name)
    b = _name_chars(activity_name)
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
            else:
                cur[j] = 0      # 必须清零才是"连续片段"；否则退化成最长公共子序列
        prev = cur
    return best


#: 通用施工后缀/词：只靠这些字对齐**不算**"更像的替代"（`混凝土沟盖板安装` 与
#: `ALC墙板安装` 共有「安装」，但两者毫无关系）。
_GENERIC_NAME_SUFFIXES = ("安装", "工程", "施工", "作业", "浇筑", "制作", "拆除",
                          "铺设", "搭设", "砌筑")

#: `_candidate_has_norm` 的进程级缓存：activity_id → bool。
_HAS_NORM_CACHE = {}

#: 允许**跨 L3** 做机械优先改绑的"土石方/场地类动作词"白名单（父代理裁决的受限例外）。
#: 为什么需要跨 L3：`1.1.1 场地平整` 在 L3 `site_prep`，而正确的机械活动
#: `GD_A11_平整场地` 在 L3 `earthwork` —— 这是 L3 映射表太粗，不是"不该改绑"。
#: 为什么必须限定动作词：不许"任意跨 L3"，否则 `铝模安装` 这种会被跨到名字碰巧像的
#: 机械活动上，等于瞎绑。
_CROSS_L3_ACTION_WORDS = ("平整", "推土", "挖装", "挖", "回填", "碾压", "夯实",
                          "铲运", "支挡土板", "打夯")


def _longest_common_fragments(a, b):
    """`a` 与 `b` 的所有**最长连续共同片段**（去重，按长度降序）。

    只用于"这两条名字是不是靠通用后缀对齐的"这个判断，不参与评分。
    """
    a, b = _name_chars(a), _name_chars(b)
    if not a or not b:
        return []
    best = 0
    frags = set()
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
                    frags = {a[i - cur[j]:i]}
                elif cur[j] == best:
                    frags.add(a[i - cur[j]:i])
            else:
                cur[j] = 0
        prev = cur
    return sorted(frags, key=len, reverse=True)


def _common_fragments(a, b, min_len):
    """长度 ≥ `min_len` 的连续共同片段（用 `_longest_common_fragments` 的结果过滤）。"""
    return [f for f in _longest_common_fragments(a, b) if len(f) >= min_len]


def _candidate_has_norm(activity_id):
    """该活动自己有没有可用定额（人工有正值行 / 台班有行 / `L4_Norm_Default` 审定量）→ 能不能当"替代"。

    带进程级缓存：名字一致性校验会对同 L3 的几十个活动逐个问一次。

    ---- 第 7 批（2026-09-21，用户裁定）：把 `L4_Norm_Default` 也算进来 ----
    原先只认 `Norm_Labor_Table` / `Norm_Equipment_Table`，于是 `MASON_ALC_PANEL`
    「ALC墙板安装」（人工表 0 行、台班表 0 行，但 `L4_Norm_Default` 有 0.095 工日/m²）
    被判"没有定额 ⇒ 不是更像的替代" ⇒ 冲突判据第③条不成立 ⇒ **不改绑**
    ⇒ 继续拿 m³ 的「砌块墙」去套 m² 的 ALC 任务 ⇒ 撞量纲墙 ⇒ 报缺退回 WBS。
    **缺陷本质**：这个判据的口径与"绑定层实际会不会用上它"不一致 —— 绑定层的
    `_labor_candidates()` 本来就会回退到 `L4_Norm_Default`。
    另参 `kb.labor_norm_default()` 的 docstring 原话："调用方应当回退到它，
    而不是判'无定额'"。

    ⚠️ 放宽**只影响本函数唯一的调用方** `_activity_name_conflict`（:901），而那里还有
    一道更严的闸：候选名必须**整名包含**任务名（或反之，:903）—— 即"这条候选就是这个
    任务"，不是"共有几个字"。所以放宽不会让"半像"的候选混进来。
    真正"试绑不成功就降级"的风险由 `_resolve_activity_conflict()` 兜着：它的试绑要过
    单位校验（`norm_is_evidence`）才算成立，不成立就不改绑、走契约 §3 降级。
    """
    if not activity_id:
        return False
    if activity_id in _HAS_NORM_CACHE:
        return _HAS_NORM_CACHE[activity_id]
    ok = False
    try:
        if kb.equipment_norms(activity_id):
            ok = True
        elif kb.labor_norms(activity_id):
            ok = any(_to_float(r.get("norm_value"), 0.0) > 0
                     or _to_float(r.get("productivity_value"), 0.0) > 0
                     for r in kb.labor_norms(activity_id))
        if not ok:
            # `L4_Norm_Default` 的人工审定量（第 7 批新增；口径同 `_labor_default_row`）
            d = kb.labor_norm_default(activity_id)
            ok = bool(d and _to_float(d.get("norm_value"), 0.0) > 0)
    except Exception:
        ok = False
    _HAS_NORM_CACHE[activity_id] = ok
    return ok


def _activity_name_conflict(task_name, info, candidates=None):
    """任务名与所绑活动名是否**明显不符**（契约 §3）→ 说明字符串或 None。

    判据（全部满足才算"明显不符"，宁可不拦也不错拦）：
      · 两边名字都有 ≥ 2 个字（拿不到活动名 / 任务名太短 → 不判）；
      · 任务名与所绑活动名的**最长连续共同片段** < `_NAME_OVERLAP_MIN`；
      · **同 L3 内确实存在一个明显更像的替代活动**（`candidates` 里有一条与任务名的
        连续共同片段 ≥ `_NAME_OVERLAP_MIN`）。

    第三条是"有没有更好的可换"这一现实判据，不是第四种名字相似度。为什么必需：
      · 契约 §3 两处确证错绑都有明确替代 —— `1.1.1 场地平整` 在同 L3 的机械活动里
        就有 `GD_A11_平整场地`（连续共同片段 4）；`7.1.3 暖通预留预埋` 同理；
      · 而 `室外给排水管网开挖` 对 `EARTH0030 挖地槽（沟）`：两者共同片段只有 1，
        但同 L3（earthwork）里**没有**任何活动与任务名的共同片段 ≥ 2 —— 也就是说
        "换一条"没有依据；此时按名字硬判不符，会把这条原本有定额的工序整条打成
        未绑定（实测 `test_中间量级机械人工两行都绑` 因此变红），违反"宁可不拦"。

    返回说明字符串（调用方据此先试改绑、改绑不到才降级未绑定）。
    """
    if not info:
        return None
    task_chars = _name_chars(task_name)
    activity_name = str(info.get("activity_name") or "")
    act_chars = _name_chars(activity_name)
    if len(task_chars) < 2 or len(act_chars) < 2:
        return None
    if _name_overlap(task_name, activity_name) >= _NAME_OVERLAP_MIN:
        return None
    # ⚠️ **必须**再加一条"同 L3 里确实有更像的替代"才判不符。名字层单独用会误伤
    # 中文任务名的**缩写**，而缩写在真实计划里是常态（实测重放，代价可量化）：
    #   · `铝模安装` 绑 `FORM_NEW_OTHER 其他模板`：连续共同片段 1。判不符 → 18 条铝模
    #     安装全丢定额（每条 7 天→14 天），总工期 665 → 787 天；
    #   · `ALC墙板安装` 绑 `LDT724_砌块墙`：同样（每条 7→3 天）。
    # 而"同 L3 有更像的替代"这条证据一加上，两者都不再误判（同 L3 里没有任何活动
    # 与"铝模安装/ALC墙板安装"共有 ≥2 字）。
    # 契约 §3 两处确证错绑仍然被拦住：
    #   · `1.1.3 暖通预留预埋` 绑 `HVAC_AI_001 风管制作安装`（共同片段 0，同 L3 有
    #     `EMB_*_001` 预留预埋类活动 → 判不符 ✔）；
    #   · `1.1.1 场地平整` 绑 `SPREP_AI_003 场地硬化`（共同片段 1，但现在该活动的 L3
    #     `site_prep` 里没有"平整场地"类替代 → 这一条**名字层拦不住**；它由口径关
    #     §2 与 WS6 的数据修复兜住，见交付报告"遗留"一节）。
    if candidates is None:
        return None
    # "更像的替代"要用**更严**的门槛（3 字而非 2 字）：`1-1层 铝模安装` 与同 L3 的
    # `FORM_ALU_INSTALL 铝合金模板安装` 恰好共有「铝模」2 字 —— 但那条活动在库里
    # **一行定额都没有**，拿它当"更好的替代"会把 18 条铝模安装从有定额（7 天）打成
    # 未绑定（14 天），总工期 +285 天。要求 3 字后这种"半像"的替代不再触发。
    # 替代还得是**真能用的替代**：候选活动必须自己有定额值（人工有正值行，或台班有行）。
    # 实测两类反例都靠这条排除：
    #  · `MASON_ALC_PANEL`（与任务同名，`Norm_Labor_Table`/`Norm_Equipment_Table` 全空）
    #    —— 不是"更像的替代"，而是"同一条且没数据"；
    #  · `FORM_ALU_INSTALL 铝合金模板安装`（共有「铝模」2 字、一行定额都没有）—— 同理。
    # 而 `EMB_HVAC_001 暖通预留预埋`（与任务同名、**有值**）是真替代 → 判不符 ✔。
    #
    # 判据本身用"**任务名整个就是候选名的一部分**"（或反之）：这比"共用 ≥3 字"严格得多，
    # 因为中文里"安装/墙板安装/沟盖板"这类通用构件词会造出大量 ≥3 字的假重合
    # （实测 `ALC墙板安装` 与 `混凝土沟盖板安装`/`窗台板安装` 都共有「板安装」3 字，
    # 但三者毫无关系）。整名包含才能说明"这条候选就是这个任务"。
    for a in candidates:
        alt = str((a or {}).get("activity_name") or "")
        alt_id = str((a or {}).get("activity_id") or "")
        alt_chars = _name_chars(alt)
        if not alt_chars or not _candidate_has_norm(alt_id):
            continue
        if task_chars in alt_chars or alt_chars in task_chars:
            return ("任务名「%s」与所绑活动 %s（%s）的工序名没有共同工序词"
                    % (task_name, info.get("activity_id") or "", activity_name))
    return None


# ==================== 换算参数（ctx）解析 ====================
# 单位换算需要工程参数（kb_units.CONTEXT_KEYS）。取值优先级（契约 §5-WS3④ + D5）：
#   ① 任务名 / 条件文本 / 工程量文本里的**显式数字**（如"桩长18m"）；
#   ② 项目参数键名（extracted_params / boundary_conditions）；
#   ③ boundary_conditions.materials；
#   ④ **定额行条件里的事实**（如「桩长18m以内」）—— D5 的"往数据库口径靠"，
#      限值/规格不是实测值，必须标 ctx_source='ai_estimate' 并进
#      by_reason='AI估算换算参数'（由 WS4 的覆盖率口径消费）；
#   ⑤ 推不出来 → 不带 ctx 去校验 → check_unit_pair 判 unusable（默认拒绝，不许 1:1）。
# ⚠️ D5 明令：**不得**再有任何写死的换算常量。原来的 `_AI_PILE_LENGTH_M = 18.0`
# 已删除，桩长改由 ④ 从定额行条件推定（re: 桩长18m以内 → 18×0.9 m/根）。

_PILE_LEN_RES = (
    re.compile(r"桩长\s*[=＝]?\s*(\d+(?:\.\d+)?)\s*(?:m|米)"),
    re.compile(r"(?:L|Ｌ)\s*[=＝]\s*(\d+(?:\.\d+)?)\s*(?:m|米)"),
)
_THICKNESS_RE = re.compile(
    r"(?:厚度|板厚|壁厚|墙厚|δ)\s*[=＝:：]?\s*(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)")
_DENSITY_RE = re.compile(r"(?:容重|密度)\s*[=＝:：]?\s*(\d+(?:\.\d+)?)")
_PIECE_WEIGHT_RE = re.compile(
    r"(?:单件重量|单重|每件重量)\s*[=＝:：]?\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)?")
_VOLUME_PER_PILE_RE = re.compile(
    r"(?:单根体积|桩体积|单桩体积)\s*[=＝:：]?\s*(\d+(?:\.\d+)?)\s*(?:m3|m³|方)?")

#: 项目参数（`extracted_params`）里可**直接**当换算 ctx 用的键名 → 正则。
#: 必须靠键名命中，因为 `_text_pool()` 只拼 value：参数名一旦写成 `wall_thickness_mm`
#: 这种不含"厚度"二字的英文键，值 200 就退化成裸数字，`_THICKNESS_RE` 匹配不上，
#: 于是"厚度明明填了却报缺参数"。
_PARAM_KEYS = (
    ("thickness_m", re.compile(r"(?:厚|thickness)", re.I)),
    ("volume_per_pile_m3", re.compile(r"(?:单根体积|单桩体积|pile_volume)", re.I)),
    ("density_t_per_m3", re.compile(r"(?:容重|密度|density)", re.I)),
    ("pile_length_m", re.compile(r"(?:桩长|pile_length)", re.I)),
)

_PARAM_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?")


def _ctx_from_text(text):
    """从一段文本里解析显式工程参数（只认明写的数字，不猜）。"""
    out = {}
    t = str(text or "")
    for rx in _PILE_LEN_RES:
        m = rx.search(t)
        if m:
            out["pile_length_m"] = float(m.group(1))
            break
    m = _THICKNESS_RE.search(t)
    if m:
        v = float(m.group(1))
        out["thickness_m"] = v / 1000.0 if m.group(2) in ("mm", "毫米") else (
            v / 100.0 if m.group(2) in ("cm", "厘米") else v)
    m = _DENSITY_RE.search(t)
    if m:
        out["density_t_per_m3"] = float(m.group(1))
    m = _PIECE_WEIGHT_RE.search(t)
    if m:
        out["unit_weight_kg_per_piece"] = float(m.group(1))
    m = _VOLUME_PER_PILE_RE.search(t)
    if m:
        out["volume_per_pile_m3"] = float(m.group(1))
    return out


def _param_length_scale(unit_text, key):
    """参数值的长度单位 → 米。**取不到明确单位就返回 None（不猜）**。

    `{"wall_thickness": 200}` 到底是 200mm 还是 200m，只有用户知道；猜错就是 1000 倍
    的工程量偏差。所以单位只能来自"值里写了"（`"200mm"`）或"键名里写了"
    （`wall_thickness_mm` / `wall_thickness_m`），两者都没有就弃用该参数。
    """
    u = str(unit_text or "").strip().lower()
    if not u:
        m = re.search(r"_(mm|cm|m|km)$", str(key or "").strip().lower())
        u = m.group(1) if m else ""
    return {"mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01,
            "m": 1.0, "米": 1.0, "km": 1000.0}.get(u)


def _ctx_from_params(ctx):
    """从 `extracted_params` / `boundary_conditions` 的**键名**取换算参数。

    与 `_ctx_from_text()` 同属①级（都是用户明写的数），只是这条路看的是参数名。
    仍然默认拒绝：认不出单位的厚度参数**不采用**（宁可报"缺厚度"）。
    """
    out = {}
    if not isinstance(ctx, dict):
        return out
    pools = [ctx.get("extracted_params"), ctx.get("boundary_conditions")]
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        for key, val in pool.items():
            if isinstance(val, bool) or val is None:
                continue
            for ctx_key, rx in _PARAM_KEYS:
                if ctx_key in out or not rx.search(str(key)):
                    continue
                m = _PARAM_NUM_RE.search(str(val))
                if not m:
                    continue
                num = float(m.group(1))
                if ctx_key == "density_t_per_m3":
                    out[ctx_key] = num
                    continue
                scale = _param_length_scale(m.group(2), key)
                if scale is None:
                    continue
                out[ctx_key] = num * scale
    return out


def _materials_text(ctx):
    """boundary_conditions 的 materials / equipment 汇成一段文本（② 级来源）。

    只用于"看看用户有没有明写桩长/厚度/容重"，材料**数量**（120 根）不进 ctx ——
    量来自叶子的 quantity，不重复造数。
    """
    bc = (ctx or {}).get("boundary_conditions")
    if not isinstance(bc, dict):
        return ""
    parts = []
    for key in ("materials", "equipment"):
        items = bc.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                parts.append("%s %s" % (it.get("name") or "", it.get("spec") or ""))
            elif it:
                parts.append(str(it))
    return " ".join(parts)


_PILE_LEN_COND_RE = re.compile(r"桩长\s*([≤≥＜＞<>=]?)\s*(\d+(?:\.\d+)?)\s*(?:m|米)")
_PILE_DIAM_RE = re.compile(r"[φΦ]\s*(\d+(?:\.\d+)?)")
#: 「桩长Xm以内」的**上界**：限值本身就是该档位的代表桩长（表头即"桩长 18m 以内"
#: 这一档），取用限值本身；不做"打折猜一个更短的值"—— 猜短只会让台班数偏多、
#: 工期偏保守，而库里给的限值是**唯一有据**的数。比例刻意留成显式常量，
#: 将来要改成"取 0.9 倍限值"只动这一处。
_PILE_LEN_LIMIT_RATIO = 1.0
#: 方桩/管桩截面按此截面形式折单根体积（(边/径)² × π/4 × 桩长）
_SQUARE_OR_ROUND_FACTOR = 3.141592653589793 / 4.0


def _band_of(fu, tu):
    """该族对是否跨族换算、以及它的量级合理带。返回 `(param, lo, hi, unit, label)` 或 None。"""
    fu, tu = kb_units.unit_family(fu), kb_units.unit_family(tu)
    if not fu or not tu or fu == tu:
        return None
    return _MAGNITUDE_BANDS.get((fu, tu))


def _band_reject(ctx, fu, tu, source):
    """D5 量级校验之一：**换算参数本身**是否离谱。离谱 → 返回拒绝说明（str），否则 ""。"""
    band = _band_of(fu, tu)
    if not band:
        return ""
    param, lo, hi, unit, label = band
    val = _to_float((ctx or {}).get(param), 0.0)
    if val <= 0:
        return ""
    if val < lo or val > hi:
        return ("换算参数「%s」= %g %s 超出合理带 %g~%g %s（来源：%s）"
                % (param, val, unit, lo, hi, unit, source or "未标注"))
    return ""


def _estimate_ctx_from_row_condition(need, row_condition, text):
    """D5 第三级取值：从**定额行的条件文本**推定换算参数（数据库口径内的推定）。

    只认"这条定额自己写的条件"——`桩长18m以内` / `桩径φ400` 都是库里的事实，
    但它们是**限值/规格**、不是实测值，所以：
      · 取用时按 `_PILE_LEN_LIMIT_RATIO` 落在限值内，且必须标 `ai_estimate`；
      · 推不出来（条件里没写）→ 返回空，由调用方按「推定不出就报缺」处理
        （`usable=False` + `not_usable_reason`），**绝不再有写死的 18 m 兜底**。
    """
    cond = str(row_condition or "")
    src_text = _append_note(cond, text)
    if need == ["pile_length_m"]:
        m = _PILE_LEN_COND_RE.search(cond)
        if not m:
            return None
        limit = float(m.group(2))
        op = m.group(1) or ""
        value = limit * _PILE_LEN_LIMIT_RATIO if op in ("≤", "＜", "<", "≤", "") else limit
        if value <= 0:
            return None
        note = ("桩长按定额行的适用条件「桩长%s%s」推定（限值 %g m × %g = %g m/根）："
                "定额条件的限值不是实测桩长，故标 ctx_source='ai_estimate'"
                % (m.group(2), "以内" if op in ("≤", "＜", "<", "") else "以外",
                   limit, _PILE_LEN_LIMIT_RATIO, value))
        return {"pile_length_m": round(value, 6)}, "ai_estimate", note
    if need == ["volume_per_pile_m3"]:
        m = _PILE_LEN_COND_RE.search(cond)
        if not m:
            return None
        size_mm = _to_float((_PILE_DIAM_RE.search(src_text) or [None, 0])[1], 0.0)
        if size_mm <= 0:
            return None
        length = float(m.group(2))
        vol = (size_mm / 1000.0) ** 2 * _SQUARE_OR_ROUND_FACTOR * length
        if vol <= 0:
            return None
        note = ("单根体积按定额行条件推定：截面 %g mm + 桩长 %g m → %g²×π/4×%g = %g m³/根，"
                "标 ctx_source='ai_estimate'" % (size_mm, length, size_mm / 1000.0,
                                                 length, round(vol, 6)))
        return {"volume_per_pile_m3": round(vol, 6)}, "ai_estimate", note
    return None


def _resolve_convert_ctx(leaf_unit, norm_unit, text, ctx=None, row_condition=None):
    """为「叶子单位 → 定额分母」的换算出 ctx。

    返回 `(ctx_dict, source, note)`：source ∈ {'text','materials','ai_estimate',''}。
    同族（或任一为空）不需要参数 → `({}, '', '')`；推不出来 → `({}, '', '')`，
    由 `check_unit_pair` 判 unusable —— 调用方不得再按 1:1 处理。

    取值优先级（契约 §5-WS3④ + D5）：
      ① 文本里的显式数字 → ①' 项目参数键名 → ② boundary_conditions.materials
      → ③ **定额行条件里的事实**（`row_condition`，如「桩长18m以内」）标 `ai_estimate`
      → ④ 都没有 → 空 ctx（调用方按 D5「推定不出就报缺」处理）。

    D5 方向是单向的：分母（`norm_unit`）是**数据库口径**，永远往它靠；
    这里只负责把参数凑出来，不反过来怀疑定额分母。
    """
    fu = kb_units.normalize_unit(leaf_unit)
    den = kb_units.denominator_of(norm_unit)
    if not fu or not den or kb_units.unit_family(fu) == kb_units.unit_family(den):
        return {}, "", ""
    fam = (kb_units.unit_family(fu), kb_units.unit_family(den))
    if "count:根" in fam and "length" in fam:
        need = ["pile_length_m"]
    elif "count:根" in fam and "volume" in fam:
        # 截（凿）桩头这类：叶子按「根」计量，定额分母是 m³（每根多大体积要用户给）。
        need = ["volume_per_pile_m3"]
    elif "mass" in fam and "volume" in fam:
        need = ["density_t_per_m3"]
    elif "area" in fam and "volume" in fam:
        need = ["thickness_m"]
    else:
        return {}, "", ""
    got = _ctx_from_text(text)
    if all(k in got for k in need):
        return {k: got[k] for k in need}, "text", "换算参数来自任务/条件文本里的显式数字"
    # ①'：项目参数（extracted_params）按**键名**取值 —— 值经 `_text_pool()` 拼进去后
    # 往往已与参数名分离（"200"），只有按键名找才拿得回来。
    got = _ctx_from_params(ctx)
    if all(k in got for k in need):
        return ({k: got[k] for k in need}, "text",
                "换算参数来自项目参数（extracted_params/boundary_conditions）")
    got = _ctx_from_text(_materials_text(ctx))
    if all(k in got for k in need):
        return {k: got[k] for k in need}, "materials", "换算参数来自 boundary_conditions.materials"
    # ③ 定额行条件里的事实（D5：往数据库口径靠；限值/规格 → ai_estimate 留痕）
    est = _estimate_ctx_from_row_condition(need, row_condition, text)
    if est:
        return est
    return {}, "", ""


def _set_usable(binding, ok, reason=""):
    """统一写「这条定额能不能作为依据」的三个标记（WS4 的覆盖率口径消费它们）。"""
    binding["norm_is_evidence"] = bool(ok)
    binding["usable"] = bool(ok)
    binding["not_usable_reason"] = "" if ok else (reason or "定额不可用")


def _shift_unit_from_kb(unit, kb_quantity_unit):
    """机械台班定额单位 = 「台班 / <KB 行的 quantity_unit>」。

    KB 缺 `quantity_unit` → 返回 `""`（分母缺失，调用方必须降级）。**绝不**用叶子单位
    补分母：旧实现 `台班`→`台班/根` 之后又拿这个分母自证单位一致，120 根 PHC 桩因此
    被算成 1 天（KB 真实分母是 m，0.49 台班/100m）。
    """
    u = kb_units.normalize_unit(unit) or "台班"
    if "/" in u:
        return u                     # KB json 已写明分母（本身也是 KB 数据）
    den = kb_units.normalize_unit(kb_quantity_unit)
    if not den:
        return ""
    return "%s/%s" % (u, den)


# ==================== 口径关（契约 §2-A1~A5） ====================
# 为什么需要：定额只写单位（m²/m/m³）不写"这个 m² 是哪张面积"。于是"风管展开面积"
# 的 0.35 工日/m² 被乘上"建筑面积 14200 m²"，两边都是 m²，单位校验放行，
# 误差约 5 倍且**无声**。`unit_family` 相等**不等于**口径相同 —— 这正是要堵的洞。
_REASON_SCOPE_UNALIGNED = "口径无法对齐"
_REASON_BINDING_CONFLICT = "活动绑定不一致"
#: 第 7 批：工作包级占位叶子（3 段 id ∧ 无 KB 活动）不许用单位盲的经验产能反算工期。
_REASON_WP_PLACEHOLDER = "工作包级占位叶子无 KB 活动，沿用 WBS 目标工期"


def _is_work_package_leaf(leaf):
    """是不是「工作包级的 3 段 id 叶子」（如 `1.1.1 场地平整` / `1.2.2 定位放线`）。

    判据与 `beat_node._stamp_non_beat_layer_fields()` 的注释口径一致："工作包级的
    3 段 id 叶子"（节拍展开出来的真 L4 是 4 段以上，如 `6.1.1.1.1 ALC墙板安装`）。
    空 id / 取不到 id → **不判它是工作包级**（宁可不动，也不误伤）。
    """
    leaf_id = str((leaf or {}).get("id") or "").strip()
    if not leaf_id:
        return False
    return len([p for p in leaf_id.split(".") if p.strip()]) <= 3


def _known_scope(value):
    """受控词表内的 `measure_scope`；表外写法 / 空 → `''`（当"未填"）。

    契约 §1 的词表是受控的。表外写法（WS6 迁移填错）宁可当"未填"（不阻断），
    也不要拿它去判"不一致"把整条工序卡死 —— 那是可修的数据问题，逐条阻断代价太大。
    """
    scope = kb_units.normalize_measure_scope(value)
    return scope if scope in kb_units.MEASURE_SCOPES else ""


def _scope_warning_text(task_name, leaf, task_scope, norm_scope, quantity, unit, note):
    """`leaf["wbs_warnings"]` 里的单条可读警告（纯函数，便于测试直接断言）。"""
    return ("[口径] %s（%s）工程量 %s %s：任务口径「%s」/ 定额口径「%s」—— %s"
            % (task_name, (leaf or {}).get("id") or "?", quantity, unit,
               task_scope or "未填", norm_scope or "未填", note))


def _scope_is_dimensional(scope):
    """该计量对象是不是"有量纲"的（体积 / 建筑面积 / 管道长度…）。

    `项 / 批 / 组 / 台数 / 件数 / 桩根数 / 自然单位` 这类是**计数/包装**单位，
    不是"哪张面积"意义上的计量对象 —— `kb_units.unit_family()` 对它们返回 `count:*`。
    只用来实现 `_task_measure_scope` 里那条"包装单位不优先于活动自己声明的有量纲口径"
    的兜正，不参与任何判定。
    """
    scope = _known_scope(scope)
    if not scope:
        return False
    return not kb_units.unit_family(scope).startswith("count:")


def _task_measure_scope(task, binding=None, activity_id=None, text=""):
    """任务的计量对象 `task_scope`（契约 §2 第 2 条）。

    ⚠️ **函数名与候选顺序是对外契约**：`resource.py` 的 `_task_measure_scope()`
    按**同一顺序**解析同一个任务。两边顺序一旦分叉，同一个任务在绑定阶段与资源阶段
    会得出不同口径，`measure_scope_state` 就会**静默**误判。顺序固定为：

      ① `binding["task_measure_scope"]`（本函数上一次的结论，幂等；WS2 读的第一顺位）
      ② `task["measure_scope"]`（WBS 叶子/任务的显式声明）
      ③ `kb.activity_measure_scope(kb_activity_id)` 兜底（活动即任务的语义）

    本文件内部额外支持的 ②' 是"任务文本里与受控词表逐词匹配的显式写法"
    （"建筑面积 14200 m²"）—— 它插在 ② 与 ③ 之间，只用于**首次**解析时把用户明写的
    口径取回来；WS2 那边没有文本池，所以它不参与顺序契约（③ 只在前两步都取不到时才跑）。

    取不到 → `''`（口径未确认，按 §2 第 5 条处理，**不阻断**）。
    返回 `(scope, 来源标记)`。
    """
    b = binding if isinstance(binding, dict) else {}
    aid = activity_id or (task or {}).get("kb_activity_id")
    act_scope = ""
    if aid:
        try:
            act_scope = _known_scope(kb.activity_measure_scope(aid))
        except Exception:
            act_scope = ""
    scope = _known_scope(b.get("task_measure_scope"))
    if scope:
        # ⑤ 兜正：`项 / 批 / 组` 这类"包装单位"不是计量对象，只是某处文本里出现过它
        # （实测：叶子 `unit=项` 的任务被判成 task_scope='项'，而台班定额口径是"体积"
        #  → 口径关把一个 3500 m³ 的正常土方任务整条打成"口径无法对齐"）。
        # 任务的活动本身有**有量纲**的口径时，以它为准，不要把包装单位当成口径冲突。
        if not (_scope_is_dimensional(scope) is False
                and _scope_is_dimensional(act_scope)):
            return scope, str(b.get("task_scope_source") or "binding")
    scope = _known_scope((task or {}).get("measure_scope"))
    if scope:
        return scope, "leaf"
    text = str(text or "")
    # ②' 文本里的"显式声明"：必须带声明语境（`建筑面积 14200 m²` / `口径：风管展开面积`），
    # 不能见到"项"字就当成"项"这个计量对象 —— 实测 prompt 里的「某项目」会把一条
    # 3500 m³ 的正常土方任务判成 task_scope='项'，进而与定额口径"体积"冲突、
    # 整条工序被打成"口径无法对齐"（3 个既有用例因此变红）。
    for candidate in kb_units.MEASURE_SCOPES:
        if not candidate:
            continue
        for marker in ("口径", "计量对象", "工程量"):
            if re.search(r"%s\s*[:：=＝]?\s*%s" % (marker, re.escape(candidate)), text):
                return candidate, "text"
    if act_scope:
        # ③ 活动字典兜底**优先于**宽松的词表扫描：活动自己的口径是库里的确定信息，
        # 而"文本里出现过某个词"只是线索。
        return act_scope, "activity"
    for candidate in kb_units.MEASURE_SCOPES:
        if candidate in text and _scope_is_dimensional(candidate):
            return candidate, "text"
    return "", ""


# ==================== 定额行 → 统一口径 ====================
def _row_condition_text(row):
    """定额行的条件文本（condition_text + condition_combination）。

    只用于 `kb_units.is_thickness_tiered()` 判断"这条定额是不是按厚度分层的墙体定额"，
    **不**从里面取厚度值（取值就是编造，见 kb_units.DEFAULT_WALL_THICKNESS_M 的说明）。
    """
    if not isinstance(row, dict):
        return ""
    parts = [str(row.get("condition_text") or "")]
    comb = row.get("condition_combination")
    if isinstance(comb, str):
        parts.append(comb)
    elif isinstance(comb, dict):
        parts.append(json.dumps(comb, ensure_ascii=False))
    return " ".join(p for p in parts if p)


#: 厚度档位的写法：`>200mm` / `≤200mm` / `墙体厚度≤150mm` / `厚0.2m`。
_TIER_RE = re.compile(
    r"(?:厚度|板厚|壁厚|墙厚|δ)?\s*([≤≥＜＞<>=]+)?\s*(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)")
_TIER_SCALE = {"mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01, "m": 1.0, "米": 1.0}
_TIER_OPS = {"≤": "le", "<": "lt", "＜": "lt", "≥": "ge", ">": "gt", "＞": "gt", "=": "eq"}

#: 砌块材料别名：任务里写「ALC」= 蒸压加气混凝土，KB 行写「加气混凝土砌块」——
#: 没有这张表，同档位的 3 行（加气 / 空心 / 陶粒）就只能按 KB 行序瞎取一个。
_MATERIAL_ALIASES = (
    ("加气", ("加气", "ALC", "蒸压加气", "加气块", "轻质砌块")),
    ("陶粒", ("陶粒",)),
    ("空心", ("空心",)),
)


def _thickness_tier_of(text):
    """从定额条件里解析"厚度档位" → `(op, value_m)`；没有档位返回 None。

    `op` ∈ {"le","lt","ge","gt","eq"}。只**读档位**（"≤200mm" 表示这一行适用于
    厚度不超过 200mm），绝不把它当项目墙厚 —— 那是编造，见 `kb_units`。
    """
    m = _TIER_RE.search(str(text or ""))
    if not m:
        return None
    op = _TIER_OPS.get(m.group(1) or "")
    if not op:
        return None
    return op, float(m.group(2)) * _TIER_SCALE.get(m.group(3), 1.0)


def _tier_contains(tier, thickness_m):
    """假定/实测厚度是否落在该档位内。"""
    op, v = tier
    if op == "le":
        return thickness_m <= v + 1e-9
    if op == "lt":
        return thickness_m < v - 1e-9
    if op == "ge":
        return thickness_m >= v - 1e-9
    if op == "gt":
        return thickness_m > v + 1e-9
    return abs(thickness_m - v) <= 1e-9


def _material_score(row, text):
    """这一行的**材料**在任务文本里有没有对应写法（命中越具体分越高，没有 → 0）。"""
    cond = str((row or {}).get("condition_text") or "")
    hay = str(text or "").upper()
    best = 0
    for key, aliases in _MATERIAL_ALIASES:
        if key not in cond:
            continue
        for alias in aliases:
            if alias.upper() in hay:
                best = max(best, len(alias))
    return best


def _needs_assumed_thickness(leaf_unit, norm_unit):
    """这条绑定的换算是不是"只缺墙厚"（面积 ↔ 体积）。"""
    den = kb_units.denominator_of(norm_unit)
    if not den:
        return False
    return kb_units.needed_context_keys(leaf_unit, den) == ["thickness_m"]


def _pick_row_by_assumed_thickness(cands, thickness_m, text):
    """按**厚度档位**选定额行：返回 `(row, 说明)`；没有可用档位 → `(None, "")`。

    为什么需要：同一条活动（实测 LDT724_砌块墙）有 6 行 = 2 档厚度 × 3 种材料。用户没提
    条件时原逻辑取 `typical_labor_norm` 的中位行（实测取到「混凝土空心砌块，>200mm」），
    而面积→体积用的墙厚假定是 200mm —— 落的是 **≤200mm** 档。档位与假定值不自洽，
    同一个项目里「ALC 墙板」与「砌块墙」（`beat_configs._calc_block_wall` 按 0.2 m 折算）
    就会得出两个墙厚口径。

    选法（全部确定性，逐条可复现）：
      ① 逐行解析厚度档位，只留**含该厚度**的行；
      ② 同档多材料 → 按任务文本里的材料写法消歧（ALC → 加气混凝土砌块）；
      ③ 还是分不出 → 按 KB 行序取首个（不猜、可复现）。
    """
    tiered = [(r, _thickness_tier_of(_row_condition_text(r))) for r in (cands or [])]
    tiered = [(r, t) for r, t in tiered if t]
    if not tiered:
        return None, ""
    fit = [(r, t) for r, t in tiered if _tier_contains(t, thickness_m)]
    if not fit:
        return None, ""
    scored = sorted(fit, key=lambda p: -_material_score(p[0], text))
    head = scored[0][0]
    desc = "「%s」（%s，%s 工日/单位）" % (
        head.get("condition_text") or "", head.get("norm_id") or "未标行号",
        head.get("norm_value"))
    others = "、".join(str(r.get("condition_text") or "") for r, _t in fit if r is not head)
    if _material_score(head, text) > 0:
        why = ("定额档位：墙厚 %gmm 落在 %s —— 同档另有 %d 行（%s），按任务文本里的材料写法选定"
               % (thickness_m * 1000.0, desc, len(fit) - 1, others))
    else:
        why = ("定额档位：墙厚 %gmm 落在 %s —— 同档 %d 行认不出材料写法，按 KB 行序取首个"
               % (thickness_m * 1000.0, desc, len(fit)))
    return head, why


def _labor_norm_of(row, note_extra="", source_note=""):
    """把一行 KB 人工定额统一成 (norm_value 工日/单位, unit, basis, note)。

    KB 里 norm_value=labor_norm_value 是「工日 / 1×quantity_unit」（**已归一**，
    留档不变式 raw_value / raw_quantity_basis == norm_value），productivity_value
    是「单位/工日」，两者互为倒数：**产能 = 1 / norm_value**。
    这里优先用 norm_value，拿不到才用 1/productivity_value 反推，
    `basis`（= raw_quantity_basis 的兼容别名）只作**溯源**，调用方不得拿它做乘法。
    """
    basis = _to_float(row.get("quantity_basis"), 1.0) or 1.0
    # kb.labor_norms 把 labor_norm_unit 映射成 norm_unit；两种键名都兼容
    unit = row.get("norm_unit") or row.get("labor_norm_unit") or ""
    if not unit:
        unit = "工日/" + (row.get("quantity_unit") or "单位")
    nv = _to_float(row.get("norm_value"), 0.0)
    note = note_extra
    if nv > 0:
        note = _append_note(note, "口径：KB 定额值 labor_norm_value（工日/单位）" + (source_note or ""))
        return nv, unit, basis, note
    pv = _to_float(row.get("productivity_value"), 0.0)
    if pv > 0:
        note = _append_note(note, "口径：由 KB 产能 productivity_value 取倒数（%s 单位/工日）" % pv)
        unit = "工日/" + (row.get("quantity_unit") or "单位")
        return 1.0 / pv, unit, basis, note
    return 0.0, unit, basis, note


# 机械定额行的"选行"口径（第 37 轮追加；父代理用真实计划实测 CONC_NEW_FOUND 选错行）：
#   ① **第一优先是主控机械名**：只有 `machine_combination_json` 里真的含
#      `Activity_Main_Machine.machine_name` 的行，才有资格提供这台机械的台班定额；
#   ② 同名多行（夯实机：平地 5.53 / 槽坑 7.18；压桩机：φ300/φ400/φ500…）才用
#      `condition_text` 消歧 —— KB 主控机械行自带的 condition_text 优先，其次叶子条件字段；
#      拿不到条件信号就保持 KB 行序（确定性，不改变既有口径）；
#   ③ 主控机械**不在任何行**里 → 返回 None，**绝不**"暂用同行机械"借定额：
#      旧实现借 `rows[0]` 的首台机械，CONC_NEW_FOUND 因此选中"后浇带／振捣器 1.26 台班"
#      （266 m³ → 33.5 台班 → 34 天/段）；正确行是 NE_CONC_002 泵车 0.055 台班/10m³
#      （266 m³ → 1.46 台班 → 2 天/段）。
_MACHINE_CONDITION_MIN_LEN = 2


def _machine_match_indices(row, machine_name):
    """该行 `machine_combination_json` 里与 machine_name 匹配的下标。

    KB 机械名常带规格后缀（"混凝土输送泵车" vs "混凝土输送泵车 90m³/h"），双向包含即可。
    """
    if not row or not machine_name:
        return []
    machines = [str(x) for x in _json_list(row.get("machine_combination_json"))]
    return [i for i, m in enumerate(machines)
            if machine_name == m or machine_name in m or m in machine_name]


def _machine_name_at(row, index):
    """该行第 index 台机械的名字（越界返回 ""）。"""
    machines = [str(x) for x in _json_list(row.get("machine_combination_json"))]
    return machines[index] if 0 <= index < len(machines) else ""


def _machine_norm_at(row, index):
    """取该行第 index 台机械的台班定额。返回 (台班数, 单位, 基准, 机械名)。"""
    basis = _to_float(row.get("quantity_basis"), 1.0) or 1.0
    shifts = _json_list(row.get("machine_shift_norm_json"))
    units = [str(x) for x in _json_list(row.get("machine_shift_unit_json"))] or ["台班"]
    shift = _to_float(shifts[index] if index < len(shifts) else None, 0.0)
    unit = units[index] if index < len(units) else (units[0] if units else "台班")
    return shift, unit, basis, _machine_name_at(row, index)


def _condition_terms(text):
    """条件串切词：按 / 、 , ， ; ； 与空白切分，丢掉空串与单字。

    额外丢掉**纯数字/纯符号**片段（`18`、`≤100mm` 这类切出来的残渣）。为什么：
    条件消歧会拿条件词的字符重合度给候选打分，而任务名里的型号串（`PHC-A400-95`）
    与「桩长**18**m以内」这类条件会隔着分隔符共享 `18`/`400` 的数字片段，
    于是 φ400 行被误判成"更贴"（实测把 φ300 的正确行挤掉，120 根 PHC-A400 台班算错）。
    真正能定生死的条件词（平地/槽坑/加气/空心）都带汉字，滤掉数字不影响它们。
    """
    parts = re.split(r"[/、,，;；\s]+", str(text or ""))
    return [t for t in (p.strip() for p in parts)
            if len(t) >= _MACHINE_CONDITION_MIN_LEN
            and any("\u4e00" <= ch <= "\u9fff" for ch in t)]


def _pick_machine_row(rows, machine_name, condition_text="", leaf_condition="",
                      task_name=""):
    """按**主控机械名**选台班定额行（同名多行才用条件消歧）。

    返回 `(row, index, hit_name)`；主控机械不在任何行 → `(None, None, "")`，
    调用方必须降级为"仅参考"，**不得**借用同行其它机械的定额。

    消歧顺序（第 41 轮补了第 ③ 步）：
      ① 条件整串互相包含（叶子条件 ↔ 行条件）；
      ② 两边条件词重合最多；
      ③ 行条件词出现在**任务名**里的个数最多（同等才保持 KB 行序）。
    ③ 是必要的：实测 `4.1.1.3 混凝土浇筑`（主控机械=混凝土振捣器）有三行
    「后浇带 / 基础浇筑（振捣器 0.77）/ 基础浇筑（泵车 0.055）」，前两步都判不出，
    按行序会选中 **后浇带**（1.26 台班/10m³ → 180 m³ 算成 23 天）；
    而任务名里的「浇筑」与 `基础浇筑` 行重合 → 第 ③ 步纠正到基础浇筑。
    """
    if not rows or not machine_name:
        return None, None, ""
    cands = []
    for row in rows:
        for i in _machine_match_indices(row, machine_name):
            cands.append((row, i))
    if not cands:
        return None, None, ""
    if len(cands) > 1:
        key = (condition_text or "").strip() or (leaf_condition or "").strip()
        if key:
            for row, i in cands:                        # ① 条件整串互相包含
                ct = row.get("condition_text") or ""
                if ct and (key in ct or ct in key):
                    return row, i, _machine_name_at(row, i)
            terms = _condition_terms(key)               # ② 条件词重合，取最高分
            best, best_score = None, 0
            for row, i in cands:
                row_terms = _condition_terms(row.get("condition_text"))
                score = sum(1 for t in terms
                            if any(t in rt or rt in t for rt in row_terms))
                if score > best_score:
                    best, best_score = (row, i), score
            if best is not None:
                row, i = best
                return row, i, _machine_name_at(row, i)
        if task_name:                                   # ③ 行条件词被任务名**明写**
            # 判据：行条件里某个**汉字片段**（连续 ≥2 个汉字）原样出现在任务名里。
            # 为什么必须"汉字片段包含"而不是"字符重合度"：任务名里的型号串会让数字
            # 骗到分数 —— `桩径φ400` 与 `PHC-A400-95` 的最长连续共同片段是 3，
            # 一条 120 根 PHC-A400 的桩任务于是被选到 φ400 行（0.58 台班/100m），
            # 台班比正确的 φ300（0.49）多 18%。
            #  · 桩行：`桩径φ300` 的汉字片段只有 `桩径`，`桩长18m以内` 只有 `桩长`，
            #    两者都不在任务名里 → 保持 KB 行序（期望值 10.6 的来源）；
            #  · 混凝土：`基础浇筑` 的汉字片段有 `基础浇筑`/`浇筑`，而 `浇筑` 出现在
            #    任务名「Ⅰ区 2.5-2层 混凝土浇筑」里 → 纠正掉「后浇带」那一行的误选。
            task = str(task_name)
            best, best_score = None, 0
            for row, i in cands:
                ct = str(row.get("condition_text") or "")
                if not ct:
                    continue
                if ct in task or task in ct:
                    return row, i, _machine_name_at(row, i)
                score = 0
                for term in _condition_terms(ct):
                    for piece in re.findall(r"[\u4e00-\u9fff]{2,}", term):
                        for n in range(len(piece), 1, -1):
                            for s in range(0, len(piece) - n + 1):
                                if piece[s:s + n] in task:
                                    score = max(score, n)
                                    break
                            if score >= n:
                                break
                        if score >= _REBIND_OVERLAP_MIN:
                            break
                if score > best_score:
                    best, best_score = (row, i), score
            if best is not None and best_score >= _REBIND_OVERLAP_MIN:
                row, i = best
                return row, i, _machine_name_at(row, i)
    row, i = cands[0]                                   # 无条件信号 → KB 行序
    return row, i, _machine_name_at(row, i)


def _machine_norm_of(row, machine_name):
    """从一行机械定额里取主控机械的台班定额。

    返回 (norm_value 台班/基准, 单位, basis, 命中机械名或 "")：
    在 machine_combination_json 里找 machine_name 对应的下标，取同下标的台班数；
    找不到该机械 → norm_value=0（由调用方决定是否降级）。
    """
    if not row:
        return 0.0, "台班", 1.0, ""
    units = [str(x) for x in _json_list(row.get("machine_shift_unit_json"))] or ["台班"]
    basis = _to_float(row.get("quantity_basis"), 1.0) or 1.0
    if not machine_name:
        return 0.0, units[0], basis, ""
    for i in _machine_match_indices(row, machine_name):
        shift, unit, _basis, name = _machine_norm_at(row, i)
        if shift > 0:
            return shift, unit, _basis, name
    return 0.0, units[0], basis, ""


# ==================== 节点 ====================
# 进度行里的"命中方式"：`match_type` 是内部枚举（exact/default/ai），
# 直接打给用户看就是天书；这里换成一句话（与终端 /sources 的口径一致）。
_MATCH_TEXT = {
    "exact": "（定额里精确命中）",
    "default": "（定额里取典型值）",
    "ai": "（定额里没有，是模型估的）",
    "unbound": "（活动绑定不一致，已降级）",
}


class NormBindNode(BaseNode):
    name = "norm_bind"
    title = "定额锚定"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm          # 可为 None；为 None 时完全走代码路径，不联网
        self._mode_cache = {}   # activity_id -> recommended_production_mode

    # ---------------- LLM 可用性 / 惰性创建 ----------------
    def _llm(self):
        """惰性创建 LLMClient —— 只有真的要调 LLM 时才会走到这里。"""
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    @property
    def llm_usable(self):
        """只有"显式注入了 LLM 客户端"才算可用。

        刻意**不**回退到 config.LLM_API_KEY：本节点契约要求 self.llm is None 时
        完全走代码路径、绝不联网（也能让测试在配了 key 的机器上依然离线可跑）。
        """
        try:
            return callable(getattr(self.llm, "chat_json", None))
        except Exception:
            return False

    # ---------------- 入口 ----------------
    def run(self, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        wbs = ctx.get("wbs") or {}
        leaves = list(iter_leaves(wbs))
        total = len(leaves)

        self.emit("node_progress", {"node": self.name, "progress": 5,
                                    "message": "开始给 %d 条工序配消耗量定额" % total})

        scope_map = self._kb_scope_map(ctx)
        # 工程量的真实来源（llm / template / 未标注）—— 决定新建 quantity 溯源的口径
        qty_source = self._wbs_quantity_source(ctx)
        bindings, warnings, sources = {}, [], []

        for idx, (leaf, pname, wp_name) in enumerate(leaves, 1):
            task_id = str(leaf.get("id") or "")
            task_name = leaf.get("name") or pname
            binding, leaves_warn = self._bind_one(ctx, leaf, task_name, wp_name, scope_map)
            leaf["norm_binding"] = binding
            leaf["provenance"] = self._provenance_of(leaf, binding, task_id, qty_source)
            if task_id:
                bindings[task_id] = binding
            warnings.extend(self._as_warnings(leaves_warn, leaf, task_name))
            code = binding.get("source_code") or ""
            if code and code not in sources:
                sources.append(code)

            if total:
                self.emit("node_progress", {
                    "node": self.name,
                    "progress": 5 + int(90 * idx / total),
                    "message": "已套定额 %d/%d 条：%s %s" % (
                        idx, total, task_id or "?",
                        _MATCH_TEXT.get(str(binding.get("match_type") or ""), "")),
                })

        # 警告按影响降序（有影响天数的排前面，同序保持稳定）
        warnings.sort(key=lambda w: w.get("_impact_days", 0.0), reverse=True)
        for w in warnings:
            w.pop("_impact_days", None)

        ctx["norm_bindings"] = bindings
        ctx["norm_warnings"] = warnings
        ctx["credibility"] = self._credibility(ctx, leaves)
        ctx["data_sources"] = self._data_sources(ctx, sources)

        n_ai = sum(1 for b in bindings.values() if b.get("match_type") == "ai")
        n_exact = sum(1 for b in bindings.values() if b.get("match_type") == "exact")
        # 「机械错绑已改绑」对用户是有价值的信息（说明系统自己纠正了张冠李戴的定额），
        # 但 norm_warnings 目前没有进 plan_json —— 至少在节点完成摘要里露出来。
        n_reanchor = sum(1 for w in warnings if w.get("kind") == "reanchor")
        self.done_summary = ("已给 %d 条工序套上消耗量定额：%d 条在定额里精确命中、"
                             "%d 条由模型估算、%d 条需要注意"
                             % (total, n_exact, n_ai, len(warnings)))
        if n_reanchor:
            self.done_summary += " · 其中有 %d 条机械用错了、已自动改绑" % n_reanchor
        # 这一行**不再**复用 done_summary：否则屏幕上相邻两处一字不差说同一句
        # （用户实测："太冗余了"）。
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "定额都套完了，正在汇总来源"})

        # 只在 ctx 里改我们负责的四个键；wbs 是原地改的（叶子挂字段）
        return {"norm_bindings": bindings, "norm_warnings": warnings,
                "credibility": ctx["credibility"], "data_sources": ctx["data_sources"]}

    # ---------------- kb_scope（上游可选产物） ----------------
    def _kb_scope_map(self, ctx):
        """把 ctx['kb_scope'] 归一成 {task_id: {...}}；没有或不合法返回 {}。"""
        raw = ctx.get("kb_scope")
        if isinstance(raw, dict):
            # 支持 {"5.1.1.1": {...}} 或 {"items": [...]} 两种形态
            items = raw.get("items") if isinstance(raw.get("items"), list) else None
            if items is not None:
                out = {}
                for it in items:
                    if isinstance(it, dict) and it.get("task_id"):
                        out[str(it["task_id"])] = it
                return out
            return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        if isinstance(raw, list):
            out = {}
            for it in raw:
                if isinstance(it, dict) and it.get("task_id"):
                    out[str(it["task_id"])] = it
            return out
        return {}

    # ---------------- 单条叶子的锚定 ----------------
    def _bind_one(self, ctx, leaf, task_name, wp_name, scope_map):
        """返回 (norm_binding dict, 警告列表)。任何异常都不得向外抛。"""
        task_id = str(leaf.get("id") or "")
        activity_id = leaf.get("kb_activity_id")
        leaf_unit = leaf.get("unit") or ""
        warnings = []

        # 机械主导判定（也决定 mode）
        mode = "labor"
        info = self._activity_info(activity_id) if activity_id else None
        if info and info.get("recommended_production_mode") == "equipment_driven":
            mode = "machine"

        binding = {
            "task_id": task_id,
            "mode": mode,
            "norm_value": None,
            "unit": "",
            "condition_text": "",
            "quantity_basis": 1.0,       # 仅溯源；labor 侧不参与乘法（机械侧由 WS4 乘）
            "source_code": "",
            "match_type": "ai",
            "crew": {},                 # 由配员节点填，这里留空
            "provenance": {},
            "leaf_unit": kb_units.normalize_unit(leaf_unit),
        }
        _set_usable(binding, False, "尚未锚定")

        # 换算参数用到的文本池（任务名 + 叶子/工作包名 + 用户原话 + 项目参数 + 材料）
        text = self._convert_text(ctx, leaf, wp_name, task_name)
        band = ""
        # D1/D3 的条件锁定留痕：只在"绑到了真实 L4"的分支里填充（见路径②）。
        # 这里先定义成空值，让"没有 L4（纯 AI 经验估算）"的路径也能安全拼接 provenance。
        cond_note = ""
        cond_lock_sources = {}

        def _done():
            """统一出口：口径关（契约 §2）+ 量级档位 + 中间量级时补另一口径的一行。

            顺序有讲究：口径关要**先**跑。它判"口径不一致且换算不了"时会把
            `usable` 置 False —— 必须在其它步骤都不再改动可用性之后落地，
            否则后续某步一句 `_set_usable(True)` 就把这道关悄悄放行了。
            `_mark_band` 只写 `quantity_band` / `dual_binding`，不动可用性，所以放后面。
            """
            if _to_float(binding.get("norm_value"), 0.0) > 0:
                warnings.extend(self._apply_measure_scope(
                    binding, leaf, activity_id, leaf_unit, binding.get("unit") or "",
                    ctx, text, task_name))
            # §13 不可计量的包装单位分母（口径关之后、任何可用性改动之前）：
            # **只打标，绝不阻断**（父代理裁决）。放在这里是因为它要在"这条绑定最终
            # 用哪一行/哪个单位"确定之后才判得准。
            self._mark_denominator_meaningless(binding, leaf)
            self._mark_band(binding, band, activity_id, task_name, leaf, ctx, text)
            return binding, warnings

        # ---- 绑定一致性校验（契约 §3-D2）：工序名 / 主导方式与所选活动明显不符 ----
        # 必须放在量级基线**之前**：基线是拿"所绑活动"的 L3/单位算出来的，活动本身就错
        # 的时候基线也没有意义。命中 → 先试着在同 L3 内改绑到真正对得上的活动（保口径），
        # 改绑不到才按契约降级为未绑定。
        conflict_note = _activity_name_conflict(task_name, info,
                                                self._sibling_activities(activity_id))
        if conflict_note:
            # ---- 第 7 批（2026-09-21，用户裁定）：**先把"第一处置"接上线** ----
            # 这里的注释一直写着「命中 → 先试着在同 L3 内改绑到真正对得上的活动，
            # 改绑不到才按契约降级」，`_resolve_activity_conflict()` 的 docstring 也
            # 自称"绑定不一致时的**第一处置**" —— 但该函数**全仓零调用**（死代码），
            # 于是实际行为是"直接降级为未绑定、从不改绑"。后果：模型把
            # `1.1.1 场地平整` 绑到「场地硬化」、`ALC墙板安装` 绑到「砌块墙」这类
            # **张冠李戴**永远得不到纠正，只能报缺退回 WBS —— 这正是 ALC 那个坑
            # "反复出现"的结构性原因。
            # 现按设计接线：改绑成功 → 用改绑后的活动继续出定额；失败 → 才走下面的
            # 契约 §3 降级（**降级路径一个字节都没改**）。
            _new_aid, _new_info, _new_mode = self._resolve_activity_conflict(
                conflict_note, activity_id, info, leaf, task_name, binding,
                leaf_unit, ctx, text, warnings)
            if _new_aid:
                # 改绑成功：`binding` 已被 `_resolve_activity_conflict` 填成完整定额
                # （含单位校验 / `match_type="default"` / `reanchored_from`），
                # 叶子上的 `kb_activity_id` 也已同步写回。
                activity_id, info, mode = _new_aid, _new_info, _new_mode
                return _done()
            # 改绑不到 → 契约 §3：错定额一个数都不许用，降级为未绑定。
            binding["activity_conflict"] = conflict_note
            binding["mode"] = mode
            binding["match_type"] = "unbound"
            binding["norm_value"] = None
            binding["unit"] = ""
            binding["condition_text"] = ""
            binding["source_code"] = ""
            binding["provenance"] = {
                "value": None, "origin": "ai",
                "ref": str(leaf.get("kb_activity_id") or ""),
                "confidence": "低",
                "note": ("活动绑定不一致：%s —— 不拿错定额去算，沿用 WBS 工期"
                         % conflict_note)}
            _set_usable(binding, False, _REASON_BINDING_CONFLICT)
            leaf["wbs_warnings"] = list(leaf.get("wbs_warnings") or []) + [
                "[绑定] %s（%s）%s —— 已降级为未绑定（需重标 kb_activity_id）"
                % (task_name, task_id or "?", conflict_note)]
            warnings.append(self._info_warning(
                leaf, task_name,
                "任务 %s（%s）：%s —— 已降级为未绑定（match_type=unbound），"
                "该错定额不参与工期计算。"
                % (task_id, task_name, conflict_note),
                "沿用 WBS 工期", kind="binding"))
            return binding, warnings

        # ① 量级基线（L3 级，契约 §5-WS3②）——比任务名关键词更硬的口径，优先级最高
        baseline, l3 = self._baseline_of(activity_id)
        band = self._quantity_band(baseline, leaf, info)
        if band == "high" and mode != "machine":
            if self._reanchor_to_hint(baseline, activity_id, task_name, binding, leaf, ctx):
                warnings.append(self._info_warning(
                    leaf, task_name,
                    "任务 %s（%s）工程量 %s %s 超过 L3「%s」的量级阈值 %s："
                    "已按量级基线改绑机械活动 %s。"
                    % (task_id, task_name, leaf.get("quantity"), leaf_unit, l3 or "",
                       baseline.get("qty_threshold_high"), leaf.get("kb_activity_id")),
                    "按机械台班定额计算", kind="reanchor"))
                return _done()
            # 改绑不到机械活动 → 该定额降级"仅参考"，绝不进入工期计算
            binding["method_conflict"] = (
                "任务 %s（%s）工程量 %s %s 超过 L3「%s」的量级阈值 %s，"
                "按量级基线应为机械作业，但同 L3 改绑不到机械活动 —— "
                "原绑定 %s（%s）降级为仅参考、沿用 WBS 工期。"
                % (task_id, task_name, leaf.get("quantity"), leaf_unit, l3 or "",
                   baseline.get("qty_threshold_high"), activity_id,
                   (info or {}).get("activity_name") or ""))
            _set_usable(binding, False,
                        "量级基线要求机械作业（%s %s > %s），同 L3 改绑不到机械活动：仅参考"
                        % (leaf.get("quantity"), leaf_unit,
                           baseline.get("qty_threshold_high")))
            warnings.append(self._info_warning(
                leaf, task_name, binding["not_usable_reason"], "沿用 WBS 工期", kind="band"))
            return _done()
        if band == "low" and mode == "machine":
            # < 阈值下限 → 人工（量级基线覆盖任务名里的机械词）
            mode = "labor"
        if band == "mid" and mode != "machine":
            # 中间量级：机械为主 + 人工只算修边量（契约 §5-WS3②）。先尽量把机械侧
            # 改绑上；改绑不上就保持人工为主，另一口径作为 dual_binding 记下来。
            if self._reanchor_to_hint(baseline, activity_id, task_name, binding, leaf, ctx):
                warnings.append(self._info_warning(
                    leaf, task_name,
                    "任务 %s（%s）工程量 %s %s 落在 L3「%s」的中间量级带（%s~%s）："
                    "已按量级基线改绑机械活动 %s，人工侧只算修边量。"
                    % (task_id, task_name, leaf.get("quantity"), leaf_unit, l3 or "",
                       baseline.get("qty_threshold_low"), baseline.get("qty_threshold_high"),
                       leaf.get("kb_activity_id")),
                    "按机械台班定额计算", kind="reanchor"))
                return _done()

        # 任务描述与所绑活动的主导方式是否**互相矛盾**（关键词辅助信号）。
        # 实测：任务叫「机械挖基坑土方 3500 m³」，却被绑到 KB 里标注为人工主导、
        # 条件写着"坑底面积≤2.5m²，深度≤3m"的**人工挖小坑**定额（EARTH0032）。
        # 这种"张冠李戴"的定额看着 origin=kb、置信度"中"，对这条任务却**没有依据**，
        # 所以必须与 AI 估算同等对待：只作参考、不用来重算工期（见 scheduler 的证据门）。
        conflict = None
        if band != "low" and mode != "machine":
            conflict = _method_conflict_note(task_name, leaf, info)
        if conflict:
            binding["method_conflict"] = conflict
            # 主动补救：先试着在同工种大类内改绑机械活动（那才是有依据的定额）；
            # 改绑不成才退回"非证据、沿用 WBS 工期"。
            if self._reanchor_machine(activity_id, task_name, binding, leaf,
                                      leaf_unit, ctx):
                warnings.append(self._info_warning(
                    leaf, task_name,
                    "任务 %s（%s）：原绑定活动 %s 与任务描述不符（标注为非机械主导），"
                    "已在同工种内改绑为机械活动 %s。"
                    % (task_id, task_name, activity_id, leaf.get("kb_activity_id")),
                    "按改绑后的台班定额计算", kind="reanchor"))
                return _done()

        # 路径①：机械主导 → 机械台班定额
        if mode == "machine":
            got = self._bind_machine(activity_id, task_name, binding,
                                     leaf_unit, leaf=leaf, ctx=ctx, text=text)
            if got:
                return _done()
            # 机械定额策略失败 → 落到人工/NULL 策略，但把 mode 修回人工口径。
            # ⚠️ 契约 §4-D3「机械优先」的兜底：库里 65 个 `equipment_driven` 活动、
            # 其中 61 个在 `Norm_Equipment_Table` 有行。**有台班行就不许改按人工口径**，
            # 否则"机械算出来比人工长"的那些会被悄悄退回人工定额（正是 §4 要堵的）。
            # `_bind_machine` 返回 False 只有三种成因：主控机械缺台班行（已由
            # `_downgrade_missing_machine` 处理并 return True）、无主控机械且该行第一台
            # 机械台班数非正、KB 根本查不到台班行。前两种下 **mode 必须留在 machine**，
            # `usable=False`（由降级/单位校验落下）继续拦住它，但口径不能被改写。
            if self._has_equipment_rows(activity_id):
                binding["mode"] = "machine_basis_missing"
                binding["provenance"] = {
                    "value": None, "origin": "ai", "ref": activity_id or "",
                    "confidence": "低",
                    "note": ("标注为机械主导且 KB 有台班定额行，但取不到可用的台班数 —— "
                             "按机械口径处理，不退回人工定额（契约 §4）")}
            else:
                binding["mode"] = "labor"
                binding["provenance"] = {
                    "value": None, "origin": "ai", "ref": activity_id or "",
                    "confidence": "低",
                    "note": "标注为机械主导但 KB 无机械台班定额，改按人工口径估算"}

        # 路径②：人工定额
        if activity_id and info:
            # 第 7 批（2026-09-21）：这里原先只取 `_labor_rows()`（**只查
            # `Norm_Labor_Table`**），漏了 `L4_Norm_Default` 的审定量回退 —— 而
            # `_labor_candidates()` 早就实现了该回退（契约 §5-WS3），**却从未被任何
            # 地方调用**（死代码）。后果实测：`MASON_ALC_PANEL`（`Norm_Labor_Table`
            # **0 行**、`L4_Norm_Default` **0.095 工日/m²**）在这条路径上判"无定额"，
            # 一路掉到路径③ 的**单位盲**经验兜底拿到 `0.5 工日/m²`（= 1/普工 2.0）。
            # 这正是 `kb.labor_norm_default()` docstring 警告的那件事：
            # "调用方应当回退到它，而不是判'无定额'——否则整条工序丢定额、工期退回 WBS"。
            # 改动范围**窄**：`_labor_candidates()` 只在 `_labor_rows()` **一行可用值都
            # 没有**时才回退（全库这种人机定额皆无的 L4 仅个位数，见总清单 §8 第 14 条）
            # ⇒ 有真规范定额的活动逐字段不变。
            rows, rows_from_default = self._labor_candidates(activity_id)
            # ---- D1：选定 L4 时锁条件（结合用户输入，没给就取典型） ----
            # ⚠️ 锁定**在 `rows` 判空之前**做，而且只要 `activity_id` 在就必须做：
            # 这条 L4 若一行定额都没有（`_labor_rows()` 返回空），D1 的契约仍然是
            # "写进每个叶子 `leaf.condition_key`" —— 锁定**不是**定额匹配的副产品，
            # 是"选定了 L4"这件事本身的留痕。`lock_leaf_conditions(leaf, [], ...)`
            # 对空行表是安全的：用户明写的维度照样锁上（来源 `user`），其余维度锁不上
            # 就是锁不上（`condition_key` 可能为空 → 由 D2 的"推定不出就报缺"接手），
            # 绝不因此凭空造条件。
            #
            # "按条件精确筛"只在用户给了 **≥2 个条件值**时启用 —— 父代理裁定-2
            # （2026-09-21）接受此边界，并列入交付待审清单。推理（务必保留）：
            #   · 只给 1 个维度（如"框架梁"）时，条件键**无法完全由用户锁定**；
            #     真要精筛，就必须先拿"中位行"把其余维度补上再筛 —— 那一步
            #     等于**从后门退默认行**（`typical_labor_norm` 的等价物），
            #     正是 D2 明令禁止的"匹配不上就退默认行"换了个位置；
            #   · 于是这种情形保持既有路径：关键字 AND 命中 → 多行时
            #     LLM 选行 / 中位行（并保留 D4 的现浇过滤），
            #     由 D3 的**逐维度来源标注**（`condition_source` 里的
            #     user / typical）保证不把系统补的条件冒充成用户条件。
            # 换句话说：宁可"少精筛"也绝不"假精筛"；判据是"这个键有几个维度
            # 是用户真的说了的"，不是"要不要看起来精确"。
            text_pool = _text_pool(ctx, leaf, wp_name)
            _lock, cond_src, _typ = lock_leaf_conditions(leaf, rows, text_pool)
            cond_note = _condition_lock_note(_lock, cond_src)
            cond_lock_sources = dict(cond_src)
            # D1/D3：用户**真的说了**的条件维度数（判据见上面的裁定-2 推理）
            user_len = len([1 for s in cond_src.values() if s == "user"])
            lock_match_type = _condition_lock_match_type(cond_src)
            binding["condition_key"] = dict(_lock)
            binding["condition_source"] = dict(cond_src)
            # ---- L4_Norm_Default 审定量直达（`Norm_Labor_Table` 无可用行时）----
            # 为什么必须在这里用掉、不能指望下面的"典型值"分支：`kb.typical_labor_norm()`
            # 同样只看 `Norm_Labor_Table`（kb.py:640-658），对这类 L4 返回 None ⇒
            # 会继续往下掉到路径③ 的单位盲经验兜底。所以默认行要在这里落定。
            # 为什么放在 D1 锁定**之后**：D1 的契约是"选定 L4 就锁条件"，
            # 锁定是"选定了 L4"这件事本身的留痕，**不是**定额匹配成功的副产品 ——
            # 提前 return 会让这类 L4 一个条件都不锁（`test_d1_lock_happens_even_when_
            # l4_has_no_labor_rows` 守的就是这条）。
            # 为什么 `origin="ai"`：这条行的 `source_code = ai_estimate_v1`、
            # `review_state = pending`（未审），如实标 AI，不冒充 `kb`。
            # 为什么这里 `return`、不再走 `_apply_machine_preference`：与 :2119 附近的
            # 既有告警同源 —— 铝模/ALC 这类"有据可依的绑定"再被机械优先改一次的历史
            # 代价是 +122 天；默认行已经是该 L4 的审定量，不该被二次改动。
            if rows and rows_from_default:
                row = rows[0]
                _record_row_conditions(leaf, row)
                self._fill_from_labor_row(
                    binding, row, "default", "ai", "中",
                    _append_note(
                        "Norm_Labor_Table 无可用行 → 回退 L4_Norm_Default 审定量"
                        "（契约 §5-WS3；review_state=%s / source=%s）"
                        % (row.get("status") or "", row.get("source_code") or ""),
                        cond_note),
                    leaf_unit, ctx, text,
                    task_quantity=_to_float(leaf.get("quantity"), 0.0))
                return _done()
            if rows:
                binding["mode"] = "labor"
                keywords, desc = extract_condition_keywords(rows, text_pool)
                task_quantity = _to_float(leaf.get("quantity"), 0.0)
                hit = []
                if user_len >= 2:
                    hit = _matches_condition_rows(rows, _lock)
                    if not hit:
                        # ---- D2：按条件精确查不到 → 报缺，**禁止退默认行** ----
                        binding["condition_key"] = dict(_lock)
                        binding["condition_source"] = dict(cond_src)
                        binding["condition_text"] = cond_note
                        binding["match_type"] = "ai"
                        binding["norm_value"] = None
                        binding["unit"] = ""
                        binding["source_code"] = ""
                        binding["provenance"] = {
                            "value": None, "origin": "ai",
                            "ref": activity_id or "",
                            "confidence": "低",
                            "note": _append_note(
                                "B1 缺定额：按锁定条件精确匹配不到定额行（不退默认行）",
                                cond_note)}
                        _set_usable(
                            binding, False,
                            "缺定额：按条件「%s」在 %s 精确匹配不到定额行（禁止退默认行）"
                            % ("；".join("%s=%s" % (k, v) for k, v in _lock.items()),
                               activity_id))
                        warnings.append(self._info_warning(
                            leaf, task_name, binding["not_usable_reason"],
                            "该定额不进入工期计算", kind="missing_norm"))
                        return _done()
                    hit_note = _append_note(
                        "条件精确命中（%s）" % desc if desc else "条件精确命中", cond_note)
                    binding["condition_key"] = dict(_lock)
                    binding["condition_source"] = dict(cond_src)
                    binding["condition_match_count"] = len(hit)
                    if len(hit) == 1:
                        # D3：条件来源逐条标注（user / l4 / typical）→ 见 cond_note
                        self._fill_from_labor_row(
                            binding, hit[0], lock_match_type, "kb", "高", hit_note,
                            leaf_unit, ctx, text, task_quantity=task_quantity)
                        return _done()
                    # 锁条件后仍多行 → LLM / 中位数在**锁定后**的候选里选（不跨条件）
                    desc = desc or "锁定条件"
                elif keywords:
                    # 只给了 1 个条件值：保持既有"关键字 AND 命中"召回（可能多行），
                    # 只按 D4 过滤掉非现浇做法的行；候选多时由 LLM / 中位值定夺。
                    hit = _matches_condition_rows(
                        kb.labor_norm_match(activity_id, keywords), {})
                    lock_match_type = "default"
                else:
                    lock_match_type = "default"

                if len(hit) > 1:
                    # LLM 只在"候选多且关键字判不了"时介入，且一次只喂这一个 L4
                    picked, llm_warn = self._llm_pick(ctx, leaf, task_name, hit)
                    if llm_warn:
                        warnings.append(llm_warn)
                    if picked is not None:
                        _record_row_conditions(leaf, picked)
                        self._fill_from_labor_row(
                            binding, picked, lock_match_type, "kb", "中",
                            "LLM 从 %d 个候选条件中选定：%s" % (len(hit), desc or "（无关键字）"),
                            leaf_unit, ctx, text, task_quantity=task_quantity)
                        return _done()
                    # 代码策略：取 productivity_value 中位数那行
                    med = self._median_row(hit)
                    med, tier_note = self._tier_adjusted_row(hit, med, leaf_unit, text, ctx)
                    if tier_note:
                        binding["norm_tier_note"] = tier_note
                    _record_row_conditions(leaf, med)
                    self._fill_from_labor_row(
                        binding, med, lock_match_type, "kb", "中",
                        _append_note("有 %d 个候选条件，取了中位值" % len(hit),
                                     _append_note(cond_note, tier_note)),
                        leaf_unit, ctx, text, task_quantity=task_quantity)
                    return _done()

                # 关键字命中 0 行 / 没提出关键字 → 典型值（用户没给条件即 D1 的"取典型"）
                typ = kb.typical_labor_norm(activity_id)
                if typ and typ.get("norm"):
                    n = _to_int(typ.get("candidates"), 1)
                    note = ("关键字未命中，取典型值（候选 %d 个）" % n) if keywords \
                        else ("用户未提供条件，取典型值（候选 %d 个）" % n)
                    typ_note = _condition_lock_note(
                        leaf.get("condition_key") or {},
                        leaf.get("condition_source") or {})
                    # 面积↔体积缺墙厚时，档位必须与墙厚自洽（见 _pick_row_by_assumed_thickness）
                    row, tier_note = self._tier_adjusted_row(
                        _cast_in_place_rows(rows) or rows, typ["norm"],
                        leaf_unit, text, ctx)
                    if tier_note:
                        binding["norm_tier_note"] = tier_note
                    _record_row_conditions(leaf, row)
                    binding["condition_key"] = dict(leaf.get("condition_key") or {})
                    binding["condition_source"] = dict(leaf.get("condition_source") or {})
                    self._fill_from_labor_row(binding, row, "default", "kb", "中",
                                              _append_note(
                                                  _append_note(note, typ_note), tier_note),
                                              leaf_unit, ctx, text,
                                              task_quantity=task_quantity)
                    # 契约 §4 机械优先：即使这条"典型值"能算出个数，只要它的来源是
                    # **AI 估算**（如 `SPREP_AI_003 场地硬化` 的 `AI_ESTIMATE_V1`），
                    # 而相邻 L3 里有真规范机械活动（`GD_A11_平整场地`），就该改绑过去。
                    if band != "low" and self._binding_is_ai(binding):
                        _aid0, _info0 = self._apply_machine_preference(
                            task_name, activity_id, binding, leaf, leaf_unit,
                            ctx, text, warnings)
                        if _info0 and _to_float(binding.get("norm_value"), 0.0) > 0:
                            activity_id, info = _aid0, _info0
                    return _done()

        # ---- 契约 §4 机械优先（"手上只有 AI 估算"这一档）----
        # 走到这里说明人工定额这条路没给出可用行；若已绑的活动是 AI 估算，而相邻 L3 里
        # 存在真规范机械活动，就改绑过去按台班算。
        # ⚠️ 只对 **AI 来源**动手：真规范定额（`origin='kb'`）不在这里改绑，否则会把
        # 铝模/ALC 那类有据可依的绑定二次改坏（实测代价 +122 天）。
        if band != "low" and info and self._binding_is_ai(binding):
            _aid0, _info0 = self._apply_machine_preference(
                task_name, activity_id, binding, leaf, leaf_unit, ctx, text, warnings)
            if _info0 and _to_float(binding.get("norm_value"), 0.0) > 0:
                activity_id, info = _aid0, _info0
                return _done()

        # 路径③：无 kb_activity_id / KB 无任何定额 → L3 工种 + 经验产能，估算 + 报警
        # 口径关**不在**这里跑（契约 §2 第 1 条要求定额行有 `measure_scope`，而本路径
        # 没有定额行）。经验产能的分母就是任务自己的口径，不存在"两个口径不一致"，
        # 所以只按 §2 第 5 条打"未确认"标（`_done` 里统一处理）。
        #
        # 契约 §4 机械优先：走到这里说明人工/机械都没拿到可用定额（或压根没绑活动）。
        # 同 L3 里若存在 `equipment_driven` 且有台班行的活动、且名字对得上，就改绑过去
        # 重走机械口径 —— 实测土石方族 56 个有台班行的机械活动一条都没被用上。
        # ⚠️ `band == "low"`（工程量低于该 L3 的人工阈值）时**不**改绑：量级基线的结论是
        # "这么小的量就该人工"（如 30 m³ 的机械挖基坑土方），机械优先不得推翻它。
        if band != "low":
            activity_id, new_info = self._apply_machine_preference(
                task_name, activity_id, binding, leaf, leaf_unit, ctx, text, warnings)
            if new_info and _to_float(binding.get("norm_value"), 0.0) > 0:
                return _done()

        # ---- 路径③-守卫（第 7 批，2026-09-21 用户裁定，直接执行）----
        # **工作包级占位叶子**（`kb_activity_id is None` ∧ 3 段 id，如 `1.1.1 场地平整`
        # / `1.2.2 定位放线`）**不许**用经验产能反算工期。
        #
        # 为什么：`_EXPERIENCE_PRODUCTIVITY`（:113-120）是**单位盲**的 —— 它只按工种给
        # "单位/工日"，不区分 m³ / m² / t / 项。实测同一份 6 栋住宅算例（127 条叶子）里
        # 9 条这类叶子拿它反算出 **5162 天**（占 `overview.total_duration_days` 5323 天的
        # 93%），其中「1.2.2 定位放线」一条 = 128000 m² × 1.0 工日/m² ÷ 26 人 = **4924 天**，
        # 而它自己的 WBS 目标工期是 **2 天**（量级差 ~2500 倍）。这不是排程链/并行度的问题，
        # 是"没有 KB 定额的任务被一张单位盲的兜底表放大"。
        #
        # 判据（与 `beat_node._stamp_non_beat_layer_fields()` 的口径一致 ——
        # 它把"工作包级的 3 段 id 叶子"如 `1.1.1 场地平整` 单列）：
        #   ① `not activity_id`（没有任何 KB 活动可绑）；② `_is_work_package_leaf(leaf)`。
        # 两条同时成立才动手 ⇒ **不误伤**"绑了真活动但 KB 无定额"的叶子，也不误伤
        # "未绑定的真 L4"（4 段以上 id，如 LLM 造出工序但没绑上活动的那类）——
        # 它们本该保留 AI 兜底行为，一个字段都不改。
        #
        # 处置：`usable=False` + 明确 `not_usable_reason` ⇒ 与 ALC 那 11 条"如实报缺"
        # **同一条链路**：`_norm_applied` 为空、工期沿用叶子自身 `duration_days`（WBS 目标），
        # 绝不编数。逐行留痕（本分支的 provenance + 一条 info 警告）。
        if not activity_id and _is_work_package_leaf(leaf):
            binding["norm_value"] = None
            binding["unit"] = ""
            binding["condition_text"] = ""
            binding["source_code"] = ""
            binding["provenance"] = {
                "value": None,
                "origin": "ai",
                "ref": str(leaf.get("work_type") or ""),
                "confidence": "低",
                "note": ("工作包级占位叶子无 KB 活动定额：不用经验产能反算工期"
                         "（`_EXPERIENCE_PRODUCTIVITY` 单位盲，会把 m² 当 m³ 用），"
                         "沿用 WBS 目标工期 %s 天" % leaf.get("duration_days")),
            }
            _set_usable(binding, False, _REASON_WP_PLACEHOLDER)
            warnings.append(self._info_warning(
                leaf, task_name,
                "任务 %s（%s）：工作包级占位叶子（3 段 id）且无 KB 活动定额 —— "
                "不按工种经验产能反算，%s（未参与容量计算）。"
                % (task_id, task_name, _REASON_WP_PLACEHOLDER),
                "沿用 WBS 工期", kind="no_kb_activity"))
            return _done()

        binding["mode"] = "labor"
        norm_value, labor_type, unit = self._estimate_by_experience(leaf, activity_id, scope_map)
        binding["norm_value"] = norm_value
        binding["unit"] = "工日/%s" % (leaf.get("unit") or "单位")
        binding["condition_text"] = "经验产能估算（无 KB 定额）"
        binding["quantity_basis"] = 1.0
        binding["source_code"] = ""
        binding["match_type"] = "ai"
        binding["provenance"] = {
            "value": norm_value,
            "origin": "ai",
            "ref": labor_type or "L3 工种经验值",
            "confidence": "低",
            "note": _append_note(_append_note(_AI_NOTE, "按 %s 经验产能 %s 单位/工日折算" % (
                labor_type or "通用", _EXPERIENCE_PRODUCTIVITY.get(labor_type, _EXPERIENCE_DEFAULT)))
                if labor_type else "", cond_note if cond_lock_sources else ""),
        }
        # G1（本轮）：**删掉 A 类判据** —— 原来这里有一条
        #   `if binding.get("norm_is_evidence"): _set_usable(binding, False,
        #    "AI估算定额：KB 无定额行，只作参考")`
        # 它把 62 条 `match_type='ai'` 的叶子全部堵死（它们**都**有 norm_value，
        # 0 条空）。政策已改为"AI 经验估算定额放行 + 逐行标注"
        # （`norm_defaults.STATE_RELEASED_AI` / `LABEL_AI_ESTIMATE`），所以这里
        # 只跑单位校验；单位校验自己的 `usable=False`（缺计量单位 / 不可换算）、
        # 口径关（`口径无法对齐`）、D5 的"推定不出就报缺"与"换算不合理被拒"
        # **全部保留** —— 删的只是"因为来源是 AI 就不许用"这一条。
        self._apply_unit_check(binding, leaf_unit, None)
        warn = self._make_warning(leaf, task_name, activity_id, binding, labor_type)
        warnings.append(warn)
        return _done()

    # ---------------- 量级基线 / 单位校验 / 换算参数 ----------------
    @staticmethod
    def _baseline_of(activity_id):
        """(量级基线 dict | None, work_type_l3 | None)。表缺失/未标定 → (None, None)。"""
        if not activity_id:
            return None, None
        try:
            l3 = kb.l3_of_activity(activity_id)
        except Exception:
            l3 = None
        if not l3:
            return None, None
        try:
            return kb.production_method_baseline(l3), l3
        except Exception:
            return None, None

    def _quantity_band(self, baseline, leaf, info):
        """量级基线落在哪一档：'high' | 'low' | 'mid' | ''（不适用）。

        阈值只在**量纲一致**时才比：土方的 500/50 是 m³ 阈值，拿 m²（如"平整场地"）
        去比毫无意义，这时不套量级基线。
        """
        if not baseline:
            return ""
        high = _to_float(baseline.get("qty_threshold_high"), 0.0)
        low = _to_float(baseline.get("qty_threshold_low"), 0.0)
        if high <= 0 and low <= 0:
            return ""
        try:
            qty = float(leaf.get("quantity"))
        except (TypeError, ValueError):
            return ""
        # 阈值单位 = 该 L3 的机械基准活动单位（没有 hint 时退到所绑活动单位）
        ref = kb_units.normalize_unit((info or {}).get("unit") or leaf.get("unit"))
        hint = baseline.get("machine_activity_hint")
        if hint:
            hinfo = self._activity_info(hint)
            if hinfo and hinfo.get("unit"):
                ref = kb_units.normalize_unit(hinfo.get("unit"))
        lu = kb_units.normalize_unit(leaf.get("unit"))
        if not ref or not lu or kb_units.unit_family(ref) != kb_units.unit_family(lu):
            return ""
        if high > 0 and qty > high:
            return "high"
        if low > 0 and qty < low:
            return "low"
        return "mid"

    @staticmethod
    def _convert_text(ctx, leaf, wp_name, task_name):
        """换算参数的文本池：任务名 + 叶子名/工作包名 + 用户原话/参数 + 工程量。"""
        ctx = ctx if isinstance(ctx, dict) else {}
        parts = [task_name or "", _text_pool(ctx, leaf, wp_name)]
        qty = leaf.get("quantity")
        if qty is not None:
            parts.append("%s %s" % (qty, leaf.get("unit") or ""))
        parts.append((leaf.get("condition_text") or "")
                     if isinstance(leaf.get("condition_text"), str) else "")
        return " ".join(p for p in parts if p)

    def _apply_band_or_reject(self, binding, check, source, row_condition=""):
        """D5：跨族换算的**量级校验** + 落盘 `basis_adjust`（六字段契约）。

        两条校验（任一不过 → 拒绝该换算，"离谱即拒"）：
          ① 换算参数本身落在 `_MAGNITUDE_BANDS` 的合理带内；
          ② 换算后的量 / 原量 = 换算系数，且 `adjusted / 原量` 与系数一致、量级有限。
        **用户明写的值优先**（来源 `text`）→ 跳过参数带校验，只记 `basis_adjust`。

        落盘的是**调整后的工程量**（`adjusted_quantity`），不是参数：参数只写进
        `method` / `note` 作为换算过程的说明，**不建表、不落盘成独立对象**。
        返回 `""`（通过，已写 basis_adjust）或拒绝说明（str，调用方据此判 unusable）。
        """
        den = check.get("denominator") or ""
        factor = _to_float(check.get("factor"), 0.0)
        task_qty = _to_float((binding or {}).get("_task_quantity"), 0.0)
        band = _band_of(binding.get("leaf_unit") or "", den)
        if not band:
            return ""
        param, lo, hi, unit, label = band
        reject = _band_reject(binding.get("convert_ctx"), binding.get("leaf_unit") or "",
                              den, source)
        if reject:
            return reject
        if factor <= 0 or not _is_finite(factor):
            return "换算系数无效（%s）：无法把工程量换算到定额分母「%s」" % (factor, den)
        if task_qty > 0:
            adjusted = task_qty * factor
            if not _is_finite(adjusted) or adjusted <= 0:
                return "换算后的工程量无效（%s %s）" % (adjusted, den)
            # 量级带复核：换算后的量必须与"参数决定的量"一致（同一次乘法的自洽检查），
            # 并拦住"薄墙按 3 m 厚折"这种系数离谱的换算。
            want = _to_float((binding.get("convert_ctx") or {}).get(param), 0.0)
            if want <= 0:
                # 具名取值（如 D5 的"定额条件厚度档位 ≤200mm → 0.2 m"）已经写进
                # `unit_assumption` / `ctx_value`：量级校验要用同一个数，别再找默认值。
                for holder in ("unit_assumption", "ctx_value"):
                    holder_val = binding.get(holder)
                    if isinstance(holder_val, dict):
                        want = _to_float(holder_val.get(param), 0.0)
                        if want > 0:
                            break
            implied = adjusted / task_qty if task_qty else 0.0
            if want > 0 and abs(implied - want) > 1e-9 * max(1.0, abs(want)):
                return ("换算系数 %g 与换算参数 %s=%g 不自洽（换算后的量 %g %s）"
                        % (implied, param, want, adjusted, den))
            if implied > hi or implied < lo:
                return ("换算系数 %g 超出合理带 %g~%g（%s）：按此换算得到 %g %s，"
                        "原量 %g %s —— 离谱即拒"
                        % (implied, lo, hi, label, adjusted, den, task_qty,
                           binding.get("leaf_unit") or "?"))
            method = ("按 %s 由 %g %s 折 %g %s"
                      % (_param_phrase(param, want, unit),
                         task_qty, binding.get("leaf_unit") or "", adjusted, den))
            binding["basis_adjust"] = {
                "task_scope": str(binding.get("leaf_unit") or ""),
                "norm_scope": den,
                "task_quantity": task_qty,
                "adjusted_quantity": round(float(adjusted), 6),
                "method": method,
                "note": ("以数据库（定额）分母口径为准：%s → %s；换算依据：%s"
                         % (binding.get("leaf_unit") or "?", den,
                            CONVERT_SOURCE_LABELS.get(source) or source or "未标注")),
            }
            prov = binding.get("provenance")
            if isinstance(prov, dict):
                prov["note"] = _append_note(prov.get("note"), binding["basis_adjust"]["note"])
        return ""

    def _apply_unit_check(self, binding, leaf_unit, convert_ctx=None, ctx_source="",
                          row_condition=""):
        """单位校验唯一入口：`kb_units.check_unit_pair`（默认拒绝）。

        写回 binding：`unit_check` / `convert_factor`（叶子量 × factor = 定额分母单位的量）
        / `norm_is_evidence` / `not_usable_reason`，跨族换算时另写 D5 的 `basis_adjust`。
        """
        unit_text = str(binding.get("unit") or "")
        check = kb_units.check_unit_pair(leaf_unit, unit_text, convert_ctx)
        binding["unit_check"] = check
        binding["convert_ctx"] = dict(convert_ctx or {})
        if check.get("verdict") == "unusable":
            if not kb_units.denominator_of(unit_text):
                reason = ("缺计量单位：定额单位「%s」没有分母、KB 未给 quantity_unit，"
                          "不得用叶子单位「%s」补" % (unit_text or "台班", leaf_unit or "?"))
            else:
                reason = "单位不可用：%s" % (check.get("detail") or "")
            _set_usable(binding, False, reason)
            return check
        binding["convert_factor"] = (check.get("factor")
                                     if check.get("verdict") == "convertible" else 1.0)
        # 分母（定额量的单位）也写下来：下游（scheduler `_build_ledger_item` 的机械分支、
        # 覆盖率报告）要"换算到哪个单位"才能自己复核，不该各自去 parse `unit`。
        binding["convert_denominator"] = check.get("denominator") or ""
        # ---- D5：跨族换算必须过"量级校验"并落盘调整后的工程量 ----
        if check.get("verdict") == "convertible":
            reject = self._apply_band_or_reject(
                binding, check, ctx_source or binding.get("ctx_source") or "",
                row_condition)
            if reject:
                _set_usable(binding, False, "换算不合理：%s" % reject)
                binding["basis_adjust_rejected"] = {
                    "reason": reject,
                    "task_quantity": _to_float(binding.get("_task_quantity"), 0.0),
                    "task_scope": str(leaf_unit or ""),
                    "norm_scope": check.get("denominator") or "",
                    "factor": check.get("factor"),
                }
                return check
        _set_usable(binding, True)
        if check.get("verdict") == "convertible":
            prov = binding.get("provenance")
            if isinstance(prov, dict):
                prov["note"] = _append_note(
                    prov.get("note"), "单位换算：1 %s = %g %s（%s）"
                    % (kb_units.normalize_unit(leaf_unit), check.get("factor") or 1.0,
                       check.get("denominator") or "", check.get("detail") or ""))
        return check

    def _apply_measure_scope(self, binding, leaf, activity_id, leaf_unit, norm_unit,
                             ctx, text, task_name):
        """口径关（契约 §2）：在"绑定活动之后、算工日之前"判"任务口径 vs 定额口径"。

        三种结论（`kb_units.measure_scope_state`）：
          · `unconfirmed`（任一方 `''`）→ **照旧按同量纲算**，只打
            `binding["basis_unconfirmed"]=True`（供 WS3 在产物上标注），**不阻断**；
          · `same` → 什么都不做；
          · `mismatch` → **以定额口径为准**：
              - 能换算（走 `_resolve_convert_ctx` 的同一套机制 + `kb_units.convert`）
                → 写 `binding["basis_adjust"]`（`task_scope` / `norm_scope` /
                `task_quantity` / `adjusted_quantity` / `method` / `note`），并把换算
                系数落进既有的 `convert_factor` / `convert_denominator`，
                由 `resource.py`（`norm_quantity = quantity * conv_factor`）与
                `scheduler.quantity_in_norm_unit` 沿用同一真源计算；
              - 换算不了 → `usable=False` + `not_usable_reason="口径无法对齐"` +
                `leaf["wbs_warnings"]` 追加可读警告。**禁止退回 1:1 硬乘、禁止静默丢弃**。

        **同量纲不同对象**（`建筑面积` vs `风管展开面积`，两边都是 m²）走"换算不了"
        这一支：单位换算恒为 1:1，没有任何换算依据能把一个计量对象变成另一个，
        不阻断就没有别的防线。这正是契约 §2 要堵的洞（5 倍误差）。

        返回新增的警告列表（`_done` 会 extend 到 `_bind_one` 的 warnings 上）。
        """
        out = []
        # 只有"真有定额值"才谈得上口径：`_downgrade_missing_machine` 那种
        # norm_value=None 的降级不该被口径关再改一次原因（它的原因更具体、必须保留）。
        if _to_float(binding.get("norm_value"), 0.0) <= 0:
            return out
        norm_scope = _known_scope(binding.get("norm_measure_scope"))
        task_scope, src = _task_measure_scope(
            leaf, binding=binding, activity_id=activity_id, text=text)
        binding["task_measure_scope"] = task_scope
        binding["task_scope_source"] = src
        state = kb_units.measure_scope_state(task_scope, norm_scope)
        if state == kb_units.SCOPE_SAME:
            return out
        if state == kb_units.SCOPE_UNCONFIRMED:
            # 契约 §2 第 5 条：未知**不是**一致，也不是不一致 —— 不阻断，只打标。
            binding["basis_unconfirmed"] = True
            return out

        # ---- 口径确定不一致：以定额口径为准 ----
        quantity = _to_float((leaf or {}).get("quantity"), 0.0)
        norm_den = kb_units.denominator_of(norm_unit)
        lu = kb_units.normalize_unit(leaf_unit)

        def _block(note):
            """换算不了 → 显式阻断 + 可读警告（绝不 1:1 硬乘、绝不静默）。"""
            _set_usable(binding, False, _REASON_SCOPE_UNALIGNED)
            leaf["wbs_warnings"] = list(leaf.get("wbs_warnings") or []) + [
                _scope_warning_text(task_name, leaf, task_scope, norm_scope,
                                    quantity, leaf_unit, note)]
            out.append(self._info_warning(
                leaf, task_name,
                "任务 %s（%s）工程量 %s %s：任务口径「%s」/ 定额口径「%s」—— %s。"
                "已置为不可用，该定额不参与工期计算。"
                % ((leaf or {}).get("id"), task_name, quantity, leaf_unit,
                   task_scope, norm_scope, note),
                "该定额不进入工期计算", kind="scope"))
            return out

        if lu and norm_den and lu == norm_den:
            # 同单位、**不同计量对象**：1:1 就是那 5 倍误差本身，没有任何换算依据。
            return _block("两边单位都是「%s」但不是同一个计量对象，没有任何换算依据" % lu)

        conv_ctx = binding.get("convert_ctx")
        if not isinstance(conv_ctx, dict) or not conv_ctx:
            conv_ctx, _s, _n = _resolve_convert_ctx(leaf_unit, norm_unit, text, ctx)
        conv = kb_units.convert(quantity, lu, norm_den, conv_ctx) if (lu and norm_den) else None
        if conv is None:
            return _block("换算不出（%s）" % (
                kb_units.next_step_hint(lu, norm_den, conv_ctx) if (lu and norm_den)
                else "缺工程量单位或定额分母"))

        adjusted, method = conv[0], conv[1]
        # `convert_factor` 是资源层/排程层唯一认的换算入口（存量机制，不新造通道）。
        # 单位校验已经判过 convertible 并写过同一个系数，这里重写是为了让
        # `convert_denominator` 与口径关的结论自洽（两次算的是同一个数）。
        binding["convert_factor"] = round(float(adjusted) / quantity, 10) if quantity else 1.0
        binding["convert_denominator"] = norm_den
        binding["basis_adjust"] = {
            "task_scope": task_scope,
            "norm_scope": norm_scope,
            "task_quantity": quantity,
            "adjusted_quantity": round(float(adjusted), 6),
            "method": method or "口径换算",
            "note": ("以定额口径为准：任务口径「%s」→ 定额口径「%s」；换算方式 %s" % (
                task_scope, norm_scope, method or "口径换算")),
        }
        prov = binding.get("provenance")
        if isinstance(prov, dict):
            prov["note"] = _append_note(prov.get("note"), binding["basis_adjust"]["note"])
        out.append(self._info_warning(
            leaf, task_name,
            "任务 %s（%s）的计量对象是「%s」、定额口径是「%s」：已按定额口径换算"
            "（%s；换算后工程量 %s），留痕 basis_adjust。"
            % ((leaf or {}).get("id"), task_name, task_scope, norm_scope,
               method or "口径换算", binding["basis_adjust"]["adjusted_quantity"]),
            "按定额口径计算", kind="scope"))
        return out

    def _mark_band(self, binding, band, activity_id, task_name, leaf, ctx, text):
        """记录量级档位；中间量级时把另一口径的一行也绑上（人工只算修边量）。"""
        if not band:
            return
        binding["quantity_band"] = band
        if band != "mid":
            return
        side = {}
        if str(binding.get("mode") or "") == "machine":
            rows = self._labor_rows(activity_id)
            if rows:
                row = self._median_row(rows)
                nv, unit, basis, _ = _labor_norm_of(row)
                side = {"role": "edge_trim", "mode": "labor",
                        "norm_value": round(nv, 6) if nv > 0 else None,
                        "unit": unit, "quantity_basis": basis,
                        "norm_id": row.get("norm_id"),
                        "labor_activity_id": activity_id,
                        "condition_text": row.get("condition_text") or "",
                        "source_code": row.get("source_code") or "",
                        "note": "中间量级：人工侧只算修边量（折算比例由排程层决定）"}
        else:
            baseline, _l3 = self._baseline_of(activity_id)
            hint = str((baseline or {}).get("machine_activity_hint") or "")
            if hint:
                trial = {"task_id": binding.get("task_id"), "mode": "labor",
                         "norm_value": None, "unit": "", "condition_text": "",
                         "quantity_basis": 1.0, "source_code": "", "match_type": "ai",
                         "crew": {}, "provenance": {}, "leaf_unit": binding.get("leaf_unit")}
                if self._bind_machine(hint, task_name, trial, leaf.get("unit") or "",
                                      leaf=leaf, ctx=ctx, text=text) \
                        and trial.get("norm_value"):
                    side = {"role": "machine_side", "mode": "machine",
                            "norm_value": trial.get("norm_value"),
                            "unit": trial.get("unit"),
                            "quantity_basis": trial.get("quantity_basis"),
                            "source_code": trial.get("source_code"),
                            "machine_name": trial.get("machine_name"),
                            "convert_factor": trial.get("convert_factor"),
                            "note": "中间量级：机械侧按量级基线改绑 %s" % hint}
        if side:
            binding["dual_binding"] = side

# ---------------- 各条路径的辅助 ----------------
    def _activity_info(self, activity_id):
        """活动元信息（带缓存，避免同一条 L4 反复查库）。"""
        if not activity_id:
            return None
        if activity_id not in self._mode_cache:
            try:
                self._mode_cache[activity_id] = kb.activity_info(activity_id)
            except Exception:
                self._mode_cache[activity_id] = None
        return self._mode_cache[activity_id]

    @staticmethod
    def _sibling_activities(activity_id):
        """同 L3 的其它活动（供名字一致性校验判断"有没有更像的替代"）。

        取不到（无 activity_id / 无 L3 / 查询异常）→ `None`，表示"没有可用替代信息"，
        此时 `_activity_name_conflict` 的第三条判据**不生效**（宁可不拦）。
        """
        if not activity_id:
            return None
        try:
            wt = kb.l3_of_activity(activity_id)
            if not wt:
                return None
            return kb.l4_for(wt) or None
        except Exception:
            return None

    def _machine_preference_candidate(self, task_name, activity_id):
        """机械优先（契约 §4）：在**同 L3** 内找一条"该改绑过去的机械活动"。

        判据（全部满足才返回候选；否则返回 None，保持现状）：
          1. 候选 `recommended_production_mode == 'equipment_driven'`；
          2. 候选在 `Norm_Equipment_Table` **确实有台班行**（`_has_equipment_rows`）
             —— 契约 §4 的原话是"有台班定额行 → 走 machine"，没有行的不选；
          3. 候选与任务名**整名包含**（`场地平整` ⊂ `GD_A11_平整场地`、或反之），
             或任务描述明确像机械作业（`_expects_machine`）且名字连续共同片段 ≥ 2。
             —— 这是"不许瞎绑"的落点：土石方族里"机械挖土方"与"平整场地"不是一回事，
             没有文字依据就不许拿它当"机械优先"的理由。

        为什么需要这一步：库里有 65 个 `equipment_driven` 活动带真规范台班行，而实测
        计划只用到 9 个 —— 土石方族（平整场地 / 推土机推土方 / 压路机碾压土石方…）
        一条都没用上，`1.1.1 场地平整` 甚至停在 AI 定额上（AI_ESTIMATE_V1）。
        机械优先的**理由是"施工方法与规范依据正确"，不是"为了让工期变短"**。
        """
        if not activity_id:
            return None
        try:
            acts = self._sibling_activities(activity_id) or []
        except Exception:
            return None
        task_chars = _name_chars(task_name)
        want_machine = _expects_machine(task_name)
        best, best_score = None, 0
        for a in acts:
            aid = str(a.get("activity_id") or "")
            alt = str(a.get("activity_name") or "")
            if not aid or aid == activity_id:
                continue
            if str(a.get("recommended_production_mode") or "") != "equipment_driven":
                continue
            if not self._has_equipment_rows(aid):
                continue
            alt_chars = _name_chars(alt)
            if not alt_chars:
                continue
            if task_chars and (task_chars in alt_chars or alt_chars in task_chars):
                return a                       # 整名包含是最强证据，直接采用
            if not want_machine:
                continue
            score = _name_overlap(task_name, alt)
            if score > best_score:
                best, best_score = a, score
        if best is not None and best_score >= _REBIND_OVERLAP_MIN:
            return best
        # ---- 受限的跨 L3 例外（父代理裁决）：只对土石方/场地类动作词开放 ----
        # 判据（逐条都要满足，缺一不换）：
        #   ① 任务名里出现白名单动作词；
        #   ② 目标活动名里**也**出现同一个动作词（整词包含，不是二元组重叠）；
        #   ③ 目标是 equipment_driven 且 `Norm_Equipment_Table` 有台班行。
        for word in _CROSS_L3_ACTION_WORDS:
            if word not in str(task_name):
                continue
            cand = self._cross_l3_machine_candidate(word)
            if cand is not None:
                return cand
        return None

    @staticmethod
    def _cross_l3_machine_candidate(word):
        """跨 L3 找一个名字含 `word` 的机械活动（有台班行）。取不到 → None。

        只扫 `L4_Activity_Dictionary` 里的活动名，代价可控；
        结果按活动编号排序（确定性、可复现）。
        """
        try:
            rows = kb._query_all(
                "SELECT activity_id, activity_name, unit, recommended_production_mode "
                "FROM L4_Activity_Dictionary "
                "WHERE recommended_production_mode = 'equipment_driven' "
                "AND activity_name LIKE ? ORDER BY activity_id", ("%" + word + "%",))
        except Exception:
            return None
        for r in rows or []:
            aid = str(r[0] or "")
            if aid and kb.equipment_norms(aid):
                return {"activity_id": aid, "activity_name": r[1], "unit": r[2],
                        "recommended_production_mode": r[3]}
        return None

    @staticmethod
    def _mark_denominator_meaningless(binding, leaf):
        """§13：分母是不可计量的包装单位（项/批/组/点/套/座…）且工程量不足以支撑 → 打标。

        **只写标记，绝不改 `usable` / `norm_is_evidence`**（父代理 2026-09-20 裁决）。
        理由：硬阻断会静默丢掉定额与人工、工期退回模型拍数 —— 用户最反对的正是"静默丢
        数据"；政策是"不得不用 AI 就用，**最后标出来**"。所以：

          `binding["denominator_meaningless"] = True`
          `binding["basis_unconfirmed"] = True`（口径未确认，交 WS3 在产物上标注）
        """
        unit_text = str(binding.get("unit") or "")
        if not unit_text:                       # 机器台班无分母时 unit 可能是裸「台班」
            return
        qty = (leaf or {}).get("quantity")
        if kb_units.denominator_is_meaningless(unit_text, qty):
            binding["denominator_meaningless"] = True
            binding["basis_unconfirmed"] = True

    @staticmethod
    def _binding_is_ai(binding):
        """这条绑定的定额来源是不是 **AI 估算**（而不是真规范定额行）。

        判据与 `scheduler._ai_source` 同口径：`provenance.origin=='ai'`
        / `match_type=='ai'` / `source_code` 以 `AI_` 开头或含 `AI_ESTIMATE`
        / 以 `SCAFFOLD` 开头（`SCAFFOLD_V1` = WS6 的**类别占位**定额，同样无规范依据）。
        ⚠️ `AI_ESTIMATE_V1` 的定额**可以**用（2026-09-20 政策），所以这里只用来决定
        "要不要再试一次机械优先改绑"，不用来否决。
        ⚠️ `SCAFFOLD_V1` 于 2026-09-20 用户裁定「保留占位但必须全面如实标注」后并入本判据，
        以与交付层 `delivery._ai_norm_source_code_ai` 保持**同一口径**
        （否则会出现"看板标非规范、排程台账却按规范计"的静默分叉）。
        """
        b = binding if isinstance(binding, dict) else {}
        prov = b.get("provenance") or {}
        if str(prov.get("origin") or "").strip().lower() == "ai":
            return True
        if str(b.get("match_type") or "").strip().lower() == "ai":
            return True
        src = str(b.get("source_code") or "").upper()
        return ("AI_ESTIMATE" in src or src.startswith("AI_")
                or src.startswith("SCAFFOLD"))

    def _apply_machine_preference(self, task_name, activity_id, binding, leaf, leaf_unit,
                                  ctx, text, warnings):
        """同 L3 内改绑到 `equipment_driven` 且**有台班行**的活动（契约 §4）。

        返回新的 `(activity_id, info)`；不改绑时原样返回。改绑成功会：
          · 换掉 binding（台班口径）、写 `reanchored_from` + `machine_preference`；
          · 把新活动写回叶子 `kb_activity_id`（`/sources`、工作面容量回查要看它）；
          · 追加一条警告说明"从哪个活动改绑到了哪个活动"。
        """
        cand = self._machine_preference_candidate(task_name, activity_id)
        if not cand:
            return activity_id, None
        new_id = str(cand.get("activity_id") or "")
        trial = dict(binding)
        try:
            ok = self._bind_machine(new_id, task_name, trial, leaf_unit,
                                    leaf=leaf, ctx=ctx, text=text)
        except Exception:
            ok = False
        if not ok or not trial.get("norm_is_evidence") or \
                not _to_float(trial.get("norm_value"), 0.0):
            return activity_id, None
        old_name = str((self._activity_info(activity_id) or {}).get("activity_name") or "")
        binding.clear()
        binding.update(trial)
        binding["reanchored_from"] = activity_id
        binding["machine_preference"] = {
            "from_activity": activity_id, "from_name": old_name,
            "to_activity": new_id, "to_name": str(cand.get("activity_name") or ""),
            "reason": "契约 §4 机械优先：同 L3 内该活动为 equipment_driven 且有台班定额行",
        }
        prov = binding.get("provenance")
        if isinstance(prov, dict):
            prov["note"] = _append_note(
                prov.get("note"),
                "机械优先：原绑定 %s（%s）改绑为 %s（%s）"
                % (activity_id, old_name, new_id, cand.get("activity_name") or ""))
        if isinstance(leaf, dict):
            leaf["kb_activity_id"] = new_id
            leaf["wbs_warnings"] = list(leaf.get("wbs_warnings") or []) + [
                "[机械优先] %s（%s）原绑 %s（%s）→ 改绑 %s（%s）"
                % (task_name, leaf.get("id") or "?", activity_id, old_name,
                   new_id, cand.get("activity_name") or "")]
        warnings.append(self._info_warning(
            leaf, task_name,
            "任务 %s（%s）：按契约 §4 机械优先，原绑定 %s（%s）改绑为机械活动 %s（%s）"
            "（该活动有台班定额行）。"
            % (leaf.get("id"), task_name, activity_id, old_name, new_id,
               cand.get("activity_name") or ""),
            "按机械台班定额计算", kind="reanchor"))
        return new_id, self._activity_info(new_id)

    def _has_equipment_rows(self, activity_id):
        """该活动在 `Norm_Equipment_Table` 有没有台班定额行（契约 §4-D3 的判据）。

        带缓存：同一个 activity_id 会被每个施工段反复查询。
        """
        if not activity_id:
            return False
        key = "__equip__" + str(activity_id)
        if key not in self._mode_cache:
            try:
                self._mode_cache[key] = bool(kb.equipment_norms(activity_id))
            except Exception:
                self._mode_cache[key] = False
        return self._mode_cache[key]

    def _labor_rows(self, activity_id):
        try:
            rows = kb.labor_norms(activity_id) or []
        except Exception:
            return []
        # 只保留有定额值或产能值的行
        rows = [r for r in rows
                if _to_float(r.get("norm_value"), 0.0) > 0
                or _to_float(r.get("productivity_value"), 0.0) > 0]
        # D4：「构件做法」硬定现浇 —— 候选集在**入口**就把非现浇行摘掉，
        # 这样所有选行路径（精确查 / 中位 / 档位 / 同 L3 改绑）都不会再把预制行
        # 当候选；行没写该维度时保留（没写 = 没把做法限定成别的）。
        cast = _cast_in_place_rows(rows)
        return cast

    def _labor_default_row(self, activity_id):
        """`L4_Norm_Default` 的人工默认定额行 → 伪装成一条 `labor_norms` 行。

        为什么需要：`Norm_Labor_Table` 里存在**"行在、值空"**的数据缺陷
        （实测 `FORM_NEW_OTHER` 的规则行只导进了条件文本、`labor_norm_value` 是 NULL，
        `status` 却写着 verified）。`_labor_rows()` 因此为空 → 整条工序丢定额、
        工期退回 WBS（实测铝模 7 天变 14 天）。`L4_Norm_Default` 有可用值
        （0.0282 工日/m²），按契约 §5-WS3 它就是"这个 L4 该用哪一行"的审定量，
        所以回退到它，而不是判"无定额"。
        """
        try:
            d = kb.labor_norm_default(activity_id)
        except Exception:
            d = None
        if not d or _to_float(d.get("norm_value"), 0.0) <= 0:
            return None
        unit = d.get("norm_unit") or ""
        if not unit:
            unit = "工日/" + (kb_units.normalize_unit(d.get("quantity_unit")) or "单位")
        return {"norm_id": "", "condition_text": "L4_Norm_Default（人工默认定额）",
                "norm_value": d.get("norm_value"),
                "norm_unit": unit,
                "quantity_basis": 1.0,
                "quantity_unit": d.get("quantity_unit"),
                "productivity_value": kb_units.productivity_of(d.get("norm_value")),
                "source_code": d.get("source_code") or "",
                "measure_scope": d.get("measure_scope") or "",
                "status": d.get("review_state") or "",
                "default_crew": d.get("default_crew")}

    def _labor_candidates(self, activity_id):
        """人工定额候选行：优先 `Norm_Labor_Table`，全无值才回退 `L4_Norm_Default`。

        返回 `(rows, from_default)`。`rows` 里每一行都保证 `norm_value>0` 或
        `productivity_value>0` —— 契约 §5-WS3 ④ 的"候选行必须可用"。
        """
        rows = self._labor_rows(activity_id)
        if rows:
            return rows, False
        dflt = self._labor_default_row(activity_id)
        if dflt:
            return [dflt], True
        return [], False

    @staticmethod
    def _median_row(rows):
        """取 productivity_value 的中位数那行（口径与 kb.typical_labor_norm 一致）。"""
        srt = sorted(rows, key=lambda r: _to_float(r.get("productivity_value"), 0.0))
        return srt[len(srt) // 2]

    @staticmethod
    def _tier_adjusted_row(cands, row, leaf_unit, text, ctx):
        """面积↔体积缺墙厚时，把候选行换成与**墙厚档位**自洽的那一行。

        返回 `(row, 档位说明)`；不适用 / 没有可用档位 / 换出来还是同一行 → `(row, "")`。

        墙厚的取值优先级（D5：参数只是中间物，**没有全局默认值**）：
          ① 用户明写的（文本 / 项目参数 / 材料，走 `_resolve_convert_ctx`）；
          ② **定额行适用条件里的厚度档位**（`kb_units.assumed_context` → 上限型档位如
             `≤200mm` 取 0.2 m；开区间如 `>200mm` 取不到值）；
          ③ 两条都取不到 → 不换行（`(row, "")`），由单位校验按 D5 判
             `unusable` + `not_usable_reason`，**绝不退默认值硬算**。
        """
        if not isinstance(row, dict):
            return row, ""
        unit_text = row.get("norm_unit") or row.get("labor_norm_unit") or ""
        if not unit_text:
            unit_text = "工日/" + str(row.get("quantity_unit") or "单位")
        if not _needs_assumed_thickness(leaf_unit, unit_text):
            return row, ""
        got, _src, _note = _resolve_convert_ctx(leaf_unit, unit_text, text, ctx)
        thickness = _to_float(got.get("thickness_m"), 0.0)
        if thickness <= 0:
            # ② 定额条件里的档位值（唯一允许的"推定"来源；取不到就是取不到）
            assumed, _asrc, _anote = kb_units.assumed_context(
                leaf_unit, unit_text, _row_condition_text(row))
            thickness = _to_float((assumed or {}).get("thickness_m"), 0.0)
        if thickness <= 0:
            return row, ""                      # ③ 推定不出 → 不换行、不硬算
        picked, why = _pick_row_by_assumed_thickness(cands, thickness, text)
        if picked is None:
            return row, ""
        if picked is row or (picked.get("norm_id")
                             and str(picked.get("norm_id")) == str(row.get("norm_id") or "")):
            return row, ""
        return picked, why

    def _fill_from_labor_row(self, binding, row, match_type, origin, confidence, note,
                             leaf_unit="", ctx=None, text="", task_quantity=None,
                             condition_note=""):
        """把一行 KB 人工定额写进 binding（含 provenance + 单位校验）。"""
        nv, unit, basis, extra = _labor_norm_of(row, note)
        # D5 量级校验要用任务原始工程量（写进 binding 内部键，值原样保留、不被改写）。
        if task_quantity is None:
            task_quantity = (ctx or {}).get("_task_quantity")
        if task_quantity is not None:
            binding["_task_quantity"] = _to_float(task_quantity, 0.0)
        if condition_note:
            extra = _append_note(extra, condition_note)
        binding["mode"] = "labor"
        binding["norm_value"] = round(nv, 6) if nv > 0 else None
        binding["unit"] = unit or ""
        # KB 只写了「工日」没写分母时，用**KB 的 quantity_unit** 补（绝不用叶子单位补）
        if not kb_units.denominator_of(binding["unit"]):
            qty_unit = kb_units.normalize_unit(row.get("quantity_unit"))
            if qty_unit:
                lab = kb_units.parse_norm_unit(binding["unit"])["labor_unit"] or "工日"
                binding["unit"] = "%s/%s" % (lab, qty_unit)
        binding["condition_text"] = row.get("condition_text") or ""
        binding["norm_id"] = row.get("norm_id") or binding.get("norm_id") or ""
        binding["quantity_basis"] = basis          # 仅溯源（labor 侧不参与乘法）
        # 定额分母的计量对象（契约 §1）：口径关（_apply_measure_scope）要跟 task_scope 比。
        # 列不存在时 `''`（未填）→ 只打 basis_unconfirmed，不阻断。
        binding["norm_measure_scope"] = _known_scope(row.get("measure_scope"))
        # 产能（单位/工日）= 1 / 定额值。labor_norm_value 入库时**已归一**
        # （raw_value / raw_quantity_basis == norm_value），quantity_basis 只是
        # 原始书页基数；写成 basis/nv 会把产能放大 basis 倍（最大 1000 倍）。
        binding["productivity_value"] = (round(1.0 / nv, 6) if nv > 0 else None)
        binding["source_code"] = row.get("source_code") or ""
        binding["match_type"] = match_type
        binding["provenance"] = {
            "value": binding["norm_value"],
            "origin": origin,
            "ref": binding["source_code"],
            "confidence": confidence,
            "note": extra,
        }
        # 单位校验：唯一真源 kb_units.check_unit_pair（默认拒绝）
        convert_ctx = None
        ctx_source = ""
        if kb_units.unit_family(leaf_unit) != kb_units.unit_family(
                kb_units.denominator_of(binding["unit"])):
            convert_ctx, src, cnote = _resolve_convert_ctx(
                leaf_unit, binding["unit"], text, ctx,
                row_condition=_row_condition_text(row))
            if not convert_ctx:
                # ④ 具名假设（第 41 轮）：**真实定额、量纲确实对不上、用户也没给参数**，
                # 但这条定额本身就是"按厚度分层的墙体定额"（实测 LDT724_砌块墙：
                # 「混凝土空心砌块，>200mm」工日/m³ vs 任务 m²）。这时不再一律判
                # unusable —— 那会让 18 条「N-N层 ALC墙板安装」一条班组都算不出来 ——
                # 而是补一个**写明的**默认墙厚，来源与说明逐处留痕（见 kb_units）。
                # ⚠️ 假定值不是从条件文本里读出来的；工程量本身**一个字都不改**。
                assumed, asrc, anote = kb_units.assumed_context(
                    leaf_unit, binding["unit"], _row_condition_text(row))
                if assumed:
                    convert_ctx, src, cnote = assumed, asrc, anote
                    binding["unit_assumption"] = dict(assumed, source=asrc, note=anote)
            ctx_source = src
            binding["ctx_source"] = src
            binding["ctx_value"] = dict(convert_ctx)
            if src == "ai_estimate":
                binding["coverage_reason"] = "AI估算换算参数"
            if src:
                extra = _append_note(extra, cnote)
                binding["provenance"]["note"] = extra
        self._apply_unit_check(binding, leaf_unit, convert_ctx, ctx_source,
                               _row_condition_text(row))

    def _resolve_activity_conflict(self, conflict_note, old_activity_id, info, leaf,
                                   task_name, binding, leaf_unit, ctx, text, warnings):
        """绑定不一致时的第一处置：在**同 L3** 内改绑到真正对得上的活动（契约 §3）。

        为什么先改绑而不是直接降级：模型给的 `kb_activity_id` 张冠李戴时（实测
        `1.1.1 场地平整` 绑到「场地硬化」、`7.1.3 暖通预留预埋` 绑到「风管制作安装」），
        降级只是把它退回模型给的 WBS 工期；同 L3 内往往**存在**一条名字对得上的活动，
        改绑过去才能给出有依据的定额。

        保守原则（任一不满足就**不**改绑 → 调用方按契约 §3 降级为未绑定）：
          · 旧活动取不到 L3（`kb.l3_of_activity` 为空）；
          · 任务名二元组 < 2（名字太短，认不出对得上谁）；
          · 同 L3 里没有任何活动和任务名的二元组交集 ≥ `_REBIND_OVERLAP_MIN`；
          · 候选试绑后台班定额不可用（单位校验 unusable 等）。
        改绑成功时把新活动写回**叶子**（`/sources`、工作面容量回查都要看它）。

        返回 `(new_activity_id | None, 新 info | None, mode)`。
        """
        try:
            wt = kb.l3_of_activity(old_activity_id)
        except Exception:
            wt = None
        if not wt or len(_bigrams(task_name)) < 2:
            return None, None, "labor"
        try:
            acts = kb.l4_for(wt) or []
        except Exception:
            return None, None, "labor"
        want = _bigrams(task_name)
        scored = []
        for a in acts:
            aid = str(a.get("activity_id") or "")
            if not aid or aid == old_activity_id:
                continue
            overlap = len(want & _bigrams(a.get("activity_name")))
            if overlap >= _REBIND_OVERLAP_MIN:
                scored.append((overlap, aid, str(a.get("activity_name") or "")))
        if not scored:
            return None, None, "labor"
        # 与 `_reanchor_machine` 同口径取最强候选（并列时按活动编号取首个，确定性可复现）
        scored.sort(key=lambda x: (-x[0], x[1]))
        new_id, new_name = scored[0][1], scored[0][2]
        new_info = self._activity_info(new_id) or {}
        new_mode = ("machine"
                    if new_info.get("recommended_production_mode") == "equipment_driven"
                    else "labor")
        trial = dict(binding)
        try:
            if new_mode == "machine":
                ok = self._bind_machine(new_id, task_name, trial, leaf_unit,
                                        leaf=leaf, ctx=ctx, text=text)
            else:
                # 第 7 批（2026-09-21）：改绑的**试绑**原先也只查 `Norm_Labor_Table`
                # （`_labor_rows`）⇒ 像 `MASON_ALC_PANEL` 这种"人工定额 0 行、
                # 只有 `L4_Norm_Default` 审定量行"的目标活动会被判"试绑失败"，
                # 改绑永远不成立。改用 `_labor_candidates()`（有默认行回退），
                # 与路径② 的口径一致 —— 否则第 1 点的接线对这类型活动等于空转。
                rows = self._labor_candidates(new_id)[0]
                ok = False
                if rows:
                    med = self._median_row(rows)
                    med, tier_note = self._tier_adjusted_row(rows, med, leaf_unit, text, ctx)
                    self._fill_from_labor_row(trial, med, "default", "kb", "中",
                                              _append_note("同 L3 内改绑以对齐活动名", tier_note),
                                              leaf_unit, ctx, text)
                    ok = True
        except Exception:
            return None, None, "labor"
        # 单位校验 unusable（如 KB 缺分母 / 量纲对不上）→ 不算改绑成功
        if not ok or not trial.get("norm_is_evidence"):
            return None, None, "labor"
        if not _to_float(trial.get("norm_value"), 0.0):
            return None, None, "labor"
        binding.clear()
        binding.update(trial)
        binding["match_type"] = "default"
        binding["reanchored_from"] = old_activity_id
        binding["activity_conflict"] = conflict_note
        prov = binding.get("provenance")
        if isinstance(prov, dict):
            prov["note"] = _append_note(
                prov.get("note"),
                "绑定一致性校验：%s —— 已在同 L3「%s」内改绑为 %s（%s）"
                % (conflict_note, wt, new_id, new_name))
        if isinstance(leaf, dict):
            leaf["kb_activity_id"] = new_id
            leaf["wbs_warnings"] = list(leaf.get("wbs_warnings") or []) + [
                "[绑定] %s（%s）%s —— 已在同 L3 内改绑为 %s（%s）"
                % (task_name, leaf.get("id") or "?", conflict_note, new_id, new_name)]
        warnings.append(self._info_warning(
            leaf, task_name,
            "任务 %s（%s）：%s —— 已在同工种「%s」内改绑为 %s（%s）。"
            % (leaf.get("id"), task_name, conflict_note, wt, new_id, new_name),
            "按改绑后的定额计算", kind="reanchor"))
        return new_id, new_info, new_mode

    def _reanchor_machine(self, old_activity_id, task_name, binding, leaf, leaf_unit="",
                          ctx=None):
        """任务像机械作业、却绑到了人工活动 → 尝试在**同工种大类**内改绑机械活动。

        为什么值得做：`kb_activity_id` 是模型给的，可能张冠李戴（实测把「机械挖基坑
        土方 3500 m³」绑到人工挖小坑活动）。只把冲突"降级为非证据"只是退回 WBS 工期；
        改绑到真正对得上的机械活动，才能给出**有依据**的定额。

        保守原则（任一不满足就**不改绑**，保持冲突标记）：
          · 同工种大类（`work_type_id`）里找不到机械主导活动；
          · 最强候选与次强并列（说明分不清）；
          · 相似度不足（二元组交集 < 2）；
          · 该活动没有台班定额（`_bind_machine` 会返回 False）；
          · 改绑后单位校验仍 unusable（如「台班」无分母）→ 视为改绑不到。

        成功时：把 binding 换成机械口径、在 provenance 里留痕、并把叶子的
        `kb_activity_id` 一并改掉 —— 否则 `/sources`、工作面容量回查还会指着旧活动。
        """
        wt = kb.l3_of_activity(old_activity_id)
        if not wt:
            return False
        want = _bigrams(task_name)
        if len(want) < 2:
            return False
        try:
            acts = kb.l4_for(wt) or []
        except Exception:
            return False
        scored = []
        for a in acts:
            if str(a.get("recommended_production_mode") or "") != "equipment_driven":
                continue
            aid = str(a.get("activity_id") or "")
            if not aid or aid == old_activity_id:
                continue
            score = len(want & _bigrams(a.get("activity_name")))
            if score >= _REBIND_OVERLAP_MIN:
                scored.append((score, aid, str(a.get("activity_name") or "")))
        if not scored:
            return False
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[0][0]
        tops = [s for s in scored if s[0] == top]
        # 并列时**确定性取一个并在说明里写清并列候选**，而不是放弃改绑：
        # 并列往往发生在"机械挖土方 / 机械挖装土方"这类近义活动之间，选哪个都远好于
        # 原来的"人工挖小坑"。真正该拒绝的是"最强候选都不太像"，那由相似度阈值把关。
        tie_note = ""
        if len(tops) > 1:
            tie_note = ("（并列候选 %d 个：%s；按活动编号取首个）"
                        % (len(tops),
                           "、".join("%s %s" % (t[1], t[2]) for t in tops[:4])))
        new_id, new_name = tops[0][1], tops[0][2]
        return self._swap_to_machine(
            new_id, new_name, old_activity_id, task_name, binding, leaf, leaf_unit, ctx,
            "原绑定 %s 标注为非机械主导，已在同工种内改绑机械活动 %s（%s）%s"
            % (old_activity_id, new_id, new_name, tie_note))

    def _reanchor_to_hint(self, baseline, old_activity_id, task_name, binding, leaf, ctx=None):
        """按量级基线的 `machine_activity_hint` 精确改绑（不需要相似度打分）。"""
        hint = str((baseline or {}).get("machine_activity_hint") or "").strip()
        if not hint:
            return False
        hinfo = self._activity_info(hint)
        if not hinfo or hinfo.get("recommended_production_mode") != "equipment_driven":
            return False
        return self._swap_to_machine(
            hint, hinfo.get("activity_name") or "", old_activity_id, task_name,
            binding, leaf, leaf.get("unit") or "", ctx,
            "量级基线：原绑定 %s（%s）非机械主导，已改绑机械活动 %s（%s）"
            % (old_activity_id, (self._activity_info(old_activity_id) or {}).get("activity_name") or "",
               hint, hinfo.get("activity_name") or ""))

    def _swap_to_machine(self, new_id, new_name, old_activity_id, task_name, binding, leaf,
                         leaf_unit, ctx, note):
        """试绑机械活动成功且单位可用 → 换掉 binding 并改叶子的 kb_activity_id。"""
        trial = dict(binding)
        try:
            ok = self._bind_machine(new_id, task_name, trial, leaf_unit, leaf=leaf, ctx=ctx)
        except Exception:
            return False
        # 单位校验 unusable（如 KB 缺分母）→ 不算改绑成功，保留原口径与冲突标记
        if not ok or not trial.get("norm_is_evidence"):
            return False

        binding.clear()
        binding.update(trial)
        binding.pop("method_conflict", None)   # 已改绑成功 → 冲突解除
        binding["reanchored_from"] = old_activity_id
        prov = binding.get("provenance")
        if isinstance(prov, dict):
            prov["note"] = _append_note(prov.get("note"), note)
        if isinstance(leaf, dict):
            leaf["kb_activity_id"] = new_id
        return True

    def _equip_condition_map(self, activity_id):
        """该 L4 台班行的 `condition_combination` **只读**取值（裁定-3 需要结构化的条件）。

        为什么不用 `kb.equipment_norms()`：它对 `Norm_Equipment_Table` 只 SELECT 了
        condition_text/machine_*/quantity_*/source_code，**没有取 `condition_combination`**
        —— 而那正是"按条件精确查"唯一可用的结构化维度（`condition_text` 是把维度拼成人话
        的结果，取值只能靠正则猜）。`kb.equipment_norms` 的行序是 `ORDER BY condition_text`，
        这里必须用**同一个排序**才能按下标对上号。

        只读、带缓存、永不抛错（DB 不可用 → 空 dict，精筛自动退化为"不做"）。
        """
        if not activity_id:
            return {}
        key = "__equip_cc__" + str(activity_id)
        if key in self._mode_cache:
            return self._mode_cache[key]
        out = {}
        try:
            rows = kb._query_all(
                "SELECT condition_text, condition_combination FROM Norm_Equipment_Table "
                "WHERE activity_id = ? ORDER BY condition_text", (activity_id,))
            for cond_text, comb in rows:
                if comb:
                    out[str(cond_text or "")] = comb
        except Exception:
            out = {}
        self._mode_cache[key] = out
        return out

    def _equip_candidates(self, rows, leaf, task_name="", condition_text="", cc_map=None):
        """裁定-3：把机械选行也接到「锁条件 → 精确匹配」上（**只做有数据支撑的部分**）。

        实测数据（`BuildPlan_KB/kb.db`，2026-09-21，见 `_probe_tmp/p3_equip_conditions.py`）：
          · `Norm_Equipment_Table` 245 行 / 63 个 L4；`condition_combination` 空 17 行（7%），
            即 228 行（93%）带条件组合 —— **不是"大量空值"**；
          · 但每行维度极少：1 维 188 行、2 维 40 行、0 维 17 行（**没有 ≥3 维**）；
          · 维度只有 10 个：条件116 / 岩类别41 / 未分类28 / 石方类别26 / 土类别18 /
            子目名称16 / 运距9 / 构件类型6 / 挡土板类型4 / 部位4；其中 `条件`(116)、
            `未分类`(28)、`子目名称`(16) 是**元字段**（D2 的 `_META_CONDITION_KEYS`），
            不参与条件比对 → **真正可比的只剩 5 个维度、合计 103 行**；
          · 与 `Norm_Labor_Table` 共有的可比维度只有 `构件类型/土类别/岩类别/石方类别/运距/部位`，
            且 `构件类型` 在机械表仅 6 行（人工表 3221 行）。

        结论：**不能**像人工侧那样"用该 L4 的典型条件锁死全部维度再精筛" —— 机械行普遍
        只有 1 个可比维度，锁全会把 93% 的行筛掉。可以做且已做的是**有数据支撑的那部分**：
        只拿"叶子条件字段里明写的、且在该 L4 台班行的**结构化维度**里出现的"条件做精确匹配；
        一个可用维度都没有 → 候选集原样保留（不硬做），继续走去重口径的主控机械名选行。

        永不抛错；返回 `(cands, record)`，`record` 是审计留痕（**只写 binding 的
        `equipment_condition_*` 键，不进 `basis_adjust` 六字段**）。
        """
        rec = {"activity_id": "", "rows_total": len(rows or []), "rows_matched": 0,
               "available_keys": [], "matched_keys": [], "matched_values": {},
               "note": ""}
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        if not rows:
            return rows, rec
        # 把 kb.equipment_norms 的行补上 condition_combination（同排序 → 按 condition_text 对齐）
        cc_map = cc_map if isinstance(cc_map, dict) else {}
        rows = [dict(r, condition_combination=(r.get("condition_combination")
                                               or cc_map.get(str(r.get("condition_text") or ""))))
                for r in rows]
        want = {}
        if isinstance(leaf, dict):
            for key in ("condition_key", "condition", "condition_text"):
                val = leaf.get(key)
                if isinstance(val, dict):
                    for dim, v in val.items():
                        want.setdefault(str(dim), [str(x) for x in _condition_values(v)])
            for dim, val in re.findall(
                    r"([\u4e00-\u9fff]{2,8})\s*[=＝:：]\s*([^,，;；、|]+)",
                    str(condition_text or "")):
                want.setdefault(dim.strip(), [val.strip()])
        # 该 L4 台班行**真的有**哪些结构化维度（元字段不算）
        avail = sorted({dim for r in rows
                        for dim in _condition_combination_of(r)
                        if dim not in _META_CONDITION_KEYS})
        rec["available_keys"] = avail
        matched = [(dim, vals) for dim, vals in want.items()
                   if dim not in _META_CONDITION_KEYS and dim in avail and any(vals)]
        if not matched:
            rec["note"] = ("该 L4 的台班行没有可用的结构化条件维度与用户明写的条件对齐"
                           "（可用维度：%s）—— 保持既有选行口径，未做条件精筛"
                           % ("、".join(avail) or "无"))
            return rows, rec
        hit_keys, used_values = [], {}
        for dim, vals in matched:
            usable = [v for v in vals if any(
                _cond_cell_matches(_condition_combination_of(r).get(dim), v)
                for r in rows)]
            if usable:
                hit_keys.append(dim)
                used_values[dim] = usable[0]
        rec["matched_keys"] = hit_keys
        rec["matched_values"] = used_values
        if not hit_keys:
            rec["note"] = ("用户写明的条件 %s 在该 L4 的全部台班行里都匹配不上 —— "
                           "保持既有选行口径（由主控机械名决定），并留痕待审"
                           % ("、".join("%s=%s" % (d, matched[0][1] and matched[0][1][0])
                                        for d, _ in matched)))
            return rows, rec
        cands = [r for r in rows if all(
            _cond_cell_matches(_condition_combination_of(r).get(dim), val)
            for dim, val in used_values.items())]
        if not cands:
            rec["note"] = "按条件精筛后候选为空 —— 已回退全部台班行（不硬筛）"
            return rows, rec
        rec["rows_matched"] = len(cands)
        rec["note"] = ("机械条件精筛：按 %s（该 L4 可比的维度）把 %d 行缩到 %d 行"
                       % ("、".join("%s=%s" % (d, v) for d, v in used_values.items()),
                          len(rows), len(cands)))
        return cands, rec

    def _bind_machine(self, activity_id, task_name, binding, leaf_unit="", leaf=None,
                      ctx=None, text=""):
        """机械主导：取台班定额 + 主控机械。成功返回 True。

        选行口径（第 37 轮修）：**先按主控机械名匹配 `machine_combination_json`**，
        同名多行才用 condition_text 消歧；主控机械不在任何行 → 降级"仅参考"
        （`not_usable_reason="主控机械缺台班定额（<机名>）"`），**绝不借同行机械**。

        分母**只能**来自 KB 行的 `quantity_unit`（见 `_shift_unit_from_kb`）；
        KB 缺分母 → 单位校验 unusable、`not_usable_reason` 写"缺计量单位"、定额降级
        "仅参考"（仍返回 True，让调用方看到 mode=machine + 降级标记，而不是静默通过）。
        """
        try:
            rows = kb.equipment_norms(activity_id) or []
        except Exception:
            rows = []
        if not rows:
            return False

        # 裁定-3：先按"叶子明写条件 × 该 L4 台班行的可比维度"做**精确**精筛，
        # 再做既有的"主控机械名选行"（精筛只缩小候选，不改选机构判）。
        rows_all = list(rows)
        leaf_cond_text = ""
        if isinstance(leaf, dict):
            for key in ("condition_text", "condition_key"):
                val = leaf.get(key)
                if isinstance(val, str) and val.strip():
                    leaf_cond_text = _append_note(leaf_cond_text, val)
                elif isinstance(val, dict):
                    leaf_cond_text = _append_note(
                        leaf_cond_text,
                        "、".join("%s=%s" % (k, v) for k, v in val.items()))
        rows, equip_rec = self._equip_candidates(
            rows, leaf, task_name, leaf_cond_text, self._equip_condition_map(activity_id))
        equip_rec["activity_id"] = str(activity_id or "")
        binding["equipment_condition_key"] = dict(equip_rec.get("matched_values") or {})
        binding["equipment_condition_available_keys"] = list(
            equip_rec.get("available_keys") or [])
        binding["equipment_condition_note"] = equip_rec.get("note") or ""
        binding["equipment_rows_total"] = equip_rec.get("rows_total")
        binding["equipment_rows_matched"] = equip_rec.get("rows_matched")

        try:
            machines = kb.main_machine(activity_id) or []
        except Exception:
            machines = []
        # 只在主控机械"有名字"时才用它，不瞎猜
        main = next((m for m in machines if (m.get("machine_name") or "").strip()), None)

        note_parts = []
        if equip_rec.get("matched_keys"):
            note_parts.append(equip_rec["note"])
        if main:
            machine_name = main["machine_name"]
            condition_text = (main.get("condition_text") or "").strip()
            leaf_condition = (leaf.get("condition_text") or "") \
                if isinstance(leaf, dict) and isinstance(leaf.get("condition_text"), str) else ""
            row, idx, hit_name = _pick_machine_row(rows, machine_name,
                                                   condition_text, leaf_condition,
                                                   task_name)
            if row is None:
                # 主控机械缺台班定额 → 不借同行机械，直接降级"仅参考"
                # （按**全部**台班行判"缺行"，不受条件精筛影响）
                return self._downgrade_missing_machine(
                    activity_id, binding, machine_name, rows_all)
            norm_value, unit, basis, hit_name = _machine_norm_at(row, idx)
            if norm_value <= 0:
                return self._downgrade_missing_machine(
                    activity_id, binding, machine_name, rows_all)
            note_parts.append("主控机械：%s" % machine_name)
            note_parts.append("台班定额行：%s" % (row.get("condition_text") or "（无条件标注）"))
            src_note = main.get("source_type") or ""
            if src_note:
                note_parts.append("主控机械来源：%s(%s)" % (src_note, main.get("confidence") or ""))
        else:
            row = rows[0]
            machine_name = ""
            norm_value, unit, basis, hit_name = self._first_machine_of_row(row)
            note_parts.append("未标注主控机械")
            if hit_name:
                note_parts.append("暂取该活动首行机械：%s" % hit_name)
            if norm_value <= 0:
                return False

        source_code = row.get("source_code") or ""
        kb_qty_unit = kb_units.normalize_unit(row.get("quantity_unit"))
        unit_text = _shift_unit_from_kb(unit, kb_qty_unit)
        binding["mode"] = "machine"
        binding["norm_value"] = round(norm_value, 6)
        # 定额单位 = 台班 / KB 的 quantity_unit；KB 缺分母时保留裸「台班」，由单位校验
        # 判 unusable（下游 units_compatible('根','台班') 为假，不会静默通过）。
        binding["unit"] = unit_text or (kb_units.normalize_unit(unit) or "台班")
        binding["kb_quantity_unit"] = kb_qty_unit
        binding["condition_text"] = row.get("condition_text") or ""
        binding["quantity_basis"] = basis     # 机械台班**未归一**，下游照旧乘 basis
        # 台班定额分母的计量对象（契约 §1/§2），口径关据此与 task_scope 比。
        binding["norm_measure_scope"] = _known_scope(row.get("measure_scope"))
        binding["source_code"] = source_code
        binding["match_type"] = "exact" if main else "default"
        binding["machine_name"] = machine_name or hit_name or ""
        conf = str((main or {}).get("confidence") or "").upper()
        if main and conf == "HIGH":
            confidence = "高"
        elif main and conf == "MEDIUM":
            confidence = "中"
        elif main:
            confidence = "低"
        else:
            confidence = "低"        # 未标注主控机械 → 暂用首行机械，置信度只能给低
        binding["provenance"] = {
            "value": binding["norm_value"],
            "origin": "kb",
            "ref": source_code,
            "confidence": confidence,
            "note": _append_note("机械台班定额（台班/基准 %s%s）" % (
                basis, kb_qty_unit), "；".join(note_parts)),
        }
        # 单位校验 + 换算参数（分母缺失时 convert_ctx=None → unusable + 缺计量单位）
        convert_ctx = None
        ctx_source = ""
        if unit_text:
            conv_text = text or self._convert_text(ctx, leaf or {}, "", task_name)
            convert_ctx, src, cnote = _resolve_convert_ctx(
                leaf_unit, unit_text, conv_text, ctx,
                row_condition=_row_condition_text(row))
            if not convert_ctx:
                # ④ 具名假设（同 labor 侧，见 _fill_from_labor_row 的说明）
                assumed, asrc, anote = kb_units.assumed_context(
                    leaf_unit, unit_text, _row_condition_text(row))
                if assumed:
                    convert_ctx, src, cnote = assumed, asrc, anote
                    binding["unit_assumption"] = dict(assumed, source=asrc, note=anote)
            ctx_source = src
            binding["ctx_source"] = src
            binding["ctx_value"] = dict(convert_ctx)
            if src == "ai_estimate":
                # AI 估算的换算参数：口径仍可用，但覆盖率里单列（WS4 读 coverage_reason）
                binding["coverage_reason"] = "AI估算换算参数"
            if src:
                note_parts.append(cnote)
                binding["provenance"]["note"] = _append_note(
                    "机械台班定额（台班/基准 %s%s）" % (basis, kb_qty_unit),
                    "；".join(note_parts))
        if isinstance(leaf, dict) and leaf.get("quantity") is not None:
            binding["_task_quantity"] = _to_float(leaf.get("quantity"), 0.0)
        self._apply_unit_check(binding, leaf_unit, convert_ctx, ctx_source,
                               _row_condition_text(row))
        return True

    def _downgrade_missing_machine(self, activity_id, binding, machine_name, rows):
        """主控机械在台班表里找不到行 → 降级"仅参考"，**不借同行机械的定额**。

        写 `not_usable_reason="主控机械缺台班定额（<机名>）"` + `usable=False` +
        `norm_is_evidence=False`（scheduler 台账层优先读绑定层的原因 → 覆盖率桶里
        出现同一个标签）。`norm_value=None`：宁可不用定额，也不拿别的机械的台班数
        冒充这台机械（旧实现借了同行机械 → CONC_NEW_FOUND 选中后浇带 1.26 台班）。
        """
        row = rows[0] if rows else {}
        kb_qty_unit = kb_units.normalize_unit(row.get("quantity_unit"))
        units = [str(x) for x in _json_list(row.get("machine_shift_unit_json"))] or ["台班"]
        unit_text = _shift_unit_from_kb(units[0], kb_qty_unit)
        binding["mode"] = "machine"
        binding["norm_value"] = None
        binding["unit"] = unit_text or (kb_units.normalize_unit(units[0]) or "台班")
        binding["kb_quantity_unit"] = kb_qty_unit
        binding["condition_text"] = ""
        binding["quantity_basis"] = _to_float(row.get("quantity_basis"), 1.0) or 1.0
        binding["source_code"] = ""
        binding["match_type"] = "default"
        binding["machine_name"] = machine_name
        binding["provenance"] = {
            "value": None,
            "origin": "kb",
            "ref": activity_id or "",
            "confidence": "低",
            "note": _append_note(
                "机械台班定额缺失",
                "主控机械「%s」不在该活动的台班表（%d 行）里，不借同行机械的定额 → "
                "降级为仅参考、沿用 WBS 工期" % (machine_name, len(rows))),
        }
        _set_usable(binding, False, "主控机械缺台班定额（%s）" % machine_name)
        return True

    @staticmethod
    def _first_machine_of_row(row):
        """取一行机械定额里第一台有台班数的机械。返回 (定额, 单位, 基准, 机械名)。"""
        if not row:
            return 0.0, "台班", 1.0, ""
        basis = _to_float(row.get("quantity_basis"), 1.0) or 1.0
        machines = [str(x) for x in _json_list(row.get("machine_combination_json"))]
        shifts = _json_list(row.get("machine_shift_norm_json"))
        units = [str(x) for x in _json_list(row.get("machine_shift_unit_json"))] or ["台班"]
        for i, m in enumerate(machines):
            shift = _to_float(shifts[i] if i < len(shifts) else None, 0.0)
            if shift > 0:
                unit = units[i] if i < len(units) else "台班"
                return shift, unit, basis, m
        return 0.0, (units[0] if units else "台班"), basis, ""

    def _estimate_by_experience(self, leaf, activity_id, scope_map):
        """无定额时的经验估算：L3 工种 → 经验产能 → 折算成「工日/单位」。

        返回 (norm_value, labor_type, unit)。
        """
        labor_type = self._labor_type_of(leaf, activity_id, scope_map)
        productivity = _EXPERIENCE_PRODUCTIVITY.get(labor_type, _EXPERIENCE_DEFAULT)
        if productivity <= 0:
            productivity = _EXPERIENCE_DEFAULT
        return round(1.0 / productivity, 6), labor_type, "工日/%s" % (leaf.get("unit") or "单位")

    def _labor_type_of(self, leaf, activity_id, scope_map):
        """工种优先级：kb_scope 给的 labor_type → KB 的 L3/L4 规则 → L3 名兜底。"""
        scope = scope_map.get(str(leaf.get("id") or "")) or {}
        lt = scope.get("labor_type")
        if lt:
            return str(lt)
        if activity_id:
            try:
                info = kb.labor_type_for_activity(activity_id)
            except Exception:
                info = None
            if info and info.get("labor_types"):
                return str(info["labor_types"][0])
        # 最后兜底：用叶子 work_type（L3 名）猜工种
        wt = str(leaf.get("work_type") or "")
        for name in _EXPERIENCE_PRODUCTIVITY:
            if name in wt:
                return name
        return "普工" if wt else ""

    @staticmethod
    def _info_warning(leaf, task_name, reason, impact="不影响工期口径", kind="info"):
        """提示性（非 AI 估算）警告。

        ⚠️ 结构必须与 `_make_warning` **完全一致**：早先这里直接
        `warnings.append("...")` 塞了个纯字符串，而其它全是 dict，于是
        `warnings.sort(key=lambda w: w.get(...))` 抛
        `'str' object has no attribute 'get'` —— 整条流水线在定额锚定节点崩掉。
        """
        return {"task_id": str(leaf.get("id") or ""), "task_name": task_name or "",
                "reason": reason, "impact": impact, "kind": kind, "_impact_days": 0.0}

    def _as_warnings(self, got, leaf, task_name):
        """把 `_bind_one` 的第二项归一成 `list[dict]`。

        为什么要有这层：`run()` 里是 `warnings.extend(leaves_warn)`，
        只要 `_bind_one` 有一次返回**字符串**，`extend` 就会把它的**每个字符**
        当成一条警告塞进列表，最后在 `warnings.sort(key=... .get)` 处炸掉
        （实测：`norm_bind: 'str' object has no attribute 'get'`，
        且只在「机械任务改绑成功」这条少见的补救路径上触发）。
        这里做一次归一，任何非 dict 一律包成 dict，绝不让它再把整条流水线打断。
        """
        if got is None:
            return []
        if isinstance(got, (dict, str)):
            got = [got]
        out = []
        for w in got:
            if isinstance(w, dict):
                out.append(w)
            else:
                out.append(self._info_warning(leaf, task_name, str(w), "—"))
        return out

    def _make_warning(self, leaf, task_name, activity_id, binding, labor_type):
        """AI 估算的警告：能算出影响天数就算，算不出写"暂无法估量"。"""
        reason = "匹配不上，已用 L3 估算" if activity_id else "无 KB 活动映射，已用 L3 估算"
        if activity_id and not self._activity_info(activity_id):
            reason = "KB 中不存在活动 %s，已用 L3 估算" % activity_id

        impact_days, impact_text = self._impact_of(leaf, binding, activity_id, labor_type)
        return {
            "task_id": str(leaf.get("id") or ""),
            "task_name": task_name or "",
            "reason": reason,
            "impact": impact_text,
            "_impact_days": impact_days,     # 仅用于排序，最终会剔除
        }

    def _impact_of(self, leaf, binding, activity_id, labor_type):
        """估算 AI 定额对工期的影响（天）：与同 L3 的 KB 典型定额对比。"""
        qty = _to_float(leaf.get("quantity"), 0.0)
        if qty <= 0:
            return 0.0, "暂无法估量（缺工程量）"
        norm = _to_float(binding.get("norm_value"), 0.0)
        if norm <= 0:
            return 0.0, "暂无法估量"

        base = None
        if labor_type:
            base = self._typical_norm_by_labor_type(activity_id, labor_type)
        if base is None or base <= 0:
            return 0.0, "暂无法估量"

        extra = (norm - base) * qty
        if extra <= 0:
            return 0.0, "估算定额不低于 KB 典型值，预计不增加工期"
        return round(extra, 1), "影响工期约 +%.0f 天（估算 %s vs 典型 %s 工日/单位 × %s）" % (
            extra, norm, round(base, 4), qty)

    def _typical_norm_by_labor_type(self, activity_id, labor_type):
        """找同 L3 下、同一工种的 KB 典型人工定额，作为影响天数对比基准。"""
        if not activity_id:
            return None
        try:
            l3 = kb.l3_of_activity(activity_id)
        except Exception:
            l3 = None
        if not l3:
            return None
        try:
            acts = kb.l4_for(l3) or []
        except Exception:
            return None
        for act in acts[:12]:
            aid = act.get("activity_id")
            if not aid or aid == activity_id:
                continue
            typ = kb.typical_labor_norm(aid)
            if not typ or not typ.get("norm"):
                continue
            nv, _unit, _basis, _note = _labor_norm_of(typ["norm"])
            if nv > 0:
                return nv
        return None

    # ---------------- LLM 分支 ----------------
    def _llm_pick(self, ctx, leaf, task_name, hit):
        """候选多行时请 LLM 选一行。返回 (候选行 | None, 警告 dict | None)。"""
        if not self.llm_usable:
            return None, self._llm_warning(leaf, task_name, "LLM 不可用（未配置），已用代码策略")
        payload = {
            "task_id": str(leaf.get("id") or ""),
            "task_name": task_name or "",
            "unit": leaf.get("unit") or "",
            "quantity": leaf.get("quantity"),
            "work_type": leaf.get("work_type") or "",
            "kb_activity_id": leaf.get("kb_activity_id") or "",
        }
        cands = [{
            "norm_id": r.get("norm_id"),
            "condition_text": r.get("condition_text"),
            "condition_combination": r.get("condition_combination"),
            "labor_norm_value": r.get("norm_value"),
            "labor_norm_unit": r.get("norm_unit"),
            "productivity_value": r.get("productivity_value"),
            "source_code": r.get("source_code"),
        } for r in hit]
        context = {
            "prompt": (ctx.get("prompt") or "")[:800],
            "extracted_params": ctx.get("extracted_params") or {},
        }
        try:
            text = load("norm_match.txt")
            user = (text.replace("{task_json}", json.dumps(payload, ensure_ascii=False))
                        .replace("{context_json}", json.dumps(context, ensure_ascii=False))
                        .replace("{candidates_json}", json.dumps(cands, ensure_ascii=False)))
            raw = self._llm().chat_json(
                "你是施工定额选行助手，只输出 JSON，不得编造 norm_id。", user, temperature=0.1)
        except (LLMError, Exception) as e:
            return None, self._llm_warning(
                leaf, task_name, "LLM 不可用（%s），已用代码策略" % str(e)[:60])

        chosen = (raw or {}).get("chosen_norm_id")
        for row in hit:
            if row.get("norm_id") == chosen:
                return row, None
        # 返回候选外的 id → 丢弃，退回代码策略
        return None, self._llm_warning(
            leaf, task_name, "LLM 返回的 norm_id 不在候选内，已丢弃并退回代码策略")

    @staticmethod
    def _llm_warning(leaf, task_name, text):
        return {"task_id": str(leaf.get("id") or ""), "task_name": task_name or "",
                "reason": text, "impact": "暂无法估量", "_impact_days": 0.0}

    # ---------------- 溯源 / 汇总 ----------------
    @staticmethod
    def _wbs_quantity_source(ctx):
        """WBS 里的工程量到底是谁给的 —— 决定 quantity 溯源的默认口径。

        历史口径一律标 origin="user"（用户提供），这是**高估**：WBS 的工序与工程量
        大多由大模型生成或取自项目模板，并不是用户报上来的数。改为按 wbs_source
        （wbs_gen.py / wbs_agent.py 写入）标注真实来源：
          llm      → ai       （大模型生成）
          template → default  （项目模板生成）
          标注过但为空 → unknown（不猜）
          压根没有该字段 → 老调用方（单测/脚本直接喂 WBS）未接入来源标注，
                          保留历史口径 user，避免静默改变这类调用方的行为。
        无论哪一种，都只影响**新建**的溯源；叶子上已有的 provenance.quantity
        （上游节点标注过，如用户总量推算而来）一律保留，见 _provenance_of。
        """
        wbs_source = ctx.get("wbs_source") or ""
        if wbs_source == "llm":
            return {"origin": "ai", "ref": "WBS 由大模型生成", "confidence": "低",
                    "note": "工程量随 WBS 由大模型生成，非用户提供"}
        if wbs_source == "template":
            return {"origin": "default", "ref": "WBS 由项目模板生成", "confidence": "低",
                    "note": "工程量随 WBS 取自项目模板；可信度口径中归入 ai"}
        if "wbs_source" in ctx:
            return {"origin": "unknown", "ref": "来源未标注", "confidence": "",
                    "note": "工程量来源未标注；可信度口径中归入 ai"}
        return {"origin": "user", "ref": "用户输入", "confidence": "高",
                "note": "调用方未接入 wbs_source 来源标注，沿用历史口径"}

    @staticmethod
    def _provenance_of(leaf, binding, task_id, qty_source=None):
        """叶子自己的溯源表：quantity / norm / duration 三条，字段名固定 provenance。

        qty_source: _wbs_quantity_source(ctx) 的结果；缺省按"来源未标注"处理。
        """
        prov = leaf.get("provenance")
        if not isinstance(prov, dict):
            prov = {}
        # 工程量：**已有的溯源一律保留**（上游若标过 origin=user 之类，不许覆盖）；
        # 没有才按 WBS 的真实来源新建。
        if not isinstance(prov.get("quantity"), dict):
            src = qty_source if isinstance(qty_source, dict) else {
                "origin": "unknown", "ref": "来源未标注", "confidence": "", "note": ""}
            origin = src.get("origin") or "unknown"
            note = src.get("note") or ""
            if leaf.get("_rollup"):
                # L3 汇总行（quantity.rollup_to_l3 的产物）：量是汇总/拆分出来的
                origin = "ai"
                note = "L3 汇总行"
            prov["quantity"] = {
                "value": leaf.get("quantity"),
                "origin": origin,
                "ref": src.get("ref") or "",
                "confidence": src.get("confidence") or "",
                "note": note,
            }
        bp = binding.get("provenance") or {}
        prov["norm"] = {
            "value": binding.get("norm_value"),
            "origin": bp.get("origin") or "ai",
            "ref": binding.get("source_code") or bp.get("ref") or "",
            "confidence": bp.get("confidence") or "",
            "note": bp.get("note") or "",
        }
        dur = _to_int(leaf.get("duration_days"), 0)
        if dur > 0:
            prov["duration"] = {
                "value": dur,
                "origin": "ai",
                "ref": "由定额推算",
                "confidence": "中",
                "note": "工期来自 WBS 既有排期；本次只锚定定额，不重排工期",
            }
        return prov

    @staticmethod
    def _credibility(ctx, leaves):
        """可信度 = 全部溯源条目（每条叶子的 quantity / norm / duration）里各来源的占比。

        口径（修正后）：键名固定为 user / kb / ai，绝不出现第四种键。
        - user：确实由用户提供的工程量（上游节点明确标注 origin="user"）
        - kb  ：定额来自数据库
        - ai  ：AI 假设、项目模板默认值、来源未标注 —— 一律归入 ai。
                理由：对读者而言 "default"/"unknown" 与 "ai" 一样，都属于
                **不可追溯到用户与 KB** 的部分，算成 user 就是高估可信度。
                （归并说明同时写在叶子 quantity 溯源的 note 里）
        三个比例相加严格 = 1（末位按 user→kb→ai 顺序吸收四舍五入误差）。
        """
        counts = {"user": 0, "kb": 0, "ai": 0}
        for leaf, _p, _w in leaves:
            prov = leaf.get("provenance") or {}
            if not isinstance(prov, dict):
                continue
            for item in prov.values():
                if not isinstance(item, dict):
                    continue
                origin = item.get("origin")
                if origin == "user":
                    counts["user"] += 1
                elif origin == "kb":
                    counts["kb"] += 1
                else:
                    counts["ai"] += 1          # ai / default / unknown / 空 → 归 ai（保守）
        total = counts["user"] + counts["kb"] + counts["ai"]
        if total <= 0:
            return {"user": 1.0, "kb": 0.0, "ai": 0.0}
        user_ratio = round(float(counts["user"]) / total, 2)
        kb_ratio = round(float(counts["kb"]) / total, 2)
        ai_ratio = round(max(0.0, 1.0 - user_ratio - kb_ratio), 2)
        return {"user": user_ratio, "kb": kb_ratio, "ai": ai_ratio}

    @staticmethod
    def _data_sources(ctx, sources):
        """去重后的来源清单（保留上游已有的来源，顺序稳定）。"""
        out = []
        for s in list(ctx.get("data_sources") or []) + list(sources):
            s = str(s or "").strip()
            if s and s not in out:
                out.append(s)
        return out
