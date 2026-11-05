import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    openai_api_key: SecretStr | None = field(repr=False)
    github_token: SecretStr | None = field(repr=False)
    github_repository: str
    chroma_persist_directory: Path
    chat_model: str

    def require_openai(self) -> SecretStr:
        if self.openai_api_key is None:
            raise ValueError("Set OPENAI_API_KEY locally before using live retrieval")
        return self.openai_api_key

    def require_github(self) -> SecretStr:
        if self.github_token is None:
            raise ValueError("Set GITHUB_TOKEN locally before using live ticket operations")
        return self.github_token


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(env_path if env_path is not None else PROJECT_ROOT / ".env", override=False)

    def secret(name: str) -> SecretStr | None:
        value = os.environ.get(name, "").strip()
        if not value or value.lower() in {"replace_me", "your_api_key_here", "your_token_here"}:
            return None
        return SecretStr(value)

    repository = os.environ.get("GITHUB_REPOSITORY", "ppratik03/smartdesk-ai").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must use owner/repository format")
    directory = os.environ.get("CHROMA_PERSIST_DIRECTORY", ".data/chroma").strip()
    model = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini").strip()
    if not directory or not model:
        raise ValueError("Persistence directory and chat model must not be blank")
    persist_directory = Path(directory)
    if not persist_directory.is_absolute():
        persist_directory = PROJECT_ROOT / persist_directory
    return Settings(
        openai_api_key=secret("OPENAI_API_KEY"),
        github_token=secret("GITHUB_TOKEN"),
        github_repository=repository,
        chroma_persist_directory=persist_directory,
        chat_model=model,
    )