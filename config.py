"""Central configuration for the workspace MCP server.

All filesystem paths and runtime limits live here. Environment variables make
the same source tree usable both on the host and inside Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


SOURCE_ROOT = Path(__file__).resolve().parent
load_dotenv(SOURCE_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Config:
    project_root: Path = Path(
        os.getenv("MCP_PROJECT_ROOT", str(SOURCE_ROOT))
    ).resolve()
    workspace_root: Path = Path(
        os.getenv("MCP_WORKSPACE_ROOT", "/opt/workspace")
    ).resolve()
    skills_dirname: str = os.getenv("MCP_SKILLS_DIRNAME", "skills")
    draft_dirname: str = os.getenv("MCP_DRAFT_DIRNAME", "draft")
    tasks_dirname: str = os.getenv("MCP_TASKS_DIRNAME", ".mcp-tasks")
    draft_diff_page: Path = Path(
        os.getenv("MCP_DRAFT_DIFF_PAGE", str(SOURCE_ROOT / "web" / "draft_diff.html"))
    ).resolve()
    draft_commit_password: str = os.getenv("MCP_DRAFT_COMMIT_PASSWORD", "1994.2.21")
    git_locale: str = os.getenv("MCP_GIT_LOCALE", "C.utf8")
    git_word_diff_regex: str = os.getenv(
        "MCP_GIT_WORD_DIFF_REGEX", "[[:alnum:]_]+|[^[:space:]]"
    )
    draft_history_limit: int = int(os.getenv("MCP_DRAFT_HISTORY_LIMIT", "20"))

    server_name: str = os.getenv("MCP_SERVER_NAME", "FinalTheory Writing Workspace")
    host: str = os.getenv("MCP_HOST", "0.0.0.0")
    port: int = int(os.getenv("MCP_PORT", "8765"))
    mcp_path: str = os.getenv("MCP_PATH", "/mcp")

    default_timeout_seconds: int = int(os.getenv("MCP_DEFAULT_TIMEOUT", "30"))
    max_timeout_seconds: int = int(os.getenv("MCP_MAX_TIMEOUT", "120"))
    max_output_chars: int = int(os.getenv("MCP_MAX_OUTPUT_CHARS", "50000"))
    max_read_chars: int = int(os.getenv("MCP_MAX_READ_CHARS", "200000"))
    max_patch_chars: int = int(os.getenv("MCP_MAX_PATCH_CHARS", "2000000"))
    default_background_timeout_seconds: int = int(
        os.getenv("MCP_DEFAULT_BACKGROUND_TIMEOUT", "21600")
    )
    max_background_timeout_seconds: int = int(
        os.getenv("MCP_MAX_BACKGROUND_TIMEOUT", "86400")
    )
    task_log_tail_chars: int = int(os.getenv("MCP_TASK_LOG_TAIL_CHARS", "10000"))
    execution_home: str = os.getenv("MCP_EXECUTION_HOME", "")
    restart_delay_seconds: float = float(os.getenv("MCP_RESTART_DELAY", "2"))

    @property
    def skills_root(self) -> Path:
        return (self.workspace_root / self.skills_dirname).resolve()

    @property
    def draft_root(self) -> Path:
        return (self.workspace_root / self.draft_dirname).resolve()

    @property
    def tasks_root(self) -> Path:
        return (self.workspace_root / self.tasks_dirname).resolve()


CONFIG = Config()
