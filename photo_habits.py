#!/usr/bin/env python3
"""
Photo Habits — 递归扫描照片目录，从 EXIF 统计拍摄习惯。

依赖：Python 3.8+（只用标准库）和 exiftool
  macOS:   brew install exiftool
  Windows: 从 https://exiftool.org 下载，把 exiftool(-k).exe 改名为 exiftool.exe，
           放到 PATH 里或用 --exiftool 指定路径

用法：
  python3 photo_habits.py <照片根目录> [--out 输出目录]
  python3 photo_habits.py <照片根目录> --exclude-dir Export --exclude-dir 备份
  python3 photo_habits.py --from-cache photo_habits_output/exif_cache.json   # 不重扫，只重算报告

输出（默认 ./photo_habits_output/）：
  report.md        统计报告
  photos.csv       去重后每张照片一行，可带回做进一步分析（不含 GPS）
  exif_cache.json  exiftool 原始结果，供 --from-cache 重算

去重规则：
  - 同一张照片的 RAW + JPEG、多处备份只算一张（按 机身 + 拍摄时间 + 文件名 去重，优先保留 RAW）
  - Lightroom / Photoshop 等软件导出的 JPEG 默认排除（--include-exports 可保留）
  - 手机照片计入「机身」统计，但默认不进习惯分析（--include-phones 可保留）
  - Bridge / Lightroom 写在 .xmp 旁车文件里的星级会合并进来
"""

import argparse
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

RAW_EXTS = {
    "dng", "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "raf", "rw2",
    "orf", "ori", "pef", "srw", "x3f", "3fr", "iiq", "rwl", "erf", "mef", "mos",
}
RENDERED_EXTS = {"jpg", "jpeg", "heic", "heif", "hif", "tif", "tiff"}
IMAGE_EXTS = RAW_EXTS | RENDERED_EXTS

PHONE_MAKES = ("apple", "google", "samsung", "xiaomi", "huawei", "honor", "oppo",
               "vivo", "oneplus", "realme", "motorola", "nothing")
EDITOR_RE = re.compile(
    r"lightroom|photoshop|camera raw|capture one|luminar|snapseed|vsco|darktable|"
    r"dxo|affinity|gimp|pixelmator|acdsee|on1|meitu|美图|醒图|picsart", re.I)
JUNK_LENS = {"", "----", "unknown", "0", "n/a", "none", "unknown lens"}

# 数值型 tag 加 # 让 exiftool 输出原始数字
TAGS = [
    "-Make", "-Model", "-LensModel", "-LensID", "-Lens",
    "-FocalLength#", "-FocalLengthIn35mmFormat#", "-FocalLength35efl#", "-ScaleFactor35efl#",
    "-FNumber#", "-MaxApertureValue#", "-ExposureTime#", "-ISO#",
    "-ExposureProgram", "-ExposureCompensation#", "-MeteringMode", "-Flash#",
    "-WhiteBalance", "-DateTimeOriginal", "-SubSecTimeOriginal",
    "-ImageWidth#", "-ImageHeight#", "-Orientation#",
    "-Software", "-CreatorTool", "-Rating#", "-Label",
]
BATCH = 400


# ---------------------------------------------------------------- 扫描

def find_exiftool(explicit):
    if explicit:
        return explicit
    for name in ("exiftool", "exiftool.exe"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("找不到 exiftool。macOS: brew install exiftool；Windows: 见脚本开头说明，或用 --exiftool 指定路径")


def walk(root, exclude_dirs):
    images, sidecars = [], []
    excl = [e.lower() for e in exclude_dirs]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and not any(e in d.lower() for e in excl)]
        for fn in filenames:
            if fn.startswith("._"):
                continue
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext in IMAGE_EXTS:
                images.append(os.path.join(dirpath, fn))
            elif ext == "xmp":
                sidecars.append(os.path.join(dirpath, fn))
    return images, sidecars


def run_exiftool(exiftool, paths, tags):
    out = []
    for i in range(0, len(paths), BATCH):
        chunk = paths[i:i + BATCH]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
            f.write("\n".join(chunk))
            argfile = f.name
        try:
            cmd = [exiftool, "-charset", "filename=utf8", "-j", "-q", "-q",
                   "-api", "largefilesupport=1", *tags, "-@", argfile]
            r = subprocess.run(cmd, capture_output=True)
            text = r.stdout.decode("utf-8", errors="replace").strip()
            if text:
                try:
                    out.extend(json.loads(text))
                except json.JSONDecodeError:
                    print(f"  警告：第 {i}–{i + len(chunk)} 个文件的结果解析失败，已跳过", file=sys.stderr)
        finally:
            os.unlink(argfile)
        done = min(i + BATCH, len(paths))
        print(f"\r  已读取 {done}/{len(paths)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return out


def scan(root, exiftool, exclude_dirs):
    print(f"扫描目录：{root}", file=sys.stderr)
    images, sidecars = walk(root, exclude_dirs)
    print(f"找到 {len(images)} 个图片文件、{len(sidecars)} 个 .xmp 旁车文件", file=sys.stderr)
    data = run_exiftool(exiftool, images, TAGS)
    side = run_exiftool(exiftool, sidecars, ["-Rating#", "-Label"]) if sidecars else []
    return {"root": os.path.abspath(root), "images": data, "sidecars": side}


# ---------------------------------------------------------------- 解析

def g(d, key):
    """取 tag，兼容 exiftool 在 JSON 里把 # 写进键名的情况。"""
    v = d.get(key + "#", d.get(key))
    return v


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace("f/", "").replace("mm", "").replace("s", "").strip()
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b)
        return float(s.split()[0])
    except (ValueError, ZeroDivisionError, IndexError):
        return None


def text(v):
    return "" if v is None else str(v).strip()


def stem_key(path):
    d, fn = os.path.split(path)
    stem, ext = os.path.splitext(fn.lower())
    if ext == ".xmp":  # IMG_1.RW2.xmp 和 IMG_1.xmp 都归到 IMG_1
        inner, ext2 = os.path.splitext(stem)
        if ext2[1:] in IMAGE_EXTS:
            stem = inner
    return (d.lower(), stem)


def body_name(make, model):
    make, model = text(make), text(model)
    if not model:
        return make or "(未知机身)"
    first = make.split()[0].lower() if make else ""
    if first and model.lower().startswith(first):
        return model
    short = {"nikon corporation": "Nikon", "olympus imaging corp.": "Olympus",
             "olympus corporation": "Olympus", "om digital solutions": "OM System",
             "ricoh imaging company, ltd.": "Ricoh", "fujifilm": "Fujifilm",
             "panasonic": "Panasonic", "sony": "Sony", "canon": "Canon"}
    return f"{short.get(make.lower(), make)} {model}".strip()


def lens_name(d, body):
    for k in ("LensModel", "LensID", "Lens"):
        v = text(d.get(k))
        if v.lower() not in JUNK_LENS and not v.lower().startswith("unknown"):
            return v
    return f"{body} 内置/未知镜头"


def parse_time(s):
    s = text(s)
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def build_records(cache, include_exports, include_phones):
    ratings = {}
    for d in cache.get("sidecars", []):
        r = num(g(d, "Rating"))
        ratings[stem_key(d.get("SourceFile", ""))] = (r, text(d.get("Label")))

    stats = Counter()
    candidates = {}
    for d in cache.get("images", []):
        stats["文件总数"] += 1
        path = d.get("SourceFile", "")
        ext = path.rsplit(".", 1)[-1].lower()
        make, model = text(d.get("Make")), text(d.get("Model"))
        if not model:
            stats["无相机信息（截图、扫描件等），跳过"] += 1
            continue
        software = text(d.get("Software")) + " " + text(d.get("CreatorTool"))
        is_export = ext in RENDERED_EXTS and bool(EDITOR_RE.search(software))
        if is_export and not include_exports:
            stats["软件导出件，跳过"] += 1
            continue

        body = body_name(make, model)
        t = parse_time(d.get("DateTimeOriginal"))
        sk = stem_key(path)
        sidecar_rating, sidecar_label = ratings.get(sk, (None, ""))
        rating = num(g(d, "Rating"))
        if sidecar_rating is not None:
            rating = sidecar_rating if rating is None else max(rating, sidecar_rating)

        focal = num(g(d, "FocalLength"))
        eq = num(g(d, "FocalLengthIn35mmFormat")) or num(g(d, "FocalLength35efl"))
        scale = num(g(d, "ScaleFactor35efl"))
        if not eq and focal and scale:
            eq = focal * scale
        if not eq and focal:  # 与 exiftool 一致：无画幅信息时按全画幅处理
            eq, scale = focal, scale or 1.0
        if not scale and focal and eq:
            scale = eq / focal
        fnum = num(g(d, "FNumber"))
        w, h = num(g(d, "ImageWidth")), num(g(d, "ImageHeight"))
        orient = num(g(d, "Orientation"))
        portrait = None
        if w and h:
            portrait = (h > w) != (orient in (5, 6, 7, 8))
        flash = num(g(d, "Flash"))

        rec = {
            "path": path, "ext": ext, "raw": ext in RAW_EXTS, "body": body, "make": make,
            "phone": make.lower().startswith(PHONE_MAKES) or "iphone" in model.lower(),
            "lens": lens_name(d, body), "time": t,
            "focal": focal, "eq_focal": eq, "crop": scale,
            "fnum": fnum, "eq_fnum": fnum * scale if fnum and scale else fnum,
            "max_ap": num(g(d, "MaxApertureValue")),
            "shutter": num(g(d, "ExposureTime")), "iso": num(g(d, "ISO")),
            "program": text(d.get("ExposureProgram")),
            "ev": num(g(d, "ExposureCompensation")),
            "metering": text(d.get("MeteringMode")),
            "wb": text(d.get("WhiteBalance")),
            "flash": None if flash is None else bool(int(flash) & 1),
            "portrait": portrait, "rating": rating,
            "label": text(d.get("Label")) or sidecar_label, "export": is_export,
        }
        if t:
            key = (body, t.isoformat(), text(d.get("SubSecTimeOriginal")), sk[1])
        else:
            key = ("nodate",) + sk
        old = candidates.get(key)
        if old is None:
            candidates[key] = rec
        else:
            stats["重复（RAW+JPEG 或备份），合并"] += 1
            keep, drop = (rec, old) if rec["raw"] and not old["raw"] else (old, rec)
            if drop["rating"] is not None:
                keep["rating"] = drop["rating"] if keep["rating"] is None else max(keep["rating"], drop["rating"])
            candidates[key] = keep

    allrecs = list(candidates.values())
    habit = allrecs if include_phones else [r for r in allrecs if not r["phone"]]
    return allrecs, habit, stats


# ---------------------------------------------------------------- 报告工具

def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "—"


def bar(a, b, width=30):
    return "█" * int(round(a / b * width)) if b else ""


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join(out)


def bucket_table(title_col, recs, value_fn, buckets, total=None):
    """buckets: [(标签, 判定函数)]，按顺序归入第一个命中的桶。"""
    counts = Counter()
    have = 0
    for r in recs:
        v = value_fn(r)
        if v is None:
            continue
        have += 1
        for label, test in buckets:
            if test(v):
                counts[label] += 1
                break
    n = total or have
    rows = [(label, counts[label], pct(counts[label], n), bar(counts[label], n)) for label, _ in buckets]
    note = f"（有该数据的 {have} 张）" if have != len(recs) else ""
    return table([title_col, "张数", "占比", ""], rows) + (f"\n\n{note}" if note else "")


def rng(lo, hi):
    return lambda v: lo <= v < hi


def fmt_shutter(s):
    if s is None:
        return "—"
    return f"1/{round(1 / s)}" if s < 1 else f"{s:g}s"


def median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def fmt(v, digits=0):
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def is_wide_open(r, lens_min):
    if r["fnum"] is None:
        return None
    ref = r["max_ap"] or lens_min.get(r["lens"])
    if not ref:
        return None
    return r["fnum"] <= ref * 1.06


def rated(r):
    return r["rating"] is not None and r["rating"] >= 1


# ---------------------------------------------------------------- 报告各节

def section_overview(allrecs, habit, stats, root, include_phones):
    times = [r["time"] for r in allrecs if r["time"]]
    days = {t.date() for t in times}
    lines = ["## 概览", ""]
    rows = [(k, v) for k, v in stats.items()]
    rows += [("去重后照片", len(allrecs)),
             ("其中手机", sum(r["phone"] for r in allrecs)),
             ("进入习惯分析" + ("（含手机）" if include_phones else "（不含手机）"), len(habit)),
             ("拍摄天数", len(days))]
    if times:
        rows.append(("时间跨度", f"{min(times):%Y-%m-%d} 至 {max(times):%Y-%m-%d}"))
    nrated = sum(rated(r) for r in habit)
    rows.append(("有星级（≥1 星）", f"{nrated}（{pct(nrated, len(habit))}）"))
    lines.append(table(["项目", "数值"], rows))
    lines += ["", f"扫描目录：`{root}`", ""]
    if nrated < 30:
        lines += ["> 星级照片太少，下文「留片」相关的列参考价值有限。", ""]
    return lines


def section_bodies(allrecs):
    by = defaultdict(list)
    for r in allrecs:
        by[r["body"]].append(r)
    rows = []
    for body, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        ts = [r["time"] for r in rs if r["time"]]
        days = len({t.date() for t in ts})
        span = f"{min(ts):%Y-%m} 至 {max(ts):%Y-%m}" if ts else "—"
        rows.append((body, len(rs), pct(len(rs), len(allrecs)), days,
                     f"{len(rs) / days:.0f}" if days else "—", span,
                     f"{sum(r['raw'] for r in rs) / len(rs) * 100:.0f}%"))
    lines = ["## 机身", "", table(["机身", "张数", "占比", "拍摄天数", "张/天", "使用期间", "RAW 比例"], rows), ""]

    years = sorted({r["time"].year for r in allrecs if r["time"]})
    top = [b for b, _ in Counter(r["body"] for r in allrecs).most_common(8)]
    if years:
        rows = []
        for y in years:
            c = Counter(r["body"] for r in allrecs if r["time"] and r["time"].year == y)
            rows.append([y, sum(c.values())] + [c.get(b, "") for b in top])
        lines += ["### 每年各机身张数", "", table(["年份", "合计"] + top, rows), ""]
    return lines


def section_lenses(habit, lens_min):
    by = defaultdict(list)
    for r in habit:
        by[r["lens"]].append(r)
    rows = []
    for lens, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        ts = [r["time"] for r in rs if r["time"]]
        days = len({t.date() for t in ts})
        wo = [is_wide_open(r, lens_min) for r in rs]
        wo = [x for x in wo if x is not None]
        bodies = ", ".join(b for b, _ in Counter(r["body"] for r in rs).most_common(2))
        rows.append((lens, bodies, len(rs), pct(len(rs), len(habit)), days,
                     f"{min(ts):%Y-%m}–{max(ts):%Y-%m}" if ts else "—",
                     fmt(median(r["eq_focal"] for r in rs)),
                     fmt(median(r["fnum"] for r in rs), 1),
                     pct(sum(wo), len(wo)),
                     pct(sum(rated(r) for r in rs), len(rs))))
    return ["## 镜头", "",
            "「全开」= 用了该焦距下的最大光圈。「等效焦距」按 35mm 画幅换算。", "",
            table(["镜头", "机身", "张数", "占比", "使用天数", "使用期间", "中位等效焦距",
                   "中位光圈", "全开比例", "留片率(≥1星)"], rows), ""]


FOCAL_BUCKETS = [("< 20mm", rng(0, 20)), ("20–23mm", rng(20, 24)), ("24–27mm", rng(24, 28)),
                 ("28–34mm", rng(28, 35)), ("35–44mm", rng(35, 45)), ("45–59mm", rng(45, 60)),
                 ("60–69mm", rng(60, 70)), ("70–99mm", rng(70, 100)), ("100–134mm", rng(100, 135)),
                 ("135–199mm", rng(135, 200)), ("200–299mm", rng(200, 300)), ("≥ 300mm", rng(300, 1e9))]


def section_focal(habit):
    lines = ["## 等效焦距", "", bucket_table("等效焦距", habit, lambda r: r["eq_focal"], FOCAL_BUCKETS), ""]
    rs = [r for r in habit if rated(r)]
    if len(rs) >= 30:
        lines += ["### 留片（≥1 星）的等效焦距", "",
                  bucket_table("等效焦距", rs, lambda r: r["eq_focal"], FOCAL_BUCKETS), ""]

    by = defaultdict(list)
    for r in habit:
        if r["focal"]:
            by[r["lens"]].append(r)
    rows = []
    for lens, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        fs = [r["focal"] for r in rs]
        lo, hi = min(fs), max(fs)
        if len(rs) < 30 or hi / lo < 1.3:
            continue
        at_lo = sum(f <= lo * 1.05 for f in fs)
        at_hi = sum(f >= hi * 0.95 for f in fs)
        top = ", ".join(f"{f:g}mm({c})" for f, c in Counter(round(f) for f in fs).most_common(5))
        rows.append((lens, len(rs), f"{lo:g}–{hi:g}mm", pct(at_lo, len(rs)), pct(at_hi, len(rs)), top))
    if rows:
        lines += ["### 变焦镜头停在哪", "",
                  "广端/长端比例高，说明焦段经常不够用。焦距为实际焦距，非等效。", "",
                  table(["镜头", "张数", "实际用到的范围", "停在广端", "停在长端", "最常用焦距(张数)"], rows), ""]
    return lines


def section_aperture(habit):
    bk = [("≤ f/1.4", rng(0, 1.5)), ("f/1.5–1.9", rng(1.5, 2.0)), ("f/2–2.7", rng(2.0, 2.8)),
          ("f/2.8–3.9", rng(2.8, 4.0)), ("f/4–5.5", rng(4.0, 5.6)), ("f/5.6–7.9", rng(5.6, 8.0)),
          ("f/8–10", rng(8.0, 11.0)), ("≥ f/11", rng(11.0, 1e9))]
    return ["## 光圈", "",
            "### 实际光圈", "", bucket_table("光圈", habit, lambda r: r["fnum"], bk), "",
            "### 等效光圈（按画幅换算后的景深与总进光）", "",
            "APS-C f/1.2 约等于全画幅 f/1.8；X100 系列 f/2 约等于全画幅 f/2.8。", "",
            bucket_table("等效光圈", habit, lambda r: r["eq_fnum"], bk), ""]


def section_exposure(habit):
    iso_bk = [("≤ 200", rng(0, 201)), ("201–400", rng(201, 401)), ("401–800", rng(401, 801)),
              ("801–1600", rng(801, 1601)), ("1601–3200", rng(1601, 3201)),
              ("3201–6400", rng(3201, 6401)), ("6401–12800", rng(6401, 12801)), ("> 12800", rng(12801, 1e9))]
    sh_bk = [("≥ 1s", rng(1, 1e9)), ("1/2–1/30", rng(1 / 30 - 1e-6, 1)), ("1/31–1/60", rng(1 / 60 - 1e-6, 1 / 30 - 1e-6)),
             ("1/61–1/125", rng(1 / 125 - 1e-6, 1 / 60 - 1e-6)), ("1/126–1/250", rng(1 / 250 - 1e-6, 1 / 125 - 1e-6)),
             ("1/251–1/1000", rng(1 / 1000 - 1e-6, 1 / 250 - 1e-6)), ("快于 1/1000", rng(0, 1 / 1000 - 1e-6))]
    lines = ["## 曝光", "", "### ISO", "", bucket_table("ISO", habit, lambda r: r["iso"], iso_bk), "",
             "### 快门", "", bucket_table("快门", habit, lambda r: r["shutter"], sh_bk), ""]

    by = defaultdict(list)
    for r in habit:
        by[r["body"]].append(r)
    rows = []
    for body, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        isos = [r["iso"] for r in rs if r["iso"]]
        rows.append((body, len(rs), fmt(median(isos)), pct(sum(i >= 3200 for i in isos), len(isos)),
                     pct(sum(i >= 6400 for i in isos), len(isos)), fmt(max(isos) if isos else None),
                     fmt_shutter(median(r["shutter"] for r in rs))))
    lines += ["### 各机身的感光度", "",
              table(["机身", "张数", "中位 ISO", "ISO ≥ 3200", "ISO ≥ 6400", "最高 ISO", "中位快门"], rows), ""]

    def counter_rows(key, label_map=None):
        c = Counter(r[key] or "(无记录)" for r in habit)
        return [(label_map.get(k, k) if label_map else k, v, pct(v, len(habit))) for k, v in c.most_common(8)]

    lines += ["### 曝光模式", "", table(["模式", "张数", "占比"], counter_rows("program")), "",
              "### 测光", "", table(["测光", "张数", "占比"], counter_rows("metering")), "",
              "### 白平衡", "", table(["白平衡", "张数", "占比"], counter_rows("wb")), ""]

    ev_bk = [("≤ −1.3", rng(-1e9, -1.2)), ("−1 至 −0.3", rng(-1.2, -0.2)), ("0", rng(-0.2, 0.2)),
             ("+0.3 至 +1", rng(0.2, 1.2)), ("≥ +1.3", rng(1.2, 1e9))]
    flashes = [r["flash"] for r in habit if r["flash"] is not None]
    lines += ["### 曝光补偿", "", bucket_table("补偿", habit, lambda r: r["ev"], ev_bk), "",
              f"闪光灯触发：{sum(flashes)} 张，占有记录的 {pct(sum(flashes), len(flashes))}", ""]
    return lines


def section_time(habit):
    ts = sorted((r["time"], r["body"]) for r in habit if r["time"])
    if not ts:
        return []
    hours = Counter(t.hour for t, _ in ts)
    n = len(ts)
    rows = [(f"{h:02d}:00", hours[h], pct(hours[h], n), bar(hours[h], max(hours.values()), 25)) for h in range(24)]
    lines = ["## 拍摄时间", "", "### 一天中的时段", "", table(["时段", "张数", "占比", ""], rows), ""]

    wd = Counter(t.weekday() for t, _ in ts)
    names = "一二三四五六日"
    lines += ["### 星期", "", table(["星期", "张数", "占比"],
                                   [(f"周{names[i]}", wd[i], pct(wd[i], n)) for i in range(7)]), ""]
    mo = Counter(t.month for t, _ in ts)
    lines += ["### 月份（各年合计）", "", table(["月份", "张数", "占比"],
                                             [(f"{m} 月", mo[m], pct(mo[m], n)) for m in range(1, 13)]), ""]

    per_day = Counter(t.date() for t, _ in ts)
    vals = sorted(per_day.values())
    burst = sum(1 for (a, ba), (b, bb) in zip(ts, ts[1:]) if ba == bb and (b - a).total_seconds() <= 1)
    lines += ["### 拍摄节奏", "", table(["项目", "数值"], [
        ("拍摄天数", len(per_day)),
        ("每个拍摄日张数（中位）", fmt(statistics.median(vals))),
        ("每个拍摄日张数（90 分位）", vals[int(len(vals) * 0.9)] if vals else "—"),
        ("单日最多", f"{vals[-1]}（{max(per_day, key=per_day.get)}）"),
        ("与上一张间隔 ≤ 1 秒（连拍或补拍）", f"{burst}（{pct(burst, n)}）"),
    ]), ""]
    return lines


def section_orientation(habit):
    by = defaultdict(list)
    for r in habit:
        if r["portrait"] is not None:
            by[r["body"]].append(r["portrait"])
    rows = [(b, len(v), pct(sum(v), len(v))) for b, v in sorted(by.items(), key=lambda x: -len(x[1]))]
    return ["## 横竖构图", "", table(["机身", "有记录张数", "竖拍比例"], rows), ""] if rows else []


def light_vs_blur(rs):
    """开大光圈的照片里，光线充足（ISO ≤ 400）多半是为了虚化，否则多半是为了进光。"""
    blur = sum(1 for r in rs if r["iso"] is not None and r["iso"] <= 400)
    return blur, len(rs) - blur


def section_tele(habit):
    tele = [r for r in habit if r["eq_focal"] and r["eq_focal"] >= 70]
    lines = ["## 长焦专题（等效 ≥ 70mm）", ""]
    if not tele:
        return lines + ["没有等效 70mm 以上的照片。", ""]
    lines += [f"共 {len(tele)} 张，占习惯分析照片的 {pct(len(tele), len(habit))}。", "",
              bucket_table("等效焦距", tele, lambda r: r["eq_focal"], FOCAL_BUCKETS[7:]), ""]

    by = Counter(r["lens"] for r in tele)
    lines += ["### 用的镜头", "", table(["镜头", "张数", "占比"],
                                      [(l, c, pct(c, len(tele))) for l, c in by.most_common(8)]), ""]

    known = [r for r in tele if r["fnum"] and r["iso"]]
    already = [r for r in known if r["fnum"] >= 5.6 * 0.97]
    wider = [r for r in known if r["fnum"] < 5.6 * 0.97]
    sim = Counter()
    for r in wider:
        need = r["iso"] * (5.6 / r["fnum"]) ** 2
        if need <= 1600:
            sim["换 f/5.6 后 ISO ≤ 1600，无影响"] += 1
        elif need <= 6400:
            sim["ISO 1600–6400，可接受"] += 1
        elif need <= 12800:
            sim["ISO 6400–12800，画质明显下降"] += 1
        else:
            sim["ISO > 12800，基本废片或必须降快门"] += 1
    order = ["换 f/5.6 后 ISO ≤ 1600，无影响", "ISO 1600–6400，可接受",
             "ISO 6400–12800，画质明显下降", "ISO > 12800，基本废片或必须降快门"]
    rows = [("本来就在 f/5.6 或更小", len(already), pct(len(already), len(known)))]
    rows += [(k, sim[k], pct(sim[k], len(known))) for k in order]
    lines += ["### 如果这些长焦照片都只能用 f/5.6", "",
              "假设快门不变，按光圈差折算所需 ISO。对应「70-300 光圈够不够」的问题。", "",
              table(["情况", "张数", "占比"], rows), ""]

    blur, light = light_vs_blur(wider)
    lines += [f"光圈比 f/5.6 大的 {len(wider)} 张里：",
              f"- 光线充足（ISO ≤ 400）仍开大，多半为了虚化：{blur} 张（{pct(blur, len(wider))}）",
              f"- 光线不足，开大是为了进光：{light} 张（{pct(light, len(wider))}）", ""]

    rated_tele = [r for r in known if rated(r)]
    if len(rated_tele) >= 20:
        rw = [r for r in rated_tele if r["fnum"] < 5.6 * 0.97]
        hurt = sum(1 for r in rw if r["iso"] * (5.6 / r["fnum"]) ** 2 > 6400)
        lines += [f"只看留片（≥1 星）的 {len(rated_tele)} 张长焦：光圈比 f/5.6 大的 {len(rw)} 张，"
                  f"换 f/5.6 后 ISO 超过 6400 的 {hurt} 张（{pct(hurt, len(rated_tele))}）。", ""]
    return lines


def section_35(habit):
    rs = [r for r in habit if r["eq_focal"] and 28 <= r["eq_focal"] < 45]
    lines = ["## 35mm 附近专题（等效 28–44mm）", ""]
    if not rs:
        return lines + ["没有这段焦距的照片。", ""]
    lines += [f"共 {len(rs)} 张，占习惯分析照片的 {pct(len(rs), len(habit))}。", ""]
    by = defaultdict(list)
    for r in rs:
        by[r["lens"]].append(r)
    rows = []
    for lens, v in sorted(by.items(), key=lambda x: -len(x[1])):
        ef = [r["eq_fnum"] for r in v if r["eq_fnum"]]
        wide = [r for r in v if r["eq_fnum"] and r["eq_fnum"] < 2.8 * 0.97]
        blur, _ = light_vs_blur(wide)
        rows.append((lens, len(v), fmt(median(ef), 1), pct(len(wide), len(ef)),
                     pct(blur, len(wide)), pct(sum(rated(r) for r in v), len(v))))
    lines += ["X100 系列等效 f/2.8。下表看过去在这段焦距有多少照片用到了比 f/2.8 更大的等效光圈，"
              "以及其中有多少是光线充足时为了虚化而开大。", "",
              table(["镜头", "张数", "中位等效光圈", "等效 < f/2.8", "其中光线充足(为虚化)", "留片率"], rows), ""]
    return lines


# ---------------------------------------------------------------- 主流程

CSV_FIELDS = ["path", "body", "lens", "time", "focal", "eq_focal", "crop", "fnum", "eq_fnum", "max_ap",
              "shutter", "iso", "program", "ev", "metering", "wb", "flash", "portrait", "rating",
              "label", "raw", "phone", "export"]


def write_csv(path, recs):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(recs, key=lambda r: (r["time"] or datetime.min, r["path"])):
            row = dict(r)
            row["time"] = r["time"].isoformat(sep=" ") if r["time"] else ""
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="递归扫描照片目录，统计拍摄习惯")
    ap.add_argument("root", nargs="?", help="照片根目录")
    ap.add_argument("--out", default="photo_habits_output", help="输出目录")
    ap.add_argument("--exiftool", help="exiftool 可执行文件路径")
    ap.add_argument("--exclude-dir", action="append", default=[], help="跳过名字包含该字符串的目录，可重复")
    ap.add_argument("--include-exports", action="store_true", help="保留软件导出的 JPEG")
    ap.add_argument("--include-phones", action="store_true", help="手机照片也进入习惯分析")
    ap.add_argument("--from-cache", help="读取之前保存的 exif_cache.json，不重新扫描")
    a = ap.parse_args()

    if a.from_cache:
        with open(a.from_cache, encoding="utf-8") as f:
            cache = json.load(f)
    elif a.root:
        if not os.path.isdir(a.root):
            sys.exit(f"目录不存在：{a.root}")
        cache = scan(a.root, find_exiftool(a.exiftool), a.exclude_dir)
    else:
        ap.error("需要照片根目录，或 --from-cache")

    os.makedirs(a.out, exist_ok=True)
    if not a.from_cache:
        with open(os.path.join(a.out, "exif_cache.json"), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    allrecs, habit, stats = build_records(cache, a.include_exports, a.include_phones)
    if not allrecs:
        sys.exit("没有读到带相机信息的照片。")

    lens_min = {}
    for r in habit:
        if r["fnum"]:
            lens_min[r["lens"]] = min(lens_min.get(r["lens"], 99), r["fnum"])

    lines = ["# 拍摄习惯统计", "", f"生成时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    lines += section_overview(allrecs, habit, stats, cache.get("root", ""), a.include_phones)
    lines += section_bodies(allrecs)
    if habit:
        lines += section_lenses(habit, lens_min)
        lines += section_focal(habit)
        lines += section_aperture(habit)
        lines += section_exposure(habit)
        lines += section_time(habit)
        lines += section_orientation(habit)
        lines += section_tele(habit)
        lines += section_35(habit)

    report = os.path.join(a.out, "report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    write_csv(os.path.join(a.out, "photos.csv"), allrecs)
    print(f"完成：{len(allrecs)} 张（习惯分析 {len(habit)} 张）")
    print(f"报告：{report}")
    print(f"明细：{os.path.join(a.out, 'photos.csv')}")


if __name__ == "__main__":
    main()
