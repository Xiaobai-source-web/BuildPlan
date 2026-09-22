# -*- coding: utf-8 -*-
"""图 2 的 **SVG 版**：模仿原文档那张架构图的风格（用户拍板），横向铺开。

用户意见（原话）：
  · 「字体还是怪怪的，模仿这个版本来做吧」——指原文档里的那张架构图
  · 「并且横向扩展一点，毕竟 word 是横着看的」

**实测原图的字体规律**（`_probe_tmp/o_sub.png` 1:1 裁自原图 2480×1910）：
  · 「0.1 工作模式确认★」与「强制 Y/n · 回车=取消」**字号相同**（2480px 下各约 26px
    → 1240 视图里 13px）；
  · 层级**只靠"加粗 + 深蓝"对比"常规 + 灰"**，不靠字号；
  · ★ 直接接在标题文字后面，不单独占位；
  · 盒子：白底、细深蓝描边、小圆角。
  → 我之前的毛病是**字号档位太多**（36/18/17/15/14/13 六档），所以看起来"怪"。
    这一版只保留**两档**：标题类加粗深蓝、说明类常规灰，字号相同。

**横向铺开**：7 列 × 4 行（7/7/6/6），viewBox 宽高比从 0.90 提到约 1.5，
在 Word 里横着看更顺；同时恢复原图右侧的竖排 SSE 虚线通道。

产出：
  docs/图2_系统架构图_SVG版.svg
  docs/图2_系统架构图_SVG版.png     （Chrome headless 渲染，宽 2480）

用法：
  python tools/draw_diagrams_svg.py            # 出 svg + png
  python tools/draw_diagrams_svg.py --svg-only
"""

import argparse
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT_SVG = ROOT / "docs" / "图2_系统架构图_SVG版.svg"
OUT_PNG = ROOT / "docs" / "图2_系统架构图_SVG版.png"

W = 1240
M_L = 16                 # 左边距
M_R = 34                 # 右边距（留给右侧竖排 SSE 通道）
CONTENT = W - M_L - M_R  # 1190

# ── 只有两档字号（照原图：标题类与说明类同号，只差字重与颜色）──
FS_TITLE = 15            # 图题        ≈5.2pt
FS_LAYER = 14            # 层标题      ≈4.9pt
FS_NODE = 13             # 节点标题    ≈4.5pt
FS_DESC = 13             # 节点说明    ≈4.5pt
FS_CHIP = 13             # 带内框标题  ≈4.5pt
FS_CHIPD = 12            # 带内说明    ≈4.2pt
FS_SMALL = 12            # 图例/注释   ≈4.2pt

C_MAIN = "#0F4761"
C_OK = "#2E7D5B"
C_AMBER = "#B07D2F"
C_RED = "#B03A2E"

# ── 工作流层几何：7 列 × 4 行，横向铺开 ──
COLS = 7
GAP_X = 12
BOX_W = (CONTENT - (COLS - 1) * GAP_X) // COLS          # = 159
BOX_H = 48
GAP_Y = 24
ROWS = [[1, 2, 3, 4, 5, 6, 7], [8, 9, 10, 11, 12, 13, 14],
        [15, 16, 17, 18, 19, 20, 21], [22, 23, 24, 25, 26, 27]]

# (编号, 简称, 说明, 类型, 门标记)  门标记: "" / "★" / "R1" / "R2" / "R3"
# ⚠️ 编号与顺序的**唯一真源**是 `backend/pipeline/builder.py` 的 `_main_nodes()`
#    （27 个）。这里漏一个就会"图里撒谎"——曾经的 26 节点版就是漏了第 12 项
#    `quantity_fill`（补全各工序工程量），它插在 WBS 审计门之后、定额锚定之前。
NODES = {
    1: ("识别当前模式", "按手选模式分流", "py", ""),
    2: ("工作模式确认", "强制 Y/N 才进", "gate", "★"),
    3: ("项目文件加载", "读施组文档", "py", ""),
    4: ("读取项目参数", "正则 + 模型", "ai", ""),
    5: ("参数复核门", "补齐 / 试算", "gate", "★"),
    6: ("边界条件补充", "场地·资源·工期", "ai", ""),
    7: ("范围与结构映射", "L3/L4 合法范围", "py", ""),
    8: ("编制 WBS 分工", "骨架→逐相展开", "ai", ""),
    9: ("节拍流水分段", "一层一段铺流水", "py", ""),
    10: ("选择展示细度", "选展示粒度", "gate", "★"),
    11: ("WBS 结构审计", "审过才往下走", "gate", "R1"),
    12: ("补全各工序工程量", "闭集表态 + 单位换算", "py", ""),
    13: ("匹配消耗量定额", "每条工序一锚", "ai", ""),
    14: ("配机械与班组", "班组+机械+容量", "py", ""),
    15: ("编排工序先后", "排先后·防成环", "ai", ""),
    16: ("计算关键路径", "理想工期对照", "py", ""),
    17: ("排程与两版工期", "理论 / 资源", "py", ""),
    18: ("两版工期审计", "审过才往下走", "gate", "R2"),
    19: ("算资源与工日", "定量 + 削峰", "py", ""),
    20: ("组装计划数据", "组装 plan_json", "py", ""),
    21: ("监督报告", "洞见 + 模板", "ai", ""),
    22: ("人工确认", "Y 交付 / N 停", "gate", "★"),
    23: ("方案交付", "校验并落盘", "py", ""),
    24: ("导出 Word 草案", "未审计·无图表", "py", ""),
    25: ("草案审计", "通过才出定稿", "gate", "R3"),
    26: ("导出 Word 定稿", "已审计 .docx", "py", ""),
    27: ("导出可视化看板", "自包含看板", "ai", ""),
}
GATE_IDS = [n for n, v in NODES.items() if v[3]]
AI_IDS = [n for n, v in NODES.items() if v[2] == "ai"]
PY_IDS = [n for n, v in NODES.items() if v[2] == "py"]
MOD_CLASS = {"ai": "modAI", "gate": "modGate", "py": "mod"}


def box_x(idx, count):
    row_w = count * BOX_W + (count - 1) * GAP_X
    return M_L + (CONTENT - row_w) / 2.0 + idx * (BOX_W + GAP_X)


def style():
    return """  <defs>
    <marker id="arr" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse"><path d="M 0 1 L 10 5 L 0 9 z" fill="#0F4761"/></marker>
    <marker id="arrG" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="6" markerHeight="6"
            orient="auto-start-reverse"><path d="M 0 1 L 10 5 L 0 9 z" fill="#9fb0bd"/></marker>
    <style>
      .ftitle { fill:#0F4761; font-size:%(fs_title)dpx; font-weight:bold; text-anchor:middle; }
      .fsub   { fill:#8496a3; font-size:%(fs_chipd)dpx; text-anchor:middle; }
      .band   { fill:#0F4761; fill-opacity:0.035; stroke:#0F4761; stroke-opacity:0.28;
                stroke-width:1.1; rx:6; }
      .blabel { fill:#0F4761; font-size:%(fs_layer)dpx; font-weight:bold; }
      .mod    { fill:#FFFFFF; stroke:#0F4761; stroke-width:1.1; rx:4; }
      .modAI  { fill:#FFFDF6; stroke:#B07D2F; stroke-width:1.1; rx:4; }
      .modGate{ fill:#FEF7F5; stroke:#B03A2E; stroke-width:1.1; rx:4; }
      .ntitle { fill:#0F4761; font-size:%(fs_node)dpx; font-weight:bold; text-anchor:middle; }
      .ndesc  { fill:#6b7b88; font-size:%(fs_desc)dpx; text-anchor:middle; }
      .chip   { fill:#FFFFFF; stroke:#0F4761; stroke-width:1.1; rx:4; }
      .chipt  { fill:#0F4761; font-size:%(fs_chip)dpx; font-weight:bold; text-anchor:middle; }
      .chipd  { fill:#6b7b88; font-size:%(fs_chipd)dpx; text-anchor:middle; }
      .note   { fill:#6b7b88; font-size:%(fs_small)dpx; text-anchor:start; }
      .tlabel { fill:#8496a3; font-size:%(fs_small)dpx; text-anchor:middle; }
      .legendT{ fill:#6b7b88; font-size:%(fs_small)dpx; text-anchor:start; }
      .vlabel { fill:#8496a3; font-size:%(fs_small)dpx; text-anchor:middle; }
      .ln     { stroke:#0F4761; stroke-width:1.7; fill:none; marker-end:url(#arr); }
      .turn   { stroke:#0F4761; stroke-width:1.5; fill:none; }
      .dash   { stroke:#9fb0bd; stroke-width:1.3; fill:none; stroke-dasharray:5 4;
                marker-end:url(#arrG); }
    </style>
  </defs>
""" % dict(fs_title=FS_TITLE, fs_layer=FS_LAYER, fs_node=FS_NODE, fs_desc=FS_DESC,
           fs_chip=FS_CHIP, fs_chipd=FS_CHIPD, fs_small=FS_SMALL)


def build():
    p = []
    mid = (M_L + W - M_R) / 2.0

    # ── 图题（一行，与层标题同量级，不另立"标题带"）──
    y = 26
    p.append('<text class="ftitle" x="%.1f" y="%d">建策 BuildPlan · 系统总体架构'
             '（27 节点顺序流水线）</text>' % (mid, y))
    p.append('<text class="fsub" x="%.1f" y="%d">算得清 · 改得动 · 审得了　|　'
             '定额为据，算法为尺，自然语言为笔　|　BuildPlan — Compute. Revise. Audit.</text>'
             % (mid, y + 16))
    y += 34

    def band(top, h, label, fill=None, stroke=None, dashed=False):
        style_attr = ""
        if fill or stroke:
            style_attr = ' style="%s%s%s"' % (
                ("fill:%s;" % fill) if fill else "",
                ("stroke:%s;" % stroke) if stroke else "",
                "stroke-dasharray:6 4;" if dashed else "")
        p.append('<rect class="band" x="%d" y="%d" width="%d" height="%d"%s/>'
                 % (M_L, top, CONTENT, h, style_attr))
        p.append('<text class="blabel" x="%d" y="%d"%s>%s</text>'
                 % (M_L + 10, top + 16,
                    (' style="fill:%s"' % stroke) if stroke else "", escape(label)))

    def chips(top, items, gap=12, pad=10):
        n = len(items)
        cw = (CONTENT - 2 * pad - (n - 1) * gap) / n
        for i, (t, d) in enumerate(items):
            bx = M_L + pad + i * (cw + gap)
            p.append('<rect class="chip" x="%.1f" y="%d" width="%.1f" height="30"/>'
                     % (bx, top + 20, cw))
            p.append('<text class="chipt" x="%.1f" y="%d">%s</text>'
                     % (bx + cw / 2, top + 33, escape(t)))
            p.append('<text class="chipd" x="%.1f" y="%d">%s</text>'
                     % (bx + cw / 2, top + 45, escape(d)))

    # ── 用户层 ──
    user_top = y
    uh = 52
    band(y, uh, "用户层（本地终端 → 本地交付物）")
    chips(y, (("本地终端控制台", "纯标准库交互循环 · 彩色 SSE 流 · 斜杠命令 · /switch 双模型"),
              ("本地交付物", "自包含 HTML 看板 · 施工进度计划.docx · 顶层索引.html")), gap=14)
    y += uh + 30

    # ── 网关层 ──
    gw_top = y
    gh = 66
    band(y, gh, "应用网关层（本地后端 FastAPI · 自研 Pipeline 引擎 · 无外部 Agent 框架）")
    chips(y, (("① 意图路由", "闲聊/提问/计划分流"), ("② 流水线调度", "顺序执行·检查点·暂停"),
              ("③ SSE 事件流", "9 类事件·严格单行帧"), ("④ 状态与存储", "run_id·共享 ctx 字典"),
              ("⑤ 人工门交互", "确认/暂停/重跑·三级门"), ("⑥ 确定性兜底", "无 key 也端到端出结果")),
          gap=8)
    p.append('<path class="dash" d="M %.1f %d L %.1f %d"/>'
             % (mid, user_top + uh + 2, mid, gw_top + 2))
    p.append('<text class="tlabel" x="%.1f" y="%d">POST /chat (SSE)</text>'
             % (mid + 80, user_top + uh + 16))
    y += gh + 30

    # ── 工作流层（7 列 × 4 行）──
    wf_top = y
    p.append('<path class="dash" d="M %.1f %d L %.1f %d"/>'
             % (mid, gw_top + gh + 2, mid, wf_top + 2))
    rows_h = len(ROWS) * BOX_H + (len(ROWS) - 1) * GAP_Y
    wf_h = 38 + rows_h + 14
    band(y, wf_h, "自研流水线工作流层（26 节点 · 顺序执行）")
    p.append('<text class="note" x="%d" y="%d">共享 ctx 字典（run_id 全池下文）—— 跨节点传参：'
             '参数 · 边界 · 中间结果 · plan_json · 支持 /revise 改参重跑</text>' % (M_L + 10, y + 33))

    pos = {}
    gy0 = y + 38
    for ri, row in enumerate(ROWS):
        ry = gy0 + ri * (BOX_H + GAP_Y)
        for ci, nid in enumerate(row):
            bx = box_x(ci, len(row))
            pos[nid] = (bx, ry)
            name, desc, kind, gate = NODES[nid]
            p.append('<rect class="%s" x="%.1f" y="%.1f" width="%d" height="%d"/>'
                     % (MOD_CLASS[kind], bx, ry, BOX_W, BOX_H))
            cx = bx + BOX_W / 2.0
            # ★/R1/R2/R3 直接接在标题后面（照原图「0.1 工作模式确认★」的写法）
            p.append('<text class="ntitle" x="%.1f" y="%.1f">%d %s%s</text>'
                     % (cx, ry + 20, nid, escape(name), escape(gate)))
            p.append('<text class="ndesc" x="%.1f" y="%.1f">%s</text>'
                     % (cx, ry + 38, escape(desc)))

    for row in ROWS:
        for ci in range(len(row) - 1):
            x1 = pos[row[ci]][0] + BOX_W
            x2 = pos[row[ci + 1]][0]
            yc = pos[row[ci]][1] + BOX_H / 2.0
            p.append('<path class="ln" d="M %.1f %.1f L %.1f %.1f"/>' % (x1, yc, x2 - 1, yc))
    rail_x = W - M_R + 8
    for ri in range(len(ROWS) - 1):
        lx, ly = pos[ROWS[ri][-1]]
        fx, fy = pos[ROWS[ri + 1][0]]
        yc = ly + BOX_H / 2.0
        gm = fy - GAP_Y / 2.0
        p.append('<path class="turn" d="M %.1f %.1f L %.1f %.1f L %.1f %.1f L %.1f %.1f"/>'
                 % (lx + BOX_W, yc, rail_x, yc, rail_x, gm, fx + BOX_W / 2.0, gm))
        p.append('<path class="ln" d="M %.1f %.1f L %.1f %.1f"/>'
                 % (fx + BOX_W / 2.0, gm, fx + BOX_W / 2.0, fy - 1))

    wf_bottom = wf_top + wf_h
    y = wf_bottom + 30

    # ── 交付物 ──
    out_top = y
    oh = 52
    band(y, oh, "交付物（Word + HTML + 索引）")
    chips(y, (("施工进度计划.docx", "草案（未审计）→ 定稿（已审计）"),
              ("计划看板.html", "自包含：甘特图·人力曲线·设备荷载·审计戳"),
              ("索引.html", "按时间倒序，自动只留最近 6 份")))
    p.append('<path class="ln" d="M %.1f %.1f L %.1f %.1f"/>'
             % (pos[26][0] + BOX_W / 2.0, wf_bottom - 2, pos[26][0] + BOX_W / 2.0, out_top + 2))
    y += oh + 22

    # ── 支线 ──
    br_top = y
    bh = 46
    band(y, bh, "支线 · 人工门 / 暂停 / 改参重算", fill="#FEF7F5", stroke=C_RED, dashed=True)
    p.append('<text class="note" x="%d" y="%d">节点暂停（node_paused）→ 用户 /revise 改参 → '
             '重跑并重算下游（recompute_after_revision）→ 审计门退回 → 重做本节点</text>'
             % (M_L + 10, y + 35))
    y += bh + 22

    # ── 运行与部署层 ──
    infra_top = y
    ih = 52
    band(y, ih, "运行与部署层（本地单机 · 全部本地进程承载）")
    chips(y, (("本地 FastAPI 后端", "uvicorn :8000 · /chat /confirm /resume"),
              ("大模型 API（qwen-plus）", "OpenAI 兼容 · 超时重试 · 降级兜底"),
              ("领域知识库 · SQLite", "BuildPlan_KB/kb.db · 19 张业务表")))
    p.append('<path class="dash" d="M %d %d L %d %d"/>'
             % (int(mid), br_top + bh + 2, int(mid), infra_top + 2))
    y += ih + 20

    # ── 图例 ──
    items = [("modAI", "含模型环节 %d 个" % len(AI_IDS)),
             ("mod", "确定性计算 %d 个" % len(PY_IDS)),
             ("modGate", "★ 人工门 %d 处（含 R1/R2/R3 回审门）" % len(GATE_IDS))]
    lx = M_L
    for cls, text in items:
        p.append('<rect class="%s" x="%d" y="%d" width="14" height="14"/>' % (cls, lx, y))
        p.append('<text class="legendT" x="%d" y="%d">%s</text>' % (lx + 20, y + 11, escape(text)))
        lx += 20 + len(text) * 7 + 22
    p.append('<text class="legendT" x="%d" y="%d">实线箭头 = 节点顺序执行　·　虚线箭头 = 跨层调用 / 依赖　·　'
             '最右侧竖直虚线 = SSE 事件流（node_start … done / confirm / resume）</text>' % (M_L, y + 28))
    y += 50

    # ── 右侧竖排 SSE 通道（照原图）──
    p.append('<path class="dash" d="M %d %d L %d %d" style="marker-end:none"/>'
             % (W - 16, infra_top + ih, W - 16, user_top + 10))
    p.append('<text class="vlabel" transform="translate(%d,%.1f) rotate(-90)">'
             'SSE 流：node_start … done ／ confirm ／ resume</text>'
             % (W - 23, (infra_top + ih + user_top) / 2.0))

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'font-family="\'Microsoft YaHei\',\'SimHei\',sans-serif">\n'
           '<title>建策 BuildPlan 系统总体架构（27 节点顺序流水线）</title>\n'
           '<desc>模仿原文档架构图风格：标题类与说明类同字号（只差字重与颜色）、'
           '七列四行横向铺开、细线箭头、右侧竖排 SSE 通道。</desc>\n%s%s</svg>\n'
           % (W, int(y), style(), "\n".join(p)))
    return svg, int(y)


# ══════════════════════════════════════════════════════════════════════════
#  图 1：五层技术栈总览
#  与图 2 同一套视觉语言：同一 viewBox 宽（1240）、同一字号体系（13px 两档：
#  加粗 #0F4761 / 常规灰 #6b7b88）、同一 4 色主题、同一盒子（1.1px 细描边 + rx4）、
#  同一层带（#0F4761 3.5% 填充 + 28% 描边 + rx6）、同一虚线跨层箭头（#9fb0bd）。
# ⚠️ 图 1 里会写「N 条自动化测试」。这个数字**必须来自真实运行**，不能拍脑袋：
#    打包前跑一次 `python -m pytest backend/tests -q`，把尾部的数字填到这里，
#    再 `python tools/draw_diagrams_svg.py --fig1` 重出图。写错就是"图里撒谎"。
TEST_COUNT = "3002"
#  内容真源：docs/图内容规格.md「图 1：五层技术栈总览」（19 张业务表 / 图里的测试数）。
#  ⚠️ 2026-09-21 起：业务表由 22 张降为 19 张（域 1.6 / H 组删了 Workface_Capacity_Rule
#     与三张 legacy 归档表）；流水线由 26 节点变为 27 节点（新增 `quantity_fill`）。

# ══════════════════════════════════════════════════════════════════════════
FIG1_SVG = ROOT / "docs" / "图1_技术栈图_SVG版.svg"
FIG1_PNG = ROOT / "docs" / "图1_技术栈图_SVG版.png"
FIG1_W = 1240
FIG1_H = 845                     # → 2480 × 1690 @device-scale-factor=2（规格表要求）
F1_M = 26                        # 四边留白 26（2× 渲染后 52 ≥ 48）
F1_CONTENT = FIG1_W - 2 * F1_M   # 1188
F1_GAP = 26                      # 层带间距（跨层虚线走这里）
F1_SUB = 10                      # 层带底内边距
F1_HEAD = 24                     # 层带顶到第一个盒子的距离

# 字号全部复用图 2 的档位：卡片标题 13 粗、正文/说明 13 常规灰、图例注释 12
F1T = 13                         # 图 1 卡片/格子标题
F1D = 13                         # 图 1 说明行
F1S = 12                         # 图例 / 跨层标签 / 层带附注

# 五层内容（文字按规格精简到 13px 一行放得下，未新增事实）
F1_L1 = [
    ("mod", "本地终端控制台（创作 / 交互）", [
        "· 纯标准库 input() 交互主循环 · 零依赖",
        "· 彩色 SSE 流 · 斜杠命令 · ! 系统命令",
        "· 人工门 + 三轮回审门 · /switch 双模型"]),
    ("mod", "本地交付物（展示 / 交付）", [
        "· 计划看板.html：SVG 甘特 · 资源曲线 · 审计戳",
        "· 施工进度计划.docx：草案（未审计）→ 定稿",
        "· 顶层 索引.html：留最近 6 份 · 标当前/历史"]),
    ("mod", "统一数据契约（单一真源）", [
        "· plan JSON Schema（backend/pipeline/schemas.py）",
        "· 落盘 plans/*.json · 只读回看",
        "· 叶子带 kb_activity_id / location / 溯源"]),
]
F1_L2 = [
    ("① 模式分流", ["用户手选四种模式", "不做意图识别"]),
    ("② 节点节奏", ["27 节点顺序流水线", "检查点 · 可暂停/取消"]),
    ("③ 状态与存储", ["run_id · 共享 ctx 字典", "落盘 plans/*.json"]),
    ("④ Human-in-loop", ["人工门 + 三轮回审", "拒绝即退回重做"]),
    ("⑤ 事件流", ["9 类事件 · SSE 流", "严格单行 JSON 帧"]),
    ("⑥ 降级容错", ["逐节点确定性兜底", "无 key 也能端到端"]),
]
F1_L3 = [
    [("modAI", "云端 LLM（qwen-plus）", [
        "· OpenAI 兼容 /chat/completions",
        "· json · text · tools 三态",
        "· 超时重试 + 降级兜底"]),
     ("mod", "确定性算法（系统计算核心）", [
        "· CPM 关键路径（Kahn 拓扑 + 正推/反推）",
        "· 两版工期：理论最短 / 资源不超额（有节拍走施工组织层）",
        "· 资源定额 · 方案汇总 · 纯 Python"]),
     ("modDoc", "领域知识库 KB · SQLite", [
        "· 19 张业务表（非向量 RAG）",
        "· L3/L4 活动 · 台班 / 人工定额",
        "· 资源工作面指标 MWI · 定额注入 WBS"])],
    [("modDoc", "逐值溯源 + 改得动", [
        "· /sources 逐值来源查询（来源分级）",
        "· /revise 自然语言改计划",
        "· 改参后重算下游并复审",
        "· 只改参数 · 定额与口径不动"]),
     ("mod", "定额锚定 + 设计班组", [
        "· 每条工序锚一条定额并记来源",
        "· 定额准入：AI 经验估算定额照常参与计算（逐条标注待审）",
        "· 工日需求 = 工程量 ÷ 定额产能",
        "· 工期 = 工日需求 ÷ 设计班组人数"]),
     ("modAI", "WBS 多级分工 + 节拍引擎", [
        "· 代码定 1 级骨架 → 逐相展开 2/3 级",
        "· 专项结构 LLM 补 1 级 · 组装融合",
        "· 节拍引擎铺流水 + 搭接（纯代码）",
        "· 复评 → 人工审核 → 定向重跑"])],
]
F1_L4 = [
    ("mod", "本地 FastAPI 后端 · uvicorn:8000", [
        "· /chat SSE 流 · /confirm · /resume · /cancel",
        "· /revise · 空闲心跳 ping · 内存 registry"]),
    ("mod", "一键部署（本地）", [
        "· 双击 一键测试.bat：装依赖 / 引导密钥",
        "· 起后端 → 等 /healthz → 起终端 → 退出清理"]),
    ("mod", "本地数据与密钥", [
        "· 密钥 LLM_API_KEY 仅存 backend/.env",
        "· plans/ · 输出结果/ · 交付物 · 索引.html"]),
]
F1_L5 = [
    ("mod", "确定性兜底", [
        "· 每节点有 fallback",
        "· 契约不漂移",
        "· 无 key 也能端到端"]),
    ("mod", "交付防污染", [
        "· 保留最近 6 份",
        "· 顶层 索引.html",
        "· 标当前/历史 · 防误开"]),
    ("mod", "密钥安全", [
        "· LLM_API_KEY",
        "· 仅存 backend/.env",
        "· 不入库 · 不入代码"]),
    ("mod", "契约一致性", [
        "· schemas.py 单一真源",
        "· 双端事件常量对齐",
        "· plan JSON 字段不漂移"]),
    ("modDoc", "可信度与口径账本", [
        "· 来源分级 user/kb/ai",
        "· 定额覆盖率",
        "· 编制口径 caliber_note"]),
    ("mod", "质量保障", [
        "· %s 条自动化测试" % TEST_COUNT,
        "· 绝不为凑工期改定额",
        "· 工期与日期单一真源"]),
]

_F1_FONT_CACHE = {}


def _font(px, bold=False):
    """用微软雅黑量真实字宽（与 Chrome 渲染同一字体族），量不到就返回 None。"""
    key = (px, bold)
    if key not in _F1_FONT_CACHE:
        f = None
        try:
            from PIL import ImageFont
            f = ImageFont.truetype("C:/Windows/Fonts/%s" % ("msyhbd.ttc" if bold else "msyh.ttc"), px)
        except Exception:
            f = None
        _F1_FONT_CACHE[key] = f
    return _F1_FONT_CACHE[key]


def measure(txt, px, bold=False):
    f = _font(px, bold)
    if f is not None:
        return float(f.getlength(txt))
    return sum(px if ord(c) > 0x2E80 else px * 0.55 for c in txt)


def style_fig1():
    """图 1 专用样式表：图 2 的全部类 + .modDoc（可选·文档类）/ .bnote。"""
    return """  <defs>
    <marker id="arr" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse"><path d="M 0 1 L 10 5 L 0 9 z" fill="#0F4761"/></marker>
    <marker id="arrG" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="6" markerHeight="6"
            orient="auto-start-reverse"><path d="M 0 1 L 10 5 L 0 9 z" fill="#9fb0bd"/></marker>
    <style>
      .ftitle { fill:#0F4761; font-size:%(fs_title)dpx; font-weight:bold; text-anchor:middle; }
      .fsub   { fill:#8496a3; font-size:%(fs_small)dpx; text-anchor:middle; }
      .band   { fill:#0F4761; fill-opacity:0.035; stroke:#0F4761; stroke-opacity:0.28;
                stroke-width:1.1; rx:6; }
      .blabel { fill:#0F4761; font-size:%(fs_layer)dpx; font-weight:bold; }
      .bnote  { fill:#8496a3; font-size:%(fs_small)dpx; text-anchor:end; }
      .mod    { fill:#FFFFFF; stroke:#0F4761; stroke-width:1.1; rx:4; }
      .modAI  { fill:#FFFDF6; stroke:#B07D2F; stroke-width:1.1; rx:4; }
      .modDoc { fill:#F5FAF7; stroke:#2E7D5B; stroke-width:1.1; rx:4; }
      .ctitle { fill:#0F4761; font-size:%(fs_node)dpx; font-weight:bold; text-anchor:middle; }
      .bline  { fill:#6b7b88; font-size:%(fs_desc)dpx; text-anchor:start; }
      .cline  { fill:#6b7b88; font-size:%(fs_desc)dpx; text-anchor:middle; }
      .legendT{ fill:#6b7b88; font-size:%(fs_small)dpx; text-anchor:start; }
      .tlabel { fill:#8496a3; font-size:%(fs_small)dpx; text-anchor:start; }
      .dash   { stroke:#9fb0bd; stroke-width:1.3; fill:none; stroke-dasharray:5 4;
                marker-end:url(#arrG); }
    </style>
  </defs>
""" % dict(fs_title=FS_TITLE, fs_layer=FS_LAYER, fs_node=F1T, fs_desc=F1D, fs_small=F1S)


def build_fig1():
    p = []
    checks = []
    mid = FIG1_W / 2.0

    def cols(n, gap):
        w = (F1_CONTENT - (n - 1) * gap) / float(n)
        return [F1_M + i * (w + gap) for i in range(n)], w

    def hbox(nlines):
        # 标题基线 top+18；说明第 i 行基线 top+32+15i；盒高 = 43 + 15(n-1)
        return 43 + 15 * (nlines - 1)

    def band(top, hh, label, note=None):
        p.append('<rect class="band" x="%d" y="%d" width="%d" height="%d"/>'
                 % (F1_M, top, F1_CONTENT, hh))
        p.append('<text class="blabel" x="%d" y="%d">%s</text>'
                 % (F1_M + 10, top + 16, escape(label)))
        checks.append(("层带标题", label, measure(label, FS_LAYER, True), F1_CONTENT - 20))
        if note:
            p.append('<text class="bnote" x="%d" y="%d">%s</text>'
                     % (F1_M + F1_CONTENT - 10, top + 16, escape(note)))
            checks.append(("层带附注", note,
                           measure(label, FS_LAYER, True) + 30 + measure(note, F1S),
                           F1_CONTENT - 20))

    def card(x, top, w, cls, title, lines, center=False):
        hh = hbox(len(lines))
        p.append('<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%d"/>'
                 % (cls, x, top, w, hh))
        p.append('<text class="ctitle" x="%.1f" y="%.1f">%s</text>'
                 % (x + w / 2.0, top + 18, escape(title)))
        checks.append(("卡片标题", title, measure(title, F1T, True), w - 20))
        for i, ln in enumerate(lines):
            yy = top + 32 + 15 * i
            if center:
                p.append('<text class="cline" x="%.1f" y="%.1f">%s</text>'
                         % (x + w / 2.0, yy, escape(ln)))
                checks.append(("格子说明", ln, measure(ln, F1D), w - 14))
            else:
                p.append('<text class="bline" x="%.1f" y="%.1f">%s</text>'
                         % (x + 12, yy, escape(ln)))
                checks.append(("卡片说明", ln, measure(ln, F1D), w - 20))
        return hh

    def band_h(content_h):
        return F1_HEAD + content_h + F1_SUB

    def link(y_from, xlist, label=None, lx=None):
        for x in xlist:
            p.append('<path class="dash" d="M %.1f %.1f L %.1f %.1f"/>'
                     % (x, y_from + 2, x, y_from + F1_GAP - 2))
        if label:
            p.append('<text class="tlabel" x="%.1f" y="%.1f">%s</text>'
                     % (lx, y_from + F1_GAP / 2.0 + 5, escape(label)))
            checks.append(("跨层标签", label, measure(label, F1S),
                           FIG1_W - F1_M - 4 - lx))

    # ── 图题 ──
    p.append('<text class="ftitle" x="%.1f" y="42">建策 BuildPlan · 五层技术栈总览</text>' % mid)
    p.append('<text class="fsub" x="%.1f" y="60">算得清 · 改得动 · 审得了　|　'
             '定额为据，算法为尺，自然语言为笔　|　华南理工大学 · 建智领航</text>' % mid)
    y = 72

    # ── 第 1 层：用户交互层 ──
    xs3, w3 = cols(3, 14)
    l1h = hbox(3)
    t1 = y
    band(y, band_h(l1h), "用户交互层（本地终端创作 → 本地交付物）")
    for x, (cls, t, lines) in zip(xs3, F1_L1):
        card(x, y + F1_HEAD, w3, cls, t, lines)
    y += band_h(l1h)

    link(y, [xs3[0] + w3 / 2.0, xs3[2] + w3 / 2.0],
         "/chat（SSE）· plan_final · confirm / resume", xs3[0] + w3 / 2.0 + 8)
    y += F1_GAP

    # ── 第 2 层：智能编排层 ──
    xs6, w6 = cols(6, 8)
    l2h = hbox(2)
    t2 = y
    band(y, band_h(l2h), "智能编排层（自研 Pipeline 引擎 · 26 节点顺序执行 · 无外部 Agent 框架）")
    for x, (t, lines) in zip(xs6, F1_L2):
        card(x, y + F1_HEAD, w6, "mod", t, lines, center=True)
    y += band_h(l2h)

    link(y, [xs6[0] + w6 / 2.0, mid, xs6[5] + w6 / 2.0],
         "能力格调用工具：LLM / 确定性算法 / 领域知识库", xs6[0] + w6 / 2.0 + 8)
    y += F1_GAP

    # ── 第 3 层：工具层（两行 × 三列，行内等宽等高）──
    l3r1, l3r2 = hbox(3), hbox(4)
    l3c = l3r1 + 12 + l3r2
    t3 = y
    band(y, band_h(l3c), "工具层（确定性优先 · LLM 只做理解/评判 · 能力可复现）",
         note="附：MCP 文件访问（自研 stdio 服务 · read_file / list_dir · 安全边界）")
    for x, (cls, t, lines) in zip(xs3, F1_L3[0]):
        card(x, y + F1_HEAD, w3, cls, t, lines)
    for x, (cls, t, lines) in zip(xs3, F1_L3[1]):
        card(x, y + F1_HEAD + l3r1 + 12, w3, cls, t, lines)
    y += band_h(l3c)

    link(y, [xs3[0] + w3 / 2.0, mid, xs3[2] + w3 / 2.0],
         "工具能力由本地后端进程编排 · 单机 uvicorn", xs3[1] + w3 / 2.0 + 8)
    y += F1_GAP

    # ── 第 4 层：运行与部署 ──
    l4h = hbox(2)
    t4 = y
    band(y, band_h(l4h), "运行与部署（本地单机 · 全部本地进程承载）")
    for x, (cls, t, lines) in zip(xs3, F1_L4):
        card(x, y + F1_HEAD, w3, cls, t, lines)
    y += band_h(l4h)

    link(y, [xs3[1] + w3 / 2.0], "同一批本地进程内的生产化保障", mid + 8)
    y += F1_GAP

    # ── 第 5 层：生产化保障（6 个框，全部完整可见）──
    l5h = hbox(3)
    t5 = y
    band(y, band_h(l5h), "生产化保障（本地单机落地 · 6 项）")
    for x, (cls, t, lines) in zip(xs6, F1_L5):
        card(x, y + F1_HEAD, w6, cls, t, lines, center=True)
    y += band_h(l5h)

    # ── 图例（4 项，与正文框留间距）──
    y += 18
    legend_y = y
    lx = F1_M
    for cls, text in (("modAI", "含模型环节"), ("mod", "确定性计算"), ("modDoc", "可选 · 文档类")):
        p.append('<rect class="%s" x="%d" y="%d" width="13" height="13"/>' % (cls, lx, legend_y))
        p.append('<text class="legendT" x="%d" y="%.1f">%s</text>'
                 % (lx + 19, legend_y + 10.5, escape(text)))
        lx += 19 + measure(text, F1S) + 34
    p.append('<path class="dash" d="M %d %.1f L %d %.1f" style="marker-end:none"/>'
             % (lx, legend_y + 6.5, lx + 26, legend_y + 6.5))
    p.append('<text class="legendT" x="%d" y="%.1f">虚线 = 调用 · 依赖 · 返回</text>'
             % (lx + 32, legend_y + 10.5))

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'font-family="\'Microsoft YaHei\',\'SimHei\',sans-serif">\n'
           '<title>建策 BuildPlan · 五层技术栈总览</title>\n'
           '<desc>五层技术栈：用户交互层 / 智能编排层 / 工具层 / 运行与部署 / 生产化保障。'
           '视觉语言与图 2 一致：13px 两档字号、四色主题、细描边小圆角、虚线跨层调用。</desc>\n'
           '%s%s</svg>\n' % (FIG1_W, FIG1_H, style_fig1(), "\n".join(p)))

    stats = dict(h=FIG1_H, top=t1, l2=t2, l3=t3, l4=t4, l5=t5,
                 l5_bottom=t5 + band_h(l5h), legend_bottom=legend_y + 13,
                 counts=[3, 6, 6, 3, 6], checks=checks)
    return svg, stats


def render_png(svg_path, png_path, w, h, scale=2):
    wrapper = svg_path.with_suffix(".render.html")
    wrapper.write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff;overflow:hidden}"
        "svg{display:block;width:%dpx;height:%dpx}</style>%s"
        % (w, h, svg_path.read_text(encoding="utf-8")), encoding="utf-8")
    chrome = Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"
    if not chrome.exists():
        print("！找不到 Chrome，跳过渲染")
        return False
    prof = Path(tempfile.gettempdir()) / "dsh_chrome_prof"
    cmd = [str(chrome), "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check", "--user-data-dir=%s" % prof,
           "--force-device-scale-factor=%d" % scale, "--default-background-color=ffffffff",
           "--window-size=%d,%d" % (w, h), "--screenshot=%s" % png_path, wrapper.as_uri()]
    # ⚠️ 必须先删目标文件再渲染：否则 Chrome 失败时**上一版的 PNG 还在**，
    #    `png_path.exists()` 照样为 True → 函数报"已写出"，而图其实是旧的
    #    （实测踩过：图里烤着的「18 张业务表 / 1233 条测试」一直没变）。
    png_path.unlink(missing_ok=True)
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wrapper.unlink(missing_ok=True)
    if r.returncode != 0 or not png_path.exists():
        print("！PNG 渲染失败（Chrome 返回 %s，目标文件%s）—— SVG 已更新，PNG 未更新"
              % (r.returncode, "已生成" if png_path.exists() else "缺失"))
        return False
    return True


def main_fig1(svg_only=False):
    svg, st = build_fig1()
    FIG1_SVG.write_text(svg, encoding="utf-8")
    print("已写出 %s" % FIG1_SVG)
    print("viewBox = 0 0 %d %d   宽高比 %.2f（横向）" % (FIG1_W, FIG1_H, FIG1_W / float(FIG1_H)))
    print("字号：图题 %d · 层标题 %d · 卡片/格子标题 %d 粗 · 说明 %d 常规灰 · 图例/标签 %d"
          % (FS_TITLE, FS_LAYER, F1T, F1D, F1S))
    print("五层框数 = 用户交互 %d / 智能编排 %d / 工具 %d / 运行与部署 %d / 生产化保障 %d"
          % tuple(st["counts"]))
    print("层带 y：L1 %d · L2 %d · L3 %d · L4 %d · L5 %d ；L5 带底 %d ；图例底 %d（画布高 %d）"
          % (st["top"], st["l2"], st["l3"], st["l4"], st["l5"],
             st["l5_bottom"], st["legend_bottom"], st["h"]))
    over = [c for c in st["checks"] if c[2] > c[3]]
    worst = sorted(st["checks"], key=lambda c: c[2] - c[3], reverse=True)[:3]
    print("文本宽度自检（微软雅黑真实字宽）：%d 条，超出 %d 条" % (len(st["checks"]), len(over)))
    for kind, txt, wd, lim in worst:
        print("  最紧 %-6s %.1f / %.1f  %s" % (kind, wd, lim, txt))
    for kind, txt, wd, lim in over:
        print("  ！溢出 %-6s %.1f > %.1f  %s" % (kind, wd, lim, txt))

    if svg_only:
        return
    if render_png(FIG1_SVG, FIG1_PNG, FIG1_W, FIG1_H):
        from PIL import Image
        im = Image.open(FIG1_PNG)
        bb = im.convert("L").point(lambda v: 255 if v < 245 else 0).getbbox()
        print("已写出 %s  %s" % (FIG1_PNG, im.size))
        print("四边留白（@2x 像素）= 左%d 上%d 右%d 下%d"
              % (bb[0], bb[1], im.size[0] - bb[2], im.size[1] - bb[3]))
        print("四边留白（1240 视图）= 左%.1f 上%.1f 右%.1f 下%.1f"
              % (bb[0] / 2.0, bb[1] / 2.0, (im.size[0] - bb[2]) / 2.0,
                 (im.size[1] - bb[3]) / 2.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--svg-only", action="store_true")
    ap.add_argument("--fig1", action="store_true",
                    help="画图 1（五层技术栈总览）；默认仍画图 2")
    args = ap.parse_args()

    if args.fig1:
        main_fig1(args.svg_only)
        return

    svg, h = build()
    OUT_SVG.write_text(svg, encoding="utf-8")
    print("已写出 %s" % OUT_SVG)
    print("viewBox = 0 0 %d %d   宽高比 %.2f（越大越横向）" % (W, h, W / float(h)))
    print("字号：图题 %d · 层标题 %d · 节点标题/说明 %d/%d px（照原图：标题与说明同号）"
          % (FS_TITLE, FS_LAYER, FS_NODE, FS_DESC))
    print("节点框 %d 个；★ 人工门 %d 处（%s）；含模型 %d 个；确定性 %d 个"
          % (len(NODES), len(GATE_IDS), GATE_IDS, len(AI_IDS), len(PY_IDS)))

    if args.svg_only:
        return
    if render_png(OUT_SVG, OUT_PNG, W, h):
        from PIL import Image
        im = Image.open(OUT_PNG)
        bb = im.convert("L").point(lambda v: 255 if v < 245 else 0).getbbox()
        print("已写出 %s  %s" % (OUT_PNG, im.size))
        print("四边留白 = 左%d 上%d 右%d 下%d"
              % (bb[0], bb[1], im.size[0] - bb[2], im.size[1] - bb[3]))


if __name__ == "__main__":
    main()
