from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_CHAT_MODEL = "qwen3.5:9b-q4_K_M"
DEFAULT_EMBED_MODEL = "bge-m3"


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODEL_", extra="ignore", populate_by_name=True)

    ollama_url: str = DEFAULT_OLLAMA_URL
    chat_model: str = Field(
        default=DEFAULT_CHAT_MODEL,
        validation_alias=AliasChoices("MODEL_CHAT_MODEL", "MODEL_CHAT"),
    )
    embed_model: str = Field(
        default=DEFAULT_EMBED_MODEL,
        validation_alias=AliasChoices("MODEL_EMBED_MODEL", "MODEL_EMBED"),
    )
    num_ctx: int = 8192
    num_predict: int = 512
    embed_num_gpu: int = 0
    keep_alive: str = "30m"
    timeout_s: float = 120
    generation_max_waiters: int = Field(default=3, ge=0)
    embed_batch: int = Field(default=32, ge=1)
