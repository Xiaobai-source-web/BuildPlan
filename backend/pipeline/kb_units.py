# -*- coding: utf-8 -*-
"""单位口径的唯一真源（第 37 轮新增）。

## 为什么需要这个模块

`Norm_Labor_Table` / `Norm_Equipment_Table` 的单位体系曾经同时存在四种写法与两种
截然相反的口径假设，导致工期可以偏 1000 倍（实测：120 根 PHC 桩被算成 1 天，
4260 m³ 基坑土方被算成 118 天）：

1. 同一量纲两种写法：`m3` / `m³`、`m2` / `m²`；`machine_shift_unit_json` 里同时有
   转义写法 `"\\u53f0\\u73ed"` 与字面 `"台班"`。
2. `productivity_value` 与 `labor_norm_value` 的关系有三种落库口径：
   `1/norm`（basis=1 的 2847 行，正确）、`basis/norm`（895 行，偏大 basis 倍）、
   `1/(basis*norm)`（135 行，偏小 basis 倍）。
3. 定额的 `quantity_basis`（10 / 100 / 1000）被下游当乘数用，而 `labor_norm_value`
   其实**已经归一**（= 原始值 / basis，例如原始 0.175 工日/10m² → 落库 0.0175 工日/m²）。
4. 机械定额行只写「台班」不写分母，于是有代码用**叶子任务的单位**把分母补出来
   （`norm_bind._compound_shift_unit`），补完再拿这个分母去校验这个分母 —— 校验必然通过。

## 本模块冻结的口径（下游不得绕过）

- `norm_value` 一律是「工日 / 1 个 quantity_unit」或「台班 / 1 个 quantity_unit」，
  **已归一**。`quantity_basis` 只是原始出处，**不参与任何乘法**。
- 产能 `productivity = 1 / norm_value`。看 `productivity_of()`，不要去乘 basis。
- 机械定额的分母**只能**来自 KB 的 `quantity_unit`；KB 没写就是「不可用」，
  **不许**用叶子单位补（看 `check_unit_pair()` 的 `unusable` 语义）。
- 单位校验是**双向**的，且**解析不出来就拒绝**（默认拒绝，不再默认放行）。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 1. 写法归一
# ---------------------------------------------------------------------------

#: 写法别名 → 规范写法。规范写法全部用上标字符（m³ / m²），与 `labor_norm_unit`
#: 的现有主流写法一致（2146 行 工日/m³、414 行 工日/m²）。
#:
#: ⚠️ 别名的**准入标准**：只收"同一个物理量的另一种写法"，绝不收"另一个量纲"。
#: 例：`㎡`(U+33A1) / `㎥`(U+33A5) 是 CJK 兼容字形的 m²/m³，`延米`就是 m；
#: 而 `方`(口语 m³，也可能是 m²)、`项`(总包计数) 这类有歧义的一律不收 ——
#: 收进来就等于替用户猜量纲，正是第 37 轮修掉的"拿面积除以体积"那类 bug 的入口。
UNIT_ALIASES = {
    "m3": "m³", "M3": "m³", "m^3": "m³", "立方米": "m³", "㎥": "m³",
    "m2": "m²", "M2": "m²", "m^2": "m²", "平方米": "m²", "㎡": "m²",
    "M": "m", "米": "m", "延米": "m", "延长米": "m", "㎜": "mm", "㎝": "cm",
    "㎞": "km",
    "T": "t", "吨": "t", "KG": "kg", "㎏": "kg",
    "台班": "台班", "工日": "工日", "人日": "工日", "工时": "工时",
    "个小时": "小时", "h": "小时",
    "\\u53f0\\u73ed": "台班",  # KB json 里残留的转义写法
}

#: 大小写兜底：`M2` / `M³` / `M^3` 等大写写法先整串查一次，查不到再转小写查一次。
#: 只对 `UNIT_ALIASES` 里**已存在的小写键**生效 —— 中文键没有大小写，不会受影响；
#: 未知写法仍然原样返回（`normalize_unit("大") == "大"` 的契约不变）。
_LOWER_ALIASES = {k.lower(): v for k, v in UNIT_ALIASES.items() if k.isascii()}

#: 量纲族。计数类**不进同一个族**（根 ≠ 块），避免把"根"换成"块"。
_FAMILY_MAP = {
    "m³": "volume", "m²": "area", "m": "length", "t": "mass", "kg": "mass",
    "工日": "labor_day", "台班": "shift", "小时": "hour", "工时": "hour",
}

#: 同族内的换算系数（值 = 1 个 key 等于多少 base）。base：length=m、mass=kg、volume=m³。
_SAME_FAMILY = {
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "kg": ("mass", 1.0), "t": ("mass", 1000.0),
    "m³": ("volume", 1.0), "L": ("volume", 0.001),
    "m²": ("area", 1.0),
}


def normalize_unit(unit):
    """把单位写法归一到规范写法。空值返回 ""；未知写法原样返回（不猜）。

    查表两级：整串 → 小写整串。两级都不中就是原样返回。**不**做去标点、去"约"、
    去括号这类模糊匹配 —— "大"、"" 之类必须原样返回，让下游判 unusable 而不是猜。
    """
    if unit is None:
        return ""
    s = str(unit).strip().replace(" ", "").replace("／", "/")
    if not s:
        return ""
    if s in UNIT_ALIASES:
        return UNIT_ALIASES[s]
    return _LOWER_ALIASES.get(s.lower(), s)


def unit_family(unit):
    """量纲族：volume / area / length / mass / labor_day / shift / hour / count:<单位> / unknown。

    计数类返回 `count:根` 这种带单位的族名，于是 `convert()` 不会把"根"当"块"。
    """
    u = normalize_unit(unit)
    if not u:
        return "unknown"
    if u in _FAMILY_MAP:
        return _FAMILY_MAP[u]
    if u in _SAME_FAMILY:
        return _SAME_FAMILY[u][0]
    if u in ("", "项", "套", "副", "组", "处"):
        return "count:" + u
    # 其余一律当"计数类"，族名带上单位本身
    return "count:" + u


def parse_norm_unit(norm_unit):
    """拆解定额单位串 → {"labor_unit", "denominator", "scale", "raw"}。

    - `"工日/m³"`     → labor_unit=工日, denominator=m³,  scale=1.0
    - `"台班/根"`     → labor_unit=台班, denominator=根,  scale=1.0
    - `"工日/10m²"`   → labor_unit=工日, denominator=m²,  scale=10.0（**原始单位串**）
    - `"台班"`        → labor_unit=台班, denominator="",  scale=1.0（**分母缺失**）

    `scale` 只用于**识别**"这是不是原始未归一的单位串"：KB 的 `labor_norm_unit`
    应当是 scale=1；出现 10/100/1000 说明传进来的是 `raw_unit`，调用方必须报错。
    """
    raw = "" if norm_unit is None else str(norm_unit).strip()
    out = {"labor_unit": "", "denominator": "", "scale": 1.0, "raw": raw}
    if not raw:
        return out
    text = raw.replace(" ", "").replace("／", "/")
    parts = text.split("/")
    out["labor_unit"] = normalize_unit(parts[0])
    if len(parts) > 1:
        den = "/".join(parts[1:])
        digits = ""
        while den and (den[0].isdigit() or den[0] == "."):
            digits += den[0]
            den = den[1:]
        if digits:
            try:
                out["scale"] = float(digits)
            except ValueError:
                pass
        out["denominator"] = normalize_unit(den)
    return out


def denominator_of(norm_unit):
    """定额单位串的分母（规范写法）；取不到返回 ""。"""
    return parse_norm_unit(norm_unit)["denominator"]


# ---------------------------------------------------------------------------
# 1b. 计量对象 `measure_scope`（契约 §1/§2）：同量纲 ≠ 同口径
# ---------------------------------------------------------------------------
#: 受控词表（契约 §1）。**只有这些字符串算"有值"**，其余一律当"未填"。
#: 为什么必须受控：`m²` 只是量纲，`建筑面积` / `风管展开面积` / `天棚面积` 是三个
#: 数量级完全不同的分母。定额只写 `m²` 时，`check_unit_pair` 会判 `same` 并放行 ——
#: "风管展开面积 0.35 工日/m²" 被乘上 "建筑面积 14200 m²" 的 5 倍误差就是这样来的。
MEASURE_SCOPES = frozenset((
    "建筑面积", "外墙面积", "内墙抹灰面积", "天棚面积", "楼地面面积", "模板接触面积",
    "风管展开面积", "保温面积", "防水面积", "管道长度", "电缆长度", "体积", "质量",
    "桩根数", "件数", "台数", "自然单位", "项",
))

#: 口径比较的三种结论（约定字符串，供 binding 与测试共用）。
SCOPE_SAME = "same"            # 两边都有值且相等
SCOPE_MISMATCH = "mismatch"    # 两边都有值且不同 → **口径不一致**
SCOPE_UNCONFIRMED = "unconfirmed"   # 任一方为 ''（未知）→ 口径未确认


def normalize_measure_scope(scope):
    """归一 `measure_scope`：空/None → `''`；词表外的值原样返回（不猜、不丢）。

    刻意**不**把词表外的写法删除或归到 `''`：WS6 迁移时若写了新词，
    丢掉它就等于把"两个人明知口径不同"退化成"未知"，反而放行。原样返回时
    两个不同的新词仍然判 mismatch，语义是单调安全的。
    """
    if scope is None:
        return ""
    return str(scope).strip()


def measure_scope_state(task_scope, norm_scope):
    """`task_scope` 与 `norm_scope` 的口径关系（契约 §2 第 3 条）。

    - 两边都有值且不同 → `"mismatch"`（**口径不一致**）
    - 任一方为 `''`      → `"unconfirmed"`（口径未确认：不是一致，也不是不一致）
    - 两边都有值且相同   → `"same"`

    ⚠️ **禁止**用 `unit_family()` 相等来替代本函数：同量纲不同对象正是本函数要拦的。
    """
    a, b = normalize_measure_scope(task_scope), normalize_measure_scope(norm_scope)
    if not a or not b:
        return SCOPE_UNCONFIRMED
    return SCOPE_SAME if a == b else SCOPE_MISMATCH


def measure_scope_conflict(task_scope, norm_scope):
    """口径是否**确定不一致**（`mismatch`）→ bool。口径未知（`''`）返回 False。

    调用方拿到 True 时按契约 §2 处理：能换算就以定额口径为准，不能就置
    `usable=False` + `not_usable_reason="口径无法对齐"`，**禁止 1:1 硬乘**。
    """
    return measure_scope_state(task_scope, norm_scope) == SCOPE_MISMATCH


# ---------------------------------------------------------------------------
# 1c. §13 禁用清单：不可计量的"包装单位"分母（**只打标，不阻断**）
# ---------------------------------------------------------------------------
#: 不可计量的包装/计数单位（归一后比对）。分母是这些单位时，"工期 = 定额值 ÷ 班组"
#: 算出来的数**没有物理含义**：任务工程量常常就是 `1`，于是定额值本身变成了工期。
#: 实测确证实例（`AI_ESTIMATE_V1`）：`DACC_AI_001~006`、`FACC_AI_001~004`、
#: `HACC_AI_001~003`、`SACC_AI_001~005`、`MPREP/TUTIL`（项/批/组）、`FIRE_AI_005`。
#:
#: ⚠️ **这些单位本身是合法的，不是"禁用这些单位"**。只有在"当作产能分母 **且** 工程量
#: 不足以支撑它（缺省 / 为 1 / 口径未确认）"时才打标。
#: 父代理 2026-09-20 裁决：**只许 labeling-only**（写 `denominator_meaningless=True` +
#: `basis_unconfirmed=True`），**禁止**用 `usable=False` 硬阻断 —— 硬阻断会静默丢掉
#: 定额与人工、工期退回模型拍数，正是用户最反对的"静默丢数据"。政策是"不得不用 AI 就用，
#: 最后标出来"。
UNMEASURABLE_DENOMINATORS = frozenset((
    "项", "批", "组", "点", "套", "座", "张", "卷", "捆", "只", "箱",
))


def is_unmeasurable_denominator(unit):
    """该单位（或其定额分母）是不是不可计量的包装单位 → bool。

    `unit` 可以是完整定额单位串（`"工日/项"`），会先取分母再比对（`normalize_unit` 归一）。
    """
    u = normalize_unit(unit)
    if not u:
        return False
    den = denominator_of(u) or u
    return den in UNMEASURABLE_DENOMINATORS


def denominator_is_meaningless(norm_unit, quantity):
    """§13 判据：分母不可计量 **且** 工程量不足以支撑它 → True（调用方只**打标**）。

    "工程量不足以支撑"= 取不到 / 非正 / 恰好 1（`1 项` 这种量本身就不构成产能分母）。

    **调用方不得据此置 `usable=False`**（父代理裁决），只写：
      `binding["denominator_meaningless"] = True`
      `binding["basis_unconfirmed"] = True`
    """
    if not is_unmeasurable_denominator(norm_unit):
        return False
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return True                 # 取不到量 → 不足以支撑
    return q <= 1.0


# ---------------------------------------------------------------------------
# 2. 产能：唯一真值 = 1 / norm_value
# ---------------------------------------------------------------------------

def productivity_of(norm_value, raw_quantity_basis=None):
    """产能（单位 / 工日 或 单位 / 台班）= 1 / norm_value。

    ⚠️ `raw_quantity_basis` 只用于**交叉校验**（`raw_value / basis == norm_value`），
    **绝不参与乘法**。历史上 `devtools/fix_norm_basis.py` 按
    `productivity = basis / norm` 写库，把 1030 行的产能放大了 basis 倍（最大 1000 倍），
    是"4260 m³ 土方 = 118 天"的成因之一。
    """
    try:
        nv = float(norm_value)
    except (TypeError, ValueError):
        return None
    if nv <= 0:
        return None
    return 1.0 / nv


# ---------------------------------------------------------------------------
# 3. 跨族换算（需要工程参数；拿不到参数就判"不可用"）
# ---------------------------------------------------------------------------

#: 换算需要哪些上下文参数（键名 → 含义），供调用方/测试参考。
CONTEXT_KEYS = {
    "pile_length_m": "单根桩长（米/根），用于 根 ↔ m",
    "volume_per_pile_m3": "单根体积（m³/根），用于 根 ↔ m³",
    "density_t_per_m3": "容重（吨/m³），用于 t ↔ m³",
    "thickness_m": "厚度（米），用于 m² ↔ m³",
    "unit_weight_kg_per_piece": "单件重量（kg/件），用于 件 ↔ kg",
}

#: 族对 → 换算所需的 ctx 键。**唯一目的**：在判 unusable 时告诉用户"补哪个参数就好了"。
#: 这张表必须与 `_cross_factor()` 里实际读的键一一对应 —— 两边分叉就会出现
#: "提示补 A、实际读 B"的假下一步。
_CTX_KEY_BY_PAIR = {
    ("count:根", "length"): "pile_length_m",
    ("length", "count:根"): "pile_length_m",
    ("count:根", "volume"): "volume_per_pile_m3",
    ("volume", "count:根"): "volume_per_pile_m3",
    ("mass", "volume"): "density_t_per_m3",
    ("volume", "mass"): "density_t_per_m3",
    ("area", "volume"): "thickness_m",
    ("volume", "area"): "thickness_m",
    ("count:件", "mass"): "unit_weight_kg_per_piece",
    ("mass", "count:件"): "unit_weight_kg_per_piece",
}


def needed_context_keys(from_unit, to_unit):
    """把 `from_unit` 换成 `to_unit` 需要哪些上下文参数（ctx 键名列表；无依据时为 []）。"""
    fu, tu = normalize_unit(from_unit), normalize_unit(to_unit)
    key = _CTX_KEY_BY_PAIR.get((unit_family(fu), unit_family(tu)))
    return [key] if key else []


def next_step_hint(from_unit, to_unit, ctx=None):
    """单位换算不出来时给**可执行下一步**（写进 `not_usable_reason`，供用户直接照做）。

    两种形态：
      - 有换算依据但参数没给  → 点名缺哪个参数（含含义），补齐即可自动换算；
      - 根本没有换算依据      → 明说"改绑同口径定额 / 先换算工程量"，不要许诺补参数。
    """
    fu, tu = normalize_unit(from_unit), normalize_unit(to_unit)
    keys = [k for k in needed_context_keys(fu, tu) if not (ctx or {}).get(k)]
    if keys:
        desc = "；".join("%s（%s）" % (k, CONTEXT_KEYS.get(k, "")) for k in keys)
        return "缺换算参数 %s，补齐后该定额即可自动换算" % desc
    return ("「%s」与「%s」之间没有量纲换算依据 —— 请改绑分母为「%s」的定额，"
            "或先把工程量换算成「%s」" % (fu, tu, fu, tu))


def _cross_factor(from_u, to_u, ctx):
    """跨族换算系数：`to = from * factor`；不可换算返回 None。

    ⚠️ 一律需要**工程参数**：缺参数返回 None（调用方据此判 unusable），
    绝不退化成 1:1 —— "120 根当 120 m 用"就是 1:1 兜底造出来的。
    """
    ctx = ctx or {}
    f_fam, t_fam = unit_family(from_u), unit_family(to_u)
    pair = (f_fam, t_fam)
    if pair == ("count:根", "length"):
        v = ctx.get("pile_length_m")
        return float(v) if v else None
    if pair == ("length", "count:根"):
        v = ctx.get("pile_length_m")
        return (1.0 / float(v)) if v else None
    if pair == ("count:根", "volume"):
        v = ctx.get("volume_per_pile_m3")
        return float(v) if v else None
    if pair == ("volume", "count:根"):
        v = ctx.get("volume_per_pile_m3")
        return (1.0 / float(v)) if v else None
    if pair == ("mass", "volume"):
        d = ctx.get("density_t_per_m3")
        return (1.0 / float(d)) if d else None      # t → m³ = t / 容重
    if pair == ("volume", "mass"):
        d = ctx.get("density_t_per_m3")
        return float(d) if d else None
    if pair == ("area", "volume"):
        v = ctx.get("thickness_m")
        return float(v) if v else None
    if pair == ("volume", "area"):
        v = ctx.get("thickness_m")
        return (1.0 / float(v)) if v else None
    if pair == ("count:件", "mass"):
        w = ctx.get("unit_weight_kg_per_piece")
        return (float(w) / 1000.0) if w else None       # 件 → t
    if pair == ("mass", "count:件"):
        w = ctx.get("unit_weight_kg_per_piece")
        return (1000.0 / float(w)) if w else None       # t → 件
    return None


def convert(qty, from_unit, to_unit, ctx=None):
    """把工程量从 `from_unit` 换到 `to_unit`。

    返回 `(换算后的量, 说明)`；不可换算返回 `None`（调用方必须据此判"不可用"，
    **不得**按 1:1 处理）。
    """
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None
    fu, tu = normalize_unit(from_unit), normalize_unit(to_unit)
    if not fu or not tu:
        return None
    if fu == tu:
        return (q, "同单位")
    ef, tf = unit_family(fu), unit_family(tu)
    if ef == tf and fu in _SAME_FAMILY and tu in _SAME_FAMILY:
        scale = _SAME_FAMILY[fu][1] / _SAME_FAMILY[tu][1]
        return (q * scale, "同族换算 %s→%s ×%g" % (fu, tu, scale))
    factor = _cross_factor(fu, tu, ctx)
    if factor is None:
        return None
    return (q * factor, "跨族换算 %s→%s ×%g（来自工程参数）" % (fu, tu, factor))


# ---------------------------------------------------------------------------
# 3b. 缺参数时的**具名取值**（D5）：不再有全局默认值
# ---------------------------------------------------------------------------
#: ⚠️ D5（2026-09-21）裁定：**禁止全局默认参数值 / 写死常量 / 为参数建表**；
#: 换算一律"以数据库（定额分母）口径为准"，参数只能来自①用户明写 ②定额行条件。
#: 因此本模块**不再用它做兜底**：这个常量只作为**兼容保留**（`__all__` 导出，
#: 老的调用方/测试还能 import 到同一个数），并且它就是定额条件里那一档的档位值
#: （200mm），不再是"缺项目墙厚就取它"的默认假设。
DEFAULT_WALL_THICKNESS_M = 0.2

DEFAULT_WALL_THICKNESS_NOTE = (
    "%g m（%dmm）是**定额条件里的厚度档位值**，不是项目实测墙厚、也不是本项目假设："
    "按 D5 只能从定额行的适用条件解析（见 kb_units.assumed_context）；"
    "项目侧的砌块墙口径仍以 beat_configs.BLOCK_WALL_THICKNESS = %g m 为准"
    % (DEFAULT_WALL_THICKNESS_M, int(round(DEFAULT_WALL_THICKNESS_M * 1000)),
       DEFAULT_WALL_THICKNESS_M))

#: 定额条件里的"厚度分层"写法：`>200mm` / `≤200mm` / `墙体厚度≤150mm` / `厚0.2m`。
#: `is_thickness_tiered()` 用它判"这条定额是不是按厚度分层的墙体定额"；
#: `assumed_context()` 用它把**档位值**取出来（只对"上限型"档位取值，见下）。
_THICKNESS_TIER_RE = re.compile(
    r"(?:厚度|板厚|壁厚|墙厚|δ)\s*[≤≥＜＞<>=]?\s*\d+(?:\.\d+)?"
    r"|[≤≥＜＞<>=]\s*\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|m|米)",
    re.I)

#: 取厚度档位值的正则：带比较符的形式（`≤200mm` / `>200mm` / `≥0.2m` / `厚度≤150mm`）。
_TIER_VALUE_RE = re.compile(
    r"(?:厚度|板厚|壁厚|墙厚|δ)?\s*([≤≥＜＞<>=]+)\s*(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)",
    re.I)
_TIER_SCALE = {"mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01, "m": 1.0, "米": 1.0}
#: "上限型"档位符：`≤` / `<` / `=` 能给出**落在档位内**的厚度值。
#: `>` / `≥` 是**开区间**，档位里没有任何确定值可取 → 不取值（D5：推定不出就报缺）。
_TIER_UPPER_OPS = ("≤", "＜", "<", "=", "＝", "==")


def is_thickness_tiered(text):
    """这段定额条件/名称是否属于"按厚度分层的墙体定额"。

    只为**允许哪一类具名取值**把关：只有这类定额才谈得上"缺的只是项目墙厚"。
    条件里出现尺寸数字的定额很多（钢筋「直径≤16mm」…），但那些组合不会是
    面积↔体积，所以这里放宽不会造成误换算（见 `assumed_context()`）。
    """
    return bool(_THICKNESS_TIER_RE.search(str(text or "")))


def _thickness_from_condition(condition_text):
    """从定额行的适用条件里取**厚度档位值**（米）；取不到 → `None`。

    只对"上限型"档位（`≤200mm` / `=200mm`）取值：档位本身就是"不超过 200mm"，
    取 0.2 m 是**在档位内**的确定值，也是这条定额自己的口径（D5：往数据库口径靠）。
    开区间档位（`>200mm` / `≥0.2m`）**不取值** —— 档位里没有可取的确定值，
    硬取边界就是编造；由调用方按"推定不出就报缺"处理。
    """
    m = _TIER_VALUE_RE.search(str(condition_text or ""))
    if not m:
        return None
    op = m.group(1)
    if op not in _TIER_UPPER_OPS:
        return None
    value = float(m.group(2)) * _TIER_SCALE.get(m.group(3), 1.0)
    return value if value > 0 else None


def assumed_context(from_unit, to_unit, condition_text=""):
    """缺换算参数时的**具名取值**：返回 `(ctx, source, note)`；无可用取值 → `({}, '', '')`。

    与 `check_unit_pair()` 的分工：这里**不判**能不能换算，只在"确实缺参数、且这条
    定额本身就是按厚度分层的墙体定额"时，把**该定额档位里的厚度值**取出来。
    调用方必须把 `source` / `note` 一起留痕（写进 binding 的 `ctx_source` /
    `coverage_reason` / `provenance.note`），否则就等于静默编数。

    D5（2026-09-21）两条裁定：
      · **没有全局默认值了**：档位是上限型（`≤200mm` / `=200mm`）→ 取档位值
        （0.2 m）；档位是开区间（`>200mm`）或条件里没有档位 → `({}, '', '')`，
        调用方按 `unusable` + `not_usable_reason` 处理（推定不出就报缺）。
      · **来源不许谎报**（裁定-1）：这个值来自**定额行适用条件**，不是 AI 猜的，
        所以 `source` 返回 **`'norm_condition'`**（绝不能标 `ai_estimate` ——
        假的溯源与漏标一样违反"逐行可溯源"契约）。将来若真出现"AI 猜参数"
        的路径，那条才用 `ai_estimate`。

    其余组合一律返回空 → 调用方继续判 unusable（例如「根 ↔ m³」要单根体积，
    只能由用户给，不许拿桩径瞎估）。
    """
    if not is_thickness_tiered(condition_text):
        return {}, "", ""
    # `to_unit` 可能是完整定额单位串（"工日/m³"），也可能只是分母（"m³"）——
    # `needed_context_keys()` 只认分母，所以先取分母（取不到才当它本身就是分母）。
    den = denominator_of(to_unit) or to_unit
    if needed_context_keys(from_unit, den) != ["thickness_m"]:
        return {}, "", ""
    thickness = _thickness_from_condition(condition_text)
    if thickness is None:
        # 开区间档位 / 解析不出 → 没有确定值可给（D5：不许退默认值硬算）
        return {}, "", ""
    note = ("厚度取 %g m（%dmm）：**来自定额行适用条件的厚度档位**"
            "（%s），不是项目实测墙厚、不是全局默认假设、**也不是 AI 估算**"
            % (thickness, int(round(thickness * 1000)),
               str(condition_text or "").strip()))
    return ({"thickness_m": thickness}, "norm_condition", note)


# ---------------------------------------------------------------------------
# 4. 校验：双向 + 默认拒绝
# ---------------------------------------------------------------------------

def check_unit_pair(leaf_unit, norm_unit, ctx=None):
    """叶子工程量单位 与 定额单位串 是否匹配。

    返回 `{"verdict": "same"|"convertible"|"unusable", "factor", "denominator",
    "detail", "scale"}`：

    - `same`        ：分母与叶子单位一致（规范写法后）
    - `convertible`  ：可经工程参数换算（`factor` 给出乘数，`convert()` 复核）
    - `unusable`    ：分母缺失 / 不可换算 / 任一单位为空 —— **必须拒绝使用该定额**

    与旧实现（`scheduler.units_compatible`）的三处区别：
      1. 双向：不再"只看叶子单位在不在候选里"，而是拿定额分母去比叶子单位；
      2. 默认拒绝：旧实现"解析不出来返回 True（宁可不拦）"，这里返回 unusable；
      3. 分母必须来自定额：`"台班"`（无分母）直接判不可用，杜绝用叶子单位补分母。
    """
    parsed = parse_norm_unit(norm_unit)
    den = parsed["denominator"]
    lu = normalize_unit(leaf_unit)
    res = {"verdict": "unusable", "factor": None, "denominator": den,
           "detail": "", "scale": parsed["scale"]}

    if not lu:
        res["detail"] = "叶子缺工程量单位，无法核对定额单位"
        return res
    if not den:
        res["detail"] = ("定额单位「%s」缺分母（只有 %s），不得用叶子单位「%s」补 —— "
                         "请补 KB 的 quantity_unit"
                         % (parsed["raw"], parsed["labor_unit"] or "?", lu))
        return res
    if parsed["scale"] != 1.0:
        res["detail"] = ("传入的是**原始未归一**单位串「%s」（倍率 %g）：定额值应已归一，"
                         "请改传 labor_norm_unit" % (parsed["raw"], parsed["scale"]))
        return res

    if den == lu:
        res["verdict"] = "same"
        res["factor"] = 1.0
        res["detail"] = "单位一致（%s）" % lu
        return res

    # factor 的定义（消费方唯一需要记的一条）：
    #     换算到定额分母单位的量 = 叶子单位的量 × factor
    # 例：叶子「根」、定额分母「m」、桩长 18 m/根 → factor = 18（120 根 → 2160 m）。
    conv = convert(1.0, lu, den, ctx)
    if conv is None:
        # `不可换算` 这个子串是既有测试（test_kb_units.test_same_unit_and_mismatch）
        # 与缺口报告的锚点，**必须保留**；后面追加的才是本轮新增的"可执行下一步"。
        # 只有"绑到了真实定额、但量纲对不上"才走到这里（分母缺失/未归一在前面已返回），
        # 所以这句话的受众是"能改配置的人"，不是最终用户 —— 要能照做。
        res["detail"] = ("单位不一致且不可换算：任务「%s」（%s） vs 定额分母「%s」（%s）；%s"
                         % (lu, unit_family(lu), den, unit_family(den),
                            next_step_hint(lu, den, ctx)))
        return res
    res["verdict"] = "convertible"
    res["factor"] = conv[0]
    res["detail"] = "需换算：1 %s = %g %s（%s）" % (lu, conv[0], den, conv[1])
    return res


__all__ = [
    "UNIT_ALIASES", "CONTEXT_KEYS", "normalize_unit", "unit_family",
    "parse_norm_unit", "denominator_of", "productivity_of", "convert",
    "check_unit_pair", "needed_context_keys", "next_step_hint",
    "DEFAULT_WALL_THICKNESS_M", "DEFAULT_WALL_THICKNESS_NOTE",
    "is_thickness_tiered", "assumed_context",
    # 计量对象（契约 §1/§2）
    "MEASURE_SCOPES", "SCOPE_SAME", "SCOPE_MISMATCH", "SCOPE_UNCONFIRMED",
    "normalize_measure_scope", "measure_scope_state", "measure_scope_conflict",
    # §13 不可计量的包装单位分母（只打标）
    "UNMEASURABLE_DENOMINATORS", "is_unmeasurable_denominator",
    "denominator_is_meaningless",
]
