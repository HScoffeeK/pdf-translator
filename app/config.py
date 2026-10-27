"""集中配置:默认值 + .env + PDT_ 前缀环境变量,三层覆盖,便于容器化部署。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # 服务
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: str = ""
    api_token: str = ""

    # 翻译
    engine: str = "google"
    target_lang: str = "zh-CN"
    source_lang: str = "auto"
    glossary_file: str = "glossary.toml"
    skip_references: bool = True

    # LLM 引擎(OpenAI 兼容)
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # 限流
    max_concurrency: int = 4
    request_interval_ms: int = 200
    max_retries: int = 4  # 每段翻译的重试次数(1~10,任务可单独覆盖)

    # 其他
    max_upload_mb: int = 50
    enable_mock: bool = False

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PDT_", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).resolve() if self.data_dir else BASE_DIR / "data"

    @property
    def resolved_glossary_file(self) -> Path:
        p = Path(self.glossary_file)
        return p if p.is_absolute() else BASE_DIR / p


settings = Settings()
