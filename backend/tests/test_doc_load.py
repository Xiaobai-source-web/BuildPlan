"""DocLoadNode + .docx 读取测试。

覆盖：
  ① MCP read_file 对 .docx（zip 内 word/document.xml）能抽取纯文本
  ② DocLoadNode 从 prompt 识别到文件 → 写入 ctx["doc_content"] / doc_files
  ③ DocLoadNode 无文件 → 发 param_review(purpose=doc) 人工门，resolve Y → 无文件继续

运行：python -m pytest backend/tests/test_doc_load.py -v
"""

import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.mcp import MCPFileTools
from pipeline.mcp_file_reader import _DEFAULT_ROOT
from pipeline.nodes.doc_load import DocLoadNode
from pipeline.registry import InteractionRegistry

# 测试基准文件（backend/ 在 MCP_FILE_ROOT=代码包 内）
_CFG = "backend/config.py"

# ⚠️ 临时 docx 一律落在**本进程独有**的目录下（第 42 轮，xdist 并行隔离）：
#   这些文件原来直接用固定名字写进 `backend/tests/`（真实源码树）——
#   两个 xdist worker 会互相覆盖、互相 unlink（`tmp.exists()` 判断后对方已删），
#   用例中途崩溃还会把垃圾留在仓库里。目录仍在 MCP 沙盒根（代码包）之内。
ISO_TMP = Path(BACKEND) / "_test_tmp" / ("doc_load_p%d" % os.getpid())

DOCX_BODY = "谭村城中村改造项目，总建筑面积12.8万㎡，混凝土约5.2万m³。"


def _make_docx(path: Path) -> None:
    """在指定绝对路径造一个最小 .docx（zip 内含 word/document.xml）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>谭村城中村改造项目</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>总建筑面积12.8万㎡，混凝土约5.2万m³。</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml.encode("utf-8"))


def test_read_docx_extracts_text():
    """MCP read_file 对 .docx 抽纯文本（含中文、数字、单位）。"""
    tmp = ISO_TMP / "_tmp_test.docx"
    try:
        _make_docx(tmp)
        m = MCPFileTools(mode="inprocess")
        try:
            text, err = m.call("read_file", {"path": str(tmp)})
        finally:
            m.close()
        assert err is False, text
        assert "谭村城中城改" in text.replace("造", "") or "谭村" in text
        assert "12.8万㎡" in text and "5.2万m³" in text
    finally:
        if tmp.exists():
            tmp.unlink()


def test_read_docx_zip_text_body_only():
    """docx 抽取结果应只含正文文本，不含 XML 标签。"""
    tmp = ISO_TMP / "_tmp_test2.docx"
    try:
        _make_docx(tmp)
        m = MCPFileTools(mode="inprocess")
        try:
            text, err = m.call("read_file", {"path": str(tmp)})
        finally:
            m.close()
        assert err is False
        assert "<w:" not in text and "</w:" not in text
        assert "谭村城中村改造项目" in text
    finally:
        if tmp.exists():
            tmp.unlink()


def test_doc_load_node_reads_file_from_prompt():
    """prompt 含文件路径 → DocLoadNode 写入 doc_content / doc_files。"""
    tmp = ISO_TMP / "_tmp_node.docx"
    try:
        _make_docx(tmp)
        node = DocLoadNode()
        node._emit = lambda ev, d: None
        node._registry = InteractionRegistry()
        node._run_id = "rt"
        node._cancel_evt = threading.Event()
        ctx = {"prompt": f"请根据项目文档 {tmp} 编制施工进度计划"}
        out = node.run(ctx)
        assert (out or {}).get("doc_files") == [str(tmp)]
        assert ctx.get("doc_content") and "谭村" in ctx["doc_content"]
        assert ctx.get("doc_files") == [str(tmp)]
    finally:
        if tmp.exists():
            tmp.unlink()


def _run_no_file(node, ctx, reg, review_box, done):
    """后台执行 DocLoadNode.run（会阻塞在人工门），并把发出的 review 事件填入 review_box。"""
    try:
        result = node.run(ctx)
        done["result"] = result
    except Exception as e:  # noqa: BLE001
        done["error"] = e
    finally:
        done["ev"].set()


def test_doc_load_node_out_of_sandbox_fallback():
    """文件在 MCP 沙盒根之外（越权）→ 直接读兜底，仍能加载，不触发人工门。"""
    tmpdir = tempfile.gettempdir()                       # 系统临时目录，必然在 _DEFAULT_ROOT 之外
    assert Path(_DEFAULT_ROOT) not in Path(tmpdir).parents and tmpdir != _DEFAULT_ROOT
    tmp = Path(tmpdir) / "_hzz_out_of_root.docx"
    try:
        _make_docx(tmp)
        # 确认 MCP 沙盒确实拒绝它（走直线 DLook 前先验证越权在上游成立）
        m = MCPFileTools(mode="inprocess")
        try:
            text, err = m.call("read_file", {"path": str(tmp)})
            assert err is True and "越权" in text
        finally:
            m.close()

        node = DocLoadNode()
        node._emit = lambda ev, d: None
        node._registry = InteractionRegistry()
        node._run_id = "osr"
        node._cancel_evt = threading.Event()
        ctx = {"prompt": f"请根据项目文档 {tmp} 编制计划"}
        out = node.run(ctx)
        assert (out or {}).get("doc_files") == [str(tmp)]
        assert "谭村城中村改造项目" in ctx.get("doc_content", "")
        assert node.done_summary.startswith("已加载项目文件 1 个")
    finally:
        if tmp.exists():
            tmp.unlink()


def test_doc_load_node_no_file_asks_and_accepts_y():
    """无文件 → 发 param_review(purpose=doc)；resolve passed=True → 无文件继续。"""
    node = DocLoadNode()
    reg = InteractionRegistry()
    node._registry = reg
    node._run_id = "nd"
    node._cancel_evt = threading.Event()
    events = []
    node._emit = lambda ev, d: (events.append((ev, d)) if ev == "param_review" else None)

    ctx = {"prompt": "一个住宅项目，请编制施工计划。"}
    done = {"ev": threading.Event()}
    t = threading.Thread(target=_run_no_file, args=(node, ctx, reg, events, done), daemon=True)
    t.start()

    # 等待 param_review 事件出现，捞出 review_id 后 resolve Y
    review_id = None
    for _ in range(100):
        if events:
            _, d = events[0]
            review_id = d.get("review_id")
            break
        done["ev"].wait(0.05)
    assert review_id, "未发出 param_review 事件"
    assert events[0][1].get("purpose") == "doc"

    reg.resolve(review_id, {"passed": True})
    t.join(timeout=5)

    result = done.get("result")
    assert result is not None
    assert result.get("doc_content") is None
    assert result.get("doc_files") == []


def test_路径粘在中文句子里也要识别出来():
    """实测 bug：'请按这个资料做计划：<路径>'（全角冒号紧贴路径）原来会把**整句**当路径。

    `_PATH_RE` 的第二个分支允许任意非空白字符，而全角冒号不在排除集里，于是从"请"
    一路吃到 ".docx"。后果是**静默失败**：按一个不存在的路径去读 → doc_content 为空
    → 计划照做（用配置默认值），而用户以为自己已经给了文件。

    关键：不能靠"收紧字符类"来修 —— 路径里合法地含中文、空格与括号
    （仓库里 信息输入文件夹/ 下的示例文件名就是中文 + 括号的），收紧就会把它截断。
    """
    from pipeline.nodes.extractor import detect_local_files

    tmp = ISO_TMP / "_tmp_glued.docx"
    try:
        _make_docx(tmp)
        prompts = [
            f"请按这个资料做计划：{tmp}",          # 全角冒号紧贴（本次 bug）
            f"请根据项目文档 {tmp} 编制计划",        # 空格分隔（原有行为，不能被改坏）
            f"请读取「{tmp}」并编制计划",            # 中文引号包裹
            f"资料在{tmp}",                        # 中文紧贴、无分隔符
            # 用户实测的写法（截图原话）：ASCII 双引号包裹 + 后面紧跟**全角逗号**，
            # 且前缀是"读取这个文件"而不是"做计划"。这条必须一直能用。
            f'读取这个文件"{tmp}"，并依据它生成进度计划',
        ]
        for prompt in prompts:
            got = detect_local_files(prompt)
            assert got == [str(tmp)], "未正确识别路径：%r → %r" % (prompt, got)

        # 端到端：粘在句子里也必须真的读进来（不只是识别对）
        node = DocLoadNode()
        node._emit = lambda ev, d: None
        node._registry = InteractionRegistry()
        node._run_id = "glued"
        node._cancel_evt = threading.Event()
        ctx = {"prompt": f"请按这个资料做计划：{tmp}"}
        out = node.run(ctx)
        assert (out or {}).get("doc_files") == [str(tmp)], out
        assert "谭村" in (ctx.get("doc_content") or ""), "文件没被真正读进来"
    finally:
        if tmp.exists():
            tmp.unlink()


def test_识别到路径但读不到时不再说成没检测到():
    """失败要可区分：把"识别到了但读不到"和"没检测到文件"分开说，并回显路径。"""
    node = DocLoadNode()
    reg = InteractionRegistry()
    node._registry = reg
    node._run_id = "unreadable"
    node._cancel_evt = threading.Event()
    events = []
    node._emit = lambda ev, d: (events.append((ev, d)) if ev == "param_review" else None)

    ghost = str(Path(BACKEND) / "tests" / "_不存在的资料.docx")
    ctx = {"prompt": "请按这个资料做计划：%s" % ghost}
    done = {"ev": threading.Event()}
    t = threading.Thread(target=_run_no_file, args=(node, ctx, reg, events, done), daemon=True)
    t.start()

    review_id = None
    for _ in range(100):
        if events:
            review_id = events[0][1].get("review_id")
            break
        done["ev"].wait(0.05)
    assert review_id, "未发出 param_review 事件"
    payload = events[0][1]
    assert payload.get("params", {}).get("unreadable"), payload
    # 老断言是 `ghost in message`：路径直接拼在提示语里。
    # 现在路径移到结构化字段（params.unreadable / reason），由终端渲染——
    # 所以改成**更强的用户视角断言**：终端真正打出来的文字里必须看得见这条路径，
    # 且必须说清是"读不到"而不是"没检测到"。
    assert payload.get("params", {}).get("reason") == "unreadable", payload
    assert "未检测到" not in payload.get("message", ""), payload.get("message")

    import re
    import sys as _sys
    _sys.path.insert(0, str(Path(BACKEND).parent / "terminal"))
    import renderer as _renderer                                   # noqa: E402
    printed = re.sub(r"\033\[[0-9;]*m", "", _renderer.render_param_review(payload))
    assert ghost in printed, "终端没把读不到的路径显示出来：\n%s" % printed
    assert "读不到" in printed, printed

    reg.resolve(review_id, {"passed": True})
    t.join(timeout=5)