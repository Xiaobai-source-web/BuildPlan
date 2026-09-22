"""节点：知识库范围装配（kb_scope）— 纯代码节点，不调用任何 LLM。

职责：把「建筑类型 + 结构形式」翻译成这个项目该用哪些 L3（工种大类）、
以及每个 L3 下哪些 L4（具体工序）是合法的，供下游 WBS 生成 / 资源估算使用。

设计要点（与产品约定一致，改动前请先读这段）：
  1. L3 选定：kb.l3_for(building_type_id) 给出 REQUIRED / OPTIONAL / EXCLUDED **三档**
     （A1 映射三档化：旧 `USUAL` 与旧 `OPTIONAL` 合并为一档，枚举名 `OPTIONAL`）。
     REQUIRED + OPTIONAL 进 l3_list（level 原样保留），EXCLUDED 进 excluded_l3。
     历史档位 `USUAL` 由 `normalize_level` 统一映射成 `OPTIONAL`（见 `LEVEL_ALIASES`），
     旧库 / 旧产物 / 旧测试里的 `USUAL` 一律照常收下，不会因为改名而丢工种。
     **注意**：kb.l3_for 的 SQL 排序里没有 OPTIONAL 分支，OPTIONAL 行会排在最前面，
     所以一律按 level 字段判断，绝不能按位置切片。
  2. 结构映射只用来**过滤 L4**。这里刻意不实现（已废弃的）"整工种降级"规则：
     不能因为某 L3 在某结构形式下没有可用 L4 就把该 L3 降级为 EXCLUDED ——
     映射表只覆盖 4 个 L3（concrete / formwork / rebar / steel_structure），
     其余 27 个没有数据，那条规则会误杀大批工种。
  3. 没数据 ≠ 不适用：kb.structure_l4_filtered 返回 mapping_absent=True 时保留该 L3 全量 L4，
     并把每个 L4 的 structure_mapping_absent 标 True，交给下游自行判断。
  4. 全部查询走 kb.py（本身已优雅降级）；本节点额外兜底，任何异常都降级 + 记 warning，绝不抛。
  5. 识别不到建筑类型 / 结构形式时不报错，只降级并写 warnings。
  6. **警告必须能被看见**（第 23 轮修的真实缺陷）：warnings 之前只被数字化成
     done_summary 里的"警告 N 条"，内容算完就丢，用户永远不知道那 N 条是什么。
     现在：① 逐条原文进 `scope["warnings"]` 与 ctx 的 `kb_warnings`；
     ② done_summary 里给出**归并后**的摘要（同类只报一次 + 计数）；
     ③ plan_json.meta.kb_warnings 留全量原文（交付物口径说明的通道）。
"""

import re

from .. import kb
from ..base import BaseNode
from ..scope_inputs import hard_gate_exclusions, normalize_exclusions

# ----------------------------------------------------------------------
# 档位（A1 映射三档化）
# ----------------------------------------------------------------------
# 现行三档：REQUIRED（必须） / OPTIONAL（可选） / EXCLUDED（排除）。
# `USUAL` 是**历史枚举值**（第 38 轮 material_transport 降级时引入），
# A1 把它与旧 `OPTIONAL` 合并成一档，枚举名统一为 `OPTIONAL`。
# 但 KB / 旧产物 / 旧测试里可能还是 `USUAL` —— 所以读档位的地方一律先过
# `normalize_level`，把 `USUAL` 映射成 `OPTIONAL`。这是**唯一的兼容点**，
# 不要在别处再写散装别名判断。
LEVEL_ALIASES = {"USUAL": "OPTIONAL"}

# L3 两个"收下"的适用性等级（EXCLUDED 单独进 excluded_l3）
_KEEP_LEVELS = ("REQUIRED", "OPTIONAL")
# 等级未知时的占位（识别不到建筑类型时使用）
_LEVEL_UNKNOWN = "UNKNOWN"

# 未识别建筑类型时的说明
_WARN_BUILDING = ("未能识别建筑类型，已退回全部 L3 工种（31 个）且不作建筑类型筛选，"
                  "请人工确认建筑类型。")
# 未识别结构形式时的说明
_WARN_STRUCTURE = "未识别结构形式，未做结构过滤，已保留全部 L4 工序。"
# 结构映射缺数据时的说明（出现在具体 L3 上）
_WARN_MAPPING_ABSENT = "结构映射表中没有该工种的映射数据，已保留其全部 L4（无结构约束）。"

# 归并回显时每类警告的**短名字**（顺序即展示优先级）。
# 为什么要归并：映射表只覆盖少数几个 L3（住宅 + 剪力墙下"缺映射"这一类一次就有二十几条），
# 逐条回显等于把同一句话刷二十几遍 —— 用户上一轮刚抱怨过"全都堆出来"。
_WARN_KINDS = (
    ("structure_mapping_absent",
     "结构映射表缺该工种数据 → 保留全部 L4",
     lambda w: w.endswith(_WARN_MAPPING_ABSENT)),
    ("structure_unknown",
     "未识别结构形式 → 未做结构过滤",
     lambda w: w == _WARN_STRUCTURE),
    ("building_unknown",
     "未识别建筑类型 → 退回全部 31 个 L3",
     lambda w: w == _WARN_BUILDING),
    ("no_l3_mapping",
     "建筑类型在 KB 中无 L3 映射 → 退回全部 L3",
     lambda w: "在 KB 中没有 L3 映射数据" in w),
)
_WARN_OTHER = "other"
# done_summary 里逐条展开的**上限**（其余按类归并成一行，绝不逐条堆）
_WARN_DETAIL_LIMIT = 3


def warning_kind(text):
    """把一条警告归到某个类别 → `(类别 key, 短类别名)`；认不出来 → 原文截断。"""
    t = _as_text(text).strip()
    for key, label, match in _WARN_KINDS:
        try:
            if match(t):
                return key, label
        except Exception:  # noqa: BLE001 — 分类失败绝不能影响节点主流程
            continue
    return _WARN_OTHER, (t[:40] or "未分类警告")


def group_warnings(warnings):
    """把警告按类别归并 → `[{"key","label","count","items"}]`（按首次出现顺序）。

    `items` 保留该类下**去重后的原文**，供明细行取"前 N 条"用。
    """
    groups = []
    index = {}
    for w in warnings or []:
        key, label = warning_kind(w)
        g = index.get(key)
        if g is None:
            g = {"key": key, "label": label, "count": 0, "items": []}
            index[key] = g
            groups.append(g)
        g["count"] += 1
        text = _as_text(w).strip()
        if text and text not in g["items"]:
            g["items"].append(text)
    return groups


def merge_warnings(warnings, limit=_WARN_DETAIL_LIMIT):
    """把警告归并成"一行摘要 + 前 N 条样例 + 其余同类一行"。

    返回 `{"total","groups","summary","samples","note","detail_lines"}`：
      · summary      —— 进 done_summary 的那**一句**：`警告 N 条（N 条均为「…」）`，
                        2~3 类时列出前几类 + 计数，**同类只说一次、不逐条重复**；
      · samples      —— 前 `limit` 条原文（终端逐条展示用）；
      · note         —— `…其余 N 条同类（类别）` 一行；没有剩余时是空串；
      · detail_lines —— samples + note（探针/交付物要整段时直接用）。

    空列表 → total=0、summary=""、samples=[]、note=""（调用方据此不加后缀）。
    """
    groups = group_warnings(warnings)
    total = sum(g["count"] for g in groups)
    if not total:
        return {"total": 0, "groups": [], "summary": "", "samples": [],
                "note": "", "detail_lines": []}

    head = "警告 %d 条" % total
    if len(groups) == 1:
        summary = "%s（%d 条均为「%s」）" % (head, total, groups[0]["label"])
    else:
        shown = groups[:limit]
        bits = "；".join("%d 条「%s」" % (g["count"], g["label"]) for g in shown)
        rest_n = total - sum(g["count"] for g in shown)
        rest_k = len(groups) - len(shown)
        summary = "%s（%s%s）" % (
            head, bits, "；另有 %d 类 %d 条" % (rest_k, rest_n) if rest_n else "")

    # ---- 明细：前 limit 条原文（不同 L3 各占一条）+ 其余按类归并成一行 ----
    flat = []
    for g in groups:
        for t in g["items"]:
            if t not in flat:
                flat.append(t)
    samples = flat[:limit]
    note = ""
    rest = total - len(samples)
    if rest > 0:
        # 同类警告若原文完全相同，`flat[limit:]` 会是空的 —— 这时用前几条的类别
        # 来告诉用户"剩下的还是这一类"，不要退化成干巴巴一句"其余 N 条"。
        rest_groups = group_warnings(flat[limit:] or samples)
        if len(rest_groups) == 1:
            kind_note = "同类（%s）" % rest_groups[0]["label"]
        else:
            kind_note = "分属 %d 类" % len(rest_groups) if rest_groups else "同类"
        # 「plan_json.meta.kb_warnings」是计划文件里的内部字段路径，不该甩给用户
        # —— 告诉他"完整原文在计划文件里"就够了。
        note = "…其余 %d 条%s；完整原文已存进计划数据的知识库警告里" % (rest, kind_note)
    return {"total": total, "groups": groups, "summary": summary, "samples": samples,
            "note": note, "detail_lines": samples + ([note] if note else [])}


def normalize_level(raw):
    """把 KB 里的档位值规范化 → 现行三档字符串（大写）。

    · `None` / 空 → `""`；
    · 历史 `USUAL` → `OPTIONAL`（`LEVEL_ALIASES`，A1 三档化的向后兼容点）；
    · 其余值原样大写返回（由调用方决定收不收）。
    """
    if raw is None:
        return ""
    level = str(raw).strip().upper()
    return LEVEL_ALIASES.get(level, level)


def _normalize_level(raw):
    """`normalize_level` 的内部别名（本模块内历史调用点保持不变）。"""
    return normalize_level(raw)


def _as_text(value):
    """把可能为 None 的字段统一成字符串（避免下游拿到 None）。"""
    return "" if value is None else str(value)


def _labor_type_of(activity_id):
    """取该 L4 的工人种类：labor_types[0]，没有则为空串（管理/验收类不派工种）。"""
    try:
        info = kb.labor_type_for_activity(activity_id) or {}
        types = info.get("labor_types") or []
        if types and types[0]:
            return str(types[0])
    except Exception:  # noqa: BLE001 — KB 异常一律降级为空工种
        return ""
    return ""


def _candidates_for(work_type_id, building_unknown, structure_type_id):
    """组装某 L3 下的合法 L4 候选。

    返回 (L4 列表, mapping_absent, 被结构剔除条数, 工序化判据留痕)：列表每项含
    activity_id / activity_name / unit / applicability_level / production_mode /
    labor_type / structure_mapping_absent。
    """
    full = kb.l4_for(work_type_id) or []
    if full and not isinstance(full, (list, tuple)):
        full = []

    # 情况 A：结构形式没识别出来 → 完全不做结构过滤
    if not structure_type_id:
        rows, absent = full, True
    # 情况 B：建筑类型没识别出来 → 退回该 L3 全量 L4（无结构约束、无适用等级）
    elif building_unknown:
        rows, absent = full, True
    else:
        rows, absent = kb.structure_l4_filtered(work_type_id, structure_type_id)
        if rows is None:
            rows = []
        if not isinstance(rows, (list, tuple)):
            rows = []

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        activity_id = row.get("activity_id")
        if not activity_id:
            continue
        info = {}
        if row.get("activity_name") is None or row.get("unit") is None:
            # 字段缺失时用活动字典补齐，尽量不让下游拿到空名称
            info = kb.activity_info(activity_id) or {}
        out.append({
            "activity_id": activity_id,
            "activity_name": _as_text(row.get("activity_name")
                                      if row.get("activity_name") is not None
                                      else info.get("activity_name")),
            "unit": _as_text(row.get("unit") if row.get("unit") is not None
                             else info.get("unit")),
            # 无结构过滤（未识别结构 / 建筑类型未知）时不带适用等级
            "applicability_level": (None if absent
                                    else _normalize_level(row.get("applicability_level")) or None),
            "production_mode": _as_text(row.get("recommended_production_mode")),
            "labor_type": _labor_type_of(activity_id),
            "structure_mapping_absent": bool(absent),
        })

    # 该 L3 在本结构形式下被剔除的条数 = 全量 - 保留（只统计真实存在的 L4）
    excluded_n = 0
    if structure_type_id and not building_unknown and not absent:
        kept_ids = set(x["activity_id"] for x in out)
        excluded_n = sum(1 for a in full
                         if isinstance(a, dict) and a.get("activity_id")
                         and a["activity_id"] not in kept_ids)
    return out, bool(absent), excluded_n, check_standalone_activities(out)


# ======================================================================
# A5：L4「该不该单独成工序」通用校验（**与档位无关**）
# ======================================================================
# 两条判据（总清单 A5）：
#   R1 有独立工序形态 —— "XX运输" 这类**资源动作**没有可注入的工序形态 → 不成工序；
#   R2 消耗未包含在其他工序定额里 —— 已被别的工序定额包含，单独列 = **重复计量**。
#
# 判据数据的权威来源是 KB（建议列见 `STANDALONE_FIELD_DDL`），由 P1 落库本模块只读；
# **列不存在时自动跳过并留痕**（下面的 `rule_data_absent` 记录），保证 kb.db 未加字段也不崩。
# R1 另有一条内置名称兜底（`_R1_NAME_RE`），因为 R1 的判据本身就只有工序名形态这一个信息来源；
# 兜底命中会在留痕里标 `source="builtin_r1_name_pattern"`，绝不会冒充 KB 判据。
STANDALONE_RULE_R1 = "R1"
STANDALONE_RULE_R2 = "R2"
#: 判定结论：不该单独成工序
STANDALONE_VERDICT = "not_standalone"

#: 内置 R1 名称判据：整名以资源动作收尾（"刨花板运输""多合土自卸运输""管桩搬运"）
_R1_NAME_RE = re.compile(r"(?:运输|搬运|转运|倒运|装卸)$")

#: 交 P1 的落库建议（本代理**不改 kb.db**，只把 DDL 写在这里 + 报告）：
#:
#:   ALTER TABLE L4_Activity_Dictionary ADD COLUMN is_standalone_activity INTEGER;
#:       -- 1 = 可单独成工序；0 = 不该单独成工序；NULL = 未判定
#:   ALTER TABLE L4_Activity_Dictionary ADD COLUMN standalone_rule TEXT;
#:       -- 'R1' / 'R2'（仅在 is_standalone_activity = 0 时有意义）
#:   ALTER TABLE L4_Activity_Dictionary ADD COLUMN standalone_note TEXT;
#:       -- 原因文案（如"该 L4 为资源动作，无独立工序形态"）
#:   或者新表：
#:   CREATE TABLE L4_Standalone_Rule (
#:       activity_id TEXT PRIMARY KEY,
#:       is_standalone_activity INTEGER NOT NULL,
#:       standalone_rule TEXT, standalone_note TEXT,
#:       source_code TEXT, source_type TEXT, confidence TEXT, review_state TEXT, notes TEXT);
STANDALONE_FIELD_DDL = (
    "ALTER TABLE L4_Activity_Dictionary ADD COLUMN is_standalone_activity INTEGER; "
    "ALTER TABLE L4_Activity_Dictionary ADD COLUMN standalone_rule TEXT; "
    "ALTER TABLE L4_Activity_Dictionary ADD COLUMN standalone_note TEXT;"
)

_RULE_LABEL = {
    STANDALONE_RULE_R1: "R1 有独立工序形态（资源动作，如「XX运输」）",
    STANDALONE_RULE_R2: "R2 消耗已包含在其他工序定额里（单独列 = 重复计量）",
}


def _standalone_cols():
    """A5 判据的三列在 `L4_Activity_Dictionary` 上是否已落库。"""
    out = {}
    for key, col in (("verdict", "is_standalone_activity"),
                     ("rule", "standalone_rule"),
                     ("note", "standalone_note")):
        try:
            out[key] = bool(kb.has_column("L4_Activity_Dictionary", col))
        except Exception:  # noqa: BLE001 — 查不到列 ≠ 崩，按"未落库"处理
            out[key] = False
    return out


def _standalone_db_rows(cols):
    """读 A5 判据列 → `{activity_id: {...}}`；列不存在返回 None（= 跳过校验）。"""
    if not (cols.get("verdict") or cols.get("rule")):
        return None
    sel = ["activity_id"]
    sel.append("is_standalone_activity" if cols.get("verdict") else "NULL")
    sel.append("standalone_rule" if cols.get("rule") else "NULL")
    sel.append("standalone_note" if cols.get("note") else "NULL")
    try:
        rows = kb._query_all(  # noqa: SLF001 — 只读查询，kb 内部函数不抛异常
            "SELECT %s FROM L4_Activity_Dictionary" % ", ".join(sel))
    except Exception:  # noqa: BLE001 — 读不到就按"未落库"跳过
        return None
    out = {}
    for r in rows or []:
        if r and r[0]:
            out[str(r[0])] = {"is_standalone_activity": r[1], "standalone_rule": r[2],
                              "standalone_note": r[3]}
    return out


def _standalone_record(activity_id, activity_name, work_type_id, rule, verdict,
                       reason, source, code=""):
    """一条 A5 留痕（结构固定，便于交付物/审计直接渲染）。"""
    return {
        "activity_id": _as_text(activity_id),
        "activity_name": _as_text(activity_name),
        "work_type_id": _as_text(work_type_id),
        "rule": _as_text(rule),
        "rule_label": _RULE_LABEL.get(_as_text(rule), _as_text(rule)),
        "verdict": _as_text(verdict),
        "reason": _as_text(reason),
        "source": _as_text(source),
        "code": _as_text(code),
    }


def check_standalone_activities(candidates, use_builtin_r1=True):
    """A5 通用校验：逐条判断某 L4「该不该单独成工序」。**与档位无关**，只产出留痕。

    参数
    ----
    candidates : `{work_type_id: [L4 dict, …]}`（`kb_scope` 的 `l4_candidates`）
    use_builtin_r1 : KB 判据列未落库时，是否用内置 R1 名称判据兜底（默认 True）

    返回
    ----
    {
      "checked": bool,        # 是否至少跑成了一条判据
      "fields_present": bool, # KB 判据列是否已落库
      "r1_checked": bool, "r2_checked": bool,
      "judged": int,          # 被判"不该单独成工序"的条数
      "audit": [ {activity_id, activity_name, work_type_id, rule, rule_label,
                  verdict, reason, source, code} ],
    }

    绝不抛异常：KB 读不到就当"未落库"，只留一条跳过留痕。
    """
    cols = _standalone_cols()
    fields_present = bool(cols.get("verdict") or cols.get("rule"))
    audit = []
    r1_checked = fields_present
    r2_checked = fields_present and bool(cols.get("verdict") or cols.get("rule"))

    if not fields_present:
        # 判据数据未落库 → **跳过校验并留痕**（绝不静默，也绝不因为缺列而崩）
        audit.append(_standalone_record(
            "", "", "", "", "", "L4_Activity_Dictionary 上没有 is_standalone_activity / "
            "standalone_rule 列，R1/R2 判据数据未落库 —— 本次跳过 KB 判据"
            + ("，改用内置 R1 名称判据兜底" if use_builtin_r1 else "（内置 R1 兜底也已关闭）"),
            "kb_schema", code="rule_data_absent"))

    rows = _standalone_db_rows(cols)
    # `candidates` 既接受 `{work_type_id: [L4, …]}`（本模块的 `l4_candidates`），
    # 也接受单个 L4 列表（`_candidates_for` 就是按列表调用的）。
    groups = (candidates if isinstance(candidates, dict)
              else {"": list(candidates or [])})
    for wt in groups:
        for it in groups.get(wt) or []:
            if not isinstance(it, dict):
                continue
            aid = _as_text(it.get("activity_id"))
            name = _as_text(it.get("activity_name"))
            db = (rows or {}).get(aid) or {}
            rule = _as_text(db.get("standalone_rule")).strip().upper()
            note = _as_text(db.get("standalone_note"))
            raw_verdict = db.get("is_standalone_activity")

            # ① KB 三态值优先（0 = 不该单独成工序）
            if raw_verdict is not None and raw_verdict != "":
                try:
                    not_standalone = int(raw_verdict) == 0
                except (TypeError, ValueError):
                    not_standalone = False
                if not_standalone:
                    r = rule if rule in (STANDALONE_RULE_R1, STANDALONE_RULE_R2) else STANDALONE_RULE_R1
                    audit.append(_standalone_record(
                        aid, name, wt, r, STANDALONE_VERDICT,
                        note or "KB 判据：该 L4 不应单独成工序", "kb_column"))
                continue

            # ② 只给了判据编号、没给三态值 → 视为"不该单独成工序"
            if rule in (STANDALONE_RULE_R1, STANDALONE_RULE_R2):
                audit.append(_standalone_record(
                    aid, name, wt, rule, STANDALONE_VERDICT,
                    note or "KB 判据编号：%s" % _RULE_LABEL.get(rule, rule), "kb_column"))
                continue

            # ③ 内置 R1 名称兜底（只判 R1；R2 需要定额包含关系数据，无数据不猜）
            if use_builtin_r1 and _R1_NAME_RE.search(name or ""):
                audit.append(_standalone_record(
                    aid, name, wt, STANDALONE_RULE_R1, STANDALONE_VERDICT,
                    "工序名「%s」是资源动作（无独立工序形态），按 R1 判为不该单独成工序" % name,
                    "builtin_r1_name_pattern"))

    judged = sum(1 for a in audit if a["verdict"] == STANDALONE_VERDICT)
    return {
        "checked": bool(r1_checked or r2_checked or judged),
        "fields_present": fields_present,
        "r1_checked": bool(r1_checked or use_builtin_r1),
        "r2_checked": bool(r2_checked),
        "judged": judged,
        "audit": audit,
    }


def _quantity_strengthened_l3(params):
    """A6 · L3 层：用户给出分项工程量 → 对应 L3 的档位**强化为"必须"**（REQUIRED）。

    映射依据（总清单 A6 + `prompts/extract_params.txt` 的字段口径）：
      · `total_concrete`  → `concrete`（混凝土工程）
      · `total_rebar`     → `rebar`（钢筋工程）
      · `total_earthwork` → `earthwork`（土石石方工程）
      · `total_masonry`   → `masonry`（砌筑工程；用户口径 m³，与砌体体积类 L4 的 m³ 一致）
      · `total_formwork`  → `formwork`（模板工程；用户口径 m²，与模板类 L4 的 m² 一致）
      · `total_pile`      → `pile_foundation`（桩基）
        【第 2 批 · 域 2 / 2.4】**临时接续**：这一项原来指向本批已删除的那个
        "地连墙/咬合桩/搅拌桩总量"键（键名见交付报告；此处刻意不写键名，免得 grep 残留），
        改指向新键 `total_pile`（桩，**不预设单位**）。
        ⚠️ 与 `ratio_scope.GROUP_TOTAL_PARAMS` 必须保持**逐字一致**（该模块的文件头
        明确要求两处口径同源）—— 所以那边同步改了同一项。
        ⚠️ 第 2 批临时接续，**域 6（桩基=基础）会重做这里**：桩型口径、单位换算
        与 `Component_Ratio` 的 `pile_foundation` 分组都在域 6 一并收口。
        注：`total_pile` 的"量 0 出局"判据只看 >0，与单位无关，所以本批不会误杀。

    ⚠️ `total_masonry`（m³）只对 `masonry` 下的**体积类** L4 口径一致（41/55 条是 m³）；
    另有 9 条是 m²（勾缝 / 砖砌地胎膜 / ALC 墙板 / 阳台栏板 / 混凝土花饰块组砌），
    量纲不同，不能拿砌体总量直接填 —— 见交付报告里的换算清单。

    ⚠️ 本批新增的 `total_infill_wall`（填充墙，m³）**不进这张表**：它的 L3 归属
    （砌筑工程？还是单独一档）与 `Component_Ratio` 分组要等域 6 定；本批只做
    "登记 + 校验 + 通路"，不预设它的 L3 映射（见交付报告"遗留/存疑"）。

    返回 `{work_type_id: (来源键, 值)}`；用户没给的键不出现。
    """
    out = {}
    for key, wt in (("total_concrete", "concrete"), ("total_rebar", "rebar"),
                    ("total_earthwork", "earthwork"), ("total_pile", "pile_foundation"),
                    ("total_masonry", "masonry"), ("total_formwork", "formwork")):
        v = (params or {}).get(key)
        if v is None or v == "" or isinstance(v, bool):
            continue
        try:
            if float(v) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        out.setdefault(wt, (key, v))
    return out


def _user_exclusion_hits(items, work_type_id, activity_name):
    """返回命中该 L3 / 该 L4 的**硬闸门**排除项（`scope=global` 且不需人工确认）。

    命中 L3：`l3_candidates` 含该 work_type_id；
    命中 L4：`activity_keywords` 里有词出现在 `activity_name` 里。
    """
    hit_l3, hit_l4 = [], []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if work_type_id and work_type_id in (it.get("l3_candidates") or []):
            hit_l3.append(it)
            continue
        name = activity_name or ""
        if name and any(k and k in name for k in (it.get("activity_keywords") or [])):
            hit_l4.append(it)
    return hit_l3, hit_l4


def _zero_quantity_activities(params):
    """A6 · L4 层：上游给的 per-L4 量 → `{activity_id: 量}`（只取"能量出数"的键）。

    上游接口（契约，见报告"数据结构定义"）：`extracted_params["l4_quantities"]`，接受两种形状
      · `{"ACT_ID": 0, …}`                       —— 直接按活动编号给量；
      · `{"work_type_id": {"ACT_ID": 0, …}, …}`  —— 按 L3 分组给量。
    没给（键不存在 / 空）→ 返回 `None`，**不做量=0 过滤**（保持现状，不猜）。

    ⚠️ **生产者（2026-09-21 W5-B345）**：`pipeline/ratio_scope.py:build()`，由
    `KBScopeNode.run` 在「②占比表」这一跳调用，把
    `params["total_<工种>"] × Component_Ratio[(结构类型, L4)] ÷ 100` 写进本键。
    在本次接线之前，`grep l4_quantities` 只有 1 实现 + 2 测试 + 1 docstring，
    **全仓无生产者** ⇒ 生产路径上"量0出局"永不触发。
    """
    raw = (params or {}).get("l4_quantities")
    if not isinstance(raw, dict) or not raw:
        return None
    flat = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            for aid, q in v.items():
                flat[_as_text(aid)] = q
        else:
            flat[_as_text(k)] = v
    return flat or None


def _is_zero(q):
    """量是不是 0（"量 = 0 的 L4 直接不进树"）。非数 / 空 → 不算 0。

    ⚠️ **必须容差判定**（B3/B4/B5 接线，2026-09-21）：改为 `abs(q) <= QTY_ZERO_TOL`。
    改前是 `float(q) == 0.0`（**精确零**），而量来自 `总量 × 占比/100` 的浮点乘法 ——
    精确命中 0.0 只发生在占比恰好为 0 时，浮点残差（如 1e-13）会让"量0出局"整体失效。
    容差实现与常量见 `pipeline/ratio_scope.py:QTY_ZERO_TOL`（= 1e-6，理由写在那里：
    比最小真实量低 4 个数量级、比浮点噪声高 6 个数量级）。负数仍然不算 0（保持原语义）。
    """
    if q is None or q == "" or isinstance(q, bool):
        return False
    try:
        v = float(q)
    except (TypeError, ValueError):
        return False
    from ..ratio_scope import QTY_ZERO_TOL      # 惰性：破包级循环导入
    return abs(v) <= QTY_ZERO_TOL


def _ratio_warnings(ratio_info):
    """把占比表拆分的**降级与违规**翻成人话警告（缺失绝不静默）。

    只报"用户需要知道"的三类：① V1–V4 硬违规；② 量 0 出局的条数；③ 降级条数。
    逐条明细在 `scope["component_ratio"]` 留痕里（不在这里刷屏）。
    """
    out = []
    trace = (ratio_info or {}).get("trace") or {}
    check = trace.get("v1_v4") or {}
    hard = [v for v in (check.get("violations") or [])
            if v.get("severity") == "error"]
    if hard:
        out.append("Component_Ratio 占比表 V1–V4 有 %d 条硬违规（示例：%s）—— 明细见留痕 "
                   "kb_scope.component_ratio.v1_v4" % (len(hard), hard[0].get("code")))
    n_zero = len(trace.get("excluded_zero_quantity") or [])
    if n_zero:
        out.append("占比表拆分后有 %d 条 L4 量为 0（缺行或占比≈0），按「量0出局」不进树" % n_zero)
    degs = trace.get("degradations") or []
    if degs:
        out.append("占比表拆分有 %d 处降级（已逐条标注，未静默、未回退旧阶段比例表）" % len(degs))
    return out


def _foundation_warnings(ratio_info):
    """【域 6｜桩基 = 基础】把「基础类型认不出 / 桩型认不出」翻成人话警告。

    为什么必须走**警告通道**（而不是只留在结构化留痕里）：域 6 的全部判断都挂在
    `foundation_type` 这个自由文本上。认不出时若只写留痕，用户看到的就是
    「基础的量和别的项目一样」，**没有任何提示说明它没按本项目的基础形式编** ——
    这正是本仓库历史上「标注了但没人看见」那类缺陷。判据与文案在
    `ratio_scope.foundation_binding_warnings()`（**唯一真源**，本节点只转发）。
    """
    try:
        from .. import ratio_scope as _RS          # 惰性：破包级循环导入
        return list(_RS.foundation_binding_warnings(ratio_info))
    except Exception:  # noqa: BLE001 — 警告转发失败绝不影响节点主流程
        return []


def _split_l3(l3_rows, building_unknown, structure_type_id, params=None):
    """把 L3 清单拆成 `(选定 L3, 排除 L3, l4_candidates, 结构剔除条数, warnings, audit)`。

    规则：REQUIRED / OPTIONAL / 未知等级 → 收下；EXCLUDED → 排除。
    历史档位 `USUAL` 由 `normalize_level` 归并到 `OPTIONAL`（A1 三档化）。
    这里**不做**整工种降级：某 L3 的 L4 被结构映射清空也仍然保留该 L3。

    A4 的 "L3 被排除 → 其下 L4 一律不进" 就是这里的**与逻辑**：
      L4 留下 ⟺ 建筑类型没排除该 L3（本函数 continue）**且** 结构类型没剔除该 L4
      （`_candidates_for` → `kb.structure_l4_filtered`）。任一命中即出局。
      每次整棵子树被剔除都在 `audit["excluded_subtrees"]` 留痕
      （来源 = 建筑类型 / 结构类型 / 用户明确排除）。

    `audit` 键：
      · `excluded_subtrees`      —— A4 留痕：`{L3, 排除来源, 原因, 被一并剔除的 L4 条数}`
      · `l3_strengthened`        —— A6：因用户给了量而强化为 REQUIRED 的 L3
      · `l4_excluded_by_quantity`—— A6：量 = 0 被剔除的 L4
      · `user_exclusions_applied`—— A6：进了硬闸门的用户明确排除项
      · `user_exclusions_pending`—— A6：局部 / 定位不了 / 认不出的（转待确认，不进闸门）
    """
    selected = []
    excluded = []
    candidates = {}
    excluded_by_structure = 0
    warnings = []
    audit = {"excluded_subtrees": [], "l3_strengthened": [],
             "l4_excluded_by_quantity": [], "user_exclusions_applied": [],
             "user_exclusions_pending": [], "standalone": []}

    # ---- A6：用户明确排除项（只有硬闸门那部分参与筛选）----
    all_exclusions = (params or {}).get("exclusions") or []
    if isinstance(all_exclusions, dict):
        all_exclusions = all_exclusions.get("items") or []
    gate_items = hard_gate_exclusions(all_exclusions)
    for it in all_exclusions:
        if isinstance(it, dict) and it not in gate_items:
            audit["user_exclusions_pending"].append({
                "canonical": _as_text(it.get("canonical")),
                "term": _as_text(it.get("term")),
                "text": _as_text(it.get("text")),
                "scope": _as_text(it.get("scope")) or "unknown",
                "local_hint": _as_text(it.get("local_hint")),
                "reason": _as_text(it.get("confirm_reason")) or "转人工确认，不进硬闸门",
            })

    strengthen = _quantity_strengthened_l3(params)
    zero_qty = _zero_quantity_activities(params)

    for row in l3_rows or []:
        if not isinstance(row, dict):
            continue
        work_type_id = row.get("work_type_id")
        if not work_type_id:
            continue
        level = _normalize_level(row.get("applicability_level"))

        # ---- ① 建筑类型排除（A4：整棵子树剔除 + 留痕）----
        if level == "EXCLUDED":
            reason = _as_text(row.get("notes")) or "该建筑类型下不适用"
            excluded.append({
                "work_type_id": work_type_id,
                "work_type_name": _as_text(row.get("work_type_name")),
                "reason": reason,
                "source": "building_type",
            })
            audit["excluded_subtrees"].append({
                "work_type_id": work_type_id,
                "work_type_name": _as_text(row.get("work_type_name")),
                "source": "building_type",
                "source_label": "建筑类型",
                "level": level,
                "reason": reason,
                "l4_count": len(kb.l4_for(work_type_id) or []),
            })
            continue

        # ---- ② 用户明确排除（全局，硬闸门）：L3 整棵子树剔除 + 留痕 ----
        gate_l3, _ = _user_exclusion_hits(gate_items, work_type_id, "")
        if gate_l3:
            texts = "；".join(_as_text(it.get("text")) for it in gate_l3)
            excluded.append({
                "work_type_id": work_type_id,
                "work_type_name": _as_text(row.get("work_type_name")),
                "reason": "用户明确排除：%s" % texts,
                "source": "user_exclusion",
            })
            audit["excluded_subtrees"].append({
                "work_type_id": work_type_id,
                "work_type_name": _as_text(row.get("work_type_name")),
                "source": "user_exclusion",
                "source_label": "用户明确排除",
                "level": level,
                "reason": "用户明确排除：%s" % texts,
                "l4_count": len(kb.l4_for(work_type_id) or []),
            })
            audit["user_exclusions_applied"].append({
                "canonical": _as_text(gate_l3[0].get("canonical")),
                "text": texts, "target": work_type_id,
                "target_name": _as_text(row.get("work_type_name")),
                "level": "L3",
            })
            continue

        if building_unknown:
            level = _LEVEL_UNKNOWN
        elif level not in _KEEP_LEVELS:
            # 理论不该出现的等级：保留但标 UNKNOWN，避免静默丢工种
            level = _LEVEL_UNKNOWN

        # ---- A6：用户给了量 → 该 L3 档位强化为 REQUIRED ----
        if work_type_id in strengthen:
            src_key, src_val = strengthen[work_type_id]
            if level != "REQUIRED":
                audit["l3_strengthened"].append({
                    "work_type_id": work_type_id,
                    "work_type_name": _as_text(row.get("work_type_name")),
                    "from": level or "UNKNOWN",
                    "to": "REQUIRED",
                    "reason": "用户给出了 %s = %s，该工种强化为「必须」" % (src_key, src_val),
                    "param": src_key,
                    "value": src_val,
                })
            level = "REQUIRED"

        selected.append({
            "work_type_id": work_type_id,
            "work_type_name": _as_text(row.get("work_type_name")),
            "level": level,
            "note": _as_text(row.get("notes")),
        })

        l4_list, mapping_absent, excluded_n, standalone = _candidates_for(
            work_type_id, building_unknown, structure_type_id)
        excluded_by_structure += excluded_n
        if mapping_absent and structure_type_id and not building_unknown:
            warnings.append("{}（{}）：{}".format(
                _as_text(row.get("work_type_name")) or work_type_id,
                work_type_id, _WARN_MAPPING_ABSENT))

        # ---- A6：用户全局排除 → 按 L4 名称关键词剔除（硬闸门）----
        kept = []
        for it in l4_list:
            _, hit_l4 = _user_exclusion_hits(gate_items, work_type_id, it.get("activity_name"))
            if hit_l4:
                audit["user_exclusions_applied"].append({
                    "canonical": _as_text(hit_l4[0].get("canonical")),
                    "text": "；".join(_as_text(x.get("text")) for x in hit_l4),
                    "target": it.get("activity_id"),
                    "target_name": _as_text(it.get("activity_name")),
                    "level": "L4",
                })
                continue
            # ---- A6：量 = 0 的 L4 直接不进树 ----
            # 生产者（2026-09-21 W5-B345）：`KBScopeNode.run` 里的 ratio_scope.build()
            # —— 占比表拆分后写 `params["l4_quantities"]`。此前该键全仓无生产者，
            # 本分支在生产路径上永不触发（只被 2 个测试覆盖）。
            if zero_qty is not None and _is_zero(zero_qty.get(_as_text(it.get("activity_id")))):
                _q = zero_qty.get(_as_text(it.get("activity_id")))
                audit["l4_excluded_by_quantity"].append({
                    "activity_id": _as_text(it.get("activity_id")),
                    "activity_name": _as_text(it.get("activity_name")),
                    "work_type_id": work_type_id,
                    "quantity": _q,
                    "reason": "按占比表（Component_Ratio）拆分后该工序量 = %r，不进树" % (_q,),
                })
                continue
            kept.append(it)
        candidates[work_type_id] = kept
        audit["standalone"].append(standalone)

        # ---- A4：整棵 L4 子树被结构形式剔空 → 也留痕（L3 本身按设计保留）----
        # ⚠️ 判据必须看 `l4_list` 为空（`_candidates_for` 返回的已经是结构过滤后的结果），
        # 不能看 `kept` 为空 —— 后者还可能是用户排除 / 量=0 造成的。
        full_n = len(kb.l4_for(work_type_id) or [])
        if (full_n and not l4_list and structure_type_id
                and not building_unknown and not mapping_absent):
            audit["excluded_subtrees"].append({
                "work_type_id": work_type_id,
                "work_type_name": _as_text(row.get("work_type_name")),
                "source": "structure_type",
                "source_label": "结构类型",
                "level": level,
                "reason": "%s 下该工种的 %d 条 L4 全部被结构形式剔除，整棵子树不进树"
                          % (_as_text(structure_type_id), full_n),
                "l4_count": full_n,
            })

    return selected, excluded, candidates, excluded_by_structure, warnings, audit


class KBScopeNode(BaseNode):
    """知识库范围装配：建筑类型 + 结构形式 → L3 清单 + 各 L3 的合法 L4 清单。"""

    name = "kb_scope"
    title = "知识库范围装配"
    # 引擎读这个键：把 ctx["kb_warnings"] 随 node_done 一起上行，供终端逐条渲染。
    # （详见 engine.py 里"节点声明的警告上行"那段注释。）
    warning_ctx_key = "kb_warnings"
    # 引擎读这个：node_done 里追加的"…其余 N 条同类"一行；run() 里按实际警告算出。
    warning_note = ""

    def run(self, ctx):
        # 进度：开始
        self.emit("node_progress", {"node": self.name, "progress": 10,
                                    "message": "读取建筑类型与结构形式…"})

        params = (ctx or {}).get("extracted_params") or {}
        building_text = params.get("building_type") or ""
        structure_text = params.get("structure_type") or ""

        warnings = []

        # ---- ① 解析建筑类型 ----
        building = None
        try:
            building = kb.resolve_building_type(building_text)
        except Exception:  # noqa: BLE001 — kb 本身不抛，这里只是双保险
            building = None
        building_unknown = not building
        if building_unknown:
            building_id, building_name = "", ""
            warnings.append(_WARN_BUILDING)
        else:
            building_id, building_name = _as_text(building[0]), _as_text(building[1])

        self.emit("node_progress", {"node": self.name, "progress": 30,
                                    "message": "建筑类型：{}".format(building_name or "未识别")})

        # ---- ② 解析结构形式 ----
        structure = None
        try:
            structure = kb.resolve_structure_type(structure_text)
        except Exception:  # noqa: BLE001
            structure = None
        structure_unknown = not structure
        if structure_unknown:
            structure_id, structure_name = "", ""
            warnings.append(_WARN_STRUCTURE)
        else:
            structure_id, structure_name = _as_text(structure[0]), _as_text(structure[1])

        self.emit("node_progress", {"node": self.name, "progress": 40,
                                    "message": "结构形式：{}".format(structure_name or "未识别")})

        # ---- ② B3/B4/B5：把工种总量按 Component_Ratio 拆到各 L4（**唯一真源**）----
        # B5 的四步顺序 = ①结构映射 → ②占比表 → ③量0出局 → ④生成 WBS。
        # ①已在上面解析（structure_id），③在 _split_l3 里（本文件 :685 附近），
        # ④在 wbs_agent / beat_build；这里补的是**缺掉的②这一跳**。
        # 产物直接写回 `ctx["extracted_params"]`（同一 dict 对象），于是下游
        # beat_build → layer_engine.expand_node 能读到同一份占比表拆分结果。
        # 旧的按施工阶段比例表（beat_configs.CONCRETE_RATIO / REBAR_RATIO）不参与、不回退。
        ratio_trace = None
        try:
            from .. import ratio_scope as _RS          # 惰性：破包级循环导入
            _ratio = _RS.build(params, structure_id)
            # 上游若已经给了 `l4_quantities`（A6 的既有接口，两种形状都认），
            # **上游的值优先**（那是显式给出的 per-L4 量），占比表结果只做补充；
            # 与占比表都没结果时**不写这个键**（`_zero_quantity_activities` 返回 None，
            # 不做量0过滤 —— 保持既有"没给就不猜"的语义）。
            _merged = dict(_ratio["l4_quantities"])
            _up = params.get("l4_quantities")
            if isinstance(_up, dict):
                for _k, _v in _up.items():
                    if isinstance(_v, dict):
                        for _aid, _q in _v.items():
                            _merged[_as_text(_aid)] = _q
                    else:
                        _merged[_as_text(_k)] = _v
            if _merged:
                params["l4_quantities"] = _merged
            elif "l4_quantities" in params:
                params.pop("l4_quantities", None)
            params[_RS.RATIO_CTX_KEY] = {
                "structure_type_id": structure_id,
                "l4_index": _ratio["index"],
                "trace": _ratio["trace"],
            }
            if structure_id:
                params[_RS.STRUCTURE_ID_KEY] = structure_id
            ratio_trace = _ratio["trace"]
            # 【域 6｜桩基 = 基础】基础类型绑定：把「认不出基础形式 / 认不出桩型」
            # 送进**警告通道**（用户在终端与交付物里都看得见）。
            # 为什么在这里、而不是在 ratio_scope 内部 append 到 warnings：
            # `_split_l3` 还没跑，warnings 列表归本节点管；且这两条警告的语义是
            # 「范围/口径提醒」，与 `_WARN_STRUCTURE` 同类 —— 同一条通道才不会被漏看。
            warnings.extend(_foundation_warnings(_ratio))
            # 【域 6 · 6.4 收口】桩基量已在 `_ratio` 里改投完毕 ⇒ `total_pile`
            # **不再**参与占比表对账（`GROUP_TOTAL_PARAMS` 已移除 `pile_foundation`），
            # 该键由 `ratio_scope._pile_total_of` 在改投逻辑里直接消费；改投明细见
            # `scope["component_ratio"]["foundation_binding"]`，不再另加一句警告刷屏。
            # ⚠️ 占比表的降级/违规**不进 `warnings` 通道**：
            # 该通道的语义是「结构映射 / 建筑类型」类范围警告，带归并摘要并被用户
            # 直接看到；把占比表的数据质量结论混进去会改变既有摘要口径（并发代理的
            # 用例钉着它）。留痕走**结构化字段** `scope["component_ratio"]`
            # （含 v1_v4 / excluded_zero_quantity / exempt / degradations），
            # 逐条可查、绝不静默，且不与既有警告通道抢语义。
        except Exception as exc:  # noqa: BLE001 — 占比表异常绝不阻断主链路，但必须留痕
            warnings.append("Component_Ratio 占比表拆分失败（已跳过，未回退旧阶段比例表）：%r"
                            % (exc,))

        # ---- ③ 取 L3 清单 ----
        # 建筑类型未识别时退回"全部 L3"（31 个，等级 UNKNOWN）；
        # 各 L3 的 L4 走 kb.l4_for 全量，不做结构过滤（见 _candidates_for 的情况 B）。
        if building_unknown:
            l3_rows = self._all_l3_rows()
        else:
            try:
                l3_rows = kb.l3_for(building_id) or []
            except Exception:  # noqa: BLE001
                l3_rows = []
            if not l3_rows:
                warnings.append("知识库里没有「{}」（{}）对应的工序大类，"
                                "已放宽到全部工序大类。".format(
                                    building_name or building_id, building_id))
                building_unknown = True
                l3_rows = self._all_l3_rows()

        self.emit("node_progress", {"node": self.name, "progress": 55,
                                    "message": "知识库里查到 {} 个可选的工序大类".format(
                                        len(l3_rows))})

        # ---- ④ 装配 L3 / L4 ----
        selected, excluded, candidates, excl_by_structure, split_warnings, split_audit = _split_l3(
            l3_rows, building_unknown, structure_id, params)
        warnings.extend(split_warnings)

        l4_total = sum(len(v) for v in candidates.values())
        stats = {
            "l3_total": len(l3_rows),
            "l3_selected": len(selected),
            "l4_selected": l4_total,
            "l4_excluded_by_structure": excl_by_structure,
            "l4_excluded_by_quantity": len(split_audit["l4_excluded_by_quantity"]),
            "l3_strengthened": len(split_audit["l3_strengthened"]),
            "excluded_subtrees": len(split_audit["excluded_subtrees"]),
        }
        # A5：把各 L3 的工序化判据留痕合成一份（去重后进 scope）
        # `check_standalone_activities` 是**按 L3 逐个**调用的，所以"判据数据未落库"
        # 这类全局留痕会出现 N 次 —— 这里按 (code, 判据编号, 活动编号, 结论) 去重，
        # 只保留每个 L4 一条 + 一条全局跳过说明。
        merged_standalone, _seen_sa = [], set()
        for _x in split_audit["standalone"]:
            for _rec in (_x.get("audit") or []):
                _k = (_rec.get("code"), _rec.get("rule"), _rec.get("activity_id"),
                      _rec.get("verdict"))
                if _k in _seen_sa:
                    continue
                _seen_sa.add(_k)
                merged_standalone.append(_rec)
        standalone_audit = {
            "checked": any(bool(x.get("checked")) for x in split_audit["standalone"]),
            "fields_present": any(bool(x.get("fields_present")) for x in split_audit["standalone"]),
            "r1_checked": any(bool(x.get("r1_checked")) for x in split_audit["standalone"]),
            "r2_checked": any(bool(x.get("r2_checked")) for x in split_audit["standalone"]),
            "judged": sum(1 for r in merged_standalone
                          if r.get("verdict") == STANDALONE_VERDICT),
            "audit": merged_standalone,
        }

        self.emit("node_progress", {"node": self.name, "progress": 90,
                                    "message": "按建筑类型与结构形式筛出 {} 个工序、{} 条可选项".format(
                                        len(selected), l4_total)})

        scope = {
            "building_type": building_id,
            "building_type_name": building_name,
            "structure_type": structure_id,
            "structure_type_name": structure_name,
            "l3_list": selected,
            "excluded_l3": excluded,
            "l4_candidates": candidates,
            "stats": stats,
            "warnings": warnings,
            # ---- 留痕（绝不静默）----
            # A4：每次因 L3 被排除（建筑类型 / 结构类型 / 用户明确排除）而**整棵子树被剔除**，
            #     都在这里留一条 `{work_type_id, work_type_name, source, source_label,
            #     level, reason, l4_count}`；`kb_conformance` 把它纳进一致性校验输出。
            "excluded_subtrees": split_audit["excluded_subtrees"],
            # A6：用户给了量而被强化为"必须"的 L3
            "l3_strengthened": split_audit["l3_strengthened"],
            # A6：按用户给的工程量分解，量 = 0 被剔除的 L4
            "l4_excluded_by_quantity": split_audit["l4_excluded_by_quantity"],
            # A6：用户明确排除项 —— 进了硬闸门的 / 转待确认的（局部、定位不了、认不出）
            "user_exclusions_applied": split_audit["user_exclusions_applied"],
            "user_exclusions_pending": split_audit["user_exclusions_pending"],
            # A5：L4「该不该单独成工序」通用校验留痕（与档位无关）
            "standalone_audit": standalone_audit,
            # ---- B3/B4/B5（2026-09-21）：占比表拆分留痕 ----
            # `Component_Ratio` 是「把工种总量拆到各构件」的**唯一真源**；这里记下
            # 结构类型、活跃工种、各组占比合计、量0出局的 L4、每一处降级，
            # 以及 component_ratio.check_ratio_v1_v4 的 V1–V4 结果（只读调用）。
            "component_ratio": ratio_trace,
        }

        # 警告**归并后**回显：只报数字等于把 27 条内容算完就丢（真实缺陷）。
        # done_summary 只放**归并后的那一句**（同类不重复），逐条原文走两条通道：
        #   ① ctx["kb_warnings"] → 引擎随 node_done 上行 → 终端打"前 3 条 + 其余同类"；
        #   ② plan_json.meta.kb_warnings → 交付物 / 修订链留档。
        digest = merge_warnings(warnings)
        self.done_summary = ("知识库范围装配完成：建筑类型 {}，结构形式 {}；"
                             "选定 L3 {} 个（排除 {} 个），可用 L4 {} 个，"
                             "结构过滤剔除 {} 个{}").format(
            building_name or "未识别",
            structure_name or "未识别",
            stats["l3_selected"], len(excluded), stats["l4_selected"],
            stats["l4_excluded_by_structure"],
            "；" + digest["summary"] if digest["total"] else "",
        )
        # 给引擎读：哪些 ctx 键里的警告要随 node_done 上行、以及"其余 N 条同类"那一行
        self.warning_note = digest["note"]

        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "知识库范围装配完成"})
        # `kb_warnings` 单独放一个 ctx 键（与上游 norm_bind 的 `norm_warnings` 同一约定）：
        # 下游节点 / plan_assembler / 终端渲染都不必钻进 kb_scope 的嵌套结构里找警告。
        return {"kb_scope": scope, "kb_warnings": list(warnings)}

    @staticmethod
    def _all_l3_rows():
        """建筑类型未识别时的 L3 全集（31 个），等级留空由 _split_l3 标 UNKNOWN。

        复用 kb.l3_for 的既有口径：任一已登记建筑类型的映射都覆盖全部 L3，
        这里取第一行 L4 的 L3 归属会漏工种，故直接读 L3 字典表；查询失败再退回
        常见建筑类型遍历。任何异常都返回可用的列表，不抛。
        """
        try:
            rows = kb._query_all(  # noqa: SLF001 — 只读查询，kb 内部函数不抛异常
                "SELECT work_type_id, work_type_name FROM L3_Work_Type ORDER BY work_type_id")
            out = [{"work_type_id": r[0], "work_type_name": r[1],
                    "applicability_level": None, "confidence": None, "notes": None}
                   for r in rows if r and r[0]]
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
        # 兜底：用任一建筑类型的映射清单（同库下覆盖一致）
        for bid in ("residential", "commercial", "industrial"):
            try:
                rows = kb.l3_for(bid) or []
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                return rows
        return []
