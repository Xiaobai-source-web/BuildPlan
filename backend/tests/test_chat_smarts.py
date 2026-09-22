# -*- coding: utf-8 -*-
"""第 35 轮：把"笨"的三处根因钉死（用户实测反馈）

用户反馈原话（按顺序）：
  ① 「这个界面太难看了…灰色字体有点看不清」（配色，见 test_terminal_ui_shell）
  ② 「为什么这里还是在显示跑意图识别」→ 界面名撒谎（见 test_router 的命名护栏）
  ③ 「不要出现问他关于产品的问题，却回答不上来的情况」
  ④ 「我明明在revise模式，它却提示我回revise模式…感觉笨笨的，是不是提示词配套不全」
  ⑤ 「修改模式明显还有很多问题」「将总工期改为306天」「将钢筋工程量改为1000立方米」
     → 预览说"没看出要改哪一项"却仍问 y/n

本文件只钉**根因**，不钉实现：
  1. 产品提问必须能到达模型（确定性拦截不许吞掉它们）；
  2. revise 模式的"基于当前计划问答"必须用 plan_qa 口径，且**不许**提示切模式；
  3. 死命令 `/edit` `/retry` `/continue` 不许再出现在任何提示词里；
  4. 预览没有可执行项时，不许再问"确认要改吗"；
  5. 改名（计划级字段）与"仓库关键词"改工程量要能被解析或给出可行动提示。

运行：python -m pytest backend/tests/test_chat_smarts.py -q
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import pytest  # noqa: E402

from pipeline.nodes.router import looks_like_plan_request, mentions_other_mode  # noqa: E402

PROMPTS = BACKEND / "prompts"


# ==================== 1. 产品提问必须放行给模型 ====================

@pytest.mark.parametrize("text", [
    "你们系统支持哪些项目类型？",
    "计划能改吗？",
    "项目样例在哪里？",
    "进度计划是什么？",
    "能不能导出 Word？",
    "三轮回审是什么？",
    "支持哪些输入格式？",
    "数据从哪来？",
    "怎么切换模式？",
    "有哪些大模型可以选？",
])
def test_产品功能提问不许被当成排计划指令(text):
    """用户实测的病根：这些句子含"项目/计划/进度"，旧实现一律拦成"请 /mode plan"，
    一次模型都不调 —— 于是"问产品却答不上来"。"""
    assert looks_like_plan_request(text) is False, text


@pytest.mark.parametrize("text", [
    "生成一份进度计划",
    "帮我排一下工期",
    "给我一份 WBS",
    "编制施工进度计划",
    "我要做一份计划",
    "生成计划：3 层框架，1500 平",
])
def test_真正的排计划指令仍然要认出来(text):
    """放宽不能放到"想排计划也不认"——那就把功能改没了。"""
    assert looks_like_plan_request(text) is True, text


@pytest.mark.parametrize("text", ["能改计划吗？", "改计划怎么用", "怎么修改计划", "怎么改计划"])
def test_提问不许被当成要切模式(text):
    """用户实测：「我明明在 revise 模式，它却提示我回 revise 模式」的同类根因。"""
    assert mentions_other_mode(text, "normal") is None, text


def test_真的要切模式时仍然给提示():
    assert mentions_other_mode("帮我改一下计划", "normal"), "祈使句该提示切 revise"


# ==================== 2. revise 模式的口径 ====================

def test_改计划模式聊天用计划问答提示词():
    """`chat_scope="plan"` 时必须走 plan_qa.txt，而不是普通模式的 router_reply.txt。"""
    from pipeline.nodes.router import RouterNode

    class _LLM:
        def __init__(self):
            self.prompts = []

        def chat_text(self, system, user, temperature=0.3):
            self.prompts.append(system)
            return "这是基于当前计划的回答"

    llm = _LLM()
    node = RouterNode(llm=llm)
    ctx = {"prompt": "再详细一些", "mode": "normal", "chat_scope": "plan"}

    class _Reg:
        def register(self, *a, **k):
            pass

    node.run(ctx)
    assert llm.prompts, "应该调了一次模型"
    assert "当前计划" in llm.prompts[0] or "正在修改计划" in llm.prompts[0], \
        "revise 模式下的问答要用计划问答口径，实际提示词：%s" % llm.prompts[0][:80]


def test_改计划模式不许提示切模式():
    """用户实测原话：「我明明在revise模式，它却提示我回revise模式」。"""
    from pipeline.nodes.router import RouterNode

    class _LLM:
        def chat_text(self, system, user, temperature=0.3):
            return "好的"

    node = RouterNode(llm=_LLM())
    out = node.run({"prompt": "我想改一下计划里的工期", "mode": "normal",
                    "chat_scope": "plan"})
    text = str(out.get("_stop") or "")
    assert "/mode revise" not in text, text
    assert "这属于" not in text, text


def test_计划问答提示词存在且不教死命令():
    text = (PROMPTS / "plan_qa.txt").read_text(encoding="utf-8")
    assert "当前正在修改的计划" in text, "要说明那行背景数据不是用户的问题"
    for dead in ("/edit", "/retry", "/continue"):
        assert dead not in text, dead


# ==================== 3. 提示词里不许有死命令（用户实测被它坑过）====================

def test_普通模式提示词不教已删除的命令():
    """实测截图：用户照着回答输入 `/edit 项目=NUS大楼` → 「未知命令」。

    这几个命令第 34 轮已从 `_COMMANDS` 撤下，提示词不能再**推荐**它们。
    提示词里允许出现"不存在 `/edit`"这种**否定声明**（那是防呆），所以先剔掉否定句。
    """
    text = (PROMPTS / "router_reply.txt").read_text(encoding="utf-8")
    affirmative = "\n".join(l for l in text.splitlines()
                            if not any(neg in l for neg in ("不存在", "不许", "已删除", "不要")))
    for dead in ("/edit", "/retry", "/continue"):
        assert dead not in affirmative, "提示词在推荐死命令 %s" % dead


def test_普通模式提示词覆盖产品全部能力():
    """用户要求：「是否包含我们产品的所有内容」。

    这里只钉**必须出现**的关键能力锚点（少一个就会出现"问产品答不上来"）。
    """
    text = (PROMPTS / "router_reply.txt").read_text(encoding="utf-8")
    must = ["normal", "plan", "revise", "import",      # 四种模式
            "/mode", "/exit",                          # 怎么切
            "/plans", "/open", "/wbs", "/sources",     # 已有计划与溯源
            "/llm",                                    # 模型档位
            "Word", "看板",                            # 交付物
            "定额",                                    # 数据来源
            "回审",                                    # 审计门
            "项目样例",                                # 样例在哪
            "层数", "建筑面积",                        # 硬必要参数
            "不可用于施工",                            # 边界
            ]
    missing = [m for m in must if m not in text]
    assert not missing, "提示词缺少：%s" % missing


def test_提示词不许写死会过期的内部数字():
    """1146 条测试 / 3877 条定额这类数字会变，写进提示词只会更快过期。"""
    text = (PROMPTS / "router_reply.txt").read_text(encoding="utf-8")
    for bad in ("1146", "3877", "18 张表", "26 节点"):
        assert bad not in text, bad


# ==================== 4. 预览没有可执行项时不许问 y/n ====================

class _Ctx(object):
    def __init__(self, client):
        self.client = client
        self.current_plan = {"overview": {"project_name": "x"}}
        self.current_plan_id = "plan_x"
        self.mode = "revise"
        self.mode_plan_id = "plan_x"
        self.history = []
        self.backend = "local"
        self.running = False
        self.run_id = "t"
        self.show_html = None
        self.verbose = False


def test_预览空手不许再问确认(monkeypatch):
    """用户实测：「这句话我没看出要改哪一项」和「确认要改吗？输入 y」同时出现。"""
    import console

    class _Client:
        def post_revise_preview(self, plan_id, text):
            return (200, {"applied": [], "rejected": []})

    ctx = _Ctx(_Client())
    seen = {}
    monkeypatch.setattr(console, "_chat_about_plan",
                        lambda c, t, pid: seen.update({"chat": t}))
    console._revise_with_confirm(ctx, "将总工期改为306天")
    assert seen.get("chat") == "将总工期改为306天", "空预览应退回聊天"
    assert not getattr(ctx, "_revise_pending", ""), "不该挂起确认状态"


def test_预览有可执行项时才问确认(monkeypatch):
    import console

    class _Client:
        def post_revise_preview(self, plan_id, text):
            return (200, {"applied": [{"target": "1.4.1", "field": "duration", "value": 20}],
                          "rejected": []})

    ctx = _Ctx(_Client())
    monkeypatch.setattr(console, "_chat_about_plan",
                        lambda c, t, pid: pytest.fail("有可执行项时不该退回聊天"))
    console._revise_with_confirm(ctx, "把 1.4.1 的工期改成 20")
    assert ctx._revise_pending == "把 1.4.1 的工期改成 20"


def test_只有被拦下的条目时说清下一步():
    import console

    lines = "\n".join(console._preview_lines(
        {"applied": [], "rejected": [{"patch": {"target": "钢筋"}, "reason": "匹配到多条"}]}))
    assert "匹配到多条" in lines
    assert "回车取消" in lines, "要给可行动的下一步，不能让用户对着 y/n 发呆"


# ==================== 5. 修改能力：改名与关键字工程量 ====================

def _plan():
    """一份结构与真实计划一致的最小计划（`iter_leaves` 只认 phases→work_packages→sub_packages）。"""
    return {
        "overview": {"project_name": "旧名字", "total_duration_days": 966},
        "meta": {},
        "wbs": {"phases": [
            {"id": "1", "name": "主体结构", "work_packages": [
                {"id": "1.4", "name": "钢筋工程", "sub_packages": [
                    {"id": "1.4.1", "name": "钢筋绑扎", "quantity": 100,
                     "duration_days": 10, "unit": "t"},
                    {"id": "1.4.2", "name": "钢筋加工", "quantity": 100,
                     "duration_days": 10, "unit": "t"},
                ]},
            ]},
        ]},
    }


def test_改计划名称能解析并生效():
    """改名必须能解析并生效。

    ⚠️ 第 36 轮更正：本文档原来把用户原话写成「我想将项目名称改为NUS大楼」，
    但用户实测截图里的原话是 **「我想修改这个项目名为NUS大楼」** ——
    那一句既过不了入口闸门（词表里没有"修改"、没有"项目名"），也过不了
    `_PAT_PLAN_TITLE`（名词后面是裸"为"，旧动词表里没有）。也就是说：
    第 35 轮是拿一句**改写过的句子**验证的，结论一直假绿，用户那句仍然改不了。
    现在两句都要过，而且以用户原话为准。
    """
    from pipeline.nodes import revise as R
    from pipeline.plan_store import apply_patch

    # ★ 用户原话（截图）：名词 + 裸"为"
    for sentence, want in (
        ("我想修改这个项目名为NUS大楼", "NUS大楼"),
        ("我想修改这个项目名称为NUS大楼", "NUS大楼"),
        ("我想将项目名称改为NUS大楼", "NUS大楼"),
        ("改名为NUS大楼", "NUS大楼"),
        ("这个项目叫NUS大楼", "NUS大楼"),
        ("把项目名字换成海之子大厦", "海之子大厦"),
        ("把项目的名字设置成海之子大厦", "海之子大厦"),
    ):
        patches = R._rule_patches(sentence, [])
        assert patches, "这句必须解析得出改名：%s" % sentence
        assert patches[0]["field"] == "plan_title", (sentence, patches)
        assert patches[0]["value"] == want, (sentence, patches)

        ok, why, norm = R._validate(_plan(), [], patches[0])
        assert ok, (sentence, why)
        out, _ids, applied = apply_patch(_plan(), norm)
        assert applied.get("applied") is True, sentence
        assert out["overview"]["project_name"] == want, "改完名字要到处看得见"
        assert out["meta"]["plan_title"] == want


def test_普通任务改动不许被误判成改名():
    """`_PAT_PLAN_TITLE` 放宽动词表后必须仍然**只**认真正的改名句。

    这里是最危险的一类回归：「把 5.1.1.1 的工期改为 20」如果被改名模式抢走，
    用户的工期改动会被写成项目名称 = "20"，属于静默数据破坏。
    """
    from pipeline.nodes import revise as R

    for sentence in ("把 5.1.1.1 的工期改成 20",
                     "把 4.1.1.1 的工程量改为 1200",
                     "把 5.1.1.1 的定额改为 4.5",
                     "把 4.1.1.2 的模板工加到 10 人",
                     "把层数改成 5 层",
                     "总工期改成 306 天",
                     "把开工日期改到 2026-07-01",
                     "这个工期怎么改？"):
        patches = R._rule_patches(sentence, [])
        fields = [p.get("field") for p in patches]
        assert "plan_title" not in fields, (sentence, patches)


def test_改名走规则不调模型():
    """改名是纯文本，规则比模型更可靠（不会自作主张加字）——命中就不该再花一次调用。"""
    from pipeline.nodes.revise import ReviseNode

    class _LLM:
        def __init__(self):
            self.calls = 0

        def chat_json(self, *a, **k):
            self.calls += 1
            raise AssertionError("改名不该调模型")

    llm = _LLM()
    node = ReviseNode(llm=llm)
    patches, _warnings = node._translate("把项目名称改为测试楼", [])
    assert patches and patches[0]["field"] == "plan_title"
    assert llm.calls == 0


def test_关键字工程量歧义时给编号提示():
    """"将钢筋工程量改为1000立方米"命中多条 → 要求补编号，而不是"没看出改哪一项"。"""
    from pipeline.nodes import revise as R

    items = R._normalize_items(None, _plan())
    patches = R._rule_patches("将钢筋工程量改为1000立方米", items)
    assert patches, "至少要给出反馈"
    reason = str(patches[0].get("reason") or "")
    assert "1.4.1" in reason or "编号" in reason, reason
    assert patches[0].get("field") == "quantity", \
        "字段必须是工程量（曾经被误判成人数）: %r" % patches[0].get("field")


def test_工种名不许被当成材料名():
    """"钢筋工程量"里的"钢筋工"是材料+工程，不是工种；误判会把字段变成人数。"""
    from pipeline.nodes import revise as R

    assert R._role_in("钢筋工程量", []) == "", "不该从「钢筋工程量」里认出工种"
    assert R._role_in("地下室混凝土工", []) == "混凝土工"
    assert R._field_of("钢筋工程量") == "quantity", "最长别名优先"
