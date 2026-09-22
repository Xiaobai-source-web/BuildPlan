# -*- coding: utf-8 -*-
"""四个闭合校验（纯逻辑 / 数据注入，不读数据库、不 import kb.py）。

来源与依据
----------
* `docs/修改项总清单_20260921.md` §六 验收要求第 2 条：
  「**四个闭合校验必须全绿**：占比 V1–V4、MWI 五类完整性、映射表三档无空档、条件无缺维」。
* `docs/修改项总清单_20260921.md:59` B3 构件占比表四条校验：
  V1 守恒 ∑=100%、V2 一致 占比>0 ⇒ 结构映射不能是 EXCLUDED、
  V3 完整 结构映射 REQUIRED ⇒ 占比>0、V4 落地 占比>0 ⇒ 最终 WBS 必须有落点。
* **B3 分组口径（用户 2026-09-21 裁定，父代理规格分叉请示）**：占比表按
  **「结构类型 × 工种(L3)」** 分组，**每组各自 ∑ = 100%**（不是对每个
  `structure_type_id` 求和）。工种取自 `L4_Activity_Dictionary.work_type_id`，
  由调用方通过 `check_ratio_v1_v4(l4_to_l3=…)` / `run_all_checks(l4_to_l3=…)`
  注入（与 `check_mapping_tiers` 的 `l4_to_l3` 同参数名、同风格）。
  缺映射的行归入哨兵组 `WORK_TYPE_UNKNOWN` 并报 `code="work_type_unknown"`
  （severity=warning），**不静默跳过**。
* `docs/修改项总清单_20260921.md:68` C1：`Resource_Workface_Index` 67 行，
  `capacity_mode` 五类 area 27 / position 21 / auxiliary 11 / transport 5 / site 3。
* `docs/资源工位密度MWI_送审表.md:38` 单位：**m²/人**（人工）、**m²/台**（机械）。
* `devtools/kb_migrate_phase1_mwi.py:150` 人工行写死 `"mwi_unit": "m2/人"`；
  `kb_migrate_phase1_mwi.py:157` 机械仅 `capacity_mode == "area"` 时写 `"m2/台"`，否则 `None`。
* `backend/pipeline/nodes/norm_bind.py:511-522` `leaf["condition_key"]` 是
  `{维度: 值}` 字典；`Norm_Labor_Table.condition_combination` 是其来源（同构字典）。

单位约定
--------
面积一律 `m²`。**严禁**出现 U+33A1 的方块平米符号。
本模块自带 `normalize_area_unit()` 把小写/ASCII 写法（`m2/人`）与方块写法统一成 `m²/人` 后再比较。

返回结构
--------
所有函数**不抛异常、不 print**，只返回结构化结果：

* `Violation` = `{"group", "code", "severity", "message", "keys", "detail"}`
  * `group`    : `"ratio"` | `"mwi"` | `"mapping"` | `"condition"`
  * `code`     : 稳定机器码，见各函数 docstring
  * `severity` : `"error"` 硬违规（计入 `all_green`）/ `"warning"` 提示
  * `keys`     : 排序键，永远是 `list[str]`，用于确定性排序
  * `detail`   : `dict`，结构化上下文
* `CheckResult` = `{"check", "all_green", "violations", "stats"}`

顶层入口 `run_all_checks()` 汇总四组。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "Violation",
    "CheckResult",
    "RATIO_TOLERANCE",
    "WORK_TYPE_UNKNOWN",
    "L4_MAPPING_LEVELS",
    "LEGACY_MAPPING_LEVELS",
    "MWI_CAPACITY_MODES",
    "MWI_MOBILITIES",
    "LEAF_CONDITION_META_KEYS",
    "normalize_area_unit",
    "check_ratio_v1_v4",
    "check_mwi_completeness",
    "check_mapping_tiers",
    "check_condition_dimensions",
    "run_all_checks",
]


# ======================================================================
# 常量
# ======================================================================

#: V1 守恒的默认浮点容差（百分点）
RATIO_TOLERANCE: float = 0.01

#: L4 → 工种(L3) 查不到时的哨兵工种名。
#: 该组仍参与「组内 ∑ = 100」的守恒判定，但会另报 `work_type_unknown`（warning）。
WORK_TYPE_UNKNOWN: str = "<unknown>"

#: **路线 2**（用户 2026-09-21 裁定）：占比表**只回答「部位/构件」**，不回答
#: 「材料/做法/体系」与「工序」。因此一个 `(结构类型 × 工种)` 组里的 L4 分两类：
#: 「切分类」（部位/构件）拿占比、组内 ∑ = 100；「不参与类」（互斥做法备选、
#: 工序/配套条目、按需出现的附属工程）**不给占比行**。
#:
#: 调用方用 ``exempt_activity_ids`` / ``exempt_pairs`` 显式声明"不参与"：
#: * 不参与 ⇒ **不受 V3**（映射必备 ⇒ 占比 > 0）约束（区分"表里没有"与"明确不参与"）；
#: * 反过来，已声明不参与的条目**不允许再拿占比**（``V5_ratio_on_exempt`` 守卫）。
#: 两个入参 keyword-only、默认 None，不传即完全保持既有语义。
EXEMPT_CODE: str = "V5_ratio_on_exempt"

#: 映射档位：三档正式枚举。`OPTIONAL` 与 `EXCLUDED` 之间即"可选"。
L4_MAPPING_LEVELS: Tuple[str, ...] = ("REQUIRED", "OPTIONAL", "EXCLUDED")

#: 遗留档位：`USUAL` 是"可选"的旧枚举名（`修改项总清单_20260921.md:142` 决定
#: 保留 `USUAL` 枚举值、界面显示「可选」）。**不算硬违规**，单列 legacy 提示。
LEGACY_MAPPING_LEVELS: Tuple[str, ...] = ("USUAL",)

#: `Resource_Workface_Index.capacity_mode` 五类
MWI_CAPACITY_MODES: Tuple[str, ...] = ("area", "position", "auxiliary", "transport", "site")

#: `Resource_Workface_Index.resource_mobility` 取值
MWI_MOBILITIES: Tuple[str, ...] = ("fixed", "mobile", "site")

#: 元数据条件维度：进 `condition_key` 但不属于"条件字典"里的工艺维度，
#: 校验维度存在性时必须跳过（与 `norm_bind.py:514` 的 `_META_CONDITION_KEYS` 同口径）。
LEAF_CONDITION_META_KEYS: Tuple[str, ...] = ("_source", "构件做法")

#: 面积单位 ↔ 资源类别的期望口径（`资源工位密度MWI_送审表.md:38`）
_UNIT_BY_KIND: Dict[str, str] = {"labor": "m²/人", "machine": "m²/台"}

_UNIT_ALIASES: Dict[str, str] = {
    "m²/人": "m²/人", "m2/人": "m²/人", "m^2/人": "m²/人",
    "m²/台": "m²/台", "m2/台": "m²/台", "m^2/台": "m²/台",
    # U+33A1（方块平米符号，项目正在清除）的历史写法也要能认出来：
    # 用 chr() 构造，源码里不出现该字符本身，免得被"彻底清除"的 grep 误伤。
    (chr(0x33A1) + "/人"): "m²/人",
    (chr(0x33A1) + "/台"): "m²/台",
}

#: 严重度
_ERROR = "error"
_WARNING = "warning"


Violation = Dict[str, Any]
CheckResult = Dict[str, Any]


# ======================================================================
# 内部小工具
# ======================================================================


def normalize_area_unit(unit: Any) -> Optional[str]:
    """把面积单位归一成 `m²/人` / `m²/台`；不认识或空值返回 None。

    >>> normalize_area_unit("m2/人")
    'm²/人'
    """
    if unit is None:
        return None
    text = str(unit).strip()
    if not text:
        return None
    return _UNIT_ALIASES.get(text, text)


def _s(value: Any) -> str:
    """稳定的字符串化（None → ""），用于排序键与分组。"""
    return "" if value is None else str(value)


def _v(group: str, code: str, severity: str, message: str,
       keys: Sequence[Any], detail: Optional[Mapping[str, Any]] = None) -> Violation:
    return {
        "group": group,
        "code": code,
        "severity": severity,
        "message": message,
        "keys": [_s(k) for k in keys],
        "detail": dict(detail or {}),
    }


def _sort_violations(items: List[Violation]) -> List[Violation]:
    """确定性排序：按 (group, code, keys, message) 稳定字典序。"""
    return sorted(items, key=lambda r: (r["group"], r["code"], r["keys"], r["message"]))


def _result(check: str, violations: List[Violation],
            stats: Mapping[str, Any]) -> CheckResult:
    ordered = _sort_violations(violations)
    return {
        "check": check,
        "all_green": not any(r["severity"] == _ERROR for r in ordered),
        "violations": ordered,
        "stats": dict(stats),
    }


def _total(rows: Iterable[Mapping[str, Any]], key: str) -> float:
    """对某列求和；非数值行按 0 计（由别的校验报"非数值"违规）。"""
    acc = 0.0
    for r in rows:
        try:
            acc += float((r or {}).get(key))
        except (TypeError, ValueError):
            continue
    return acc


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return False


def _num(value: Any) -> Optional[float]:
    return float(value) if _is_number(value) else None


def _get(row: Any, key: str, default: Any = None) -> Any:
    """同时支持 Mapping 与属性对象（dataclass）。"""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _resolve_work_type(activity_id: str,
                       l4_to_l3: Optional[Mapping[str, Any]]) -> str:
    """查 ``L4 → 工种(L3)``；未注入映射或查不到 → ``WORK_TYPE_UNKNOWN``。

    >>> _resolve_work_type("A1", {"A1": "concrete"})
    'concrete'
    >>> _resolve_work_type("A2", {"A1": "concrete"}) == WORK_TYPE_UNKNOWN
    True
    """
    if isinstance(l4_to_l3, Mapping):
        text = _s(l4_to_l3.get(activity_id)).strip()
        if text:
            return text
    return WORK_TYPE_UNKNOWN


def _exempt_pairs(exempt_activity_ids: Optional[Iterable[Any]],
                  exempt_pairs: Optional[Iterable[Any]],
                  structure_ids: Iterable[str]) -> set:
    """把两种豁免入参展开成 ``{(structure_type_id, activity_id)}``（纯逻辑，不抛异常）。

    * ``exempt_activity_ids``：元素是 **L4 字符串**（对该 L4 在**所有**出现过的
      结构类型下都豁免）或 **``(结构类型, L4)`` 二元组/二元列表**（只豁免该结构）。
    * ``exempt_pairs``：只接受二元组/二元列表形式。

    非法元素（None、非 2 元、元素为 None）一律**忽略**，不报错也不抛异常。
    """
    sids = [str(s) for s in structure_ids if s is not None]
    out = set()

    def _take(item: Any, global_ok: bool) -> None:
        if item is None:
            return
        if isinstance(item, str):
            if global_ok and item.strip():
                for sid in sids:
                    out.add((sid, item))
            return
        if isinstance(item, (tuple, list)) and len(item) == 2:
            sid, aid = item
            if sid is not None and aid is not None:
                out.add((str(sid), str(aid)))

    for item in exempt_activity_ids or ():
        _take(item, True)
    for item in exempt_pairs or ():
        _take(item, False)
    return out


# ======================================================================
# 校验 1：构件占比 V1–V4
# ======================================================================


def check_ratio_v1_v4(
    ratio_rows: Iterable[Mapping[str, Any]],
    mapping_rows: Iterable[Mapping[str, Any]],
    wbs_landed: Optional[Iterable[Any]] = None,
    *,
    tolerance: float = RATIO_TOLERANCE,
    l4_to_l3: Optional[Mapping[str, Any]] = None,
    exempt_activity_ids: Optional[Iterable[Any]] = None,
    exempt_pairs: Optional[Iterable[Any]] = None,
) -> CheckResult:
    """B3 构件占比表四条闭合校验（纯逻辑）。

    分组口径（**用户 2026-09-21 裁定**）
    ---------------------------------
    V1 按 **「结构类型 × 工种(L3)」** 分组，**每个小组各自 ∑ = 100%**：
    工种由 `l4_to_l3`（`{activity_id: work_type_id}`，来自
    `L4_Activity_Dictionary.work_type_id`）解析。**不是**对每个
    `structure_type_id` 求和。同一结构类型下不同工种互不干扰。
    组内只有 1 个 L4 时，该 L4 必须是 100（V1 的自然推论）。
    `l4_to_l3` 未注入或查不到该 L4 → 归入哨兵工种 `WORK_TYPE_UNKNOWN`
    并报 `code="work_type_unknown"`（severity=warning），**不静默跳过**。

    路线 2：占比表只回答「部位」（**用户 2026-09-21 裁定**）
    ----------------------------------------------------
    占比表**只回答「部位/构件」**，不回答「材料/做法/体系」与「工序」。组内的 L4 因此
    分两类：

    * **切分类**（部位/构件，如基础/柱/梁/板/墙/楼梯/屋架）→ 拿占比，组内 ∑ = 100。
    * **不参与类** → **不给占比行**，由调用方在 ``exempt_activity_ids`` /
      ``exempt_pairs`` 里显式声明：
      1. **互斥做法/材料备选**（砖墙/石墙/空斗墙/ALC…、各种桩型、各种柱型）——
         由条件维择一，每个族只留一个**代表项**在切分类里；
      2. **工序/配套条目**（安装/拆除/勾缝/套丝/花饰块组砌、大门铁件、滑道…）——
         量派生自其主体，不另占占比；
      3. **按需出现的附属工程**（烟囱/水塔/检查井/护坡/排水沟…）——出现与否由条件维决定。

    豁免的作用有两条（**必须成对使用才有意义**）：
    * 不参与 ⇒ **不受 V3 约束**（区分"表里没有"与"明确不参与"）；
    * 不参与 ⇒ **不允许再拿占比**，否则 ``code=EXEMPT_CODE``（``V5_ratio_on_exempt``，error）。

    ``(结构类型 × 工种)`` 组整体**部位不可切分**时，整组不编、连占比行都不建；校验器只看
    数据，不感知"哪些组被判定为不可切分"，因此这类组既不报 V1 也不报 V3（调用方可从
    ``stats["groups_without_ratio"]`` 看到"没有任何占比行的组"的规模）。

    数据契约
    --------
    * ``ratio_rows``   每行 ``{"structure_type_id": str, "activity_id": str,
      "ratio_percent": float}``。``ratio_percent`` 单位是**百分点**（0~100）。
    * ``mapping_rows`` 每行 ``{"structure_type_id": str, "activity_id": str,
      "applicability_level": str}``；档位见 ``L4_MAPPING_LEVELS``。
    * ``wbs_landed``   可选，最终 WBS 里实际出现的 ``(structure_type_id,
      activity_id)`` 集合（元素是 2 元 tuple/list，或 ``"结构|L4"`` 字符串）。
      为 None → V4 标 ``skipped`` 并跳过。
    * ``l4_to_l3``     可选，``{activity_id: work_type_id}``；与
      `check_mapping_tiers` 的同名参数同一个字典。

    判据
    ----
    1. **V1 守恒**：对每个 ``(structure_type_id, work_type_id)`` 组，
       ∑ ``ratio_percent`` 必须 = 100（容差 ``tolerance``，默认 0.01），
       否则 ``code="V1_ratio_sum"``。
    2. **V2 一致**：``ratio_percent > 0`` ⇒ 该 (结构类型, L4) 在映射表里**不能是
       ``EXCLUDED``**，否则 ``code="V2_ratio_excluded"``。
    3. **V3 完整**：映射表 ``REQUIRED`` ⇒ 该 (结构类型, L4) 占比必须 > 0
       （缺失视同 0），否则 ``code="V3_required_zero"``（违规 detail 带所属组）。
       **已在豁免集合里的 REQUIRED 条目不算缺失**（明确不参与 ≠ 表里没有）。
    4. **V4 落地**：``ratio_percent > 0`` ⇒ 必须出现在 ``wbs_landed`` 里，
       否则 ``code="V4_not_landed"``；未提供 ``wbs_landed`` 时 ``code="V4_skipped"``
       （severity=warning）。
    5. **V5 豁免守卫**（路线 2 新增）：已声明「不参与」的 (结构类型, L4) **不允许**
       带 ``ratio_percent > 0``，否则 ``code="V5_ratio_on_exempt"``（error）——
       保证组内占比只落在「切分类」条目上，工序/做法不会被再次算进占比。

    诚实留白（**缺失 ≠ EXCLUDED**）
    ------------------------------
    映射表**没有**该行时**不算 V2 违规**（A3 明确保留 ``structure_mapping_absent``）。
    但占比 > 0 且映射缺失必须给出显式警告：
    ``code="mapping_absent"``（severity=warning）。

    返回
    ----
    ``CheckResult``，``stats`` 含 ``ratio_rows`` / ``structure_types`` /
    ``groups``（**已编组**数，即有占比行的组） / ``work_types`` /
    ``group_sums``（键 ``"结构|工种"``）/
    ``structure_type_sums``（仅参考，不再是判据） / ``single_l4_groups`` /
    ``exempt_inputs``（声明的豁免 pair 数） / ``exempt_entries``（豁免里真正出现在
    数据中的 pair 数） / ``groups_without_ratio``（**不可切分组 + 全 EXCLUDED 组**：
    有映射行但一行占比都没有的组） / ``work_type_unknown_pairs`` /
    ``v4_skipped`` / ``mapping_absent`` / ``tolerance``。
    """
    tol = abs(float(tolerance))
    ratios: List[Dict[str, Any]] = [_norm_ratio_row(r, i) for i, r in enumerate(ratio_rows or [])]
    mappings: List[Dict[str, Any]] = [
        _norm_mapping_row(r, i) for i, r in enumerate(mapping_rows or [])
    ]
    violations: List[Violation] = []

    # --- 映射表索引：档位与重复行 ---
    level_by_pair: Dict[Tuple[str, str], Any] = {}
    excluded_pairs = set()
    required_pairs = set()
    for m in mappings:
        pair = (m["structure_type_id"], m["activity_id"])
        level = m["applicability_level"]
        level_by_pair.setdefault(pair, level)
        lv = _s(level).strip().upper()
        if lv == "EXCLUDED":
            excluded_pairs.add(pair)
        elif lv == "REQUIRED":
            required_pairs.add(pair)

    # --- 路线 2：显式豁免（"不参与"条目）展开 ---
    all_structure_ids = sorted(
        {_s(r["structure_type_id"]) for r in ratios}
        | {_s(m["structure_type_id"]) for m in mappings}
    )
    exempt = _exempt_pairs(exempt_activity_ids, exempt_pairs, all_structure_ids)

    # --- 工种(L3) 解析：按 (结构类型, 工种) 分组 ---
    wt_cache: Dict[str, str] = {}

    def _wt(aid: str) -> str:
        if aid not in wt_cache:
            wt_cache[aid] = _resolve_work_type(aid, l4_to_l3)
        return wt_cache[aid]

    # --- V1 守恒（按 (结构类型 × 工种) 分组，各组独立 ∑ = 100）---
    sums: Dict[str, float] = {}
    single_l4_groups = 0
    by_group: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    by_structure: Dict[str, List[Dict[str, Any]]] = {}
    for r in ratios:
        by_group.setdefault(
            (r["structure_type_id"], _wt(r["activity_id"])), []).append(r)
        by_structure.setdefault(r["structure_type_id"], []).append(r)

    for sid, wt in sorted(by_group):
        rows = by_group[(sid, wt)]
        group_key = "%s|%s" % (sid, wt)
        if len(rows) == 1:
            single_l4_groups += 1
        total = 0.0
        for r in rows:
            value = _num(r["ratio_percent"])
            if value is None:
                violations.append(_v(
                    "ratio", "ratio_not_numeric", _ERROR,
                    "组 %s 的 L4 %s 占比不是数值：%r"
                    % (group_key, r["activity_id"], r["ratio_percent"]),
                    [sid, r["activity_id"], "ratio_percent"],
                    {"structure_type_id": sid, "work_type_id": wt,
                     "group_key": group_key,
                     "activity_id": r["activity_id"],
                     "raw": repr(r["ratio_percent"])},
                ))
                continue
            total += value
        sums[group_key] = total
        if abs(total - 100.0) > tol:
            violations.append(_v(
                "ratio", "V1_ratio_sum", _ERROR,
                "V1 守恒违规：组 %s 的占比合计 %.6f ≠ 100（容差 %g）"
                % (group_key, total, tol),
                [sid, wt, "ratio_percent"],
                {"structure_type_id": sid, "work_type_id": wt,
                 "group_key": group_key, "sum": total, "expected": 100.0,
                 "tolerance": tol, "delta": total - 100.0,
                 "l4_count": len(rows),
                 "activity_ids": sorted(r["activity_id"] for r in rows)},
            ))

    # --- V2 / V3 / mapping_absent / work_type_unknown ---
    all_pairs = set(by_structure_pairs(ratios)) | set(level_by_pair)
    unknown_work_type_pairs: List[Tuple[str, str]] = []
    for pair in sorted(all_pairs):
        sid, aid = pair
        value = _pair_ratio(ratios, pair)
        level = level_by_pair.get(pair)
        lv = _s(level).strip().upper()
        has_mapping = pair in level_by_pair
        wt = _wt(aid)
        group_key = "%s|%s" % (sid, wt)

        if wt == WORK_TYPE_UNKNOWN:
            unknown_work_type_pairs.append(pair)
            violations.append(_v(
                "ratio", "work_type_unknown", _WARNING,
                "L4 %s 在 l4_to_l3 里查不到工种(L3) → 该行归入 %s 组，"
                "无法按「结构类型 × 工种」口径判定（组 %s）"
                % (aid, WORK_TYPE_UNKNOWN, group_key),
                [sid, aid, "work_type_id"],
                {"structure_type_id": sid, "activity_id": aid,
                 "work_type_id": WORK_TYPE_UNKNOWN, "group_key": group_key},
            ))

        if value is not None and value > 0.0:
            if has_mapping and lv == "EXCLUDED":
                violations.append(_v(
                    "ratio", "V2_ratio_excluded", _ERROR,
                    "V2 一致违规：组 %s 的 L4 %s 占比 %.6f > 0，但映射档位为 EXCLUDED"
                    % (group_key, aid, value),
                    [sid, aid, "applicability_level"],
                    {"structure_type_id": sid, "activity_id": aid,
                     "work_type_id": wt, "group_key": group_key,
                     "ratio_percent": value, "applicability_level": level},
                ))
            elif not has_mapping:
                violations.append(_v(
                    "ratio", "mapping_absent", _WARNING,
                    "占比 > 0 但结构映射表没有该行（诚实留白，不是 V2 违规）："
                    "组 %s 的 L4 %s 占比 %.6f"
                    % (group_key, aid, value),
                    [sid, aid, "mapping_absent"],
                    {"structure_type_id": sid, "activity_id": aid,
                     "work_type_id": wt, "group_key": group_key,
                     "ratio_percent": value},
                ))
        if pair in exempt and value is not None and value > 0.0:
            violations.append(_v(
                "ratio", EXEMPT_CODE, _ERROR,
                "V5 豁免守卫违规：组 %s 的 L4 %s 已声明「不参与」（路线 2 豁免），"
                "但占比 %.6f > 0 —— 占比只允许落在「切分类」（部位/构件）条目上"
                % (group_key, aid, value),
                [sid, aid, "exempt"],
                {"structure_type_id": sid, "activity_id": aid,
                 "work_type_id": wt, "group_key": group_key,
                 "ratio_percent": value, "applicability_level": level,
                 "exempt": True},
            ))
        if lv == "REQUIRED" and (value is None or value <= 0.0) and pair not in exempt:
            violations.append(_v(
                "ratio", "V3_required_zero", _ERROR,
                "V3 完整违规：组 %s 的 L4 %s 档位 REQUIRED，但占比 %s（须 > 0）"
                % (group_key, aid, "缺失" if value is None else "%.6f" % value),
                [sid, aid, "ratio_percent"],
                {"structure_type_id": sid, "activity_id": aid,
                 "work_type_id": wt, "group_key": group_key,
                 "ratio_percent": value, "applicability_level": level},
            ))

    # --- V4 落地 ---
    v4_skipped = wbs_landed is None
    mapping_absent_count = sum(
        1 for p in all_pairs
        if p not in level_by_pair and (_pair_ratio(ratios, p) or 0.0) > 0.0
    )
    if v4_skipped:
        violations.append(_v(
            "ratio", "V4_skipped", _WARNING,
            "V4 落地校验已跳过（未提供 wbs_landed）", ["", "", "wbs_landed"], {},
        ))
    else:
        landed = _landed_set(wbs_landed)
        for pair in sorted(by_structure_pairs(ratios)):
            value = _pair_ratio(ratios, pair)
            if value is not None and value > 0.0 and pair not in landed:
                wt = _wt(pair[1])
                group_key = "%s|%s" % (pair[0], wt)
                violations.append(_v(
                    "ratio", "V4_not_landed", _ERROR,
                    "V4 落地违规：组 %s 的 L4 %s 占比 %.6f > 0，但最终 WBS 无落点"
                    % (group_key, pair[1], value),
                    [pair[0], pair[1], "wbs_landed"],
                    {"structure_type_id": pair[0], "activity_id": pair[1],
                     "work_type_id": wt, "group_key": group_key,
                     "ratio_percent": value},
                ))

    structure_sums: Dict[str, float] = {}
    for r in ratios:
        value = _num(r["ratio_percent"])
        if value is not None:
            sid = r["structure_type_id"]
            structure_sums[sid] = structure_sums.get(sid, 0.0) + value

    # 组全集（占比侧 ∪ 映射侧）：用于给出「没有任何占比行的组」规模
    # = 被判定为「部位不可切分」的组 + 全部档位都是 EXCLUDED 的组。
    universe_groups = {(r["structure_type_id"], _wt(r["activity_id"])) for r in ratios}
    universe_groups |= {
        (_s(m["structure_type_id"]), _wt(m["activity_id"])) for m in mappings
    }

    stats = {
        "ratio_rows": len(ratios),
        "mapping_rows": len(mappings),
        "structure_types": len(by_structure),
        "groups": len(by_group),
        "groups_without_ratio": len(universe_groups) - len(by_group),
        "work_types": sorted({wt for (_sid, wt) in by_group
                              if wt != WORK_TYPE_UNKNOWN}),
        "group_sums": {k: sums[k] for k in sorted(sums)},
        "structure_type_sums": {k: structure_sums[k] for k in sorted(structure_sums)},
        "single_l4_groups": single_l4_groups,
        "exempt_inputs": len(exempt),
        "exempt_entries": len(exempt & all_pairs),
        "work_type_unknown_pairs": len(unknown_work_type_pairs),
        "v4_skipped": v4_skipped,
        "mapping_absent": mapping_absent_count,
        "tolerance": tol,
    }
    return _result("ratio_v1_v4", violations, stats)


def by_structure_pairs(rows: Iterable[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """返回 ``（结构类型, L4）`` 去重列表（已去重，不排序）。"""
    seen = {}
    for r in rows or []:
        seen[(r["structure_type_id"], r["activity_id"])] = True
    return list(seen)


def _pair_ratio(rows: Iterable[Mapping[str, Any]], pair: Tuple[str, str]) -> Optional[float]:
    """同一 (结构类型, L4) 多行时**累加**占比（重复行由映射表校验负责报违规）。"""
    acc, found = 0.0, False
    for r in rows or []:
        if (r["structure_type_id"], r["activity_id"]) == pair:
            value = _num(r["ratio_percent"])
            if value is not None:
                acc += value
                found = True
    return acc if found else None


def _norm_ratio_row(row: Any, index: int) -> Dict[str, Any]:
    return {
        "structure_type_id": _s(_get(row, "structure_type_id")),
        "activity_id": _s(_get(row, "activity_id")),
        "ratio_percent": _get(row, "ratio_percent"),
        "_index": index,
    }


def _norm_mapping_row(row: Any, index: int) -> Dict[str, Any]:
    return {
        "structure_type_id": _s(_get(row, "structure_type_id")),
        "activity_id": _s(_get(row, "activity_id")),
        "applicability_level": _get(row, "applicability_level"),
        "_index": index,
    }


def _landed_set(wbs_landed: Iterable[Any]) -> set:
    """把 ``wbs_landed`` 归一成 ``{(structure_type_id, activity_id)}`` 集合。

    接受三种元素：2 元 tuple/list、``"结构|L4"`` 字符串、``{"structure_type_id":…,
    "activity_id":…}`` 映射。
    """
    out = set()
    for item in wbs_landed or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            out.add((_s(item[0]), _s(item[1])))
        elif isinstance(item, Mapping):
            out.add((_s(item.get("structure_type_id")), _s(item.get("activity_id"))))
        elif isinstance(item, str) and "|" in item:
            head, _, tail = item.partition("|")
            out.add((head, tail))
    return out


# ======================================================================
# 校验 2：MWI 五类完整性
# ======================================================================


def check_mwi_completeness(
    mwi_rows: Iterable[Mapping[str, Any]],
    *,
    expected_mode_counts: Optional[Mapping[str, int]] = None,
    required_modes: Sequence[str] = MWI_CAPACITY_MODES,
) -> CheckResult:
    """MWI 表（``Resource_Workface_Index``）五类完整性校验（纯逻辑）。

    数据契约
    --------
    ``mwi_rows`` 每行 ``{"resource_name": str, "resource_kind": str("labor"|"machine"),
    "mwi": float|None, "mwi_unit": str|None, "resource_mobility": str, "capacity_mode": str}``。

    判据
    ----
    * ``capacity_mode`` 必须 ∈ ``MWI_CAPACITY_MODES``（五类）→ 否则
      ``code="mwi_mode_unknown"``。
    * **五类必须非空**（用户要求"MWI 五类完整性"）→ 缺类 ``code="mwi_mode_empty"``。
    * ``mwi`` 必须是 > 0 的有限数值 → 否则 ``code="mwi_value_invalid"``。
    * ``mwi_unit`` 与 ``resource_kind`` 必须匹配：人工 → ``m²/人``，机械 → ``m²/台``
      （见 ``资源工位密度MWI_送审表.md:38``、``kb_migrate_phase1_mwi.py:150,157``）。
      → 否则 ``code="mwi_unit_kind_mismatch"``。
    * ``resource_mobility`` 必须 ∈ ``{fixed, mobile, site}`` → 否则
      ``code="mwi_mobility_unknown"``。
    * ``resource_kind == "labor"`` 且 ``mobility != "fixed"`` → 报警
      （§3.2 判据：人工在作业面里面干活，全部人工都是 fixed）→
      ``code="mwi_labor_mobility_not_fixed"``。
    * ``resource_name`` 不得重复 → 否则 ``code="mwi_duplicate_name"``。

    单位口径（**已确认**）
    ----------------------
    * 人工：``mwi_unit`` 必须是 ``m²/人``（``kb_migrate_phase1_mwi.py:150`` 写死）。
    * 机械：``m²/台`` **只对** ``capacity_mode == "area"`` 要求
      （``kb_migrate_phase1_mwi.py:157``：``unit = "m2/台" if mode == "area" else None``）；
      非 area 型的 ``mwi`` 本身就是 ``None``，其**也没有面积单位**。
      非 area 型带了面积单位 → 同样报 ``mwi_unit_kind_mismatch``。
    * 非 area 型（position/auxiliary/transport/site）的 ``mwi`` 允许为 ``None``；
      **但若给了数值就必须 > 0**。

    参数
    ----
    * ``expected_mode_counts``：可选，如 ``{"area": 27, "position": 21, "auxiliary": 11,
      "transport": 5, "site": 3}``；与实际条数不符 → ``code="mwi_mode_count_mismatch"``
      （severity=warning，因为库可能仍在迁移中）。
    * ``required_modes``：必须非空的类别集合，默认五类全要。

    返回
    ----
    ``CheckResult``，``stats`` 含 ``rows`` / ``mode_counts`` / ``kind_counts``。
    """
    rows = [_norm_mwi_row(r, i) for i, r in enumerate(mwi_rows or [])]
    violations: List[Violation] = []
    mode_counts: Dict[str, int] = {m: 0 for m in MWI_CAPACITY_MODES}
    kind_counts: Dict[str, int] = {}
    expected = dict(expected_mode_counts or {})

    for r in rows:
        name = r["resource_name"]
        mode = r["capacity_mode"]
        kind = r["resource_kind"]
        mobility = r["resource_mobility"]
        unit = normalize_area_unit(r["mwi_unit"])
        value = _num(r["mwi"])

        kind_counts[kind] = kind_counts.get(kind, 0) + 1

        # capacity_mode
        if mode not in MWI_CAPACITY_MODES:
            violations.append(_v(
                "mwi", "mwi_mode_unknown", _ERROR,
                "资源 %s 的 capacity_mode=%r 不在五类 %s 内"
                % (name, mode, list(MWI_CAPACITY_MODES)),
                [name, "capacity_mode"],
                {"resource_name": name, "capacity_mode": mode},
            ))
        else:
            mode_counts[mode] += 1

        # mwi 数值
        if mode == "area":
            if value is None or value <= 0:
                violations.append(_v(
                    "mwi", "mwi_value_invalid", _ERROR,
                    "资源 %s（area 型）的 mwi=%r 必须是 > 0 的数值" % (name, r["mwi"]),
                    [name, "mwi"],
                    {"resource_name": name, "mwi": repr(r["mwi"]),
                     "capacity_mode": mode},
                ))
        else:
            if r["mwi"] is not None and (value is None or value <= 0):
                violations.append(_v(
                    "mwi", "mwi_value_invalid", _ERROR,
                    "资源 %s（%s 型）的 mwi=%r 既不是正数也不是空值"
                    % (name, mode, r["mwi"]),
                    [name, "mwi"],
                    {"resource_name": name, "mwi": repr(r["mwi"]),
                     "capacity_mode": mode},
                ))

        # mwi_unit ↔ resource_kind
        expected_unit = _UNIT_BY_KIND.get(kind)
        if expected_unit is None:
            violations.append(_v(
                "mwi", "mwi_kind_unknown", _ERROR,
                "资源 %s 的 resource_kind=%r 不是 labor/machine" % (name, kind),
                [name, "resource_kind"],
                {"resource_name": name, "resource_kind": kind},
            ))
        elif mode == "area" and unit != expected_unit:
            violations.append(_v(
                "mwi", "mwi_unit_kind_mismatch", _ERROR,
                "资源 %s（%s / area 型）的 mwi_unit=%r，应为 %r"
                % (name, kind, r["mwi_unit"], expected_unit),
                [name, "mwi_unit"],
                {"resource_name": name, "resource_kind": kind,
                 "capacity_mode": mode, "mwi_unit": r["mwi_unit"],
                 "expected_unit": expected_unit},
            ))
        elif mode != "area" and unit is not None:
            violations.append(_v(
                "mwi", "mwi_unit_kind_mismatch", _ERROR,
                "资源 %s（%s / %s 型）不应带面积单位，得到 mwi_unit=%r"
                % (name, kind, mode, r["mwi_unit"]),
                [name, "mwi_unit"],
                {"resource_name": name, "resource_kind": kind,
                 "capacity_mode": mode, "mwi_unit": r["mwi_unit"],
                 "expected_unit": None},
            ))

        # mobility
        if mobility not in MWI_MOBILITIES:
            violations.append(_v(
                "mwi", "mwi_mobility_unknown", _ERROR,
                "资源 %s 的 resource_mobility=%r 不在 %s 内"
                % (name, mobility, list(MWI_MOBILITIES)),
                [name, "resource_mobility"],
                {"resource_name": name, "resource_mobility": mobility},
            ))
        elif kind == "labor" and mobility != "fixed":
            violations.append(_v(
                "mwi", "mwi_labor_mobility_not_fixed", _ERROR,
                "资源 %s 是 labor，但 resource_mobility=%r（人工必须 fixed）"
                % (name, mobility),
                [name, "resource_mobility"],
                {"resource_name": name, "resource_kind": kind,
                 "resource_mobility": mobility},
            ))

    # 五类非空
    for mode in required_modes:
        if mode_counts.get(mode, 0) == 0:
            violations.append(_v(
                "mwi", "mwi_mode_empty", _ERROR,
                "MWI 五类完整性违规：capacity_mode=%s 一行都没有" % mode,
                ["", mode],
                {"capacity_mode": mode, "count": 0},
            ))

    # 预期条数核对
    for mode in sorted(expected):
        want = expected[mode]
        got = mode_counts.get(mode, 0)
        if got != want:
            violations.append(_v(
                "mwi", "mwi_mode_count_mismatch", _WARNING,
                "capacity_mode=%s 预期 %d 行，实际 %d 行" % (mode, want, got),
                ["", mode],
                {"capacity_mode": mode, "expected": want, "actual": got},
            ))

    # 资源名重复
    seen: Dict[str, List[int]] = {}
    for i, r in enumerate(rows):
        seen.setdefault(r["resource_name"], []).append(i)
    for name in sorted(seen):
        idx = seen[name]
        if len(idx) > 1:
            violations.append(_v(
                "mwi", "mwi_duplicate_name", _ERROR,
                "资源名 %s 重复 %d 次（行序 %s）" % (name, len(idx), idx),
                [name, "resource_name"],
                {"resource_name": name, "count": len(idx), "row_indexes": idx},
            ))

    stats = {
        "rows": len(rows),
        "mode_counts": {m: mode_counts.get(m, 0) for m in MWI_CAPACITY_MODES},
        "kind_counts": {k: kind_counts[k] for k in sorted(kind_counts)},
    }
    return _result("mwi_completeness", violations, stats)


def _norm_mwi_row(row: Any, index: int) -> Dict[str, Any]:
    return {
        "resource_name": _s(_get(row, "resource_name")),
        "resource_kind": _s(_get(row, "resource_kind")),
        "mwi": _get(row, "mwi"),
        "mwi_unit": _get(row, "mwi_unit"),
        "resource_mobility": _s(_get(row, "resource_mobility")),
        "capacity_mode": _s(_get(row, "capacity_mode")),
        "_index": index,
    }


# ======================================================================
# 校验 3：映射表三档无空档
# ======================================================================


def check_mapping_tiers(
    mapping_rows: Iterable[Mapping[str, Any]],
    known_l4: Optional[Iterable[str]] = None,
    known_structures: Optional[Iterable[str]] = None,
    *,
    l4_to_l3: Optional[Mapping[str, Any]] = None,
) -> CheckResult:
    """映射表三档无空档校验（纯逻辑）。

    数据契约
    --------
    * ``mapping_rows``：每行 ``{"structure_type_id": str, "activity_id": str,
      "applicability_level": str|None}``。
    * ``known_l4``：全部 L4（``activity_id``）集合；``None`` → 跳过外键校验。
    * ``known_structures``：全部结构类型集合；``None`` → 跳过外键校验。
    * ``l4_to_l3``：可选 ``{activity_id: l3_id}``，用于算 L3 覆盖率。

    判据
    ----
    * ``applicability_level`` 必须 ∈ ``L4_MAPPING_LEVELS``。
      ``USUAL`` 是**遗留值**，单列 ``code="mapping_level_legacy"``（warning），
      **不算硬违规**。
    * 档位为空 / NULL → ``code="mapping_level_null"``（error）；"无空档"的硬要求
      就是**不允许有行却档位为空**。
    * 允许"某 L4 完全没有任何结构映射行"（A3 的诚实留白）→ 只登记
      ``stats["mapping_absent_l4"]``，不报违规。
    * ``structure_type_id`` / ``activity_id`` 必须存在于 ``known_structures`` /
      ``known_l4`` → 否则 ``code="mapping_fk_unknown_structure"`` /
      ``"mapping_fk_unknown_l4"``。
    * 重复行（同一 (结构类型, L4) 出现多次）→ ``code="mapping_duplicate_row"``。
    * 每个 L3 的覆盖率（有映射行的 L4 数 ÷ 该 L3 的 L4 总数）；覆盖率为 0 的 L3
      逐个列出 → ``code="mapping_l3_zero_coverage"``（warning，A2 要修的对象）。

    返回
    ----
    ``CheckResult``，``stats`` 含 ``rows`` / ``level_counts`` /
    ``distinct_l4`` / ``mapping_absent_l4`` / ``l3_coverage`` / ``zero_coverage_l3``。
    """
    rows = [_norm_mapping_row(r, i) for i, r in enumerate(mapping_rows or [])]
    violations: List[Violation] = []
    check_fk = known_l4 is not None and known_structures is not None
    l4_set = set(_s(x) for x in (known_l4 or []))
    st_set = set(_s(x) for x in (known_structures or []))

    level_counts: Dict[str, int] = {}
    pair_rows: Dict[Tuple[str, str], List[int]] = {}

    for r in rows:
        sid, aid = r["structure_type_id"], r["activity_id"]
        raw_level = r["applicability_level"]
        level = _s(raw_level).strip()
        upper = level.upper()

        if level == "":
            violations.append(_v(
                "mapping", "mapping_level_null", _ERROR,
                "映射行档位为空/NULL：结构类型 %s 的 L4 %s" % (sid, aid),
                [sid, aid, "applicability_level"],
                {"structure_type_id": sid, "activity_id": aid,
                 "applicability_level": raw_level},
            ))
            level_counts["<NULL>"] = level_counts.get("<NULL>", 0) + 1
        elif upper in LEGACY_MAPPING_LEVELS:
            violations.append(_v(
                "mapping", "mapping_level_legacy", _WARNING,
                "映射行档位 %r 是遗留值（应迁移为 OPTIONAL）：结构类型 %s 的 L4 %s"
                % (raw_level, sid, aid),
                [sid, aid, "applicability_level"],
                {"structure_type_id": sid, "activity_id": aid,
                 "applicability_level": raw_level, "suggested": "OPTIONAL"},
            ))
            level_counts[upper] = level_counts.get(upper, 0) + 1
        elif upper in L4_MAPPING_LEVELS:
            level_counts[upper] = level_counts.get(upper, 0) + 1
        else:
            violations.append(_v(
                "mapping", "mapping_level_unknown", _ERROR,
                "映射行档位 %r 不在三档 %s 内：结构类型 %s 的 L4 %s"
                % (raw_level, list(L4_MAPPING_LEVELS), sid, aid),
                [sid, aid, "applicability_level"],
                {"structure_type_id": sid, "activity_id": aid,
                 "applicability_level": raw_level},
            ))
            level_counts[upper or "<EMPTY>"] = level_counts.get(upper or "<EMPTY>", 0) + 1

        if check_fk:
            if sid not in st_set:
                violations.append(_v(
                    "mapping", "mapping_fk_unknown_structure", _ERROR,
                    "映射行的结构类型 %r 不在 known_structures 内" % sid,
                    [sid, aid, "structure_type_id"],
                    {"structure_type_id": sid, "activity_id": aid},
                ))
            if aid not in l4_set:
                violations.append(_v(
                    "mapping", "mapping_fk_unknown_l4", _ERROR,
                    "映射行的 L4 %r 不在 known_l4 内" % aid,
                    [sid, aid, "activity_id"],
                    {"structure_type_id": sid, "activity_id": aid},
                ))

        pair_rows.setdefault((sid, aid), []).append(r["_index"])

    # 重复行
    for pair in sorted(pair_rows):
        idx = pair_rows[pair]
        if len(idx) > 1:
            violations.append(_v(
                "mapping", "mapping_duplicate_row", _ERROR,
                "映射行重复：结构类型 %s 的 L4 %s 出现 %d 次（行序 %s）"
                % (pair[0], pair[1], len(idx), idx),
                [pair[0], pair[1], "applicability_level"],
                {"structure_type_id": pair[0], "activity_id": pair[1],
                 "count": len(idx), "row_indexes": idx},
            ))

    # 覆盖率（诚实留白：某 L4 完全没有映射行 → 只计数，不报违规）
    mapped_l4 = set(pair[1] for pair in pair_rows)
    absent_l4 = sorted(l4_set - mapped_l4) if l4_set else []
    l3_coverage: Dict[str, Dict[str, Any]] = {}
    zero_l3: List[str] = []
    if l4_to_l3:
        by_l3: Dict[str, List[str]] = {}
        for aid in sorted(l4_set or set(l4_to_l3.keys())):
            l3 = _s(l4_to_l3.get(aid))
            if not l3:
                continue
            by_l3.setdefault(l3, []).append(aid)
        for l3 in sorted(by_l3):
            members = by_l3[l3]
            covered = [m for m in members if m in mapped_l4]
            ratio = (len(covered) / len(members)) if members else 0.0
            l3_coverage[l3] = {"l4_total": len(members),
                               "l4_covered": len(covered),
                               "coverage": ratio}
            if not covered:
                zero_l3.append(l3)
                violations.append(_v(
                    "mapping", "mapping_l3_zero_coverage", _WARNING,
                    "L3 %s 的 %d 个 L4 全部没有任何结构映射行（覆盖率 0）"
                    % (l3, len(members)),
                    [l3, "", "coverage"],
                    {"l3": l3, "l4_total": len(members), "l4_covered": 0,
                     "sample_l4": members[:10]},
                ))

    stats = {
        "rows": len(rows),
        "level_counts": {k: level_counts[k] for k in sorted(level_counts)},
        "distinct_pairs": len(pair_rows),
        "distinct_l4": len(mapped_l4),
        "mapping_absent_l4": len(absent_l4),
        "l3_coverage": l3_coverage,
        "zero_coverage_l3": zero_l3,
    }
    return _result("mapping_tiers", violations, stats)


# ======================================================================
# 校验 4：条件无缺维
# ======================================================================


def check_condition_dimensions(
    condition_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    norm_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    condition_keys_used: Optional[Iterable[Any]] = None,
    *,
    meta_keys: Sequence[str] = LEAF_CONDITION_META_KEYS,
) -> CheckResult:
    """D 组条件无缺维校验（纯逻辑）。

    数据契约
    --------
    * ``condition_rows``：``Condition_Dictionary`` 的行。
      真实表结构（已核对 `BuildPlan_KB/kb.db`，1635 行）：
      ``{condition_id: str, condition_type: str, condition_value: str,
      applicable_work_type: str|None}``。
      本函数把 ``condition_type`` 当**维度名**、``condition_value`` 当**取值**。
    * ``norm_rows``：定额行索引，每条
      ``{activity_id: str, condition_key: Any, table: "labor"|"equipment",
      condition_combination: dict 可选}``。
      ``condition_key`` 可以是 ``{维度: 值}`` 字典，或 ``"维度=值;…"`` 字符串，
      或已序列化 JSON；``condition_combination`` 可给显式维度字典。
    * ``condition_keys_used``：绑定阶段实际产生的 ``leaf["condition_key"]`` 集合；
      ``None`` → 从 ``norm_rows`` 的 ``condition_key`` 反推（去重）。
      元素可以是 ``{维度: 值}`` 字典 / ``"维度=值;…"`` 字符串。

    判据
    ----
    * **D2 精确匹配**：每个用到的 ``condition_key`` 必须能在 ``Norm_Labor_Table``
      或 ``Norm_Equipment_Table`` 里找到精确匹配行（维度集合逐维相等）。
      匹配不上 → ``code="condition_key_unmatched"``。
      **禁止模糊匹配与退默认行**：本函数只做整键相等，不做子集/前缀/相似度匹配。
    * 条件维不得为空 / NULL：``condition_key`` 整键为空，或任一维的名字/取值为空
      → ``code="condition_dim_empty"``。
    * 若给了 ``condition_rows``，``condition_key`` 的每一维（跳过 ``meta_keys``）
      都必须能在字典里查到取值 → 否则 ``code="condition_dim_unknown"``。

    返回
    ----
    ``CheckResult``，``stats`` 含 ``keys_used`` / ``matched_keys`` /
    ``norm_rows`` / ``dictionary_dims``。
    """
    dict_rows = [_norm_condition_row(r, i) for i, r in enumerate(condition_rows or [])]
    norms = [_norm_norm_row(r, i) for i, r in enumerate(norm_rows or [])]
    violations: List[Violation] = []

    # 字典维度 → 取值集合
    dict_dims: Dict[str, set] = {}
    for d in dict_rows:
        dim = d["dimension"]
        if dim == "":
            violations.append(_v(
                "condition", "condition_dict_dim_empty", _ERROR,
                "Condition_Dictionary 第 %d 行维度为空（condition_id=%s）"
                % (d["_index"], d["condition_id"]),
                [d["condition_id"], "", "condition_type"],
                {"condition_id": d["condition_id"],
                 "condition_type": d["dimension"]},
            ))
            continue
        value = d["value"]
        if value is None or _s(value).strip() == "":
            violations.append(_v(
                "condition", "condition_dict_value_empty", _ERROR,
                "Condition_Dictionary 维度 %s 的取值为空（condition_id=%s）"
                % (dim, d["condition_id"]),
                [d["condition_id"], dim, "condition_value"],
                {"condition_id": d["condition_id"], "dimension": dim,
                 "condition_value": value},
            ))
            continue
        dict_dims.setdefault(dim, set()).add(_s(value).strip())

    # 定额行索引：维度组合 → 行
    norm_index: Dict[frozenset, List[Dict[str, Any]]] = {}
    for n in norms:
        combo = n["dims"]
        key = frozenset(combo.items())
        norm_index.setdefault(key, []).append(n)

    # 用到的 condition_key 集合
    if condition_keys_used is None:
        raw_keys: List[Any] = []
        seen_raw = set()
        for n in norms:
            marker = repr(n["raw_key"])
            if marker not in seen_raw:
                seen_raw.add(marker)
                raw_keys.append(n["raw_key"])
    else:
        raw_keys = list(condition_keys_used)

    matched = 0
    for i, raw in enumerate(raw_keys):
        dims = _condition_dims(raw)
        label = _key_label(raw)
        # 维度空/NULL
        empty_dims = []
        if not dims:
            empty_dims.append("<整键为空>")
        for dim, val in sorted(dims.items()):
            if dim is None or _s(dim).strip() == "" or val is None or _s(val).strip() == "":
                empty_dims.append(_s(dim))
        if empty_dims:
            violations.append(_v(
                "condition", "condition_dim_empty", _ERROR,
                "条件缺维/空值：condition_key %s 的问题维度 %s" % (label, empty_dims),
                [label, ",".join(empty_dims), "condition_key"],
                {"condition_key": dims, "empty_dims": empty_dims, "index": i},
            ))
            continue

        # D2 精确匹配（整键相等；不模糊、不退默认）
        if frozenset(dims.items()) not in norm_index:
            violations.append(_v(
                "condition", "condition_key_unmatched", _ERROR,
                "D2 精确匹配失败：condition_key %s 在 Norm_Labor_Table / "
                "Norm_Equipment_Table 找不到精确匹配行（禁止模糊匹配与退默认行）" % label,
                [label, ",".join(sorted(_s(d) for d in dims)), "condition_key"],
                {"condition_key": dims, "index": i},
            ))
        else:
            matched += 1

        # 维度取值必须在字典里
        if dict_dims:
            for dim in sorted(dims):
                if dim in meta_keys:
                    continue
                if dim not in dict_dims:
                    violations.append(_v(
                        "condition", "condition_dim_unknown", _ERROR,
                        "条件维度 %s（值 %r）不在 Condition_Dictionary 的任何维度里"
                        % (dim, dims[dim]),
                        [label, dim, "dimension"],
                        {"condition_key": dims, "dimension": dim,
                         "value": dims[dim], "known_dims": sorted(dict_dims)},
                    ))
                elif _s(dims[dim]).strip() not in dict_dims[dim]:
                    violations.append(_v(
                        "condition", "condition_dim_value_unknown", _ERROR,
                        "条件维度 %s 的取值 %r 不在 Condition_Dictionary 里（已知 %d 个取值）"
                        % (dim, dims[dim], len(dict_dims[dim])),
                        [label, dim, "condition_value"],
                        {"condition_key": dims, "dimension": dim,
                         "value": dims[dim],
                         "known_values": sorted(dict_dims[dim])[:20]},
                    ))

    stats = {
        "keys_used": len(raw_keys),
        "matched_keys": matched,
        "norm_rows": len(norms),
        "condition_dictionary_rows": len(dict_rows),
        "dictionary_dims": {k: len(dict_dims[k]) for k in sorted(dict_dims)},
        "norm_tables": sorted({n["table"] for n in norms}),
    }
    return _result("condition_dimensions", violations, stats)


def _norm_condition_row(row: Any, index: int) -> Dict[str, Any]:
    """`Condition_Dictionary` 行 → 归一化维度/取值。"""
    dimension = _get(row, "dimension")
    if dimension is None:
        dimension = _get(row, "condition_type")
    value = _get(row, "value")
    if value is None:
        value = _get(row, "condition_value")
    return {
        "condition_id": _s(_get(row, "condition_id")),
        "dimension": _s(dimension).strip(),
        "value": value,
        "_index": index,
    }


def _norm_norm_row(row: Any, index: int) -> Dict[str, Any]:
    raw_key = _get(row, "condition_key")
    combo = _get(row, "condition_combination")
    dims = _condition_dims(combo) if combo else _condition_dims(raw_key)
    return {
        "activity_id": _s(_get(row, "activity_id")),
        "raw_key": raw_key,
        "dims": dims,
        "table": _s(_get(row, "table")),
        "_index": index,
    }


def _condition_dims(raw: Any) -> Dict[str, Any]:
    """把 ``condition_key`` 归一成 ``{维度: 值}`` 字典（无法解析 → ``{}``）。"""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        out = {}
        for k, v in raw.items():
            if v is None:
                continue
            out["" if k is None else _s(k).strip()] = v
        return out
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("{}", "null", "None"):
            return {}
        if text.startswith("{"):
            try:
                import json
                parsed = json.loads(text)
            except Exception:
                return {}
            if isinstance(parsed, Mapping):
                return {_s(k).strip(): v for k, v in parsed.items() if v is not None}
            return {}
        out: Dict[str, Any] = {}
        for chunk in text.replace("；", ";").replace("，", ";").split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            for sep in ("=", ":", "："):
                if sep in chunk:
                    dim, _, val = chunk.partition(sep)
                    out[dim.strip()] = val.strip()
                    break
        return out
    return {}


def _key_label(raw: Any) -> str:
    """条件键的稳定文本标签（用于排序键与报错文案）。"""
    dims = _condition_dims(raw)
    if not dims:
        return _s(raw).strip() or "<空>"
    return ",".join("%s=%s" % (d, _s(dims[d]).strip()) for d in sorted(dims))


# ======================================================================
# 顶层入口
# ======================================================================


def run_all_checks(
    *,
    ratio_rows: Iterable[Mapping[str, Any]] = (),
    mapping_rows: Iterable[Mapping[str, Any]] = (),
    wbs_landed: Optional[Iterable[Any]] = None,
    mwi_rows: Iterable[Mapping[str, Any]] = (),
    expected_mode_counts: Optional[Mapping[str, int]] = None,
    known_l4: Optional[Iterable[str]] = None,
    known_structures: Optional[Iterable[str]] = None,
    l4_to_l3: Optional[Mapping[str, Any]] = None,
    exempt_activity_ids: Optional[Iterable[Any]] = None,
    exempt_pairs: Optional[Iterable[Any]] = None,
    condition_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    norm_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    condition_keys_used: Optional[Iterable[Any]] = None,
    tolerance: float = RATIO_TOLERANCE,
    meta_keys: Sequence[str] = LEAF_CONDITION_META_KEYS,
) -> Dict[str, Any]:
    """运行四个闭合校验并汇总（确定性、不抛异常、不 print）。

    数据契约见各子函数 docstring。

    ``l4_to_l3``（``{activity_id: work_type_id}``）**同时**下传给
    ``check_ratio_v1_v4``（B3 按「结构类型 × 工种」分组）与
    ``check_mapping_tiers``（L3 覆盖率），两处口径一致。

    ``exempt_activity_ids`` / ``exempt_pairs``（路线 2 的"不参与"清单）**只**下传给
    ``check_ratio_v1_v4``：映射档位校验不关心占比口径。两个参数默认 None，不传即
    完全保持路线 1 的既有语义。

    返回
    ----
    ``{"checks": [CheckResult,…],            # 固定顺序：ratio/mwi/mapping/condition
       "violations": [Violation,…],          # 全部违规，稳定排序
       "warnings": [Violation,…],            # severity == "warning" 的子集
       "stats": {group: stats,…},
       "all_green": bool}``
    """
    ratio = check_ratio_v1_v4(ratio_rows, mapping_rows, wbs_landed,
                              tolerance=tolerance, l4_to_l3=l4_to_l3,
                              exempt_activity_ids=exempt_activity_ids,
                              exempt_pairs=exempt_pairs)
    mwi = check_mwi_completeness(mwi_rows, expected_mode_counts=expected_mode_counts)
    mapping = check_mapping_tiers(mapping_rows, known_l4, known_structures, l4_to_l3=l4_to_l3)
    condition = check_condition_dimensions(
        condition_rows, norm_rows, condition_keys_used, meta_keys=meta_keys)

    checks = [ratio, mwi, mapping, condition]
    violations: List[Violation] = []
    for c in checks:
        violations.extend(c["violations"])
    violations = _sort_violations(violations)
    warnings = [r for r in violations if r["severity"] == _WARNING]
    return {
        "checks": checks,
        "violations": violations,
        "warnings": warnings,
        "stats": {c["check"]: c["stats"] for c in checks},
        "all_green": all(c["all_green"] for c in checks),
    }
