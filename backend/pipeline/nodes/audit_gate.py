"""三轮回审门 —— 计划生成后必须由**用户**审过，才算数

产品定位是"不替用户拍板"，所以三个关键决策点都要停下来给用户看：

    R1  audit_wbs       WBS 结构审：阶段/工作包/叶子数、各阶段行数、细度、工程量分布
    R2  audit_schedule  两版工期审：理论最短 / 资源不超额、差多少、峰值、定额覆盖率
    R3  audit_draft     Word 草案审（**不含图表**）：看完打 Y 才整理最终计划 + 画可视化看板

三个门都用既有的人工门机制（register → emit EV_PARAM_REVIEW → wait → /params 决策），
不引入新的引擎概念、不新增事件类型（终端与前端因此**零改动**就能显示）。

审计语义（与"参数复核门"刻意不同）：
  · 打 Y          → 本轮通过，记进审计链；三轮全过 → `meta.audit_status = 已审计`
  · 疑问句         → **不算审计意见**：解释清楚（本门在审什么 / 展示粒度影响什么 /
                    三轮回审门各审什么）后**重新问一次**。不记账、不计轮次、不影响后续门。
                    实测用户就是把疑问句「我选择的不是五层为一组吗」输进来的。
  · 输入修改意见   → 先把"本门改不了上游 WBS"这个事实与代价摆到台面上，让用户在
                    [1] 停下重跑 / [2] 继续跑完 / [3] 返回重填 里明确选一条
                    （`_menu_text`），**不立刻记账**：
                      [1] → `_stop`，消息带原话 + 重跑时在【WBS 复评门】怎么改最省事；
                      [2] → 记进 `meta.audit_comments`，计划保持"未审计"，并明确告知
                            "接下来还会继续算、且【R2】会被跳过、最终不出定稿与看板"；
                      [3] → 重新问（不记账、不计轮次）。
                    计划本身照常交付 —— 用户要能拿到它、看清问题、用 /revise 改。
  · 认不出的输入   → 按 [2] 处理（保守，与老行为逐项一致）。
  · abort/取消     → 取消整次运行（不变）。

为什么"未通过就停"而不是"记下意见继续跑"：交付物上写着"施工进度计划"却带着用户
没认可的内容，是最容易出事的一种"静默错误"。停下来的代价远小于误交付。

为什么**不**在 R1/R2 就把整条流水线掐掉：那时 `plan_json` 还没组装（R2 在 assembler
之前），掐掉等于让用户白跑一遍、连计划都拿不到。所以 R1/R2 退回只**标记**未通过，
照常把计划落盘（带"未审计"），到 R3 门口统一拦下最终交付物。
**但代价必须说清**（否则就是本轮实测里的"意见被记下了，却还在做定额"）：
WBS 不改，后面的定额锚定/配员/排程都是建立在错误结构上的白算，而且被退回后
【R2】两版工期审计门会**被跳过**。这两句写在菜单里（选之前就看得见）；
真的跳过时也会在终端明确打印一行 —— 走 `warning_ctx_key` 这条既有的警告上行通道，
默认折叠模式下同样可见（折叠策略只折叠"没有警告"的 node_done）。

§D 冻结契约（用户最不满的一点）：「WBS 门应该返回详细的 WBS 树，为什么只返回摘要？
你打算让用户依据什么来决定是否继续计划？」——所以每道门在原有文字摘要之外，还要带
**用户能据以决策的实物内容**：

    R1  wbs_tree          WBS 树（阶段 → 工作包 → 叶子：编号/名称/工程量+单位/工期）
    R1  granularity_note  展示粒度**口径说明**（第 23 轮新增）：你选了什么、本门为什么
                          按原始 WBS 展示、交付物会按你的粒度合并成几行
    R2  schedule_compare  两版工期对比（天数/叶子数/关键任务数/人工峰值 + 最长任务 + 口径）
    R3  draft_outline     草案目录（章节 + 行数、表格 + 行数、图清单、定额覆盖率）

字段名与结构见 `资料/终端界面改造规格.md` §D，**只增不改**：原有的
`summary` / `highlights` / `issues` / `output_summary` / `context_summary` 全部保留
（界面有兼容路径，字段缺失时优雅退化）。

三条铁律（本文件所有 §D 取数函数都遵守）：
  1. 数据**只从 ctx / 节点已有产物里取**（`wbs` / `schedule_versions` /
     `norm_coverage` / `plan_json` / 口径 warnings），不重算工程量、不重算工期、不编数；
  2. 取不到数据 → 返回 None → 事件里**不带**该字段（界面优雅退化），绝不抛异常；
  3. 大树/长表一律**从头按顺序截断**并如实报被截掉的数量 —— 不许把 415 条叶子
     全塞进 SSE 帧（帧会非常大）。
"""

import math
import re
import uuid

from .. import quantity
from ..base import BaseNode
from ..events import EV_PARAM_REVIEW
# 人工门的等待上限统一在这里（第 35 轮：门只等 10 分钟导致"过期后回话被当取消"）
from ..registry import GATE_TIMEOUT_SECONDS
from .boundary import is_abort_decision
from .kb_conformance import check_scope_conformance

# §D1 限流：最多 6 个阶段、总计最多 40 条叶子（按阶段顺序从头取）
WBS_TREE_MAX_PHASES = 6
WBS_TREE_MAX_LEAVES = 40
# §D2 top_tasks 条数（按工期降序）
SCHEDULE_TOP_TASKS = 6

# ==================== 审计身份：这道门是**谁**答的 ====================
#
# 真实事故（用户审计 P0-A）：`devtools/rerun_sample3.py` 用
# `REGISTRY.resolve(key, {"action": "continue", "decision": True, "passed": True, ...})`
# 替真人把三个审计门**全部点了通过**。于是计划数据里出现「R3 通过」，
# 定稿 Word 就照实印出「【已审计定稿】」—— 但没有任何人真的看过那份草案。
#
# 结论：`passed=True` 只能证明"这道门被放行了"，**不能**证明"有人审过"。
# 所以每一轮审计除 `passed` 之外还必须记 **`answered_by`（谁答的门）**：
# 判据是 resolve 载荷里的**明确标记**，取不到就是 `unknown`。
# **绝不假设是人工** —— 宁缺勿假：`unknown` 与 `script` / `auto` 一律不算人工复审。
#
# 谁该在载荷里写 `answered_by`：
#   · 人工通道（main.py 的 /params、/resume、/confirm）→ `"human"`；
#   · 自动化脚本 → `"script"`；引擎自己的兜底放行 → `"auto"`。
# 一个都没写 → `unknown`，交付物按未审计印，并说明"应答来源未记录"。

#: 允许的取值，没有第四个。`unknown` 是"拿不到记录"的兜底，不是一种身份。
ANSWER_SOURCES = ("human", "script", "auto")

#: 非人工来源的显示名（`human` 不在此表 —— 它由 `audit_honesty` 直接判定）。
_SOURCE_TEXT = {"script": "脚本代答", "auto": "系统自动通过"}

#: 三轮的名字。只在"缺记录"的说明里用（老计划可能连 name 都没存）。
AUDIT_ROUND_NAMES = {1: "WBS 结构", 2: "两版工期", 3: "Word 草案（不含图表）"}

#: 配得上「已审计定稿」的轮次集合：三轮缺一不可。
AUDIT_ROUNDS_REQUIRED = (1, 2, 3)


def answered_by_of(payload) -> str:
    """从 resolve 载荷里读「谁答的这道门」。读不到/值不认识 → ``"unknown"``。

    只认**明确标记**（`answered_by` / `answeredBy` / `_answered_by`）。判据永远只有
    一个方向：拿不到证据就是 `unknown`，**不**从"有 passed=True"倒推"是人工点过 Y"。
    """
    src = payload if isinstance(payload, dict) else {}
    for key in ("answered_by", "answeredBy", "_answered_by"):
        raw = src.get(key)
        if isinstance(raw, str):
            value = raw.strip().lower()
            if value in ANSWER_SOURCES:
                return value
            if value:
                return "unknown"        # 有标记但不认识 → 不猜，按未审计处理
    return "unknown"


def _audit_round_text(rounds, confirmed=False) -> str:
    """交付物里那一行「三轮回审：…」。

    ⚠️ **未确认已审计时不许出现「R3 通过」这样的字**：写在页头就等于在说"有人审过了"，
    而脚本代答同样会让 `passed` 为真。所以未确认时逐轮写**谁答的**：
    通过（人工）/ 脚本代答 / 自动放行 / 已放行(来源未记录) / 退回。
    用户的复检脚本正是按禁语抓这句话的（`backend/_probe_tmp/q_audit_check.py`：
    「定稿不许谎报已审计」把 `R3 通过` 列为禁语）。
    """
    items = [r for r in (rounds or []) if isinstance(r, dict)]
    items.sort(key=lambda x: x.get("round") or 0)
    if confirmed:
        return "、".join("R%s 通过" % r.get("round") for r in items) or "尚未开始"
    out = []
    for r in items:
        n = r.get("round")
        if not r.get("passed"):
            out.append("R%s 退回" % n)
            continue
        src = answered_by_of(r)
        out.append("R%s %s" % (n, {"human": "通过", "script": "脚本代答",
                                   "auto": "自动放行"}.get(src, "已放行(来源未记录)")))
    return "、".join(out) or "尚未开始"


def audit_honesty(meta) -> dict:
    """审计身份的**唯一判据** —— 什么时候才配印「已审计定稿」。

    条件只有一条：**R1/R2/R3 三轮都有记录、都 `passed`、且每一轮都写明是 `human`
    答的**。除此之外一律按「未审计」处理，并把原因逐条说出来：

    - 缺轮次（老计划只存了 R1、R2）→ 未审计；
    - `answered_by` 不是 human（脚本代答 / 系统自动）→ 未审计；
    - `answered_by` 缺失（第 36 轮之前的老计划）→ 未审计（**不默认人工**）；
    - `meta.audit_status` 自称「已审计」但拿不出上述记录 → **不采信自称**。

    返回的 `rounds` / `round_text` 只描述"数据里真的有什么"，`reasons` 是给用户看的
    「为什么不能印已审计」。交付侧（Word 页头、总览、看板）一律读这里，不许各写一套。
    """
    m = meta if isinstance(meta, dict) else {}
    raw = m.get("audit_rounds")
    rounds = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    by_round = {}
    for r in rounds:
        try:
            by_round[int(r.get("round"))] = r
        except (TypeError, ValueError):
            continue

    reasons, human_rounds = [], 0
    for n in AUDIT_ROUNDS_REQUIRED:
        entry = by_round.get(n)
        name = AUDIT_ROUND_NAMES.get(n, "第 %d 轮" % n)
        if entry is None:
            reasons.append("R%d 没有审计记录（%s）" % (n, name))
            continue
        if not entry.get("passed"):
            reasons.append("R%d 未通过（%s，用户退回）" % (n, name))
            continue
        src = answered_by_of(entry)
        if src == "human":
            human_rounds += 1
        elif src == "unknown":
            reasons.append("R%d 缺少「谁答的门」记录（%s）—— "
                           "老计划或非人工通道，不能算人工复审" % (n, name))
        else:
            reasons.append("R%d 由%s，不是人工复审（%s）"
                           % (n, _SOURCE_TEXT.get(src, src), name))

    confirmed = (human_rounds == len(AUDIT_ROUNDS_REQUIRED))
    claimed = str(m.get("audit_status") or "").strip()
    if claimed == "已审计" and not confirmed:
        reasons.insert(0, "计划数据自称「已审计」，但拿不出三轮人工通过的记录 → 按未审计处理")
    return {
        "confirmed": confirmed,                       # True 才允许印「已审计定稿」
        "status": "已审计" if confirmed else "未审计",
        "rounds": rounds,
        "human_rounds": human_rounds,
        "round_text": _audit_round_text(rounds, confirmed),
        "reasons": reasons,
        "claimed_status": claimed or "未审计",
    }

# ==================== 门的对话：提问 ≠ 修改意见 ====================
#
# 真实用户反馈：他在【R1】门里输入了一句**疑问**「我选择的不是五层为一组吗」，
# 系统把它当成审计意见 —— 计划被标「未审计」，流水线却照常把定额/配员/排程全算完
# （"明明返回了意见，却还在做定额"），而【R2】被静默跳过、一路跑到终稿确认门
# （"不管我输入什么都直接跳到最后一个门"）。
#
# 所以本门把输入分成三类：疑问句（解释 + 再问）/ 修改意见（给三条出路）/ 认不出。
# 阈值一律"保守优先"：拿不准就按**意见**处理（那才是老行为）。
QUESTION_MAX_ASKS = 3     # 同一轮里疑问句最多解释几次；之后转成"意见"菜单，绝不无限循环
MENU_MAX_ROUNDS = 3       # 菜单最多给几次（"[3] 返回重填"用掉一次）；之后按 [2] 保守处理

# 以 ？/?/吗/呢 结尾 —— 最强信号（"我选择的不是五层为一组吗" 走的就是这条）
_RE_Q_TAIL = re.compile(r"[？?]\s*$|[吗呢]\s*[。.！!]?\s*$")
# 明确的疑问词：出现就按提问算（"为什么不能改成 18 层"是问，不是修改意见）
_RE_Q_STRONG = re.compile(r"为什么|为何|是不是|难道|什么意思|啥意思|怎么回事|咋回事|是否")
# 弱疑问词：只有**没有**"要我改"的指令时才算提问
# （"怎么改"= 提问；"怎么把工期缩短"= 要我改 → 按意见处理）
_RE_Q_WEAK = re.compile(r"怎么|如何|哪个|哪一个|哪些|有没有|能否")
# "要我改"的指令信号：改动动词 + 前面的请求/祈使标记（要/请/把/麻烦…）
_RE_CHANGE_INTENT = re.compile(
    r"(要|请|帮我|给我|麻烦|把|得|应该|必须|需要|建议)[^，。；？！,;?!]{0,10}"
    r"(改|修改|调整|换成|换为|展开|补齐|补充|增补|补到|补上|加上|增加|减少|删掉|删除|"
    r"去掉|加大|缩小|重做|细化|拆细|合并|拉长|缩短|提前|延后|上调|下调)")


def looks_like_question(text):
    """这句话像不像**提问**（而不是审计意见）？

    保守优先 —— 只有"确实像疑问"才返回 True：
      · 以 ？/?/吗/呢 结尾：算（最强信号）；
      · 含"为什么/是不是/难道/什么意思/怎么回事/是否"：算（这些词本身就在问）；
      · 含"怎么/如何/哪个/哪些/有没有/能否"，但**没有**"要/请/把…改/展开/补齐"这类
        改动指令：算；
      · 其余（含含糊不清的）→ False → 按审计意见处理（最保守，与老行为一致）。

    已知误判与代价（宁可漏判成"意见"——那只是多给一个菜单，也不静默吞掉修改意见）：
      · "为什么钢筋这么少，请补到 8000 吨" → 判成提问：用户会看到解释并被再问一次，
        重打一遍即可，而且消息里明说"不当成审计意见"，不会让人以为已经记下了；
      · "怎么把工期缩短" → 含改动指令 → 判成意见 → 菜单（[3] 可返回重填）。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if _RE_Q_TAIL.search(t):
        return True
    if _RE_Q_STRONG.search(t):
        return True
    if _RE_Q_WEAK.search(t) and not _RE_CHANGE_INTENT.search(t):
        return True
    return False


def _choice_of(text):
    """菜单态下认 [1]/[2]/[3]（容错 `1` / `[1]` / `（2）` / `3.`）。认不出回 None。"""
    m = re.match(r"^[\[（(]?\s*([123])\s*[\]）)]?[.。、]?$", str(text or "").strip())
    return int(m.group(1)) if m else None


def _is_option_noise(text):
    """像"编号敲错了"而不是一句意见：纯数字/括号/标点（`9`、`[4]`、`1.2` 之外的空串）。

    只用来决定"记账时记哪一句"：菜单态下认不出的输入一律走 [2]（保守），
    但记进 `audit_comments` 的应该是那条**真正的意见**，不是敲错的编号。
    """
    t = str(text or "").strip()
    return not t or bool(re.match(r"^[\d\s\[\]（）()【】.。、,，;；:：!！?？-]+$", t))


def display_granularity_labels(ctx):
    """用户选的展示粒度标签 —— **只用** `quantity` 里现成的 DEPTH_LABELS / FLOOR_LABELS。

    绝不自己造中文粒度名；取不到 / 值不认识 → 对应标签返回 ""，调用方**整节不提**。
    """
    g = _ctx_get(ctx, "display_granularity")
    if not isinstance(g, dict) or not g:
        return "", ""
    depth = quantity.DEPTH_LABELS.get(_text_of(g.get("depth")), "")
    grouping = quantity.FLOOR_LABELS.get(_text_of(g.get("floor_grouping")), "")
    return depth, grouping


def _leaves(wbs):
    return [l for ph in (wbs.get("phases") or [])
            for wp in (ph.get("work_packages") or [])
            for l in (wp.get("sub_packages") or [])
            if isinstance(l, dict)]


# 展示细度：库里叫 L3 / L4，用户不认识这两个编号 —— 门里补一句中文
# （"工序级（细）" 由 terminal/quantity 的标签给，这里只补"是什么级别"）。
_LEVEL_TEXT = {"L3": "工种级（L3）", "L4": "工序级（L4）"}


def _level_text(level):
    raw = _text_of(level) or "L4"
    return _LEVEL_TEXT.get(raw.upper(), raw)


def _fmt_num(v):
    """整数优先的展示数字（不进位、不带 .0）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return "{:,}".format(int(round(f))) if abs(f - round(f)) < 1e-9 else "{:,.2f}".format(f)


def scope_conformance_payload(ctx):
    """R1 用：知识库范围一致性核对（**在 R1 现场重算**，不是只读上游的旧结论）。

    为什么必须重算：`beat_build` 在 `wbs_agent` 的下游，节拍型的 4 个阶段是它按
    `beat_configs` 展开的，**完全不经过 WBS 生成的提示词**。所以"范围违规"最典型的
    来源（配置里硬编码了一条被结构形式排除的工序）只在这一步之后才存在于树里。
    只读 `ctx["kb_scope_conformance"]`（wbs_agent 那一刻的结论）会漏掉它。

    返回 `(text_or_None, payload_dict_or_None)`；没有可用范围时返回 `(None, None)`，
    门照常走老路径（只增不改）。
    """
    scope = ctx.get("kb_scope")
    if not isinstance(scope, dict) or not scope.get("l4_candidates"):
        return None, None
    try:
        res = check_scope_conformance(ctx.get("wbs"), scope)
    except Exception:                      # noqa: BLE001 — 核对失败不能把审计门搞挂
        return None, None
    # 把 R1 现场的重算结果写回 ctx：`plan_json.meta` 与交付物要的是**最终树**的结论
    # （beat_build 之后的），而不是 wbs_agent 那一刻的。
    ctx["kb_scope_conformance"] = res
    ctx["scope_violations"] = [
        "%s（%s）：%s 挂了 %s，%s" % (it.get("phase") or "未知阶段",
                                     it.get("wp") or "未知工作包",
                                     it.get("leaf_name") or "一条叶子",
                                     it.get("activity_id"),
                                     it.get("reason") or it.get("kind_label") or "")
        for it in (res.get("issues") or [])]
    n = int(res.get("violations") or 0)
    lines = ["  · 知识库范围一致性：%s" % (res.get("summary") or "（无结论）")]
    for it in (res.get("issues") or [])[:5]:
        lines.append("      - [%s] %s / %s：%s 挂了 %s（%s）"
                     % (it.get("kind_label"), it.get("phase") or "?",
                        it.get("wp") or "?", it.get("leaf_name") or "一条叶子",
                        it.get("activity_id"), it.get("reason") or ""))
    if n:
        lines.append("    ⚠ 这些工序不在本工程「建筑类型 + 结构形式」允许的范围内"
                     "（多为节拍配置里写死的工序，不经过 WBS 提示词的过滤）。"
                     "要改就在本门输入意见退回（改完重跑），"
                     "或认可现状打 Y 继续（结论会留档在计划的 meta 里）。")
    return "\n".join(lines), {"kb_scope_conformance": res}


def wbs_highlights(ctx):
    """R1 用：WBS 结构摘要（纯确定性，不调 LLM）。"""
    wbs = ctx.get("wbs") or {}
    phases = wbs.get("phases") or []
    leaves = _leaves(wbs)
    per_phase = []
    for ph in phases:
        n = len([l for wp in (ph.get("work_packages") or [])
                 for l in (wp.get("sub_packages") or []) if isinstance(l, dict)])
        per_phase.append((str(ph.get("phase") or "?"), n,
                          len(ph.get("work_packages") or [])))
    per_phase.sort(key=lambda t: -t[1])
    # 工程量按单位汇总（让用户一眼看出量级是否合理）
    by_unit = {}
    for l in leaves:
        unit = str(l.get("unit") or "项")
        by_unit[unit] = by_unit.get(unit, 0.0) + float(l.get("quantity") or 0)
    plan_level = _level_text(ctx.get("plan_level") or "L4")
    lines = [
        "【第 1 轮 · WBS 结构审计】（WBS = 任务分解结构）",
        "  阶段 %d 个 ｜ 工作包 %d 个 ｜ 叶子任务 %d 条 ｜ 展示细度 %s"
        % (len(phases), sum(len(ph.get("work_packages") or []) for ph in phases),
           len(leaves), plan_level),
        "  各阶段行数：" + "；".join("%s %d 条" % (name, n) for name, n, _ in per_phase[:8]),
        "  工程量合计：" + "；".join("%s %s" % (_fmt_num(v), u)
                                    for u, v in sorted(by_unit.items(), key=lambda kv: -kv[1])[:6]),
    ]
    thin = [name for name, n, _ in per_phase if n == 0]
    if thin:
        lines.append("  ⚠ 以下阶段没有叶子任务，请重点核对：%s" % "、".join(thin[:6]))
    return "\n".join(lines), {
        "phases": len(phases), "leaves": len(leaves), "plan_level": plan_level,
        "per_phase": per_phase, "by_unit": by_unit, "empty_phases": thin,
    }


def schedule_highlights(ctx):
    """R2 用：两版工期摘要（含定额覆盖率与目标判定）。"""
    sv = ctx.get("schedule_versions") or {}
    th = (sv.get("theory_min") or {})
    ok = (sv.get("resource_ok") or {})
    cmp_ = sv.get("compare") or {}
    cov = ctx.get("norm_coverage") or {}
    lines = [
        "【第 2 轮 · 两版工期审计】",
        "  理论最短 %s 天 ｜ 资源不超额 %s 天 ｜ 相差 %s 天"
        % (_fmt_num(th.get("total_duration_days")), _fmt_num(ok.get("total_duration_days")),
           _fmt_num(cmp_.get("delta_days") or 0)),
        "  人工峰值 %s 人 ｜ 机械峰值 %s 台"
        % (_fmt_num(ok.get("peak_labor") or 0), _fmt_num(ok.get("peak_equipment") or 0)),
    ]
    if cov:
        lines.append("  定额口径覆盖率 %.1f%%（%d/%d 条有据可查）"
                     % (cov.get("bound_pct") or 0.0, cov.get("bound") or 0,
                        cov.get("total") or 0))
        for reason, n in sorted((cov.get("by_reason") or {}).items(), key=lambda kv: -kv[1])[:3]:
            lines.append("    未纳入：%s %d 条（%.1f%%）"
                         % (reason, n, (cov.get("by_reason_pct") or {}).get(reason) or 0.0))
    if cmp_.get("target_verdict"):
        lines.append("  用户目标 %s 天 → 判定「%s」"
                     % (_fmt_num(cmp_.get("user_target")), cmp_.get("target_verdict")))
    elif cmp_.get("target_note"):
        lines.append("  用户目标：" + str(cmp_["target_note"])[:80])
    warns = sv.get("warnings") or []
    if warns:
        lines.append("  口径与风险提示 %d 条（终端/看板可查全文），其中：%s"
                     % (len(warns), str(warns[0])[:70]))
    return "\n".join(lines), {
        "theory_min_days": th.get("total_duration_days"),
        "resource_ok_days": ok.get("total_duration_days"),
        "delta_days": cmp_.get("delta_days"),
        "peak_labor": ok.get("peak_labor"),
        "norm_coverage": cov,
        "target_verdict": cmp_.get("target_verdict"),
    }


# ==================== §D 冻结契约：门的结构化产物字段 ====================
#
# 这些函数是**纯取数**：只读 ctx / 节点已有产物，不重算任何工程量或工期。
# 每一个都可能返回 None（拿不到就不带该字段），调用方一律 try/except 兜住。

def _num_or(value, default=0):
    """数字原样返回（int 仍是 int，前端不会看到 12.0），非法/NaN/inf 退回 default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _dict_or(value):
    return value if isinstance(value, dict) else {}


def _list_or(value):
    return value if isinstance(value, list) else []


def _ctx_get(ctx, key):
    """容错取键：ctx 不是 dict（None / 烂数据）也要能安全过 —— 门绝不因取数而崩。"""
    return ctx.get(key) if isinstance(ctx, dict) else None


def _text_of(value, default=""):
    return default if value is None else str(value)


def _leaf_item(leaf):
    """§D1 的一条叶子：id / name / qty / unit / duration_days（+ 有锚定才带 kb_activity_id）。

    为什么缺 kb_activity_id 时**不带这个键**而不是填 null：界面是"缺失即优雅退化"，
    给它一个 null 反而多一种要判的情况。
    """
    leaf = _dict_or(leaf)
    lid = _text_of(leaf.get("id"))
    item = {
        "id": lid,
        "name": _text_of(leaf.get("name")) or lid,
        "qty": _num_or(leaf.get("quantity")),
        "unit": _text_of(leaf.get("unit")),
        "duration_days": _num_or(leaf.get("duration_days")),
    }
    kb_id = leaf.get("kb_activity_id")
    if kb_id:
        item["kb_activity_id"] = _text_of(kb_id)
    return item


def wbs_tree_payload(wbs):
    """§D1 WBS 树：阶段 → 工作包 → 叶子，供用户"看着实物"决定结构是否可继续。

    限流：最多 `WBS_TREE_MAX_PHASES` 个阶段、总计最多 `WBS_TREE_MAX_LEAVES` 条叶子，
    **按阶段顺序从头截取**（用户看到的是计划的开始部分），其余用 `truncated_leaves`
    如实报数。取不到 phases → None（事件里不带 wbs_tree）。

    R1 门与 WBS 复评人工门共用这**唯一一份**实现 —— 两处给用户的必须是同一棵树。
    """
    if not isinstance(wbs, dict):
        return None
    phases_in = wbs.get("phases")
    if not isinstance(phases_in, list) or not phases_in:
        return None

    # counts 报的是**全量**规模（未截断），用户才知道树到底多大
    total_wps = 0
    total_leaves = 0
    for ph in phases_in:
        for wp in _list_or(_dict_or(ph).get("work_packages")):
            total_wps += 1
            total_leaves += len([l for l in _list_or(_dict_or(wp).get("sub_packages"))
                                 if isinstance(l, dict)])

    phases_out = []
    shown = 0
    for ph in phases_in[:WBS_TREE_MAX_PHASES]:
        if not isinstance(ph, dict):
            continue
        wps_out = []
        for wp in _list_or(ph.get("work_packages")):
            if shown >= WBS_TREE_MAX_LEAVES:
                break
            wp = _dict_or(wp)
            leaves_out = []
            for leaf in _list_or(wp.get("sub_packages")):
                if shown >= WBS_TREE_MAX_LEAVES:
                    break
                if not isinstance(leaf, dict):
                    continue
                leaves_out.append(_leaf_item(leaf))
                shown += 1
            wps_out.append({"id": _text_of(wp.get("id")),
                            "name": _text_of(wp.get("name")),
                            "leaves": leaves_out})
        phases_out.append({"phase": _text_of(ph.get("phase"), "?"),
                           "work_packages": wps_out})
        if shown >= WBS_TREE_MAX_LEAVES:
            break

    return {
        "counts": {"phases": len(phases_in), "work_packages": total_wps,
                   "leaves": total_leaves},
        "phases": phases_out,
        "shown_leaves": shown,
        "truncated_leaves": max(0, total_leaves - shown),
    }


# ==================== §D1 前置：展示粒度口径说明（第 23 轮） ====================
def _granularity_of(ctx):
    """取用户选定的展示粒度 → `(depth, grouping)`；没选过 / 值不合法 → None。

    优先 `ctx["display_granularity"]`（计划细度门当场写下的），回退
    `plan_json.meta.display_granularity`（修订重跑时 ctx 里只剩计划那一份）。
    两处都没有 → None（**不瞎写**，界面保持现状）。
    """
    ctx = _dict_or(ctx)
    g = _dict_or(ctx.get("display_granularity"))
    if not g:
        g = _dict_or(_dict_or(_dict_or(ctx.get("plan_json")).get("meta"))
                     .get("display_granularity"))
    if not g:
        return None
    depth, grouping = g.get("depth"), g.get("floor_grouping")
    if depth not in quantity.DEPTHS or grouping not in quantity.FLOOR_GROUPINGS:
        return None
    return depth, grouping


def granularity_caliber_note(ctx):
    """R1 门 WBS 树**前面**的口径说明：你选了什么、本门为什么按原始 WBS 展示、
    交付物会按你的粒度合并成几行。

    真实缺陷（用户实测原话）："为什么这里明明选择的是五层一组，主体结构还是返回的一层
    一段？到底有没有采用用户输入的内容？" —— 选择其实**生效了**（只影响交付物展示行的
    合并，见 `delivery.group_rows`），可 R1 门里一个字都没提：用户看到原始结构的逐层
    叶子，只能怀疑自己白填了。这段说明就是把那层账摆到台面上。

    三条铁律照旧：只读 ctx 里已有的产物；行数用 `quantity.estimate_rows_for` **真算**
    （不写死、不估算 —— 它也是 `group_rows` 的行数口径）；语言标签取现成的
    `quantity.DEPTH_LABELS` / `FLOOR_LABELS`，不自己造中文。
    没有 display_granularity（老数据 / 没经过细度门）或树是空的 → 返回 None。
    """
    picked = _granularity_of(ctx)
    if not picked:
        return None
    depth, grouping = picked
    wbs = _dict_or(_dict_or(ctx).get("wbs"))
    try:
        leaves = int((quantity.count_rows(wbs) or {}).get("rows") or 0)
        rows = int(quantity.estimate_rows_for(wbs, depth, grouping))
    except Exception:  # noqa: BLE001 — 算不出行数就不显示这段，绝不抛
        return None
    if leaves <= 0:
        return None
    return [
        "展示粒度：%s × %s（你刚才在计划细度门的选择）"
        % (quantity.DEPTH_LABELS.get(depth, depth),
           quantity.FLOOR_LABELS.get(grouping, grouping)),
        "本门按原始 WBS 展示：%d 条叶子，一条一行 —— 审计要看真实结构，"
        "不拿合并后的视图糊弄你。" % leaves,
        "交付物里会按这个粒度合并成 %d 行（Word 工序表 / 看板横道图）；"
        "工程量、工期、资源一律不变。" % rows,
    ]


def _wbs_task_names(wbs):
    """task_id → 叶子名。R2 跑在 assembler 之前、plan_json 还没组装，名字只能从 WBS 取。"""
    out = {}
    for ph in _list_or(_dict_or(wbs).get("phases")):
        for wp in _list_or(_dict_or(ph).get("work_packages")):
            for leaf in _list_or(_dict_or(wp).get("sub_packages")):
                leaf = _dict_or(leaf)
                if leaf.get("id"):
                    out[_text_of(leaf["id"])] = _text_of(leaf.get("name"))
    return out


def _version_block(version):
    """§D2 一版工期：天数 / 叶子数 / 关键任务数 / 人工峰值 —— 全部取自 ctx 里的排程结果。"""
    version = _dict_or(version)
    sched = version.get("schedule") if isinstance(version.get("schedule"), list) else []
    crit = version.get("critical_path") if isinstance(version.get("critical_path"), list) else []
    return {
        "total_duration_days": _num_or(version.get("total_duration_days")),
        "leaves": len(sched),
        "critical": len(crit),
        "peak_labor": _num_or(version.get("peak_labor")),
    }


def _labor_note_of(versions):
    """§D2 labor_note：**原样取用** ctx 里既有的口径警告文本（决不在门里另算一份）。"""
    for warning in (_dict_or(versions).get("warnings") or []):
        text = _text_of(warning).strip()
        if "定额工日需求合计" in text:
            # 只砍掉"——"之后那段解释（口径全文仍在 warnings 里，终端会另打）
            return text.split("——", 1)[0].strip()
    return None


def _has_user_limits(ctx):
    """用户到底有没有给资源限额。判不出来 → None（措辞会退一步，不硬说"两版一致"）。"""
    try:
        from .scheduler import parse_boundary_limits
        limits = parse_boundary_limits(_ctx_get(ctx, "boundary_conditions")
                                      or _ctx_get(ctx, "boundary") or {})
        return bool(limits.get("labor_total") or limits.get("by_trade")
                    or limits.get("equipment"))
    except Exception:
        return None


def schedule_compare_payload(ctx):
    """§D2 两版工期对比（R2 门）。`schedule_versions` 取不到 → None。"""
    versions = _ctx_get(ctx, "schedule_versions")
    if not isinstance(versions, dict) or not versions:
        return None
    theory = _version_block(versions.get("theory_min"))
    resource_ok = _version_block(versions.get("resource_ok"))
    compare = _dict_or(versions.get("compare"))
    delta = _num_or(compare.get("delta_days"))

    # 最长任务取自**交付版**（resource_ok）；它没有排程行时退回理论版
    base = versions.get("resource_ok")
    if not (_dict_or(base).get("schedule")):
        base = versions.get("theory_min")
    names = _wbs_task_names(_ctx_get(ctx, "wbs"))
    rows = []
    for row in (_dict_or(base).get("schedule") or []):
        if not isinstance(row, dict):
            continue
        tid = _text_of(row.get("task_id"))
        es, ef = _num_or(row.get("es")), _num_or(row.get("ef"))
        rows.append({
            "task_id": tid,
            "task_name": names.get(tid) or tid,   # 查不到名字就退回编号，绝不编名字
            "days": ef - es,
            "es": es,
            "ef": ef,
            "crew": dict(_dict_or(row.get("crew"))),
            "capped": bool(row.get("capped")),
        })
    # 工期降序；并列时按 task_id 排序，保证同一输入两次运行结果完全一致
    rows.sort(key=lambda r: (-r["days"], r["task_id"]))

    same = (theory["total_duration_days"] == resource_ok["total_duration_days"]
            and theory["peak_labor"] == resource_ok["peak_labor"])
    has_limits = _has_user_limits(ctx)
    if has_limits is True:
        limit_note = ("已按用户给的资源限额封顶：资源不超额版取「工作面容量 / 用户限额」"
                      "的较小值排程，两版相差 %s 天" % _fmt_num(delta))
    elif has_limits is False:
        limit_note = ("用户未给资源限额，两版一致" if same
                      else "用户未给资源限额，两版差异来自工作面容量（相差 %s 天）"
                           % _fmt_num(delta))
    elif same:
        limit_note = "两版结果一致（未取到资源限额口径，按现有排程结果如实对比）"
    else:
        limit_note = ("资源不超额版比理论最短多 %s 天（受资源限额 / 工作面容量约束）"
                      % _fmt_num(delta))

    payload = {
        "theory_min": theory,
        "resource_ok": resource_ok,
        "limit_note": limit_note,
        "top_tasks": rows[:SCHEDULE_TOP_TASKS],
    }
    labor_note = _labor_note_of(versions)
    if labor_note:
        payload["labor_note"] = labor_note
    return payload


def _draft_view(plan):
    """复用**草案自己**的统计视图（delivery._compute_view），保证行数与 Word 一致。

    这是一次"取已有产物"而非重算：横道行数、工种数、设备数都由它给出；
    导入失败/抛异常 → (None, "", "")，调用方退化成直接点数 plan_json 的集合长度。
    """
    try:
        from .delivery import _compute_view, granularity_note, params_banner
        view = _compute_view(plan)
        return view, (_text_of(params_banner(plan))), (_text_of(granularity_note(plan)))
    except Exception:
        return None, "", ""


def _report_line_count(report):
    return len([l for l in _text_of(report).splitlines() if l.strip()])


def draft_outline_payload(ctx):
    """§D3 草案目录（R3 门）：章节（标题 + 行数）、表格（标题 + 行数）、图清单、覆盖率。

    章节骨架与行数口径**镜像** `delivery.build_plan_docx`（草案 Word 的唯一生成处）：
    标题是它 `add_h()` 的原文，行数是该章节在草案里的内容行/表格行数。
    取不到 `plan_json` → None（R3 门就不带该字段，界面优雅退化）。

    `figures` 报的是"确认后将画出的看板图"：草案本身**不含图表**（这正是本轮的语义）。
    所有具体数字都来自 plan_json / norm_coverage，没有一处新算。
    """
    plan = _ctx_get(ctx, "plan_json")
    if not isinstance(plan, dict) or not plan:
        return None

    meta = _dict_or(plan.get("meta"))
    milestones = plan.get("key_milestones") or []
    risks = plan.get("risks") or []
    tasks = plan.get("all_tasks_schedule") or []
    view, banner, granularity = _draft_view(plan)

    if isinstance(view, dict):
        gantt_rows = len(view.get("gantt") or [])
        trades = sorted({k for x in (view.get("labor_daily") or [])
                         for k in _dict_or(x).get("trades", {})})
        equips = sorted({k for x in (view.get("equip_daily") or [])
                         for k in _dict_or(x).get("items", {})})
    else:
        gantt_rows = len(tasks)
        trades = sorted({k for t in tasks
                         for k in _dict_or(_dict_or(t).get("assigned_resources"))})
        equips = []

    # 一、计划总览：8 行键值表 + 若干说明段（编制口径 / 参数横幅 / 细度说明 / 审计意见）
    overview_rows = 8
    overview_rows += 1 if meta.get("caliber_note") else 0
    overview_rows += 1 if banner else 0
    overview_rows += 1 if granularity else 0
    overview_rows += len(list(meta.get("audit_comments") or [])[:3])

    sections = [
        {"title": "一、计划总览", "lines": overview_rows},
        {"title": "二、关键里程碑", "lines": len(milestones)},
        {"title": "三、横道图（甘特排程，★=关键路径）", "lines": gantt_rows},
        {"title": "四、人员配置（各工种总工日 / 峰值）", "lines": 3 + len(trades)},
        {"title": "五、设备资源荷载（峰值）", "lines": len(equips) or 1},
        {"title": "六、流水施工组织与季节性保障", "lines": 2 + len(risks)},
    ]
    if risks:                       # 第七章只在有风险时出现在草案里，目录必须跟着
        sections.append({"title": "七、主要风险与应对", "lines": len(risks)})
    sections.append({"title": "八、施工监督报告", "lines": _report_line_count(plan.get("report"))})

    tables = [
        {"title": "计划总览（键值表）", "rows": 8},
        {"title": "横道图（任务 / 开始 / 完成 / 工期 / 关键）", "rows": gantt_rows},
        {"title": "人员配置汇总（峰值 / 峰值工种 / 总人·日）", "rows": 3},
    ]
    if trades:
        tables.append({"title": "人员配置（分工种累计人·日）", "rows": len(trades)})
    if equips:
        tables.append({"title": "设备资源荷载（单日峰值 / 累计台·日）", "rows": len(equips)})

    figures = ["甘特图", "人员配置曲线"]
    if equips:
        figures.append("设备资源荷载图")

    payload = {
        "sections": sections,
        "tables": tables,
        "figures": figures,
        "note": "草案未审计、不含图表；通过后才出定稿与看板",
    }
    cov = _ctx_get(ctx, "norm_coverage")
    if isinstance(cov, dict) and cov.get("total"):
        payload["coverage"] = "定额口径覆盖率 %.1f%%（%d/%d）" % (
            cov.get("bound_pct") or 0.0, cov.get("bound") or 0, cov.get("total") or 0)
    return payload


class _AuditGate(BaseNode):
    """三轮回审门的公共实现（子类只需给 name/title/round/summary 函数）。"""

    round_no = 0
    round_name = ""
    next_hint = ""

    # 引擎的警告上行是 **opt-in**（见 `engine.Pipeline._done_payload`）：声明这个键之后，
    # `ctx[warning_ctx_key]` 里的通知会随 node_done 一起发出去。为什么本门需要它：
    # "某个审计门被跳过"是**重要状态变化**，而默认折叠策略只在 node_done 带警告时才打印 ——
    # 用户实测就是"不管输入什么都直接跳到最后一个门"，因为跳过只写进 done_summary。
    warning_ctx_key = "audit_gate_notices"
    #: 本轮审计「谁答的门」；每次 `run()` 里从 resolve 载荷刷新（见 `answered_by_of`）。
    #: 默认 `unknown` —— 没拿到证据就不算人工。
    answered_by = "unknown"

    def _summary(self, ctx):
        raise NotImplementedError

    def _extra(self, ctx):
        """§D 结构化产物字段（只增不改）。

        返回 {字段名: 值}；值是 None 的**不进事件**（界面优雅退化）。子类按轮次覆写。
        这里默认空 —— 门不会因为取数失败而崩，也不会因为拿不到数据而少掉摘要。
        """
        return {}

    def run(self, ctx):
        ctx = ctx or {}
        # 前序轮次已经退回 → 后面的轮次不必再打扰用户（计划照常往下走，见类注释）。
        # 但"跳过"必须**看得见**：否则用户只会看到"不管输入什么都跳到最后一个门"。
        if ctx.get("audit_rejected") and self.round_no < 3:
            return self._skip_after_reject(ctx)

        pending = ""            # 已判定为「修改意见」的原话（菜单态）
        asked = 0               # 疑问句已经解释过几次
        menu_shown = 0          # 菜单已经给过几次
        show_explain = False    # 下一帧要不要先打解释（上一句被判定成提问）

        while True:
            review_id = "au%d_%s_%s" % (self.round_no, getattr(self, "_run_id", "run"),
                                        uuid.uuid4().hex[:4])
            self._registry.register(review_id)
            try:
                text, highlights = self._summary(ctx)
            except Exception as exc:                 # 摘要算不出来不能让门挂掉
                text, highlights = "【第 %d 轮 · %s】摘要生成失败：%s" % (
                    self.round_no, self.round_name, exc), {}
            notice = self._notice_text(ctx, pending, show_explain)
            self.emit(EV_PARAM_REVIEW,
                      self._payload(ctx, review_id, text, highlights, notice,
                                    menu=bool(pending)))
            self.emit("node_progress", {"node": self.name, "progress": 100,
                                        "message": "等待用户审计（第 %d 轮：%s）"
                                                   % (self.round_no, self.round_name)})

            decision = self._registry.wait(
                review_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)
            # 谁答的这道门：必须在**任何 return 之前**记下来 —— `_mark` 的通过路径与
            # 两条退回路径都要把它写进审计链（见 `answered_by_of` 的说明）。
            self.answered_by = answered_by_of(decision)
            # ⚠️ 手输 `/abort` 也必须真的中止：老实现只认 `action == "abort"`，于是
            # `/abort` 掉到下面被当成**审计意见**（计划被标「未审计」）—— 用户以为停了。
            # 与文件门 / 参数门共用 `boundary.is_abort_decision`（同一份判定）。
            # 文案保留"取消"：注册表给的是 `action=abort`（超时/被取消），老终端与测试
            # 都按"取消"这个词读；用户手输中止走下面那条更具体的说明。
            if is_abort_decision(decision):
                if decision.get("action") == "abort":
                    self.done_summary = "用户在审计门取消"
                    return {"_stop": "用户在第 %d 轮审计（%s）取消了本次运行"
                                     % (self.round_no, self.round_name)}
                self.done_summary = "用户输入中止指令，已停止"
                return {"_stop": "用户在第 %d 轮审计（%s）中止了本次运行"
                                 % (self.round_no, self.round_name)}

            if decision.get("passed"):
                self.done_summary = "第 %d 轮审计（%s）通过" % (self.round_no, self.round_name)
                return self._mark(ctx, passed=True, comment="")

            comment = str(decision.get("manual_input") or "").strip()

            # 空输入（EOF / Ctrl+C 兜底路径）：保持老行为 —— 记一次"未审计"继续跑。
            # 终端本身不会把空输入发上来（它会重新问），所以这里不需要菜单。
            if not comment:
                if self.round_no >= 3:
                    return self._reject_stop(ctx, pending)
                return self._reject_and_continue(ctx, pending)

            # ① 疑问句：不当成审计意见 —— 解释清楚再问一次（不记账 / 不计轮次）
            if looks_like_question(comment) and asked < QUESTION_MAX_ASKS:
                asked += 1
                show_explain = True
                continue

            # ② 菜单态：用户在 [1] 停 / [2] 继续 / [3] 重填 里选一条
            if pending:
                choice = _choice_of(comment)
                show_explain = False
                if choice == 1:
                    self.done_summary = ("第 %d 轮审计（%s）：按你的选择停止本次运行，"
                                         "不自作主张往下算"
                                         % (self.round_no, self.round_name))
                    return {"_stop": self._stop_message(pending)}
                if choice == 3:
                    pending = ""                      # 返回重填：这一轮从头再问
                    continue
                if choice == 2:
                    return self._reject_and_continue(ctx, pending)
                # 认不出的输入（编号越界 / 乱码 / 又写了一句新意见）→ 保守：按 [2]。
                # 记进审计链的是**那句真正的意见**：用户又写了新句子就记新的，否则记菜单里
                # 原来那条（把 "9" 这种越界编号记成"审计意见"没有意义）。
                return self._reject_and_continue(
                    ctx, pending if _is_option_noise(comment) else comment)

            # ③ 修改意见
            if self.round_no >= 3:
                # 第 3 轮后面就是定稿 Word 与看板 —— 立刻停（与老行为逐字一致）
                return self._reject_stop(ctx, comment)
            if menu_shown >= MENU_MAX_ROUNDS:
                # 连续给了几次菜单都拿不到明确选择 → 按最保守的方式处理（老行为）
                return self._reject_and_continue(ctx, comment)
            # 先把代价说清楚，再让用户选（不立刻记账）
            pending = comment
            menu_shown += 1
            show_explain = False
            continue

    # ---------- 门载荷 ----------
    def _payload(self, ctx, review_id, text, highlights, notice="", menu=False):
        """门载荷。有 `notice`（解释 / 菜单）时同时进 `summary` 与 `next_hint`。

        为什么两处都放：R1/R2 的正文是**结构化实物内容**（WBS 树 / 两版工期），
        终端渲染时 `summary` 会被正文顶掉；而 `next_hint` 是审计门**一定会打印**的
        最后一行（它本来就是"你该怎么回答"的位置），所以菜单与解释必须落在那儿。

        `menu=True`（本轮是"意见菜单"态）时额外带 `options_hint`：终端据此把输入提示
        换成「输入 1/2/3 选菜单项」——否则提示仍写「直接输入审计意见=退回」，
        用户不知道该回编号（实测反馈：菜单态提示语与菜单对不上）。**只增字段**，
        老终端不认识就继续用原来的提示语，行为不变。
        """
        payload = {
            "review_id": review_id,
            "purpose": "audit",
            "round": self.round_no,
            "round_name": self.round_name,
            "title": "第 %d 轮审计：%s" % (self.round_no, self.round_name),
            "summary": (text + "\n" + notice) if notice else text,
            "highlights": highlights,
            "next_hint": notice or self.next_hint,
        }
        if menu:
            payload["options_hint"] = ("  [输入 1 / 2 / 3 选择上面的菜单项；"
                                       "要直接通过请打 Y；abort 取消] ")
        # §D：把"用户能据以决策的实物内容"并进同一帧。取数失败 → 不带该键，
        # 但摘要与既有字段一个都不能少（界面有兼容路径）。
        try:
            for key, value in (self._extra(ctx) or {}).items():
                if value is not None:
                    payload[key] = value
        except Exception:
            pass
        return payload

    def _notice_text(self, ctx, pending, show_explain):
        """下一帧要额外打的字：疑问解释和/或菜单（都没有 → 老界面一字不变）。"""
        parts = []
        if show_explain:
            parts.append(self._question_text(ctx))
        if pending:
            parts.append(self._menu_text(ctx, pending))
        return "\n\n".join(parts)

    def _question_text(self, ctx):
        """疑问句的解释（用户实测最想知道的几件事）。

        只讲事实：本门审什么、他选的展示粒度到底影响了什么、三轮回审门各审什么。
        粒度标签取现成的 `quantity.*_LABELS`；**拿不到粒度就不提这一节**（不编默认值）。
        """
        depth, grouping = display_granularity_labels(ctx)
        lines = [
            "这一句我按【提问】回答，不当成审计意见（不记账、计划不会被标「未审计」，"
            "下面把这一轮重新问你一次）：",
            "  · 本门审的是【原始 WBS】（阶段 → 工作包 → 工序，逐层一条）："
            "审计必须看真实结构。",
        ]
        if depth and grouping:
            lines.append("  · 你选的展示粒度是「%s × %s」——它【只影响交付物里的展示行"
                         "怎么合并】，不改 WBS 结构、不改工程量与工期。"
                         % (depth, grouping))
        elif depth or grouping:
            lines.append("  · 你选的展示粒度是「%s」——它只影响交付物里的展示行怎么合并，"
                         "不改 WBS 结构、不改工程量与工期。" % (depth or grouping))
        lines += [
            "  · 三轮回审门分别在审：R1 WBS 结构 → R2 两版工期 → R3 Word 草案（不含图表）。"
            "本轮是 R%d。" % self.round_no,
            "  · " + self.next_hint,
        ]
        return "\n".join(lines)

    def _upstream_note(self):
        """本门为什么改不了上游（R1 与 R2 的措辞略有不同，但事实一样）。"""
        if self.round_no == 1:
            return "本门【无法】自动改上游的 WBS（WBS 生成节点在更前面，引擎不支持回退重跑）。"
        return ("本门【无法】自动改上游的 WBS/排程（WBS 与两版工期都在更前面算好，"
                "引擎不支持回退重跑）。")

    def _skip_consequence(self):
        """选了 [2] 继续跑之后**会发生什么**（必须如实说，不能只写"未审计"）。"""
        if self.round_no == 1:
            return "且【R2】两版工期审计门会被自动跳过"
        if self.round_no == 2:
            return "且【R3】Word 草案审计门会被自动跳过（直接停止产出定稿与看板）"
        return "且后续审计门会被自动跳过"

    def _menu_text(self, ctx, comment):
        """修改意见的菜单：把副作用摆到台面上，让用户明确选一条出路。

        关键：**在选之前**就说清 [2] 的代价（会继续算 + 后面的审计门会被跳过），
        以及 [1]（停下重跑）为什么是推荐的 —— 这正是实测里"意见被记下了却还在做定额"
        那个取舍从没告知用户的缺陷。
        """
        depth, grouping = display_granularity_labels(ctx)
        lines = [
            "你的意见：%s" % (comment or "（未填写文字意见）"),
            self._upstream_note(),
            "WBS 不改就继续算，后面套定额、配机械班组、排工期都是建在错误结构上，等于白算。",
            "请选择：",
            "  [1] 停止本次运行，带上这条意见重跑（推荐）",
            "      —— 重跑时在【WBS 复评门】用编号选项让系统自己改"
            "（按项目参数重新展开受影响的 WBS 相 / 补齐知识库里缺失的必含工程类型），"
            "比在这里写意见更省事",
            "  [2] 仍然继续跑完 —— 会继续算定额/配员/排程（约 10 个节点、数分钟），"
            "计划保持「未审计」、最终不出定稿与看板；%s" % self._skip_consequence(),
            "  [3] 返回重填（要通过请直接输入 Y）",
        ]
        if depth and grouping:
            lines.append("提示：若你其实是在问展示粒度 —— 本门审的是原始 WBS，"
                         "你选的「%s × %s」只影响交付物里的展示行怎么合并，"
                         "不改 WBS 结构与工程量/工期。" % (depth, grouping))
        return "\n".join(lines)

    # ---------- 三种收尾 ----------
    def _reject_stop(self, ctx, comment):
        """第 3 轮退回：立刻停（它后面就是定稿 Word 与看板）—— 老行为，逐字不变。"""
        self.done_summary = ("第 %d 轮审计未通过（%s）：已记下意见，计划保持「未审计」，"
                             "不出定稿与看板" % (self.round_no, self.round_name))
        self._mark(ctx, passed=False, comment=comment)
        return {"_stop": self._reject_message(comment)}

    def _reject_and_continue(self, ctx, comment):
        """R1/R2 退回（菜单里的 [2] / 认不出的输入）：记账并继续跑。

        与老版本**逐项一致**（`_mark(passed=False, comment)` + `{"audit_rejected": True}`），
        只多了一句诚实的交代：接下来还会继续算，后面的审计门会被跳过，最终不出定稿与看板。
        注意：这里**不能**带 `_stop` 键 —— 引擎判的是 `"_stop" in result`，
        哪怕值是 None 也会结束整条流水线。
        """
        self.done_summary = ("⚠ 第 %d 轮审计未通过（%s）：已记下意见，计划保持「未审计」。"
                             "接下来还会继续算定额/配员/排程，%s，最终不出定稿与看板"
                             % (self.round_no, self.round_name, self._skip_consequence()))
        self._mark(ctx, passed=False, comment=comment)
        return {"audit_rejected": True}

    def _skip_after_reject(self, ctx):
        """前序轮次已退回 → 本轮跳过；**必须让用户看得见**（真实用户反馈）。

        通道（都用既有通道，不改 `terminal/`）：
          · `done_summary` 里带 ⚠ → `renderer.has_warning()` 命中 → 默认折叠策略下
            node_done 也会完整打印（折叠只吃"没有警告"的 node_done）；
          · 声明 `warning_ctx_key` → 通知随 node_done 的 `warnings` 上行，终端会打印
            前 3 条（这是引擎为"警告只留数字、用户看不到内容"专门加的 opt-in）。
        两条一起用，避免以后单点失效时这件事又变成静默。
        """
        rejected = sorted({int(r.get("round") or 0)
                           for r in (ctx.get("audit_rounds") or [])
                           if isinstance(r, dict) and not r.get("passed")} - {0})
        source = "、".join("【R%d】" % r for r in rejected) or "前序"
        notice = ("【R%d】%s审计门：因%s已退回，本轮跳过（计划保持「未审计」）。"
                  % (self.round_no, self.round_name, source))
        self.done_summary = "⚠ " + notice + "跳过 ≠ 审过。"
        self.warning_note = ("跳过 ≠ 通过：这一轮没有向你提问，本轮该看的工期/资源问题"
                             "不会被指出；最终不会出定稿与看板。")
        notices = list(ctx.get("audit_gate_notices") or [])
        notices.append(notice)
        ctx["audit_gate_notices"] = notices
        return {"audit_rejected": True, "audit_gate_notices": notices}

    def _stop_message(self, comment):
        """[1] 停下重跑：消息必须能直接给用户看（中文、可执行、带原话）。"""
        return ("已按你的选择停止本次运行（第 %d 轮审计 · %s），不往下算。\n"
                "  你的意见：%s\n"
                "  为什么停：本门改不了上游的 WBS；WBS 不改，后面套定额、配机械班组、"
                "排工期都是白算。\n"
                "  重跑时最省事的改法：在【WBS 复评门】用编号选项让系统自己改 ——\n"
                "    · 按项目参数重新展开受影响的 WBS 阶段（工程量和工期一次改齐）\n"
                "    · 补齐知识库里缺失的必含工程类型\n"
                "  想自己写也行：重跑后仍可在本门直接输入你的 WBS 修改意见。\n"
                "  本次没有出定稿与看板，计划保持「未审计」。"
                % (self.round_no, self.round_name, comment or "（未填写文字意见）"))

    def _reject_message(self, comment):
        return ("第 %d 轮审计（%s）未通过：计划保持「未审计」，已停止产出定稿与看板。\n"
                "  你的意见：%s\n"
                "  改完再重跑即可（终端可用 /revise \"<你的意见>\" 直接落到计划上）。"
                % (self.round_no, self.round_name, comment or "（未填写文字意见）"))

    # ---------- 审计链落账 ----------
    def _mark(self, ctx, passed, comment, answered_by=None):
        """记审计结论（**含「谁答的门」**）+ 同步计划元数据 + 写回磁盘 + 落修订链。

        写回磁盘是 P0-A 的核心：老实现只改**内存**里的 `ctx["plan_json"]`，而落盘发生在
        `PlanAssemblerNode`（在三个门**之前**）。于是磁盘上的计划永远停在
        「未审计 / 只有 R1、R2」，定稿 Word 却从内存渲染出「R3 通过」——同一份计划两个
        真相，用户一查 JSON 就发现交付物在说谎。现在每走完一轮就重写一次计划数据。
        """
        if answered_by is None:
            answered_by = getattr(self, "answered_by", "unknown")
        if answered_by not in ANSWER_SOURCES:
            answered_by = "unknown"          # 传进来的值不认识 → 宁缺勿假
        rounds = list(ctx.get("audit_rounds") or [])
        entry = {"round": self.round_no, "name": self.round_name,
                 "passed": bool(passed), "answered_by": answered_by}
        if comment:
            entry["comment"] = comment
        rounds = [r for r in rounds if r.get("round") != self.round_no] + [entry]
        ctx["audit_rounds"] = rounds
        ctx["audited"] = bool(passed) and all(r.get("passed") for r in rounds) and \
            len([r for r in rounds if r.get("passed")]) >= 3
        if not passed:
            comments = list(ctx.get("audit_comments") or [])
            comments.append({"round": self.round_no, "name": self.round_name,
                             "comment": comment})
            ctx["audit_comments"] = comments
            ctx["audit_rejected"] = True

        # 计划元数据要跟着变：草案（未审计）与定稿（已审计）必须一眼能分
        plan = ctx.get("plan_json")
        if isinstance(plan, dict):
            # `setdefault` 对"键在但值是 None"不管用（会原样返回 None）→ 门会在
            # 这里崩掉，用户被卡死。畸形 plan_json 也要兜住：审计结论先落，元数据
            # 能写多少写多少。
            meta = plan.get("meta")
            if not isinstance(meta, dict):
                meta = {}
                plan["meta"] = meta
            meta["audit_rounds"] = rounds
            if ctx.get("audit_comments"):
                meta["audit_comments"] = ctx["audit_comments"]
            # ⚠️「三轮都通过」≠「三轮**人工**都通过」。脚本代答同样会让 `ctx["audited"]`
            # 为真，所以 `audit_status` 必须走 `audit_honesty`（要求每轮 answered_by 都是
            # human）—— 数据自己先撒谎，后面渲染层再诚实也没用。
            meta["audit_status"] = ("已审计" if audit_honesty(meta)["confirmed"] else "未审计")
            # ---- 写回磁盘：计划数据才是真源（见本方法 docstring）----
            self._persist_plan(plan)

        # 审计事件进修订链存档（终端 /versions 能查到"谁在第几轮审了什么"）
        plan_id = None
        plan = ctx.get("plan_json")
        if isinstance(plan, dict):
            plan_id = plan.get("plan_id") or ctx.get("plan_id") or ctx.get("_run_id")
        if plan_id:
            try:
                from ..plan_store import PlanStore
                PlanStore().log_audit(
                    str(plan_id), "R%d %s" % (self.round_no, self.round_name),
                    "通过" if passed else "退回", comment[:200])
            except Exception:
                pass                    # 存档失败不影响审计结论本身
        return {"audit_rounds": rounds, "audited": ctx["audited"]}

    @staticmethod
    def _persist_plan(plan):
        """把（含审计结论的）计划写回 `<PLANS_DIR>/<plan_id>.json`。

        与 `plan_assembler` 落盘用的是**同一个实现** —— 复用而不是另写一份，保证
        "落盘格式"只有一处定义。该实现最近被从 `PlanAssemblerNode` 挪到了
        `PlanDeliverNode`（同一文件里重构），所以这里按"谁有 `_save` 就用谁"的顺序找，
        找不到才放弃；**不复制写入逻辑**（复制出来的第二份一定会漂移）。
        延迟导入：避免 `audit_gate` ↔ `plan_assembler` 的模块级互相导入。
        落盘失败不影响审计结论本身（与 `log_audit` 同一策略），返回空串。
        """
        if not isinstance(plan, dict) or not plan.get("plan_id"):
            return ""
        try:
            from . import plan_assembler as PA
        except Exception:
            return ""
        for owner in (getattr(PA, "PlanDeliverNode", None),
                      getattr(PA, "PlanAssemblerNode", None),
                      PA):
            save = getattr(owner, "_save", None)
            if callable(save):
                try:
                    return save(plan)
                except Exception:
                    return ""
        return ""


class WBSAuditNode(_AuditGate):
    name = "audit_wbs"
    title = "第 1 轮审计：WBS 结构"
    round_no = 1
    round_name = "WBS 结构"
    next_hint = "确认结构没问题请输入 Y；要改结构请直接输入意见（或稍后用 /revise）。"

    def _summary(self, ctx):
        text, highlights = wbs_highlights(ctx)
        # 知识库范围一致性（现场重算，见 scope_conformance_payload 的注释）：
        # 有违规就是用户必须看见的一件事 —— 它决定"这份 WBS 有没有出现被明令禁止的工序"。
        scope_text, scope_payload = scope_conformance_payload(ctx)
        if scope_text:
            text = text + "\n" + scope_text
        return text, highlights

    def _extra(self, ctx):
        # §D1：R1 门必须让用户看着**树**决定结构是否可继续（限流 6 阶段 / 40 叶子）
        # 第 23 轮：树**前面**先讲清展示粒度口径 —— "我选的 5 层一组到底生效了没有"
        # 是用户在 R1 门里真实问过的问题（生效的是交付物的展示行，不是这棵树）。
        extra = {"wbs_tree": wbs_tree_payload(ctx.get("wbs")),
                 "granularity_note": granularity_caliber_note(ctx)}
        # 范围核对结果是结构化产物，一并进帧（取数已在 _summary 里做过一次，
        # 这里直接读 ctx：`_payload` 会先调 `_summary` 再调 `_extra`，顺序有保证）。
        res = ctx.get("kb_scope_conformance")
        if isinstance(res, dict) and res.get("checked"):
            extra["kb_scope_conformance"] = res
        return extra


class ScheduleAuditNode(_AuditGate):
    name = "audit_schedule"
    title = "第 2 轮审计：两版工期"
    round_no = 2
    round_name = "两版工期"
    next_hint = "认可这两版工期请输入 Y；要调整资源/班组请直接输入意见（或稍后用 /revise）。"

    def _summary(self, ctx):
        return schedule_highlights(ctx)

    def _extra(self, ctx):
        # §D2：两版对比表 + 最长 6 条任务 + 资源限额口径 + 定额工日需求（都取自 ctx）
        return {"schedule_compare": schedule_compare_payload(ctx)}


class DraftAuditNode(_AuditGate):
    name = "audit_draft"
    title = "第 3 轮审计：Word 草案（不含图表）"
    round_no = 3
    round_name = "Word 草案（不含图表）"
    # ⚠️ 不许写「最终计划 / 已交付」：这一刻三轮回审还没走完，定稿 Word 与看板
    # 都还没产出（renderer 那边的落盘文案也有同一条禁令，两处是同一件事）。
    next_hint = "认可这份草案请输入 Y，我再整理计划定稿并绘制看板。"

    def run(self, ctx):
        # 前两轮已经退回 → 不必再问第 3 轮，直接把最终交付物拦下（计划已落盘）
        if (ctx or {}).get("audit_rejected"):
            prev = [c for c in (ctx.get("audit_comments") or []) if c.get("comment")]
            notes = "；".join("R%s：%s" % (c.get("round"), c.get("comment")) for c in prev[:3])
            self.done_summary = "前序审计未通过，已停止产出定稿与看板"
            return {"_stop": ("前序审计未通过（%s），计划保持「未审计」。\n"
                              "  定稿 Word 与可视化看板**未产出**；已经落盘的只是"
                              "一份可复核的计划数据（JSON），**不能当作已交付的成果**。\n"
                              "  要拿到定稿与看板：按上述意见改完（或直接 /revise "
                              "\"<你的意见>\"）再重跑一遍。"
                              % (notes or "见审计意见"))}
        return _AuditGate.run(self, ctx)

    def _summary(self, ctx):
        plan = ctx.get("plan_json") or {}
        ov = plan.get("overview") or {}
        art = ctx.get("artifacts") or {}
        tasks = ctx.get("schedule") or {}
        leaves = _leaves(ctx.get("wbs") or {})
        lines = [
            "【第 3 轮 · Word 草案审计（不含图表）】",
            "  草案文件：%s" % (art.get("docx") or "（未生成）"),
            "  项目：%s" % (ov.get("project_name") or "未命名"),
            "  总工期 %s 天 ｜ 计划起止 %s → %s ｜ 任务 %d 条"
            % (_fmt_num((tasks or {}).get("total_duration_days") or ov.get("total_duration_days")),
               ov.get("planned_start_date") or "-", ov.get("planned_end_date") or "-",
               len(leaves)),
            "  草案里的进度表/资源表是**纯文字表格**（不含甘特图与曲线）；",
            "  可视化看板（SVG 甘特 + 人工曲线 + 资源荷载）会在你确认后才绘制。",
        ]
        for w in (plan.get("wbs_warnings") or [])[:2]:
            lines.append("  ⚠ " + str(w)[:80])
        return "\n".join(lines), {
            "docx": art.get("docx"),
            "total_duration_days": (tasks or {}).get("total_duration_days"),
            "leaves": len(leaves),
        }

    def _extra(self, ctx):
        # §D3：草案目录（章节/行数、表格/行数、图清单、定额覆盖率）
        return {"draft_outline": draft_outline_payload(ctx)}
