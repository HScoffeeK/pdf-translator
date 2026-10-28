# PDF 翻译工具

本地运行的 PDF 整篇翻译工具:上传 PDF → 自动提取
翻译 → 浏览器里看**双语对照**结果。

## 快速开始

**零命令行部署**:

1. 从 GitHub 获取代码:仓库页绿色 **Code** 按钮 → **Download ZIP**,解压(不需要装 git);
2. **Windows**:双击 `start.bat`;**macOS/Linux**:终端里运行 `./start.sh`。

脚本会自动完成以下全部步骤,无需任何命令:

- 检测 Python;若未安装会**询问是否自动下载并安装**(Windows 仅装到当前用户、免管理员,约 26MB,自动配国内镜像)
- 创建虚拟环境并安装依赖(依赖清单更新时会自动补装,否则直接跳过)
- 启动服务并**自动打开浏览器** http://127.0.0.1:8765

> 从浏览器下载的脚本可能触发 Windows SmartScreen 提示,点"仍要运行"即可。
> 需能访问谷歌的网络(免费引擎经系统 curl 请求;Windows 10+/macOS/Linux 自带 curl)。

**手动方式**:

```bash
cd pdf-translator
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # Windows
# .venv/bin/pip install -r requirements.txt          # Linux/macOS

.venv\Scripts\python -m app                          # 启动,默认 http://127.0.0.1:8765
``` 

浏览器打开 http://127.0.0.1:8765 ,拖入 PDF 即可。结果页可切换"双语对照 / 仅译文",
Ctrl+P 可把结果页另存为 PDF。所有数据(上传的 PDF、数据库)都在 `data/` 目录。

## 翻译引擎

| 引擎 | 配置 | 说明 |
|---|---|---|
| `google` | 零配置 | 默认。免费网页接口|
| `glm` | 网页里填智谱 API Key(存你自己的浏览器) | **推荐**。智谱 GLM(OpenAI 兼容),`glm-4-flash` 免费;地址和模型已内置,只需填 Key(open.bigmodel.cn 注册即得) |
| `llm` | 网页"LLM 引擎配置"里填,或 `PDT_LLM_API_KEY` | 自定义 OpenAI 兼容服务:GPT/DeepSeek/Kimi/本地 Ollama 均可。术语表经 prompt 生效,质量最好 |
| `mock` | `PDT_ENABLE_MOCK=1` | 测试用 |

切换方式:上传时在页面下拉选择,或改 `.env` 里的 `PDT_ENGINE`。
LLM 的 Base URL / API Key / 模型名可直接在网页上填写保存(存 `data/app.db`),不用改配置文件。

### Key 不共享

- **Key 默认只保存在各用户自己的浏览器里**(localStorage),翻译时随请求
  (`X-LLM-Key` 头)带给服务端、仅在内存中使用一次,**不写入任何数据库或文件**。
- 服务端无 Key 时,AI 引擎(GLM/LLM)的任务会提示补 Key;Google 引擎不受影响。
- 因此**部署给多人是安全的**:别人用你的实例,消耗的是自己浏览器里的 Key,
  你的 Key 他们拿不到也用不了。每位使用者在页面"AI 翻译设置"里填自己的 Key 即可。
- 配置区的"清除"按钮会同时清掉浏览器里的 Key 和服务端可能存在的共享配置。

## 大文件:选页翻译

上传时在"页码范围"里填 `1-10,15` 可只译部分页(留空=全部);任务完成后在
"追加翻译页"里继续补译其余页(如 `11-20`),已译页自动跳过。中文版导出时,
未翻译的页保留英文原样。

## 配置

复制 `.env.example` 为 `.env` 按需修改;每项均可用 `PDT_` 前缀的环境变量覆盖。常用:

- `PDT_ENGINE` 默认引擎;`PDT_TARGET_LANG` 目标语言(默认 zh-CN)
- `PDT_LLM_BASE_URL` / `PDT_LLM_API_KEY` / `PDT_LLM_MODEL` — LLM 引擎
- `PDT_MAX_CONCURRENCY` 并发段数;`PDT_REQUEST_INTERVAL_MS` 请求最小间隔
- `PDT_SKIP_REFERENCES` 遇 References 章节停止(默认开)
- `PDT_API_TOKEN` 部署到公网时设置,请求需带 `Authorization: Bearer <token>`

术语表:编辑 `glossary.toml`,格式 `"原文" = "固定译名"`(每行一条)。

## 数据

任务、段落、翻译缓存都存在 `data/app.db`(SQLite)。重跑失败任务时已译段落不会重复请求引擎。删除任务会同时删除上传的文件。

## 部署(预留)

架构已为部署做好准备:前后端严格分离(前端只走 `/api/*`)、配置全外置、
状态入库。部署时:

```bash
XNOROS_HOST=0.0.0.0 PDT_API_TOKEN=某长随机串 .venv/bin/python -m app
```

前端为纯静态文件,可交给 nginx;后端可 `uvicorn app.main:app` 多进程。
SQLite 换 Postgres 只需改 `app/db.py` 一层。Docker 部署:

```bash
docker build -t pdf-translator .
docker run -d -p 8765:8765 -v pdf-translator-data:/srv/app/data pdf-translator
```

镜像内已装 curl(谷歌引擎需要);数据在 `pdf-translator-data`。

## 导出中文版 PDF(两种版式引擎)

任务完成后在页面选择版式引擎,再点"下载中文版 PDF":

| 引擎 | 原理 | 适合 |
|---|---|---|
| **智能重排 v2(试验,默认不选)** | [pdf2zh/babeldoc](https://github.com/Byaidu/PDFMathTranslate) 做版面分析(标题/正文/公式/表格/双栏),再在块内重排中文 | **部分文档**:数学/多栏/复杂版式通常更可靠，但不同字体可能触发公式识别误判;失败会拒绝发布不完整 PDF |
| 原位覆写 v1 | 覆盖原文,按提取段落几何原位写中文 | 当前默认,适合版式简单或需要即时可用的文档 |

v2 的翻译**仍走本项目引擎**(curl 谷歌 / GLM / LLM + 用户 Key),复用同一份
翻译缓存,不会重复消耗额度。首次运行会下载约 75MB 版面模型到
`~/.cache/babeldoc/models/`(国内可从 ModelScope 手动放置同名文件)。

v2 的自动降级:先用默认公式字体规则(学术论文的公式原样保留);若文本块翻译失败或公式占位符被改写,v2 不会发布不完整 PDF,而是报错让你切换引擎/重试。

导出文件在 `data/exports/`,文件名 `<原文件名>_中文版.pdf`(v2 带 `_重排版` 后缀)。

## 已知限制

- 扫描件(图片型 PDF)提取不出文本,会明确报错提示
- 图片型公式不翻译
- 谷歌接口为非官方通道,若整体被限流,页面报错后切换 GLM/LLM 引擎即可
- v2 若提示"未能提取可译文本",说明该 PDF 文字被判为公式/图片,改用 v1 引擎
