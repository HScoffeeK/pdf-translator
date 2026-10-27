"""v2 回归:缓存隔离、公式占位符、选页、失败不发布。无需网络。"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PDT_ENABLE_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pymupdf
from app import db
from app.pdf2zh_export import _make_bridge, export_pdf2zh


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.init(self.root / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bridge_cache_and_placeholders(self):
        bridge = _make_bridge("mock", None)
        t = bridge("en", "zh", None)
        result = t.translate("Text {v2}")
        self.assertEqual(result, "[mock] Text {v2}")
        with patch("app.translate.MockTranslator.translate_batch", side_effect=AssertionError):
            self.assertEqual(t.translate("Text {v2}"), result)
        self.assertFalse(bridge.failures)
        async def broken(_self, texts):
            return ["中文:占位符丢失"]
        with patch("app.translate.MockTranslator.translate_batch", broken):
            self.assertEqual(t.translate("Other {v3}"), "Other {v3}")
        self.assertEqual(len(bridge.failures), 1)
        self.assertFalse(any(r["v"] == "中文:占位符丢失" for r in db.query("select v from cache")))

    def test_selected_pages_and_failure_no_publication(self):
        src, out = self.root / "src.pdf", self.root / "out.pdf"
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 100), "First page")
            doc.new_page().insert_text((50, 100), "Second page")
            doc.save(src)
        seen = []
        def fake(src, outdir, bridge, model, vfont, pages=None):
            seen.append(pages)
            outdir.mkdir(parents=True)
            mono = outdir / "mono.pdf"
            with pymupdf.open(src) as doc:
                doc.save(mono)
            return mono
        with patch("app.pdf2zh_export._load_model", return_value=object()), \
             patch("app.pdf2zh_export._run_once", fake):
            stats = export_pdf2zh(src, out, "mock", pages_spec="2")
        self.assertEqual(seen, [[1]])
        self.assertEqual(stats["translated_pages"], 1)
        self.assertEqual(stats["pages"], 2)
        with patch("app.pdf2zh_export._load_model", return_value=object()), \
             patch("app.pdf2zh_export._run_once", side_effect=RuntimeError("failure")):
            before = out.read_bytes()
            with self.assertRaises(RuntimeError):
                export_pdf2zh(src, out, "mock")
            self.assertEqual(before, out.read_bytes())
        self.assertFalse(list(self.root.glob(".pdf2zh_*")))


if __name__ == "__main__":
    unittest.main()
