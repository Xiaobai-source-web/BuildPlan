# -*- coding: utf-8 -*-
"""大字符串断言护栏（域 9.1 / 9.2）—— 交付物类用例统一入口。

## 为什么要有它

pytest 的断言重写在 `assert x not in <超长字符串>` **失败**时会调
`_pytest.assertion.util._notin_text` → `difflib` 求最长公共子串，复杂度约 O(n·m)。

交付物看板 HTML 内嵌 ECharts，单份 **1,052,775 字符**且第三方库整个挤在一行里。
实测（`test_delivery_capacity_caliber.py`）两条 `assert "None" not in text`
各自耗时 **567.95 s / 505.86 s，合计 1074 s = 全量套件的 97%**。

## 但真正的病根不是"断言太慢"，是**断言对象选错了**

探针 `backend/_probe_tmp/q_none_in_html.py` 实测（用真实测试计划生成看板）：

    HTML len = 1052775
    HTML 里 'None' 出现次数 = 7
    script 段总长 = 1041957, 其中 'None' = 7 处
    非 script 段总长 = 10785, 其中 'None' = 0 处
    => script 段里的是 ECharts 自己的标识符 `enableNone` / `enableNone:!n`
       对照（换一条带 capacity_source 的正常计划）：'None' 仍然 = 7 处

也就是说这 7 处**全部**来自内嵌的 ECharts 库，**用户可见区一处都没有**。
对原始 HTML 断言 `"None" not in text` 永远不可能通过 —— 它测的不是
"有没有把 Python 的 None 印给用户"，而是"ECharts 里有没有这个单词"。

所以本模块同时提供两件事：

1. `strip_invisible()` / `visible_html()` —— 剥掉 `<script>` / `<style>`，
   只留用户真正看得到的区域；
2. `assert_absent()` / `assert_present()` —— **先算布尔再断言**，
   失败时自己定位并只报上下文，绝不把超长串交给 pytest 的失败 diff。

## 用法

    from _text_guard import assert_absent, assert_present, strip_invisible

    text = strip_invisible(Path(path).read_text(encoding="utf-8"))
    assert_absent(text, "None", what="Python 的 None 字面量")
    assert_present(text, "工期不随工程量变化")

（本文件不以 `test_` 开头，pytest 不会收集它。）
"""

import re
from pathlib import Path

__all__ = [
    "strip_invisible",
    "visible_html",
    "assert_absent",
    "assert_present",
]

# 注意 `</script\s*>`：ECharts 是内联的单个巨大 <script>，中间的 `</script >`
# 之类写法也要吃掉，否则只剥掉前半截、后半截仍是 1 MB。
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.I | re.S)

# 断言失败时给多少上下文（字符）
_CTX = 100


def strip_invisible(text):
    """剥掉 `<script>` / `<style>` 段，返回用户可见区。

    非 HTML（例如 Word 抽出来的纯文本）原样返回，可安全无脑套用。
    """
    return _STYLE_RE.sub("", _SCRIPT_RE.sub("", text))


def visible_html(path_or_text):
    """读 HTML 文件（或直接收一段 HTML 文本）并只留用户可见区。"""
    p = Path(path_or_text)
    try:
        if p.is_file():
            return strip_invisible(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        pass                      # 不是路径（含 NUL / 超长）→ 当文本处理
    return strip_invisible(str(path_or_text))


def assert_absent(text, needle, what=""):
    """`needle not in text` 的安全版本。

    先算布尔，**只在真命中时**用 `str.find` 定位一次（O(n)），报出 offset 与
    上下文。绝不把 `text` 交给 pytest 的失败 diff —— 那才是 O(n·m) 的来源。
    """
    idx = text.find(needle)
    if idx < 0:
        return
    lo = max(0, idx - _CTX)
    hi = min(len(text), idx + len(needle) + _CTX)
    label = what or repr(needle)
    raise AssertionError(
        "%s 不该出现，但在 offset %d 处出现了（文本长度 %d）：\n    ...%s..."
        % (label, idx, len(text), text[lo:hi].replace("\n", "\\n"))
    )


def assert_present(text, needle, what=""):
    """`needle in text` 的安全版本（`in` 的失败 diff 本身不慢，但统一入口）。"""
    if needle in text:
        return
    label = what or repr(needle)
    raise AssertionError(
        "%s 应当出现但没找到（文本长度 %d）" % (label, len(text))
    )
