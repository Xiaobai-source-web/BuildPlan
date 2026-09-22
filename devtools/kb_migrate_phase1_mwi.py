# -*- coding: utf-8 -*-
"""阶段1：Resource_Workface_Index（MWI 表）建表与灌数。

设计要点（用户 2026-09-21 裁定）
  - **一张表装全部** MWI：人工 + 机械。作业面面积不再单独存表（FACE_AREA_BY_L3 废弃，
    作业面面积 = 施工段面积，由 MSSA 分段规则产生）。
  - **直接确定** MWI，不再从旧 Workface_Capacity_Rule 反推（旧表 crew_max 恒为 15，
    反推等于把常数包装成依据）。逐行标 AI_ESTIMATE_V1。
  - 机械分四类：area（面积型，有 MWI）/ position（工位型，1 台/段）/
    auxiliary（配套型，由主控派生）/ transport（运输型，由主控产能派生）/ site（场地型）
  - 单位：㎡/人（人工）、㎡/台（机械，area 型）

用法
  python devtools/kb_migrate_phase1_mwi.py --db <kb路径>                # dry-run（默认）
  python devtools/kb_migrate_phase1_mwi.py --db <kb路径> --apply        # 写入（先备份）
  python devtools/kb_migrate_phase1_mwi.py --db <kb路径> --verify       # 只读校验
"""
import argparse
import datetime
import io
import os
import sqlite3
import sys

TABLE = "Resource_Workface_Index"
AI = "AI_ESTIMATE_V1"

DDL = """
CREATE TABLE IF NOT EXISTS Resource_Workface_Index (
    resource_id      TEXT PRIMARY KEY,
    resource_kind    TEXT NOT NULL CHECK (resource_kind IN ('labor','machine')),
    resource_name    TEXT NOT NULL,
    capacity_mode    TEXT NOT NULL CHECK (capacity_mode IN
                        ('area','position','auxiliary','transport','site')),
    mwi              REAL,
    mwi_unit         TEXT,
    resource_mobility TEXT CHECK (resource_mobility IN ('fixed','mobile','site')),
    companion_ratio  TEXT,
    basis_formula    TEXT,
    source_code      TEXT,
    source_type      TEXT,
    confidence       TEXT,
    review_state     TEXT DEFAULT 'needs_review',
    notes            TEXT,
    created_at       TEXT,
    updated_at       TEXT
)
"""

CREATED = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------- 人工（18）
# 三档工位密度：密 12~15 / 中 20 / 疏 25~30 ㎡/人
LABOR = [
    # (工种, MWI, 档, 理由)
    ("电焊工", 12, "密", "焊位窄，焊接集中在接口，工位间距小"),
    ("瓦工", 12, "密", "沿墙线作业，单人工作面沿墙展开、进深小"),
    ("抹灰工", 12, "密", "墙面作业为主，同墙面上多人并排可展开；C2 2026-09-21 由 15 调为 12"),
    ("油漆工", 15, "密", "与抹灰同理，墙面/顶面涂饰"),
    ("装修工", 15, "密", "天棚及装饰面作业，工位密集"),
    ("保温工", 15, "密", "板/棉铺贴，面作业"),
    ("防水工", 15, "密", "卷材/涂料面作业"),
    ("钢筋工", 12, "密", "板面绑扎需站位+料堆+绑扎空间；C2 2026-09-21 由 20 调为 12"),
    ("模板工", 15, "密", "支模需操作面+模板临时堆放；C2 2026-09-21 由 20 调为 15"),
    ("架子工", 20, "中", "搭设需杆件周转空间"),
    ("电工", 20, "中", "配管配线需开槽/排布空间"),
    ("管道工", 20, "中", "管道安装需接管操作空间"),
    ("通风工", 20, "中", "风管制作安装需展开面"),
    ("安装工", 20, "中", "门窗安装需洞口与吊装空间"),
    ("泥工", 20, "中", "楼地面找平/块料，需刮平空间"),
    ("混凝土工", 25, "疏", "需振捣、泵管移动、找平空间"),
    ("桩机工", 25, "疏", "桩机配合人员需机械周边作业空间"),
    ("普工", 25, "疏", "配合/搬运/清理，需通行与堆料空间；C2 2026-09-21 由 30 调为 25"),
]

# ---------------------------------------------------------------- 机械（49）
# area：MWI 数值；其余四类无数值
MACHINE = [
    # ---- A 面积型（9）----
    ("履带式推土机", "area", 667, "GD_2018_A1_1 平整场地 0.15 台班/100㎡ → 100/0.15",
     "GD_2018_A1_1", "regional_quota", "HIGH", "真实规范台班反推（唯一可推导的机械之一）"),
    ("电动夯实机", "area", 179, "GD_2018_A1_1 原土打夯 台班/㎡ 反推",
     "GD_2018_A1_1", "regional_quota", "HIGH", "真实规范台班反推"),
    ("履带式单斗液压挖掘机", "area", 500, "一个 500㎡ 施工段 1 台（开挖分区）",
     AI, "ai_estimate", "LOW", None),
    ("抓铲挖掘机", "area", 500, "同挖掘机", AI, "ai_estimate", "LOW", None),
    ("轮胎式装载机", "area", 500, "同挖掘机", AI, "ai_estimate", "LOW", None),
    ("拖式铲运机", "area", 800, "铲运机作业范围大于挖掘机", AI, "ai_estimate", "LOW", None),
    ("钢轮内燃压路机", "area", 1500, "压路机按碾压带宽度×行程估",
     AI, "ai_estimate", "LOW", "旧推导值 5556 ㎡/台班（原土打夯行）疑规范解析错位，未采用"),
    ("钢轮振动压路机", "area", 1500, "同钢轮内燃压路机", AI, "ai_estimate", "LOW", None),
    ("混凝土输送泵车", "area", 500, "用户裁定示例：500㎡/台",
     AI, "ai_estimate", "LOW", "用户 2026-09-21 举例值"),
    # ---- B 工位型（21）：1 台/施工段 ----
    ("静力压桩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("履带式柴油打桩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("振动沉拔桩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("冲击式打桩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("吊锤打桩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("长螺旋钻机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("回旋钻机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("螺旋钻机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("履带式旋挖钻机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("工程地质液压钻机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("双液压注浆泵", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("电动灌浆机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("履带式起重机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("汽车式起重机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段（吊装作业工位）"),
    ("卷扬机架(单笼5t内)", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("风动凿岩机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("履带式单头岩石破碎机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("岩石切割机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("内切割机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("灰浆搅拌机", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段"),
    ("铁驳船", "position", None, None, AI, "ai_estimate", "LOW", "1 台/施工段（水上作业配套）"),
    # ---- C 配套型（11）：由主控/班组派生 ----
    ("交流弧焊机", "auxiliary", None, "1 台/1~2 名电焊工", AI, "ai_estimate", "LOW", None),
    ("剪板机", "auxiliary", None, "1 台/金属加工作业面", AI, "ai_estimate", "LOW", None),
    ("管子切断机", "auxiliary", None, "1 台/1~2 名管道工", AI, "ai_estimate", "LOW", None),
    ("电动修钎机", "auxiliary", None, "1 台/凿岩作业面", AI, "ai_estimate", "LOW", None),
    ("内燃空气压缩机", "auxiliary", None, "1 台/凿岩/喷涂作业面", AI, "ai_estimate", "LOW", None),
    ("电动空气压缩机", "auxiliary", None, "同上", AI, "ai_estimate", "LOW", None),
    ("潜水泵", "auxiliary", None, "按降水/排水需要配", AI, "ai_estimate", "LOW", None),
    ("泥浆泵", "auxiliary", None, "1 台/台钻机", AI, "ai_estimate", "LOW", None),
    ("电动多级离心清水泵", "auxiliary", None, "按用水点配", AI, "ai_estimate", "LOW", None),
    ("电动单筒慢速卷扬机", "auxiliary", None, "1 台/吊装工位", AI, "ai_estimate", "LOW", None),
    ("混凝土振捣器", "auxiliary", None, "1 台/1~2 名混凝土工（原 76㎡/台班推导值不作容量依据）",
     AI, "ai_estimate", "LOW", None),
    # ---- D 运输型（5）：容量由运距/趟数决定，不能用面积算 ----
    ("自卸汽车", "transport", None, "按主控挖掘机产能配（v1 AI 比例 1:2，允许用户限额覆盖）",
     AI, "ai_estimate", "LOW", "运输能力=运距×趟数，面积法不适用"),
    ("载货汽车", "transport", None, "按需运输量配", AI, "ai_estimate", "LOW",
     "旧推导值 900㎡/台班（支挡土板行）语义不成立，未采用"),
    ("机动翻斗车", "transport", None, "按场内运距配", AI, "ai_estimate", "LOW", None),
    ("洒水车", "transport", None, "按需配", AI, "ai_estimate", "LOW", None),
    ("泥浆罐车", "transport", None, "按泥浆排放量配", AI, "ai_estimate", "LOW", None),
    # ---- S 场地型（3）：走 _site_equipment ----
    ("塔吊", "site", None, None, AI, "ai_estimate", "LOW", "服务整栋楼，不进施工段容量"),
    ("塔式起重机", "site", None, None, AI, "ai_estimate", "LOW", "与塔吊同物异名，保留兼容"),
    ("施工电梯", "site", None, None, AI, "ai_estimate", "LOW", "服务整栋楼，不进施工段容量"),
]


# ---------------------------------------------------------------- 资源机动性（方案 §4.2）
# 权威依据：`docs/资源与工期计算重构方案_v1.md` §3.2「固定型 / 移动型的判据」（:114-125）
#   判据（原文）：「里面干活 → 段就是它的岗位，一个资源只能在一个段 → 固定型，
#                   逐段取整相加；外面服务 → 沿面移动或从外部覆盖，一个资源能连续
#                   照顾多个段 → 移动型，汇总取整一次」
#   fixed （:121）：全部人工工种 + 履带式单斗液压挖掘机/履带式推土机/抓铲挖掘机/
#                   轮胎式装载机/拖式铲运机/静力压桩机/履带式柴油打桩机/振动沉拔桩机/
#                   长螺旋·回旋·冲击式钻机/履带式旋挖钻机/工程地质液压钻机/风动凿岩机/
#                   内燃·电动空气压缩机/电动修钎机/岩石切割机/电动夯实机/混凝土振捣器/
#                   交流弧焊机/剪板机/切割机
#   mobile（:122）：混凝土输送泵车/混凝土罐车/自卸汽车/载货汽车/洒水车/泥浆罐车/
#                   汽车式起重机/钢轮压路机/摊铺机
#   site  （:123 场地级，不进段容量）：塔吊/施工电梯
# 兜底（父代理 2026-09-21 裁决）：§4.2 **未列举**的机械一律 `fixed`
#   （保守：Σ⌈A_i/MWI⌉ ≥ ⌈ΣA_i/MWI⌉，逐段算不会少算），并逐行在 notes 注明。
MOBILITY_FALLBACK_NOTE = u"§4.2 未列，兜底 fixed"

#: §4.2 明确列举（含同物异名/复合名，逐个在注释里写明对应关系）→ 判定为「有依据」
MOBILITY_DOCUMENTED = frozenset([
    # ---- :121 固定型 ----
    u"履带式推土机", u"电动夯实机", u"履带式单斗液压挖掘机", u"抓铲挖掘机",
    u"轮胎式装载机", u"拖式铲运机",
    u"静力压桩机", u"履带式柴油打桩机", u"振动沉拔桩机",
    u"长螺旋钻机",                       # 原文「长螺旋/回旋/冲击式钻机」
    u"回旋钻机",                         # 同上
    u"履带式旋挖钻机", u"工程地质液压钻机", u"风动凿岩机",
    u"岩石切割机",                       # 原文「岩石切割机」「切割机」
    u"内切割机",                         # 原文「切割机」（末三字）
    u"交流弧焊机", u"剪板机", u"电动修钎机",
    u"内燃空气压缩机", u"电动空气压缩机",  # 原文「内燃/电动空气压缩机」
    u"混凝土振捣器",
    # ---- :122 移动型 ----
    u"混凝土输送泵车",
    u"钢轮内燃压路机", u"钢轮振动压路机",  # 原文「钢轮压路机」
    u"汽车式起重机", u"自卸汽车", u"载货汽车", u"洒水车", u"泥浆罐车",
    # ---- :123 场地级 ----
    u"塔吊", u"塔式起重机",               # 同物异名
    u"施工电梯",
])

#: §4.2 未列举 → 兜底 fixed（逐行列入 `docs/资源分型_待审表.md`）
MOBILITY_UNDOCUMENTED = frozenset([
    u"冲击式打桩机", u"吊锤打桩机", u"螺旋钻机", u"双液压注浆泵", u"电动灌浆机",
    u"履带式起重机", u"卷扬机架(单笼5t内)", u"履带式单头岩石破碎机", u"灰浆搅拌机",
    u"铁驳船", u"管子切断机", u"潜水泵", u"泥浆泵", u"电动多级离心清水泵",
    u"电动单筒慢速卷扬机", u"机动翻斗车",
])

#: 机动性取值（人工一律 fixed —— §4.2:121「全部人工工种」）
MOBILITY = {}
for _n in MOBILITY_DOCUMENTED:
    MOBILITY[_n] = "site" if _n in (u"塔吊", u"塔式起重机", u"施工电梯") else (
        "mobile" if _n in (u"混凝土输送泵车", u"钢轮内燃压路机", u"钢轮振动压路机",
                           u"汽车式起重机", u"自卸汽车", u"载货汽车", u"洒水车",
                           u"泥浆罐车") else "fixed")
for _n in MOBILITY_UNDOCUMENTED:
    MOBILITY[_n] = "fixed"


def mobility_of(kind, name):
    """返回 `(mobility, note_suffix)`。labor 一律 fixed；machine 查表，兜底 fixed。"""
    if kind == "labor":
        return "fixed", None
    mv = MOBILITY.get(name)
    if mv is None:
        return "fixed", MOBILITY_FALLBACK_NOTE
    if name in MOBILITY_UNDOCUMENTED:
        return mv, MOBILITY_FALLBACK_NOTE
    return mv, None


def build_rows():
    rows, n = [], 0
    for name, mwi, tier, why in LABOR:
        n += 1
        mv, mv_note = mobility_of("labor", name)
        rows.append({
            "resource_id": "RWI_L_%03d" % n, "resource_kind": "labor",
            "resource_name": name, "capacity_mode": "area",
            "mwi": float(mwi), "mwi_unit": "m2/人", "resource_mobility": mv,
            "companion_ratio": None,
            "basis_formula": "AI 直接确定（工位密度档 %s）；%s" % (tier, why),
            "source_code": AI, "source_type": "ai_estimate", "confidence": "LOW",
            "review_state": "needs_review",
            "notes": "MWI=一个工人正常作业所需的最小工位面积；规范定额不含此量，故直接确定"
                     + (("；" + mv_note) if mv_note else ""),
        })
    for i, (name, mode, mwi, formula, src, stype, conf, note) in enumerate(MACHINE, 1):
        unit = "m2/台" if mode == "area" else None
        if mode == "area" and not formula:
            formula = "AI 直接确定"
        mv, mv_note = mobility_of("machine", name)
        rows.append({
            "resource_id": "RWI_M_%03d" % i, "resource_kind": "machine",
            "resource_name": name, "capacity_mode": mode,
            "mwi": float(mwi) if mwi is not None else None,
            "mwi_unit": unit, "resource_mobility": mv,
            "companion_ratio": formula if mode in ("auxiliary", "transport") else None,
            "basis_formula": formula or ("规则：1 台/施工段" if mode == "position" else None),
            "source_code": src, "source_type": stype, "confidence": conf,
            "review_state": "needs_review",
            "notes": (note + "；" + mv_note) if (note and mv_note)
                     else (mv_note or note) or mv_note,
        })
    return rows


def ensure_table(con, apply_):
    """建表；表已存在但缺 `resource_mobility` 列时补列（幂等）。"""
    cur = con.cursor()
    if not apply_:
        return False, "dry-run：不建表"
    cur.execute(DDL)
    have = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % TABLE)]
    if "resource_mobility" not in have:
        # SQLite 的 ADD COLUMN 允许带 CHECK（列级约束）；万一不支持则退化为无约束补列。
        try:
            cur.execute("ALTER TABLE %s ADD COLUMN resource_mobility TEXT "
                        "CHECK (resource_mobility IN ('fixed','mobile','site'))" % TABLE)
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE %s ADD COLUMN resource_mobility TEXT" % TABLE)
        con.commit()
        return True, "已确保表存在；补加列 resource_mobility"
    return True, "已确保表存在"


def write(con, rows):
    cur = con.cursor()
    cur.execute("DELETE FROM %s" % TABLE)
    cols = ("resource_id", "resource_kind", "resource_name", "capacity_mode", "mwi",
            "mwi_unit", "resource_mobility", "companion_ratio", "basis_formula",
            "source_code", "source_type",
            "confidence", "review_state", "notes", "created_at", "updated_at")
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (TABLE, ",".join(cols), ",".join("?" * len(cols)))
    for r in rows:
        # 注意：只对 created_at / updated_at 补默认时间戳。
        # 旧的 `r.get(c) or CREATED` 会把 None 的 mwi / mwi_unit 也写成时间戳字符串，
        # 导致 verify() 报「非 area 型误带 MWI 40」（2026-09-21 修复）。
        vals = [CREATED if (r.get(c) is None and c in ("created_at", "updated_at"))
                else r.get(c) for c in cols]
        cur.execute(sql, vals)
    con.commit()
    return len(rows)


def dump(rows, path):
    mode_cn = {"area": "A 面积型", "position": "B 工位型", "auxiliary": "C 配套型",
               "transport": "D 运输型", "site": "S 场地型"}
    buf = ["# Resource_Workface_Index 灌数预览\n",
           "> 共 %d 行；生成 %s\n" % (len(rows), CREATED)]
    for kind in ("labor", "machine"):
        sub = [r for r in rows if r["resource_kind"] == kind]
        buf.append("\n## %s（%d 行）\n" % ("人工工种" if kind == "labor" else "机械", len(sub)))
        buf.append("| id | 名称 | 类别 | MWI | 单位 | 机动性 | 配套比例 | 依据 | 来源 | 置信 |")
        buf.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in sub:
            buf.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                r["resource_id"], r["resource_name"],
                mode_cn.get(r["capacity_mode"], r["capacity_mode"]),
                "%.0f" % r["mwi"] if r["mwi"] else "—",
                r["mwi_unit"] or "—", r.get("resource_mobility") or "—",
                r["companion_ratio"] or "—",
                (r["basis_formula"] or "—").replace("|", "/"),
                r["source_code"], r["confidence"]))
    buf.append("\n## 分类计数\n")
    for m, cn in mode_cn.items():
        buf.append("- %s：%d 行" % (cn, len([r for r in rows if r["capacity_mode"] == m])))
    buf.append("")
    buf.append("## 机动性计数（方案 §4.2）")
    buf.append("")
    for m, cn in (("fixed", "固定型 fixed（逐段取整后相加）"),
                  ("mobile", "移动型 mobile（汇总后取整一次）"),
                  ("site", "场地级 site（不进段容量，走 _site_equipment）")):
        buf.append("- %s：%d 行" % (cn, len([r for r in rows
                                             if r.get("resource_mobility") == m])))
    buf.append("- 其中 §4.2 未列举、按兜底规则判为 fixed：%d 行" % len(
        [r for r in rows if MOBILITY_FALLBACK_NOTE in (r.get("notes") or "")]))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    return path


def verify(con):
    cur = con.cursor()
    # 先自证：MOBILITY 必须覆盖 MACHINE 全表（防名字写错导致静默走兜底）
    unknown = [m[0] for m in MACHINE if m[0] not in MOBILITY]
    if unknown:
        print("FAIL MOBILITY 未覆盖机械：%s" % unknown)
        return 1
    try:
        n = cur.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    except sqlite3.OperationalError as e:
        print("FAIL 表不存在或不可读：%s" % e)
        return 1
    bad = cur.execute("SELECT COUNT(*) FROM %s WHERE capacity_mode='area' AND (mwi IS NULL"
                      " OR mwi<=0)" % TABLE).fetchone()[0]
    bad2 = cur.execute("SELECT COUNT(*) FROM %s WHERE capacity_mode<>'area' AND mwi IS NOT NULL"
                       % TABLE).fetchone()[0]
    # ---- 追加-1：resource_mobility 必须三型合法且非空（component_ratio 的 MWI 完整性校验要求）----
    if "resource_mobility" not in [r[1] for r in cur.execute("PRAGMA table_info(%s)" % TABLE)]:
        print("FAIL 缺列 resource_mobility")
        return 1
    bad3 = cur.execute("SELECT COUNT(*) FROM %s WHERE resource_mobility IS NULL "
                       "OR resource_mobility NOT IN ('fixed','mobile','site')"
                       % TABLE).fetchone()[0]
    dist = dict(cur.execute("SELECT resource_mobility, COUNT(*) FROM %s "
                            "GROUP BY 1 ORDER BY 2 DESC" % TABLE))
    fallback = cur.execute("SELECT COUNT(*) FROM %s WHERE notes LIKE '%%%s%%'"
                           % (TABLE, MOBILITY_FALLBACK_NOTE)).fetchone()[0]
    ai = cur.execute("SELECT COUNT(*) FROM %s WHERE source_code='%s'" % (TABLE, AI)).fetchone()[0]
    print("行数 %d；area 型缺 MWI %d；非 area 型误带 MWI %d；mobility 非法/为空 %d"
          % (n, bad, bad2, bad3))
    print("机动性分布 %s；兜底 fixed %d 行；标 AI 估算 %d" % (dist, fallback, ai))
    ok = (n == len(build_rows()) and bad == 0 and bad2 == 0 and bad3 == 0)
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="kb.db 路径（必填，无默认，防误写真库）")
    ap.add_argument("--apply", action="store_true", help="真正写入（先备份）")
    ap.add_argument("--verify", action="store_true", help="只读校验")
    a = ap.parse_args()
    if not os.path.isfile(a.db):
        print("找不到库：%s" % a.db)
        return 2
    rows = build_rows()
    out = dump(rows, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "backend", "_probe_tmp", "phase1_mwi_table.md"))
    con = sqlite3.connect(a.db)
    if a.verify:
        rc = verify(con)
        con.close()
        return rc
    ok, msg = ensure_table(con, a.apply)
    if a.apply:
        bak = "%s.bak_%s_pre_mwi" % (a.db, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        if not os.path.exists(bak):
            import shutil
            shutil.copy2(a.db, bak)
            print("已备份 → %s" % bak)
        n = write(con, rows)
        print("已写入 %d 行（%s）" % (n, msg))
    else:
        print("dry-run：不写库。%s" % msg)
    print("预览表 → %s" % out)
    con.close()
    print("分类计数：%s" % ", ".join(
        "%s=%d" % (m, len([r for r in rows if r["capacity_mode"] == m]))
        for m in ("area", "position", "auxiliary", "transport", "site")))
    print("总计 %d 行" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
