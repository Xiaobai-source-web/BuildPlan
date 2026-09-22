"""品牌标识 —— 单一真源。

改这一个文件，终端 / 启动器 / Word / 看板 / 索引页 全部同步生效。
（终端侧通过 terminal/branding.py 薄封装复用本模块；取不到时用内置副本兜底。）

产品定位：
    算得清（定额为据、可溯源） · 改得动（自然语言修改） · 审得了（未审计 + 数据溯源）
"""

PRODUCT = "建策 BuildPlan"
PRODUCT_EN = "BuildPlan"
TEAM = "智建领航"
SCHOOL = "华南理工大学"
SLOGAN = "算得清 · 改得动 · 审得了"
SUBTITLE = "定额为据，算法为尺，自然语言为笔"
TAGLINE_EN = "BuildPlan — Compute. Revise. Audit."
VERSION = "2.1"

# 组合串（各处直接用，避免拼错）
SIGN = f"{SCHOOL} · {TEAM}"                       # 华南理工大学 · 智建领航
TITLE = f"{PRODUCT} —— 施工进度计划生成系统"
FOOTER = f"{PRODUCT} v{VERSION} · {SIGN}"
META = f"{PRODUCT} v{VERSION} · {SIGN} · 算得清 · 改得动 · 审得了"


def banner_lines():
    """终端欢迎界面的若干行文字（不含边框与颜色）。"""
    return [
        f"{PRODUCT}  v{VERSION}",
        SLOGAN,
        SUBTITLE,
        SIGN,
    ]


def footer_text(date_str=None):
    """交付物页脚：带日期的署名。"""
    tail = f" · {date_str}" if date_str else ""
    return f"{FOOTER}{tail}"


def html_brand_head(extra=""):
    """HTML 交付物页头（自包含，无外链）。

    extra：可选的附加行（如项目名）；副标语始终保留。
    """
    extra_html = f"<div class='brand-sub'>{extra}</div>" if extra else ""
    return (
        "<div class='brand-head'>"
        f"<div class='brand-name'>{PRODUCT}</div>"
        f"<div class='brand-slogan'>{SLOGAN}</div>"
        f"<div class='brand-sub'>{SUBTITLE}</div>"
        f"{extra_html}<div class='brand-sign'>{SIGN}</div>"
        "</div>"
    )


def html_brand_foot(date_str=None):
    """HTML 交付物页脚。"""
    return f"<div class='brand-foot'>{footer_text(date_str)}</div>"


BRAND_CSS = """
.brand-head{border-left:5px solid #4a90d9;padding:6px 0 6px 12px;margin:0 0 14px}
.brand-name{font-size:19px;font-weight:700;color:#1f3a5f;letter-spacing:.5px}
.brand-slogan{font-size:13px;color:#4a90d9;font-weight:600;margin-top:2px}
.brand-sub{font-size:12px;color:#6b7688;margin-top:2px}
.brand-sign{font-size:11px;color:#9aa4b5;margin-top:3px}
.brand-foot{margin-top:22px;padding-top:10px;border-top:1px solid #dfe6ef;
             font-size:11px;color:#9aa4b5;text-align:center}
"""
