from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil

from sqlalchemy import URL


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _default_workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sqlite_url(root: Path) -> URL:
    return URL.create("sqlite+aiosqlite", database=str(root / "local-data" / "qice.sqlite3"))


def _default_codex_bin() -> str:
    configured = os.getenv("CFDA_CODEX_BIN")
    if configured:
        return configured
    path_binary = shutil.which("codex")
    if path_binary:
        return path_binary
    resources = Path("/Applications/ChatGPT.app/Contents/Resources")
    app_binary = resources / "codex-cli/CodexCLI.app/Contents/MacOS/codex"
    return str(app_binary) if app_binary.is_file() else "codex"


@dataclass(frozen=True, slots=True)
class Settings:
    workspace_root: Path = field(default_factory=_default_workspace_root)
    database_url: str | URL | None = field(default=None, repr=False)
    codex_bin: str = field(default_factory=_default_codex_bin)
    codex_bin_explicit: bool = False
    codex_model: str | None = None
    codex_timeout_seconds: int = 900
    codex_start_timeout_seconds: int = 30
    sse_heartbeat_seconds: int = 15
    sse_poll_milliseconds: int = 500
    auto_refresh_if_empty: bool = False
    prefer_codex_sdk: bool = False
    require_chatgpt_auth: bool = True
    service_name: str = "qice-local"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.getenv("CFDA_WORKSPACE_ROOT", _default_workspace_root())).expanduser().resolve()
        model = os.getenv("CFDA_CODEX_MODEL") or None
        codex_timeout_seconds = _int_env(
            "CFDA_CODEX_TIMEOUT_SECONDS", 900, 30, 3600
        )
        codex_start_timeout_seconds = _int_env(
            "CFDA_CODEX_START_TIMEOUT_SECONDS", 30, 1, 3600
        )
        if codex_start_timeout_seconds > codex_timeout_seconds:
            raise ValueError(
                "CFDA_CODEX_START_TIMEOUT_SECONDS must not exceed "
                "CFDA_CODEX_TIMEOUT_SECONDS"
            )
        origins = tuple(
            value.strip()
            for value in os.getenv(
                "CFDA_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:4173,http://localhost:4173,http://127.0.0.1:8000,http://localhost:8000",
            ).split(",")
            if value.strip()
        )
        return cls(
            workspace_root=root,
            database_url=os.getenv("CFDA_DATABASE_URL"),
            codex_bin=_default_codex_bin(),
            codex_bin_explicit=bool(os.getenv("CFDA_CODEX_BIN")),
            codex_model=model,
            codex_timeout_seconds=codex_timeout_seconds,
            codex_start_timeout_seconds=codex_start_timeout_seconds,
            sse_heartbeat_seconds=_int_env("CFDA_SSE_HEARTBEAT_SECONDS", 15, 5, 60),
            sse_poll_milliseconds=_int_env("CFDA_SSE_POLL_MILLISECONDS", 500, 100, 5000),
            auto_refresh_if_empty=_bool_env("CFDA_AUTO_REFRESH_IF_EMPTY", False),
            prefer_codex_sdk=_bool_env("CFDA_PREFER_CODEX_SDK", False),
            require_chatgpt_auth=_bool_env("CFDA_REQUIRE_CHATGPT_AUTH", True),
            cors_origins=origins,
        )

    @property
    def storage_url(self) -> str | URL:
        return self.database_url or _sqlite_url(self.workspace_root)

    @property
    def model_label(self) -> str:
        return self.codex_model or "configured_default"
