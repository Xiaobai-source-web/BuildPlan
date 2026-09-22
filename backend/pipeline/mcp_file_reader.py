#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极简 MCP 文件读取服务（纯 Python 标准库，零第三方依赖）
================================================================
在 stdin/stdout 上用 JSON-RPC 2.0（换行分隔的 JSON 行）实现 MCP 的
stdio 传输，暴露两个工具给任何 MCP 客户端（Claude Code / 自写循环 / 流水线…）：
  - read_file(path, offset?, limit?)   读取文本文件（自动防二进制、防超大文件）
  - list_dir(path?)                    列目录（让模型先摸清路径再读）

用法（作为 MCP stdio 服务器运行，由客户端拉起，无需手动启动）：
  python mcp_file_reader.py
可选环境变量：
  MCP_FILE_ROOT=<目录>   设置后只允许读该目录内的文件（默认 = 代码包目录）
自检（不带客户端时手动验证协议）：
  python mcp_file_reader.py --self-test

本文件同时可被 import（供打包/in-process 场景直接调用，避免子进程）：
  from mcp_file_reader import TOOLS, tool_result
  tool_result("read_file", {"path": "..."})   # -> {"content":[...],"isError":bool}
"""

import json
import os
import re
import sys
import zipfile

# ---------- 常量 ----------
MAX_CHARS = 120_000          # read_file 单次最多返回的字符数（防撑爆模型上下文）
MAX_FILE_BYTES = 4 * 1024 * 1024  # 超过此大小的文件只读开头一段
BINARY_PROBE = 4096          # 用开头 N 字节探测是否二进制
# 默认根目录 = 代码包目录（本文件位于 代码包/backend/pipeline/ 下，上溯 3 层）
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.environ.get("MCP_FILE_ROOT", "").strip() or _DEFAULT_ROOT

TOOLS = [
    {
        "name": "read_file",
        "description": "读取文本文件，返回 UTF-8 解码后的内容。可传 offset/limit 翻页；"
                       "二进制或超大文件会被安全处理并说明。目录请用 list_dir。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（绝对或相对）"},
                "offset": {"type": "integer", "description": "跳过开头 N 个字符（翻页）"},
                "limit": {"type": "integer", "description": "最多返回 N 个字符"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "列出目录下的条目（目录优先，按名称排序，含大小），"
                       "便于先摸清路径再 read_file。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径，默认当前目录"}},
        },
    },
]


def send(obj):
    """写一条 JSON-RPC 消息到 stdout；整行 ASCII 转义，避开 Windows 编码坑。"""
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def resolve(path):
    """绝对化路径；相对路径一律以 MCP_FILE_ROOT 为基准（跨模式/CWD 一致）；越权抛 ValueError。"""
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    ap = os.path.abspath(os.path.normpath(path))
    if ROOT:
        r = os.path.abspath(os.path.normpath(ROOT))
        a, b = os.path.normcase(ap), os.path.normcase(r)
        if not (a == b or a.startswith(b + os.sep)):
            raise ValueError(f"越权：路径不在 MCP_FILE_ROOT（{ROOT}）内")
    return ap


def _read_docx_text(ap):
    """读取 .docx：word 是 zip，取 word/document.xml 抽段落文本（零第三方依赖）。"""
    with zipfile.ZipFile(ap) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    # 单元格/表格行 → 换行，段落 → 换行
    xml = xml.replace("</w:tc>", "\t").replace("</w:tr>", "\n")
    xml = re.sub(r"</w:p>", "\n", xml)
    # 去掉剩余 XML 标签，还原常见 XML 转义
    xml = re.sub(r"<[^>]+>", "", xml)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&apos;", "'"), ("&nbsp;", " ")):
        xml = xml.replace(ent, ch)
    # 折叠 3 个以上连续空白行
    xml = re.sub(r"\n{3,}", "\n\n", xml).strip()
    return xml


def read_file(args):
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("缺少参数 path")
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or MAX_CHARS)
    if offset < 0 or limit < 0:
        raise ValueError("offset/limit 不能为负")
    limit = min(limit, MAX_CHARS)

    ap = resolve(path)
    if not os.path.exists(ap):
        raise ValueError(f"文件不存在：{ap}")
    if not os.path.isfile(ap):
        raise ValueError(f"{ap} 是目录不是文件，请用 list_dir")

    size = os.path.getsize(ap)

    # .docx → 解包抽文本
    if ap.lower().endswith(".docx"):
        text = _read_docx_text(ap)
        lines = text.count("\n") + 1
        piece = text[offset:offset + limit]
        head = (f"path: {ap}\n"
                f"size: {size} 字节（.docx，约 {lines} 行）\n"
                f"range: 字符 {offset}..{offset + len(piece)}\n")
        return head + "--- content ---\n" + piece + "\n--- end ---"

    with open(ap, "rb") as f:
        raw = f.read(MAX_FILE_BYTES)
    if b"\x00" in raw[:BINARY_PROBE]:
        raise ValueError(f"疑似二进制文件，拒绝读取：{ap}")

    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n")
    if text and not text.endswith("\n"):
        lines += 1
    if offset >= len(text):
        raise ValueError(f"offset({offset}) 超出文件长度({len(text)} 字符)")
    piece = text[offset:offset + limit]

    head = (f"path: {ap}\n"
            f"size: {size} 字节，约 {lines} 行\n")
    if size > MAX_FILE_BYTES:
        head += f"注意：文件超过 {MAX_FILE_BYTES // 1024}KB，只读了开头部分\n"
    head += f"range: 字符 {offset}..{offset + len(piece)}\n"
    return head + "--- content ---\n" + piece + "\n--- end ---"


def list_dir(args):
    path = args.get("path") or "."
    ap = resolve(path)
    if not os.path.isdir(ap):
        raise ValueError(f"不是目录：{ap}")
    entries = sorted(os.scandir(ap), key=lambda e: (e.is_dir(), e.name.lower()))
    out = [ap.rstrip("\\/") + "/"]
    for e in entries:
        try:
            is_dir = e.is_dir()
            size = "" if is_dir else f"{e.stat().st_size} B"
        except OSError:
            is_dir, size = False, "?"
        out.append(f"  {'[DIR] ' if is_dir else '      '}{e.name}  {size}")
    out.append(f"{len(entries)} 个条目")
    return "\n".join(out)


def tool_result(name, args):
    """执行工具；业务错误用 isError=True 返回（模型能读到错误原因并自行调整）。"""
    try:
        if name == "read_file":
            text = read_file(args)
        elif name == "list_dir":
            text = list_dir(args)
        else:
            raise ValueError(f"未知工具：{name}")
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"错误：{e}"}], "isError": True}


def handle(msg):
    if not isinstance(msg, dict) or "method" not in msg:
        return  # 客户端发来的响应，忽略
    method = msg["method"]
    mid = msg.get("id")
    is_notif = "id" not in msg  # 通知没有 id，规范要求不回
    params = msg.get("params") or {}

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "file-reader-mcp", "version": "0.1.0"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": mid,
              "result": tool_result(params.get("name"), params.get("arguments") or {})})
    elif method == "resources/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"resources": []}})
    elif method == "prompts/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}})
    elif not is_notif:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    if "--self-test" in sys.argv:
        return _self_test()
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, list):  # JSON-RPC 批量（规范 2025-08-27+ 支持）
            for m in msg:
                handle(m)
        else:
            handle(msg)


def _self_test():
    """不带客户端时手动验证协议与工具（复用 test_mcp.py 的核心断言）。"""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    p = subprocess.Popen([sys.executable, __file__], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, cwd=here)

    def req(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        p.stdin.flush()
        return json.loads(p.stdout.readline().decode("utf-8"))

    r = req({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {}}})
    assert r["result"]["capabilities"]["tools"]
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()
    r = req({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r["result"]["tools"]]
    assert names == ["read_file", "list_dir"], names
    r = req({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "read_file",
                        "arguments": {"path": os.path.basename(__file__), "limit": 100}}})
    assert r["result"]["isError"] is False
    r = req({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {"path": "不存在xyz.txt"}}})
    assert r["result"]["isError"] is True
    p.stdin.close()
    p.wait(timeout=5)
    print("MCP file-reader self-test OK ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
