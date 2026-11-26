from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODEL_", extra="ignore")

    ollama_url: str = "http://localhost:11434"
    chat_model: str = "qwen3.5:9b-q4_K_M"
    embed_model: str = "bge-m3"
    num_ctx: int = 8192
    num_predict: int = 512
    embed_num_gpu: int = 0
    keep_alive: str = "30m"
    timeout_s: float = 120
    embed_batch: int = Field(default=32, ge=1)
    chat_slots: int = Field(default=1, ge=1)
    chat_queue_max: int = Field(default=0, ge=0)  # 0 = unlimited
