"""提取算法断言测试:标记词必须按阅读顺序出现,页眉页脚/参考文献被剔除。
运行:.venv/Scripts/python.exe tests/test_extract.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extract import extract_pdf  # noqa: E402
from tests.make_sample_pdf import build  # noqa: E402

SAMPLE = Path(__file__).parent / "sample.pdf"


def main() -> None:
    build(str(SAMPLE))
    paras, _meta = extract_pdf(str(SAMPLE), skip_references=False)
    full = "\n".join(p.text for p in paras)

    # 1. 阅读顺序:标记词严格递增(左栏 → 右栏,跨栏/跨页续行拼接)
    order = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO",
             "FOXTROT", "GOLF", "HOTEL", "INDIA", "JULIET", "KILO", "LIMA"]
    pos = [full.find(m) for m in order]
    for m, p in zip(order, pos):
        assert p >= 0, f"缺失标记词 {m}"
    assert pos == sorted(pos), f"阅读顺序错误: {list(zip(order, pos))}"

    # 2. 跨栏续行:CHARLIE 段落应直接接 DELTA 开头(同一段落)
    assert "continues into the right column" in full, "跨栏续行未拼接"

    # 3. 跨页续行:INDIA 句子应与下一页开头拼接
    assert "continues onto the next page across the page boundary" in full, \
        "跨页续行未拼接"

    # 4. 页眉页脚剔除
    assert "arXiv:2610" not in full, "页眉未被剔除"
    for p in paras:
        assert p.text.strip() not in {"1", "2", "3"}, "页码未被剔除"

    # 5. References 截断
    paras_noref, meta_noref = extract_pdf(str(SAMPLE), skip_references=True)
    assert meta_noref and all(
        set(m) >= {"page", "w", "h", "ncols"} for m in meta_noref
    ), "页面元数据缺失"
    full_noref = "\n".join(p.text for p in paras_noref)
    assert "References" not in full_noref and "Scaling laws" not in full_noref, \
        "References 未被截断"

    # 6. 公式块防御(基本烟雾:无空段落)
    assert all(p.text.strip() for p in paras)

    print(f"PASS  paragraphs={len(paras)} (skip_references=True 时 {len(paras_noref)})")
    for p in paras:
        print(f"  p{p.page} #{p.seq}: {p.text[:80]}")


if __name__ == "__main__":
    main()
