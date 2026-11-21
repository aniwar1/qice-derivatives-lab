from __future__ import annotations

from pathlib import Path

import pytest

from app.codex_workspace import CodexWorkspace


def test_workspace_copies_skill_inputs_and_isolates_persistent_state(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("skill", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "scan_market.py").write_text("scan", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_universe.json").write_text("{}", encoding="utf-8")
    (tmp_path / "local-data" / "market-history" / "daily").mkdir(parents=True)
    (tmp_path / "local-data" / "market-history" / "daily" / "latest_manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "local-data" / "codex-home").mkdir(parents=True)
    (tmp_path / "local-data" / "codex-home" / "auth.json").write_text(
        "secret", encoding="utf-8"
    )
    (tmp_path / "local-data" / "postgres").mkdir(parents=True)
    (tmp_path / "local-data" / "postgres" / "base.dat").write_text(
        "database", encoding="utf-8"
    )

    workspace = CodexWorkspace.create(tmp_path, "run-123")
    try:
        assert workspace.root != tmp_path
        assert workspace.relative_run_dir == "local-data/codex-runs/run-123"
        assert (workspace.root / "SKILL.md").read_text(encoding="utf-8") == "skill"
        assert (workspace.root / "scripts" / "scan_market.py").is_file()
        assert (workspace.root / "local-data" / "market-history" / "daily" / "latest_manifest.json").is_file()
        assert not (workspace.root / "local-data" / "codex-home").exists()
        assert not (workspace.root / "local-data" / "postgres").exists()

        output = workspace.run_dir / "scan.json"
        output.write_text("{}", encoding="utf-8")
        assert output.read_text(encoding="utf-8") == "{}"
        assert not (tmp_path / "local-data" / "codex-runs").exists()
    finally:
        parent = workspace._temporary_parent
        workspace.cleanup()
        assert not parent.exists()


def test_workspace_rejects_unsafe_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="safe workspace"):
        CodexWorkspace.create(tmp_path, "../outside")
