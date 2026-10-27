"""PDF 文本提取核心(行级):双栏识别、阅读顺序重排、段落合并、页眉页脚剔除。

为什么按"行"而不是按"块":PyMuPDF 的块会把左右栏同一水平线的文本合并成一个
跨栏块,块级信息不可靠;行级坐标是我们自己可控的最小单元。

流程:
1. 逐页取文本行(坐标、字号、粗体);
2. 剔除页眉页脚(顶部/底部区域 + 跨页重复 + 页码模式);
3. 判定全宽行(标题/摘要/跨栏图注),把连续全宽行聚成"全宽组"作为分带边界;
4. 检测中缝(分栏);带内排序:全宽行 → 左栏 → 右栏,栏内自上而下;
5. 行聚成块(同栏同带、垂直紧邻、样式一致),块再按续行规则合并成段落;
6. 公式块(符号占比过高)丢弃;可选:References 章节即停。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

TERMINAL_RE = re.compile(r"[.?!:;。?!…；]\s*$")
PAGENUM_RE = re.compile(r"^\s*(?:[ivxlcdm]{1,8}|\d{1,4})\s*$", re.IGNORECASE)
NEWPARA_RE = re.compile(
    r"^\s*(?:\(?\d{1,3}[\.\)]\s|"
    r"\(?(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)[\.\)]\s|"
    r"[A-Za-z]\d{0,3}:\s|"
    r"[•\-–*·]\s|"
    r"(?:fig(?:ure)?|table|algorithm|section|eq(?:uation)?)\s+\d+)",
    re.IGNORECASE,
)
REF_HEADING_RE = re.compile(
    r"^\s*(references|bibliography|文献|参考文献)\s*(?:[\s:：].*)?$", re.IGNORECASE
)

TOP_ZONE = 0.08     # 页面上部 8% 视为页眉区
BOTTOM_ZONE = 0.92  # 页面下部 8% 视为页脚区
WIDE_RATIO = 0.55   # 行宽超过页宽 55% 视为全宽行


@dataclass
class _Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool
    color: int = 0     # 首个非空 span 颜色;白字=色条标签(UI 元素),不与正文合并
    wide: bool = False
    col: int = 0      # 0=左/单栏, 1=右栏
    col_key: float = 0.0  # 排序用:全宽行 1.5,栏行 0/1
    band: int = 0     # 全宽组分带序号


@dataclass
class _Block:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool
    color: int
    col: int
    band: int
    wide: bool
    n_lines: int = 1


@dataclass
class Paragraph:
    seq: int = 0
    page: int = 0
    text: str = ""
    col: int = 0          # 0=左栏, 1=右栏
    wide: bool = False  # 全宽段落(标题/摘要)
    top: float = 0.0   # 段落顶部 / 页高(0~1,前端按比例定位实现左右对齐)
    bottom: float = 0.0   # 段落底部 / 页高
    left: float = 0.0     # 段落左边缘 / 页宽
    right: float = 0.0    # 段落右边缘 / 页宽
    ncols: int = 1        # 所在页栏数


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_wide(ln: _Line, w: float) -> bool:
    mid = w / 2
    # 跨中线的行只有在从左页边附近开始时才算全宽带(标题/摘要);
    # 从页面中部起步的并排元素(如左侧色条标签 + 右侧正文)不算,
    # 否则其续行会被分进另一个"带"而无法合并成段。
    return (ln.x1 - ln.x0) > WIDE_RATIO * w or (
        ln.x0 < 0.30 * w and ln.x0 < mid - 0.06 * w and ln.x1 > mid + 0.06 * w
    )


def _merge_fragments(frags: list[_Line], w: float) -> list[_Line]:
    """把同一文本行被大词距拆开的碎片合并(justified 排版常见)。

    同基线(y 重叠超过行高 60%)且水平间距 < 2.5 倍字号的相邻碎片合并;
    中缝(gutter)两侧的碎片永不合并——那是左右两栏,不是一行。
    阈值 2.5×:更宽的间隙(如"Learning outcomes"色条标签与右侧正文,~3.4×)
    是不同版式元素,合并会把白字标签和正文粘成一段,排版时无法分别处理。
    """
    if len(frags) < 2:
        return frags
    gutter = _detect_gutter(frags, w)
    frags.sort(key=lambda l: (l.y0, l.x0))
    out: list[_Line] = []
    for ln in frags:
        if out:
            prev = out[-1]
            v_overlap = min(prev.y1, ln.y1) - max(prev.y0, ln.y0)
            h_min = max(min(prev.y1 - prev.y0, ln.y1 - ln.y0), 1.0)
            same_baseline = v_overlap > 0.6 * h_min
            gap = ln.x0 - prev.x1
            max_size = max(prev.size, ln.size)
            crosses_gutter = (
                gutter is not None
                and prev.x1 <= gutter <= ln.x0
                and gap > 2 * max_size
            )
            if (same_baseline and 0 <= gap < 2.5 * max_size
                    and prev.color == ln.color and not crosses_gutter):
                prev.text = prev.text + " " + ln.text
                prev.x1 = ln.x1
                prev.y0 = min(prev.y0, ln.y0)
                prev.y1 = max(prev.y1, ln.y1)
                continue
        out.append(ln)
    return out


def _collect_pages(
    doc: pymupdf.Document,
) -> dict[int, tuple[float, float, list[_Line]]]:
    pages: dict[int, tuple[float, float, list[_Line]]] = {}
    for pno in range(len(doc)):
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        frags: list[_Line] = []
        seen: set[tuple] = set()
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = _clean("".join(s["text"] for s in line["spans"]))
                if not text:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                # 防御:PyMuPDF 偶尔会在重叠块中重复输出同一行
                key = (pno + 1, round(x0, 1), round(y0, 1),
                       round(x1, 1), round(y1, 1), text)
                if key in seen:
                    continue
                seen.add(key)
                size = sum(s["size"] for s in spans) / len(spans)
                bold = any(s["flags"] & 16 for s in spans)
                color = spans[0]["color"]
                frags.append(_Line(pno + 1, x0, y0, x1, y1, text, size, bold, color))
        lines = _merge_fragments(frags, w)
        for ln in lines:
            ln.wide = _is_wide(ln, w) or bool(REF_HEADING_RE.match(ln.text))
        pages[pno + 1] = (w, h, lines)
    return pages


def _drop_running_zones(
    pages: dict[int, tuple[float, float, list[_Line]]]
) -> None:
    """就地剔除页眉页脚:顶部/底部区域内,跨页重复或形如页码的行。"""
    top_cnt: Counter[str] = Counter()
    bottom_cnt: Counter[str] = Counter()
    for _, (w, h, lines) in pages.items():
        for ln in lines:
            if ln.y1 <= h * TOP_ZONE:
                top_cnt[ln.text.lower()] += 1
            elif ln.y0 >= h * BOTTOM_ZONE:
                bottom_cnt[ln.text.lower()] += 1
    multi_page = len(pages) >= 2

    for pno, (w, h, lines) in list(pages.items()):
        kept: list[_Line] = []
        for ln in lines:
            in_top = ln.y1 <= h * TOP_ZONE
            in_bottom = ln.y0 >= h * BOTTOM_ZONE
            if in_top or in_bottom:
                t = ln.text.lower()
                if multi_page and (
                    top_cnt[t] >= 3 or bottom_cnt[t] >= 3
                ):
                    continue
                if PAGENUM_RE.match(ln.text):
                    continue
            kept.append(ln)
        pages[pno] = (w, h, kept)


def _detect_gutter(lines: list[_Line], w: float) -> float | None:
    """在中缝附近找没有被非全宽行横穿的竖直空白带。

    右栏只需 1 行即认定双栏:自动分页常把右栏内容挤到只剩孤行,
    孤行按右栏处理(排在左栏之后)恰好是正确的阅读顺序。
    """
    body = [ln for ln in lines if not ln.wide]
    if not body:
        return None
    mid = w / 2
    margin = 0.015 * w
    min_col_w = 0.12 * w
    for g in (mid - 0.06 * w, mid, mid + 0.06 * w):
        crossing = [ln for ln in body if ln.x0 < g - margin and ln.x1 > g + margin]
        left = [ln for ln in body if ln.x1 <= g and (ln.x1 - ln.x0) > min_col_w]
        right = [ln for ln in body if ln.x0 >= g and (ln.x1 - ln.x0) > min_col_w]
        # 右栏行垂直稀疏(相邻行距 > 4×行高)不是真分栏:单栏文档右上角的
        # 孤立表头(如 "Practice work 1")会伪造出一条中缝,整页被套上双栏几何
        if len(right) >= 2:
            rights = sorted(right, key=lambda l: l.y0)
            heights = max(ln.y1 - ln.y0 for ln in rights)
            gaps = [b.y0 - a.y1 for a, b in zip(rights, rights[1:])]
            if max(gaps) > 4 * max(heights, 1.0):
                continue
        if len(right) == 1:
            # 右栏孤行与中缝左侧的行同排(基线重叠)→ 它是宽表头行的碎片,
            # 不是栏内容;真正的栏延续孤行(如双栏末页右栏末行)同排无左侧行。
            r0 = right[0]
            left_all = [ln for ln in body if ln.x1 <= g - margin]
            same_row = any(
                min(l.y1, r0.y1) - max(l.y0, r0.y0) > 0.5 * min(l.y1 - l.y0, r0.y1 - r0.y0)
                for l in left_all)
            if same_row:
                continue
        # 容忍极少数横穿行:未达全宽阈值的长标题/图注不应废掉整个分栏判定
        tol = max(2, int(0.08 * len(body)))
        if len(crossing) <= tol and len(left) >= 2 and len(right) >= 1:
            return g
    return None


def _wide_groups(lines: list[_Line]) -> list[list[_Line]]:
    """把连续的全宽行聚成组(多行标题/摘要),作为分带边界。"""
    groups: list[list[_Line]] = []
    cur: list[_Line] = []
    for ln in lines:
        if ln.wide:
            if cur and ln.y0 - cur[-1].y1 > 1.5 * max(ln.y1 - ln.y0, 8.0):
                groups.append(cur)
                cur = []
            cur.append(ln)
        else:
            if cur:
                groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)
    return groups


def _same_row(a: _Line, b: _Line) -> bool:
    """两条线是否在同一物理行(基线重叠超过较矮者的一半)。"""
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap > 0.5 * min(a.y1 - a.y0, b.y1 - b.y0)


def _order_page(lines: list[_Line], w: float) -> tuple[list[_Line], int]:
    """返回(按阅读顺序排列的行, 页面栏数)。"""
    if not lines:
        return [], 1
    gutter = _detect_gutter(lines, w)
    ncols = 2 if gutter is not None else 1

    if ncols == 1:
        # 单栏页:没有栏序与分带,阅读顺序就是从上到下(同排元素按 x)。
        # 普通整行句子常横穿中线,若按"全宽带"分带,会把连续段落切成
        # 带间隔的孤岛(P1:/P2:/斜体续行各成一个带而无法合并)。
        for ln in lines:
            ln.band = 0
            ln.col_key = 0
            ln.col = 0
        ordered = sorted(lines, key=lambda l: (l.y0, l.x0))
        result: list[_Line] = []
        i = 0
        while i < len(ordered):
            j = i + 1
            while j < len(ordered) and _same_row(ordered[i], ordered[j]):
                j += 1
            result.extend(sorted(ordered[i:j], key=lambda l: l.x0))
            i = j
        return result, 1

    groups = _wide_groups(lines)
    group_bottoms = [max(ln.y1 for ln in g) for g in groups]
    group_tops = [min(ln.y0 for ln in g) for g in groups]

    for ln in lines:
        # 带 = 完全位于该行之上的全宽组数量(自身所在组不含)
        ln.band = sum(1 for bt in group_bottoms if bt <= ln.y0 + 2)
        if ln.wide:
            ln.col_key = 1.5
            ln.col = 0
        elif (ln.x0 + ln.x1) / 2 >= gutter:
            ln.col_key = 1
            ln.col = 1
        else:
            ln.col_key = 0
            ln.col = 0
    ordered = sorted(lines, key=lambda l: (l.band, l.col_key, l.y0, l.x0))
    # 同一物理行内的并排元素(如色条标签 + 右侧正文)按 x 排序:
    # 基线 0.x pt 的误差会让标签插到条目与续行之间,拆散整段。
    result: list[_Line] = []
    i = 0
    while i < len(ordered):
        j = i + 1
        while (j < len(ordered) and ordered[j].band == ordered[i].band
               and ordered[j].col_key == ordered[i].col_key
               and _same_row(ordered[i], ordered[j])):
            j += 1
        result.extend(sorted(ordered[i:j], key=lambda l: l.x0))
        i = j
    return result, ncols


def _x_overlap(a: _Line, b: _Line) -> float:
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def _group_blocks(ordered: list[_Line]) -> list[_Block]:
    """行 → 块:同页同带同栏同宽属性、垂直紧邻、水平重叠、样式一致。"""
    blocks: list[_Block] = []
    cur: _Line | None = None
    cur_text: list[str] = []
    last_line_h = 0.0  # 上一行(已入块)的单行高度;块高会随合并增长,不能用

    def close() -> None:
        nonlocal cur, cur_text, last_line_h
        if cur is not None:
            blocks.append(
                _Block(
                    page=cur.page, x0=cur.x0, y0=cur.y0, x1=cur.x1, y1=cur.y1,
                    text=_clean(" ".join(cur_text)), size=cur.size, bold=cur.bold,
                    color=cur.color, col=cur.col, band=cur.band, wide=cur.wide,
                    n_lines=len(cur_text),
                )
            )
        cur, cur_text, last_line_h = None, [], 0.0

    for ln in ordered:
        # 列表/标签开头的行(P1: / a) / 1. / ii.)永远另起新块:
        # 行尾无标点的并列项(P1: ... (true))否则会被垂直紧邻规则粘成一段
        starts_item = bool(NEWPARA_RE.match(ln.text))
        if cur is not None:
            same_stream = (
                ln.page == cur.page
                and ln.band == cur.band
                and ln.col == cur.col
                and ln.wide == cur.wide
            )
            # 用单行高(而非累积块高)判断紧邻:块会越并越高,用块高会让段间距判断失效
            line_h = max(ln.y1 - ln.y0, last_line_h, 1.0)
            v_ok = (ln.y0 - cur.y1) <= max(2.5, 0.35 * line_h)
            x_ok = _x_overlap(cur, ln) >= 0.3 * min(cur.x1 - cur.x0, ln.x1 - ln.x0)
            style_ok = (
                max(cur.size, ln.size) / min(cur.size, ln.size) <= 1.12
                and cur.bold == ln.bold
                and cur.color == ln.color
            )
            if starts_item or not (same_stream and v_ok and x_ok and style_ok):
                close()
                cur = ln
                cur_text = [ln.text]
                continue
            cur_text.append(ln.text)
            cur.y1 = ln.y1
            cur.x0 = min(cur.x0, ln.x0)
            cur.x1 = max(cur.x1, ln.x1)
        else:
            cur = ln
            cur_text = [ln.text]
    close()
    return [b for b in blocks if len(b.text) >= 2 and not _is_formula(b.text)]


def _is_formula(text: str) -> bool:
    """符号占比过高或字母占比过低的块视为公式/乱码,丢弃。

    阈值 0.55:紧凑数学行如 "(c) P(A) ∩P(B)." 符号占比 0.53,
    用 0.5 会把这类有内容的短行误杀(导出层另有公式恢复兜底)。
    """
    if len(text) < 6:
        return False
    letters = sum(1 for c in text if c.isalpha())
    symbols = sum(1 for c in text if not c.isalnum() and not c.isspace())
    return letters / len(text) < 0.25 or symbols / len(text) > 0.55


def _mergeable(a: _Block, b: _Block) -> bool:
    """块 b 是否并入块 a 所在段落(a、b 阅读顺序相邻)。"""
    if a.color != b.color:
        return False  # 白字标签(色条)与黑字正文是不同版式元素
    at = a.text
    if at.endswith("-"):
        return True  # 断字连字符,总是续接
    if TERMINAL_RE.search(at):
        return False
    if a.bold and len(at) < 80:
        return False  # a 是标题,不向后吸收
    if b.bold and len(b.text) < 80:
        return False  # b 是标题,起新段
    if max(a.size, b.size) / min(a.size, b.size) > 1.15:
        return False
    if NEWPARA_RE.match(b.text):
        return False
    if a.page == b.page:
        if a.col == b.col and a.band == b.band:
            line_h = max(8.0, min(a.y1 - a.y0, b.y1 - b.y0) / max(a.n_lines, b.n_lines, 1))
            # 0.75×行高:大行距文档(如 24pt 行距)行间空隙 ≈ 行高,若用 1.2×
            # 会把并列项(P4: ...(false) + Given that...)粘成一段
            return (b.y0 - a.y1) <= max(6.0, 0.75 * line_h)
        if b.col == a.col + 1 and a.band == b.band and b.text[0].islower():
            return True  # 跨栏续行
        return False
    if b.page == a.page + 1 and b.text[0].islower():
        return True  # 跨页续行
    return False


def extract_pdf(
    path: str, skip_references: bool = True
) -> tuple[list[Paragraph], list[dict]]:
    """返回 (段落列表, 页面元数据)。页面元数据供前端按比例渲染对照视图。"""
    doc = pymupdf.open(path)
    try:
        pages = _collect_pages(doc)
    finally:
        doc.close()
    if not pages:
        return [], []

    _drop_running_zones(pages)

    ordered: list[_Line] = []
    pages_meta: dict[int, dict] = {}
    for pno in sorted(pages):
        w, h, lines = pages[pno]
        ordered_p, ncols = _order_page(lines, w)
        ordered.extend(ordered_p)
        pages_meta[pno] = {"page": pno, "w": round(w, 1), "h": round(h, 1),
                           "ncols": ncols}

    blocks = _group_blocks(ordered)

    def make_para(b: _Block) -> Paragraph:
        meta = pages_meta.get(b.page, {})
        h = meta.get("h") or 842.0
        w = meta.get("w") or 595.0
        return Paragraph(
            page=b.page, text=b.text, col=b.col, wide=b.wide,
            top=round(b.y0 / h, 4), bottom=round(b.y1 / h, 4),
            left=round(b.x0 / w, 4), right=round(b.x1 / w, 4),
            ncols=meta.get("ncols", 1),
        )

    paras: list[Paragraph] = []
    cur: _Block | None = None
    cur_page = 0
    cur_top = 0.0

    def close_para() -> None:
        if cur is not None:
            p = make_para(cur)
            h = pages_meta.get(cur_page, {}).get("h") or 842.0
            p.top = round(min(cur_top, cur.y0) / h, 4)
            paras.append(p)

    for b in blocks:
        if skip_references and REF_HEADING_RE.match(b.text):
            break
        if cur is not None and _mergeable(cur, b):
            if cur.text.endswith("-"):
                cur.text = cur.text[:-1] + b.text
            else:
                cur.text = cur.text + " " + b.text
            cur.y1 = max(cur.y1, b.y1)
            cur.x0 = min(cur.x0, b.x0)
            cur.x1 = max(cur.x1, b.x1)
            # 吸收的块可能位于段落首块上方(右栏续行的起点往往更高)
            cur_top = min(cur_top, b.y0)
            # 跨栏合并的段落按全宽处理(导出覆盖与对照视图都更正确)
            if b.col != cur.col or b.wide:
                cur.wide = True
        else:
            close_para()
            cur = b
            cur_page = b.page
            cur_top = b.y0
    close_para()

    for i, p in enumerate(paras, 1):
        p.seq = i
    return paras, [pages_meta[p] for p in sorted(pages_meta)]
