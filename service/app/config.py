from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    vision_provider: str = "gemini"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llava:7b"

    grocy_base_url: str = "http://grocy:80"
    grocy_api_key: str = ""

    data_dir: str = "/app/data"
    secret_key: str = "change_me"

    class Config:
        env_file = ".env"


settings = Settings()
