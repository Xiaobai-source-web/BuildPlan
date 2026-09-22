"""流水线配置 — 读环境变量，密钥不进代码。

**接口是标准 OpenAI 兼容协议**（`POST {LLM_BASE_URL}/chat/completions` +
`Authorization: Bearer {key}`），所以**任何提供该协议的厂商都能用**：
通义千问/百炼、DeepSeek、月之暗面 Kimi、智谱 GLM、硅基流动、OpenRouter、
OpenAI，以及本地 vLLM / Ollama / LM Studio 等。

环境变量（真实环境变量优先于 `backend/.env`）：

  密钥：`LLM_API_KEY`（通用）· 别名 `QWEN_API_KEY` / `DASHSCOPE_API_KEY`
    —— 变量名与厂商**无关**，历史上叫 QWEN_* 只是默认厂商是千问。
  端点：`LLM_BASE_URL`   默认 https://dashscope.aliyuncs.com/compatible-mode/v1
  模型：`LLM_MODEL`      默认 qwen-plus
  其它：`LLM_TIMEOUT` · `PORT` · `HOST`

**没有密钥也能跑**：全仓没有一处调用 `require_api_key()`，缺 key 时每个依赖模型的
环节都会走确定性兜底（`tools/measure_plan.py` 就是强制无 key 跑的）。

本模块位于 pipeline/ 内，保证 `pipeline` 作为顶层包时（uvicorn / 测试）
相对导入 `from . import config` / `from .. import config` 均一致可用。
"""

import os
from pathlib import Path

def iter_env_lines(path):
    """逐行产出 .env 的 (键, 值)。

    ⚠️ 用 `utf-8-sig` 读：Windows 上用记事本 / VS Code 保存的 .env 常常带 **UTF-8 BOM**，
    而 Python 的 `str.strip()` **不会**去掉 `\\ufeff`。若按普通 utf-8 读，第一行的键名会变成
    `\\ufeffLLM_API_KEY` —— 密钥因此"看不见"，整条流水线静默降级为确定性兜底，用户只看到
    「本次未调用大模型（0 token）」而完全不知道原因（实测踩过，见 test_llm_provider.py）。
    额外再 `lstrip("\\ufeff")` 兜一层，防止 BOM 出现在中间某行。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip().lstrip("\ufeff").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        yield k.strip().lstrip("\ufeff"), v.strip().strip("\"'")


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
# 记下"模块导入之前就存在的真实环境变量"：它们是**最高优先级**（临时试一家厂商用）。
# 而 .env 的值马上会被灌进 os.environ，事后无法区分来源 —— 所以必须在这里先快照，
# 否则切档会被 .env 的旧 key 顶住（表现为"切了没生效"）。
_REAL_ENV = {_k: os.environ[_k] for _k in
             ("LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
              "LLM_BASE_URL", "LLM_MODEL") if os.environ.get(_k)}
# .env 原文值（`.env` 里没写的键不在其中；切换档位时作为最低优先级的回退基线）
_ENV_FILE_VALUES = dict(iter_env_lines(_ENV_FILE))
for _k, _v in _ENV_FILE_VALUES.items():
    os.environ.setdefault(_k, _v)

# ---- LLM（OpenAI 兼容；变量名与厂商无关，优先通用名）----
KEY_ENV_NAMES = ("LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")
LLM_API_KEY = ""
for _n in KEY_ENV_NAMES:
    if os.environ.get(_n):
        LLM_API_KEY = os.environ[_n]
        break

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
# `or` 而不是 `get(k, default)`：`.env` 里写成 `LLM_BASE_URL=`（空值）时也要落回默认，
# 否则会拿到空串，调用时报出莫名其妙的 URL 错误。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
LLM_MODEL = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))

# ---- 思考（reasoning）开关：本项目最快的那个性能开关 ----
# 实测（小米 mimo-v2.5 端点，第 6 步"补全边界条件"）：
#   默认           93.7s，其中 2115 token 是**内部思考**、正文才 952 字
#   reasoning=none 12.8s，思考 0 token、正文 1023 字   ← 7.3 倍，质量没差
# 也就是说"慢"根本不是网络或我们的代码，而是模型把时间花在了没用的思考上。
# 默认关掉：本项目的模型任务大多是"读长文、吐短 JSON"的抽取/补全，思考是纯开销；
# 想开回来的用户设 `LLM_REASONING=high`（也接受 low/medium 或 `off` 覆盖默认）。
# 端点若不认这个参数（返回 400），`llm.py` 会自动退回"不带该参数"重试一次。
REASONING_MODE = (os.environ.get("LLM_REASONING") or "none").strip().lower()
REASONING_OFF_WORDS = ("none", "off", "false", "0", "no", "disabled", "")


def reasoning_payload(override=None) -> dict:
    """把"思考开关"翻译成请求体片段（空 dict = 不加任何参数）。

    `override` 为 None 时用全局 `REASONING_MODE`；显式传字符串则只影响本次调用。
    """
    mode = REASONING_MODE if override is None else str(override).strip().lower()
    if mode in REASONING_OFF_WORDS:
        # 两个写法一起给：不同兼容实现认不同的键，多余的那个会被服务端忽略
        return {"reasoning_effort": "none", "thinking": {"type": "disabled"}}
    if mode in ("low", "medium", "high"):
        return {"reasoning_effort": mode}
    return {}

# 哨兵：区分 `refresh_active_profile()`（去读当前档）与 `refresh_active_profile(None)`
# （明确表示"没有档位"）。
_UNSET = object()

# 启动时的静态值：/.env 读到的"出厂档"。切换模型档位时以它作为回退基线
# （第 35 轮：多 key / 多端点支持，见 pipeline/llm_profiles.py）。
LLM_API_KEY_BASE = LLM_API_KEY
LLM_BASE_URL_BASE = LLM_BASE_URL
LLM_MODEL_BASE = LLM_MODEL

def env_file_values() -> dict:
    """**现读** `backend/.env`（不走 os.environ，因此不会被导入时的快照顶住）。

    为什么现读：`一键测试.py` 会在用户处于"没有档位"的状态下改 .env（它不认识档位），
    改完不重启后端也该立刻生效。`.env` 是用户手改的文件，读失败一律当空。
    """
    try:
        return dict(iter_env_lines(_ENV_FILE))
    except Exception:
        return {}


def refresh_active_profile(profile=_UNSET) -> str:
    """把**当前档**应用到本模块的 LLM_* 变量，返回生效来源说明。

    必须在 `build_pipeline()` **之前**调用：`LLMClient.__init__` 是那时从本模块
    现读三个值的，所以在这里刷新 = "发下一条消息即换模型"，无需重启后端。

    **只覆盖档位里填了的字段，且只覆盖"档位比其他来源更靠前"的值**：

      · 档位字段非空 + 真实环境变量里没有该键 → 覆盖（并记来源）
      · 档位字段为空 → **原样不动**（"只换模型不换端点"因此天然可用）
      · 一份档位都没有 → 整个函数是 no-op，绝不把值重置回 .env

    最后一条是**测试契约**：`conftest._no_network_llm` 靠把 `config.LLM_API_KEY`
    置空来保证"任何用例都不可能联网"。若这里无档也重置，就会把 `.env` 里的真 key
    顶回来，等于拆掉那道保障（实测过：探针里 refresh 之后又读到了真 key）。

    注意"真实环境变量"指的是**导入本模块之前**就存在的（快照见 `_REAL_ENV`）：
    `.env` 的值在导入时也被灌进了 `os.environ`，不能算作覆盖来源，否则它会顶住档位、
    表现为"切了不生效"。
    """
    global LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    if profile is _UNSET:
        try:
            from .llm_profiles import active_profile as _active

            profile = _active()
        except Exception:
            profile = None
    if not profile:
        return ""                       # 无档：no-op（见 docstring 最后一段）

    src = ""
    pairs = (("LLM_API_KEY", "api_key", "LLM_API_KEY", KEY_ENV_NAMES),
             ("LLM_BASE_URL", "base_url", "LLM_BASE_URL", ("LLM_BASE_URL",)),
             ("LLM_MODEL", "model", "LLM_MODEL", ("LLM_MODEL",)))
    for var, field, env_name, aliases in pairs:
        real = next((_REAL_ENV[a] for a in aliases if _REAL_ENV.get(a)), "")
        if real:
            # 真实环境变量优先 —— 但**必须把值写回去**：一旦别的来源先动过这个变量，
            # 只判断优先级而不赋值，`set LLM_MODEL=xxx` 就形同虚设（实测踩过，见
            # test_真实环境变量优先于档位）。
            globals()[var] = real
            src = (src + "；" if src else "") + "环境变量 %s" % env_name
            continue
        val = str(profile.get(field) or "").strip()
        if not val:
            continue                    # 档位没填 → 沿用现值
        globals()[var] = val
    if not src:
        src = "模型档位「%s」" % (profile.get("name") or profile.get("id") or "")
    return src

# 已知厂商：把 base_url 的主机名映射成人看得懂的名字（仅用于回显，不做任何校验）
PROVIDER_HOSTS = (
    ("dashscope.aliyuncs.com", "通义千问 / 阿里百炼"),
    ("deepseek.com", "DeepSeek"),
    ("moonshot.cn", "月之暗面 Kimi"),
    ("bigmodel.cn", "智谱 GLM"),
    ("siliconflow.cn", "硅基流动"),
    ("openrouter.ai", "OpenRouter"),
    ("openai.com", "OpenAI"),
    ("localhost", "本地模型（Ollama / vLLM / LM Studio）"),
    ("127.0.0.1", "本地模型（Ollama / vLLM / LM Studio）"),
)


def describe_provider() -> str:
    """回显当前接的是哪家端点、哪个模型 —— 避免"以为在跑千问其实不是"。"""
    url = str(LLM_BASE_URL or "")
    name = next((n for host, n in PROVIDER_HOSTS if host in url), url or "（未设置）")
    return "%s · %s" % (name, LLM_MODEL)


# ---- 服务 ----
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")

# ---- 路径 ----
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BACKEND_DIR / "prompts"
SAMPLE_DIR = BACKEND_DIR / "sample_data"
PLANS_DIR = BACKEND_DIR / "plans"
# 交付物输出到"启动 bat 同级"（一键测试.bat 所在目录），目录名固定"输出结果"
DELIVERABLES_DIR = BACKEND_DIR.parent / "输出结果"

# ---- BuildPlan 知识库（BuildPlan_KB/kb.db，完整版）----
KB_DIR = BACKEND_DIR.parent / "BuildPlan_KB"
KB_DB_PATH = KB_DIR / "kb.db"


def require_api_key() -> None:
    """显式要求密钥（**当前全仓无人调用**）。

    保留它是为了给"必须用模型"的调用方一个明确失败点；正常流水线**不调用它**，
    缺 key 时逐节点走确定性兜底，照样端到端出结果。
    """
    if not LLM_API_KEY:
        raise RuntimeError(
            "缺少 LLM API Key：请设置环境变量 LLM_API_KEY"
            "（别名 QWEN_API_KEY / DASHSCOPE_API_KEY 也可以）。\n"
            "参考 backend/.env.example。"
        )
