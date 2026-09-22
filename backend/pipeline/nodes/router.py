"""节点 0：模式路由（**不再做意图识别**）— 第 34 轮改写

用户原话（本轮需求）：
    「不再保留自动切换模式的意图识别，只保留手动切换方式…取消意图识别功能。」
    「普通模式就纯粹用来聊天」
    「如果用户在某个模式提及了别的模式的事情，就提示他该如何切换模式」

所以本节点的工作变成**纯粹的模式分发**，一次大模型都不调：

| 当前模式 | 输入被当作 | 本节点做什么 |
|---|---|---|
| `plan`   | 一定是要生成计划 | 直接进流水线（不再先问"你是要排计划吗"） |
| `normal` | 一定是聊天问答 | 用模型回答；**不**开流水线。若这句明显是在要计划 → 回一句"请先 `/mode plan`" |
| `revise` | 针对当前基准计划的问答/修改 | 回一句"用终端改计划"（终端在本地就处理了，正常走不到这里） |
| `import` | 给一个计划 JSON 路径 | 回一句用法 |

保留的三个"确定性判断"（都不调模型，只做关键词/正则）：
  · `looks_like_plan_request()`：普通模式里帮用户认出"你其实想生成计划"；
  · `mentions_other_mode()`：在某个模式里提到别的模式的事 → 提示怎么切；
  · 剩下的**一律当聊天**——这正是用户要的"普通模式纯粹聊天"。

⚠️ 为什么彻底删掉 LLM 意图分类：用户实测的痛点是"每次问一些简单的问题都还要识别半天意图"
（一次分类 = 一次模型往返，几秒 + token），而识别错了后果更糟（把闲聊当计划、把计划当闲聊，
两种都在实测里出现过）。模式是用户显式选的，比模型猜的可靠。
"""

import re

from ..base import BaseNode
from ..events import EV_CONFIRM_REQUIRED
from ..llm import LLMClient, LLMError
from ..registry import GATE_TIMEOUT_SECONDS
from ..prompts_loader import load
from .extractor import detect_intent

# ==================== 确定性判断：这句话像不像"要生成计划" ====================
# 只用于**普通模式**下的引导（"你其实想排计划吧？请先 /mode plan"），不用于分流。
_PLAN_WORDS = ("进度计划", "施工计划", "工期", "排期", "wbs", "cpm", "生成计划",
               "做个计划", "排个计划", "编制计划", "关键路径", "资源定额", "排一下")
# 疑问句启发式（"能不能帮我排一下工期？"）
_QUESTION_HINTS = re.compile(
    r"[?？]|怎么|如何|是什么|什么是|吗|能否|能不能|可不可以|要不要|需不需要|请问|帮我.*吗|需要.*吗"
)


# 明确的"请你做"祈使式：说到"生成/编制/排一份"这种要求时，才算动作请求
_PLAN_NOUNS = ("计划", "进度", "工期", "排期", "wbs", "cpm", "横道图")
_PLAN_IMPERATIVE = re.compile(
    r"(生成|编制|做一个|做一份|做个|排一份|排一个|出一份|出一版|出个|给我|帮我|我要|想要|"
    r"来一份|来一个|排一下|安排一下|开始编制|新建|算一份)"
    r".{0,10}(计划|进度|工期|排期|wbs|cpm)"
)

# 句首的疑问词：命中即视为**提问**（要回答，不是要动作）
_QUESTION_LEAD = re.compile(
    r"^\s*(什么|啥|怎么|如何|为什么|为何|哪|哪些|哪种|能不能|能否|可不可以|可以吗|"
    r"是否|是不是|有没有|有吗|支持|介绍|解释|讲讲|说说|告诉我|请问|who|what|how|why|which|can|does|do|is|are)"
)
_QUESTION_TAIL = re.compile(r"(吗|呢|么|嘛)[\s？?！!。.~～]*$")
# 句中疑问词：出现在任何位置都说明这句在**问**，不是在**要**
_INTERROGATIVE = re.compile(r"怎么|如何|为什么|为何|是什么|什么是|哪些|哪个|多少|能否|能不能|是否")


def looks_like_plan_request(text):
    """这句是不是"**要我生成一份计划**"（确定性关键词，不调模型）。

    ⚠️ 第 35 轮修正（用户实测："不要出现问他关于产品的问题，却回答不上来的情况"）：
    旧实现直接用 `extractor.detect_intent()`，而它只要句子里含"项目/计划/进度/施工"
    任一就判 plan。于是这四类**产品功能提问**全被拦成"请输入 /mode plan 切过去"，
    一次模型都没调 —— 用户问"你们支持哪些项目类型？""计划能改吗？""进度计划是什么？"
    "项目样例在哪里？" 得到的全是同一句牛头不对马嘴的引导，这就是"感觉笨笨的"的真源。

    现在改成**两层判定**：
      ① 疑问句（有问号 / 句首疑问词 / 句末"吗呢么"）→ 一律**不是**动作请求，放行给模型；
      ② 其余情况必须命中祈使式（"生成/编制/排一份… + 计划/进度/工期"），或命中
         `extractor.detect_intent` 的 plan **且**含生成类动词。

    保守取舍：宁可把"想生成计划"误判成提问（模型会照提示词提醒 `/mode plan`，
    信息不丢），也不要把产品提问吞成一句固定引导（那才是用户投诉的行为）。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        return False
    if _QUESTION_LEAD.match(t) or _QUESTION_TAIL.search(t):
        return False
    if _PLAN_IMPERATIVE.search(t):
        return True
    # 量词/语气词会隔开动词与名词（"给我**一份** WBS"），先抠掉再判。
    # 抠完可能只剩名词（"给我WBS" → "WBS"），所以再加一条"动作词 + 计划名词"的宽松判据：
    # 名词必须在动作词**之后**，避免"为什么这个计划这么慢"这类被误判（它的"计划"在前）。
    stripped = t
    for filler in ("一份", "一个", "个", "份", "张", "套", "的"):
        stripped = stripped.replace(filler, "")
    if _PLAN_IMPERATIVE.search(stripped):
        return True
    m = re.search(r"(生成|编制|做|排|出|来|要|给|拿|开始|新建|安排|算|看看|看一下)",
                  stripped)
    if m and any(n in stripped[m.end():].lower() for n in _PLAN_NOUNS):
        return True
    # 兜底：老词表（"排期/预算/造价"等）仍认，但要求同时出现生成类动作词，
    # 避免"我有个项目"这种陈述句被当成排计划指令。
    low = t.lower()
    if detect_intent(t) == "plan" and re.search(
            r"(生成|编制|做|排|出|安排|新建|开始)", low):
        return True
    return False


# ==================== 确定性判断：在别的模式里提到"别的模式的事" ====================
# 键=目标模式，值=命中这些词就提示"这属于 X 模式"。
# ⚠️ 不包含"普通/聊天"：回普通模式由 `/exit` 负责，普通模式里说要改计划也该走 revise。
_MODE_WORDS = (
    ("plan", re.compile(
        r"生成.{0,4}计划|排.{0,2}计划|做个计划|做一份计划|编制.{0,4}计划|排.{0,2}工期|排一下")),
    ("revise", re.compile(
        r"改.{0,4}计划|修改.{0,4}计划|调整.{0,4}计划|改.{0,2}工期|缩短.{0,4}工期|"
        r"工期.{0,2}缩短|改.{0,2}工程量|改.{0,2}班组")),
    ("import", re.compile(r"导入.{0,4}计划|我有一份计划|计划\s*json")),
)


def mentions_other_mode(text, mode):
    """当前模式下，用户是不是提到了**另一个模式**的事。

    返回 `(提示语 or None)`。设计原则：**只提示、不自动切**（用户明确要求取消自动切换）。

    ⚠️ 第 35 轮补一道疑问句闸门：旧实现只看关键词，于是"**能**改计划**吗**？"这种
    提问也被回成"这属于【修改计划】模式…请输入 /mode revise"，而用户问的其实是
    产品能力（"能不能改"），答案该是"能，这样改"+ 顺带告知怎么进。判据同
    `looks_like_plan_request`：问号 / 句首疑问词 / 句末"吗呢么" 一律不算"要切模式"。
    """
    t = str(text or "")
    if "?" in t or "？" in t:
        return None
    if _QUESTION_LEAD.match(t) or _QUESTION_TAIL.search(t):
        return None
    # 句中带"怎么/如何/为什么"等疑问词的也是在**问**（"改计划怎么用"），
    # 不是在下指令；纯净的祈使句（"帮我改一下计划"）不含疑问词，照旧给提示。
    if _INTERROGATIVE.search(t):
        return None
    for target, pat in _MODE_WORDS:
        if target == mode:
            continue
        if pat.search(t):
            return ("这属于【%s】模式的事情，当前在【%s】模式。想切过去请输入 `%s`。"
                    % (_MODE_LABELS.get(target, target), _MODE_LABELS.get(mode, mode),
                       _SWITCH_CMD.get(target, "/mode " + target)))
    return None


_MODE_LABELS = {"normal": "普通（聊天）", "plan": "生成计划",
                "revise": "修改计划", "import": "导入计划"}
# 用户要求「命令都用英文」→ 提示里给的是**英文命令**
_SWITCH_CMD = {"normal": "/exit", "plan": "/mode plan",
               "revise": "/mode revise", "import": "/mode import"}
_MODES = ("normal", "plan", "revise", "import")


# 闲聊回复的确定性命令引导兜底：模型忘了提示时，代码也补一条。
_CMD_HINT = ("\n\n💡 想生成计划请先输入 /mode plan；/help 看全部命令。")


def mode_of(ctx):
    """本次运行的终端模式（缺省 = 普通）。非法值一律按普通处理。"""
    m = str((ctx or {}).get("mode") or "normal").strip().lower()
    return m if m in _MODES else "normal"


class RouterNode(BaseNode):
    name = "router"
    # 界面名由 builder.PIPELINE_TITLES 覆盖成「识别当前模式」—— 第 34 轮起它其实
    # 只做模式分发，不再识别意图；节点名保持不变（测试与日志都按 `router` 找它）。
    # 下面这个 title 只是"没有界面名表时"的兜底，也一并不许再叫"意图识别"。
    title = "识别当前模式"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm or LLMClient()

    def run(self, ctx):
        prompt = (ctx.get("prompt") or "").strip()
        mode = mode_of(ctx)
        ctx["mode"] = mode

        if mode == "plan":
            self.emit("node_progress", {"node": self.name, "progress": 100,
                                        "message": "生成计划模式：直接开始编制"})
            ctx["intent"] = "plan"
            return {"intent": "plan"}

        if mode == "normal":
            # 第 35 轮：**改计划模式下"基于当前计划聊天"**走的是本条路径（终端把请求
            # 标成 normal，避免它去排计划），但提示词与"别的模式"提示必须换成计划问答版。
            # 实测踩到的 bug：用户明明在 revise 模式里问"再详细一些"，却收到
            # 「这属于【修改计划】模式…想切过去请输入 /mode revise」—— 让人以为它傻了。
            # 根因：这里无条件用普通模式的提示词 + 无条件做"别的模式"提示。
            if str(ctx.get("chat_scope") or "") == "plan":
                return self._answer_and_stop(ctx, prompt, "修改计划模式：基于当前计划回答",
                                             prompt_name="plan_qa.txt")
            # 普通模式 = 纯聊天。但用户可能就是**忘了切模式**，所以先给确定性引导。
            # 顺序很重要：**先看是不是别的模式的事**（"帮我改一下计划" 里有"计划"二字，
            # 但它属于 revise 模式，不能被下面的 plan 判断抢走）。
            other = mentions_other_mode(prompt, mode)
            if other:
                return self._answer_and_stop(ctx, prompt,
                                             "普通模式：这句话属于别的模式，已给出切换方法",
                                             guide=other)
            if looks_like_plan_request(prompt):
                # ⚠️ 这里**不能再调模型**（实测踩到）：原先让模型顺口答一句，结果它
                # 直接吐出一份"WBS 大纲 / 第一阶段：土方开挖…"，用户看到的就是
                # "我说了要排计划、它没切模式却已经在排计划了"。普通模式只给指路。
                guide = ("你要的是【生成计划】模式（当前是 normal 普通模式，这里只聊天）。\n"
                         "请输入 `/mode plan` 切过去，再把项目条件说一遍 —— "
                         "**层数 + 总建筑面积**是必须的，能给再说上栋数/类型/结构/方量/开工日期。")
                return self._stop_with(ctx, guide, "普通模式：像要计划，已提示切换")
            return self._answer_and_stop(ctx, prompt, "普通模式：已直接回答这个提问")

        # revise / import：终端在本地就会处理，正常走不到这里；到了就给一句正确的用法。
        if mode == "revise":
            return self._answer_and_stop(
                ctx, prompt, "修改计划模式：请用终端改计划",
                guide="改计划在本机终端里完成（说一句「把 5.1.1.1 的工期改成 20」即可）。")
        return self._answer_and_stop(
            ctx, prompt, "导入计划模式：等待一个计划 JSON 路径",
            guide="导入计划请在使用终端里输入 `/import <计划 JSON 的完整路径>`。")

    # ---------------- 工具 ----------------
    def _stop_with(self, ctx, text, summary):
        """**不调模型**，直接把一段话作为本次运行的答复（引导类用）。

        为什么普通模式的引导必须走这条路（第 34 轮实测）：引导句如果和模型回答拼在一起，
        模型会顺手把"生成计划"这件事**做出一半**（实测吐出一份 WBS 大纲），
        用户看到的就是"没切模式却已经在排计划"。引导类回复必须只有引导。
        """
        answer = str(text or "").strip()
        if answer and not answer.endswith(_CMD_HINT):
            answer = answer.rstrip() + _CMD_HINT
        ctx["intent"] = "chat"
        ctx["chat_reply"] = answer
        self.done_summary = summary
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "已给出切换提示"})
        return {"_stop": answer, "intent": "chat"}

    def _answer_and_stop(self, ctx, prompt, summary, guide="", hint_other=False,
                         prompt_name="router_reply.txt"):
        """答一句就停（`normal` 模式的唯一出路）。

        `hint_other=True`：顺手看一下用户是不是在提**别的模式**的事，是就把切法写在前面
        （只提示、不自动切 —— 用户明确要求取消自动切换）。
        `prompt_name`：用哪份 system prompt。改计划模式下基于计划问答用 `plan_qa.txt`，
        因为 `router_reply.txt` 是"普通模式"的口径，会答错模式（第 35 轮实测踩到）。
        """
        answer = self._answer(prompt, prompt_name=prompt_name)
        other = mentions_other_mode(prompt, mode_of(ctx)) if hint_other else None
        if other:
            answer = other + "\n\n" + str(answer or "")
        if guide:
            answer = (str(guide).rstrip() + "\n\n" + str(answer or "")).strip()
        if answer and not answer.endswith(_CMD_HINT):
            answer = answer.rstrip() + _CMD_HINT
        ctx["intent"] = "chat"
        ctx["chat_reply"] = answer
        self.done_summary = summary
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "回答完成"})
        return {"_stop": answer, "intent": "chat"}

    def _classify(self, prompt):
        """**已废弃**（第 34 轮）：保留名字只为兼容极旧的调用方与日志搜索。

        现在不再有任何调用点 —— 分流由模式决定（见 `run`）。若哪天有人把它接回去，
        请先读本文件顶部那段"为什么彻底删掉 LLM 意图分类"。
        """
        return "plan" if looks_like_plan_request(prompt) else "chat"

    def _answer(self, prompt, prompt_name="router_reply.txt"):
        try:
            reply = self.llm.chat_text(load(prompt_name), prompt, temperature=0.7)
        except (LLMError, Exception):
            reply = None
        return (reply or "").strip() or _CHAT_FALLBACK_REPLY

    def _confirm_enter(self, prompt):
        """把"确认进入工作模式"的门留给 work_confirm 节点 —— 本节点不再重复问一遍。

        保留这个函数是因为历史上它发过 `confirm_required`；现在**没有任何调用点**
        （模式已由用户手选，不需要再确认一次"你真的要排计划吗"）。
        """
        registry = getattr(self, "_registry", None)
        if registry is None:
            return True
        import uuid as _uuid
        cid = f"confirm_{getattr(self, '_run_id', 'run')}_{_uuid.uuid4().hex[:4]}"
        registry.register(cid)
        self.emit(EV_CONFIRM_REQUIRED, {
            "confirm_id": cid,
            "message": "你是想让我生成一份施工进度计划吗？",
            "context": {"summary": f"输入：{prompt[:80]}"},
        })
        decision = registry.wait(cid, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)
        return decision.get("decision") is True


_CHAT_FALLBACK_REPLY = (
    "我是施工进度计划生成助手。你可以描述项目情况（如建筑面积、混凝土方量、开工日期），"
    "我会为你生成完整的施工进度计划（WBS、关键路径、资源定额等）。"
)
