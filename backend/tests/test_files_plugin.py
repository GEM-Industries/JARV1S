from pathlib import Path

import pytest

from core.plugins.capabilities import CapabilityErrorDetail
from plugins import files as files_mod
from plugins.files import FilesPlugin


def _text(result) -> str:
    if isinstance(result, CapabilityErrorDetail):
        return result.message
    if hasattr(result, "content") and not hasattr(result, "code"):
        return result.content
    return str(result)


def test_resolve_relative_paths_under_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)

    assert files_mod._resolve("pendingInputD.test") == tmp_path / "pendingInputD.test"


@pytest.mark.asyncio
async def test_find_falls_back_when_spotlight_misses(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())

    target = tmp_path / "pendingInputD.test"
    target.write_text("Hello")

    class EmptyProc:
        async def communicate(self):
            return b"", b""

    async def fake_mdfind(*args, **kwargs):
        return EmptyProc()

    monkeypatch.setattr(files_mod.asyncio, "create_subprocess_exec", fake_mdfind)

    result = await FilesPlugin().find("pendingInputD.test", path=str(tmp_path))

    assert "~/pendingInputD.test" in result


@pytest.mark.asyncio
async def test_write_refuses_overwrite_without_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())
    existing = tmp_path / "notes.txt"
    existing.write_text("old")

    result = await FilesPlugin().write(str(existing), "new")
    assert "already exists" in _text(result)
    assert existing.read_text() == "old"

    ok = await FilesPlugin().write(str(existing), "new", overwrite=True)
    assert "Written" in _text(ok)
    assert existing.read_text() == "new"


@pytest.mark.asyncio
async def test_edit_requires_unique_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())
    path = tmp_path / "doc.txt"
    path.write_text("alpha beta alpha")

    missing = await FilesPlugin().edit(str(path), "gamma", "delta")
    assert "not found" in _text(missing).lower()

    multi = await FilesPlugin().edit(str(path), "alpha", "ALPHA")
    assert "unique" in _text(multi).lower() or "multiple" in _text(multi).lower() or "replace_all" in _text(multi)
    assert path.read_text() == "alpha beta alpha"

    ok = await FilesPlugin().edit(str(path), "alpha", "ALPHA", replace_all=True)
    assert "Edited" in _text(ok)
    assert path.read_text() == "ALPHA beta ALPHA"


@pytest.mark.asyncio
async def test_delete_is_consent_gated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())
    path = tmp_path / "doomed.txt"
    path.write_text("x")

    async def fake_consent(description, action, detail=""):
        return CapabilityErrorDetail(
            code="approval_needed",
            message=f"Approval needed: {description} The action has not executed yet.",
        )

    monkeypatch.setattr(files_mod, "require_consent", fake_consent)
    result = await FilesPlugin().delete(str(path))
    assert isinstance(result, CapabilityErrorDetail)
    assert result.code == "approval_needed"
    assert path.exists()


@pytest.mark.asyncio
async def test_sandbox_blocks_secret_dotdirs_but_allows_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())

    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("secret")
    (tmp_path / ".env").write_text("API_KEY=1")
    (tmp_path / ".zshrc").write_text("alias ll='ls -la'")

    assert "Access denied" in _text(await FilesPlugin().read(str(tmp_path / ".ssh" / "id_rsa")))
    assert "Access denied" in _text(await FilesPlugin().read(str(tmp_path / ".env")))
    # Ordinary config dotfiles are readable.
    assert "alias ll" in await FilesPlugin().read(str(tmp_path / ".zshrc"))


@pytest.mark.asyncio
async def test_sandbox_blocks_outside_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    result = await FilesPlugin().read("/etc/passwd")
    assert "Access denied" in _text(result)


@pytest.mark.asyncio
async def test_read_oversized_file_returns_first_page(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())
    monkeypatch.setattr(files_mod, "MAX_READ_BYTES", 200)

    path = tmp_path / "big.txt"
    path.write_text("\n".join(f"row-{i}" for i in range(50)))

    result = await FilesPlugin().read(str(path), offset=1, limit=5)
    assert "row-0" in result
    assert "row-4" in result
    assert "offset=6" in result or "grep()" in result


@pytest.mark.asyncio
async def test_read_truncates_a_single_huge_line(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(files_mod, "BLOCKED_PATHS", ())

    path = tmp_path / "one-line.txt"
    path.write_text("x" * (files_mod.MAX_LINE_CHARS * 2))

    result = await FilesPlugin().read(str(path))

    assert "chars truncated" in result
    assert len(result) < files_mod.MAX_LINE_CHARS + 500
