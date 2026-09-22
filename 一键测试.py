#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建策 BuildPlan —— 一键测试启动器（非技术人员用）

算得清 · 改得动 · 审得了 ｜ 定额为据，算法为尺，自然语言为笔
华南理工大学 · 建智领航

用法：双击同目录的 `一键测试.bat`（或命令行 `python 一键测试.py`）。

职责：
  1. 检查 Python 与依赖（缺失自动 pip install）
  2. 必须有 AI 接口密钥（QWEN_API_KEY）：缺失则引导粘贴；不提供则中止
     （本测试不做无 key 的静默演示）
  3. 重启后端（uvicorn，端口 8000）：先杀掉占用 8000 的旧进程强制加载最新代码，
     再全新启动等待 /healthz 就绪
  4. 启动终端（terminal/console.py --real）
  5. 终端退出后关闭后端，防进程残留占口

纯标准库，零第三方依赖（pip 安装的是后端所需依赖）。
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "backend")
TERMINAL = os.path.join(HERE, "terminal")
ENV_FILE = os.path.join(BACKEND, ".env")
LAUNCH_LOG = os.path.join(BACKEND, "_launch.log")

PORT = 8000
BASE_URL = f"http://localhost:{PORT}"
KEY_PLACEHOLDER = "sk-xxxx"          # .env.example 里的占位符
REQUIRED_MODULES = ["fastapi", "uvicorn", "pydantic", "httpx", "docx"]   # docx = python-docx
MIN_PYTHON = (3, 8)                  # 代码实测最低可用版本

# 密钥环境变量：**通用名优先**，QWEN_* / DASHSCOPE_* 只作向后兼容别名。
# 接口是标准 OpenAI 兼容协议，所以变量名与厂商无关。
KEY_ENV_NAMES = ("LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY")

# 厂商预设：(编号, 名字, base_url, 默认模型, 取 key 的页面)
# 只有预设，不做任何校验 —— 选错端点会在调用时报 401，但那是用户自己可发现的错。
PROVIDERS = (
    ("1", "通义千问 / 阿里百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen-plus", "https://bailian.console.aliyun.com/  → 右上角 API-KEY 管理"),
    ("2", "DeepSeek", "https://api.deepseek.com/v1",
     "deepseek-chat", "https://platform.deepseek.com/  → API keys"),
    ("3", "月之暗面 Kimi", "https://api.moonshot.cn/v1",
     "moonshot-v1-8k", "https://platform.moonshot.cn/  → API Key 管理"),
    ("4", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
     "glm-4-flash", "https://open.bigmodel.cn/  → API Keys"),
    ("5", "硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1",
     "Qwen/Qwen2.5-7B-Instruct", "https://cloud.siliconflow.cn/  → API 密钥"),
    ("6", "OpenAI", "https://api.openai.com/v1",
     "gpt-4o-mini", "https://platform.openai.com/api-keys"),
    ("7", "本地模型（Ollama / vLLM / LM Studio）", "http://localhost:11434/v1",
     "qwen2.5:7b", "本地起好服务即可；Ollama 默认 http://localhost:11434/v1"),
)
CUSTOM_CHOICE = "8"
SKIP_CHOICE = "0"
DEFAULT_CHOICE = "1"
BRAND_LINES = (
    "  建策 BuildPlan · 算得清 · 改得动 · 审得了",
    "  定额为据，算法为尺，自然语言为笔",
    "  华南理工大学 · 智建领航",
)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PY = sys.executable or "python"


def _hr(title=""):
    print("=" * 62)
    if title:
        print(f"  {title}")
        print("=" * 62)


# ---------------- 1. 依赖 ----------------
def ensure_deps():
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True
    print(f"[依赖] 缺少 {', '.join(missing)}，正在安装（首次需联网，约 1-3 分钟）...")
    req = os.path.join(BACKEND, "requirements.txt")
    r = subprocess.run([PY, "-m", "pip", "install", "-r", req])
    if r.returncode != 0:
        print("\n[错误] 依赖安装失败。请检查网络后重试，或手动执行：")
        print(f'   "{PY}" -m pip install -r "{req}"')
        return False
    return True


# ---------------- 2. 密钥 ----------------
def _read_key():
    """返回 (key, 来源)；环境变量优先，其次 backend/.env。

    密钥变量名按 `KEY_ENV_NAMES` 顺序找（通用名 `LLM_API_KEY` 优先，
    `QWEN_API_KEY` / `DASHSCOPE_API_KEY` 只作向后兼容别名）。
    """
    for name in KEY_ENV_NAMES:
        k = os.environ.get(name)
        if k and not k.startswith(KEY_PLACEHOLDER):
            return k, "环境变量 " + name
    if os.path.exists(ENV_FILE):
        try:
            for line in open(ENV_FILE, encoding="utf-8", errors="replace"):
                line = line.strip()
                for name in KEY_ENV_NAMES:
                    if line.startswith(name + "="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v and not v.startswith(KEY_PLACEHOLDER):
                            return v, ".env 的 " + name
        except OSError:
            pass
    return "", ""


def _read_env_value(name):
    """从 backend/.env 读一个值（启动时回显端点/模型用）。"""
    if not os.path.exists(ENV_FILE):
        return ""
    try:
        for line in open(ENV_FILE, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _write_env(pairs):
    """把若干 `键=值` 写进 backend/.env（保留其它行、覆盖同名行）。

    迁移：写通用名 `LLM_API_KEY` 时，把旧的 `QWEN_API_KEY=` / `DASHSCOPE_API_KEY=`
    行一并删掉 —— 否则下次会读到一个已经作废的旧 key（它在读取顺序里排后面），
    造成"明明改了却没生效"的困惑。
    """
    lines = []
    if os.path.exists(ENV_FILE):
        try:
            lines = open(ENV_FILE, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            lines = []
    drop = set(pairs)
    if "LLM_API_KEY" in drop:
        drop |= {"QWEN_API_KEY", "DASHSCOPE_API_KEY"}
    out = [ln for ln in lines if ln.split("=", 1)[0].strip() not in drop]
    for k, v in pairs.items():
        out.append(f"{k}={v}")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


def _print_provider_menu():
    print()
    _hr("首次使用：配置 AI 接口密钥")
    print("  接口是标准 OpenAI 兼容协议 —— **任何厂商的 key 都能用**，选一家即可。")
    print("  密钥只写进本机 backend\\.env，不上传。")
    print()
    for num, name, url, model, where in PROVIDERS:
        print(f"   [{num}] {name:<28} 默认模型 {model}")
    print(f"   [{CUSTOM_CHOICE}] 自定义端点（自己填 base_url 与模型名）")
    print(f"   [{SKIP_CHOICE}] 不填密钥 —— 用确定性兜底跑")
    print("        （**照样出完整计划/Word/看板**，只是少了“读懂语言、拆解工序”的模型能力）")
    print()


def ensure_key():
    """确保有可用配置；**允许跳过密钥**走确定性兜底。

    返回 True = 可以继续（有 key，或用户选择跳过）。

    为什么允许跳过：README 与运行指南都写着“没有密钥也能端到端出结果”，后端确实支持
    （全仓没有一处调用 `config.require_api_key()`）。而老启动器是“不给 key 就退出”——
    没有百炼 key 的评委连门都进不去，这是实打实的风险点。
    """
    key, src = _read_key()
    if key:
        print(f"[密钥] 已配置（来自{src}）")
        base = os.environ.get("LLM_BASE_URL") or _read_env_value("LLM_BASE_URL")
        model = os.environ.get("LLM_MODEL") or _read_env_value("LLM_MODEL")
        if base or model:
            print(f"[模型] 端点 {base or '（默认：通义千问）'} · "
                  f"模型 {model or '（默认：qwen-plus）'}")
        return True

    _print_provider_menu()
    try:
        choice = input(f"请选择（回车＝[{DEFAULT_CHOICE}] 通义千问）：").strip() or DEFAULT_CHOICE
    except (EOFError, KeyboardInterrupt):
        choice = DEFAULT_CHOICE

    if choice == SKIP_CHOICE:
        print()
        print("[密钥] 已选择**不使用模型**：本次运行全部走确定性兜底规则。")
        print("       计划、两版工期、定额锚定、Word 与看板照常产出。")
        _write_env({"LLM_API_KEY": ""})       # 明确置空，不让旧值阴魂不散
        return True

    if choice == CUSTOM_CHOICE:
        print()
        try:
            base = input("base_url（含 /v1，例如 https://api.example.com/v1）：").strip()
            model = input("模型名（例如 my-model）：").strip()
        except (EOFError, KeyboardInterrupt):
            base = model = ""
        if not base or not model:
            print("\n[中止] 自定义端点需要 base_url 与模型名都填写。")
            return False
    else:
        preset = next((p for p in PROVIDERS if p[0] == choice), None)
        if preset is None:
            print(f"\n[中止] 无法识别的选项「{choice}」。")
            return False
        base, model = preset[2], preset[3]
        print()
        print(f"  已选：{preset[1]}")
        print(f"  端点：{base}")
        print(f"  模型：{model}")
        print(f"  取 key：{preset[4]}")

    try:
        val = input("\n请粘贴密钥（直接回车取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    if not val or val.startswith(KEY_PLACEHOLDER):
        print("\n[中止] 未提供有效密钥。若不想用模型，请在菜单里选 "
              f"[{SKIP_CHOICE}] 走确定性兜底。")
        return False

    _write_env({"LLM_API_KEY": val, "LLM_BASE_URL": base, "LLM_MODEL": model})
    print("[密钥] 已保存到 backend\\.env（下次无需再填）。")
    print(f"[模型] 端点 {base} · 模型 {model}")
    return True


# ---------------- 3. 后端 ----------------
def backend_healthy(timeout=1.5):
    try:
        with urllib.request.urlopen(BASE_URL + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def kill_port(port):
    """强制结束占用指定端口的进程（Windows netstat/taskkill；其它平台尽力而为）。"""
    if os.name != "nt":
        return
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
        out = raw.decode("utf-8", errors="replace") if raw else ""
    except Exception:
        return
    pids = set()
    for ln in out.splitlines():
        # netstat 行形如： TCP 0.0.0.0:8000 0.0.0.0:0  LISTENING  1234
        if f":{port}" not in ln or "LISTENING" not in ln:
            continue
        nums = [p for p in ln.split() if p.isdigit()]
        if nums:                              # 最后一列是 PID
            pids.add(nums[-1])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        except Exception:
            pass


def start_backend():
    """重启后端：先杀掉占用 8000 的旧进程（避免复用旧代码），再全新启动。

    返回后端 subprocess。失败返回 None 且不健康。
    """
    if backend_healthy():
        print("[后端] 检测到旧进程仍在运行，正在重启（强制加载最新代码）...")
        kill_port(PORT)
        for _ in range(10):                 # 等端口释放
            if not backend_healthy():
                break
            time.sleep(0.5)
    print("[后端] 启动中...")
    log = open(LAUNCH_LOG, "w", encoding="utf-8")
    try:
        p = subprocess.Popen(
            [PY, "-m", "uvicorn", "main:app", "--port", str(PORT)],
            cwd=BACKEND, stdout=log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        print(f"[错误] 无法启动后端：{e}")
        log.close()
        return None
    for _ in range(60):                      # 最多等 ~30 秒
        if backend_healthy():
            print("[后端] 就绪")
            return p
        if p.poll() is not None:
            print("[错误] 后端启动失败（进程已退出）。日志尾部：")
            _dump_log(log)
            return None
        time.sleep(0.5)
    print("[错误] 后端启动超时。日志尾部：")
    _dump_log(log)
    try:
        p.terminate()
    except Exception:
        pass
    return None


def _dump_log(log):
    try:
        log.flush()
        with open(LAUNCH_LOG, encoding="utf-8", errors="replace") as f:
            for ln in f.read().splitlines()[-12:]:
                print("   ", ln)
    except OSError:
        pass


# ---------------- 4. 终端 ----------------
def run_terminal():
    console = os.path.join(TERMINAL, "console.py")
    try:
        subprocess.run([PY, console, "--real"], cwd=TERMINAL)
    except KeyboardInterrupt:
        pass


# ---------------- 5. 清理 ----------------
def stop_backend(p):
    if p is None:
        return
    print("\n[后端] 关闭中...")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True)
        else:
            p.terminate()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def ensure_python():
    """Python 版本闸门：低于最低版本时给出明确中文提示，而不是让用户看到语法错误。"""
    if sys.version_info >= MIN_PYTHON:
        return True
    cur = ".".join(str(x) for x in sys.version_info[:3])
    need = ".".join(str(x) for x in MIN_PYTHON)
    _hr("Python 版本过低")
    print(f"  当前版本：{cur}")
    print(f"  需要版本：{need} 及以上")
    print()
    print("  本软件的部分模块使用了较新的语法，在旧版本上无法运行。")
    print("  请安装 Python " + need + " 及以上版本后重试，或使用 Anaconda 最新版。")
    print()
    print(f"  下载地址：https://www.python.org/downloads/")
    print(f"  当前解释器：{PY}")
    return False


def main():
    os.chdir(HERE)
    _hr("建策 BuildPlan —— 施工进度计划生成")
    for line in BRAND_LINES:
        print(line)
    print()
    if not ensure_python():
        return 1
    print(f"  Python: {PY}  ({'.'.join(str(x) for x in sys.version_info[:3])})")
    print()
    if not ensure_deps():
        return 1
    if not ensure_key():
        return 1
    p = start_backend()
    if p is None and not backend_healthy():
        print("\n[中止] 后端未能启动，已退出。")
        return 1
    print()
    try:
        run_terminal()
    finally:
        stop_backend(p)
    print("\n测试结束，感谢使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
