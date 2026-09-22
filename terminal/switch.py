"""双轨降级：云端主后端 / 本地 llama.cpp — T-07

/switch 在 cloud 与 local 之间切换。

注意（README 亦注明）：本地后端必须实现与云端后端**相同的契约**——
POST /chat（SSE）、/confirm、/resume、/cancel，否则切换无意义。
"""

BACKENDS = {
    # 键 → (标签, base_url)
    "cloud": {"label": "云端主后端 (localhost:8000)", "url": "http://localhost:8000"},
    "local": {"label": "本地 llama.cpp (127.0.0.1:8080)", "url": "http://127.0.0.1:8080"},
}
# 切换顺序：cloud <-> local
_ORDER = ["cloud", "local"]


def describe(key):
    b = BACKENDS.get(key, BACKENDS["cloud"])
    return f"{b['label']}"


def toggle(current_key):
    """cloud <-> local 切换，返回新键。"""
    idx = _ORDER.index(current_key)
    return _ORDER[(idx + 1) % len(_ORDER)]


def url_of(key):
    b = BACKENDS.get(key, BACKENDS["cloud"])
    return b["url"]
