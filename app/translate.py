"""翻译引擎抽象与适配器。

统一接口 translate_batch(texts) -> list[str] | None(None 表示引擎不可用/未启用)。
- google: 谷歌翻译免费网页接口(translate.googleapis.com),无需 key;
- llm: OpenAI 兼容 Chat Completions,支持术语表 prompt,默认关闭;
- mock: 测试用,返回原文加标记。

所有适配器内部处理:分片(超长自动拆)、退避重试、请求间隔限速。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import subprocess
import urllib.parse
from pathlib import Path

import httpx

from . import db
from .config import settings

try:  # tomllib 在 3.11+,项目要求 3.12
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

# curl 回退 httpx 时用的浏览器 UA
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class TranslationError(Exception):
    pass


class _CurlUnavailable(Exception):
    """系统没有可用的 curl,需回退 httpx。"""


class Glossary:
    """术语表:原文 → 固定译名。LLM 引擎进 prompt;免费引擎译后替换。"""

    def __init__(self, entries: dict[str, str] | None = None):
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        if not path.is_file():
            return cls()
        if tomllib is None:  # pragma: no cover
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            return cls()
        entries = {str(k): str(v) for k, v in data.get("terms", {}).items()}
        return cls(entries)

    def apply(self, text: str) -> str:
        """译后替换:英文术语 → 固定译名(词边界匹配,大小写不敏感)。"""
        for en, zh in sorted(self.entries.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(
                r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])",
                lambda _m, zh=zh: zh,
                text,
                flags=re.IGNORECASE,
            )
        return text

    def prompt_block(self) -> str:
        if not self.entries:
            return ""
        lines = [f"- {en} => {zh}" for en, zh in self.entries.items()]
        return "\n".join(["术语表(必须严格遵守):", *lines])


def cache_key(engine: str, target_lang: str, source_lang: str, text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{engine}:{target_lang}:{source_lang}:{h}"


GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
GLM_MODEL = "glm-4-flash"


def llm_config(engine: str | None = None) -> dict:
    """LLM 运行配置:网页设置(库)优先,.env/env 兜底。

    engine="glm"(智谱预设)读取 glm_* 设置,未设置时回退通用 llm_* 配置,
    Base URL 与模型名内置默认值——用户只需填 API Key。
    """
    prefix = "glm" if engine == "glm" else "llm"

    def pick(name: str, fallback: str) -> str:
        v = db.get_setting(f"{prefix}_{name}")
        if v:
            return v
        if prefix == "glm":
            v = db.get_setting(f"llm_{name}")
            if v and name == "api_key":  # 仅 key 回退通用配置,地址/模型用内置预设
                return v
        return fallback

    if engine == "glm":
        return {
            "base_url": pick("base_url", GLM_BASE_URL),
            "api_key": pick("api_key", settings.llm_api_key),
            "model": pick("model", GLM_MODEL),
        }
    return {
        "base_url": pick("base_url", settings.llm_base_url),
        "api_key": pick("api_key", settings.llm_api_key),
        "model": pick("model", settings.llm_model),
    }


class BaseTranslator:
    name = "base"
    glossary: "Glossary"  # get_translator 统一注入
    max_chunk = 4000      # 单次请求的最大字符数
    max_retries = 4
    base_interval_ms: float  # 两次请求的最小间隔,由设置决定
    # 浏览器随请求携带的 Key(X-LLM-Key 头):只在内存中用一次,绝不落盘
    api_key_override: str | None = None

    def __init__(self) -> None:
        self.base_interval_ms = float(settings.request_interval_ms)
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self.base_interval_ms / 1000 - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = loop.time()

    async def _request(self, fn) -> httpx.Response:
        """带指数退避重试的 HTTP 请求。429/5xx/网络错误重试,4xx 直接失败。"""
        delay = 1.0
        last_err: Exception | None = None
        for _ in range(self.max_retries):
            await self._throttle()
            try:
                resp = await fn()
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_err = TranslationError(f"HTTP {resp.status_code}")
                else:
                    raise TranslationError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
            except (httpx.HTTPError, TranslationError) as e:
                last_err = e
            await asyncio.sleep(delay + random.random())
            delay *= 2
        raise TranslationError(f"重试耗尽: {last_err}")

    def _split(self, texts: list[str]) -> list[list[str]]:
        """把段落切成分片:优先按段落边界,超长段落内部按句子硬切。"""
        chunks: list[list[str]] = []
        cur: list[str] = []
        n = 0
        for t in texts:
            if len(t) > self.max_chunk:
                for piece in _hard_split(t, self.max_chunk):
                    if n + len(piece) + 1 > self.max_chunk and cur:
                        chunks.append(cur)
                        cur, n = [], 0
                    cur.append(piece)
                    n += len(piece) + 1
                continue
            if n + len(t) + 1 > self.max_chunk and cur:
                chunks.append(cur)
                cur, n = [], 0
            cur.append(t)
            n += len(t) + 1
        if cur:
            chunks.append(cur)
        return chunks

    async def translate_batch(self, texts: list[str]) -> list[str]:
        out: list[str] = []
        for chunk in self._split(texts):
            out.extend(await self._translate_chunk(chunk))
        return out

    async def _translate_chunk(self, texts: list[str]) -> list[str]:  # pragma: no cover
        raise NotImplementedError


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _hard_split(text: str, limit: int) -> list[str]:
    parts = _SENT_SPLIT_RE.split(text)
    out: list[str] = []
    for p in parts:
        while len(p) > limit:
            out.append(p[:limit])
            p = p[limit:]
        out.append(p)
    return [p for p in out if p]


class GoogleTranslator(BaseTranslator):
    """谷歌翻译免费网页接口(非官方)。

    传输层用系统 curl 子进程而非 httpx:该接口按 TLS 指纹拒绝 Python
    客户端(httpx 即使带浏览器 UA 也返回 429),curl 的 TLS 握手可通过。
    curl 不可用时回退 httpx(此时若被限流会报 429,由上层重试)。
    单请求约 5000 字符上限,取保守值。
    """
    name = "google"
    max_chunk = 4500

    URL = "https://translate.googleapis.com/translate_a/single"

    async def _translate_chunk(self, texts: list[str]) -> list[str]:
        sep = "\n@@PDT@@\n"
        joined = sep.join(texts)
        data = await self._fetch_json(joined)
        try:
            translated = "".join(part[0] for part in data[0])
        except (IndexError, TypeError) as e:
            raise TranslationError(f"谷歌接口响应异常: {e}") from e
        pieces = translated.split(sep)
        if len(pieces) != len(texts):
            # 分隔符被译文破坏:退化为逐段请求
            return await self._translate_one_by_one(texts)
        return pieces

    async def _translate_one_by_one(self, texts: list[str]) -> list[str]:
        out: list[str] = []
        for t in texts:
            data = await self._fetch_json(t)
            out.append("".join(part[0] for part in data[0]))
        return out

    async def _fetch_json(self, q: str) -> list:
        params = {
            "client": "gtx", "sl": settings.source_lang,
            "tl": settings.target_lang, "dt": "t", "q": q,
        }
        url = f"{self.URL}?{urllib.parse.urlencode(params)}"
        try:
            return await self._fetch_json_curl(url)
        except _CurlUnavailable:
            pass  # 无 curl(部分部署环境)→ httpx 回退
        client = httpx.AsyncClient(timeout=30, headers={"User-Agent": UA})
        try:
            resp = await self._request(lambda: client.get(self.URL, params=params))
            return resp.json()
        finally:
            await client.aclose()

    async def _fetch_json_curl(self, url: str) -> list:
        """curl 传输:带节流与指数退避重试;429/5xx/网络错误重试,其他 4xx 直接失败。"""
        delay = 1.0
        last_err: Exception | None = None
        for _ in range(self.max_retries):
            await self._throttle()
            try:
                code, body = await asyncio.to_thread(self._curl_get, url)
            except _CurlUnavailable:
                raise
            if code == 200 and body:
                try:
                    return json.loads(body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    last_err = TranslationError(f"谷歌接口响应异常: {e}")
            elif code == 429 or code >= 500 or code == 0:
                last_err = TranslationError(f"HTTP {code or '网络错误'}")
            else:
                raise TranslationError(f"HTTP {code}: {body[:200]!r}")
            await asyncio.sleep(delay + random.random())
            delay *= 2
        raise TranslationError(f"重试耗尽: {last_err}")

    def _curl_get(self, url: str) -> tuple[int, bytes]:
        """返回 (http_status, body);curl 不存在抛 _CurlUnavailable,网络失败返回 (0, b"")。"""
        try:
            proc = subprocess.run(
                ["curl", "-s", "--max-time", "30", "-w", "\n%{http_code}", url],
                capture_output=True,
            )
        except (FileNotFoundError, OSError):
            raise _CurlUnavailable from None
        if proc.returncode != 0:
            return 0, b""
        body, _, code = proc.stdout.rpartition(b"\n")
        try:
            return int(code.strip()), body
        except ValueError:
            return 0, proc.stdout


class LLMTranslator(BaseTranslator):
    """OpenAI 兼容 Chat Completions(DeepSeek/GLM/Kimi/中转均可)。默认关闭。"""
    name = "llm"
    max_chunk = 6000
    preset: str | None = None  # GLMTranslator 覆盖为 "glm",配置走 glm 预设

    def __init__(self, glossary: Glossary):
        super().__init__()
        self.glossary = glossary

    def _cfg(self) -> dict:
        cfg = llm_config(self.preset)
        if self.api_key_override:  # 请求携带的 Key 优先;不写入任何存储
            cfg = {**cfg, "api_key": self.api_key_override}
        return cfg

    def available(self) -> bool:
        return bool(self._cfg()["api_key"])

    async def _translate_chunk(self, texts: list[str]) -> list[str]:
        cfg = self._cfg()
        if not cfg["api_key"]:
            raise TranslationError(
                "未提供 AI Key:请在页面「AI 翻译设置」里填入并保存后重试")
        sep = "\n@@PDT@@\n"
        joined = sep.join(texts)
        sys_prompt = (
            f"你是专业翻译。把用户消息中的文本从"
            f"{settings.source_lang if settings.source_lang != 'auto' else '原文语言'}"
            f"翻译成 {settings.target_lang},只输出译文。"
            "保持段落分隔符 @@PDT@@ 原样。遇到数学公式、代码、URL 保持原样不译。"
        )
        glossary = self.glossary.prompt_block()
        if glossary:
            sys_prompt += "\n" + glossary
        body = {
            "model": cfg["model"],
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": joined},
            ],
        }
        headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        client = httpx.AsyncClient(timeout=120)
        try:
            resp = await self._request(
                lambda: client.post(
                    cfg["base_url"].rstrip("/") + "/chat/completions",
                    json=body, headers=headers,
                )
            )
        finally:
            await client.aclose()
        try:
            data = resp.json()
            translated = data["choices"][0]["message"]["content"].strip()
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise TranslationError(f"LLM 响应异常: {e}") from e
        pieces = [p.strip() for p in translated.split("@@PDT@@")]
        pieces = [p for p in pieces if p]
        if len(pieces) != len(texts):
            if len(texts) == 1 and pieces:
                # 模型把单段文本又拆成了多段:合并回一段(容错,避免整段报废)
                return [" ".join(pieces)]
            raise TranslationError(
                f"LLM 返回段数不符({len(pieces)}/{len(texts)}),已丢弃"
            )
        return pieces


class GLMTranslator(LLMTranslator):
    """智谱 GLM(OpenAI 兼容):glm-4-flash 免费档,地址/模型内置,只需填 key。"""
    name = "glm"
    preset = "glm"


class MockTranslator(BaseTranslator):
    """测试引擎:译文 = [mock] + 原文,零网络。"""
    name = "mock"
    max_chunk = 100000

    async def _translate_chunk(self, texts: list[str]) -> list[str]:
        return [f"[mock] {t}" for t in texts]


def get_translator(engine: str | None = None, glossary: Glossary | None = None):
    glossary = glossary or Glossary.load(settings.resolved_glossary_file)
    name = (engine or settings.engine).lower()
    if name == "google":
        t: BaseTranslator = GoogleTranslator()
    elif name == "glm":
        t = GLMTranslator(glossary)
    elif name == "llm":
        t = LLMTranslator(glossary)
    elif name == "mock":
        if not settings.enable_mock:
            raise TranslationError("mock 引擎未启用(设置 PDT_ENABLE_MOCK=1)")
        t = MockTranslator()
    else:
        raise TranslationError(f"未知引擎: {name}")
    t.glossary = glossary  # 免费引擎译后替换用;LLM 引擎已进 prompt
    return t
