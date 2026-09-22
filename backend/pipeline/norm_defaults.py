"""L4 默认定额行（L4_Norm_Default）—— **工期口径的唯一允许来源**（第 38 轮）

## 它解决什么问题

第 37 轮把"定额锚定"做成了**运行时临时选择**：`norm_bind` 给每条 L4 现场挑一行
`Norm_Labor_Table`，挑中什么就用什么算工期。实测两个致命后果：

  ① **同 L4 下的错误行**：节拍配置把「铝模安装」挂到 `FORM_NEW_OTHER`（该 L4 的 6 行
     **全部是"垫层"**），匹配器兜底取首行 `LN_3853 = 0.025 工日/m²`（垫层/带形/木模板）
     → 1972 m² 的铝合金模板被按"垫层木模"算，量级差 30~50 倍。
  ② **AI 估算被当证据**：`AI_ESTIMATE_V1` 的 75 条（围挡 0.15 工日/m、测量放线
     0.012 工日/m²…）在 `scheduler.norm_is_evidence` 判据里**全部为 True**，于是
     `duration = by_norm`（定额反算值直接覆盖 WBS 目标工期，连"不得慢于目标工期"
     这条下限都不复用）。同一份输入，仅因上游补齐了 `usable` 键，总工期就从 847 天
     涨到 1488 天。

## 本模块的口径（第 41 轮修订 —— 2026-09-20 政策变更）

> ### ⚠️ 政策变更注记（2026-09-20，用户决定）
>
> **依据**：用户本人决定放开"AI 经验估算定额不得参与工期/班组"这条限制。
> **理由**：AI 估算是当前**唯一可得的覆盖面来源** —— 装饰 / 机电 / 临建类活动
> 在库里只有 `AI_ESTIMATE_V1` 行，继续拦截等于这些活动永远没有定额口径；
> 放开的前提是**交付物逐条标注**（`state="released_ai"` + `LABEL_AI_ESTIMATE`），
> 让用户一眼看出哪些数没有规范依据、尚未人工审定。
> **范围**：只改 `estimated` 这一档（拦下 → 放行 + 标注）。其余判据一条都没松，
> 逐条列在下面"仍然拦下"里。

**谁能决定工期，看的是"这条定额从哪来"，不是"有没有人点过通过"。**

| 行的来源（`confidence`）         | 能否决定工期 | 状态 / 标注 |
|---------------------------------|--------------|------|
| `verified` 现行国标/省定额原文行   | **可以**     | `approved` 或 `released_unapproved` |
| `parsed` 资料解析得到的真人定额行  | **可以**     | `approved` 或 `released_unapproved` |
| `estimated` AI 经验估算           | **可以（第 41 轮起）** | `released_ai` + `LABEL_AI_ESTIMATE` |
| 来源不明（`confidence` 缺失/拼写异常） | 不可以   | `unknown`（保守拦下，去人工审定） |

    gate_open(activity_id, unit, kind) → True   ⟺  该 L4 下有**非 rejected**、
                                                   **norm_value > 0** 的行，且其中最好的
                                                   一行是 approved / verified / parsed /
                                                   estimated

**仍然拦下（第 41 轮一条都没松）**：

  * `review_state='rejected'`：**唯一的人工硬开关** —— 被否决的行永远不复活；
  * `norm_value` 取不到或 <= 0（空值/零值不构成工期证据）；
  * 来源不明（`confidence` 既不是 verified / parsed / estimated）→ 保守拦下待审；
  * 表里没有这个 L4 / 任务没锚定 L4 → **放行**（`no_row` / `no_activity`，
    沿用第 37 轮旧判据 —— 否则"表还没灌"会把所有合成活动打死）；
  * **旁路判据**（不在本闸门内，但继续拦）：单位不可用/不可换算（`units_compatible`
    类）、`method_conflict`（定额口径与任务不符）、绑定层 `usable is False`、
    `origin/match/ai_source` 的 AI 标记 —— 由 `norm_bind` 与
    `scheduler._build_ledger_item` 判。本闸门只是**叠在上面的一层**，
    它放行**不等于**绕过那几条（详见 `backend/pipeline/nodes/scheduler.py`
    `_build_ledger_item` 里的 `norm_is_evidence` 表达式）。

第 38 轮把闸门写成"必须 `review_state='approved'` 才放行"，实测把 **373 条**
（283 条真人 `parsed` + 90 条 AI `estimated`）**一起拦下**：真人定额也被当成
"没资格定工期"，用户看到的是"我的定额明明是从资料里解析出来的，凭什么不算"。
第 39 轮改成**按来源分档**：

  * `verified` / `parsed` → **放行**，但**必须标注"未经人工审定"**：`gate_label()`
    给出 `state / confidence / source_code`，由 `scheduler` 收进台账，
    再进 `meta.norm_coverage` 与 Word「数据来源与置信度」章节；
  * `estimated` → 第 39 轮**仍然拦下**；**第 41 轮（2026-09-20）起改为放行 +
    逐条标注** `released_ai`（文案 `LABEL_AI_ESTIMATE`），理由见上方政策变更注记。
    这一档是第 41 轮**唯一**被放开的档；
  * `review_state='rejected'` → **拦下**：人工显式否决过的那一行永远不复活 ——
    否决取代了原来"默认全部待审 = 全部不放行"的一刀切。

`scheduler._build_ledger_item` 把它并入 `norm_is_evidence`：放行的照旧
`duration = by_norm`（定额反算）；拦下的一律 `norm_is_evidence=False` → 退回
`duration = max(target_days, by_norm)`（目标工期当地板，定额只作参考）。

## 表结构

见 `_DDL`。主键 `(activity_id, quantity_unit, norm_kind)`：一个 L4 在一个计量单位下
只允许一个默认行。条件组合（构件类型/材料/规格）留在 `condition_key` 里做溯源与展示，
**不做运行时消歧** —— 消歧正是上一版选错行的入口。

初始化见 `backend/tools/seed_norm_defaults.py`；人工审定见同目录 `approve_norm_default.py`。
"""

from __future__ import annotations

from . import kb
from . import kb_units

# ---- 置信度 / 审状态枚举（与 KB 里 status 的 verified/parsed/needs_review 对齐）----
CONF_VERIFIED = "verified"
CONF_PARSED = "parsed"
CONF_ESTIMATED = "estimated"
CONFIDENCES = (CONF_VERIFIED, CONF_PARSED, CONF_ESTIMATED)

STATE_APPROVED = "approved"
STATE_PENDING = "pending"
STATE_REJECTED = "rejected"
REVIEW_STATES = (STATE_APPROVED, STATE_PENDING, STATE_REJECTED)

KIND_LABOR = "labor"
KIND_MACHINE = "machine"

TABLE = "L4_Norm_Default"

_DDL = """
CREATE TABLE IF NOT EXISTS L4_Norm_Default (
    activity_id       TEXT    NOT NULL,
    quantity_unit     TEXT    NOT NULL,
    norm_kind         TEXT    NOT NULL,
    condition_key     TEXT    NOT NULL DEFAULT '',
    norm_value        REAL    NOT NULL,
    norm_unit         TEXT    NOT NULL DEFAULT '',
    source_code       TEXT    NOT NULL DEFAULT '',
    source_kind       TEXT    NOT NULL DEFAULT '',
    confidence        TEXT    NOT NULL DEFAULT 'estimated',
    default_crew      INTEGER,
    review_state      TEXT    NOT NULL DEFAULT 'pending',
    reviewed_by       TEXT    NOT NULL DEFAULT '',
    reviewed_at       TEXT    NOT NULL DEFAULT '',
    notes             TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (activity_id, quantity_unit, norm_kind)
)
"""

# 性能护栏：一次排程会对 300+ 条任务反复问"这条放行了吗"。
# 不依赖 kb._memoize（那是给 KB 只读表的，本表运行期可被审改）。
_APPROVED_CACHE = {}
# 完整判定缓存（含来源标注）—— gate_open / gate_label 共用，避免每条任务查两次库。
_DECISION_CACHE = {}


def clear_cache():
    """清空放行缓存（审定脚本 / 测试改了表就调它）。"""
    _APPROVED_CACHE.clear()
    _DECISION_CACHE.clear()


def ensure_table():
    """建表（幂等）。**不动任何既有表**；只新增一张 L4_Norm_Default。"""
    try:
        conn = kb._connect()
    except Exception:
        return False
    try:
        conn.execute(_DDL)
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _norm_key(unit, kind):
    """闸门用的单位归一 —— 必须走 `kb_units.normalize_unit`。

    否则 `m3`（ASCII）与 `m³`（上标）会被当成两个单位：实测 EARTH0032 的行建在
    "m³"、任务写 "m3"，精确匹配查不到 → 闸门误判成"没有这个 L4"而放行。
    """
    try:
        u = kb_units.normalize_unit(str(unit or "").strip())
    except Exception:
        u = str(unit or "").strip()
    return (u, str(kind or KIND_LABOR).strip() or KIND_LABOR)


def pre_approved(activity_id, unit, kind=KIND_LABOR):
    """该 L4 + 计量单位 + 人工/机械 是否已有**人工审定过**的默认行。

    返回 True 才算"定额可作工期证据"。取不到 / 表不存在 / 未审 → False
    （**默认拒绝**：这正是不让 AI 现场选值接管工期的开关）。
    """
    aid = str(activity_id or "").strip()
    if not aid:
        return False
    u, k = _norm_key(unit, kind)
    key = (aid, u, k)
    if key in _APPROVED_CACHE:
        return _APPROVED_CACHE[key]
    rows = kb._query_all(
        "SELECT review_state, norm_value FROM %s "
        "WHERE activity_id = ? AND quantity_unit = ? AND norm_kind = ?" % TABLE,
        (aid, u, k))
    ok = False
    for state, value in rows:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if str(state or "") == STATE_APPROVED and v > 0:
            ok = True
            break
    _APPROVED_CACHE[key] = ok
    return ok


# 不通过的原因（写进台账 / 覆盖率报告，让用户知道"为什么这条没算定额"）
REASON_NO_ROW = "L4无默认定额行（未预置）"
REASON_NOT_APPROVED = "L4默认定额行待人工审定"
# 第 39 轮新增：AI 估算单独成一档（原来和"待人工审定"混在一句里，用户看不出
# "我该去审" 还是 "这条本来就是 AI 猜的、无可审"）。
# ⚠️ 第 41 轮（2026-09-20）政策变更后：**闸门不再返回本常量** —— `estimated` 行
# 改为放行 + `released_ai` 标注（见模块 docstring 的政策变更注记）。保留定义是因为
# 其它文件/测试仍在引用它（历史台账与覆盖率文案的兼容读取）。
# 第 41 轮起不再拦截，此处仅作历史说明。
REASON_AI_ONLY = "L4默认定额行仅有 AI 估算，不作工期证据"
REASON_REJECTED = "L4默认定额行已被人工否决"
# 第 41 轮新增：放行 AI 估算的同时**不能**把"零值/空值行"一起放进来。原来这类行是
# 被 REASON_AI_ONLY 顺带挡住的（estimated 一律拦），AI 放开后必须自己挡 ——
# 一个取不到 / <= 0 的值不构成工期证据。
REASON_NO_VALUE = "L4默认定额行定额值缺失或非正（<=0），不作工期证据"

# ---- 第 41 轮（2026-09-20 政策变更）：AI 经验估算定额的放行接口 ----
# ⚠️ 下面两个名字是**下游契约**（scheduler / resource / 交付物标注按字面引用），
# 不许改名：状态字符串固定为 "released_ai"，标注文案固定为 LABEL_AI_ESTIMATE 的值。
STATE_RELEASED_AI = "released_ai"
LABEL_AI_ESTIMATE = "AI 经验估算定额（无规范依据，待审）"
# 零值/空值行单独一个状态（既不"放行"也不"待审"—— 改也改不出一个数）。
STATE_NO_VALUE = "no_value"

# 有据可查的真人来源：这两档**不需要**先 approve 就能决定工期（但要标注 ——
# 见 `gate_label`）。第 38 轮的"必须 approved"把 283 条真人 parsed 行一起误伤了。
# 第 41 轮（2026-09-20）起 `estimated` **同样放行**，只是标注档不同（released_ai）——
# 所以闸门不再把它当成"非人类来源就拦"，而在 `gate_decision` 里单独给 released_ai。
HUMAN_CONFIDENCES = (CONF_VERIFIED, CONF_PARSED)
_CONFIDENCE_RANK = {CONF_VERIFIED: 3, CONF_PARSED: 2, CONF_ESTIMATED: 1}


def _candidate_rows(aid, u, k):
    """该 L4 的候选行 ``[(review_state, norm_value, confidence, source_code)]``。

    先按 (活动, 单位, 口径) 精确查；查不到再按 (活动, 口径) 找一次 —— 任务是"根"、
    默认行建在"m³"很正常（EARTH0032 就是这种）。**单位写法一换就绕开闸门**是旧洞，
    这里必须保留这道兜底。
    """
    rows = kb._query_all(
        "SELECT review_state, norm_value, confidence, source_code FROM %s "
        "WHERE activity_id = ? AND quantity_unit = ? AND norm_kind = ?" % TABLE,
        (aid, u, k))
    if not rows:
        rows = kb._query_all(
            "SELECT review_state, norm_value, confidence, source_code FROM %s "
            "WHERE activity_id = ? AND norm_kind = ?" % TABLE, (aid, k))
    return rows


def gate_decision(activity_id, unit, kind=KIND_LABOR):
    """闸门的**完整**判定 → ``(allowed, reason, label)``。

    判据（第 41 轮，2026-09-20 政策变更；详见模块 docstring）：

      | 该 L4 的行状态                          | allowed | label.state |
      |----------------------------------------|---------|-------------|
      | 有人 approved（且值 > 0）                | True    | approved |
      | 无 approved，最好的一行是 verified/parsed | **True** | **released_unapproved** |
      | 无 approved，最好的一行是 estimated       | **True** | **released_ai**（第 41 轮放开） |
      | 非 rejected 的行全部取不到正定额值         | False   | no_value |
      | 所有行都被 rejected                      | False   | rejected |
      | 来源不明（confidence 不在三档内）          | False   | unknown |
      | 该 L4 一行都没有 / 任务没锚定 L4          | True    | no_row / no_activity |

    `label` 是给**交付物标注**用的来源信息（`state / confidence / source_code / note`），
    由 `scheduler` 收进台账 → `meta.norm_coverage` → Word「数据来源与置信度」。
    AI 估算那一档的 `note` 固定为 `LABEL_AI_ESTIMATE`（下游按字面比对，别改）。
    """
    aid = str(activity_id or "").strip()
    if not aid:
        return True, "", {"state": "no_activity", "confidence": "", "source_code": "",
                          "note": "任务没有锚定 L4 活动，闸门不发表意见"}
    u, k = _norm_key(unit, kind)
    key = (aid, u, k)
    if key in _DECISION_CACHE:
        return _DECISION_CACHE[key]
    rows = _candidate_rows(aid, u, k)
    if not rows:
        out = (True, "", {"state": "no_row", "confidence": "", "source_code": "",
                          "note": "该 L4 在默认定额表里没有行，闸门不发表意见"
                                  "（沿用第 37 轮旧判据）"})
        _DECISION_CACHE[key] = out
        return out
    live = [r for r in rows if str(r[0] or "") != STATE_REJECTED]
    if not live:
        out = (False, REASON_REJECTED,
               {"state": "rejected", "confidence": "", "source_code": "",
                "note": REASON_REJECTED})
        _DECISION_CACHE[key] = out
        return out
    # ① 已有人审定通过 → 放行（原第 38 轮口径，优先级最高）
    for state, value, conf, src in live:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if str(state or "") == STATE_APPROVED and v > 0:
            out = (True, "", {"state": "approved", "confidence": str(conf or ""),
                              "source_code": str(src or ""), "note": "已人工审定"})
            _DECISION_CACHE[key] = out
            return out
    # ② 值判据（第 41 轮显式化）：取不到 / <= 0 的定额值不能决定工期。
    #    第 41 轮之前，estimated 的零值行是被 REASON_AI_ONLY 顺带挡住的；放开 AI 后
    #    必须自己挡，否则"0 工日/m²"会被标成 released_ai 一路放行。
    valued = []
    for r in live:
        try:
            if float(r[1]) > 0:
                valued.append(r)
        except (TypeError, ValueError):
            continue
    if not valued:
        worst = max(live, key=lambda r: _CONFIDENCE_RANK.get(str(r[2] or ""), 0))
        out = (False, REASON_NO_VALUE,
               {"state": STATE_NO_VALUE, "confidence": str(worst[2] or ""),
                "source_code": str(worst[3] or ""), "note": REASON_NO_VALUE})
        _DECISION_CACHE[key] = out
        return out
    # ③ 没审定过 → 按**来源**分档（第 39 轮新口径；第 41 轮起 AI 估算也放行 + 标注）
    best = max(valued, key=lambda r: _CONFIDENCE_RANK.get(str(r[2] or ""), 0))
    conf = str(best[2] or "")
    src = str(best[3] or "")
    if conf in HUMAN_CONFIDENCES:
        out = (True, "", {
            "state": "released_unapproved", "confidence": conf, "source_code": src,
            "note": "未经人工审定，按来源放行（confidence=%s，来源 %s）——"
                    "如认为该行不适用于这条活动，请用 tools/approve_norm_default.py "
                    "把它标为 rejected" % (conf, src or "—")})
    elif conf == CONF_ESTIMATED:
        # 第 41 轮（2026-09-20 政策变更）：AI 经验估算**放行**（允许决定工期与班组），
        # 但必须逐条标注 —— `state="released_ai"`，文案就是 LABEL_AI_ESTIMATE。
        out = (True, "", {"state": STATE_RELEASED_AI, "confidence": conf,
                          "source_code": src, "note": LABEL_AI_ESTIMATE})
    else:
        # 来源不明的行（confidence 缺失/拼写异常）：保守拦下，让它去人工审定
        out = (False, REASON_NOT_APPROVED,
               {"state": "unknown", "confidence": conf, "source_code": src,
                "note": REASON_NOT_APPROVED})
    _DECISION_CACHE[key] = out
    return out


def gate_label(activity_id, unit, kind=KIND_LABOR):
    """放行/拦下的**来源标注**（进 `meta.norm_coverage` 与交付物）。"""
    return gate_decision(activity_id, unit, kind)[2]


def gate_open(activity_id, unit, kind=KIND_LABOR):
    """工期闸门的最终判定 → ``(allowed, reason)``。

    第 39 轮起真正的判据在 `gate_decision()`（按**来源**分档：真人放行 + 标注，
    rejected 一票否决）；**第 41 轮（2026-09-20 政策变更）起 AI 经验估算
    （`confidence="estimated"`）也放行**，状态名为 `released_ai`，标注文案
    `LABEL_AI_ESTIMATE` —— 见模块 docstring 的政策变更注记。
    本函数保留**两元组**签名与返回形状（既有调用方与 tests/conftest 的替身都靠它）；
    要拿来源标注请调 `gate_label()`。

    为什么"没有行"要放行而不是拦下：`norm_bind` 里那套旧判据（origin/match/
    ai_source/method_conflict/单位换算）**继续生效**，本闸门只是额外叠一层。
    若把"没有行"也判 False，就会把"表还没灌 / 测试里的合成 L4 / 未知活动"一并
    打成不可用，等于用一道数据缺口否掉整条既有契约（实测：test_scheduler 26 个
    用例连带失败，它们构造的合成活动在库里根本没有对应行）。
    """
    return gate_decision(activity_id, unit, kind)[:2]


def gate_reason(activity_id, unit, kind=KIND_LABOR):
    """未放行时返回中文原因；放行返回 ""。"""
    return gate_open(activity_id, unit, kind)[1]
