"""翻译任务管线:提取 → 并发翻译(带缓存)→ 状态入库。

run_job 是后台协程:上传接口创建任务与 job_id 后 spawn 本函数,不阻塞请求。
任何时候进程退出,任务状态都会留库;重启后 interrupted 状态的任务可 retry 续跑
(已完成段落命中缓存,不重复请求引擎)。
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from . import db
from .config import settings
from .extract import extract_pdf
from .translate import TranslationError, cache_key, get_translator

# 进度回调:job_id, 状态, done, total —— 服务层用来记日志,测试层用来断言
ProgressCb = Callable[[int, str, int, int], Awaitable[None]]


async def _noop(_job_id: int, _status: str, _done: int, _total: int) -> None:
    pass


def job_retries(job_id: int) -> int:
    """任务的重试次数:jobs 表有值用之,否则用全局配置,夹在 1~10。"""
    job = db.get_job(job_id)
    if job and job.get("retries"):
        n = int(job["retries"])
    else:
        n = settings.max_retries
    return max(1, min(10, n))


_COMBINING_LINE_RE = re.compile("[\u0305\u0332\u0333]")


def _postprocess(text: str, translator) -> str:
    """译后处理:非 LLM 引擎套术语表(译后替换);LLM 引擎已进 prompt。

    另剥离组合上/下划线(U+0305/U+0332):谷歌把数学上划线记号译成
    组合符号(如 ∪̸),渲染成乱码,剥离后至少保留可读的基础符号。
    """
    if getattr(translator, "name", "") != "llm":
        text = _COMBINING_LINE_RE.sub("", text)
        return translator.glossary.apply(text)
    return text


_LEAD_MARKER_RE = re.compile(
    r"^\s*((?:[a-z]|\d{1,3}|(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii))[\.\)]\s+)",
    re.IGNORECASE,
)


def _split_marker(source: str) -> tuple[str, str]:
    """剥离段首列表编号(a) / 1. / ii.):编号直译会变成"岛"“我。”之类乱码,
    翻译正文后把原编号拼回。返回(编号, 正文)。"""
    m = _LEAD_MARKER_RE.match(source)
    if not m:
        return "", source
    return m.group(1), source[m.end():]


def _join_marker(marker: str, cached: str) -> str:
    """缓存值归一化:旧格式条目(或同正文不同编号的段落)可能已带编号前缀,
    拼接前先剥掉,避免出现 "ii. i. ..." 这类重复编号。"""
    cached = _LEAD_MARKER_RE.sub("", cached, count=1)
    return marker + cached


def _group_requests(paras: list[dict], max_chars: int = 2500) -> list[list[dict]]:
    """把待译段落按字符数打包成请求组:一次请求译多段,大幅减少引擎请求数。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    n = 0
    for p in paras:
        size = max(1, len(p["source"]))
        if cur and n + size > max_chars:
            groups.append(cur)
            cur, n = [], 0
        cur.append(p)
        n += size + 1
    if cur:
        groups.append(cur)
    return groups


def parse_pages(spec: str | None) -> set[int] | None:
    """解析页码选择 "1-10,15,20-" → 页码集合;空/无效返回 None(=全部页)。"""
    if not spec or not spec.strip():
        return None
    out: set[int] = set()
    for token in spec.replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token.endswith("-"):
            try:
                out.update(range(int(token[:-1]), 100000))
            except ValueError:
                return None
            continue
        m = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                return None
            out.update(range(a, b + 1))
            continue
        if token.isdigit():
            out.add(int(token))
            continue
        return None
    return out or None


async def extract_more_pages(job_id: int, pages_spec: str,
                             on_progress: ProgressCb = _noop,
                             llm_key: str | None = None) -> None:
    """给已有任务追加翻译页:提取未译过的新页段落,再走 run_job 翻译 pending。"""
    try:
        job = db.get_job(job_id)
        if job is None:
            return
        sel = parse_pages(pages_spec)
        if not sel:
            db.set_job(job_id, status="failed", error="页码格式无效")
            await on_progress(job_id, "failed", 0, 0)
            return
        db.set_job(job_id, status="extracting")
        await on_progress(job_id, "extracting", 0, 0)
        existing = {r["page"] for r in db.query(
            "SELECT DISTINCT page FROM paragraphs WHERE job_id=?", (job_id,))}
        new_pages = sel - existing
        if new_pages:
            # 显式选页时用户对范围负责,不再因 References 章节截断
            paras, pages_meta = await asyncio.to_thread(extract_pdf, job["file_path"], False)
            add = [p for p in paras if p.page in new_pages]
            if add:
                db.add_paragraphs(job_id, add)
            db.set_pages_meta(job_id, pages_meta)
        combined = (job.get("pages") or "").strip()
        db.set_job(job_id, pages=f"{combined},{pages_spec.strip()}" if combined
                   else pages_spec.strip())
        await run_job(job_id, on_progress, llm_key=llm_key)
    except Exception as e:  # noqa: BLE001
        db.set_job(job_id, status="failed", error=str(e)[:500])
        try:
            await on_progress(job_id, "failed", 0, 0)
        except Exception:  # pragma: no cover
            pass


async def run_job(job_id: int, on_progress: ProgressCb = _noop,
                  llm_key: str | None = None) -> None:
    job = db.get_job(job_id)
    if job is None:
        return
    engine = job["engine"]

    try:
        # ---- 阶段 1:提取(仅首次;重跑/续跑时段落已入库)----
        counts = db.job_counts(job_id)
        if counts["total"] == 0:
            db.set_job(job_id, status="extracting")
            await on_progress(job_id, "extracting", 0, 0)
            # 显式选页时用户对范围负责,不因 References 章节截断
            skip_refs = settings.skip_references and not (job.get("pages") or "").strip()
            paras, pages_meta = await asyncio.to_thread(
                extract_pdf, job["file_path"], skip_refs
            )
            sel = parse_pages(job.get("pages"))
            if sel:
                paras = [p for p in paras if p.page in sel]
            if not paras:
                db.set_job(job_id, status="failed", error="未能从 PDF 提取出文本(可能是扫描件)")
                return
            db.add_paragraphs(job_id, paras)
            db.set_pages_meta(job_id, pages_meta)
            counts = db.job_counts(job_id)

        total = counts["total"]
        db.set_job(job_id, status="translating")
        await on_progress(job_id, "translating", counts["done"], total)

        # ---- 阶段 2:并发翻译 pending 段落(请求级批量,失败回退逐段)----
        translator = get_translator(engine)
        translator.max_retries = job_retries(job_id)
        translator.api_key_override = llm_key  # 浏览器带来的 Key,仅内存使用
        sem = asyncio.Semaphore(settings.max_concurrency)
        done = counts["done"]

        async def translate_one(para: dict) -> None:
            nonlocal done
            marker, body = _split_marker(para["source"])
            key = cache_key(engine, settings.target_lang, settings.source_lang, body)
            cached = db.cache_get(key)
            if cached is not None:
                db.mark_para(para["id"], "ok",
                             translation=_postprocess(_join_marker(marker, cached), translator))
            else:
                try:
                    result = (await translator.translate_batch([body]))[0]
                    if not result or not result.strip():
                        raise TranslationError("空译文")
                    result = result.strip()
                    db.mark_para(para["id"], "ok",
                                 translation=_postprocess(marker + result, translator))
                    db.cache_put(key, result)  # 缓存只存不带编号的正文
                except Exception as e:  # noqa: BLE001 单段失败不影响整体
                    db.mark_para(para["id"], "failed", error=str(e)[:500])
            done += 1
            await on_progress(job_id, "translating", done, total)

        async def translate_group(group: list[dict]) -> None:
            """一组段落合并为一次引擎请求;批量失败回退逐段,隔离坏段。"""
            nonlocal done
            async with sem:
                todo: list[tuple[dict, str, str]] = []  # (para, 编号, 正文)
                for para in group:
                    marker, body = _split_marker(para["source"])
                    key = cache_key(engine, settings.target_lang,
                                    settings.source_lang, body)
                    cached = db.cache_get(key)
                    if cached is not None:
                        db.mark_para(para["id"], "ok",
                                     translation=_postprocess(_join_marker(marker, cached), translator))
                        done += 1
                        await on_progress(job_id, "translating", done, total)
                    else:
                        todo.append((para, marker, body))
                if not todo:
                    return
                try:
                    results = await translator.translate_batch(
                        [body for _, _, body in todo])
                    if len(results) != len(todo):
                        raise TranslationError("返回段数不符")
                    if any(not r or not r.strip() for r in results):
                        raise TranslationError("空译文")
                except Exception:  # noqa: BLE001 批量失败 → 逐段,保住单段隔离
                    await asyncio.gather(*(translate_one(p) for p, _, _ in todo))
                    return
                for (para, marker, _body), result in zip(todo, results):
                    result = result.strip()
                    db.mark_para(para["id"], "ok",
                                 translation=_postprocess(marker + result, translator))
                    db.cache_put(cache_key(engine, settings.target_lang,
                                           settings.source_lang, _body), result)
                    done += 1
                    await on_progress(job_id, "translating", done, total)

        while True:
            batch = db.pending_paragraphs(job_id, limit=64)
            if not batch:
                break
            groups = _group_requests(batch)
            await asyncio.gather(*(translate_group(g) for g in groups))

        final = db.job_counts(job_id)
        if final["failed"] and final["done"] == 0:
            db.set_job(job_id, status="failed",
                       error=f"全部 {final['failed']} 段翻译失败,请检查网络或更换引擎")
        elif final["failed"]:
            db.set_job(job_id, status="partial",
                       error=f"{final['failed']} 段失败,可重试续跑")
        else:
            db.set_job(job_id, status="done", error=None)
        await on_progress(job_id, "done" if final["failed"] == 0 else "partial",
                          final["done"], total)

    except Exception as e:  # noqa: BLE001 管线级失败:提取崩溃、引擎构造失败等
        db.set_job(job_id, status="failed", error=str(e)[:500])
        try:
            await on_progress(job_id, "failed", 0, 0)
        except Exception:  # pragma: no cover
            pass
