from __future__ import annotations

"""Disposable workspaces used by the resident Codex decision runner.

The decision Skill deliberately runs evidence collectors that write manifests
and content-addressed evidence.  Giving the model ``workspace-write`` access
to the live application checkout would make those writes (and any accidental
model edits) durable.  This module copies only the Skill inputs into a
run-scoped staging directory, so the Codex process can write while the real
application source, database files, and learning ledger remain outside its
workspace.
"""

from dataclasses import dataclass
import re
from pathlib import Path
import shutil
import tempfile


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# These are the immutable Skill inputs needed by the local evidence scripts.
# In particular, do not copy local-data/codex-home, postgres, runtime, or logs.
_ROOT_FILES = ("README.md", "SKILL.md", "requirements.txt")
_ROOT_DIRECTORIES = ("agents", "config", "docs", "references", "schemas", "scripts", "templates")
_STATE_DIRECTORIES = ("market-history", "trade-learning", "decision-ledger")


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    """Keep caches and transient Python artifacts out of every stage copy."""

    return {
        name
        for name in names
        if name in {".git", ".venv", ".pytest_cache", "__pycache__", "build"}
        or name.endswith(".pyc")
    }


@dataclass(slots=True)
class CodexWorkspace:
    """A disposable, self-contained Codex working tree for one strategy run."""

    source_root: Path
    root: Path
    run_dir: Path
    _temporary_parent: Path

    @classmethod
    def create(cls, source_root: Path, run_id: str) -> "CodexWorkspace":
        source = source_root.expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"Codex source workspace does not exist: {source}")
        if _SAFE_RUN_ID.fullmatch(run_id) is None:
            raise ValueError("strategy run id is not a safe workspace name")

        temporary_parent = Path(tempfile.mkdtemp(prefix=f"cfda-codex-{run_id}-")).resolve()
        stage_root = temporary_parent / "workspace"
        stage_root.mkdir()
        try:
            for relative in _ROOT_FILES:
                origin = source / relative
                if origin.is_file():
                    shutil.copy2(origin, stage_root / relative)
            for relative in _ROOT_DIRECTORIES:
                origin = source / relative
                if origin.is_dir():
                    shutil.copytree(
                        origin,
                        stage_root / relative,
                        ignore=_copy_ignore,
                    )

            state_root = stage_root / "local-data"
            state_root.mkdir()
            for relative in _STATE_DIRECTORIES:
                origin = source / "local-data" / relative
                destination = state_root / relative
                if origin.is_dir():
                    shutil.copytree(origin, destination, ignore=_copy_ignore)
                else:
                    destination.mkdir(parents=True, exist_ok=True)

            run_dir = state_root / "codex-runs" / run_id
            run_dir.mkdir(parents=True)
            return cls(
                source_root=source,
                root=stage_root,
                run_dir=run_dir,
                _temporary_parent=temporary_parent,
            )
        except Exception:
            # The target is the exact mkdtemp directory created above; cleanup
            # is intentionally bounded to that directory only.
            shutil.rmtree(temporary_parent, ignore_errors=True)
            raise

    @property
    def relative_run_dir(self) -> str:
        return self.run_dir.relative_to(self.root).as_posix()

    def cleanup(self) -> None:
        """Remove only this run's staging tree after Codex has exited."""

        shutil.rmtree(self._temporary_parent, ignore_errors=True)


__all__ = ["CodexWorkspace"]
