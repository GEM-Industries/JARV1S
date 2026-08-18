import json

import pytest

from core.integrations import utterance_cache


async def read_file(path: str) -> str:
    """Read a text file from disk."""
    return path


@pytest.mark.asyncio
async def test_load_or_generate_uses_heuristic_without_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(utterance_cache, "_CACHE_DIR", tmp_path)

    utterances = await utterance_cache.load_or_generate(
        "files",
        "Sandboxed file system access.",
        {"read_file": read_file},
        llm_service=None,
    )

    assert utterances
    assert any("read file" in utterance.lower() for utterance in utterances)


@pytest.mark.asyncio
async def test_load_or_generate_ignores_stale_cache_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(utterance_cache, "_CACHE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "files.json").write_text(
        json.dumps({
            "version_hash": "old-version",
            "utterances": ["Read a project file"],
        })
    )

    utterances = await utterance_cache.load_or_generate(
        "files",
        "Sandboxed file system access.",
        {"read_file": read_file},
        llm_service=None,
    )

    assert utterances != ["Read a project file"]
    assert any("read file" in utterance.lower() for utterance in utterances)


@pytest.mark.asyncio
async def test_load_or_generate_allows_stale_cache_when_requested(monkeypatch, tmp_path):
    monkeypatch.setattr(utterance_cache, "_CACHE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "files.json").write_text(
        json.dumps({
            "version_hash": "old-version",
            "utterances": ["Read a project file"],
        })
    )

    utterances = await utterance_cache.load_or_generate(
        "files",
        "Sandboxed file system access.",
        {"read_file": read_file},
        llm_service=None,
        allow_stale_cache=True,
    )

    assert utterances == ["Read a project file"]


@pytest.mark.asyncio
async def test_load_or_generate_falls_back_to_heuristic_on_llm_error(monkeypatch, tmp_path):
    monkeypatch.setattr(utterance_cache, "_CACHE_DIR", tmp_path)

    class BrokenLLM:
        async def chat(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    utterances = await utterance_cache.load_or_generate(
        "files",
        "Sandboxed file system access.",
        {"read_file": read_file},
        llm_service=BrokenLLM(),
    )

    assert utterances
    assert any("read file" in utterance.lower() for utterance in utterances)
