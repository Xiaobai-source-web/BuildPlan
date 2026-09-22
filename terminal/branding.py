"""终端侧品牌标识 —— 复用后端单一真源，取不到时用内置副本兜底。

真源在 backend/pipeline/branding.py；终端是独立进程，这里做一层薄封装。
终端因此仍保持"零第三方依赖"（只多了一个跨目录 import，失败也不影响运行）。
"""

import os
import sys

# 开场白署名：规格《终端界面改造规格》§B4 指定为「华南理工大学 · 建智领航」，
# 与 一键测试.py / README / 建策BuildPlan_运行指南.md 一致。
# 注意：backend/pipeline/branding.py 的 TEAM 写作「智建领航」（两字颠倒），
# 交付物 Word/看板取的是后端那份 —— 终端这里按规格走。真源若要统一，
# 改 backend/pipeline/branding.py 的 TEAM 即可（本文件会自动跟随）。
TEAM_SPEC = "建智领航"

_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")

try:
    if _BACKEND not in sys.path:
        sys.path.insert(0, _BACKEND)
    from pipeline.branding import (  # noqa: F401
        FOOTER, META, PRODUCT, SCHOOL, SIGN, SLOGAN, SUBTITLE, TEAM,
        TAGLINE_EN, VERSION, banner_lines,
    )
except Exception:                                    # pragma: no cover
    PRODUCT = "建策 BuildPlan"
    TEAM = "智建领航"
    SCHOOL = "华南理工大学"
    SLOGAN = "算得清 · 改得动 · 审得了"
    SUBTITLE = "定额为据，算法为尺，自然语言为笔"
    VERSION = "2.1"
    SIGN = f"{SCHOOL} · {TEAM}"
    FOOTER = f"{PRODUCT} v{VERSION} · {SIGN}"
    META = f"{FOOTER} · {SLOGAN}"
    TAGLINE_EN = "BuildPlan — Compute. Revise. Audit."

    def banner_lines():
        return [f"{PRODUCT}  v{VERSION}", SLOGAN, SUBTITLE, SIGN]


def welcome_lines():
    """开场白品牌块 —— **只有一行**。

    用户明确要求（原话）：
      「这个开始页面不需要这么多内容，把学校，小组，口号，只留"海之子·建策BuildPlan"，
        简洁优先。」
    所以这里**故意不返回** SLOGAN / SUBTITLE / TAGLINE_EN / SCHOOL / TEAM ——
    这些仍然存在于交付物（Word / 看板）的品牌标识里（后端 `pipeline/branding.py` 是真源），
    只是**终端开场页不再堆**。

    别再把标语加回来：`test_开场白只留一行品牌` 守着这件事。
    """
    product = PRODUCT if isinstance(PRODUCT, str) and PRODUCT else "建策 BuildPlan"
    return ["海之子 · %s" % product]
