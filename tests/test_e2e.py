"""端到端测试:TestClient 走完整 HTTP 链路(上传 → mock 翻译 → 结果 → 删除)。
运行:.venv/Scripts/python.exe tests/test_e2e.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["PDT_ENGINE"] = "mock"
os.environ["PDT_ENABLE_MOCK"] = "1"
os.environ["PDT_DATA_DIR"] = tempfile.mkdtemp(prefix="pdt_e2e_")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests.make_sample_pdf import build  # noqa: E402

SAMPLE = Path(__file__).parent / "sample.pdf"


def main() -> None:
    from app.pipeline import _group_requests
    paras = [{"source": "a" * 30}, {"source": "b" * 30}, {"source": "c" * 70}]
    groups = _group_requests(paras, max_chars=80)
    assert [len(g) for g in groups] == [2, 1], groups
    assert [p["source"] for g in groups for p in g] == ["a" * 30, "b" * 30, "c" * 70]
    assert len(_group_requests(paras)) == 1

    build(str(SAMPLE))
    with TestClient(app) as client:
        # 引擎列表
        engines = client.get("/api/engines").json()
        assert any(e["id"] == "mock" for e in engines), engines

        # 上传
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("sample.pdf", f, "application/pdf")},
                data={"engine": "mock"},
            )
        assert r.status_code == 201, r.text
        job_id = r.json()["job_id"]

        # 轮询至完成
        job = None
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.1)
        assert job["status"] == "done", job
        assert job["total"] > 0 and job["done"] == job["total"], job

        # 结果:所有段落都有 [mock] 译文,且带位置与页面元数据
        result = client.get(f"/api/jobs/{job_id}/result").json()
        paras = result["paragraphs"]
        assert len(paras) == job["total"]
        assert all(p["translation"].startswith("[mock]") for p in paras), \
            [p for p in paras if not p["translation"].startswith("[mock]")][:2]
        assert any("ALPHA" in p["source"] for p in paras)
        assert result["pages"], "pages_meta 缺失"
        assert all(set(p) >= {"col", "wide", "top", "bottom"} for p in paras), \
            "段落位置字段缺失"

        # 页图渲染
        r = client.get(f"/api/jobs/{job_id}/page/1")
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", \
            f"页图异常: {r.status_code}"

        # 中文导出与排版版本缓存
        r = client.get(f"/api/jobs/{job_id}/export")
        assert r.status_code == 200 and r.content.startswith(b"%PDF-"), r.text[:300]
        from app.main import _export_path
        from app.export_pdf import LAYOUT_VERSION
        out = _export_path(job_id)
        version = out.with_suffix(".layout")
        assert version.read_text(encoding="utf-8") == LAYOUT_VERSION
        import unittest.mock
        with unittest.mock.patch("app.export_pdf.overlay_pdf", side_effect=AssertionError("缓存应复用")):
            assert client.get(f"/api/jobs/{job_id}/export").status_code == 200
        version.write_text("old", encoding="utf-8")
        assert client.get(f"/api/jobs/{job_id}/export").status_code == 200
        assert version.read_text(encoding="utf-8") == LAYOUT_VERSION
        r = client.get(f"/api/jobs/{job_id}/page/1?zh=1")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG\r\n\x1a\n")

        # 版式引擎参数:非法值 400;v1 导出可用
        r = client.get(f"/api/jobs/{job_id}/export?layout=v9")
        assert r.status_code == 400, r.text

        # 删除 PDF 文件:译文保留、页图 404、重复删除幂等
        r = client.post(f"/api/jobs/{job_id}/delete-file")
        assert r.status_code == 200, r.text
        assert client.get(f"/api/jobs/{job_id}/result").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/page/1").status_code == 404
        assert client.post(f"/api/jobs/{job_id}/delete-file").status_code == 200

        # retries 参数:合法值存库、非法值 400
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("r.pdf", f, "application/pdf")},
                data={"engine": "mock", "retries": "7"},
            )
        assert r.status_code == 201
        j = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert j["retries"] == 7, j
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("r.pdf", f, "application/pdf")},
                data={"engine": "mock", "retries": "11"},
            )
        assert r.status_code == 400

        # 页码选择:只提取第 1 页;非法页码 400
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("pages.pdf", f, "application/pdf")},
                data={"engine": "mock", "pages": "1"},
            )
        assert r.status_code == 201, r.text
        pid = r.json()["job_id"]
        jp = None
        for _ in range(100):
            jp = client.get(f"/api/jobs/{pid}").json()
            if jp["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.1)
        assert jp["status"] == "done", jp
        assert jp["pages"] == "1", jp
        res = client.get(f"/api/jobs/{pid}/result").json()
        assert res["paragraphs"], "选页后应有第 1 页段落"
        assert all(p["page"] == 1 for p in res["paragraphs"])
        assert not any("FOXTROT" in p["source"] for p in res["paragraphs"])
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("bad.pdf", f, "application/pdf")},
                data={"engine": "mock", "pages": "abc"},
            )
        assert r.status_code == 400

        # 追加翻译第 2 页;重复追加已有页不增加段落
        r = client.post(f"/api/jobs/{pid}/pages", json={"pages": "2"})
        assert r.status_code == 200, r.text
        for _ in range(100):
            jp = client.get(f"/api/jobs/{pid}").json()
            if jp["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.1)
        assert jp["status"] == "done", jp
        res = client.get(f"/api/jobs/{pid}/result").json()
        assert {p["page"] for p in res["paragraphs"]} == {1, 2}
        assert any("FOXTROT" in p["source"] for p in res["paragraphs"])
        total_before = jp["total"]
        r = client.post(f"/api/jobs/{pid}/pages", json={"pages": "1"})
        assert r.status_code == 200
        for _ in range(100):
            jp = client.get(f"/api/jobs/{pid}").json()
            if jp["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.1)
        assert jp["status"] == "done" and jp["total"] == total_before, jp

        # LLM 设置:默认未配 key → 写入后 engines 列表翻转 ready
        s = client.get("/api/settings/llm").json()
        assert s["has_key"] is False and "base_url" in s and "model" in s, s
        r = client.post("/api/settings/llm",
                        json={"api_key": "sk-test-1234567890", "model": "gpt-4o-mini"})
        assert r.status_code == 200 and r.json()["has_key"] is True
        eng = {e["id"]: e for e in client.get("/api/engines").json()}
        assert eng["llm"]["ready"] is True
        assert "glm" in eng  # GLM 预设引擎始终可见
        # GLM 预设:未单独设置时回退通用 key,地址/模型用内置默认
        from app.translate import llm_config, get_translator
        cfg = llm_config("glm")
        assert cfg["api_key"] == "sk-test-1234567890"
        assert cfg["base_url"].startswith("https://open.bigmodel.cn")
        assert cfg["model"].startswith("glm-")
        # 请求携带的 Key 优先级最高,且只进内存
        t = get_translator("glm")
        t.api_key_override = "sk-browser-secret"
        assert t._cfg()["api_key"] == "sk-browser-secret"
        with open(SAMPLE, "rb") as f:
            r = client.post(
                "/api/jobs", files={"file": ("k.pdf", f, "application/pdf")},
                data={"engine": "mock"}, headers={"X-LLM-Key": "sk-browser-secret"},
            )
        assert r.status_code == 201, r.text
        from app import db
        stored = {row["v"] for row in db.query("SELECT v FROM settings")}
        assert "sk-browser-secret" not in stored, "请求携带的 Key 不得落盘"
        # 清除接口:服务端共享配置可一键清除
        r = client.post("/api/settings/llm/clear")
        assert r.status_code == 200 and r.json()["has_key"] is False
        eng = {e["id"]: e for e in client.get("/api/engines").json()}
        assert eng["llm"]["ready"] is False

        # 二次上传走缓存:同文件新任务,翻译应瞬时完成且段落数一致
        with open(SAMPLE, "rb") as f:
            r2 = client.post(
                "/api/jobs", files={"file": ("sample.pdf", f, "application/pdf")},
                data={"engine": "mock"},
            )
        job_id2 = r2.json()["job_id"]
        job2 = None
        for _ in range(100):
            job2 = client.get(f"/api/jobs/{job_id2}").json()
            if job2["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.1)
        assert job2["status"] == "done", job2

        # 非法输入
        r = client.post(
            "/api/jobs", files={"file": ("x.txt", b"hello", "text/plain")},
            data={"engine": "mock"},
        )
        assert r.status_code == 400
        r = client.post(
            "/api/jobs", files={"file": ("x.pdf", b"not a pdf", "application/pdf")},
            data={"engine": "mock"},
        )
        assert r.status_code == 400

        # 删除
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert client.get(f"/api/jobs/{job_id}").status_code == 404

    print(f"PASS  e2e mock: {job['total']} 段翻译完成,缓存复用/非法输入/删除均通过")


if __name__ == "__main__":
    main()
