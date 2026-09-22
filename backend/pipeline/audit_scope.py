# -*- coding: utf-8 -*-
"""审计层：把三件"可疑却被静默使用"的数据事实摆到台面上，**不改任何值**。

背景（第 41 轮，均在本仓库真实数据上实测）：
  计划是由一串"看起来都对"的值拼出来的，其中有三处**无人复核**的口径分歧：

   1. **工作面人数上限被公式抬高**（`Workface_Capacity_Rule`，域 1.6 已删除）
      表里同时存着两代口径：`legacy_max_labor`（旧 v1 表的 `max_labor`）与 `crew_max`（v2 标定上限）。
      排程真正用的是 `scheduler.effective_crew_max()` = `max(crew_max, min(40,
      ceil(crew_base×2.5)))` —— 实测 385/387 行**再被抬高一次**（如 crew_base=8、
      crew_max=15 → 上限 20）。于是同一张表里三个数（旧上限 / 新上限 / 实际生效上限）
      并存，谁都没有被复核过（实测 487 行 `review_state='pending'`、置信度全 LOW）。
      本模块把它们**逐行列出来**，但一个值都不改。

   2. **同一个 source_code 在两次绑定里给出差 1.79 倍的值**（实测 `plan_sample3_after_fix`）
      `4.1.1.1`（钢筋绑扎，REBAR_NEW_FOUND）取 `LD_T72_7_2008` = 4.43 工日/t，
      `5.1.1.1`（钢筋绑扎，REBAR_NEW_SLAB）取**同一个** `LD_T72_7_2008` = 7.91 工日/t。
      定额来源相同、工序名相同、值差 1.79 倍 —— 工期直接按它算，却没有任何一处提示。

   3. **同一活动 + 同一楼层同时按面积和体积各排一条**（实测 18 组）
      `6.1.1.1 1-1层 ALC墙板安装` 1420 m² 与 `6.1.1.3 1-1层 砌块墙` 284 m³ 都绑
      `LDT724_砌块墙`、都在 `1-1层`。1420/284 = 0.2 m，正好是 200 mm 墙厚 ——
      物理上等价、排程上却是两条独立任务（工期各算一遍）。是不是重复计价，需要人看。

三个函数都是**纯函数**（`cmax_review_rows` 只读 KB，绝不写），返回 JSON 可序列化的
dict，可直接进 `plan.meta["scope_audit"]`（`schemas._Base` 是 `extra="allow"`，
不会被 `PlanJson.model_validate()` 丢掉）。

口径纪律（与 `kb.py` 的注释一致）：
  · 兼容键 `max_labor` 取 `legacy_max_labor`，**不是** `crew_max` 的别名；
  · `effective_crew_max` 直接调 `scheduler` 里那一个函数，**不另写一份公式**；
  · 认不出来的一律不猜：单位走 `kb_units.normalize_unit`，判不出的原样保留。
"""

from __future__ import annotations

import collections
import copy
import datetime
import os
import re
import sqlite3

try:                                  # 包内导入（pipeline.audit_scope）
    from . import kb_units
except ImportError:                   # 顶层导入（测试把 backend/ 塞进 sys.path）
    from pipeline import kb_units     # type: ignore

__all__ = [
    "CMAX_TABLE",
    "EFFECTIVE_CMAX_FLOOR",
    "DUPLICATE_THICKNESS_RANGE",
    "NORM_SPREAD_RATIO",
    "cmax_review_rows",
    "norm_row_spread",
    "duplicate_scope_groups",
    "leaf_tasks",
    "scope_audit",
    "scope_audit_summary",
    "clear_cache",
]

#: 合表后的**唯一**工作面容量表（域 1.6 已删除）。
CMAX_TABLE = "Workface_Capacity_Rule"

#: 上架门槛：某一档（crew_base/crew_max/legacy_max_labor/effective）至少这么多行才列出来，
#: 或者它本身就"够显眼"（legacy → effective 抬高 ≥ 5 人）。两条取或 —— 于是列出来的
#: 永远是**量大或量差大**的档，不是随机抽样。实测真实库：26 档全过阈值，覆盖 387 行。
CMAX_FLOOR_ROWS = 10
CMAX_FLOOR_LIFT = 5

#: `legacy_max_labor < crew_max` 才算"旧口径被新口径顶上去过"（实测 387/487 行）。
#: `legacy >= crew_max` 的行没有这处分歧，不进本清单。
EFFECTIVE_CMAX_FLOOR = 0.0

#: 重复范围判据：体积 ÷ 面积 落在这个厚度带内才算"物理上可能是同一批墙"。
#: 0.05 m（隔墙板）～0.6 m（剪力墙）—— 实测真实计划的 18 组全是 0.2 m。
DUPLICATE_THICKNESS_RANGE = (0.05, 0.6)

#: 定额离散判据：max/min > 1.5 才算"同一来源给出明显不同的值"。
#: 实测真实计划：LD_T72_7_2008 工日/t = 1.79（真信号）；
#: LD_T72_4_2008 工日/m³ = 1.11（被 1.5 挡掉，实际是"ALC 板 0.943 / 砌块 0.85"两个工序，
#: 不是同一个值的漂移）。
NORM_SPREAD_RATIO = 1.5

#: 楼层前缀：`1-1层` / `12层` / `3-4.5层` —— 去掉它才比得出"是不是同一道工序"。
_LAYER_PREFIX_RE = re.compile(r"^[\d\-.．]+\s*层\s*")
#: 名称切词用的标点（含中英文括号与顿号）。
_PUNCT_RE = re.compile(r"[\s()（）/·、,，.。:：;；\-—+]+")
#: 名称里的尺寸规格（`200mm` / `C30` / `Φ12`）—— 规格不同不算同一道工序。
_SPEC_RE = re.compile(r"[0-9０-９]")

_ZERO = 1e-9

#: 只读查询的结果缓存（`kb.py::_KB_CACHE` 的同一手法）。`build_parts` 与 `build_meta`
#: 在**同一份计划**上会各问一次；不缓存就是同一张 478 行的表读两遍。
#: 键带上 `(路径, mtime_ns, size)` —— 库被换掉/改过就不会命中旧结果。
#: **只缓存默认阈值下的调用**：非默认阈值是给 `tools/list_cmax_review.py` 的，
#: 缓存它既没用又会把缓存键复杂化。
_CMAX_CACHE = {}


def _cache_key(db_path):
    try:
        stat = os.stat(str(db_path))
        return (str(db_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _cache_get(key):
    if key is None:
        return None
    return _CMAX_CACHE.get(key)


def _cache_put(key, value):
    if key is not None:
        _CMAX_CACHE[key] = value


def clear_cache():
    """清空只读缓存（测试改库后调用；与 `kb.clear_cache()` 同名同义）。"""
    _CMAX_CACHE.clear()


# ---------------------------------------------------------------------------
# 小工具（不引入新依赖，全部容错）
# ---------------------------------------------------------------------------
def _num(value):
    """数值化：拿不到 / 非数 / NaN / inf → None（**不编数**，也不把 0 当"有值"）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _pos(value):
    """正数才返回，否则 None（用于比值/厚度这类必须为正的量）。"""
    f = _num(value)
    return f if (f is not None and f > _ZERO) else None


def _text(value):
    return "" if value is None else str(value).strip()


def _canon_unit(unit):
    """单位规范写法：`m2/㎡/m^2 → m²`，`m3/㎥ → m³`。空值 → ""，未知原样。

    ⚠️ 定额单位是**复合串**（`工日/m³`、`台班/根`），而 `kb_units.normalize_unit` 只认
    整串别名 —— `工日/M2` 整串查不到就会原样返回，分子分母的归一就丢了。所以先按 `/`
    拆开、逐段归一（与 `kb_units.parse_norm_unit` 同一个口径），再拼回去。
    """
    text = _text(unit).replace(" ", "").replace("／", "/")
    if not text:
        return ""
    try:
        if "/" in text:
            head, _, tail = text.partition("/")
            return "%s/%s" % (kb_units.normalize_unit(head),
                              kb_units.normalize_unit(tail))
        return kb_units.normalize_unit(text)
    except Exception:                                   # noqa: BLE001 — 审计层不许抛
        return text


def _unit_family(unit):
    """量纲族：area / volume / mass / length / count:xx / unknown。"""
    try:
        return kb_units.unit_family(unit)
    except Exception:                                   # noqa: BLE001
        return "unknown"


def _name_tokens(name):
    """把任务名切成"工序词"：去楼层前缀 → 按标点切 → 丢掉含数字的规格词。"""
    text = _LAYER_PREFIX_RE.sub("", _text(name))
    tokens = set()
    for tok in _PUNCT_RE.sub("|", text).split("|"):
        tok = tok.strip()
        if len(tok) < 2:
            continue
        if _SPEC_RE.search(tok):        # `200mm` / `C30` / `Φ12` 是规格，不是工序
            continue
        tokens.add(tok)
    return tokens


def leaf_tasks(node):
    """从 **wbs 树** 或 **整份 plan** 里取全部叶子（`sub_packages`）。非 dict 一律跳过。

    与 `plan_assembler._leaf_tasks` 同一个口径（phases → work_packages → sub_packages），
    只是额外容忍 plan 顶层（自动往下找 `wbs`）。
    """
    if not isinstance(node, dict):
        return []
    wbs = node.get("wbs") if isinstance(node.get("wbs"), dict) else node
    leaves = []
    for phase in (wbs.get("phases") or []):
        if not isinstance(phase, dict):
            continue
        for pkg in (phase.get("work_packages") or []):
            if not isinstance(pkg, dict):
                continue
            for leaf in (pkg.get("sub_packages") or []):
                if isinstance(leaf, dict):
                    leaves.append(leaf)
    return leaves


# ---------------------------------------------------------------------------
# ① 工作面人数上限：三套口径（旧 / 新 / 实际生效）逐行列出来
# ---------------------------------------------------------------------------
def _open_conn(conn, db_path):
    """解析连接：`conn` 优先；否则按 `db_path`（或 config.KB_DB_PATH）只读打开。"""
    if conn is not None:
        return conn, False
    if db_path is None or db_path == "":
        from . import config                        # 延迟导入：本模块要能被独立测
        db_path = config.KB_DB_PATH
    if not db_path or not os.path.exists(str(db_path)):
        return None, False
    try:
        # uri=...mode=ro：只读打开，物理上杜绝"审计层写库"
        uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/").replace("?", "%3f")
        return sqlite3.connect(uri, uri=True), True
    except sqlite3.Error:
        try:
            return sqlite3.connect(str(db_path)), True
        except sqlite3.Error:
            return None, False


def _table_columns(conn, table):
    try:
        cur = conn.cursor()
        return [str(r[1]) for r in cur.execute("PRAGMA table_info(%s)" % table).fetchall()]
    except sqlite3.Error:
        return []


def _effective_crew_max(crew_base, crew_min, crew_max):
    """**C8-7（2026-09-21）已删除**：`scheduler.effective_crew_max` 连同它的
    "×2.5 带"（`max(crew_max, min(40, ceil(crew_base × 2.5)))`）按 C 组删除清单
    第 7 项整条删除，本函数恒返回 `None`。

    保留签名只为让调用点少改一处；**绝不**在这里复刻 2.5 / 40 两个常量
    （那正是要删的东西）。新链路里"每工能上多少"来自 MWI 表，见
    `org_plan.plan_capacity_chain` / `scheduler.plan_organization`。
    """
    return None


def cmax_review_rows(conn=None, db_path=None, min_rows=CMAX_FLOOR_ROWS,
                     min_lift=CMAX_FLOOR_LIFT, sample_limit=5):
    """域 1.6 已删 Workface_Capacity_Rule 表，本函数返回 `no_table` 状态。

    只读，一个值都不改。返回::

        {"status": "ok" | "no_db" | "no_table",   # 拿不到库/表时**明说**，不返回空清单冒充"没问题"
         "table": "Workface_Capacity_Rule",
         "total_rows": 478,                       # 表里总行数
         "selected_rows": 381,                    # legacy_max_labor < crew_max 的行数
         "distinct_bands": 26,                    # 上述行按 (base,cmax,legacy,eff) 分几档
         "legacy_below_crew_max": 381,            # 旧上限 < 新上限
         "legacy_above_crew_max": 0,              # 旧上限 > 新上限（反向分歧，也要报）
         "null_ceiling_rows": 0,                  # crew_max 或 legacy_max_labor 为空
         "review_state": {"pending": 477, ...},    # 复核状态分布（**全部未复核**是最要紧的一条）
         "confidence": {"LOW": 477, ...},
         "bands": [ {...}, ... ],                 # 过阈值的档，按行数降序
         "note": "…口径说明…"}

    每一档 `{crew_base, crew_max, legacy_max_labor, effective_crew_max, lift_over_legacy,
    rows, work_type_l3, quantity_units, sample_activity_ids, sample_rule_ids, review_state,
    confidence, source_type, model_version}`：**三个上限值并排摆出来**，
    `effective_crew_max = max(crew_max, min(40, ceil(crew_base×2.5)))` 是排程实际用的那个。
    """
    # 默认阈值 + 走配置里的 KB 路径 → 结果缓存（`clear_cache()` 可清）。
    use_cache = (conn is None and db_path is None
                 and min_rows == CMAX_FLOOR_ROWS and min_lift == CMAX_FLOOR_LIFT
                 and sample_limit == 5)
    cache_key = None
    if use_cache:
        from . import config
        cache_key = _cache_key(getattr(config, "KB_DB_PATH", None))
        cached = _cache_get(cache_key)
        if cached is not None:
            # 缓存的是**只读快照**，每次给调用方一份独立拷贝 —— 万一有人就地改
            # 返回值（例如往 bands 里塞东西），不许污染下一次审计。
            return copy.deepcopy(cached)

    result = _cmax_review_rows_uncached(conn, db_path, min_rows, min_lift, sample_limit)
    if use_cache:
        _cache_put(cache_key, result)
    return result


def _cmax_review_rows_uncached(conn, db_path, min_rows, min_lift, sample_limit):
    """`cmax_review_rows` 的实际实现（缓存包装在上一层）。"""
    conn, owned = _open_conn(conn, db_path)
    if conn is None:
        return {
            "status": "no_db",
            "table": CMAX_TABLE,
            "total_rows": 0,
            "selected_rows": 0,
            "distinct_bands": 0,
            "legacy_below_crew_max": 0,
            "legacy_above_crew_max": 0,
            "null_ceiling_rows": 0,
            "review_state": {},
            "confidence": {},
            "bands": [],
            "note": "KB 不可读（库文件缺失或打不开）—— 本清单为空**不代表**没有分歧。",
        }
    try:
        cols = _table_columns(conn, CMAX_TABLE)
        if not cols:
            return {
                "status": "no_table",
                "table": CMAX_TABLE,
                "total_rows": 0,
                "selected_rows": 0,
                "distinct_bands": 0,
                "legacy_below_crew_max": 0,
                "legacy_above_crew_max": 0,
                "null_ceiling_rows": 0,
                "review_state": {},
                "confidence": {},
                "bands": [],
                "note": "KB 里没有 %s 表（迁移未跑）—— 本清单为空**不代表**没有分歧。"
                        % CMAX_TABLE,
            }
        rows = [dict(zip(cols, r)) for r in
                conn.cursor().execute("SELECT * FROM %s" % CMAX_TABLE).fetchall()]
    except sqlite3.Error:
        return {
            "status": "no_table",
            "table": CMAX_TABLE,
            "total_rows": 0,
            "selected_rows": 0,
            "distinct_bands": 0,
            "legacy_below_crew_max": 0,
            "legacy_above_crew_max": 0,
            "null_ceiling_rows": 0,
            "review_state": {},
            "confidence": {},
            "bands": [],
            "note": "KB 读取失败 —— 本清单为空**不代表**没有分歧。",
        }
    finally:
        if owned:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    total = len(rows)
    below = above = nulls = 0
    bands = collections.OrderedDict()
    states = collections.Counter()
    confs = collections.Counter()
    for row in rows:
        cmax = _num(row.get("crew_max"))
        legacy = _num(row.get("legacy_max_labor"))
        if cmax is None or legacy is None:
            nulls += 1
            continue
        if legacy < cmax - _ZERO:
            below += 1
        elif legacy > cmax + _ZERO:
            above += 1
        else:
            continue                      # 两代口径一致 → 没有这处分歧，不进清单
        base = _num(row.get("crew_base"))
        eff = _effective_crew_max(row.get("crew_base"), row.get("crew_min"), cmax)
        key = (base, cmax, legacy, _num(eff))
        band = bands.get(key)
        if band is None:
            band = bands[key] = {
                "crew_base": base,
                "crew_max": cmax,
                "legacy_max_labor": legacy,
                "effective_crew_max": _num(eff),
                "lift_over_legacy": (round(_num(eff) - legacy, 4)
                                     if _num(eff) is not None else None),
                "rows": 0,
                "work_type_l3": collections.Counter(),
                "quantity_units": collections.Counter(),
                "sample_activity_ids": [],
                "sample_rule_ids": [],
                "review_state": collections.Counter(),
                "confidence": collections.Counter(),
                "source_type": collections.Counter(),
                "model_version": collections.Counter(),
            }
        band["rows"] += 1
        for src_key, dst_key in (("work_type_l3", "work_type_l3"),
                                 ("quantity_unit", "quantity_units"),
                                 ("review_state", "review_state"),
                                 ("confidence", "confidence"),
                                 ("source_type", "source_type"),
                                 ("model_version", "model_version")):
            band[dst_key][_text(row.get(src_key)) or "（空）"] += 1
        if len(band["sample_activity_ids"]) < sample_limit:
            band["sample_activity_ids"].append(_text(row.get("activity_id")))
        if len(band["sample_rule_ids"]) < sample_limit:
            band["sample_rule_ids"].append(_text(row.get("rule_id")))
        states[_text(row.get("review_state")) or "（空）"] += 1
        confs[_text(row.get("confidence")) or "（空）"] += 1

    out_bands = []
    for band in bands.values():
        lift = band["lift_over_legacy"]
        if band["rows"] < min_rows:
            # C8-7（2026-09-21）：`effective_crew_max` / `lift_over_legacy` 口径已删除 →
            # 可见性**只看行数** `min_rows`；`min_lift` 保留为兼容参数但已不再生效。
            continue
        out_bands.append({
            "crew_base": band["crew_base"],
            "crew_max": band["crew_max"],
            "legacy_max_labor": band["legacy_max_labor"],
            "effective_crew_max": band["effective_crew_max"],
            "lift_over_legacy": lift,
            "rows": band["rows"],
            "work_type_l3": _top(band["work_type_l3"]),
            "quantity_units": _top(band["quantity_units"], n=3),
            "review_state": _top(band["review_state"]),
            "confidence": _top(band["confidence"]),
            "source_type": _top(band["source_type"]),
            "model_version": _top(band["model_version"]),
            "sample_activity_ids": band["sample_activity_ids"],
            "sample_rule_ids": band["sample_rule_ids"],
        })
    out_bands.sort(key=lambda b: (-b["rows"], -(b["lift_over_legacy"] or 0)))
    return {
        "status": "ok",
        "table": CMAX_TABLE,
        "total_rows": total,
        "selected_rows": below + above,
        "distinct_bands": len(bands),
        "bands_shown": len(out_bands),
        "legacy_below_crew_max": below,
        "legacy_above_crew_max": above,
        "null_ceiling_rows": nulls,
        "review_state": _top(states, n=5),
        "confidence": _top(confs, n=5),
        "bands": out_bands,
        "note": ("**C8-7（2026-09-21）已删除** `effective_crew_max = max(crew_max, "
                 "min(40, ceil(crew_base×2.5)))`（无规范依据的 ×2.5 带），因此本清单的 "
                 "`effective_crew_max` 一列**恒为 None**（不再参与任何排程口径）。"
                 "新链路里「每工能上多少」来自 MWI 表（`Resource_Workface_Index`），"
                 "而写进计划的 `leaf.workface_capacity.max_labor` 取的是 legacy_max_labor。"
                 "本清单只把它们并排列出，**不改任何值**。"),
    }


def _top(counter, n=1):
    """Counter → `[{"value": 名称, "rows": 条数}]`（降序，名字做二级键保证稳定）。"""
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [{"value": k, "rows": v} for k, v in items[:n]]


# ---------------------------------------------------------------------------
# ② 同一 source_code 给出明显不同的定额值
# ---------------------------------------------------------------------------
def norm_row_spread(tasks, min_ratio=NORM_SPREAD_RATIO, min_tasks=2):
    """找出"同一个 `source_code` + 同一量纲"却给出明显不同 `norm_value` 的叶子。

    分组键 = `(source_code, 规范化单位)`；还要同时满足：

      · ≥2 个**不同**的 `norm_value`，且 `max/min > min_ratio`（默认 1.5）；
      · ≥2 个**不同**的 `kb_activity_id` —— 同一个 activity 按不同条件取不同值是正常的
        （实测 `LD_T72_4_2008` 工日/m³ = 0.943 vs 0.85 就是 ALC 板 / 砌块两个工序）；
      · 任务名去掉楼层前缀后**还有共同的工序词** —— 挡掉 `AI_ESTIMATE_V1` 那一大类
        （实测 34 条、10 个不同值、比值 41.67，但工序名互不相干，是 AI 逐条估算，不是同一个值漂移）。

    实测真实计划（`plans/plan_sample3_after_fix.json`，333 条叶子）：**只命中 1 组** ——
    `LD_T72_7_2008` / 工日/t：4.43（`4.1.1.1` 钢筋绑扎 REBAR_NEW_FOUND）vs
    7.91（`5.1.1.1` 钢筋绑扎 REBAR_NEW_SLAB），比值 1.79，共同工序词「钢筋绑扎」。

    返回 `[{source_code, unit, tasks, distinct_values, distinct_activities, min_value,
    max_value, ratio, shared_tokens, norm_ids, condition_texts, samples:[…]}]`，按比值降序。
    """
    groups = collections.defaultdict(list)
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        binding = task.get("norm_binding")
        if not isinstance(binding, dict):
            continue
        source_code = _text(binding.get("source_code"))
        value = _pos(binding.get("norm_value"))
        if not source_code or value is None:
            continue
        unit = _canon_unit(binding.get("unit") or binding.get("norm_unit")
                           or task.get("unit"))
        groups[(source_code, unit)].append((value, task, binding))

    out = []
    for (source_code, unit), rows in groups.items():
        if len(rows) < min_tasks:
            continue
        values = collections.Counter(r[0] for r in rows)
        if len(values) < 2:
            continue
        lo, hi = min(values), max(values)
        ratio = hi / lo if lo > _ZERO else None
        if ratio is None or ratio <= min_ratio:
            continue
        activities = {_text(r[1].get("kb_activity_id")) for r in rows}
        activities.discard("")
        if len(activities) < 2:
            continue
        shared = set(_name_tokens(rows[0][1].get("name")))
        for _, task, _b in rows[1:]:
            shared &= _name_tokens(task.get("name"))
        if not shared:
            continue
        out.append({
            "source_code": source_code,
            "unit": unit,
            "family": _unit_family(unit),
            "tasks": len(rows),
            "distinct_values": len(values),
            "distinct_activities": len(activities),
            "min_value": lo,
            "max_value": hi,
            "ratio": round(ratio, 4),
            "shared_tokens": sorted(shared),
            "norm_ids": _uniques(r[2].get("norm_id") for r in rows),
            "condition_texts": _uniques(r[2].get("condition_text") for r in rows),
            "value_counts": [{"norm_value": v, "tasks": c}
                             for v, c in sorted(values.items())],
            "samples": [_sample_task(r[1], r[0]) for r in
                        sorted(rows, key=lambda x: x[0])[:6]],
        })
    out.sort(key=lambda g: (-g["ratio"], g["source_code"]))
    return out


def _uniques(values, limit=12):
    """按出现顺序去重（空值丢弃），最多 `limit` 个。"""
    seen, out = set(), []
    for value in values:
        key = _text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def _sample_task(task, norm_value=None):
    binding = task.get("norm_binding") if isinstance(task.get("norm_binding"), dict) else {}
    return {
        "id": _text(task.get("id")),
        "name": _text(task.get("name")),
        "kb_activity_id": _text(task.get("kb_activity_id")),
        "location": _text(task.get("location")),
        "quantity": _num(task.get("quantity")),
        "unit": _text(task.get("unit")),
        "duration_days": _num(task.get("duration_days")),
        "norm_value": _num(norm_value if norm_value is not None
                           else binding.get("norm_value")),
        "source_code": _text(binding.get("source_code")),
        "condition_text": _text(binding.get("condition_text")),
    }


# ---------------------------------------------------------------------------
# ③ 同一 activity + 同一楼层，同时按面积与体积各排一条
# ---------------------------------------------------------------------------
def duplicate_scope_groups(tasks, thickness_range=DUPLICATE_THICKNESS_RANGE,
                           sample_limit=6):
    """找出"同一 `kb_activity_id` + 同一 `location`"下**面积条目与体积条目物理等价**的组合。

    判据（三条全中，实测真实计划 18 组、0 误报）：

      · `kb_activity_id` 与 `location` 都非空且相同；
      · 该组里既有面积单位（m²/㎡/m2）又有体积单位（m³/㎥/m3）；
      · 体积 ÷ 面积 落在 `thickness_range`（默认 0.05–0.6 m）内 —— 等于"这堵墙有厚度"。

    **为什么必须加第三条**：只按 `(kb_activity_id, location)` 分组会得到 18 组，但同一
    个 activity 被复用于不同工序是常态 —— `SPREP_AI_003` = 场地平整 3200 m² + 场地硬化
    1200 m³/㎡ 混排、`GD_A11_机械挖土方` 被 6 条土方任务共用。只认"面积↔体积能换算出
    合理墙厚"的那批：实测 `LDT724_砌块墙` 在 `1-1层`…`18-18层` 各有 1420 m² / 284 m³
    （= 0.2 m 墙厚）。

    返回 `[{kb_activity_id, location, area:{…}, volume:{…}, thickness_m, tasks, ids}]`。
    """
    lo, hi = thickness_range
    groups = collections.defaultdict(list)
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        activity_id = _text(task.get("kb_activity_id"))
        location = _text(task.get("location"))
        if not activity_id or not location:
            continue
        groups[(activity_id, location)].append(task)

    out = []
    for (activity_id, location), rows in groups.items():
        if len(rows) < 2:
            continue
        by_family = collections.defaultdict(list)
        for task in rows:
            by_family[_unit_family(task.get("unit"))].append(task)
        areas, volumes = by_family.get("area") or [], by_family.get("volume") or []
        if not areas or not volumes:
            continue
        pairs = []
        for area in areas:
            qa = _pos(area.get("quantity"))
            if qa is None:
                continue
            for volume in volumes:
                qv = _pos(volume.get("quantity"))
                if qv is None:
                    continue
                thickness = qv / qa
                if lo <= thickness <= hi:
                    pairs.append((_sample_task(area), _sample_task(volume),
                                  round(thickness, 4)))
        if not pairs:
            continue
        thicknesses = sorted({p[2] for p in pairs})
        out.append({
            "kb_activity_id": activity_id,
            "location": location,
            "thickness_m": thicknesses[0] if len(thicknesses) == 1 else thicknesses,
            "consistent_thickness": len(thicknesses) == 1,
            "tasks": sum(len(by_family[f]) for f in ("area", "volume")),
            "ids": [t.get("id") for t in areas] + [t.get("id") for t in volumes],
            "areas": [_sample_task(t) for t in areas[:sample_limit]],
            "volumes": [_sample_task(t) for t in volumes[:sample_limit]],
            "pairs": [{"area_id": a["id"], "volume_id": v["id"], "thickness_m": t}
                      for a, v, t in pairs[:sample_limit]],
        })
    out.sort(key=lambda g: (g["kb_activity_id"], g["location"]))
    return out


# ---------------------------------------------------------------------------
# ④ 汇总裁剪：plan / ctx → meta["scope_audit"]
# ---------------------------------------------------------------------------
def _as_leaves(plan_or_ctx):
    """入参可以是整份 plan、ctx，或直接是叶子列表。"""
    if isinstance(plan_or_ctx, (list, tuple)):
        return [t for t in plan_or_ctx if isinstance(t, dict)]
    if isinstance(plan_or_ctx, dict):
        if plan_or_ctx.get("sub_packages") is not None:
            return [plan_or_ctx]
        return leaf_tasks(plan_or_ctx)
    return []


def scope_audit(plan_or_ctx, conn=None, db_path=None, cmax=True,
                norm_spread=True, duplicate_scopes=True, sample_limit=6):
    """一次跑完三项审计 → 可直接进 `plan.meta["scope_audit"]` 的 dict。

    `plan_or_ctx` 收：整份 plan（自动取 `wbs`）、ctx（`ctx["wbs"]`）或叶子列表。
    `cmax` 需要读 KB（可用 `conn` / `db_path` 换成测试库；拿不到库时 status 会**明说**
    `no_db`，不会用空清单冒充"没问题"）。

    返回::

        {"generated_at": "2026-…", "leaves": 333,
         "cmax_ceiling":  {…cmax_review_rows()…},
         "norm_spread":   {…count/ratio_max/groups…},
         "duplicate_scope": {…count/thickness/leaves_involved/groups…},
         "flags":         ["cmp…", …],     # 需要人看一眼的短句（可直接打印）
         "note": "审计层只读：本块**不改任何值**，只把可疑口径摆出来。"}
    """
    leaves = _as_leaves(plan_or_ctx)
    audit = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "leaves": len(leaves),
        "note": "审计层只读：本块**不改任何值**，只把可疑口径摆出来供人复核。",
    }

    if cmax:
        audit["cmax_ceiling"] = cmax_review_rows(conn=conn, db_path=db_path)
    else:
        audit["cmax_ceiling"] = {"status": "skipped"}

    if norm_spread:
        groups = norm_row_spread(leaves)
        audit["norm_spread"] = {
            "groups": groups,
            "count": len(groups),
            "ratio_max": max([g["ratio"] for g in groups], default=None),
            "tasks_involved": sum(g["tasks"] for g in groups),
        }
    else:
        audit["norm_spread"] = {"groups": [], "count": 0, "ratio_max": None,
                                "tasks_involved": 0}

    if duplicate_scopes:
        dups = duplicate_scope_groups(leaves, sample_limit=sample_limit)
        thicknesses = sorted({d["thickness_m"] for d in dups
                              if isinstance(d["thickness_m"], (int, float))})
        audit["duplicate_scope"] = {
            "groups": dups,
            "count": len(dups),
            "thicknesses_m": thicknesses,
            "leaves_involved": sum(len(d["ids"]) for d in dups),
        }
    else:
        audit["duplicate_scope"] = {"groups": [], "count": 0, "thicknesses_m": [],
                                    "leaves_involved": 0}

    flags = []
    ceiling = audit["cmax_ceiling"]
    if ceiling.get("status") == "ok" and ceiling.get("selected_rows"):
        states = {s["value"]: s["rows"] for s in (ceiling.get("review_state") or [])}
        pending = states.get("pending", 0)
        flags.append(
            "工作面人数上限：%d/%d 行新旧口径不一致（旧上限被顶高），实际生效上限再被公式抬高；"
            "复核状态 pending %d 行 —— 需要人核"
            % (ceiling["selected_rows"], ceiling["total_rows"], pending))
    elif ceiling.get("status") in ("no_db", "no_table"):
        flags.append("工作面人数上限：KB 读不到（%s），本项**未核**" % ceiling["status"])

    spread = audit["norm_spread"]
    if spread["count"]:
        top = spread["groups"][0]
        flags.append(
            "定额离散：%d 组同 source_code 给出差 %.2f 倍的值（最大 %s %s：%s → %s）；"
            "工期直接按它算 —— 需要人核"
            % (spread["count"], top["ratio"], top["source_code"], top["unit"],
               top["min_value"], top["max_value"]))

    dup = audit["duplicate_scope"]
    if dup["count"]:
        flags.append(
            "重复范围：%d 组同 activity + 同楼层同时按面积与体积各排一条"
            "（换算厚度 %s m，物理等价）—— 是否重复计价需要人核"
            % (dup["count"], dup["thicknesses_m"]))

    audit["flags"] = flags
    return audit


def scope_audit_summary(audit, limit=6):
    """把 `scope_audit()` 的结果压成**几行文本**（终端工具用；不给就自己算）。"""
    if not isinstance(audit, dict):
        audit = scope_audit(audit)
    lines = ["审计层（只读，不改值）：%d 条叶子" % audit.get("leaves", 0)]
    ceiling = audit.get("cmax_ceiling") or {}
    if ceiling.get("status") == "ok":
        lines.append("  ① 工作面人数上限：%d/%d 行旧口径 < 新口径，%d 档"
                     % (ceiling.get("selected_rows", 0), ceiling.get("total_rows", 0),
                        ceiling.get("distinct_bands", 0)))
        for band in (ceiling.get("bands") or [])[:limit]:
            lines.append("     base=%s → crew_max=%s / 旧 max_labor=%s / 实际生效=%s  (%d 行)"
                         % (band.get("crew_base"), band.get("crew_max"),
                            band.get("legacy_max_labor"), band.get("effective_crew_max"),
                            band.get("rows", 0)))
    else:
        lines.append("  ① 工作面人数上限：未核（%s）" % ceiling.get("status"))
    spread = audit.get("norm_spread") or {}
    lines.append("  ② 定额离散：%d 组" % spread.get("count", 0))
    for group in (spread.get("groups") or [])[:limit]:
        lines.append("     %s %s 比值 %.2f（%s → %s），%d 条任务，共同工序 %s"
                     % (group.get("source_code"), group.get("unit"), group.get("ratio", 0),
                        group.get("min_value"), group.get("max_value"),
                        group.get("tasks", 0), "/".join(group.get("shared_tokens") or [])))
    dup = audit.get("duplicate_scope") or {}
    lines.append("  ③ 重复范围：%d 组（换算厚度 %s m）"
                 % (dup.get("count", 0), dup.get("thicknesses_m")))
    for group in (dup.get("groups") or [])[:limit]:
        lines.append("     %s @ %s：厚度 %s m，%s"
                     % (group.get("kb_activity_id"), group.get("location"),
                        group.get("thickness_m"), "/".join(group.get("ids") or [])))
    for flag in (audit.get("flags") or []):
        lines.append("  ⚠ " + flag)
    return "\n".join(lines)
