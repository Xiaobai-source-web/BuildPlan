# -*- coding: utf-8 -*-
"""第 14 轮：把「技术说明文档」一次做完 —— 品牌标识 + 合并第一章 + 章号顺移。

为什么合成一个脚本：这三件事互相影响段落索引（插入即漂移），分三次跑容易出现
"第二次照着第一次的索引改"的错位。这里**全部用文字锚点**定位，每一步都 assert
原文，索引漂移立刻报错而不是误改。

做四件事：
  A. 品牌标识：封面区品牌块（主标语 / 副标语 / 英文 / 署名）+ 页眉 + 页脚（含页码）
     + 文末品牌块。用户反馈"说明文档里还是没有插入足够的产品标识"。
  B. 章号顺移：原「一、系统概述」…「十一、总结」→「二、…」…「十二、…」，
     二级编号 1.1→2.1 … 10.4→11.4 同步；交叉引用 §4.2 → §5.2。
  C. 合并第一章《背景与场景》（用户放进来的 1背景与场景.docx）——**带 7 处修正**：
       · "上传文档" → 把施组文件的**本地路径**交给系统（没有上传功能）
       · "生成→审查→重生成闭环审核" → 三级人工门 + 三轮回审门，用户不确认不放行
       · "缓存回放 / 浏览器即可访问" → 本地终端；浏览器只用于 /show 打开看板
       · "多方案左右并排对比" → 未实现（标为规划中）
       · 场景二"自动追回工期" → 未实现；改写为已实现的"改得动"（/revise）
       · 名创优品算例（仓库里查不到任何输入产出，复现不出）
         → 换成**可复现**的真实用例：交付包内 项目样例/示例4_真实工程用例_广州潭村安置地块.docx
  D. 自检：章号无重复、品牌串齐全、旧表述 0 残留。

保留格式的手法沿用 step1/step2：**克隆参考段落的 XML 再改文字**。

用法：
    python tools/finalize_docx.py --dry-run
    python tools/finalize_docx.py
"""
import argparse
import copy
import shutil
import sys
from datetime import datetime
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "建策BuildPlan_技术说明文档_v3.0.docx"

BRAND_L1 = "建策 BuildPlan · 施工进度计划生成系统"
BRAND_SLOGAN = "算得清 · 改得动 · 审得了"
BRAND_SUB = "定额为据，算法为尺，自然语言为笔"
BRAND_EN = "BuildPlan — Compute. Revise. Audit."
BRAND_SIGN = "华南理工大学 · 建智领航"
BRAND_HEADER = "建策 BuildPlan　|　" + BRAND_SLOGAN
BRAND_FOOTER = BRAND_SIGN + "　|　" + BRAND_EN

# ── B. 章号顺移映射（旧前缀 → 新前缀）。只匹配段落**开头**，逐条 assert 唯一 ──
RENUM = [
    ("一、系统概述", "二、系统概述"),
    ("1.1 系统定位", "2.1 系统定位"),
    ("1.2 设计原则", "2.2 设计原则"),
    ("1.3 系统特色", "2.3 系统特色"),
    ("二、总体架构（三层）", "三、总体架构（三层）"),
    ("2.1 关键架构决策", "3.1 关键架构决策"),
    ("三、用户交互层", "四、用户交互层"),
    ("3.1 本地终端控制台", "4.1 本地终端控制台"),
    ("3.2 本地交付物", "4.2 本地交付物"),
    ("3.3 统一数据契约", "4.3 统一数据契约"),
    ("3.4 终端命令体系", "4.4 终端命令体系"),
    ("四、智能体编排层", "五、智能体编排层"),
    ("4.1 引擎中枢的关键能力", "5.1 引擎中枢的关键能力"),
    ("4.2 流水线节点", "5.2 流水线节点"),
    ("4.3 三级人工门", "5.3 三级人工门"),
    ("4.4 进度实时反馈", "5.4 进度实时反馈"),
    ("4.5 展示粒度", "5.5 展示粒度"),
    ("五、能力工具层", "六、能力工具层"),
    ("5.1 大模型推理", "6.1 大模型推理"),
    ("5.2 确定性算法", "6.2 确定性算法"),
    ("5.3 工程文件接入", "6.3 工程文件接入"),
    ("5.4 内置领域知识库", "6.4 内置领域知识库"),
    ("5.5 逐值溯源", "6.5 逐值溯源"),
    ("5.6 改得动", "6.6 改得动"),
    ("六、本地基础设施与运行", "七、本地基础设施与运行"),
    ("七、核心业务流程", "八、核心业务流程"),
    ("八、生产化保障", "九、生产化保障"),
    ("8.1 可靠兜底", "9.1 可靠兜底"),
    ("8.2 交付物防污染", "9.2 交付物防污染"),
    ("8.3 密钥安全", "9.3 密钥安全"),
    ("8.4 前端一致性与双轨", "9.4 前端一致性与双轨"),
    ("九、技术选型", "十、技术选型"),
    ("十、部署与运维", "十一、部署与运维"),
    ("10.1 运行环境", "11.1 运行环境"),
    ("10.2 部署流程（一键）", "11.2 部署流程（一键）"),
    ("10.3 常见问题", "11.3 常见问题"),
    ("10.4 运维要点", "11.4 运维要点"),
    ("十一、总结", "十二、总结"),
]

# ── 交叉引用：§x.y → §(x+1).y。**必须降序替换**，否则 §4.→§5. 的结果会被
#    后面的 §5.→§6. 再吃一次（实测 §4.2 变成了 §6.2）──
XREF = [("§5.", "§6."), ("§4.", "§5."), ("§3.", "§4."), ("§2.", "§3."), ("§1.", "§2.")]

# ── 品牌露出：给已有的版本行与图题加产品名（页眉页脚之外再多几处）──
BRANDIFY_PREFIXES = ("版本：v3.0", "图 1", "图 2")

# ── 原文里指错的引用（核过内容后按小节名改写）──
TEXT_FIXES = [
    ("（见 §6.3 的双向改参机制）", "（见「本地基础设施与运行」一节的「改参重算机制」）"),
]

# ── C. 第一章内容（已修正）──
# kind: major=章级标题 / sub=小节标题 / body=正文 / bullet=项目符号 / label=加粗小标题
CH1 = [
    ("major", "一、背景与场景"),
    ("body", "本章说明系统为谁而做、解决什么问题、在什么场景下使用，以及它带来的价值。"
             "文中所有能力描述均与交付包中的实际实现一一对应；尚未实现的部分会明确标注「规划中」。"),

    ("sub", "1.1 目标用户"),
    ("body", "用户身份：工程项目的施工管理人员、进度计划编制人员、项目经理、工程管理部门人员。"
             "主要面向建设工程领域（房建、市政、基础设施等）的中基层管理者。"),
    ("body", "年龄或阶段：25-45 岁为主，处于项目执行与管控的关键岗位，具备一定的工程施工管理经验。"),
    ("body", "行为特征：日常工作需要频繁处理工期调整、资源配置、进度汇报等事务；经常面临工期偏差、"
             "工况变化等突发情况，需要快速调整方案并重新生成进度计划。用户通常习惯使用 Word 编写施工组织设计、"
             "用 Excel 管理工程量数据，但缺乏专业的进度排程软件操作经验。"),
    ("body", "数字化能力：具备基础的办公软件操作能力（如 Word、Excel），熟悉施工组织设计文档的编写规范，"
             "但普遍不具备编程或复杂排程软件（如 MS Project、P6）的操作经验。对工具的要求是低门槛、直观、快速上手"
             "——这也是系统选择「对话式输入」（用户只需把施工组织设计文件的**本地路径**交给系统，"
             "系统自动读取并解析）而非「表单式填参」的核心原因。"),

    ("sub", "1.2 用户痛点与问题定义"),
    ("label", "痛点一：从「文档」到「进度计划」的转化依赖人工，效率低且易出错"),
    ("body", "具体场景：项目启动阶段，施工管理人员已经编写了详细的施工组织设计文档（含工程概况、施工部署、"
             "主要工程量、资源配置计划等），但要将这些文本内容转化为一份可执行的进度计划（含 WBS 分解、工序排程、"
             "关键路径、资源曲线），仍需人工逐项摘录参数、手动在 Excel 或 Project 中排程。"),
    ("body", "现有解决方式：由计划工程师手动阅读施工组织设计，提取关键参数后在 Excel 或 MS Project 中"
             "逐条录入任务、设置工期、建立依赖关系、分配资源。"),
    ("body", "现有方式存在的不足："),
    ("bullet", "•  施工组织设计中的工程量、工期等信息为非结构化文本，无法被排程软件直接读取，"
               "需人工「翻译」为结构化数据，耗时长且容易遗漏。"),
    ("bullet", "•  一旦工况变化（如材料到货延迟、设计变更），需要逐项手动修改关联任务，"
               "极易遗漏且难以评估对整体工期的影响。"),
    ("bullet", "•  传统排程软件无法理解施工组织设计中的技术间歇约束（如混凝土养护 3-7 天、防水闭水试验）"
               "和资源峰值限制，生成的计划常脱离实际。"),
    ("body", "要解决的核心问题：如何让系统直接「读懂」施工组织设计文档，自动完成从非结构化文本到"
             "可执行进度计划的转化，让用户从繁琐的手动排程中解放出来。"),
    ("label", "痛点二：排程软件操作门槛高，中小项目团队难以负担"),
    ("body", "具体场景：大量中小型施工项目（如单栋厂房、小型公建、市政节点工程）同样需要编制进度计划，"
             "但团队中往往没有配备专职计划工程师，项目经理或施工员需要兼顾进度编排工作。"),
    ("body", "现有解决方式：部分团队尝试使用 MS Project 等专业排程软件，但因学习曲线陡峭而放弃；"
             "更多团队回归 Excel 手工排程，或依赖经验丰富的老师傅「凭感觉」估算工期。"),
    ("body", "现有方式存在的不足："),
    ("bullet", "•  专业排程软件操作复杂，需专门培训，对中小型项目团队不友好。"),
    ("bullet", "•  Excel 手工排程缺乏逻辑校验，关键路径、资源冲突等问题难以自动发现。"),
    ("bullet", "•  缺乏行业知识沉淀，不同项目之间的进度计划编制经验难以复用。"),
    ("body", "要解决的核心问题：如何降低进度计划编制的专业门槛，让不具备排程软件操作经验的"
             "项目管理人员也能快速生成专业、合规的进度计划。"),

    ("sub", "1.3 使用场景"),
    ("label", "场景一：新建施工进度计划——从施工组织设计到完整方案"),
    ("body", "用户已完成某项目的施工组织设计编制（含工程概况、工程量清单、施工部署等），"
             "在终端里给出该文件的本地路径并输入「请根据这份施组帮我生成一份进度计划」。"
             "系统自动读取文档，按预定义 Schema 提取工程概况、技术约束、资源约束、外部约束等参数；"
             "对文档中缺失的关键参数（如栋数、层数、部分工种配置），系统**不会替你猜**——"
             "参数门会把缺什么、缺了会怎样一次讲清，请你补齐或明确选择「试算」（试算结果会在交付物上"
             "标注为不可用于施工）。参数齐备后，系统自动完成 WBS 多级分解、工序依赖关系生成、"
             "关键路径 CPM 计算、知识库定额锚定与设计班组求解、两版工期排程与资源削峰，"
             "最终输出一份含总体概述、关键里程碑、关键工序、全部工序排程、资源总需求计划的完整进度方案，"
             "并同步渲染为 HTML 看板（甘特图、人员配置曲线、设备负荷）供你审阅。"),
    ("body", "全程设有**三级人工门**（进入工作模式、计划细度、终稿确认）与**三轮回审门**"
             "（【R1】WBS 结构审计、【R2】两版工期审计、【R3】草案审计）：每一步都由你按键拍板，"
             "不确认就不放行；要中止随时可以，系统不会替你决定、也不会静默通过。"),
    ("body", "真实算例（可复现）：以交付包内「项目样例/示例4_真实工程用例_广州潭村安置地块.docx」"
             "为例。该文档给出总建筑面积 301,354.26 ㎡、"
             "总工期 900 日历天、总劳动力峰值 929 人等关键参数；在参数门补上「栋数 12、地上 38 层」后，"
             "系统编制出 415 条工序叶子、109 条关键路径任务，定额口径覆盖率 67.0%（278/415 条任务有据可查"
             "并计入定额工日需求，其余如实标注缺口）。单栋定额工日需求合计 50,671 人日"
             "（架子工 316、模板工 224、瓦工 7,690、钢筋工 42,441），另有机械主导任务的人工需求 23,184 人日，"
             "实际总用工 73,855 人日；无人力限额时两版工期一致（理论最短 = 资源不超额 = 1,495 天）。"),
    ("body", "这个算例恰好演示了「审得了」：该项目自身给出的总工期是 900 天、总劳动力峰值 929 人，"
             "而按文档给出的钢筋量（7.5 万吨，按 12 栋折算到单栋约 6,250 吨）套用知识库《劳动定额》，"
             "仅钢筋工一项的工日需求就有 42,441 人日——**在定额口径下 900 天装不下**。"
             "系统不去修改定额来凑这个目标，而是如实报出差额，并把缺口逐条列出来"
             "（模板工与架子工的设计班组不足 3 人、45 条任务缺工作面容量数据、38 条任务定额单位不一致）。"
             "所有数字都可由交付包内的样例复现：同一份输入，跑多少次结果都一样。"),
    ("label", "场景二：用自然语言修改计划（「改得动」）"),
    ("body", "计划生成后，你可以用 /revise 直接改，例如「把主体结构的钢筋工加到 60 人」"
             "或「地下室工期压到 30 天」。系统只重算受影响的下游，并把改动、依据与结果写回计划；"
             "/versions 看修改版本、/undo 回退、/goto 跳到指定版本，改动全程留痕。"
             "若你改的是工期，系统不会去改定额，而是反解所需班组"
             "（班组 = ⌈工日需求 ÷ 天数⌉）——「定额不可动」与「用户可指挥」两条原则都不破。"),
    ("body", "规划中（尚未实现）：工况扰动（台风停工、构件到货延迟等）的自动解析，以及"
             "「组织优化 → 峰值内增补资源 → 禁止单纯顺延」的三档自动追回。"
             "当前版本需要你用自然语言说明扰动、由系统重算，系统**不自动判定影响范围**。"),
    ("label", "场景三：多方案比选（规划中）"),
    ("body", "项目开工前，用户往往需要评估不同的施工组织方案（如「增加一个作业班组」"
             "对比「延长工期 2 周」）。当前版本可以借助 /revise 与 /versions 逐个生成并回看"
             "不同参数下的计划版本（各自的总工期、关键路径与资源需求都可以分别查看）；"
             "**两个方案的甘特图左右并排对比尚未实现**，此处如实标注，不做超前承诺。"),

    ("sub", "1.4 价值体现"),
    ("table", "价值维度|具体体现"),
]

# 1.4 价值表 5 行（旧的错误表述已修正）
CH1_TABLE = [
    ("效率提升",
     "用户只需给出施工组织设计文件的本地路径并用自然语言下达指令，系统自动完成从文档解析→参数提取"
     "→WBS 分解→依赖生成→CPM 计算→定额锚定→两版排程→资源削峰→方案生成→审核定稿的全流程，"
     "将原本需要数小时乃至数天的人工排程工作压缩到分钟级；同一份项目参数可在会话内连续迭代"
     "（/revise），无需重新录入。"),
    ("体验改善",
     "无需安装任何专业排程软件（如 MS Project、P6），一条命令在本机终端启动即可；采用对话式交互，"
     "用户用自然语言描述需求即可，无需学习复杂的软件操作；工作流运行过程实时可视化"
     "（意图识别→参数提取→WBS→CPM→资源平衡→报告生成），用户可清晰感知系统「正在做什么」，"
     "消除黑盒焦虑。需要浏览器时只有一种情形：用 /show 打开本机生成的看板页面。"),
    ("决策辅助",
     "自动输出关键路径任务列表（锁定影响工期的关键工序）、资源总需求计划（峰值人数、总人工日、"
     "设备类型与数量），帮助管理者科学调配资源；/versions 可回看并比较历次修改版本的工期、"
     "关键路径与资源需求，辅助择优决策。"),
    ("管理规范化",
     "系统严格遵守「计算与展示分离」原则：能用固定规则算清的一律由算法计算，模型只负责理解与判断，"
     "数据格式标准化、可复用性强；将技术间歇不可压缩（混凝土养护 3-7 天、防水闭水试验等，"
     "依据 GB 50204 等规范）、资源峰值约束、工程量单位规则等施工领域硬约束固化进系统，"
     "确保生成的计划符合施工规范而非「看起来合理」；三级人工门 + 三轮回审门"
     "（用户不确认不放行）进一步保证方案质量与合规性。"),
    ("成本降低",
     "减少因进度计划编制疏漏或调整失误导致的工期延误和资源浪费，降低项目延期风险和相关成本；"
     "中小型项目团队无需额外聘请专职计划工程师或购买昂贵排程软件，即可获得专业级的进度计划编制能力。"),
]


def set_text(p, text):
    """只改第一个 run 的文字、清空其余 —— 保留原段落的字体字号等直接格式。"""
    if p.runs:
        p.runs[0].text = text
        for r in p.runs[1:]:
            r.text = ""
    else:
        p.add_run(text)


def set_rich_text(p, text):
    """同 set_text，但把 **...** 之间的内容**真的加粗**（不是留字面星号）。"""
    segs = text.split("**")
    if len(segs) == 1:
        set_text(p, text)
        return
    if p.runs:
        base = p.runs[0]
        for r in p.runs[1:]:
            r.text = ""
    else:
        base = p.add_run("")
    base.text = segs[0]
    if segs[0] == "":
        base.bold = False
    cur = base._r
    for i, seg in enumerate(segs[1:], start=1):
        new = copy.deepcopy(base._r)
        for t in new.findall(qn("w:t")):
            new.remove(t)
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = seg
        new.append(t)
        rpr = new.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            new.insert(0, rpr)
        for b in rpr.findall(qn("w:b")):
            rpr.remove(b)
        if i % 2 == 1:
            rpr.insert(0, OxmlElement("w:b"))
        cur.addnext(new)
        cur = new


def clone_after(ref_p, cursor):
    new_p = copy.deepcopy(ref_p._p)
    cursor._p.addnext(new_p)
    return Paragraph(new_p, cursor._parent)


def find_one(doc, prefix, what="段落"):
    hits = [p for p in doc.paragraphs if p.text.strip().startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit("锚点不唯一：%s 命中 %d 处（%s）" % (what, len(hits), prefix))
    return hits[0]


def find_refs(doc):
    """借用原文格式的参考段落。"""
    refs = {
        "major": find_one(doc, "二、系统概述"),
        "sub": find_one(doc, "2.1 系统定位"),
        "body": find_one(doc, "本系统是一套面向施工项目"),
        "bullet": find_one(doc, "•  本地单机"),
    }
    return refs


def fill_table(tbl, rows):
    """按行填表：保留原表格样式与单元格格式，只改文字。"""
    for ri, row in enumerate(tbl.rows):
        for ci, cell in enumerate(row.cells):
            text = rows[ri][ci] if ri < len(rows) else ""
            p = cell.paragraphs[0]
            set_text(p, text)
            for extra in cell.paragraphs[1:]:
                set_text(extra, "")


def insert_chapter_one(doc, refs):
    """把（已修正的）第一章插到「二、系统概述」之前。"""
    anchor = find_one(doc, "二、系统概述")
    cursor = anchor
    # 插入位置：anchor 之前 → 先把 cursor 移到 anchor 的前一个段落
    prev = None
    for p in doc.paragraphs:
        if p._p is anchor._p:
            break
        prev = p
    cursor = prev if prev is not None else anchor

    n = 0
    for kind, text in CH1:
        if kind == "table":
            # 1.4 价值表：克隆文档里同规格的 6 行 x 2 列表（表 5：组件/作用）
            src = None
            for t in doc.tables:
                if len(t.rows) == 6 and len(t.columns) == 2:
                    src = t
                    break
            if src is None:
                raise SystemExit("找不到 6x2 参考表格")
            new_tbl = copy.deepcopy(src._tbl)
            cursor._p.addnext(new_tbl)
            tbl = Table(new_tbl, doc._body)
            rows = [tuple(text.split("|"))] + CH1_TABLE
            fill_table(tbl, rows)
            n += 1
            continue
        ref = refs["major"] if kind == "major" else (
            refs["sub"] if kind in ("sub", "label") else (
                refs["bullet"] if kind == "bullet" else refs["body"]))
        np = clone_after(ref, cursor)
        set_rich_text(np, text)
        cursor = np
        n += 1
    return n


def add_brand(doc):
    """封面品牌块 + 页眉 + 页脚（含页码）。"""
    paras = doc.paragraphs
    title = paras[0]
    assert "技术说明文档" in title.text, title.text
    set_text(title, BRAND_L1)

    ref_body = find_one(doc, "本系统是一套面向施工项目")
    ref_sub = find_one(doc, "2.1 系统定位")

    cursor = title
    for ref, text in ((ref_sub, "—— 技术说明文档 v3.0"),
                      (ref_sub, BRAND_SLOGAN),
                      (ref_sub, BRAND_SUB),
                      (ref_sub, BRAND_EN),
                      (ref_body, BRAND_SIGN + "　|　交付包：建策BuildPlan_一键测试包_v3.0（海之子）")):
        np = clone_after(ref, cursor)
        set_rich_text(np, text)
        cursor = np

    # 文末品牌块
    last = [p for p in doc.paragraphs if p.text.strip()][-1]
    cursor = last
    for ref, text in ((ref_sub, BRAND_EN), (ref_body, BRAND_SIGN)):
        np = clone_after(ref, cursor)
        set_rich_text(np, text)
        cursor = np

    # 页眉 / 页脚
    sec = doc.sections[0]
    h = sec.header.paragraphs[0]
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hr = h.add_run(BRAND_HEADER)
    hr.font.size = Pt(9)
    hr.font.color.rgb = RGBColor(0x59, 0x59, 0x59)

    f = sec.footer.paragraphs[0]
    f.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = f.add_run(BRAND_FOOTER + "　|　第 ")
    fr.font.size = Pt(9)
    fr.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    inner = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "18")
    rpr.append(sz)
    inner.append(rpr)
    t = OxmlElement("w:t")
    t.text = "1"
    inner.append(t)
    fld.append(inner)
    f._p.append(fld)
    fr2 = f.add_run(" 页")
    fr2.font.size = Pt(9)
    fr2.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    return 5 + 2


def _fix_xref(p):
    """把一段里的 §x.y 按章号顺移改写；返回是否改动。"""
    s = p.text
    if "§" not in s:
        return False
    out = s
    for a, b in XREF:                       # 降序：§5.→§6. 先做，避免级联自增
        out = out.replace(a, b)
    if out == s:
        return False
    set_text(p, out)
    return True


def renumber(doc):
    done = []
    for old, new in RENUM:
        hits = [p for p in doc.paragraphs if p.text.strip().startswith(old)]
        if len(hits) != 1:
            raise SystemExit("顺移锚点不唯一：%r 命中 %d 处" % (old, len(hits)))
        p = hits[0]
        set_text(p, new + p.text.strip()[len(old):])
        done.append((old, new))

    # 表格里的交叉引用 §x.y
    xref = 0
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if _fix_xref(p):
                        xref += 1

    # 原文这处 §6.3 本来就指错了（「双向改参机制」在「本地基础设施与运行」一节，
    # 那一节没有二级编号）→ 改成按小节名引用，以后章号再动也不会错。
    fixed = 0
    for old, new in TEXT_FIXES:
        for p in doc.paragraphs:
            if old in p.text:
                set_text(p, p.text.replace(old, new))
                fixed += 1
    # 正文段落里的 §x.y（数量少，但要一并顺移）
    for p in doc.paragraphs:
        if _fix_xref(p):
            xref += 1
    return done, xref, fixed


def brandify(doc):
    """给版本行与图题加「建策 BuildPlan」，让产品名在正文里反复出现。"""
    n = 0
    for p in doc.paragraphs:
        s = p.text.strip()
        if not s or s.startswith("建策 BuildPlan"):
            continue
        if s.startswith("版本：v3.0"):
            set_rich_text(p, "建策 BuildPlan · 版本 v3.0" + s[len("版本：v3.0"):])
            n += 1
        elif s.startswith(BRANDIFY_PREFIXES):
            set_rich_text(p, "建策 BuildPlan · " + s)
            n += 1
    return n


def fix_bold_markers(doc):
    """把文档里残留的字面 ** 标记转成真加粗（正文 + 表格，含历史遗留）。"""
    targets = list(doc.paragraphs)
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                targets.extend(cell.paragraphs)
    n = 0
    for p in targets:
        if "**" in p.text:
            set_rich_text(p, p.text)
            n += 1
    return n


def verify(doc):
    txt = "\n".join(p.text for p in doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                txt += "\n" + c.text
    bad = []
    for s in ("名创优品", "缓存回放", "浏览器即可访问", "生成→审查→重生成"):
        if s in txt:
            bad.append(s)
    for s in (BRAND_SLOGAN, BRAND_SUB, BRAND_EN, BRAND_SIGN, BRAND_L1):
        if s not in txt:
            bad.append("缺品牌：" + s)
    # 章号唯一
    seen = {}
    for p in doc.paragraphs:
        s = p.text.strip()
        for cn in ("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、",
                   "九、", "十、", "十一、", "十二、"):
            if s.startswith(cn) and len(s) < 30:
                seen[s] = seen.get(s, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    xrefs = []
    for p in doc.paragraphs:
        if "§" in p.text:
            xrefs.append(p.text.strip()[:90])
    for tbl in doc.tables:
        for row in tbl.rows:
            for c in row.cells:
                if "§" in c.text:
                    xrefs.append(c.text.replace("\n", " ").strip()[:90])
    return bad, seen, dup, xrefs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DOC.exists():
        sys.exit("找不到：%s" % DOC)
    doc = docx.Document(str(DOC))
    print("读入 %s：段落 %d、表格 %d" % (DOC.name, len(doc.paragraphs), len(doc.tables)))

    print("\n[A] 章号顺移 %d 条（必须先做：后面借「2.1 系统定位」当格式参考）" % len(RENUM))
    if args.dry_run:
        for old, new in RENUM:
            hits = [p for p in doc.paragraphs if p.text.strip().startswith(old)]
            print("    %-28s -> %-28s 命中 %d" % (old, new, len(hits)))
        n_xref = 0
    else:
        done, n_xref, n_fixed = renumber(doc)
        n_brandify = brandify(doc)
        print("    已顺移 %d 条，交叉引用 %d 处，错引修正 %d 处，品牌前缀 %d 处"
              % (len(done), n_xref, n_fixed, n_brandify))

    print("\n[B] 品牌块 + 页眉页脚")
    print("    标题段落 = %r" % doc.paragraphs[0].text[:40])
    if not args.dry_run:
        n_brand = add_brand(doc)
        print("    已写入品牌段 %d 段 + 页眉 1 + 页脚 1" % n_brand)

    print("\n[C] 合并第一章（%d 段 + 1 张表）" % len(CH1))
    if not args.dry_run:
        refs = find_refs(doc)
        n = insert_chapter_one(doc, refs)
        print("    已插入 %d 个块" % n)
        n_bold = fix_bold_markers(doc)
        print("    字面 ** 标记转真加粗：%d 段" % n_bold)

    print("\n[D] 自检")
    if args.dry_run:
        print("    [dry-run] 未写盘")
        return
    bad, seen, dup, xrefs = verify(doc)
    print("    旧表述残留 / 缺品牌：%s" % (bad or "无"))
    print("    章级标题：%s" % sorted(seen))
    print("    重复章号：%s" % (dup or "无"))
    print("    交叉引用：%s" % (xrefs or "无"))

    bak = DOC.with_name("建策BuildPlan_技术说明文档_v3.0.bak_%s.docx"
                        % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(DOC, bak)
    doc.save(str(DOC))
    print("\n已备份 -> %s" % bak.name)
    print("已写回 -> %s（段落 %d、表格 %d）"
          % (DOC.name, len(docx.Document(str(DOC)).paragraphs),
             len(docx.Document(str(DOC)).tables)))


if __name__ == "__main__":
    main()
