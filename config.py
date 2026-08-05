"""Central configuration for the workspace MCP server.

All filesystem paths and runtime limits live here. Environment variables make
the same source tree usable both on the host and inside Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    project_root: Path = Path(
        os.getenv("MCP_PROJECT_ROOT", str(Path(__file__).resolve().parent))
    ).resolve()
    workspace_root: Path = Path(
        os.getenv("MCP_WORKSPACE_ROOT", "/opt/workspace")
    ).resolve()
    skills_dirname: str = os.getenv("MCP_SKILLS_DIRNAME", "skills")
    draft_dirname: str = os.getenv("MCP_DRAFT_DIRNAME", "draft")

    server_name: str = os.getenv("MCP_SERVER_NAME", "FinalTheory Writing Workspace")
    host: str = os.getenv("MCP_HOST", "0.0.0.0")
    port: int = int(os.getenv("MCP_PORT", "8765"))
    mcp_path: str = os.getenv("MCP_PATH", "/mcp")

    default_timeout_seconds: int = int(os.getenv("MCP_DEFAULT_TIMEOUT", "30"))
    max_timeout_seconds: int = int(os.getenv("MCP_MAX_TIMEOUT", "120"))
    max_output_chars: int = int(os.getenv("MCP_MAX_OUTPUT_CHARS", "50000"))
    max_read_chars: int = int(os.getenv("MCP_MAX_READ_CHARS", "200000"))
    restart_delay_seconds: float = float(os.getenv("MCP_RESTART_DELAY", "2"))

    @property
    def skills_root(self) -> Path:
        return (self.workspace_root / self.skills_dirname).resolve()

    @property
    def draft_root(self) -> Path:
        return (self.workspace_root / self.draft_dirname).resolve()


CONFIG = Config()
