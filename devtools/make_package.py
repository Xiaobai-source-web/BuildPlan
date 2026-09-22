# -*- coding: utf-8 -*-
"""C3：把交付包打成 zip（排除版本库、缓存、运行产物与临时目录）。

用法（第 33 轮起脚本在 `devtools/` 下，仍从仓库根推算，不受影响）：
    python devtools/make_package.py            # 打包到 交付包/ 下
    python devtools/make_package.py --dry-run  # 只列出会打进包的文件，不写盘

排除原则：能由代码/数据再生的一律不进包（版本库、缓存、运行产物、临时目录）。
"""
import argparse
import re
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ══════════════════════════════════════════════════════════════════════════════
# 交付包**根目录白名单**（用户 2026-09-21 裁定）
# ------------------------------------------------------------
# 用户原话口径：「肯定要踢出去，就保留最新的一份说明文档和 readme」。
# 所以交付包根目录只放：README + **最新一份**《技术说明文档》 + 启动器 + .gitignore。
# 其余根目录文件（内部诊断 / 汇报 / 修改清单 / 录屏稿 / 提词卡 / 旧版说明文档…）
# **留在仓库里**（用户还要继续更新它们），但**不进交付包**。
# ⚠️ 每跑一次都会把"因白名单被挡掉的根文件"逐条打印出来（不静默），
#    免得以后新增的根文件被无声丢掉。
# ══════════════════════════════════════════════════════════════════════════════
ROOT_KEEP = {
    ".gitignore",
    "README.md",
    "一键测试.py",      # 启动器：跑一次就会生成 输出结果\计划_<id>\
    "一键测试.bat",
    # ---- 产品向说明文档（用户 2026-09-21 22:46~22:47 全面更新后随包交付）----
    # 上一版（v3.2）这三份也更新了，但被根目录白名单整体挡在包外，
    # 用户解包只看到 README + 一份**过期**的《技术说明文档》。
    "建策BuildPlan_产品说明文档.md",
    "建策BuildPlan_产品说明文档_补充附录.md",
    "建策BuildPlan_运行指南.md",
}
#: 《技术说明文档》只保留**内容最新**的那一份。
# ---------------------------------------------------------------------------
# ⚠️ 第 44 轮补（用户 2026-09-21 实测踩到的坑）：**绝不能按文件名里的版本号挑**。
# 实测仓库里同时存在：
#   · `建策BuildPlan_技术说明文档_v3.0.docx` —— 1,362,373 字节 / mtime 09-21 22:51 /
#     文档内 `cp:revision`=16、942 段、2 张图，正文写「27 节点流水线 + 3002 条测试」
#     ← **内容最新**，但版本号低；
#   · `建策BuildPlan_技术说明文档_v3.1.docx` —— 53,468 字节 / mtime 09-20 17:45 /
#     revision=1、389 段、0 图，正文还写着「26 节点流水线」← **旧的**，但版本号高。
# 原来按版本号元组取最大 → 选中 v3.1（旧的），于是 v3.2 包里装的是过期说明文档，
# 用户刚更新的大文档 v3.0.docx 反而被白名单挡在包外 —— 这就是"包内文档是旧的"根因。
# 现在改为按 **文档内部修订号 → 文档内部修改时间 → 文件 mtime** 排序取最新，
# 完全不看文件名版本号，并把"选中 / 被挡掉"逐条打印，绝不静默。
_SPEC_DOC_RE = re.compile(r"^建策BuildPlan_技术说明文档_v(\d+(?:\.\d+)*)\.docx$")


def _docx_revision(path):
    """读 docx 的 `docProps/core.xml`，返回 `(修订号, 内部修改时间)`；读不到返回 `(0, "")`。

    docx 就是 zip；`cp:revision` 是 Word 每次保存自增的修订号，比文件名和 mtime 都可信。
    """
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("docProps/core.xml").decode("utf-8", "ignore")
    except Exception:
        return 0, ""
    rev = 0
    m = re.search(r"<cp:revision>\s*(\d+)\s*</cp:revision>", xml)
    if m:
        rev = int(m.group(1))
    mod = ""
    m = re.search(r"<dcterms:modified[^>]*>\s*([^<]+?)\s*</dcterms:modified>", xml)
    if m:
        mod = m.group(1)
    return rev, mod


def latest_spec_doc(verbose=True):
    """挑**内容最新**的《技术说明文档》（判据见上面的 ⚠️ 注释，不看文件名版本号）。"""
    cands = []
    for p in ROOT.glob("建策BuildPlan_技术说明文档_v*.docx"):
        if not _SPEC_DOC_RE.match(p.name):
            continue
        rev, mod = _docx_revision(p)
        cands.append((p, rev, mod, p.stat().st_mtime))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[1], c[2], c[3]), reverse=True)
    best = cands[0][0]
    if verbose:
        print("[说明文档] 候选 %d 份，按「内部修订号 → 内部修改时间 → 文件 mtime」排序："
              % len(cands))
        for p, rev, mod, mt in cands:
            print("   %s %-44s rev=%-4s mod=%-22s mtime=%s"
                  % ("✅ 进包" if p.name == best.name else "   挡掉",
                     p.name, rev or "-", mod or "-",
                     datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")))
    return best.name


ROOT_KEEP.add(latest_spec_doc() or "")

# 目录名（任一层命中即整棵排除）
EXCLUDE_DIRS = {
    ".git", ".pytest_cache", "__pycache__", ".mypy_cache", ".idea", ".vscode",
    "输出结果", "_probe_tmp", "_measure_tmp", "_test_tmp", "_tmp_run", "_smoke_tmp",
    "_plan_archive", "交付包", "node_modules", ".venv", "venv",
    # ⚠️ 第 7 批补：`_src_snapshots` 是**内部回滚快照**（每批改动前对未跟踪的核心模块做
    # 一次转储）。它里面**还各带一份完整的 `kb.db`**（实测 5.7 MB）—— 这正是本脚本
    # `kb.db.bak_*` 那条规则要防的"另一个知识库"混淆 + 白占体积。与 `_probe_tmp`
    # 同类，属于开发期产物，不进交付包。
    "_src_snapshots",
    # 空目录（实测 0 文件），显式挡掉以防将来被写入
    "_qa_plan_run_1789895021",
    # ⚠️ 第 7 批（用户 2026-09-21 裁定）：`docs/` 与 `devtools/_dev-notes/` 是**对内资料**
    # （修改契约 / 修改项总清单 / 交接总纲 / 各批任务书 / 残留死代码清单 / 送审表 /
    #  WS1–WS8 开发记录 / 工作状态 / 父代理验收记录 …），**不进交付包**。
    # 文件原地保留在仓库里（用户还要继续编辑），只是不打包。
    # 影响面（实测，务必知悉）：踢掉 `docs/` 后，**从解压目录跑 pytest 会有 2 条失败** ——
    #   `tests/test_workface_segments.py` 硬读 `docs/修改契约_v1.md`；
    #   `tests/test_algorithm_parity.py` 会把 `docs/` 当成扫描根之一（容错，不致命）。
    # 产品入口不受影响：`一键测试.py` 只装依赖 / 起后端 / 开终端，**不读 docs/**。
    "docs",
    "_dev-notes",
    # ⚠️ 第 7 批补：`_doc_sync_<日期>` 是**开发期工作目录**（实测含 `fix_kb_readme.py` /
    # `kb_stats.py` / `_tmp_*.py` / `_tmp_doc_v30_full.txt` 等 24 个文件）。
    # 它会随日期改名，所以除了点名，再加一条前缀规则（见 `EXCLUDE_DIR_PREFIXES`）。
    "_doc_sync_20260921",
    "_backup", "_backup_旧稿",            # 文档改稿备份（二进制大文件，不进交付包）
    "plans", "deliverables",          # 运行产物（backend/plans、backend/deliverables、terminal/plans）
}
# 文件名模式
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp", ".bak")
EXCLUDE_PREFIXES = ("~$",)
EXCLUDE_NAMES = {"_t.docx", ".DS_Store", "Thumbs.db"}
# 文件名里**含有**这些片段就排除（后缀规则抓不到 `kb.db.bak_20260918_184619` 这种）
# ⚠️ 第 33 轮补：`kb.db.bak_*` 是改库前的数据库快照（每个约 3.7 MB）。早先只靠
# `_backup` 目录名排除，抓不到这种**文件**，于是交付包里塞了两个历史库副本 ——
# 占体积、还容易被误当成"另一个知识库"。这里用片段规则挡掉。
EXCLUDE_CONTAINS = (".bak_", ".bak.", "~$", "_backup_", "pytest-cache-files-")

#: **目录名前缀**排除（比 `EXCLUDE_DIRS` 的点名更耐改名）。
#  `_doc_sync_<日期>` 这类开发期工作目录会随日期改名，点名只能挡住今天这一个。
EXCLUDE_DIR_PREFIXES = ("_doc_sync",)

# ⚠️ 密钥类：**绝不能进交付包**。.gitignore 只挡 git，挡不住打包脚本。
SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".keystore")
SECRET_KEEP = {".env.example"}          # 模板没有密钥，要保留
# 多套模型配置（第 35 轮）：存的是**明文 key**。它既不是 .env 也不是密钥后缀，
# 上面两条规则都抓不到 —— 实测差点把用户真 key 打进交付包，这里显式点名。
SECRET_NAMES = {"llm_profiles.json", "bp_profiles_smoke.json"}


def is_secret(name: str) -> bool:
    if name in SECRET_KEEP:
        return False
    if name in SECRET_NAMES:
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith(SECRET_SUFFIXES):
        return True
    return False


def should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS:
            return True
        # 目录名前缀规则（`_doc_sync_<日期>` 这类随日期改名的开发期目录）
        if part.startswith(EXCLUDE_DIR_PREFIXES):
            return True
    name = rel.name
    if rel.parts[0] in EXCLUDE_DIRS or rel.parts[0].startswith(EXCLUDE_DIR_PREFIXES):
        return True
    if name in EXCLUDE_NAMES:
        return True
    if is_secret(name):
        return True
    if name.startswith(EXCLUDE_PREFIXES):
        return True
    if any(s in name for s in EXCLUDE_CONTAINS):
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    # ---- 根目录白名单（见文件头的 ROOT_KEEP 说明）----
    # 只在**仓库根**这一层生效，子目录（backend/ docs/ devtools/ …）完全不受影响。
    if len(rel.parts) == 1 and name not in ROOT_KEEP:
        return True
    return False


def collect():
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if should_skip(p):
            continue
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--name", default="建策BuildPlan_一键测试包_v3.0.zip")
    args = ap.parse_args()

    files = collect()
    total = sum(f.stat().st_size for f in files)

    # 硬保险：密钥类文件一旦进了列表就直接中止（宁可打不出包，也不许泄露密钥）
    leaked = [f.relative_to(ROOT).as_posix() for f in files if is_secret(f.name)]
    if leaked:
        raise SystemExit("❌ 检测到密钥类文件将被打包，已中止：%s" % leaked)

    print("将打包 %d 个文件，原始体积 %.1f MB" % (len(files), total / 1024 / 1024))
    print("已排除密钥文件（.env 等）；保留 .env.example 模板")
    print("-" * 70)
    # 按顶层目录归类显示，便于核对
    buckets = {}
    for f in files:
        rel = f.relative_to(ROOT)
        top = rel.parts[0] if len(rel.parts) > 1 else "(根目录文件)"
        b = buckets.setdefault(top, [0, 0])
        b[0] += 1
        b[1] += f.stat().st_size
    for top in sorted(buckets, key=lambda k: -buckets[k][1]):
        n, sz = buckets[top]
        print("  %-28s %4d 个  %8.1f KB" % (top, n, sz / 1024))
    print("-" * 70)
    print("被排除的顶层目录：%s" % ", ".join(sorted(
        d for d in EXCLUDE_DIRS if (ROOT / d).exists())))
    print("注意：输出结果/ 与 plans/ 是运行产物，包内不含（用户跑一次即生成）。")
    # ---- 根目录白名单的可见留痕（不静默）----
    dropped_root = []
    for p in sorted(ROOT.iterdir()):
        if not p.is_file():
            continue
        n = p.name
        if n in ROOT_KEEP or is_secret(n) or n.startswith(EXCLUDE_PREFIXES):
            continue
        if any(s in n for s in EXCLUDE_CONTAINS) or n.endswith(EXCLUDE_SUFFIXES):
            continue
        dropped_root.append(n)
    print("-" * 70)
    print("根目录**进包**的文件（白名单）：%s" % sorted(n for n in ROOT_KEEP if n))
    print("根目录**挡在包外**的文件：%d 份（文件仍在仓库里，可继续编辑）" % len(dropped_root))
    for n in dropped_root:
        print("    - %s" % n)

    if args.dry_run:
        print()
        print("[dry-run] 未写盘。前 40 个文件：")
        for f in files[:40]:
            print("   %s" % f.relative_to(ROOT))
        return

    out_dir = ROOT / "交付包"
    out_dir.mkdir(exist_ok=True)
    zip_path = out_dir / args.name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
    print()
    print("已生成 -> %s（%.1f MB）"
          % (zip_path, zip_path.stat().st_size / 1024 / 1024))

    # 自检：逐条查"排除目录 / 备份等文件模式 / 密钥"，而不是只看目录名（上一版就漏了
    # `_smoke_tmp` 与 `kb.db.bak_*` —— 一个进了临时产物、一个把包撑大 3.8 MB）
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    problems = []
    for n in names:
        segs = n.split("/")
        fname = segs[-1]
        if any(s in EXCLUDE_DIRS for s in segs[:-1]):
            problems.append(("排除目录", n))
        if fname.endswith(EXCLUDE_SUFFIXES) or any(s in fname for s in EXCLUDE_CONTAINS):
            problems.append(("排除文件模式", n))
        if is_secret(fname):
            problems.append(("密钥", n))
    if problems:
        print("❌ 自检未通过：包内混入 %d 个不该有的文件（前 10）" % len(problems))
        for why, n in problems[:10]:
            print("   [%s] %s" % (why, n))
        raise SystemExit(1)
    print("✅ 自检通过：无排除目录 / 备份文件 / 密钥类混入（共 %d 个文件）" % len(names))
    print("包含文档：%s"
          % [n for n in names if n.endswith((".docx", ".md", ".html", ".png"))][:10])


if __name__ == "__main__":
    main()
