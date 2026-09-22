"""多套模型配置（多 key / 多端点）—— **随时切换，全局当前档**。

为什么要有这个模块
------------------
接口是标准 OpenAI 兼容协议，于是"换一家厂商"其实只是换三个值：
`base_url` / `api_key` / `model`。但改前它们只存在 `backend/.env` 的**单一槽位**里，
换厂商 = 覆盖旧 key，想切回来还得把 key 再翻出来粘一遍。

本模块把配置从"一个槽位"变成"一叠名片"，并记住**当前生效的那一张**：

    backend/llm_profiles.json
    {
      "active": "ab12cd34",                       # 当前档的 id（"" = 不启用任何档）
      "profiles": [
        {"id": "ab12cd34", "name": "千问 · 主力", "base_url": "...",
         "model": "qwen-plus", "api_key": "sk-...", "note": ""}
      ]
    }

**语义（用户选定：全局当前档）**：切一次，之后所有对话与运行都用它，跨会话保持。
已经生成的计划不会被追溯修改 —— 换档只影响**下一次**调用模型。

生效优先级（`refresh_active()`）::

    1. 真实环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL   ← 最高，便于临时覆盖
    2. llm_profiles.json 里 active 指向的那一档              ← 平时走这条
    3. backend/.env 的静态值                                  ← 向后兼容（旧用户不填档也照跑）

为什么要在**每次运行前**刷新
----------------------------
`LLMClient` 在 `build_pipeline()` 时从 `config` 现读三个值，而 `main.py` 每次
`/chat` 都重建流水线 —— 所以在建流水线之前刷新一次 config，就能做到
"发下一条消息即换模型"，无需重启后端。见 `config.refresh_active_profile()`。

安全：**key 只留在本机**，`mask_key()` 是回显用的打码；接口与终端回显一律不打码全量。
"""

import json
import os
import time
import uuid
from pathlib import Path

# 配置文件位置：默认与 .env 同目录；测试用环境变量重定向到临时路径
# （与第 33 轮 `BUILDPLAN_MODE_FILE` 同一套路，避免用例把真实档案写脏）。
ENV_PATH = "BUILDPLAN_LLM_PROFILES"
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "llm_profiles.json"

# 已知厂商预设：与 `一键测试.py` 的首次配置菜单同一份口径（编号、名字、端点、默认模型）。
PRESETS = (
    ("1", "通义千问 / 阿里百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen-plus"),
    ("2", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("3", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("4", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("5", "硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1",
     "Qwen/Qwen2.5-7B-Instruct"),
    ("6", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("7", "本地模型（Ollama / vLLM / LM Studio）", "http://localhost:11434/v1",
     "qwen2.5:7b"),
)
CUSTOM_CHOICE = "8"


def profiles_path() -> Path:
    """当前生效的配置文件路径（测试可经环境变量重定向）。"""
    raw = (os.environ.get(ENV_PATH) or "").strip()
    return Path(raw) if raw else DEFAULT_FILE


# ---------------- 打码与展示 ----------------

def mask_key(key) -> str:
    """把 key 打码成 `sk-ab…3456`（回显用）。空值给「未填」。"""
    s = str(key or "")
    if not s:
        return "未填"
    if len(s) <= 10:
        return s[:2] + "…" + s[-2:]
    return "%s…%s" % (s[:5], s[-4:])


def host_of(base_url) -> str:
    """从 base_url 里取主机名（打码回显用，不做任何校验）。"""
    s = str(base_url or "")
    for sep in ("://",):
        if sep in s:
            s = s.split(sep, 1)[1]
    return s.split("/", 1)[0] or "（未设置）"


# ---------------- 读写 ----------------

def _new_id(name="") -> str:
    """生成稳定可读的档位 id（不依赖序号，删档后不会撞车）。"""
    return uuid.uuid4().hex[:8]


def load(path=None):
    """读配置。**任何异常都不许抛出** —— 配置坏了就当没配，绝不让终端起不来。"""
    p = Path(path) if path else profiles_path()
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError:
        return {"active": "", "profiles": []}
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"active": "", "profiles": []}
    if not isinstance(data, dict):
        return {"active": "", "profiles": []}
    items = data.get("profiles")
    profiles = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            model = str(it.get("model") or "").strip()
            base = str(it.get("base_url") or "").strip()
            if not (name or model or base):
                continue
            profiles.append({
                "id": str(it.get("id") or _new_id(name)),
                "name": name or model or host_of(base),
                "base_url": base,
                "model": model,
                "api_key": str(it.get("api_key") or ""),
                "note": str(it.get("note") or ""),
            })
    active = str(data.get("active") or "")
    if active and not any(x["id"] == active for x in profiles):
        active = ""          # 指向已删除的档 → 视为未启用，不报错
    return {"active": active, "profiles": profiles}


def save(data, path=None) -> bool:
    """写配置（原子替换：先写 .tmp 再 os.replace，避免中途断电留下半截 JSON）。"""
    p = Path(path) if path else profiles_path()
    payload = {
        "active": str((data or {}).get("active") or ""),
        "profiles": list((data or {}).get("profiles") or []),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        try:
            os.chmod(str(p), 0o600)          # 含密钥，尽力收紧权限（Windows 上可能无效）
        except OSError:
            pass
        return True
    except OSError:
        return False


def add(name, base_url, model, api_key, note="", path=None):
    """新增一档并返回它（同名不覆盖，直接追加 —— 允许"千问·主力/千问·备用"并存）。"""
    data = load(path)
    item = {
        "id": _new_id(name),
        "name": str(name or "").strip() or str(model or "").strip(),
        "base_url": str(base_url or "").strip(),
        "model": str(model or "").strip(),
        "api_key": str(api_key or "").strip(),
        "note": str(note or "").strip(),
    }
    data["profiles"].append(item)
    save(data, path)
    return item


def remove(id_or_index, path=None):
    """按 id（或 1 起的序号）删一档。删掉当前档时 active 归零。返回被删的档或 None。"""
    data = load(path)
    hit = find(data, id_or_index)
    if hit is None:
        return None
    data["profiles"] = [x for x in data["profiles"] if x["id"] != hit["id"]]
    if data.get("active") == hit["id"]:
        data["active"] = ""
    save(data, path)
    return hit


def find(data, key):
    """按 1 起的序号、id、或名字（允许唯一前缀匹配）找一档；找不到返回 None。"""
    items = list((data or {}).get("profiles") or [])
    s = str(key or "").strip()
    if not s:
        return None
    if s.isdigit():
        i = int(s) - 1
        return items[i] if 0 <= i < len(items) else None
    for it in items:
        if it.get("id") == s:
            return it
    for it in items:                       # 名字精确
        if str(it.get("name") or "").strip() == s:
            return it
    hits = [it for it in items if str(it.get("name") or "").startswith(s)]
    return hits[0] if len(hits) == 1 else None   # 前缀唯一才认，避免误切


def use(key, path=None):
    """把某一档设为**当前档**（不改 key，只改 active）。返回该档或 None。"""
    data = load(path)
    hit = find(data, key)
    if hit is None:
        return None
    data["active"] = hit["id"]
    save(data, path)
    return hit


def active_profile(data=None):
    """当前生效的那一档（dict）或 None。"""
    d = data if data is not None else load()
    aid = str(d.get("active") or "")
    if not aid:
        return None
    for it in d.get("profiles") or []:
        if it.get("id") == aid:
            return it
    return None


# ---------------- 与 .env 的衔接 ----------------

def static_fallback() -> dict:
    """没有启用任何档位时，实际生效的三个值（真实环境变量或 `backend/.env`）。

    **刻意不自动建档**：档位序号是用户要输入的东西，不能让"启动即建档"把它挤后一位
    （实测踩过：自动收录的 .env 档占了 [1]，用户自己加的第一档就变成 [2]）。
    所以旧用户升级后行为与升级前完全一致，想随时切换时再自己 `/llm-add` 建档。
    """
    try:
        from . import config

        return {
            "api_key": config.LLM_API_KEY,
            "base_url": config.LLM_BASE_URL,
            "model": config.LLM_MODEL,
        }
    except Exception:
        return {"api_key": "", "base_url": "", "model": ""}


def describe(item) -> str:
    """一档的一行人话（打码 key）。"""
    if not item:
        return "（未配置）"
    return "%s · %s · key %s" % (item.get("name") or "（无名）",
                                 item.get("model") or "（未填模型）",
                                 mask_key(item.get("api_key")))


def snapshot(path=None) -> dict:
    """给终端/接口用的只读快照：档位列表（key 打码）+ 当前档 + 是否启用。"""
    data = load(path)
    items = []
    for i, it in enumerate(data["profiles"], 1):
        items.append({
            "index": i,
            "id": it["id"],
            "name": it["name"],
            "base_url": it["base_url"],
            "model": it["model"],
            "host": host_of(it["base_url"]),
            "key_masked": mask_key(it["api_key"]),
            "has_key": bool(it.get("api_key")),
            "note": it.get("note") or "",
            "active": it["id"] == data.get("active"),
        })
    cur = active_profile(data)
    fallback = static_fallback()
    return {
        "active_id": data.get("active") or "",
        "active": ({k: v for k, v in cur.items() if k != "api_key"} if cur else None),
        "active_key_masked": mask_key(cur.get("api_key")) if cur else "",
        "profiles": items,
        # 一份档位都没有时，终端也要能说清"现在到底在用哪家的哪个模型"
        "fallback": {
            "host": host_of(fallback.get("base_url")),
            "base_url": fallback.get("base_url") or "",
            "model": fallback.get("model") or "",
            "key_masked": mask_key(fallback.get("api_key")),
            "has_key": bool(fallback.get("api_key")),
        },
        "path": str(path or profiles_path()),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
