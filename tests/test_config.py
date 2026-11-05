from pathlib import Path

import pytest

from smartdesk.config import PROJECT_ROOT, load_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_REPOSITORY",
        "CHROMA_PERSIST_DIRECTORY", "OPENAI_CHAT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_credentials_are_allowed_offline(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env")
    assert settings.openai_api_key is None
    assert settings.chroma_persist_directory == PROJECT_ROOT / ".data/chroma"
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        settings.require_openai()


def test_secret_is_hidden_and_environment_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-secret")
    settings = load_settings(tmp_path / "missing.env")
    assert settings.require_openai().get_secret_value() == "synthetic-test-secret"
    assert "synthetic-test-secret" not in repr(settings)


def test_placeholder_key_is_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "replace_me")
    assert load_settings(tmp_path / "missing.env").openai_api_key is None


def test_dotenv_loads_without_overwriting_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=synthetic-file-secret\nOPENAI_CHAT_MODEL=synthetic-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-secret")
    settings = load_settings(env_path)
    assert settings.require_openai().get_secret_value() == "synthetic-environment-secret"
    assert settings.chat_model == "synthetic-model"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert load_settings(env_path).require_openai().get_secret_value() == "synthetic-file-secret"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OPENAI_CHAT_MODEL")


def test_invalid_repository_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "https://github.com/owner/repo")
    with pytest.raises(ValueError, match="owner/repository"):
        load_settings(tmp_path / "missing.env")