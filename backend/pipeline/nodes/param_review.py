"""参数人工复核门 —— 参数提取与边界条件补充之间的人工关口

ExtractorNode 抽取出核心项目参数后，本节点把**全部**提取结果推到终端，用户可选：
  - 打 Y 通过（passed=True）→ 采信提取结果，交给边界条件 LLM；
  - 直接输入项目参数（passed=False, manual_input=文本）→ 作为用户补充，
    随核心参数一同送给边界条件 LLM（"结合用户输入 + 常识补全"）；
  - **明确输入「试算」**（或上行 trial=true）→ 用默认值先算一版，全程标注不可用。

v2.4 起本节点还承担**必要参数门**的职责：
  实测过的问题：零参数 + 计划意图原本会照样跑完 26 节点、产出 821 条叶子 / 1184 天的
  计划（只在编制口径里写了一行"层数暂用默认（待确认）"）—— **标注了但没拦**。

  现在的规则（用户拍板选"必须明说"）：
    · 缺硬必要参数时，**按通过不会放行** —— 按多少次都只重复提示；
    · 想用默认值试算，**必须明确输入「试算」**（或上行 trial=true），不接受误触；
    · 补齐参数（手输或改输入）→ 正常放行，不是试算；
    · 【第 2 批 · 域 2 / 2.1】**绝对必要键**（`boundary.ABSOLUTE_KEYS`）**连试算也不放行**
      —— 缺它直接 `_stop` 中止。**第 2 批收口（用户裁决）后本档 = `foundation_type`
      基础类型 + `structure_type` 结构形式**：两者分别决定"基础形式"与"结构体系"，
      也就是"这份计划按什么口径编、选哪些工序"。用户原话：「结构各类型和基础类型
      都是，如果没有输入，那就报错，让用户重新输入。」
    · 这两个键**门上补得出来**（`_merge_manual` → `extract_by_regex` 认标准名），
      所以缺它们时的正确行为是：**报错 + 提醒 + 让用户重新输入全套参数**，而不是把门
      变成死路（修前 `_absolute_hint` 谎称"不能在门上补"，用户照着上面那句"请直接输入
      这些参数"一直输入、一直被挡 —— 门的体验就是空转）。
    · 要中止：/abort（或等待超时）—— 门超时会返回 abort，不会无限追问。
      ⚠️ 与门上的提示严格对齐：/abort、/cancel 必须**整条输入就是它**才算中止
      （`/abort 顺便说一句` 是补充内容，不是喊停）；命中即 `_stop`，不再当补充参数。

采用自管理交互（register → emit → wait），同 nodes/confirm.py 范式，
不是引擎层的 pause_point。决策经 POST /params 上行。
"""

import uuid

from ..base import BaseNode
from ..registry import GATE_TIMEOUT_SECONDS
from ..events import EV_PARAM_REVIEW
from .boundary import (ABORT_HINTS, abort_exit, cadence_gate_note,  # noqa: F401
                       is_abort_decision, param_label, param_label_list,
                       params_completeness)

# 安全网：正常用户不会走到。真到了说明终端在空转，宁可停也不要无限问。
MAX_ROUNDS = 20

TRIAL_HINTS = ("试算", "试用", "用默认", "默认值", "先算算")

# 中止出口的唯一文案（注册表超时/取消 与 用户手输中止命令 共用，行为必须一致）
ABORT_STOP = ("用户在参数复核门选择中止（输入 /help 看用法；重新描述项目即可再开始）")
ABORT_SUMMARY = "用户在参数复核门选择中止"

# ⚠️ 中止判定与出口已收口到 `boundary.py`（`is_abort_decision` / `abort_exit` /
# `hits_abort_command`）——**文件门与审计门也共用同一份**。原先只有本节点认 `/abort`，
# 于是另外两道门把 `/abort` 当成路径 / 审计意见（用户以为中止了，实际还在跑）。
_abort_exit = abort_exit("参数复核门")


def _merge_manual(params, manual):
    """把用户手输的文本并入参数视图（**只为判断完备性**，不改 ctx）。

    复用抽取器的正则（"栋数 12" / "12 栋" / "总建筑面积 215000 ㎡" 都认），不另写解析。
    """
    merged = dict(params or {})
    text = str(manual or "")
    if not text:
        return merged
    try:
        from .extractor import normalize_params
        for k, v in (normalize_params({}, text) or {}).items():
            if v not in (None, "", 0):
                merged[k] = v
    except Exception:
        pass
    return merged


def _absolute_hint(comp):
    """缺 `ABSOLUTE_KEYS` 里那些**不许试算**的键时，告诉用户怎么补齐。

    【第 2 批收口 · 用户裁决】修前的文案是**错的**：它写着这两个键"不能在门上补"，
    于是用户照着上面那句"请直接输入这些参数"一直输入、一直被挡（门的体验 = 空转）。
    实测（`_merge_manual` → `normalize_params` → `extract_by_regex`）：
      输入「基础类型：筏板基础」        → `foundation_type = '筏板基础'`
      输入「结构形式：框架-剪力墙结构」   → `structure_type = 'frame_shear'`
    也就是说**门上完全补得出来**，只是提示在撒谎。

    用户原话：「输入参数没有基础类型，那就报错，并提醒用户就好了。让用户重新输入
    全套参数（包含基础类型），再开始生成计划，也就是说，结构各类型和基础类型都是，
    如果没有输入，那就报错，让用户重新输入。」
    ⇒ **报错 + 提醒 + 让用户重输「全套」参数**，就是本函数要交付的行为。
    返回空串时不占版面。
    """
    miss = [k for k in (comp.get("missing_absolute") or [])]
    if not miss:
        return ""
    return ("\n⛔ 其中「%s」是**不能靠试算绕过**的硬事实：它们决定这份计划按什么基础形式、"
            "什么结构体系来编，缺了连「不可用于施工的试算版」都没有意义。\n"
            "请**重新输入全套项目参数**（含下列必填项）后继续：\n"
            "  · 层数\n"
            "  · 总建筑面积(m²)\n"
            "  · 基础类型 —— 写标准名，如「筏板基础」「独立基础」「桩基础」「箱形基础」\n"
            "  · 结构形式 —— 写标准名，如「框架结构」「框架-剪力墙结构」「剪力墙结构」\n"
            "例：层数 38，总建筑面积 215000 ㎡，基础类型 筏板基础，结构形式 框架-剪力墙结构\n"
            "（栋数、混凝土总量、开工日期等可选；不给会推算并在交付物里标注）"
            % param_label_list(miss))


def _gate_message(comp, attempt, cadence_note=""):
    """门上的提示：缺什么 + 缺了会怎样 + 有哪些出路（一次说清）。

    ⚠️ 所有参数名都走 `boundary.param_label()` —— 用户实测反馈过门上直接出现
    `total_concrete、total_rebar` 这种内部键名（「不要刻意使用一些英文和专业术语」）。

    `cadence_note`（第 41 轮）：**施工节拍的回显**，由 `boundary.cadence_gate_note()`
    生成（"检测到标准层节拍 = 7 天/层（来源：用户输入…）"或"未检测到…"）。
    加它的原因：示例3 原文写着「标准层7天一层」，可整份产物里"7天"一次都没出现，
    用户没有任何机会发现自己给的节拍被忽略了；这道门是唯一能在编排前说一句的地方。
    """
    if comp.get("ok"):
        extra = ""
        if comp.get("missing_default"):
            extra += ("\n（将按默认值编制并在交付物里标注：%s）"
                      % param_label_list(comp.get("missing_default")))
        fb = comp.get("missing_fallback") or []
        if fb:
            extra += ("\n（以下参数缺失，将用推算/默认值并在交付物里标注：%s）"
                      % param_label_list(fb))
        msg = "参数提取完成，请人工复核：" + extra
        return msg + ("\n" + cadence_note if cadence_note else "")

    head = ("⚠️ 参数提取完成，但还缺编制所必需的项目事实：%s\n%s"
            % (param_label_list(comp.get("missing_required")), comp.get("note") or ""))
    if attempt > 1:
        head = ("⚠️ 必要参数仍然缺失（你刚选择了通过，但缺必要参数不会因为按通过而放行）。\n"
                + head)
    return head + (
        "\n请直接输入这些参数，例如：栋数 12，地上 38 层，总建筑面积 215000 ㎡\n"
        "（带单位与前缀标签都可以；也可写成「12 栋 / 38 层」这类数字在前的说法）\n"
        "若你确实想先用默认值试算，请输入「试算」两个字 —— "
        "试算结果会标注为「不可用于施工」。\n"
        "要中止本次运行，输入 /abort。"
        + _absolute_hint(comp)
        + ("\n" + cadence_note if cadence_note else ""))


class ParamReviewNode(BaseNode):
    name = "param_review"
    title = "参数人工复核门"

    def run(self, ctx):
        params = ctx.get("extracted_params") or {}
        manual_all = []
        applied_all = {}
        comp = params_completeness(params)
        trial = False

        for attempt in range(1, MAX_ROUNDS + 1):
            review_id = f"pr_{getattr(self, '_run_id', 'run')}_{uuid.uuid4().hex[:4]}"
            self._registry.register(review_id)
            # 第 41 轮：**施工节拍回显**。示例3 原文写着「标准层7天一层」，可整份产物里
            # "7天"从未出现，用户没有任何机会发现它被静默忽略 —— 这道门是编排前唯一能
            # 说一句的地方。用手输累计文本试算，用户本轮刚补的节拍下一轮立刻生效。
            _probe = dict(ctx)
            _probe["_manual_param_input"] = "\n".join(manual_all) or None
            self.emit(EV_PARAM_REVIEW, {
                "review_id": review_id,
                "message": _gate_message(comp, attempt, cadence_gate_note(_probe, params)),
                "params": params,
                "completeness": comp,
                "round": attempt,
            })
            decision = self._registry.wait(
                review_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)

            # 取消 / 超时 → wait 返回 {"action": "abort"}；用户手输 /abort（或 /cancel、
            # 退出、中止）→ 也算中止。**两条必须走同一个出口**：门上的提示说了会中止，
            # 行为就得真的中止，不能把它当"补充参数"继续往下跑。
            if is_abort_decision(decision):
                self.done_summary = ABORT_SUMMARY
                return {"_stop": ABORT_STOP}

            manual = (decision.get("manual_input") or "").strip()
            # 唯一进入试算的途径：**明确说试算**（或上行 trial=true）
            if bool(decision.get("trial")) or any(h in manual for h in TRIAL_HINTS):
                # ---- 【第 2 批 · 域 2 / 2.1】`ABSOLUTE_KEYS` 连试算也不放行 ----
                # 缺基础类型（`foundation_type`）时，连"不可用于施工的试算版"也不许产出：
                # 本项目不支持装配式建筑，基础形式（独立/筏板/桩基…）是整份计划按什么
                # 口径编的前提 —— 缺它编制的计划连"参考"都谈不上。
                # 判据复用 `params_completeness` 的 `missing_absolute`（= missing_required
                # ∩ ABSOLUTE_KEYS），**没有另造一套报错机制**：出口仍是本节点既有的
                # `{"_stop": ...}`（与"必要参数未补齐"那条同一形态）。
                _abs_miss = comp.get("missing_absolute") or []
                if _abs_miss:
                    _abs_txt = param_label_list(_abs_miss)
                    self.done_summary = ("绝对必要参数缺失，试算也不放行 → 中止（缺：%s）"
                                         % _abs_txt)
                    return {"_stop": (
                        "缺少不能靠试算代替的项目事实：%s。"
                        "它们决定基础形式与结构体系，请**重新输入全套项目参数**"
                        "（含基础类型与结构形式，写标准名，如「筏板基础」"
                        "「框架-剪力墙结构」）后再生成计划。" % _abs_txt)}
                trial = True
                break

            if manual:
                manual_all.append(manual)
                merged = _merge_manual(params, manual)
                comp = params_completeness(merged)
                # **用户手输的项目事实必须真的进计划。**
                # 修前：这里算出的 merged 只用来判断完备性，值本身仅随 _manual_param_input
                # 送给边界条件的 LLM；而 栋数/层数 属于 _DOC_ONLY_KEYS（禁止 LLM 补全），
                # 于是用户照着门上的提示输入「栋数 12，地上 38 层」也等于没输 ——
                # 实测后果：计划仍按"层数未知"编制（821 条叶子 / 5735 天）。
                store = ctx.get("extracted_params")
                if not isinstance(store, dict):
                    store = {}
                    ctx["extracted_params"] = store
                applied = {k: v for k, v in merged.items()
                           if v not in (None, "", 0) and store.get(k) != v}
                if applied:
                    store.update(applied)
                    applied_all.update(applied)
                    params = store
            if comp.get("ok"):
                break
            # 缺必要参数且没明说试算 → 继续问（按通过不会放行）

        joined = "\n".join(manual_all) if manual_all else ""
        if applied_all:
            # 留痕：这些键是**用户在门上明确给出**的项目事实（交付物要说得清口径哪来的）
            ctx["manual_params_applied"] = dict(applied_all)
        # 循环走完（安全网触发）仍未齐、也未明说试算 → **不放行**。
        # 绝不允许静默通过：那正是本次要修的毛病（零参数也能出计划）。
        if not trial and not comp.get("ok"):
            _miss = param_label_list(comp.get("missing_required"))
            self.done_summary = "必要参数仍未补齐，且未明确选择「试算」→ 中止（缺：%s）" % _miss
            return {"_stop": "必要参数未补齐（%s）。请补齐后重来；"
                             "若要用默认值试算，请在参数门明确输入「试算」。" % _miss}
        ctx["_manual_param_input"] = joined or None
        ctx["params_completeness"] = comp
        ctx["trial_mode"] = bool(trial)

        if trial:
            self.done_summary = (
                "用户明确选择用默认值试算（缺必要参数：%s）——交付物将标注为不可用于施工"
                % (param_label_list(comp.get("missing_required")) or "无"))
            return {"review_passed": bool(joined), "trial_mode": True,
                    "_manual_param_input": ctx["_manual_param_input"],
                    "params_completeness": comp}

        if joined:
            self.done_summary = "用户手动输入补充参数，必要参数已齐，进入边界条件补全"
            return {"review_passed": False, "_manual_param_input": joined,
                    "params_completeness": comp}
        self.done_summary = "用户确认提取参数通过，进入边界条件补全"
        return {"review_passed": True, "_manual_param_input": None,
                "params_completeness": comp}
