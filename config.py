"""Central configuration for the workspace MCP server.

Stable application policy lives here as constants. Environment variables are
reserved for values that genuinely vary between machines or deployments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

SOURCE_ROOT = Path(__file__).resolve().parent
load_dotenv(SOURCE_ROOT / ".env", override=False)


def _env_optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


@dataclass(frozen=True)
class Config:
    project_root: Path = SOURCE_ROOT
    workspace_root: Path = Path(
        os.getenv("MCP_WORKSPACE_ROOT", "/opt/workspace")
    ).resolve()
    additional_root: Path | None = _env_optional_path("MCP_ADDITIONAL_ROOT")
    skills_dirname: str = "skills"
    draft_dirname: str = "draft"
    tasks_dirname: str = ".mcp-tasks"
    temp_dirname: str = "temp"
    draft_diff_page: Path = SOURCE_ROOT / "web" / "draft_diff.html"
    draft_commit_password: str = os.getenv("MCP_DRAFT_COMMIT_PASSWORD", "1994.2.21")
    git_locale: str = "C.utf8"
    git_word_diff_regex: str = "[[:alnum:]_]+|[^[:space:]]"
    draft_history_limit: int = 20

    server_name: str = "FinalTheory Writing Workspace"
    host: str = "0.0.0.0"
    port: int = int(os.getenv("MCP_PORT", "8765"))
    mcp_path: str = "/mcp"

    default_timeout_seconds: int = 30
    max_timeout_seconds: int = 120
    max_output_chars: int = 50_000
    max_read_chars: int = 200_000
    max_patch_chars: int = 2_000_000
    max_list_entries: int = 2_000
    max_search_results: int = 500
    max_search_file_bytes: int = 2_000_000
    default_background_timeout_seconds: int = 3_600
    max_background_timeout_seconds: int = 86_400
    task_default_wait_seconds: int = 30
    task_max_wait_seconds: int = 60
    task_retention_days: int = 30
    execution_home: str = os.getenv("MCP_EXECUTION_HOME", "")
    restart_delay_seconds: float = 2.0

    chatgpt_automation_enabled: bool = _env_bool(
        "MCP_CHATGPT_AUTOMATION_ENABLED", False
    )
    chatgpt_url: str = "https://chatgpt.com/?temporary-chat=true"
    chatgpt_browser_channel: str = os.getenv("MCP_CHATGPT_BROWSER_CHANNEL", "")
    chatgpt_browser_executable: str = os.getenv("MCP_CHATGPT_BROWSER_EXECUTABLE", "")
    chatgpt_browser_headless: bool = _env_bool("MCP_CHATGPT_BROWSER_HEADLESS", False)
    chatgpt_browser_no_sandbox: bool = _env_bool(
        "MCP_CHATGPT_BROWSER_NO_SANDBOX", False
    )
    chatgpt_profile_dir: Path = SOURCE_ROOT / "chatgpt-profile"
    chatgpt_prompt_file: Path = SOURCE_ROOT / "prompts" / "chatgpt_subagent.md"
    chatgpt_browser_timeout_seconds: int = 30
    chatgpt_completion_timeout_seconds: int = 3_600
    chatgpt_completion_sentinel: str = "WRITERSUBAGENTCOMPLETE7D3A9F6C"

    @property
    def skills_root(self) -> Path:
        return (self.workspace_root / self.skills_dirname).resolve()

    @property
    def draft_root(self) -> Path:
        return (self.workspace_root / self.draft_dirname).resolve()

    @property
    def tasks_root(self) -> Path:
        return (self.workspace_root / self.tasks_dirname).resolve()

    @property
    def temp_root(self) -> Path:
        return (self.workspace_root / self.temp_dirname).resolve()


CONFIG = Config()
