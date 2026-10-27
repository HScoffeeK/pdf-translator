"""生成测试 PDF:确定性版式,每个段落带唯一标记词,用于提取算法的断言测试。

布局(模拟两栏论文):
  第 1 页:页眉 + 全宽标题/摘要 + 双栏(ALPHA/BRAVO/标题/CHARLIE | DELTA/ECHO),
           CHARLIE 未完结句跨栏接 DELTA;
  第 2 页:双栏(FOXTROT/标题/GOLF | HOTEL/INDIA),INDIA 未完结句跨页接 JULIET;
  第 3 页:双栏(JULIET/KILO | LIMA)+ 左栏 References。
"""
import sys

from fpdf import FPDF

HEADER = "arXiv:2610.00000v1 [cs.CL] 20 Oct 2026"
ABSTRACT = (
    "We present a systematic study of efficient pretraining methods for large "
    "language models. Our results demonstrate that careful data curation "
    "combined with adaptive schedules can reduce training cost substantially "
    "without sacrificing quality."
)
REF_ENTRY = (
    "[1] A. Author et al. Scaling laws for neural language models. "
    "Journal of Example Studies, 2026."
)

COL_W = 88
PARA_LH = 4.6


def body(marker: str) -> str:
    return (
        f"{marker} The quick brown fox jumps over the lazy dog while researchers "
        "measure performance across multiple benchmarks and report consistent "
        "gains in all settings considered by the evaluation protocol."
    )


def add_header_footer(pdf: FPDF, page_no: int) -> None:
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_xy(10, 6)
    pdf.cell(190, 4, HEADER, align="L")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(95, 288)
    pdf.cell(20, 5, str(page_no), align="C")


def col_para(pdf: FPDF, x: float, text: str, size: float = 9, lh: float = PARA_LH) -> None:
    pdf.set_font("Helvetica", "", size)
    pdf.set_x(x)
    pdf.multi_cell(COL_W, lh, text)


def col_heading(pdf: FPDF, x: float, text: str) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_x(x)
    pdf.multi_cell(COL_W, 5, text)


def build(path: str) -> None:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)

    # ---- 第 1 页 ----
    pdf.add_page()
    add_header_footer(pdf, 1)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(10, 14)
    pdf.multi_cell(190, 6, "A Sample Paper Layout for Extraction Testing")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, 28)
    pdf.multi_cell(190, 5, "Abstract")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(10, 34)
    pdf.multi_cell(190, 4.5, ABSTRACT)

    pdf.set_xy(10, 58)  # 左栏
    col_para(pdf, 10, body("ALPHA"))
    pdf.ln(4)
    col_para(pdf, 10, body("BRAVO"))
    pdf.ln(4)
    col_heading(pdf, 10, "2.1 Left Heading")
    col_para(pdf, 10, body("CHARLIE") + " and this sentence continues")

    pdf.set_xy(110, 58)  # 右栏
    col_para(pdf, 110, "into the right column of the same page. " + body("DELTA"))
    pdf.ln(4)
    col_para(pdf, 110, body("ECHO"))

    # ---- 第 2 页 ----
    pdf.add_page()
    add_header_footer(pdf, 2)
    pdf.set_xy(10, 25)
    col_para(pdf, 10, body("FOXTROT"))
    pdf.ln(4)
    col_heading(pdf, 10, "2.2 Another Heading")
    col_para(pdf, 10, body("GOLF"))
    pdf.set_xy(110, 25)
    col_para(pdf, 110, body("HOTEL"))
    pdf.ln(4)
    col_para(pdf, 110, body("INDIA") + " meanwhile this sentence continues onto the next page")

    # ---- 第 3 页 ----
    pdf.add_page()
    add_header_footer(pdf, 3)
    pdf.set_xy(10, 25)
    col_para(
        pdf, 10,
        "across the page boundary and finishes here. "
        "JULIET marks the final paragraph of the left column on the last page.",
    )
    pdf.ln(4)
    col_para(pdf, 10, body("KILO"))
    pdf.set_xy(110, 25)
    col_para(pdf, 110, body("LIMA"))
    pdf.set_xy(10, 80)
    col_heading(pdf, 10, "References")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    pdf.multi_cell(COL_W, 4.5, REF_ENTRY)

    pdf.output(path)
    print("written", path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
