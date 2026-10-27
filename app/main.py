"""FastAPI 服务:静态前端 + REST API。

API 一览:
  POST /api/jobs            上传 PDF,创建任务并后台运行 {job_id}
  GET  /api/jobs            任务列表(含进度统计)
  GET  /api/jobs/{id}       任务状态与进度
  GET  /api/jobs/{id}/result 双语对照结果(JSON)
  POST /api/jobs/{id}/retry 失败续跑(已完成段落走缓存)
  DELETE /api/jobs/{id}     删除任务及文件
  GET  /api/engines         可用引擎列表(前端下拉用)
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, pipeline
from .config import settings
from .translate import TranslationError, get_translator, llm_config

PDF_MAGIC = b"%PDF-"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    data = settings.resolved_data_dir
    (data / "uploads").mkdir(parents=True, exist_ok=True)
    db.init(data / "app.db")
    db.recover_interrupted()
    yield


app = FastAPI(title="PDF 翻译工具", lifespan=lifespan)


# ---- 鉴权(为将来部署预留;本地默认关闭)----

def _check_auth(authorization: str | None) -> None:
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="无效令牌")


def _auth(authorization: str | None = Header(None)) -> None:
    _check_auth(authorization)


# ---- API ----

@app.post("/api/jobs", dependencies=[])
async def create_job(
    file: UploadFile = File(...),
    engine: str | None = Form(None),
    retries: int | None = Form(None),
    pages: str | None = Form(None),
    authorization: str | None = Header(None),
    x_llm_key: str | None = Header(None),
) -> JSONResponse:
    _check_auth(authorization)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    content = await file.read()
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(400, "不是有效的 PDF 文件")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB 上限")
    pages = (pages or "").strip() or None
    if pages and pipeline.parse_pages(pages) is None:
        raise HTTPException(400, "页码格式无效,示例:1-10,15,20-")

    eng = engine or settings.engine
    try:
        get_translator(eng)  # 提前校验引擎可用性
    except TranslationError as e:
        raise HTTPException(400, str(e)) from e

    if retries is not None and not (1 <= retries <= 10):
        raise HTTPException(400, "重试次数须在 1~10 之间")

    job_id = db.create_job(
        filename=file.filename,
        file_path=str(settings.resolved_data_dir / "uploads" / f"{uuid.uuid4().hex}.pdf"),
        engine=eng,
        retries=retries,
        pages=pages,
    )
    Path(job_path := db.get_job(job_id)["file_path"]).write_bytes(content)
    # Key 只经内存传给本次任务,不写入数据库
    asyncio.get_running_loop().create_task(
        _run_logged(job_id, llm_key=(x_llm_key or "").strip() or None))
    return JSONResponse({"job_id": job_id}, status_code=201)


async def _run_logged(job_id: int, llm_key: str | None = None) -> None:
    try:
        await pipeline.run_job(job_id, _log_progress, llm_key=llm_key)
    except Exception as e:  # pragma: no cover create_task 的兜底
        print(f"[job {job_id}] crashed: {e}", flush=True)


@app.get("/api/engines")
async def engines() -> list[dict]:
    # Key 默认存各用户浏览器、随请求携带,服务端 ready 只反映"服务端共享配置",
    # 前端会按浏览器里是否有 Key 修正显示。
    out = [{"id": "google", "name": "Google(免费,无需 key)", "default": settings.engine == "google"}]
    out.append({
        "id": "glm", "name": "GLM 智谱(免费模型)",
        "default": settings.engine == "glm",
        "ready": bool(llm_config("glm")["api_key"]),
    })
    out.append({
        "id": "llm", "name": "LLM(OpenAI 兼容)",
        "default": settings.engine == "llm",
        "ready": bool(llm_config()["api_key"]),
    })
    if settings.enable_mock:
        out.append({"id": "mock", "name": "Mock(测试)", "default": settings.engine == "mock"})
    return out


# ---- LLM 配置:Key 默认存各用户浏览器(随 X-LLM-Key 头携带,不落盘);
#      这里只存非敏感的地址/模型,或部署者自愿共享的自用 Key(.env 兜底) ----

class LlmSettings(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


def _llm_settings_payload() -> dict:
    cfg = llm_config()
    key = cfg["api_key"]
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(key),
        "key_hint": (f"{key[:4]}…{key[-4:]}" if len(key) > 10 else ("已配置" if key else "")),
    }


@app.get("/api/settings/llm")
async def get_llm_settings() -> dict:
    return _llm_settings_payload()


@app.post("/api/settings/llm")
async def set_llm_settings(body: LlmSettings,
                           authorization: str | None = Header(None)) -> dict:
    _check_auth(authorization)
    for field in ("base_url", "api_key", "model"):
        value = getattr(body, field)
        if value:  # 空/None = 保持不变
            db.set_setting(f"llm_{field}", value.strip())
    return _llm_settings_payload()


@app.post("/api/settings/llm/clear")
async def clear_llm_settings(authorization: str | None = Header(None)) -> dict:
    """清掉服务端存储的共享 LLM/GLM 配置(浏览器本地 Key 不受影响)。"""
    _check_auth(authorization)
    for k in ("llm_base_url", "llm_api_key", "llm_model",
              "glm_base_url", "glm_api_key", "glm_model"):
        db.execute("DELETE FROM settings WHERE k=?", (k,))
    return _llm_settings_payload()


# ---- 追加翻译页 ----

class PagesBody(BaseModel):
    pages: str


async def _log_progress(jid: int, status: str, done: int, total: int) -> None:
    print(f"[job {jid}] {status} {done}/{total}", flush=True)


@app.post("/api/jobs/{job_id}/pages")
async def job_add_pages(job_id: int, body: PagesBody,
                        authorization: str | None = Header(None),
                        x_llm_key: str | None = Header(None)) -> dict:
    """给已有任务追加翻译页(如大书先译几章,之后再补其余页)。"""
    _check_auth(authorization)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] in ("queued", "extracting", "translating"):
        raise HTTPException(409, "任务正在进行中,请稍后再追加")
    if not Path(job["file_path"]).exists():
        raise HTTPException(404, "PDF 源文件已删除,无法追加页")
    spec = body.pages.strip()
    if pipeline.parse_pages(spec) is None:
        raise HTTPException(400, "页码格式无效,示例:1-10,15,20-")
    asyncio.get_running_loop().create_task(
        pipeline.extract_more_pages(job_id, spec, _log_progress,
                                    llm_key=(x_llm_key or "").strip() or None))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs")
async def jobs() -> list[dict]:
    return db.list_jobs()


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    counts = db.job_counts(job_id)
    return {**job, **counts}


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] not in ("done", "partial"):
        raise HTTPException(409, f"任务未完成(当前状态: {job['status']})")
    paragraphs = db.job_paragraphs(job_id)
    from .export_pdf import _layout_text
    for p in paragraphs:
        if p.get("translation"):
            p["translation"] = _layout_text(p)
    pages = db.get_pages_meta(job_id)
    return {"job": {k: job[k] for k in ("id", "filename", "engine", "status")},
            "pages": pages,
            "paragraphs": paragraphs}


def _export_path(job_id: int, layout: str = "v1") -> Path:
    suffix = "_zh" if layout == "v1" else "_zh2"
    return settings.resolved_data_dir / "exports" / f"job{job_id}{suffix}.pdf"


_EXPORT_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_EXPORT_LOCKS_GUARD = threading.Lock()


def _export_lock(job_id: int, layout: str) -> threading.Lock:
    key = (job_id, layout)
    with _EXPORT_LOCKS_GUARD:
        return _EXPORT_LOCKS.setdefault(key, threading.Lock())


def _ensure_export(job: dict, layout: str = "v1", llm_key: str | None = None) -> Path:
    """确保中文版 PDF 缓存存在且新鲜;失败抛 HTTPException。

    layout="v1" 自研几何回填(快、需逐文档调优);
    layout="v2" pdf2zh/babeldoc 版面分析 + 块内重排(通用性强,推荐)。
    """
    if layout not in ("v1", "v2"):
        raise HTTPException(400, "layout 只能是 v1 或 v2")
    with _export_lock(job["id"], layout):
        return _generate_export(job, layout, llm_key)


def _generate_export(job: dict, layout: str, llm_key: str | None) -> Path:
    from .export_pdf import LAYOUT_VERSION, overlay_pdf
    from .pdf2zh_export import LAYOUT_VERSION_PDF2ZH, Pdf2zhUnavailable, export_pdf2zh
    out = _export_path(job["id"], layout)
    version_path = out.with_suffix(".layout")
    version = LAYOUT_VERSION if layout == "v1" else LAYOUT_VERSION_PDF2ZH
    src = Path(job["file_path"])
    if out.exists():
        import datetime as _dt
        try:
            job_ts = _dt.datetime.fromisoformat(job["updated_at"]).timestamp()
        except (KeyError, ValueError):
            job_ts = 0.0
        version_ok = version_path.exists() and version_path.read_text(encoding="utf-8") == version
        if version_ok and out.stat().st_mtime >= job_ts - 1:
            return out
    if not src.exists():
        if out.exists():
            return out
        raise HTTPException(404, "PDF 源文件已删除且无缓存,无法生成中文版")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if layout == "v2":
            engine = job.get("engine") or settings.engine
            stats = export_pdf2zh(src, out, engine, llm_key=llm_key,
                                  pages_spec=job.get("pages"))
        else:
            paras = db.job_paragraphs(job["id"])
            pages = db.get_pages_meta(job["id"])
            stats = overlay_pdf(src, paras, pages, out)
        version_path.write_text(version, encoding="utf-8")
    except Pdf2zhUnavailable as e:
        raise HTTPException(503, f"版式引擎 v2 不可用: {e}") from e
    except Exception as e:
        raise HTTPException(500, f"生成中文版 PDF 失败: {e}") from e
    print(f"[job {job['id']}] export({layout}) cached: {stats}", flush=True)
    return out


# 页图渲染缓存:键含源文件 mtime,导出重新生成后自动失效。
# 前端虽带 no-store 每次回源,但服务端命中缓存可免去重复光栅化。
_PNG_CACHE: dict[tuple, bytes] = {}
_PNG_CACHE_MAX = 48
_PNG_LOCK = asyncio.Lock()


async def _render_page_png(src: Path, page_no: int, zoom: float) -> bytes:
    try:
        mtime = src.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(src), mtime, page_no, round(zoom, 3))
    cached = _PNG_CACHE.get(key)
    if cached is not None:
        return cached
    async with _PNG_LOCK:
        cached = _PNG_CACHE.get(key)
        if cached is not None:
            return cached

    def render() -> bytes:
        import pymupdf
        doc = pymupdf.open(src)
        try:
            if not (1 <= page_no <= len(doc)):
                raise HTTPException(404, "页码超出范围")
            page = doc[page_no - 1]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            return pix.tobytes("png")
        finally:
            doc.close()

    png = await asyncio.to_thread(render)
    if len(_PNG_CACHE) >= _PNG_CACHE_MAX:
        _PNG_CACHE.pop(next(iter(_PNG_CACHE)))
    _PNG_CACHE[key] = png
    return png


@app.get("/api/jobs/{job_id}/page/{page_no}")
async def job_page_image(job_id: int, page_no: int, zoom: float = 1.5, zh: int = 0,
                         layout: str = "v1",
                         x_llm_key: str | None = Header(None)):
    """渲染某页为 PNG。zh=1 时渲染中文版 PDF(按需生成并缓存)。"""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if zh:
        src = await asyncio.to_thread(
            _ensure_export, job, layout, (x_llm_key or "").strip() or None)
    else:
        src = Path(job["file_path"])
        if not src.exists():
            raise HTTPException(404, "PDF 文件已被删除")

    png = await _render_page_png(src, page_no, zoom)
    return Response(
        content=png, media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/jobs/{job_id}/export")
async def job_export(job_id: int, layout: str = "v1",
                     authorization: str | None = Header(None),
                     x_llm_key: str | None = Header(None)):
    """生成中文版 PDF 并返回文件。layout=v1 自研 / v2 pdf2zh 版式引擎。"""
    _check_auth(authorization)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] not in ("done", "partial"):
        raise HTTPException(409, f"任务未完成(当前状态: {job['status']})")
    if layout not in ("v1", "v2"):
        raise HTTPException(400, "layout 只能是 v1 或 v2")

    out = await asyncio.to_thread(
        _ensure_export, job, layout, (x_llm_key or "").strip() or None)
    stem = Path(job["filename"]).stem
    tag = "" if layout == "v1" else "_重排版"
    return FileResponse(
        out, media_type="application/pdf",
        filename=f"{stem}_中文版{tag}.pdf",
        background=None,
    )


@app.post("/api/jobs/{job_id}/delete-file")
async def job_delete_file(job_id: int, authorization: str | None = Header(None)) -> dict:
    """删除任务的 PDF 源文件(译文结果保留)。"""
    _check_auth(authorization)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    try:
        Path(job["file_path"]).unlink(missing_ok=True)
    except OSError as e:  # pragma: no cover
        raise HTTPException(500, f"删除文件失败: {e}") from e
    return {"file_deleted": job_id}


@app.post("/api/jobs/{job_id}/retry")
async def job_retry(job_id: int, authorization: str | None = Header(None),
                    x_llm_key: str | None = Header(None)) -> dict:
    _check_auth(authorization)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] not in ("failed", "partial", "interrupted"):
        raise HTTPException(409, f"状态 {job['status']} 无需续跑")
    db.reset_for_retry(job_id)
    db.set_job(job_id, status="queued", error=None)
    asyncio.get_running_loop().create_task(
        _run_logged(job_id, llm_key=(x_llm_key or "").strip() or None))
    return {"job_id": job_id, "status": "queued"}


@app.delete("/api/jobs/{job_id}")
async def job_delete(job_id: int, authorization: str | None = Header(None)) -> dict:
    _check_auth(authorization)
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    try:
        Path(job["file_path"]).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
    db.delete_job(job_id)
    return {"deleted": job_id}


# ---- 静态前端 ----

app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True),
          name="static")
