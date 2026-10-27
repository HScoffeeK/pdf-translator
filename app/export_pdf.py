"""把译文写回 PDF,生成中文版:擦除英文原文,写入中文,保留版式装饰。

单栏文档策略:
1. 记录原页矢量图形(标题栏/边框/色块/分隔线)与图片;
2. 整页文本区盖白(消除原文);
3. 重绘步骤 1 的图形与图片;
4. 译文按"容器盒"(原版白底大矩形)分组,盒内串行排版;
5. 手动折行:保留显式换行,公式优先在运算符处断开,长串逐字符兜底。

双栏文档策略:按行搜索盖白 + 逐段原位写入(保住左右栏结构)。
"""
from __future__ import annotations

import functools
from pathlib import Path
import re

import pymupdf

CJK_FONT = "china-s"
LAYOUT_VERSION = "bar-white-10"
MAX_SIZE = 10.5
MIN_SIZE = 5.0
GAP = 3.0
LINE_LH = 1.5

_SYMBOL_FONT = next((p for p in (
    Path("C:/Windows/Fonts/seguisym.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Apple Symbols.ttf"),
) if p.exists()), None)
# 数学符号全集:黑体缺 −(U+2212)∩∪∈⊆ 等字形,缺一个就静默丢字符
_SYMBOL_CHARS = "¬Ø∧∨→↔∀∃∑∫≤≥≠×÷−∩∪∈∉∋⊆⊂⊇⊃∖∅⊕⊗⊖∓≈≡∼∝∞√∂∇"
_SYMBOL_RUN = re.compile("([" + _SYMBOL_CHARS + "]+)")
_SYMBOL_FONT_OBJ = pymupdf.Font(fontfile=str(_SYMBOL_FONT)) if _SYMBOL_FONT else None
# 拉丁字符(字母/数字/半角标点)单独用内置 Helvetica:china-s 把 ASCII 渲染成
# 全宽,一个 "P1" 占两个汉字宽度,间距过大;helv 为比例宽度且度量精确。
_LATIN_FONT_OBJ = pymupdf.Font("helv")
# 一行文本按 三类游程 切分:逻辑符号 | 可打印 ASCII | 其余(汉字/全角标点)
_RUN_SPLIT = re.compile(
    "([" + _SYMBOL_CHARS + r"]+|[\x20-\x7E]+|[^" + _SYMBOL_CHARS + r"\x20-\x7E]+)"
)


def _line_width(text: str, fontsize: float) -> float:
    """按游程类别实算行宽(与 _insert_line 的写入推进完全一致)。"""
    x = 0.0
    for part in _RUN_SPLIT.split(text):
        if not part:
            continue
        if _SYMBOL_FONT_OBJ and _SYMBOL_RUN.fullmatch(part):
            x += _SYMBOL_FONT_OBJ.text_length(part, fontsize=fontsize)
        elif part.isascii():
            x += _LATIN_FONT_OBJ.text_length(part, fontsize=fontsize)
        else:
            x += len(part) * fontsize
    return x


def _insert_line(page: pymupdf.Page, point: tuple[float, float], text: str,
                 fontsize: float, color: tuple = (0.1, 0.1, 0.1)) -> None:
    x, y = point
    for part in _RUN_SPLIT.split(text):
        if not part:
            continue
        if _SYMBOL_FONT_OBJ and _SYMBOL_RUN.fullmatch(part):
            page.insert_text((x, y), part, fontsize=fontsize, fontname="pdt-symbols",
                             fontfile=str(_SYMBOL_FONT), color=color)
            x += _SYMBOL_FONT_OBJ.text_length(part, fontsize=fontsize)
        elif part.isascii():
            page.insert_text((x, y), part, fontsize=fontsize, fontname="helv",
                             color=color)
            x += _LATIN_FONT_OBJ.text_length(part, fontsize=fontsize)
        else:
            page.insert_text((x, y), part, fontsize=fontsize, fontname=CJK_FONT,
                             color=color)
            # china-s 的汉字/全角字符恰为一个字宽,Font.text_length 不适用。
            x += len(part) * fontsize


FOOTER_RE = re.compile(
    r"^\s*(第\s*\d+\s*/\s*\d+\s*页|page\s+\d+(\s+of\s+\d+)?)\s*$", re.IGNORECASE
)
LIST_RE = re.compile(r"^\s*(?:[a-z]\)|\([a-z]\)|[ivxl]+\.|\d+[\.\)])\s+", re.IGNORECASE)


def _find_bars(page: pymupdf.Page, w: float, h: float) -> list[tuple[pymupdf.Rect, tuple]]:
    """识别"白字色条":原文里白色文字所在的彩色小色块(如 Learning outcomes 红条、
    Exercise N 蓝标签)。导出时其上的译文要用白字,色条需拉伸到覆盖中文行宽。"""
    white_centers: list[pymupdf.Point] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            for s in line["spans"]:
                if s["color"] == 0xFFFFFF and s["text"].strip():
                    r = pymupdf.Rect(s["bbox"])
                    white_centers.append(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    bars: list[tuple[pymupdf.Rect, tuple]] = []
    for d in page.get_drawings():
        f = d.get("fill")
        r = d["rect"]
        if not f or f == (1, 1, 1):
            continue
        if not (30 < r.width < 0.5 * w and 6 < r.height < 40
                and r.get_area() < 0.05 * w * h):
            continue
        if any(r.x0 <= c.x <= r.x1 and r.y0 <= c.y <= r.y1 for c in white_centers):
            bars.append((pymupdf.Rect(r), f))
    return bars


def _stretch_bars(
    bars: list[tuple[pymupdf.Rect, tuple]],
    writes: list[tuple[float, float, str, float]],
) -> tuple[list[tuple[pymupdf.Rect, tuple]], list[bool]]:
    """按写入计划拉伸色条并标记需白字的行。

    writes: [(x, 基线y, 文本, 字号)];行盒宽按 _line_width 实算(与写入推进一致)。
    行中心落在色条纵向范围内且与色条横向相交 → 该行白字,色条扩到覆盖整行。
    """
    boxes = [
        pymupdf.Rect(x - 1, y - fs, x - 1 + max(2.0, _line_width(t, fs)) + 1, y + 0.3 * fs)
        for x, y, t, fs in writes
    ]
    out: list[tuple[pymupdf.Rect, tuple]] = []
    white = [False] * len(writes)
    for bar, fill in bars:
        br = pymupdf.Rect(bar)
        for i, box in enumerate(boxes):
            cy = (box.y0 + box.y1) / 2
            if not (br.y0 - 2 <= cy <= br.y1 + 2):
                continue
            if not (box.x0 < br.x1 - 2 and box.x1 > br.x0 + 2):
                continue
            white[i] = True
            br.x0 = min(br.x0, box.x0 - 4)
            br.x1 = max(br.x1, box.x1 + 4)
            br.y0 = min(br.y0, box.y0)
            br.y1 = max(br.y1, box.y1 + 1)
        out.append((br, fill))
    return out, white


def _column_box(p: dict, meta: dict) -> tuple[float, float]:
    w = meta["w"]
    if p["wide"] or meta["ncols"] == 1:
        return p["left"] * w, max(p["right"] * w, 0.96 * w)
    if p["col"] == 1:
        return 0.53 * w, 0.96 * w
    return 0.04 * w, 0.48 * w


@functools.lru_cache(maxsize=4096)
def _source_lines(source: str) -> tuple[str, ...]:
    """6 词滑动窗口,用于页面搜索定位(段落定位与盖白共用,记忆化)。"""
    words = source.split()
    if not words:
        return (source,) if source else ()
    out: list[str] = []
    for i in range(0, max(1, len(words) - 5), 3):
        out.append(" ".join(words[i:i + 6]))
    if len(words) > 6:
        out.append(" ".join(words[-6:]))
    return tuple(out)


def _search_cached(
    page: pymupdf.Page, line: str, cache: dict[str, list[pymupdf.Rect]],
) -> list[pymupdf.Rect]:
    """同页同字符串的 search_for 结果缓存:模板句/重复行避免重复全文扫描。"""
    hits = cache.get(line)
    if hits is None:
        hits = page.search_for(line)
        cache[line] = hits
    return hits


def _para_regions(
    page: pymupdf.Page, p: dict, w: float, h: float,
    claimed: list[pymupdf.Rect] | None = None,
    search_cache: dict[str, list[pymupdf.Rect]] | None = None,
    ncols: int = 2,
) -> list[pymupdf.Rect]:
    """段落在本页的占位:搜索命中按栏聚类,裁剪到段落记录范围。

    ncols<=1(单栏页)时搜索范围必须用全宽:并排元素(色条标签 + 右侧正文)
    的文字中心落在页面右半,按左半栏裁剪会把大部分命中裁掉,只剩碎片。
    """
    claimed = claimed or []
    cache = search_cache if search_cache is not None else {}
    if p["wide"] or ncols <= 1:
        cx0, cx1 = 0.02 * w, 0.98 * w
    elif p["col"] == 1:
        cx0, cx1 = 0.50 * w, 0.98 * w
    else:
        cx0, cx1 = 0.02 * w, 0.50 * w
    cy0 = p["top"] * h - 4
    cy1 = max(p["bottom"] * h, p["top"] * h + 12) + 4

    hits: list[pymupdf.Rect] = []
    for line in _source_lines(p["source"]):
        for r in _search_cached(page, line, cache):
            mx, my = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            if not (cx0 <= mx <= cx1 and cy0 <= my <= cy1):
                continue
            if any(r.intersects(c) for c in claimed):
                continue
            if any(r.intersects(c) for c in hits):
                continue
            hits.append(r)
    if not hits:
        x0, x1 = p["left"] * w, p["right"] * w
        x1 = max(x0 + 12.0, x1)
        y0 = p["top"] * h
        y1 = max(p["bottom"] * h, y0 + 12)
        return [pymupdf.Rect(x0, y0, x1, y1)]
    # 命中区域并入段落自身记录的几何盒:行聚类段落(如表头跨 3 个碎片)的
    # 搜索窗口可能只命中第一块碎片,记录盒才是段落真实footprint。
    rec = pymupdf.Rect(p["left"] * w, p["top"] * h,
                       p["right"] * w, max(p["bottom"] * h, p["top"] * h + 12))
    hit_union = pymupdf.Rect(min(r.x0 for r in hits), min(r.y0 for r in hits),
                             max(r.x1 for r in hits), max(r.y1 for r in hits))
    if rec.intersects(hit_union):
        hit_union |= rec
    if ncols <= 1:
        # 单栏页:一个段落一个区域(命中并集 ∪ 记录盒),原位排版。
        return [hit_union]
    mid = w / 2
    cols: dict[int, list[pymupdf.Rect]] = {0: [], 1: []}
    for r in hits:
        cols[0 if r.x0 < mid else 1].append(r)
    regions = []
    for c, rs in cols.items():
        if not rs:
            continue
        x0 = min(r.x0 for r in rs) - 1
        x1 = max(r.x1 for r in rs) + 1
        y0 = min(r.y0 for r in rs) - 1
        y1 = max(r.y1 for r in rs) + 1
        regions.append(pymupdf.Rect(x0, y0, x1, y1))
    regions.sort(key=lambda r: r.get_area(), reverse=True)
    return regions


# ---- 折行引擎 ----

# 公式、路径和中英混排文本常常没有可供浏览器/insert_textbox 使用的空格。
# 在这些位置优先断行，若单个 token 仍超过盒宽再逐字符兜底。
_BREAK_AFTER = frozenset(" \t=→↔∨∧+-*/^×÷|,:;，、；：")
_ITEM_RE = re.compile(r"(?<!\w)([a-z]\))\s*")
_MATH_RE = re.compile(r"[¬∧∨→↔∀∃∑∫=≤≥]")


def _formula_only(text: str) -> bool:
    return bool(_MATH_RE.search(text)) and not re.search(r"[A-Za-z]{4,}", text)


def _layout_text(p: dict) -> str:
    text = p["source"] if _formula_only(p["source"]) else p["translation"]
    if "Ø" not in p["source"] and text.count("Ø") == p["source"].count("¬"):
        text = text.replace("Ø", "¬")
    # 抽取阶段会把同字号的纵向公式列表合为一段，项目边界仍由 a) ... b) 保留。
    labels = list(_ITEM_RE.finditer(p["source"]))
    if len(labels) > 1 and _MATH_RE.search(p["source"]):
        translated = list(_ITEM_RE.finditer(text))
        if [m.group(1).lower() for m in labels] == [m.group(1).lower() for m in translated]:
            for match in reversed(translated[1:]):
                text = text[:match.start()].rstrip() + "\n" + text[match.start():]
    return text


def _is_cjk(c: str) -> bool:
    cp = ord(c)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
    )


def _manual_wrap(
    text: str, fontsize: float, max_width: float,
) -> list[str]:
    """按显式换行和可读断点折行,宽度用 _line_width 实算(与渲染一致)。"""
    if not text:
        return []
    width = max(float(max_width), float(fontsize))
    out: list[str] = []

    for segment in text.split("\n"):
        if not segment:
            out.append("")
            continue
        cur: list[str] = []
        last_break = -1
        for ch in segment:
            cur.append(ch)
            if ch in _BREAK_AFTER or _is_cjk(ch):
                last_break = len(cur)
            while _line_width("".join(cur), fontsize) > width and len(cur) > 1:
                cut = last_break if 0 < last_break < len(cur) else len(cur) - 1
                line = "".join(cur[:cut]).rstrip()
                if not line:
                    cut = len(cur) - 1
                    line = "".join(cur[:cut])
                out.append(line)
                cur = cur[cut:]
                while cur and cur[0].isspace():
                    cur.pop(0)
                last_break = -1
                for i, prev in enumerate(cur):
                    if prev in _BREAK_AFTER or _is_cjk(prev):
                        last_break = i + 1
        if cur:
            out.append("".join(cur).rstrip())

    return out


def _fit_wrapped_lines(
    text: str, box: pymupdf.Rect, fontsize: float = MAX_SIZE,
    min_size: float = MIN_SIZE,
) -> tuple[float, list[str]]:
    """返回在盒内可写入的字号和行列表，测量与实际写入共用。"""
    if box.width <= 4.0 or box.height <= 4.0:
        raise ValueError("译文排版区域过小，无法安全写入")
    available_w = (box.width - 4.0) * 0.94
    available_h = box.height - 4.0
    fs = fontsize
    lines = _manual_wrap(text, fs, available_w)
    while fs > min_size and len(lines) * fs * LINE_LH > available_h:
        fs = max(min_size, fs - 0.5)
        lines = _manual_wrap(text, fs, available_w)
    if len(lines) * fs * LINE_LH > available_h or fs > available_w:
        raise ValueError("译文换行后仍超出可用区域，无法在不重叠的前提下写入")
    return fs, lines


# ---- 导出主体 ----

def overlay_pdf(
    src_path: str | Path,
    paras: list[dict],
    pages_meta: list[dict],
    out_path: str | Path,
) -> dict:
    meta_by_page = {m["page"]: m for m in pages_meta}
    by_page: dict[int, list[dict]] = {}
    for p in paras:
        if p.get("status") == "ok" and p.get("translation"):
            by_page.setdefault(p["page"], []).append(p)

    doc = pymupdf.open(src_path)
    written = 0
    try:
        for pno, plist in sorted(by_page.items()):
            if pno > len(doc):
                continue
            page = doc[pno - 1]
            meta = meta_by_page.get(pno, {"w": page.rect.width, "h": page.rect.height,
                                          "ncols": 1})
            w, h = meta["w"], meta["h"]
            plist = list(plist)
            known = re.sub(r"\s+", "", " ".join(p["source"] for p in plist))
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not _formula_only(text) or re.sub(r"\s+", "", text) in known:
                        continue
                    x0, y0, x1, y1 = line["bbox"]
                    if not (h * 0.05 < y0 < h * 0.93):
                        continue
                    plist.append(dict(seq=-len(plist)-1, page=pno, source=text,
                                      translation=text, status="ok", wide=meta["ncols"] == 1,
                                      col=int(x0 > w * 0.5 and meta["ncols"] > 1),
                                      left=x0 / w, right=x1 / w, top=y0 / h, bottom=y1 / h))

            search_cache: dict[str, list[pymupdf.Rect]] = {}
            # 0) 判定页面模式:有真右栏→双栏贴位,否则单栏重排
            has_right = any(
                p["col"] == 1 and not p["wide"] and any(
                    m["ncols"] >= 2 for m in pages_meta if m["page"] == p["page"]
                )
                for p in plist
            )

            if not has_right:
                _export_single_col(page, plist, meta, w, h, search_cache)
            else:
                _export_double_col(page, plist, meta, by_page, w, h, search_cache)
            written += len(plist)

        n_pages = len(by_page)
        doc.save(str(out_path), garbage=3, deflate=True)
    finally:
        doc.close()
    return {"written": written, "pages": n_pages}


def _export_single_col(
    page: pymupdf.Page,
    plist: list[dict],
    meta: dict,
    w: float,
    h: float,
    search_cache: dict[str, list[pymupdf.Rect]],
) -> None:
    # 每段译文只做一次布局预处理(列表项还原/公式符号回替),供 measure/layout 复用
    layout_of = {p["seq"]: _layout_text(p) for p in plist}

    # 1) 记录原页装饰 + 找容器 + 识别白字色条(必须在盖白/重绘之前,基于原始 drawings)
    decorations, text_cover = _record_decorations(page, w, h)
    bars = _find_bars(page, w, h)
    containers = _find_containers(page, w, h, text_cover)

    # 2) 整页文本区盖白
    page.draw_rect(
        pymupdf.Rect(0.03 * w, 0.05 * h, 0.97 * w, 0.95 * h),
        color=None, fill=(1, 1, 1),
    )

    # 3) 按原序重绘装饰(蓝底→白面→描边层叠保持,文字稍后写在最上层)
    _restore_decorations(page, decorations)

    # 5) 按容器分组段落
    para_regions: dict[int, list[pymupdf.Rect]] = {}
    claimed: list[pymupdf.Rect] = []
    narrow = [p for p in plist if not p["wide"]]
    wide = [p for p in plist if p["wide"]]
    for p in narrow + wide:
        regions = _para_regions(page, p, w, h, claimed, search_cache,
                                meta.get("ncols", 1))
        para_regions[p["seq"]] = regions
        claimed.extend(regions)

    # 6) 段落分配到容器。
    #    页码/页脚段写回原位;段落优先分给**垂直有交集**且水平包含的容器;
    #    与所有容器都无交集的段(容器之间的过渡段)自成"虚拟容器"原位排版,
    #    避免被硬塞进远处容器导致压缩变形。
    footers: list[dict] = []
    assigned: dict[int, list[dict]] = {cid: [] for cid in range(len(containers))}
    virtual: list[dict] = []  # 容器外段,原位排版
    for p in sorted(plist, key=lambda p: para_regions[p["seq"]][0].y0):
        main = para_regions[p["seq"]][0]
        my = (main.y0 + main.y1) / 2
        is_footer = (
            main.y0 > h * 0.90
            and len(p["translation"]) <= 16
            and FOOTER_RE.match(p["translation"].strip())
        )
        if is_footer:
            footers.append(p)
            continue
        # 候选:水平包含 且 垂直有交集(段中心落在容器 y 范围 ±8pt 内)。
        # 容差过宽会把两个容器之间"贴边"的段落(如色条标签)吸进错误容器,
        # 串行排版后被写到远离其色条的位置。
        cands = [
            cid for cid in range(len(containers))
            if containers[cid].x0 <= main.x0 + 2 and main.x1 - 2 <= containers[cid].x1
            and containers[cid].y0 - 8 <= my <= containers[cid].y1 + 8
        ]
        if cands:
            best = min(cands, key=lambda cid: abs(
                (containers[cid].y0 + containers[cid].y1) / 2 - my))
            assigned[best].append(p)
        else:
            virtual.append(p)

    # 4) 构建完整写入计划(纯计算,不写页): 页脚原位;虚拟段与容器按字号适配排版。
    #    计划先行才能在重绘装饰时把白字色条拉伸到覆盖中文行(见 _stretch_bars)。
    writes: list[tuple[float, float, str, float]] = []

    # 页脚段写回原位(保持页脚外观;灰色小字,不参与色条/白字逻辑)
    for p in footers:
        main = para_regions[p["seq"]][0]
        page.insert_text(
            ((main.x0 + main.x1) / 2 - 18, main.y1 - 1),
            p["translation"],
            fontsize=8.5, fontname=CJK_FONT, color=(0.3, 0.3, 0.3),
        )

    # 虚拟容器段:以段落主区域为盒原位排版(允许向下扩展到下一虚拟段/容器顶)。
    vp_sorted = sorted(virtual, key=lambda p: para_regions[p["seq"]][0].y0)
    for vi, p in enumerate(vp_sorted):
        main = para_regions[p["seq"]][0]
        vy1 = h * 0.93
        # 下边界:下一个**真正位于下方**的虚拟段或容器的顶部。
        # 同排并排的虚拟段(色条标签 + 右侧条目)互相不构成边界,
        # 否则两者的可用高度都被压到近零,字号一路缩到下限。
        if vi + 1 < len(vp_sorted):
            nxt = para_regions[vp_sorted[vi + 1]["seq"]][0]
            if nxt.y0 > main.y0 + 2:
                vy1 = min(vy1, nxt.y0 - GAP)
        for c in containers:
            if c.x0 <= main.x0 + 2 and main.x1 - 2 <= c.x1 and c.y0 > main.y0:
                vy1 = min(vy1, c.y0 - GAP)
        fs = 10.0
        # 宽度放宽到页面文本区(原文块窄时中文长句装不下)
        wrap_w = min(max(main.x1 - main.x0, 60), w * 0.9 - main.x0)
        text = layout_of[p["seq"]]
        lines = _manual_wrap(text, fs, wrap_w * 0.94)
        while fs > 6.0:
            if main.y0 + len(lines) * fs * LINE_LH <= vy1:
                break
            fs -= 0.5
            lines = _manual_wrap(text, fs, wrap_w * 0.94)
        cursor = main.y0 + 1.5  # 盒顶微留白,避免与上方内容贴死
        for line in lines:
            writes.append((main.x0, cursor + fs, line, fs))
            cursor += fs * LINE_LH

    # 5) 每个容器内串行排版。
    #    两阶段:先纯计算试出能装下的字号(不写页),再按计划写入——
    #    直接边写边降字号会把失败尝试的行也留在页面上,造成叠字。
    #    列表段(a) b) i. 1.)统一缩进:同容器内取列表段原文 x0 的最小值为基准,
    #    消除英文右对齐标签造成的参差。
    def _cont_indents(cid: int, cparas: list[dict]) -> dict[int, float]:
        """容器内每段的缩进:列表段统一,普通段保持原文相对位置(栅格化)。"""
        bx0 = bx0_cache[cid]
        list_base = None
        for p in cparas:
            if LIST_RE.match(p["translation"]) or LIST_RE.match(p["source"]):
                x0 = para_regions[p["seq"]][0].x0
                if list_base is None or x0 < list_base:
                    list_base = x0
        out: dict[int, float] = {}
        for p in cparas:
            main = para_regions[p["seq"]][0]
            is_list = bool(LIST_RE.match(p["translation"]) or LIST_RE.match(p["source"]))
            base = list_base if (is_list and list_base is not None) else main.x0
            # 缩进上限 24pt:并排元素(色条右侧正文)的原 x0 会算出荒谬大缩进,
            # 挤压换行宽度迫使字号缩小;真正的列表缩进一般不超过两格。
            out[p["seq"]] = round(max(0.0, min(base - bx0 - 2, 24.0)) / 12.0) * 12.0
        return out

    def measure_container(cid: int, cparas: list[dict], fontsize: float) -> float | None:
        """返回该字号下容器需要的总高度;装得下返回高度,装不下返回 None。"""
        total = 0.0
        indents = _cont_indents(cid, cparas)
        for p in cparas:
            indent = indents[p["seq"]]
            lines = _manual_wrap(layout_of[p["seq"]], fontsize, (bx1_cache[cid] - bx0_cache[cid] - indent) * 0.94)
            total += len(lines) * fontsize * LINE_LH + fontsize * 0.5
        return total if total <= (cont_cache[cid].y1 - cont_cache[cid].y0 - 6) else None

    def plan_container(cid: int, fontsize: float) -> list[tuple[float, float, str, float]]:
        """容器排版计划:与实际写入共用同一份行列表(字号/折行完全一致)。"""
        cparas = assigned[cid]
        bx0, bx1 = bx0_cache[cid], bx1_cache[cid]
        indents = _cont_indents(cid, cparas)
        cursor = cont_cache[cid].y0 + 3
        out: list[tuple[float, float, str, float]] = []
        for p in cparas:
            indent = indents[p["seq"]]
            lines = _manual_wrap(layout_of[p["seq"]], fontsize, (bx1 - bx0 - indent) * 0.94)
            # 缩进作用于整段所有行:原版的缩进是"块缩进"(引用块/列表项续行都与
            # 首行对齐),只缩首行会让续行跑到容器最左,破坏层级。
            for line in lines:
                out.append((bx0 + indent, cursor + fontsize, line, fontsize))
                cursor += fontsize * LINE_LH
            cursor += fontsize * 0.5  # 段间距
        return out

    # 容器几何缓存(measure 与 plan 共用)
    cont_cache = {cid: c for cid, c in enumerate(containers)}
    bx0_cache = {cid: c.x0 + 4 for cid, c in enumerate(containers)}
    bx1_cache = {cid: c.x1 - 4 for cid, c in enumerate(containers)}

    for cid in range(len(containers)):
        cparas = assigned[cid]
        if not cparas:
            continue
        fs = 10.0
        while fs > 5.0 and measure_container(cid, cparas, fs) is None:
            fs -= 0.5
        writes.extend(plan_container(cid, fs))

    # 6) 色条拉伸与白字标记 → 重绘装饰 → 补画拉伸色条 → 按计划写入
    stretched_bars, white_flags = _stretch_bars(bars, writes)
    _restore_decorations(page, decorations)
    for br, fill in stretched_bars:
        page.draw_rect(br, color=None, fill=fill)
    for (x, y, line, fs), wf in zip(writes, white_flags):
        _insert_line(page, (x, y), line, fs, color=(1, 1, 1) if wf else (0.1, 0.1, 0.1))


def _export_double_col(
    page: pymupdf.Page,
    plist: list[dict],
    meta: dict,
    by_page: dict[int, list[dict]],
    w: float,
    h: float,
    search_cache: dict[str, list[pymupdf.Rect]],
) -> None:
    """双栏:按原文行搜索盖白 + 逐段贴原位写入。"""
    pno = plist[0]["page"] if plist else 0

    # 搜索原文行盖白
    para_regions: dict[int, list[pymupdf.Rect]] = {}
    claimed: list[pymupdf.Rect] = []
    narrow = [p for p in plist if not p["wide"]]
    wide = [p for p in plist if p["wide"]]
    for p in narrow + wide:
        regions = _para_regions(page, p, w, h, claimed, search_cache,
                                meta.get("ncols", 1))
        para_regions[p["seq"]] = regions
        claimed.extend(regions)

    # 盖白只需覆盖本页及上一页段落(段落合并最多跨到 page+1);
    # 之前对全部页的段落都搜索,既慢又会把别页模板句误盖到本页。
    coverable = [p for p in (by_page.get(pno, []) + by_page.get(pno - 1, []))
                 if p.get("status") == "ok" and p.get("translation")]
    covered: list[pymupdf.Rect] = []
    for p in coverable:
        for line in _source_lines(p["source"]):
            for r in _search_cached(page, line, search_cache):
                if any(r.intersects(c) for c in covered):
                    continue
                covered.append(r)
                page.draw_rect(
                    pymupdf.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1),
                    color=None, fill=(1, 1, 1),
                )

    layout_of = {p["seq"]: _layout_text(p) for p in plist}

    def measure_page(fs: float) -> list[tuple[dict, pymupdf.Rect]] | None:
        placed: list[tuple[dict, pymupdf.Rect]] = []
        for p in sorted(plist, key=lambda p: (para_regions[p["seq"]][0].y0, p["seq"])):
            main = para_regions[p["seq"]][0]
            col_x0, col_x1 = _column_box(p, meta)
            x0 = col_x0 if p["wide"] else max(main.x0, col_x0)
            x1 = min(col_x1, w * 0.97)
            y0 = main.y0
            for _, prev in placed:
                if prev.x0 < x1 and prev.x1 > x0:
                    y0 = max(y0, prev.y1 + GAP)
            lines = _manual_wrap(layout_of[p["seq"]], fs, (x1 - x0 - 4) * 0.94)
            box = pymupdf.Rect(x0, y0, x1, y0 + len(lines) * fs * LINE_LH + 4.1)
            if box.y1 > h * 0.93:
                return None
            placed.append((p, box))
        return placed

    fs = MAX_SIZE
    placed = measure_page(fs)
    while placed is None and fs > MIN_SIZE:
        fs = max(MIN_SIZE, fs - 0.5)
        placed = measure_page(fs)
    if placed is None:
        raise ValueError("双栏译文换行后超过页面可用高度")
    writes = []
    for p, box in placed:
        fs2, lines = _fit_wrapped_lines(layout_of[p["seq"]], box, fontsize=fs, min_size=fs)
        cursor = box.y0 + 2.0
        for line in lines:
            writes.append((box.x0 + 2.0, cursor + fs2, line, fs2))
            cursor += fs2 * LINE_LH
    # 色条上的译文写白字,色条拉伸到覆盖中文行(双栏无盖白,色条在原页上)
    stretched_bars, white_flags = _stretch_bars(_find_bars(page, w, h), writes)
    for br, fill in stretched_bars:
        page.draw_rect(br, color=None, fill=fill)
    for (x, y, line, fs2), wf in zip(writes, white_flags):
        _insert_line(page, (x, y), line, fs2, color=(1, 1, 1) if wf else (0.1, 0.1, 0.1))


def _record_decorations(
    page: pymupdf.Page, w: float, h: float,
) -> tuple[list[tuple], pymupdf.Rect]:
    """记录原页装饰元素,返回(装饰列表, 文本区矩形)。"""
    text_rects: list[pymupdf.Rect] = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 0:
            text_rects.append(pymupdf.Rect(b["bbox"]))

    # 文本区:包含 80%+ 文本块的矩形
    if text_rects:
        tx0 = min(r.x0 for r in text_rects)
        tx1 = max(r.x1 for r in text_rects)
        ty0 = min(r.y0 for r in text_rects)
        ty1 = max(r.y1 for r in text_rects)
    else:
        tx0, ty0, tx1, ty1 = 0.03 * w, 0.05 * h, 0.97 * w, 0.95 * h
    text_area = pymupdf.Rect(tx0 - 2, ty0 - 2, tx1 + 2, ty1 + 2)

    deco: list[tuple] = []
    for d in page.get_drawings():
        r = d["rect"]
        # 记录**全部**图形,按原顺序恢复:
        # 蓝底白面的内容区在原 PDF 里是"蓝块→白面"两层,只记蓝块会丢失白面,
        # 重绘后蓝块直接压住文字区。恢复原始层叠顺序才是正确的做法。
        deco.append(("drawing", d))

    # 记录图片(置于最前,保持其在内容流中的早期位置)
    images = [("image", item) for item in page.get_image_info()]
    return images + deco, text_area


def _restore_decorations(page: pymupdf.Page, deco: list[tuple]) -> None:
    """按原始顺序重绘装饰元素(在盖白后)。

    必须保持原 PDF 的绘制顺序(蓝底→白面→描边),否则底色块会盖住文字。
    """
    for kind, data in deco:
        if kind == "drawing":
            d = data
            fill = d.get("fill")
            color = d.get("color")
            width = d.get("width")
            if fill or color:
                page.draw_rect(
                    d["rect"],
                    color=color,
                    fill=fill,
                    width=width if color else None,
                )
        elif kind == "image":
            try:
                page.insert_image(pymupdf.Rect(data["bbox"]))
            except Exception:
                pass


def _find_containers(
    page: pymupdf.Page, w: float, h: float, text_area: pymupdf.Rect,
) -> list[pymupdf.Rect]:
    """在原版页面上找'白底大矩形':这些是文本容器,译文在容器内排版。

    排除接近整页文本区的大矩形:调用方先盖了整页白矩形,若不排除,
    它会成为第一个容器把所有段落吸进去,从页顶一路写下去。
    """
    containers = []
    for d in page.get_drawings():
        r = d["rect"]
        fill = d.get("fill")
        if fill == (1, 1, 1) and r.get_area() > 5000 and r.width > 100:
            # 接近整页文本区的白矩形是盖底层,不是内容容器
            if (r.x0 <= text_area.x0 + 5 and r.y0 <= text_area.y0 + 5
                    and r.x1 >= text_area.x1 - 5 and r.y1 >= text_area.y1 - 5):
                continue
            if not any(r in cnt for cnt in containers):
                containers.append(r)
    # 找不到纯白容器时**不做**整页回退:整页大容器会把所有段落吸进去
    # 从页顶串行重排,位置全错(第4页标签/条目错位的根因)。
    # 空容器列表 = 全部段落走虚拟路径,按各自区域原位排版。
    containers.sort(key=lambda r: r.y0)
    return containers