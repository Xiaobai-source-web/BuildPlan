"""pytest 全局配置：把所有会写盘的测试输出，重定向到临时目录。

背景（务必保留此隔离）：
  部分测试会跑完整流水线（test_contracts）或直接生成交付物（test_delivery）。
  若不隔离，它们会写进生产目录「输出结果/」，并且触发 delivery._maintain 的
  MAX_KEEP=6 滚动淘汰 —— 连跑几次 pytest 就会把真实交付物静默删掉；
  同时还会覆盖 backend/plans/ 下的示例计划。

  注意：这里刻意用普通 mkdir 而不是 tempfile.mkdtemp / pytest 的 tmp_path，
  因为在本机沙箱下，那两种方式创建的目录后续写入会被拒绝访问。
"""

import os
import shutil
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_outputs(monkeypatch):
    """把交付物目录与计划落盘目录都指向**本进程独有**的临时目录。

    必须按进程隔离：本机可能同时跑多个 pytest（主流程与并行子代理各跑一份，
    已在实践中遇到过），共用同一个目录会互相 rmtree，产生 FileNotFoundError、
    "缺少 plan_final" 这类**与环境有关、与被测代码无关**的假失败。
    """
    from pipeline import config

    base = BACKEND / "_test_tmp"
    root = base / ("p{}".format(os.getpid()))
    out = root / "outputs"
    plans = root / "plans"
    out.mkdir(parents=True, exist_ok=True)
    plans.mkdir(exist_ok=True)

    monkeypatch.setattr(config, "DELIVERABLES_DIR", out, raising=False)
    monkeypatch.setattr(config, "PLANS_DIR", plans, raising=False)

    # 第 42 轮（测试隔离缺口）：`terminal/renderer.plans_dir()` 不经过 `config`，
    # 而是读 `BUILDPLAN_PLANS_DIR`（默认 `<terminal>/plans`）。不设这个变量，
    # 任何触发"终端保存计划 / `/show` 写 HTML"的用例都会写进**真实**
    # `terminal/plans/`（探针实测：`REALDIR-WRITE: ...\terminal\plans`）——
    # 既污染真实运行产物，也让 xdist 多 worker 抢同一个目录。
    # 这里指到同一个按 pid 隔离的 plans 目录，与 config.PLANS_DIR 同源。
    monkeypatch.setenv("BUILDPLAN_PLANS_DIR", str(plans))
    yield

    for p in (out, plans):
        shutil.rmtree(p, ignore_errors=True)
    try:
        root.rmdir()
    except OSError:
        pass
    try:
        base.rmdir()          # 没有其他进程在用就顺手收掉父目录
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _no_network_llm(monkeypatch):
    """测试期间强制"没有 API Key"，保证任何测试都不可能真的调用大模型。

    背景：backend/.env 里可能存着真实 QWEN_API_KEY，而部分节点的 llm_usable
    会回退到 config.LLM_API_KEY —— 那样即使传 llm=None 也会联网，既慢又烧额度
    （实测曾有一次测试真的发出了请求、耗时 42 秒）。

    这里把 Key 清空后，LLMClient 会在调用时报"未配置 QWEN_API_KEY"，
    各节点按既有降级逻辑走确定性兜底，测试结果不受影响。
    """
    from pipeline import config

    monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_mode_file(monkeypatch):
    """把「终端模式」文件也隔离掉（第 34 轮加）。

    模式是**跨会话**状态（`plans/档案/_session/模式.json`），用户手选后会落盘。
    测试里若不隔离，某个用例切到 plan 模式就会漏进后面的用例 —— 实测踩到：
    `test_tui_scrollback` 的整链路用例读回了上一轮留下的 `plan`，于是提示符变成
    `[生成计划]`、状态区被占，断言"已上翻"失败；全量套件也因此跑到超时。

    第 37 轮修：原来这里用 `tmp_path_factory.mktemp()`，与本文件开头第 9-10 行
    写明的教训（本机沙箱下 tmp_path / tempfile.mkdtemp 建的目录后续访问会被
    `WinError 5 拒绝访问`）自相矛盾 —— 结果是**整个套件 1387 个用例全部 setup 报错**，
    pytest 也无法清理自己的 basetemp（`cleanup_dead_symlinks` 抛 PermissionError）。
    现在和 `_isolate_outputs` / `_isolate_llm_profiles` 统一：普通 mkdir + 按 pid 隔离。
    """
    mode_dir = BACKEND / "_test_tmp" / ("p{}".format(os.getpid()))
    mode_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUILDPLAN_MODE_FILE", str(mode_dir / "模式.json"))
    yield


@pytest.fixture(autouse=True)
def _isolate_llm_profiles(monkeypatch):
    """把「模型档位」配置也隔离掉（第 35 轮加）。

    契约：档位是**跨会话**状态（`backend/llm_profiles.json`），而且
    `config.refresh_active_profile()` 会把它覆盖到 LLM_API_KEY/BASE_URL/MODEL 上。

    三条隔离理由：
      1. 用例若真读了用户的档位文件，就会拿到他的 key（`_no_network_llm` 的清空
         会被 refresh 顶掉，存在联网风险）；
      2. `refresh` 会覆盖 `_no_network_llm` 设的空 key —— 那是本套件唯一的
         "绝不联网"保障，不能被绕过；
      3. 用例（尤其 UI/命令用例）会建档、切档，不能写进真实文件。
    """
    from pipeline import config

    tmp = BACKEND / "_test_tmp" / ("p{}".format(os.getpid()))
    tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUILDPLAN_LLM_PROFILES", str(tmp / "llm_profiles.json"))
    # 档位为空 ⇒ refresh 会退回"无档基线"；这里把基线也钉死成空 key，
    # 于是无论用例怎么调 refresh，都不可能拿到一个真 key。
    monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _norm_defaults_gate_open_for_synthetic_activities(monkeypatch):
    """第 38 轮引入、第 41 轮（2026-09-20 政策变更）后**仍然保留**的替身。

    它替身的是**合成活动**，不是政策：用例里的 L4（`EARTH0032` / `REBAR_NEW_BEAM` …）
    是**合成**数据，它们在真实库里的默认定额行是给真实项目审的，与用例意图无关。
    若让真闸门读真库，用例结果就取决于"真实库里这条 L4 被审成什么状态" —— 实测
    `test_norm_method_conflict` 的"定额应生效"用例就被那条 pending 行挡掉过
    （`EARTH0032` 恰好是 pending）。

    ⚠️ 为什么第 41 轮放开了 AI 估算（`estimated`）之后它**还不能删**：真闸门仍然会拦
    两档 —— `review_state='rejected'`（人工否决）与零值/空值行（`REASON_NO_VALUE`）。
    真实项目对库的任何一次审改，都可能把某条合成 L4 的行标成 rejected，从而让 1500 个
    既有用例随**库内容**而不是**被测逻辑**变红。本替身切断的就是这种耦合；AI 政策本身
    已由 `norm_defaults` 放开，不靠它。

    这里把闸门对测试进程关掉（等价于"这些 L4 没有意见"）。闸门自身的语义由
    `test_norm_defaults.py` 单独覆盖 —— 它用 `_real_gate()` 显式取回真函数，
    所以本替身不会让闸门逻辑失去覆盖。
    """
    from pipeline import norm_defaults as nd

    monkeypatch.setattr(nd, "gate_open", lambda *a, **k: (True, ""), raising=False)
    monkeypatch.setattr(nd, "pre_approved", lambda *a, **k: True, raising=False)
    nd.clear_cache()
    yield
    nd.clear_cache()


# ---------------------------------------------------------------------------
# 覆盖 pytest 官方的临时目录装置（第 37 轮）
#
# 为什么必须覆盖：`tmp_path` / `tmp_path_factory` 走 `_pytest.pathlib.
# make_numbered_dir()`，在本机沙箱下它建出来的目录**随后访问会被拒绝**
# （`PermissionError: [WinError 5]`）—— 落点是 `%TEMP%\dsh-XXXX\pytest-of-<user>`。
# 实测：即便把 TEMP 或 --basetemp 指到工作区内，pytest 自己都会在自己的
# basetemp 上 `cleanup_dead_symlinks()` 失败（iterdir 被拒）。
# 而**普通 `Path.mkdir()` 建出来的目录完全正常**（同样是工作区内的目录，
# iterdir 可用）。所以这里用普通 mkdir 覆盖官方装置，语义保持一致。
#
# 影响面：本套件里有 3 个文件在用（test_llm_profiles / test_main_llm_api /
# test_modes），不覆盖就是 130 个用例 setup 报错的假失败。
# ---------------------------------------------------------------------------

class _PlainTmpPathFactory:
    """`tmp_path_factory` 的替代品：普通 mkdir，不用 pytest 的 numbered dir。"""

    def __init__(self, base):
        self._base = base
        self._counter = 0

    def mktemp(self, basename="tmp", numbered=True):
        self._counter += 1
        name = "{}{}".format(basename, self._counter) if numbered else basename
        path = self._base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def getbasetemp(self):
        return self._base


@pytest.fixture(scope="session")
def tmp_path_factory():
    base = BACKEND / "_test_tmp" / ("p{}".format(os.getpid())) / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return _PlainTmpPathFactory(base)


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("test")


@pytest.fixture
def tmpdir(tmp_path):
    """兼容极少数还用 py.path 写法的用例（py.path 不可用时直接退化为 Path）。"""
    try:
        import py.path
    except ImportError:
        return tmp_path
    return py.path.local(str(tmp_path))


# ---------------------------------------------------------------------------
# 域 9.2 通用护栏：给 pytest 的失败 diff 装一道长度闸门
#
# 背景：`assert x not in <超长字符串>` **失败**时，pytest 9 走
# `_pytest.assertion.compare_text._notin_text` → difflib 求最长公共子串，
# 复杂度约 O(n·m)。交付物看板 HTML 内嵌 ECharts，单份 **1,052,775 字符**
# 且第三方库整个挤在一行里 —— 实测两条这样的断言各自耗时
# **567.95 s / 505.86 s，合计 1074 s = 全量套件的 97%**。
#
# 这里把 util 里那个名字换成一个**只报定位与上下文**的版本：
#   · 语义完全不变（断言照旧失败，只是失败信息换了写法）；
#   · 短字符串（≤ _ASSERT_TEXT_LIMIT）仍走官方实现，保留漂亮的 diff；
#   · 超长字符串不再跑 difflib。
#
# 为什么打 `util._notin_text` 而不是 `compare_text._notin_text`：
# pytest 9 的 `util.py` 是 `from ...compare_text import _notin_text`——
# 它把符号绑成**自己的**模块全局，调用处 `source = _notin_text(...)` 在
# **调用时**查 util 的全局。所以只有改 util 才拦得住（已实测确认）。
#
# 这是**兜底**，不是替代品：新写的断言请优先用 `tests/_text_guard.assert_absent`
# ——它先算布尔再断言，连一次定位都省了，且失败信息自带上下文。
# ---------------------------------------------------------------------------

_ASSERT_TEXT_LIMIT = 20000


def _install_bounded_assert_guard():
    """装上长度闸门。返回 True 表示已生效（幂等）。"""
    try:
        from _pytest.assertion import util as _pu
    except Exception:                                  # pragma: no cover
        return False
    if getattr(_pu, "_dsh_bounded_guard", False):
        return True
    _orig = getattr(_pu, "_notin_text", None)
    if _orig is None:                                  # pragma: no cover
        return False

    def _notin_text(term, text, verbose=0):
        if len(text) <= _ASSERT_TEXT_LIMIT:
            yield from _orig(term, text, verbose)
            return
        idx = text.find(term)
        lo = max(0, idx - 150)
        hi = min(len(text), idx + len(term) + 150)
        yield (
            "%r is contained here (offset %d, 文本长度 %d —— 已启用超长文本"
            "闸门，跳过 difflib 失败 diff):\n%s\n"
            % (term, idx, len(text), text[lo:hi].replace("\n", "\\n"))
        )

    _pu._notin_text = _notin_text
    _pu._dsh_bounded_guard = True
    return True


_install_bounded_assert_guard()
