# -*- coding: utf-8 -*-
"""模型档位：多套 key / 随时换模型（第 35 轮）

这一轮要钉住的是**行为契约**，不是实现细节：

  1. 多档并存 —— 每档带自己的 key / 端点 / 模型，互不覆盖；
  2. 切换即生效 —— `use()` 之后 `refresh_active_profile()`，新建的 `LLMClient`
     **真的**换成了另一家（这正是 `build_pipeline()` 里发生的事，所以能在不重启后端的
     前提下换模型）；
  3. 无档 = no-op —— 一份档位都没有时，刷新**不许**改写任何值。这条是硬要求：
     `conftest._no_network_llm` 靠把 key 置空来保证用例绝不联网，若刷新会把 .env 里的
     真 key 顶回来，那道保障就被绕过了（实测踩过：探针里 refresh 后又读到了真 key）；
  4. 不许抛出 —— 配置是旁路，文件损坏 / 指向已删档 / 空文件都必须退化成"没有配置"，
     绝不能让后端起不来；
  5. key 不泄露 —— 终端与接口回显一律打码。

运行：python -m pytest backend/tests/test_llm_profiles.py -q
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import pytest  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline import llm_profiles as lp  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402

KEY_A = "sk-aaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "sk-bbbbbbbbbbbbbbbbbbbb2222"


@pytest.fixture()
def store(tmp_path_factory):
    """一份独立、干净的档位文件（不碰真实 backend/llm_profiles.json）。"""
    return tmp_path_factory.mktemp("llmprof") / "profs.json"


@pytest.fixture()
def two(store):
    """已建好两档（未启用任何一档）的存储。"""
    lp.add("千问 · 主力", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           "qwen-plus", KEY_A, path=store)
    lp.add("DeepSeek · 备用", "https://api.deepseek.com/v1",
           "deepseek-chat", KEY_B, path=store)
    return store


# ==================== 1. 多档并存 ====================

def test_两档并存互不覆盖(two):
    data = lp.load(two)
    assert [p["name"] for p in data["profiles"]] == ["千问 · 主力", "DeepSeek · 备用"]
    assert data["profiles"][0]["api_key"] == KEY_A
    assert data["profiles"][1]["api_key"] == KEY_B, "新增一档不许覆盖上一档的 key"
    assert data["active"] == "", "建档不等于启用"


def test_同名档允许并存(store):
    """同一家厂商可以存多个档（主力 key / 备用 key），不许被去重吃掉。"""
    lp.add("千问", "https://a.example/v1", "qwen-plus", KEY_A, path=store)
    lp.add("千问", "https://a.example/v1", "qwen-plus", KEY_B, path=store)
    assert len(lp.load(store)["profiles"]) == 2


def test_落盘是原子替换且格式可读(two):
    raw = two.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["profiles"][0]["name"] == "千问 · 主力"
    assert not list(two.parent.glob("*.tmp")), "临时文件必须被替换掉，不许残留"


def test_空配置是空而不是报错(store):
    assert lp.load(store) == {"active": "", "profiles": []}
    snap = lp.snapshot(store)
    assert snap["profiles"] == [] and snap["active_id"] == ""
    assert "fallback" in snap, "没有档位时也要能说清当前用的是什么"


# ==================== 2. 切换与查找 ====================

def test_按序号切换(two):
    hit = lp.use("2", path=two)
    assert hit["name"] == "DeepSeek · 备用"
    assert lp.active_profile(lp.load(two))["id"] == hit["id"]


def test_按名字与唯一前缀切换(two):
    assert lp.use("千问 · 主力", path=two)["name"] == "千问 · 主力"
    assert lp.use("DeepSeek", path=two)["name"] == "DeepSeek · 备用"
    assert lp.use("", path=two) is None, "空参数不许被当成某一档"
    assert lp.use("不存在的档", path=two) is None


def test_序号越界返回None不报错(two):
    assert lp.use("0", path=two) is None
    assert lp.use("99", path=two) is None
    assert lp.use("-1", path=two) is None


def test_前缀不唯一时不乱切(two):
    """两档都以「千问」开头时，"千问" 是歧义的，宁可不切也不猜。"""
    lp.add("千问 · 甲", "https://a.example/v1", "m1", KEY_A, path=two)
    lp.add("千问 · 乙", "https://a.example/v1", "m2", KEY_B, path=two)
    assert lp.use("千问", path=two) is None
    assert lp.use("千问 · 乙", path=two)["name"] == "千问 · 乙"


def test_删档与删当前档(two):
    lp.use("1", path=two)
    removed = lp.remove("1", path=two)
    assert removed["name"] == "千问 · 主力"
    data = lp.load(two)
    assert len(data["profiles"]) == 1
    assert data["active"] == "", "删掉当前档后 active 必须归零，不能指向不存在的档"
    assert lp.active_profile(data) is None
    assert lp.remove("99", path=two) is None


# ==================== 3. 切换 → 刷新 → 新建客户端真的换了一家 ====================

def test_切档后新建的客户端真的换了一家(two, monkeypatch):
    """这是整件事的核心：`build_pipeline()` 里就是这么新建客户端的。

    真实环境变量在导入 config 时就快照了（`_REAL_ENV`），本机并没有设它们，
    所以档位能生效；若设了，`refresh` 会按要求让环境变量优先（另有用例覆盖）。
    """
    for name in ("LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
                 "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setattr(config, "_REAL_ENV", {}, raising=False)
    monkeypatch.setenv("BUILDPLAN_LLM_PROFILES", str(two))

    lp.use("2")
    src = config.refresh_active_profile()
    assert "DeepSeek" in src, "应回显生效来源，实际：%r" % src
    client = LLMClient()
    assert client.model == "deepseek-chat"
    assert client.base_url == "https://api.deepseek.com/v1"
    assert client.api_key == KEY_B

    lp.use("1")
    config.refresh_active_profile()
    client = LLMClient()
    assert client.model == "qwen-plus"
    assert client.api_key == KEY_A, "切回来必须拿回原来那把 key（多 key 的意义所在）"


def test_无档时刷新是noop(store, monkeypatch):
    """硬要求：没有档位就不许改写任何值（否则会拆掉测试的禁网保障）。"""
    monkeypatch.setenv("BUILDPLAN_LLM_PROFILES", str(store))
    monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://keep.example/v1", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "keep-me", raising=False)
    assert config.refresh_active_profile() == ""
    assert config.LLM_API_KEY == ""
    assert config.LLM_BASE_URL == "https://keep.example/v1"
    assert config.LLM_MODEL == "keep-me"


def test_档位没填的字段沿用现值(monkeypatch, store):
    """只换模型不换端点（或反之）必须能work —— 空字段不做覆盖。"""
    item = lp.add("只换模型", "", "only-model", "", path=store)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://keep.example/v1", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "old-model", raising=False)
    config.refresh_active_profile(item)
    assert config.LLM_MODEL == "only-model"
    assert config.LLM_BASE_URL == "https://keep.example/v1", "没填 base_url 就不许清空它"


def test_真实环境变量优先于档位(monkeypatch, store):
    """用户临时 `set LLM_MODEL=xxx` 试一家厂商时，档位不许把它顶掉，且要说明原因。"""
    item = lp.add("档位模型", "https://a.example/v1", "from-profile", KEY_A, path=store)
    monkeypatch.setattr(config, "_REAL_ENV", {"LLM_MODEL": "from-env"}, raising=False)
    src = config.refresh_active_profile(item)
    assert config.LLM_MODEL == "from-env"
    assert "环境变量" in src, "被环境变量顶住时必须回显原因，否则用户以为切档坏了"


# ==================== 4. 损坏/异常一律退化，绝不抛出 ====================

@pytest.mark.parametrize("raw", ["{ 这不是 JSON", "[1,2,3]", "", "null", '"str"',
                                 '{"profiles": "不是列表"}', '{"profiles": [1, 2]}'])
def test_坏文件退化成空配置(store, raw):
    store.write_text(raw, encoding="utf-8")
    data = lp.load(store)
    assert data == {"active": "", "profiles": []} or data["profiles"] == []
    assert lp.snapshot(store)["profiles"] == data["profiles"]


def test_带BOM的配置文件也能读(store):
    store.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"active": "", "profiles": [{"id": "x", "name": "n", "api_key": KEY_A}]},
        ensure_ascii=False).encode("utf-8"))
    assert lp.load(store)["profiles"][0]["api_key"] == KEY_A


def test_active指向已删档时视为未启用(store):
    store.write_text(json.dumps({"active": "已删除", "profiles": [
        {"id": "x", "name": "仅存的一档"}]}, ensure_ascii=False), encoding="utf-8")
    data = lp.load(store)
    assert data["active"] == ""
    assert lp.active_profile(data) is None


def test_写到不可写路径返回False不抛(monkeypatch):
    """配置写不进去时只能返回 False（后端照跑，用户仍看到提示），不许把请求打成 500。"""
    bad = Path("Z:/不存在的盘/llm.json") if sys.platform == "win32" else Path("/proc/x/llm.json")
    assert lp.save({"active": "", "profiles": []}, path=bad) is False


def test_路径可用环境变量重定向(monkeypatch, tmp_path_factory):
    p = tmp_path_factory.mktemp("redir") / "自定义.json"
    monkeypatch.setenv("BUILDPLAN_LLM_PROFILES", str(p))
    assert lp.profiles_path() == p
    lp.add("x", "https://a.example/v1", "m", KEY_A)
    assert p.exists(), "应写到环境变量指定的路径"


# ==================== 5. key 不泄露 ====================

def test_打码不泄露完整key():
    masked = lp.mask_key(KEY_A)
    assert KEY_A not in masked
    assert masked.startswith("sk-aa") and masked.endswith("1111")
    assert lp.mask_key("") == "未填"
    assert lp.mask_key("abc") == "ab…bc"


def test_快照里没有完整key(two):
    lp.use("1", path=two)
    text = json.dumps(lp.snapshot(two), ensure_ascii=False)
    assert KEY_A not in text and KEY_B not in text, "快照是给界面用的，不许带完整 key"
    assert "sk-aa…1111" in text


def test_主机名回显():
    assert lp.host_of("https://api.deepseek.com/v1") == "api.deepseek.com"
    assert lp.host_of("http://localhost:11434/v1") == "localhost:11434"
    assert lp.host_of("") == "（未设置）"
    assert lp.host_of("api.deepseek.com/v1") == "api.deepseek.com"


# ==================== 6. 终端命令 ====================

class _Ctx(object):
    def __init__(self, client):
        self.client = client
        self.current_plan = None
        self.current_plan_id = None
        self.history = []
        self.backend = "local"
        self.running = False
        self.run_id = "t"
        self.show_html = None


class _FakeClient(object):
    """只记录调用并回放响应；真后端在本文件里一次都不该被连上。"""

    def __init__(self, profiles=None, use=None, add=None, remove=None):
        self._profiles = profiles
        self._use = use
        self._add = add
        self._remove = remove
        self.calls = []

    def list_llm_profiles(self):
        self.calls.append("list")
        return (200, self._profiles if self._profiles is not None else
                {"active_id": "", "active": None, "profiles": [], "fallback": {}})

    def use_llm_profile(self, key):
        self.calls.append(("use", key))
        return (200, self._use if self._use is not None else {"error": "profile not found"})

    def add_llm_profile(self, name, base_url, model, api_key, note="", use=False):
        self.calls.append(("add", name, base_url, model, api_key, use))
        return (200, self._add if self._add is not None else
                {"ok": True, "name": name, "total": 1, "switched": use})

    def remove_llm_profile(self, key):
        self.calls.append(("remove", key))
        return (200, self._remove if self._remove is not None else {"ok": True, "total": 0})


def _snap_two():
    return {
        "active_id": "id-a", "active": {"id": "id-a", "name": "千问 · 主力",
                                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                        "model": "qwen-plus"},
        "active_key_masked": "sk-aa…1111",
        "profiles": [
            {"index": 1, "id": "id-a", "name": "千问 · 主力", "host": "dashscope.aliyuncs.com",
             "model": "qwen-plus", "key_masked": "sk-aa…1111", "active": True, "has_key": True},
            {"index": 2, "id": "id-b", "name": "DeepSeek · 备用", "host": "api.deepseek.com",
             "model": "deepseek-chat", "key_masked": "sk-bb…2222", "active": False, "has_key": True},
        ],
        "fallback": {"host": "dashscope.aliyuncs.com", "base_url": "x", "model": "qwen-plus",
                     "key_masked": "sk-aa…1111", "has_key": True},
    }


def test_llm_列出档位并标出当前档():
    import commands
    out = commands.dispatch(_Ctx(_FakeClient(profiles=_snap_two())), "/llm")
    text = str(out)
    assert "当前模型档位：[1] 千问 · 主力" in text
    assert "DeepSeek · 备用" in text and "deepseek-chat" in text
    assert KEY_A not in text


def test_llm_没有档位时说明沿用env():
    import commands
    out = commands.dispatch(_Ctx(_FakeClient()), "/llm")
    text = str(out)
    assert "没有启用档位" in text
    assert "/llm-add" in text, "空列表必须告诉用户怎么加第一档"


def test_llm_use_立刻回显新档():
    import commands
    payload = {"ok": True, "active": {"id": "id-b", "name": "DeepSeek · 备用",
                                      "base_url": "https://api.deepseek.com/v1",
                                      "model": "deepseek-chat"},
               "key_masked": "sk-bb…2222", "source": "模型档位「DeepSeek · 备用」"}
    out = commands.dispatch(_Ctx(_FakeClient(use=payload)), "/llm-use 2")
    text = str(out)
    assert "已切换到模型档位" in text and "DeepSeek · 备用" in text
    assert "deepseek-chat" in text
    assert KEY_A not in text and KEY_B not in text


def test_llm_use_找不到档位时给可行动提示():
    import commands
    out = commands.dispatch(_Ctx(_FakeClient()), "/llm-use 99")
    text = str(out)
    assert "没有这个档位" in text and "/llm" in text


def test_llm_use_不带参数时给用法():
    import commands
    fake = _FakeClient()
    out = commands.dispatch(_Ctx(fake), "/llm-use")
    assert "用法" in str(out) and fake.calls == []


def test_llm_add_引导式问答能建成一档(monkeypatch):
    """一路回车/输入数字即可建档；key 必须原样提交给后端（后端才负责打码回显）。"""
    import commands
    answers = iter(["2", "我的 DeepSeek", KEY_B, "y"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    fake = _FakeClient(add={"ok": True, "name": "我的 DeepSeek", "total": 3,
                            "switched": True, "key_masked": "sk-bb…2222"})
    out = commands.dispatch(_Ctx(fake), "/llm-add")
    assert ("add", "我的 DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
            KEY_B, True) in fake.calls
    text = str(out)
    assert "已保存模型档位" in text and "已切到这一档" in text
    assert KEY_B not in text, "回显里不许出现完整 key"


def test_llm_add_取消不留档(monkeypatch):
    import commands
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    fake = _FakeClient()
    out = commands.dispatch(_Ctx(fake), "/llm-add")
    assert fake.calls == [] and "已取消" in str(out)


def test_llm_add_自定义端点缺参数时取消(monkeypatch):
    import commands
    answers = iter(["8", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    fake = _FakeClient()
    out = commands.dispatch(_Ctx(fake), "/llm-add")
    assert fake.calls == [] and "已取消" in str(out)


def test_llm_del_删除并回显剩余():
    import commands
    fake = _FakeClient(remove={"ok": True, "removed": {"name": "DeepSeek · 备用"}, "total": 1})
    out = commands.dispatch(_Ctx(fake), "/llm-del 2")
    text = str(out)
    assert ("remove", "2") in fake.calls
    assert "已删除模型档位" in text and "还剩 1 个" in text


def test_llm_命令的别名写法等价():
    """`/llm use 2` 与 `/llm-use 2` 必须等价 —— 用户记不住连字符时不该收到"未知命令"。"""
    import commands
    a, b = _FakeClient(), _FakeClient()
    commands.dispatch(_Ctx(a), "/llm use 2")
    commands.dispatch(_Ctx(b), "/llm-use 2")
    assert a.calls == b.calls == [("use", "2")]
    c = _FakeClient(remove={"ok": True, "removed": {"name": "x"}, "total": 0})
    commands.dispatch(_Ctx(c), "/llm rm 1")
    assert ("remove", "1") in c.calls


def test_llm_命令在网络失败时给红字而不是堆栈():
    import commands

    class _Boom(_FakeClient):
        def list_llm_profiles(self):
            raise RuntimeError("后端没起来")

    out = commands.dispatch(_Ctx(_Boom()), "/llm")
    assert "读取模型档位失败" in str(out)


def test_help_列出模型档位命令():
    import commands
    fake = _FakeClient()
    out = str(commands.dispatch(_Ctx(fake), "/help"))
    for cmd in ("/llm ", "/llm-use", "/llm-add", "/llm-del"):
        assert cmd in out, "帮助里必须列出 %s" % cmd
    assert "llm_profiles.json" in out
    assert fake.calls == [], "帮助不许连后端"
