"""导出换行回归测试:运行 .venv/Scripts/python.exe tests/test_export.py。"""
import re
import sys
import tempfile
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.export_pdf import _fit_wrapped_lines, _layout_text, _line_width, _manual_wrap, overlay_pdf


def main():
    assert _manual_wrap("P\nQ\n\nR\n", 10, 400) == ["P", "Q", "", "R", ""]
    for text in ["P→Q∧R∨S↔T" * 20, "abcdefghijk" * 20, "中文与公式(P→Q)混排" * 15]:
        lines = _manual_wrap(text, 10, 83)
        assert len(lines) > 1
        # 行宽用渲染实算校验(符号/拉丁按实际字体度量,不再按全宽)
        assert all(_line_width(line, 10) <= 83.01 for line in lines), lines
        assert "".join(lines) == text
    source = "a) ¬P b) P→Q c) P ∨Q h) ¬P ∨(P ∧Q)"
    text = _layout_text({"source": source, "translation": source})
    assert text.splitlines() == ["a) ¬P", "b) P→Q", "c) P ∨Q", "h) ¬P ∨(P ∧Q)"]
    assert _layout_text({"source": source, "translation": text}) == text
    assert _layout_text({"source": "a) ¬P b) P↔Q", "translation": "a) ØP b) P↔Q"}) == "a) ¬P\nb) P↔Q"
    assert _layout_text({"source": "Consider (P ∨¬Q).", "translation": "考虑 (P ∨ØQ)。"}) == "考虑 (P ∨¬Q)。"
    fs, lines = _fit_wrapped_lines("P→Q" * 40, pymupdf.Rect(0, 0, 110, 90))
    assert fs < 10.5 and len(lines) * fs * 1.5 <= 86
    try:
        _fit_wrapped_lines("P→Q" * 1000, pymupdf.Rect(0, 0, 30, 20))
    except ValueError:
        pass
    else:
        raise AssertionError("空间不足须报错，而非丢字/越界")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src, dst = tmp / "source.pdf", tmp / "out.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=800)
        page.insert_text((35, 100), "LEFT FORMULA", fontsize=10)
        page.insert_text((330, 100), "RIGHT FORMULA", fontsize=10)
        doc.save(src)
        doc.close()
        paras = []
        for seq, x, col in [(1, 35, 0), (2, 330, 1)]:
            paras.append(dict(seq=seq, page=1, source=("LEFT" if col == 0 else "RIGHT") + " FORMULA",
                              translation="P→Q∧R∨S↔T" * 7, status="ok", wide=False, col=col,
                              top=88/800, bottom=104/800, left=x/600, right=(x+100)/600))
        overlay_pdf(src, paras, [dict(page=1, w=600, h=800, ncols=2)], dst)
        with pymupdf.open(dst) as doc:
            translated = [s for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0
                          for l in b["lines"] for s in l["spans"]
                          if (s["color"] & 0xFFFFFF) == 0x1A1A1A]
            assert len(translated) > 2, translated
            assert sum(len(re.sub(r"\s", "", s["text"])) for s in translated) == 2 * len(paras[0]["translation"])
            for span in translated:
                x0, _, x1, _ = span["bbox"]
                assert x1 <= (288 if x0 < 300 else 576) + 1, span
    print("PASS export: explicit newlines, formulas, long tokens, font floor, two-column content and bounds")


if __name__ == "__main__":
    main()
