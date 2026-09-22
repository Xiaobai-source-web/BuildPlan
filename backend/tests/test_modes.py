# -*- coding: utf-8 -*-
"""第 33 轮回归：模式（意图对话隔离）+ 已有计划/导入 + WBS 与输入留档

对应用户提出的三件事：
  1. 「意图对话隔离是必须要做的，必须要在每次对话时，自己处于什么模式，用户能清清楚楚」
     「当用户进入修改模式之后，一定要在聊天框固定显示"修改模式，plan id：xxxx"」
     → `ctx.mode` + 输入框标识 `[改计划 · plan_x]` + `/退出`；模式落盘（跨会话）
  2. 「基准计划选择是必要的」→ 修改模式没有基准时给 `[1]已有 [2]导入 [3]先生成 [0]返回`
  3. 「WBS树保存也是要做的…配套命令能够显示自己保存了哪些wbs树」
     「用户的输入也要保存下来…并编号，给每一份 plan 和 wbs 都标清楚来自哪份输入」
     → `run_archive` 落 `输入/<input_id>.json` 与 `WBS/<run_id>.json`；`/wbs`、`/inputs`

运行：python -m pytest backend/tests/test_modes.py -q
"""

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT / "terminal")):
    if p not in sys.path:
        sys.path.insert(0, p)

import commands   # noqa: E402
import console    # noqa: E402
import renderer   # noqa: E402
from pipeline import run_archive as A  # noqa: E402

PLAIN = renderer.strip_ansi


# ======================================================================
# 0. 隔离：每个用例把档案目录指到临时目录（绝不写仓库里的 plans/）
# ======================================================================
@pytest.fixture(autouse=True)
def _isolated_archive(tmp_path, monkeypatch):
    # ⚠️ `run_archive` 自 2026-09-20 起**按需读** `config.PLANS_DIR`，不再按值缓存
    # 模块常量 `PLANS_DIR`（按值缓存的后果实测过：`tests/conftest.py` 的 monkeypatch
    # 对它无效，每个走 `/chat` 的用例都往**真实** `backend/plans/输入/` 写档，
    # 累积出 719 个测试垃圾文件）。所以这里两处都要指：
    #   · `config.PLANS_DIR` —— 模块真正读的那个，不指它就会出现
    #     "测试写到 A.PLANS_DIR、list_plans 却去读 config.PLANS_DIR" → 断言拿到 0 条
    #   · `A.PLANS_DIR`      —— 本文件测试体仍用它拼路径（原来就只指了这一个）
    from pipeline import config as _config
    monkeypatch.setattr(_config, "PLANS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(A, "PLANS_DIR", tmp_path, raising=False)
    yield tmp_path


class _FakeClient:
    """记录调用、返回可编排结果的假客户端（不联网）。"""

    def __init__(self, plans=None, plan=None, mode=None):
        self.calls = []
        self._plans = plans if plans is not None else []
        self._plan = plan
        self._mode = mode or {"mode": "normal", "plan_id": ""}

    def list_plans(self):
        self.calls.append(("list_plans",))
        return 200, {"plans": self._plans}

    def list_wbs(self):
        self.calls.append(("list_wbs",))
        return 200, {"wbs": A.list_wbs()}

    def list_inputs(self):
        self.calls.append(("list_inputs",))
        return 200, {"inputs": A.list_inputs()}

    def get(self, path):
        self.calls.append(("get", path))
        if path.startswith("/plans/"):
            if self._plan is None:
                return 404, {"error": "plan not found"}
            return 200, self._plan
        return 404, {"error": "not found"}

    def get_mode(self):
        return 200, dict(self._mode)

    def post_mode(self, mode, plan_id=""):
        self.calls.append(("post_mode", mode, plan_id))
        self._mode = {"mode": mode, "plan_id": plan_id}
        return 200, {"ok": True}

    def post_baseline(self, plan_id, plan):
        self.calls.append(("post_baseline", plan_id))
        return 200, {"ok": True}

    def post_revise(self, plan_id, text):
        self.calls.append(("post_revise", plan_id, text))
        return 200, {"summary": "改好了", "applied": [], "total_duration_days": 100}


class _Ctx:
    def __init__(self, client=None, **kw):
        self.client = client or _FakeClient()
        self.history = []
        self.current_plan = None
        self.current_plan_id = None
        self.mode = "normal"
        self.mode_plan_id = ""
        self.verbose = False
        self.tui = None
        self.feed = None
        for k, v in kw.items():
            setattr(self, k, v)


def _plan(pid="plan_x", days=100, name="示例项目"):
    return {"plan_id": pid,
            "overview": {"project_name": name, "total_duration_days": days},
            "all_tasks_schedule": [{"task_id": "1.1.1", "task_name": "钢筋绑扎"}],
            "meta": {}}


def _capture(ctx, fn, *a, **kw):
    """抓 `console._emit` 的输出（它走 term.out / print 两条路）。"""
    out = []

    def _fake_emit(c, text, **kwargs):
        out.append(PLAIN(str(text)))
    saved = console._emit
    console._emit = _fake_emit
    try:
        res = fn(ctx, *a, **kw)
    finally:
        console._emit = saved
    return res, "\n".join(out)


# ======================================================================
# 1. 输入框标识（用户要求：固定显示当前模式与计划编号）
# ======================================================================
def test_普通模式标识():
    assert console._mode_label(_Ctx()) == "[普通]"
    assert console._ask_hint(_Ctx()) == "[普通] 你 ▸ "


def test_修改模式标识带计划编号():
    ctx = _Ctx(mode="revise", mode_plan_id="plan_x")
    assert console._mode_label(ctx) == "[改计划 · plan_x]"
    assert "plan_x" in console._ask_hint(ctx)


def test_生成与导入模式标识():
    assert console._mode_label(_Ctx(mode="plan")) == "[生成计划]"
    assert console._mode_label(_Ctx(mode="import")) == "[导入计划]"


def test_非法模式退回普通():
    assert console._mode_label(_Ctx(mode="bogus")) == "[普通]"


# ======================================================================
# 2. 模式切换命令 /退出、/模式
# ======================================================================
def test_斜杠退出回普通模式():
    ctx = _Ctx(mode="revise", mode_plan_id="plan_x")
    _res, out = _capture(ctx, console._handle_mode_command, "/exit")
    assert ctx.mode == "normal"
    assert "normal" in out or "普通模式" in out
    # 落盘：跨会话
    assert ctx.client._mode["mode"] == "normal"


def test_退出不退出程序():
    """`/exit` 只是回普通模式 —— 返回 True 表示"这条已处理"，不是 quit。"""
    ctx = _Ctx(mode="plan")
    res, _out = _capture(ctx, console._handle_mode_command, "/exit")
    assert res is True and ctx.mode == "normal"


def test_模式命令手动切换():
    """第 34 轮：命令一律英文（用户要求）。`/mode plan` 是唯一入口。"""
    ctx = _Ctx()
    _capture(ctx, console._handle_mode_command, "/mode plan")
    assert ctx.mode == "plan"
    assert ctx.client._mode["mode"] == "plan"


def test_模式命令四种都认():
    for name, expect in (("normal", "normal"), ("plan", "plan"),
                         ("revise", "revise"), ("import", "import")):
        ctx = _Ctx(mode_plan_id="plan_x")      # 给个基准，避免 revise 走菜单
        _capture(ctx, console._handle_mode_command, "/mode " + name)
        assert ctx.mode == expect, name


def test_模式命令切到改计划但没基准时给菜单():
    ctx = _Ctx()
    _res, out = _capture(ctx, console._handle_mode_command, "/mode revise")
    assert "[1] 改一份已有的计划" in out
    # 用户实测的坑：这里曾经把模式判回 normal，于是用户按下菜单里的 1 却被当闲聊。
    # 现在：模式**定在 revise**，并且菜单挂起（由主循环的 `_route_mode_menu` 接号）。
    assert ctx.mode == "revise", "打完菜单必须真的处在修改模式"
    assert ctx._menu_pending is True


def test_未知模式给用法():
    ctx = _Ctx()
    _res, out = _capture(ctx, console._handle_mode_command, "/mode 乱写")
    assert "用法" in out and "/mode" in out


def test_空revise进修改模式而不是只报用法():
    ctx = _Ctx()
    _res, out = _capture(ctx, console._handle_mode_command, "/revise")
    assert "[3] 先生成一份新计划" in out
    assert ctx.mode == "revise" and ctx._menu_pending is True


# ======================================================================
# 3. 修改模式的基准计划选择（用户提案的三选一）
#     菜单态由 `_route_mode_menu` 接号 —— 它**与模式解耦**，这就是用户实测
#     "按提示选了 1 却被当闲聊" 那个坑的修法。
# ======================================================================
def test_菜单号1列出计划_菜单号2切导入_号3切生成_号0回普通():
    # ① 号 1 → 列已有计划
    ctx = _Ctx(mode="revise")
    ctx.client = _FakeClient(plans=[{"plan_id": "plan_a", "项目": "甲", "总工期": 10,
                                     "修改时间": "2026-09-19 10:00:00"},
                                    {"plan_id": "plan_b", "项目": "乙", "总工期": 20,
                                     "修改时间": "2026-09-18 10:00:00"}])
    console._mode_menu(ctx)
    _res, out = _capture(ctx, console._route_mode_menu, "1")
    assert "plan_a" in out and "plan_b" in out
    assert ctx._plan_picker == ["plan_a", "plan_b"]

    # ② 号 2 → 导入模式
    ctx = _Ctx(mode="revise")
    console._mode_menu(ctx)
    _res, out = _capture(ctx, console._route_mode_menu, "2")
    assert ctx.mode == "import" and "路径" in out

    # ③ 号 3 → 生成模式
    ctx = _Ctx(mode="revise")
    console._mode_menu(ctx)
    _res, out = _capture(ctx, console._route_mode_menu, "3")
    assert ctx.mode == "plan" and "生成计划" in out

    # ④ 号 0 → 普通模式
    ctx = _Ctx(mode="revise")
    console._mode_menu(ctx)
    _res, out = _capture(ctx, console._route_mode_menu, "0")
    assert ctx.mode == "normal" and "普通模式" in out


def test_菜单号带方括号也认():
    ctx = _Ctx(mode="revise")
    console._mode_menu(ctx)
    _capture(ctx, console._route_mode_menu, "[3]")
    assert ctx.mode == "plan"


def test_菜单态下乱输入的兜底提示():
    """菜单开着但用户说了句没用的 → 重打菜单（不能静默、不能掉进意图识别）。"""
    ctx = _Ctx(mode="revise")
    _res, out = _capture(ctx, console._handle_mode_input, "我不知道该选哪个")
    assert "[1] 改一份已有的计划" in out
    assert ctx.mode == "revise"


def test_挑计划后真的打开并进修改模式():
    plan = _plan("plan_b", 20, "乙项目")
    ctx = _Ctx(mode="revise")
    ctx.client = _FakeClient(plans=[{"plan_id": "plan_b", "项目": "乙", "总工期": 20}],
                             plan=plan)
    console._mode_menu(ctx)
    _capture(ctx, console._route_mode_menu, "1")        # 列出计划
    _res, out = _capture(ctx, console._handle_plan_pick, "1")   # 选第 1 份
    assert ctx.current_plan_id == "plan_b"
    assert ctx.mode == "revise" and ctx.mode_plan_id == "plan_b"
    assert ("post_baseline", "plan_b") in ctx.client.calls
    assert ctx._plan_picker == []
    assert "已进入修改模式" in out


def test_没计划时提示改用3或2():
    ctx = _Ctx(mode="revise")
    ctx._plan_picker = []
    _res, out = _capture(ctx, console._revise_pick_existing)
    assert "还没有已生成的计划" in out


def test_菜单态与模式解耦_模式丢了也能选():
    """真实缺陷回归（用户实测）：菜单打出来之后，即使模式状态没跟上（旧实现把它
    判回 normal），用户按菜单打 1 也必须被菜单接住 —— 而不是掉进意图识别当闲聊。"""
    ctx = _Ctx(mode="normal")            # 故意"模式没跟上"
    console._mode_menu(ctx)              # 但菜单是开着的
    ctx.client = _FakeClient(plans=[{"plan_id": "plan_x", "项目": "甲", "总工期": 10}])
    _res, out = _capture(ctx, console._route_mode_menu, "1")
    assert "plan_x" in out, "菜单号必须由菜单自己接，不能看当前模式"


def test_菜单号在生成模式下也归菜单管():
    """连"模式"都不该干扰菜单：生成模式下菜单开着，按 1 仍然列计划。"""
    ctx = _Ctx(mode="plan")
    console._mode_menu(ctx)
    ctx.client = _FakeClient(plans=[{"plan_id": "plan_y", "项目": "乙", "总工期": 20}])
    _res, out = _capture(ctx, console._route_mode_menu, "1")
    assert "plan_y" in out


# ======================================================================
# 4. 修改模式的裸输入（第 34 轮语义，用户明确要求）
#    「不要将任何输入都识别为修改，应该还是默认闲聊，能够基于某个计划来回答问题，
#      如果识别到修改意图时，再向用户确认修改项。」
# ======================================================================
def test_修改模式里没看出修改意图就当聊天():
    """关键回归：**概况/提问**这类输入绝不能被当成修改（用户实测那条就是这个）。"""
    calls = []

    class _C(_FakeClient):
        def post_revise(self, plan_id, text):
            calls.append((plan_id, text))
            return 200, {"summary": "", "applied": []}

    ctx = _Ctx(client=_C(), mode="revise", mode_plan_id="plan_x")
    ctx.current_plan = _plan("plan_x", 966, "某项目")
    seen = {}
    saved = console.run_chat
    console.run_chat = lambda c, t, force_mode=None, chat_scope=None: seen.update(
        {"text": t, "mode": force_mode})
    try:
        _capture(ctx, console._handle_mode_input, "告诉我目前这个计划的概况")
    finally:
        console.run_chat = saved
    assert not calls, "没看出修改意图就不能调修改接口：%s" % calls
    assert seen.get("mode") == "normal", "这是聊天，按普通模式问"
    assert "计划" in seen.get("text", ""), "应该把当前计划摘要带进上下文"


def test_修改模式里识别到修改意图先预览再确认():
    class _C(_FakeClient):
        def __init__(self):
            _FakeClient.__init__(self)
            self.previewed = []

        def post_revise_preview(self, plan_id, text):
            self.previewed.append((plan_id, text))
            return 200, {"dry_run": True,
                         "applied": [{"target": "5.1.1.1", "field": "duration", "value": 20}],
                         "rejected": [], "summary": "把 5.1.1.1 的工期改成 20"}

    ctx = _Ctx(client=_C(), mode="revise", mode_plan_id="plan_x")
    _res, out = _capture(ctx, console._handle_mode_input, "把 5.1.1.1 的工期改成 20")
    assert ctx.client.previewed, "识别到修改意图要先算预览"
    assert "5.1.1.1" in out and "确认要改吗" in out, out
    assert getattr(ctx, "_revise_pending", "") == "把 5.1.1.1 的工期改成 20"


def _preview_with_one_change(plan_id, text):
    """预览**有可执行项**时的回放（第 35 轮：空预览会退回聊天，不再问 y/n）。"""
    return 200, {"dry_run": True, "summary": text,
                 "applied": [{"target": "5.1.1.1", "field": "duration", "value": 20}],
                 "rejected": []}


def test_预览后按y才真的改():
    class _C(_FakeClient):
        def post_revise_preview(self, plan_id, text):
            return _preview_with_one_change(plan_id, text)

    ctx = _Ctx(client=_C(), mode="revise", mode_plan_id="plan_x")
    ctx.current_plan_id = "plan_x"
    _capture(ctx, console._handle_mode_input, "把 5.1.1.1 的工期改成 20")
    _res, out = _capture(ctx, console._route_revise_confirm, "y")
    assert ("post_revise", "plan_x", "把 5.1.1.1 的工期改成 20") in ctx.client.calls


def test_预览后不确认就什么都不改():
    class _C(_FakeClient):
        def post_revise_preview(self, plan_id, text):
            return _preview_with_one_change(plan_id, text)

    ctx = _Ctx(client=_C(), mode="revise", mode_plan_id="plan_x")
    ctx.current_plan_id = "plan_x"
    _capture(ctx, console._handle_mode_input, "把 5.1.1.1 的工期改成 20")
    _res, out = _capture(ctx, console._route_revise_confirm, "算了")
    assert not [c for c in ctx.client.calls if c[0] == "post_revise"], ctx.client.calls
    assert "已取消" in out


@pytest.mark.parametrize("text,is_revise", [
    ("把 5.1.1.1 的工期改成 20", True),
    ("把钢筋工班组改成 12 人", True),
    ("缩短总工期", True),
    ("把主体结构的开工日期提前 3 天", True),
    ("删掉 5.1.1.2 这道工序", True),
    # 第 36 轮：用户实测截图里的原话，旧词表（没有"修改"、没有"项目名"）把它挡在
    # 门外，于是被当闲聊丢给模型，模型又照着 plan_qa.txt 回了一句
    # 「修改项目名称属于计划级别的变更，目前我无法直接执行此操作」——**假答案**。
    ("我想修改这个项目名为NUS大楼", True),
    ("我想修改这个项目名称为NUS大楼", True),
    ("改名为NUS大楼", True),
    ("这个项目叫NUS大楼", True),
    ("把项目的名字设置成海之子大厦", True),
    ("告诉我目前这个计划的概况", False),
    ("现在总工期是多少？", False),
    ("这个计划为什么这么长", False),
    ("你好", False),
    ("这个工期怎么改？", False),          # 是"问怎么改"，不是"要改"
    ("主体结构整体加 3 天", False),        # 数量副词没带对象 → 不当修改（保守优先）
    # 第 36 轮：这些是"让我解释"，句子里却带着"计划/项目"这类修改对象，
    # 必须留在闲聊，否则每次提问都白花一次 /revise 预览调用。
    ("帮我把这份计划讲一下", False),
    ("这个项目叫什么", False),
    ("总工期是多少", False),
    ("介绍一下这份计划", False),
    # 第 36 轮 Phase 3：新能力要进得来
    ("把 5.1.1.1 的名字改成 地下室防水", True),
    ("5.1.1.2 改名为 模板加固", True),
    ("把开工日期改到 2026-07-01", True),
    ("总工期改成 306 天", True),
    ("增加一个工序：地下室防水", True),
    ("新增工序：屋面保温", True),
    ("删除 4.1.1.1 这条工序", True),
    ("把 5.1.1.2 删掉", True),
    # 第 36 轮 Phase 3：寒暄里也含"改"（"改天"），句子里还带着"计划"，字面一撞就会
    # 被判成"要改计划"。它在中文里是"另找一天"，必须留在闲聊。
    ("改天再生成一份计划吧", False),
    ("改日再说吧", False),
    # 做不到的意图也要进到解析器（由解析器如实回"做不到 + 能做的是…"），
    # 而不是被丢给模型编一句假答案。
    ("把层数改成 5 层", True),
    ("栋数改成 3 栋", True),
])
def test_修改意图判定(text, is_revise):
    assert console._looks_like_revision(text) is is_revise, text


@pytest.mark.parametrize("text,maybe", [
    # 弱闸门：闸门漏判时的"再想一下" —— 句子里有修改对象、又不是提问语气
    ("我想修改这个项目名为NUS大楼", True),
    ("把项目的名字设置成海之子大厦", True),
    ("帮我改改这个计划的名字", True),
    ("总工期是多少", False),            # 提问语气
    ("这个项目叫什么", False),           # 提问语气
    ("帮我把这份计划讲一下", False),       # 是"让我解释"
    ("你好", False),
    ("", False),
])
def test_闸门漏判时也要再想一下(text, maybe):
    """`_maybe_revision` 是闸门的兜底：宁可多花一次预览调用，也不能把用户的
    真实修改请求丢给闲聊（闲聊会给出"我做不到"这种假答案）。"""
    assert console._maybe_revision(text) is maybe, text


def test_普通模式下裸输入不当作修改():
    ctx = _Ctx()
    assert console._handle_mode_input(ctx, "把工期改成 20") is False


# ======================================================================
# 5. 普通模式里"我要改计划" → 直接进修改模式（省一次意图识别）
# ======================================================================
def test_说改计划且已有基准则直接进模式():
    assert console._wants_revise_mode("我要改计划") is True
    assert console._wants_revise_mode("帮我修改计划") is True
    assert console._wants_revise_mode("生成一份计划") is False


# ======================================================================
# 6. 输入留档 / WBS 留档（编号 + 来源）
# ======================================================================
def test_输入留档_文本():
    iid = A.save_input("生成计划：住宅 3 层", run_id="run_1")
    assert iid.startswith("in_")
    rec = A.get_input(iid)
    assert rec["类型"] == "文本" and rec["文本"].startswith("生成计划")
    assert rec["run_id"] == "run_1"


def test_输入留档_文件只存路径():
    iid = A.save_input("看这个文件 D:\\a\\项目.docx", file_path="D:\\a\\项目.docx",
                       run_id="run_2")
    rec = A.get_input(iid)
    assert rec["类型"] == "文件"
    assert rec["文件路径"] == "D:\\a\\项目.docx"
    assert rec["文本"] == "", "文件输入只存路径，不复制内容"


def test_输入编号含内容指纹():
    a = A.save_input("同一句话")
    b = A.save_input("另一句话")
    assert a != b
    assert A.input_id_of("x", "y") != A.input_id_of("x", "z")


def test_wbs留档带输入编号与统计():
    wbs = {"phases": [{"phase": "施工准备", "work_packages": [
        {"id": "1.1", "name": "临建", "sub_packages": [
            {"id": "1.1.1", "name": "场地平整", "quantity": 1, "unit": "项"}]}]}]}
    path = A.save_wbs(wbs, run_id="run_9", input_id="in_abc",
                      params={"floors": 3, "total_area": 1500}, source="流水线末")
    assert path
    rec = A.get_wbs("run_9")
    assert rec["input_id"] == "in_abc"
    assert rec["统计"] == {"阶段": 1, "工作包": 1, "工序": 1}
    assert rec["参数摘要"] == {"floors": 3, "total_area": 1500}
    rows = A.list_wbs()
    assert rows and rows[0]["run_id"] == "run_9"
    assert "树" not in rows[0], "列表不该带树本体（响应会很大）"


def test_wbs留档_空树不存():
    assert A.save_wbs({"phases": []}, run_id="run_x") == ""
    assert A.save_wbs(None, run_id="run_x") == ""


# ======================================================================
# 7. 计划列表：只读，绝不创建目录
# ======================================================================
def test_计划列表_不创建任何目录():
    A.list_plans()
    assert list(A.PLANS_DIR.iterdir()) == [], "只读扫描不许 mkdir"


def test_计划列表读到交付物本体():
    (A.PLANS_DIR / "plan_t1.json").write_text(
        json.dumps(_plan("plan_t1", 123, "甲项目"), ensure_ascii=False), encoding="utf-8")
    rows = A.list_plans()
    assert len(rows) == 1
    r = rows[0]
    assert r["plan_id"] == "plan_t1" and r["总工期"] == 123
    assert r["来源"] == "交付物" and r["项目"] == "甲项目"


def test_计划列表优先档案当前版():
    # 归档目录里的当前版优先于交付物本体
    d = A.PLANS_DIR / A.ARCHIVE_NAME / "plan_t2"
    d.mkdir(parents=True)
    (d / "当前版本.json").write_text(
        json.dumps(_plan("plan_t2", 55, "归档项目"), ensure_ascii=False), encoding="utf-8")
    (A.PLANS_DIR / "plan_t2.json").write_text(
        json.dumps(_plan("plan_t2", 99, "本体项目"), ensure_ascii=False), encoding="utf-8")
    rows = A.list_plans()
    assert len(rows) == 1 and rows[0]["总工期"] == 55 and rows[0]["来源"] == "档案"


def test_坏文件不让列表崩():
    (A.PLANS_DIR / "plan_bad.json").write_text("{ not json", encoding="utf-8")
    assert A.list_plans() == []


# ======================================================================
# 8. 模式持久化（跨会话）
# ======================================================================
def test_模式落盘并读回():
    A.save_mode("revise", "plan_x")
    assert A.load_mode() == {"mode": "revise", "plan_id": "plan_x"}


def test_非法模式读回时归一到normal():
    A.save_mode("bogus", "plan_x")
    assert A.load_mode()["mode"] == "normal"


def test_启动时恢复模式():
    client = _FakeClient(mode={"mode": "revise", "plan_id": "plan_keep"})
    ctx = _Ctx(client=client)
    console._load_mode(ctx)
    assert ctx.mode == "revise" and ctx.mode_plan_id == "plan_keep"


def test_后端读不到模式时不影响启动():
    class _Boom(_FakeClient):
        def get_mode(self):
            raise RuntimeError("后端没起")
    ctx = _Ctx(client=_Boom())
    console._load_mode(ctx)          # 不许抛
    assert ctx.mode == "normal"


# ======================================================================
# 9. /plans /open /wbs /inputs 四条命令
# ======================================================================
def _dispatch(ctx, text):
    out = commands.dispatch(ctx, text)
    return PLAIN(str(out or ""))


def test_plans命令列出并提示open():
    ctx = _Ctx(client=_FakeClient(plans=[{"plan_id": "plan_a", "项目": "甲", "总工期": 10,
                                          "审计状态": "未审计"}]))
    out = _dispatch(ctx, "/plans")
    assert "plan_a" in out and "/open" in out


def test_plans命令空列表给人话():
    out = _dispatch(_Ctx(client=_FakeClient(plans=[])), "/plans")
    assert "还没有" in out


def test_open命令装载计划并进修改模式():
    plan = _plan("plan_z", 88, "丙项目")
    ctx = _Ctx(client=_FakeClient(plan=plan))
    out = _dispatch(ctx, "/open plan_z")
    assert ctx.current_plan_id == "plan_z"
    assert ctx.mode == "revise" and ctx.mode_plan_id == "plan_z"
    assert "修改模式" in out


def test_open不存在的编号给指路():
    ctx = _Ctx(client=_FakeClient(plan=None))
    out = _dispatch(ctx, "/open plan_missing")
    assert "/plans" in out


def test_import缺少参数时教怎么给路径():
    out = _dispatch(_Ctx(), "/import")
    assert "路径" in out


def test_import文件不存在():
    out = _dispatch(_Ctx(), "/import D:\\no\\such\\plan.json")
    assert "找不到" in out


def test_import损坏的json(tmp_path):
    bad = tmp_path / "plan_bad.json"
    bad.write_text("{ 不是 json", encoding="utf-8")
    out = _dispatch(_Ctx(), "/import %s" % bad)
    assert "不是可读的 JSON" in out


def test_import不像计划的json被拦下(tmp_path):
    f = tmp_path / "plan_wrong.json"
    f.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    out = _dispatch(_Ctx(), "/import %s" % f)
    assert "导入失败" in out


def test_wbs命令空档给人话():
    out = _dispatch(_Ctx(), "/wbs")
    assert "还没有留档" in out


def test_wbs命令列出树与输入编号():
    wbs = {"phases": [{"phase": "施工准备", "work_packages": [
        {"id": "1.1", "name": "临建", "sub_packages": [
            {"id": "1.1.1", "name": "场地平整", "quantity": 1, "unit": "项"}]}]}]}
    A.save_wbs(wbs, run_id="run_cmd", input_id="in_zzz", params={"floors": 3})
    out = _dispatch(_Ctx(), "/wbs")
    assert "run_cmd" in out and "in_zzz" in out and "施工准备" not in out


def test_inputs命令列出文本与文件():
    A.save_input("生成计划", run_id="r1")
    A.save_input("见文件", file_path="D:\\x.docx", run_id="r2")
    out = _dispatch(_Ctx(), "/inputs")
    assert "生成计划" in out and "D:\\x.docx" in out and "文本" in out and "文件" in out


def test_help里有新命令():
    out = _dispatch(_Ctx(), "/help")
    for cmd in ("/plans", "/open", "/import", "/wbs", "/inputs"):
        assert cmd in out, cmd
    assert "/retry" not in out, "主链没有暂停点，死命令已从帮助撤下"


def test_后端标签显示真实地址():
    """`--url` 启动时横幅不能还写"云端主后端 (localhost:8000)"（实测发现）。"""
    import switch

    class _C:
        base_url = "http://127.0.0.1:8015"

    ctx = _Ctx(client=_C())
    ctx.backend = "cloud"
    desc = console._backend_desc(ctx)
    assert "8015" in desc, desc
    # 默认地址时保持原标签（不加括号）
    class _C2:
        base_url = switch.BACKENDS["cloud"]["url"]

    ctx2 = _Ctx(client=_C2())
    ctx2.backend = "cloud"
    assert console._backend_desc(ctx2) == switch.describe("cloud")


# ======================================================================
# 10. 主循环集成：菜单要真的走通（用户实测的那个坑的端到端回归）
# ======================================================================
def test_主循环里菜单按号走通_不会掉进意图识别(monkeypatch, tmp_path):
    """端到端复现用户实测：`/mode revise` → `1` → `1`。

    旧实现的真实后果是：第 2 个 `1` 掉进普通输入被当成闲聊（`run_chat` 被调用）。
    现在必须：菜单接住号码、列出计划、打开计划、并在 `run_chat` **零调用**的前提下
    把模式切到 revise。
    """
    import tui as tui_mod

    seq = []
    queue = ["/mode revise", "1", "1"]
    plan = _plan("plan_e2e", 77, "端到端项目")
    client = _FakeClient(plans=[{"plan_id": "plan_e2e", "项目": "端到端项目",
                                 "总工期": 77, "修改时间": "2026-09-19 10:00:00"}],
                         plan=plan)

    class _FakeTui:
        vt = False

        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def ask(self, *a, **k):
            if not queue:
                raise EOFError
            asked.append(queue[0])
            return queue.pop(0)

        def out(self, text, **kw):
            seq.append(("out", PLAIN(str(text))))

        def status(self, *a, **k):
            pass

        def clear_status(self):
            pass

        def suspend_mouse(self):
            pass

        def resume_mouse(self):
            pass

    asked = []
    monkeypatch.setattr(console.tui, "Tui", _FakeTui)
    monkeypatch.setattr(console, "run_chat",
                        lambda ctx, text, **kw: seq.append(("run_chat", text)))
    monkeypatch.setattr(console.Ctx, "client", client, raising=False)

    seen_ctx = {}
    real_ctx_init = console.Ctx.__init__

    def _init(self, **kw):
        real_ctx_init(self, **kw)
        self.client = client
        seen_ctx["ctx"] = self
    monkeypatch.setattr(console.Ctx, "__init__", _init)

    saved = tui_mod._current
    try:
        console.main([])
    finally:
        tui_mod._current = saved

    ctx = seen_ctx.get("ctx")
    assert ctx is not None
    out = "\n".join(t for k, t in seq if k == "out")
    assert not [k for k, _ in seq if k == "run_chat"], \
        "菜单号绝不能被当成普通输入送进流水线（那正是用户看到的'重新分向闲聊'）。" \
        "实际输出：\n%s\n调用了：%s" % (out, [t for k, t in seq if k == "run_chat"])
    assert ctx.mode == "revise", ctx.mode
    assert ctx.mode_plan_id == "plan_e2e", ctx.mode_plan_id
    assert ctx.current_plan_id == "plan_e2e"
    out = "\n".join(t for k, t in seq if k == "out")
    assert "本机已有 1 份计划" in out, out
    assert "已进入修改模式" in out
    # 提示符里带上基准计划编号（用户要求固定可见）
    assert "plan_e2e" in console._ask_hint(ctx)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
