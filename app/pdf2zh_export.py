"""版式引擎 v2:用 pdf2zh/babeldoc 做版面分析与块内重排,翻译走本项目引擎。

为什么不用 pdf2zh 自带翻译器:它的免费谷歌调用走 translate.google.com/m,
在本机网络被限流(实测卡死);本项目的 curl 通道 + GLM/LLM 适配器已验证可用。
所以这里只借用它的**版式引擎**,翻译层桥接回 app.translate。

桥接方式:pdf2zh 在 TranslateConverter 里按固定类名列表解析 service,
把列表中的 ArgosTranslator 临时替换成我们的桥接类(name="pdtbridge"),
再用 service="pdtbridge" 调用;调用期间持全局锁,避免并发导出互相干扰。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import threading
from pathlib import Path

from . import db
from .config import settings
from .translate import cache_key, get_translator

LAYOUT_VERSION_PDF2ZH = "pdf2zh-2"

_lock = threading.Lock()
_model = None


class Pdf2zhUnavailable(RuntimeError):
    """pdf2zh 未安装或版面模型缺失。"""


def _load_model():
    """加载 DocLayout-YOLO 版面模型(首次需下载 ~75MB,缓存在 ~/.cache/babeldoc)。"""
    global _model
    if _model is not None:
        return _model
    try:
        from pdf2zh.doclayout import DocLayoutModel
    except ImportError as e:  # pragma: no cover 依赖缺失
        raise Pdf2zhUnavailable(f"pdf2zh 未安装: {e}") from e
    try:
        _model = DocLayoutModel.load_available()
    except Exception as e:  # pragma: no cover 模型下载失败
        raise Pdf2zhUnavailable(
            "版面模型缺失或下载失败(DocLayout-YOLO)。"
            "请检查网络,或手动放置 doclayout_yolo_docstructbench_imgsz1024.onnx "
            f"到 ~/.cache/babeldoc/models/。原始错误: {e}") from e
    return _model


def _make_bridge(engine: str, llm_key: str | None):
    """只用本项目缓存;pdf2zh 的默认缓存不区分桥接后的翻译服务。"""
    from collections import Counter
    from pdf2zh.translator import BaseTranslator

    translator = get_translator(engine)
    translator.api_key_override = llm_key
    failures: list[str] = []
    placeholders = re.compile(r"\{v\d+\}")
    cache_engine = f"layout2:{engine}"
    if engine in ("glm", "llm"):
        import hashlib
        cfg = translator._cfg()
        identity = cfg["base_url"] + "\n" + cfg["model"]
        cache_engine += ":" + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def do_translate(self, text: str) -> str:
        if not text.strip():
            return text
        key = cache_key(cache_engine, settings.target_lang, settings.source_lang, text)
        try:
            result = db.cache_get(key)
            if result is None:
                results = asyncio.run(translator.translate_batch([text]))
                if len(results) != 1 or not results[0].strip():
                    raise ValueError("翻译返回空值或段数不符")
                result = results[0].strip()
                if getattr(translator, "name", "") not in ("llm", "glm"):
                    result = translator.glossary.apply(result)
            if Counter(placeholders.findall(text)) != Counter(placeholders.findall(result)):
                raise ValueError("公式占位符缺失或被改写")
            if engine != "mock" and re.search(r"[A-Za-z]{3,}", text):
                if result.strip() == text.strip():
                    raise ValueError("英文正文未翻译")
            db.cache_put(key, result)
            return result
        except Exception:
            # pdf2zh 对异常无限重试;收集失败并让本遍结束,调用方不发布结果。
            failures.append("正文翻译失败或公式占位符校验失败")
            return text

    def __init__(self, lang_in, lang_out, model=None, ignore_cache=False, **kwargs):
        self.lang_in, self.lang_out = lang_in, lang_out
        self.model, self.ignore_cache = model, ignore_cache

    return type("PdtBridgeTranslator", (BaseTranslator,), {
        "name": "pdtbridge", "do_translate": do_translate,
        "translate": lambda self, text, ignore_cache=False: do_translate(self, text),
        "__init__": __init__, "failures": failures,
    })


def _cjk_lines(pdf_path: Path) -> int:
    """统计 PDF 中的中文字行数,用于判断这一遍是否真的翻译了。"""
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as doc:
            n = 0
            for page in doc:
                for b in page.get_text("dict")["blocks"]:
                    if b["type"] != 0:
                        continue
                    for line in b["lines"]:
                        text = "".join(s["text"] for s in line["spans"])
                        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
                            n += 1
            return n
    except Exception:  # pragma: no cover
        return 0


def _run_once(src: Path, outdir: Path, bridge, model, vfont: str,
              pages: list[int] | None = None) -> Path:
    import pdf2zh.converter as converter
    from pdf2zh.high_level import translate

    failures_before = len(bridge.failures)
    outdir.mkdir(parents=True, exist_ok=True)
    with _lock:  # pdf2zh 的 service 解析依赖临时替换全局类,串行执行
        saved = converter.ArgosTranslator
        converter.ArgosTranslator = bridge
        try:
            results = translate(
                files=[str(src.resolve())],
                output=str(outdir.resolve()),
                pages=pages,
                lang_in="en",
                lang_out="zh",
                service="pdtbridge",
                thread=1,          # 顺序翻译:我们的节流与缓存状态是单实例的
                skip_subset_fonts=True,
                model=model,
                vfont=vfont,
            )
        finally:
            converter.ArgosTranslator = saved
    if len(bridge.failures) > failures_before:
        raise RuntimeError(f"有 {len(bridge.failures) - failures_before} 个文本块翻译/公式校验失败,未发布不完整 PDF")
    mono = next((Path(m) for m, _dual in results if Path(m).exists()), None)
    if mono is None:
        raise RuntimeError("pdf2zh 未产出单语 PDF(可能页面无可译文本)")
    return mono


def export_pdf2zh(
    src_path: str | Path,
    out_path: str | Path,
    engine: str,
    llm_key: str | None = None,
    pages_spec: str | None = None,
) -> dict:
    """用 pdf2zh 生成中文版 PDF(原版式重排),写入 out_path。

    两遍策略:先用默认公式字体规则(学术论文的公式原样保留);若几乎没译出
    中文(典型原因:无衬线字体的 LaTeX 正文被 CM[^R] 规则误判为公式而被跳过),
    放宽规则重跑一遍,并取中文行数更多的结果。
    """
    try:
        import pdf2zh.converter  # noqa: F401  仅探测依赖可用性
        from pdf2zh.high_level import translate  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise Pdf2zhUnavailable(f"pdf2zh 未安装: {e}") from e

    if engine in ("glm", "llm") and not llm_key:
        raise Pdf2zhUnavailable(
            "版式引擎 v2 使用 AI 翻译时,必须从浏览器携带 X-LLM-Key。"
            "请在 AI 翻译设置里保存自己的 Key 后重试。")
    model = _load_model()
    src = Path(src_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import pymupdf
    with pymupdf.open(src) as doc:
        total_pages = len(doc)
    pages: list[int] | None = None
    if pages_spec:
        from .pipeline import parse_pages
        selected = parse_pages(pages_spec)
        if selected is None:
            raise ValueError("页码范围无效")
        pages = sorted(p - 1 for p in selected if 1 <= p <= total_pages)
        if not pages:
            raise ValueError("选择的页码超出 PDF 范围")
    root = Path(tempfile.mkdtemp(prefix=".pdf2zh_", dir=out_path.parent))
    dir_a, dir_b = root / "a", root / "b"
    bridge = _make_bridge(engine, llm_key)
    try:
        mono = _run_once(src, dir_a, bridge, model, vfont="", pages=pages)
        best, best_n = mono, _cjk_lines(mono)
        if best_n == 0 and engine != "mock":
            # 仅排除 CMSS 正文字体的误判,仍保留 CMMI/CMSY 等数学字体。
            vfont = r"(CM(?!SS)[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)"
            mono_b = _run_once(src, dir_b, bridge, model, vfont=vfont, pages=pages)
            n_b = _cjk_lines(mono_b)
            if n_b > best_n:
                best, best_n = mono_b, n_b
        if best_n == 0 and engine != "mock":
            raise RuntimeError(
                "版式引擎 v2 未能确认生成中文正文,未发布结果。"
                "请改用原位覆写或检查 PDF 是否为扫描件。")
        best.replace(out_path)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {"pages": total_pages, "engine": "pdf2zh", "cjk_lines": best_n,
            "translated_pages": len(pages) if pages is not None else total_pages}
