# -*- coding: utf-8 -*-
"""大模型接入的"厂商无关"保证 + 允许跳过密钥（第 27 轮）

对应用户提问：
  「我看你目前插入 key 的方法总是提及千问，假如用户插入的不是千问的 key 呢，
    能否接受任何厂商的 key？支持哪些？」

**接口本来就是标准 OpenAI 兼容协议**（`POST {LLM_BASE_URL}/chat/completions`
+ `Authorization: Bearer {key}`），所以任何厂商都能用。问题出在**启动器的体验**：
它只认 `QWEN_API_KEY` 这个名字、提示语只讲通义千问、没有改端点的入口，
而且**不给 key 就直接退出** —— 而 README 写着"没有密钥也能端到端出结果"
（后端确实支持，全仓没有一处调用 `config.require_api_key()`）。

这组测试守住三件事：
  1. 密钥变量名**与厂商无关**：通用名 `LLM_API_KEY` 优先，旧名仍作别名可用；
  2. `describe_provider()` 把端点回显成人看得懂的名字（含本地模型与未知端点）；
  3. 启动器**允许跳过密钥**走确定性兜底，并且会写对 `.env`（含旧名清理）。

运行：python -m pytest backend/tests/test_llm_provider.py -q
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _load_launcher():
    """导入根目录的 `一键测试.py`（文件名不是合法标识符，只能按路径加载）。"""
    path = ROOT / "一键测试.py"
    spec = importlib.util.spec_from_file_location("yijian_ceshi", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reload_config(monkeypatch, **env):
    """在指定环境变量下重新加载 config（它是在 import 时读环境变量的）。

    ⚠️ 必须把这些变量都**显式置为空串**（连 `LLM_BASE_URL`/`LLM_MODEL` 也要）：
    仓库里真实的 `backend/.env` 会带着它们，而 config 用 `os.environ.setdefault` 装载 ——
    空串是"已设置"的，setdefault 不会覆盖；而 config 现在对端点/模型也用 `or 默认值`
    处理空串，所以这样才能真正隔离掉 .env，测的是环境变量本身。
    """
    for name in ("LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
                 "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from pipeline import config
    return importlib.reload(config)


# ======================================================================
# 1. 密钥变量名与厂商无关
# ======================================================================
def test_通用名_LLM_API_KEY_优先生效(monkeypatch):
    cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-generic")
    assert cfg.LLM_API_KEY == "sk-generic"


def test_旧名_QWEN_API_KEY_仍作别名可用(monkeypatch):
    cfg = _reload_config(monkeypatch, QWEN_API_KEY="sk-legacy-qwen")
    assert cfg.LLM_API_KEY == "sk-legacy-qwen", "旧名不许失效（会打断已有用户的 .env）"


def test_旧名_DASHSCOPE_API_KEY_仍作别名可用(monkeypatch):
    cfg = _reload_config(monkeypatch, DASHSCOPE_API_KEY="sk-legacy-dashscope")
    assert cfg.LLM_API_KEY == "sk-legacy-dashscope"


def test_通用名优先于旧名(monkeypatch):
    cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-new", QWEN_API_KEY="sk-old")
    assert cfg.LLM_API_KEY == "sk-new", "两个都在时必须用通用名"


def test_端点与模型可用环境变量换厂商(monkeypatch):
    cfg = _reload_config(
        monkeypatch, LLM_API_KEY="sk-x",
        LLM_BASE_URL="https://api.deepseek.com/v1", LLM_MODEL="deepseek-chat")
    assert cfg.LLM_BASE_URL == "https://api.deepseek.com/v1"
    assert cfg.LLM_MODEL == "deepseek-chat"


def test_没给端点时默认仍是通义千问(monkeypatch):
    cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-x")
    assert cfg.LLM_BASE_URL == cfg.DEFAULT_BASE_URL
    assert cfg.LLM_MODEL == "qwen-plus"


# ======================================================================
# 2. 端点回显（避免"以为在跑千问其实不是"）
# ======================================================================
def test_describe_provider_认得各家厂商(monkeypatch):
    cases = [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "通义千问"),
        ("https://api.deepseek.com/v1", "DeepSeek"),
        ("https://api.moonshot.cn/v1", "月之暗面"),
        ("https://open.bigmodel.cn/api/paas/v4", "智谱"),
        ("https://api.siliconflow.cn/v1", "硅基流动"),
        ("https://openrouter.ai/api/v1", "OpenRouter"),
        ("https://api.openai.com/v1", "OpenAI"),
    ]
    for url, expect in cases:
        cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-x", LLM_BASE_URL=url)
        assert expect in cfg.describe_provider(), (url, cfg.describe_provider())


def test_本地模型端点要认出来(monkeypatch):
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1"):
        cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-x", LLM_BASE_URL=url)
        assert "本地模型" in cfg.describe_provider(), url


def test_未知端点原样回显不冒充厂商(monkeypatch):
    cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-x",
                         LLM_BASE_URL="https://llm.internal.corp/v1")
    out = cfg.describe_provider()
    assert "llm.internal.corp" in out, out
    for wrong in ("通义", "DeepSeek", "OpenAI"):
        assert wrong not in out, "未知端点不许冒充已知厂商：%s" % out


def test_describe_provider_带上模型名(monkeypatch):
    cfg = _reload_config(monkeypatch, LLM_API_KEY="sk-x",
                         LLM_BASE_URL="https://api.deepseek.com/v1",
                         LLM_MODEL="deepseek-reasoner")
    assert "deepseek-reasoner" in cfg.describe_provider()


# ======================================================================
# 3. 启动器：厂商预设 + 允许跳过密钥
# ======================================================================
def test_启动器的预设表覆盖主流厂商():
    mod = _load_launcher()
    names = [p[1] for p in mod.PROVIDERS]
    for must in ("通义千问", "DeepSeek", "月之暗面", "智谱", "OpenAI", "本地模型"):
        assert any(must in n for n in names), "预设缺厂商：%s（现有 %s）" % (must, names)
    nums = [p[0] for p in mod.PROVIDERS]
    assert len(nums) == len(set(nums)), nums
    assert mod.CUSTOM_CHOICE not in nums and mod.SKIP_CHOICE not in nums
    for num, name, url, model, where in mod.PROVIDERS:
        assert url.startswith("http"), (name, url)
        assert model.strip(), name
        assert where.strip(), name


def test_预设编号与配置的厂商识别对得上():
    """启动器给的 base_url，config 必须能回显成对应的厂商名（防两处漂移）。"""
    mod = _load_launcher()
    from pipeline import config as cfg
    for num, name, url, model, where in mod.PROVIDERS:
        got = next((n for host, n in cfg.PROVIDER_HOSTS if host in url), "")
        assert got, "config 不认识启动器的端点 %s（%s）" % (url, name)
        assert got.split(" ")[0][:2] in name or got[:2] in name or name[:2] in got, \
            "两处厂商名对不上：启动器「%s」 vs config「%s」" % (name, got)


def test_密钥变量名两处一致():
    """启动器与 config 的变量名/顺序必须一致，否则会出现"写了却没生效"。"""
    mod = _load_launcher()
    from pipeline import config as cfg
    assert tuple(mod.KEY_ENV_NAMES) == tuple(cfg.KEY_ENV_NAMES)
    assert mod.KEY_ENV_NAMES[0] == "LLM_API_KEY", "通用名必须排第一"


def test_启动器允许跳过密钥(monkeypatch):
    """老行为是"不给 key 就退出" —— 而 README 写着没有密钥也能端到端出结果。

    现在的保证：选 [0] 跳过 → `ensure_key()` 返回 True，并把 `.env` 里的密钥置空
    （明确置空，避免旧值阴魂不散），带着空 key 继续启动。
    """
    mod = _load_launcher()
    tmp = BACKEND / "tests" / "_tmp_launcher.env"
    # 故意留一个**占位符**旧行（会被 `_read_key` 当作"没有 key"忽略 → 走菜单），
    # 用来验证跳过密钥时会把这条作废的旧行清掉。
    tmp.write_text("# 旧配置\nQWEN_API_KEY=sk-xxxx\nPORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_FILE", str(tmp))
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("QWEN_API_KEY", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setattr("builtins.input", lambda *a, **k: mod.SKIP_CHOICE)
    try:
        assert mod.ensure_key() is True, "跳过密钥必须被允许"
        text = tmp.read_text(encoding="utf-8")
        assert "LLM_API_KEY=" in text, text
        assert "QWEN_API_KEY=sk-xxxx" not in text, \
            "旧的密钥行必须被清掉，否则下次会读到一个作废的 key：\n%s" % text
        assert "PORT=8000" in text, "其它行必须保留：\n%s" % text
    finally:
        tmp.unlink(missing_ok=True)


def test_选厂商后把端点与模型写进env(monkeypatch):
    mod = _load_launcher()
    tmp = BACKEND / "tests" / "_tmp_launcher2.env"
    tmp.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_FILE", str(tmp))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    answers = iter(["2", "sk-deepseek-abc"])          # 选 DeepSeek + 粘 key
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    try:
        assert mod.ensure_key() is True
        text = tmp.read_text(encoding="utf-8")
        assert "LLM_API_KEY=sk-deepseek-abc" in text, text
        assert "LLM_BASE_URL=https://api.deepseek.com/v1" in text, text
        assert "LLM_MODEL=deepseek-chat" in text, text
    finally:
        tmp.unlink(missing_ok=True)


def test_空密钥且不跳过则明确拒绝(monkeypatch):
    mod = _load_launcher()
    tmp = BACKEND / "tests" / "_tmp_launcher3.env"
    tmp.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_FILE", str(tmp))
    for n in mod.KEY_ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
    answers = iter(["1", ""])                          # 选千问但直接回车
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    try:
        assert mod.ensure_key() is False
        assert "sk-" not in tmp.read_text(encoding="utf-8"), "空密钥不许被写进 .env"
    finally:
        tmp.unlink(missing_ok=True)


def test_无法识别的选项要拒绝(monkeypatch):
    mod = _load_launcher()
    tmp = BACKEND / "tests" / "_tmp_launcher4.env"
    tmp.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_FILE", str(tmp))
    for n in mod.KEY_ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "99")
    try:
        assert mod.ensure_key() is False
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("  %s" % name)
    print("请用 pytest 运行（需要 monkeypatch fixture）")


# ======================================================================
# 4. .env 带 BOM 也必须能读到密钥（实测踩过的真缺陷）
# ======================================================================
def test_带BOM的env也必须能读到密钥():
    """用户实测「0 token / 本次未调用大模型」的真根因之一。

    Windows 上用记事本 / VS Code 保存的 `.env` 常带 **UTF-8 BOM**（EF BB BF），
    而 Python 的 `str.strip()` **不会**去掉 `\\ufeff` —— 用普通 utf-8 读时第一行键名
    会变成 `\\ufeffLLM_API_KEY`，密钥因此"看不见"，整条流水线静默降级成确定性兜底。

    本测试**不碰真实 backend/.env**（那是用户配置，测试里改它出过事故），
    只对 `config.iter_env_lines` 喂一个临时文件。
    """
    from pipeline import config as cfg

    tmp = BACKEND / "tests" / "_tmp_bom.env"
    tmp.write_bytes(b"\xef\xbb\xbf" + "LLM_API_KEY=sk-bom-test\nLLM_MODEL=m.model\n".encode("utf-8"))
    try:
        got = dict(cfg.iter_env_lines(tmp))
        assert got.get("LLM_API_KEY") == "sk-bom-test", got
        assert got.get("LLM_MODEL") == "m.model", got
        # 键名里不许残留 BOM/零宽字符
        for k in got:
            assert k == k.strip().lstrip("\ufeff"), repr(k)
    finally:
        tmp.unlink(missing_ok=True)


def test_没有BOM的env照常工作():
    from pipeline import config as cfg

    tmp = BACKEND / "tests" / "_tmp_nobom.env"
    tmp.write_text("LLM_API_KEY=sk-plain\n# 注释\n\nPORT=8000\n", encoding="utf-8")
    try:
        got = dict(cfg.iter_env_lines(tmp))
        assert got["LLM_API_KEY"] == "sk-plain", got
        assert got["PORT"] == "8000", got
        assert "#" not in "".join(got), got          # 注释行不许被当成键
    finally:
        tmp.unlink(missing_ok=True)


def test_不存在的env文件不报错():
    from pipeline import config as cfg
    assert list(cfg.iter_env_lines(BACKEND / "tests" / "_nope_.env")) == []
