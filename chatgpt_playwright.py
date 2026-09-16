"""Playwright adapter for sending one prompt to ChatGPT Web."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from time import monotonic, sleep
from typing import Any, Callable, Iterator

sys.dont_write_bytecode = True
from urllib.parse import urlparse

COMPOSER_SELECTORS = (
    '#prompt-textarea[contenteditable="true"]',
    '[contenteditable="true"][data-testid="composer-input"]',
)
SEND_BUTTON_SELECTORS = (
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send message"]',
    'button[aria-label="发送提示"]',
    'button[aria-label="发送消息"]',
)
IGNORED_CHROME_DEFAULT_ARGS = ("--use-mock-keychain",)
DEBUG_UI_HOLD_FILE = Path("/tmp/mymcp-debug-ui/hold-browser-open")
DEBUG_UI_HOLD_SECONDS = 60
SUBAGENT_STARTED_SENTINEL = "WRITERSUBAGENTSTARTED4C81E2B5"
SUBAGENT_COMPLETED_SENTINEL = "WRITERSUBAGENTCOMPLETE7D3A9F6C"
SUBAGENT_START_TIMEOUT_SECONDS = 300
BROWSER_TASK_CONCURRENCY = 10
TASK_PROFILES_ROOT = Path(__file__).resolve().parent / "chatgpt-task-profiles"


class ChatGPTPreSendError(RuntimeError):
    """Known-unsent browser failure that may be retried safely."""


class ChatGPTRateLimitError(RuntimeError):
    """Platform rate-limit UI blocked a known-unsent prompt."""


class ChatGPTPostSendError(RuntimeError):
    """Failure after Send that must never trigger another submission."""



@contextmanager
def _browser_profile_lock(profile_dir: Path) -> Iterator[None]:
    """Serialize process-level access to one persistent Chromium profile."""
    lock_path = profile_dir / ".send.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _clone_browser_profile(source: Path, destination: Path) -> None:
    """Copy authenticated profile state without volatile browser caches."""
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for filename in ("Local State", "first_party_sets.db"):
        candidate = source / filename
        if candidate.is_file():
            shutil.copy2(candidate, destination / filename)

    ignored_profile_entries = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "GrShaderCache",
        "ShaderCache",
    }

    def ignore_profile_cache(_directory: str, names: list[str]) -> set[str]:
        return set(names) & ignored_profile_entries

    for candidate in source.iterdir():
        if not candidate.is_dir() or not (candidate / "Preferences").is_file():
            continue
        shutil.copytree(
            candidate,
            destination / candidate.name,
            ignore=ignore_profile_cache,
        )


@contextmanager
def _browser_task_profile(source: Path) -> Iterator[Path]:
    """Lease one of ten bounded repo-local profile slots."""
    TASK_PROFILES_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        for index in range(BROWSER_TASK_CONCURRENCY):
            slot_dir = TASK_PROFILES_ROOT / f"slot_{index:02d}"
            slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_handle = (slot_dir / ".lock").open("a+b")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_handle.close()
                continue

            profile_dir = slot_dir / "profile"
            try:
                if profile_dir.exists():
                    shutil.rmtree(profile_dir)
                with _browser_profile_lock(source):
                    _clone_browser_profile(source, profile_dir)
                yield profile_dir
            finally:
                if profile_dir.exists():
                    shutil.rmtree(profile_dir)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            return
        sleep(0.2)


def render_subagent_prompt(
    template_file: Path,
    input_path: str,
    output_path: str,
) -> str:
    """Replace task paths and fixed lifecycle markers in the prompt template."""
    if not template_file.is_file():
        raise FileNotFoundError(
            f"ChatGPT sub-agent prompt is unavailable: {template_file}"
        )
    template = template_file.read_text(encoding="utf-8")
    placeholders = {
        "{{INPUT_PATH}}": json.dumps(input_path, ensure_ascii=False),
        "{{OUTPUT_PATH}}": json.dumps(output_path, ensure_ascii=False),
        "{{STARTED_SENTINEL}}": SUBAGENT_STARTED_SENTINEL,
        "{{COMPLETION_SENTINEL}}": SUBAGENT_COMPLETED_SENTINEL,
    }
    for placeholder, value in placeholders.items():
        actual_count = template.count(placeholder)
        if actual_count != 1:
            raise ValueError(
                f"prompt template must contain {placeholder} exactly once; found {actual_count}"
            )
        template = template.replace(placeholder, value)
    return template


def normalize_workspace_relative_path(relative_path: str) -> str:
    """Normalize a remote workspace path without consulting the local filesystem."""
    if not relative_path or "\0" in relative_path or "\\" in relative_path:
        raise ValueError("path must be a non-empty POSIX workspace-relative path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must not be absolute or escape the workspace")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("path must identify a workspace file")
    return normalized


def validate_subagent_path_pair(
    input_path: str, output_path: str, tasks_dirname: str
) -> str:
    """Validate one task-owned .mcp-tasks/task_<uid>/input-output pair."""
    task_parts = PurePosixPath(tasks_dirname).parts
    input_parts = PurePosixPath(input_path).parts
    output_parts = PurePosixPath(output_path).parts
    prefix_length = len(task_parts)
    expected_length = prefix_length + 2
    if (
        input_parts[:prefix_length] != task_parts
        or output_parts[:prefix_length] != task_parts
        or len(input_parts) != expected_length
        or len(output_parts) != expected_length
        or input_parts[-1] != "input.md"
        or output_parts[-1] != "output.md"
        or input_parts[-2] != output_parts[-2]
        or not re.fullmatch(r"task_[0-9a-f]{32}", input_parts[-2])
    ):
        raise ValueError(
            "paths must be one internally allocated .mcp-tasks/task_<uid>/"
            "input.md and output.md pair"
        )
    return input_parts[-2]


def normalize_workspace_file(
    workspace_root: Path, relative_path: str, *, must_exist: bool
) -> Path:
    """Resolve one CLI path without allowing it to leave the workspace."""
    workspace_root = workspace_root.resolve()
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("path must be a non-empty path relative to the workspace")
    resolved = (workspace_root / relative_path).resolve()
    if resolved != workspace_root and workspace_root not in resolved.parents:
        raise ValueError("path escapes the workspace")
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(relative_path)
    if resolved.exists() and not resolved.is_file():
        raise ValueError("path exists and is not a file")
    return resolved


def _visible_locator(page: Any, selectors: tuple[str, ...], timeout_ms: int) -> Any:
    deadline = monotonic() + timeout_ms / 1000
    while monotonic() < deadline:
        for selector in selectors:
            candidates = page.locator(selector)
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    return candidate
        page.wait_for_timeout(150)
    return None


def _find_composer(page: Any, timeout_ms: int) -> Any:
    return _visible_locator(page, COMPOSER_SELECTORS, timeout_ms)


def _rate_limit_modal_visible(page: Any) -> bool:
    try:
        modal = page.locator('[data-testid="modal-conversation-history-rate-limit"]')
        return modal.count() > 0 and modal.first.is_visible()
    except Exception:
        return False


def _click_verified_send_button(
    page: Any, markers: tuple[str, ...], timeout_ms: int
) -> bool:
    deadline = monotonic() + timeout_ms / 1000
    while monotonic() < deadline:
        button = _visible_locator(page, SEND_BUTTON_SELECTORS, 250)
        if button is not None and button.is_enabled():
            composer = _find_composer(page, 250)
            if composer is None:
                return False
            text = composer.inner_text()
            if any(marker not in text for marker in markers):
                return False
            if _rate_limit_modal_visible(page):
                raise ChatGPTRateLimitError(
                    "ChatGPT rate-limit modal blocked Send before submission"
                )
            button.click()
            return True
        page.wait_for_timeout(150)
    return False


def _fill_verified_prompt(
    page: Any,
    prompt: str,
    markers: tuple[str, ...],
    timeout_ms: int,
) -> None:
    """Fill the live editor and refuse to send if a page remount loses the prompt."""
    deadline = monotonic() + timeout_ms / 1000
    missing = list(markers)
    for _ in range(3):
        remaining_ms = round((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        composer = _find_composer(page, remaining_ms)
        if composer is None:
            break
        composer.fill(prompt)
        page.wait_for_timeout(400)
        current = _find_composer(page, min(1000, max(1, remaining_ms)))
        if current is None:
            continue
        text = current.inner_text()
        missing = [marker for marker in markers if marker not in text]
        if not missing:
            return
    raise ChatGPTPreSendError(
        "ChatGPT composer lost the delegated prompt before sending; "
        f"missing markers: {missing}"
    )


def _is_temporary_chat_url(url: str) -> bool:
    query = urlparse(url).query
    return any(
        key == "temporary-chat" and value.lower() == "true"
        for key, _, value in (part.partition("=") for part in query.split("&"))
    )


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        raise ValueError(f"completion path is not a file: {path}")
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _file_ends_with_sentinel(path: Path, sentinel: str) -> bool:
    encoded = sentinel.encode("utf-8")
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - len(encoded) - 1024))
        tail = handle.read()
    return tail.rstrip().endswith(encoded)


def _wait_for_file_completion(
    path: Path,
    baseline: tuple[int, int, int] | None,
    timeout_seconds: int,
    sentinel: str,
) -> float:
    started = monotonic()
    deadline = started + timeout_seconds
    while monotonic() < deadline:
        signature = _file_signature(path)
        if (
            signature is not None
            and signature != baseline
            and _file_ends_with_sentinel(path, sentinel)
        ):
            return monotonic() - started
        sleep(1)
    raise TimeoutError(
        "output file was not updated with the completion sentinel within "
        f"{timeout_seconds} seconds: {path}"
    )


def send_prompt(
    prompt: str,
    *,
    url: str,
    profile_dir: Path,
    browser_channel: str,
    headless: bool,
    timeout_seconds: int,
    verification_markers: tuple[str, ...],
    started_wait: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Send one verified prompt with bounded retries only before Send."""
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not _is_temporary_chat_url(url):
        raise ValueError("ChatGPT automation requires a temporary-chat=true URL")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    if not verification_markers or any(not marker for marker in verification_markers):
        raise ValueError("verification_markers must contain non-empty values")

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright is not installed; run: "
            "python -m pip install -r requirements-playwright.txt"
        ) from error

    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass

    timeout_ms = timeout_seconds * 1000
    attempts = 3
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        click_may_have_committed = False
        try:
            with _browser_task_profile(profile_dir) as attempt_profile:
                with sync_playwright() as playwright:
                    options: dict[str, Any] = {
                        "user_data_dir": str(attempt_profile),
                        "headless": headless,
                        "ignore_default_args": list(IGNORED_CHROME_DEFAULT_ARGS),
                    }
                    if browser_channel:
                        options["channel"] = browser_channel
                    context = playwright.chromium.launch_persistent_context(**options)
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        _fill_verified_prompt(page, prompt, verification_markers, timeout_ms)
                        if not _is_temporary_chat_url(page.url):
                            raise ChatGPTPreSendError(
                                "ChatGPT left Temporary Chat before sending"
                            )
                        if _rate_limit_modal_visible(page):
                            raise ChatGPTRateLimitError(
                                "ChatGPT rate-limit modal blocked Send before submission"
                            )

                        # From this point onward, a browser failure is ambiguous: the
                        # click may already have reached ChatGPT. Never auto-resend.
                        click_may_have_committed = True
                        clicked = _click_verified_send_button(
                            page, verification_markers, min(timeout_ms, 10000)
                        )
                        if not clicked:
                            click_may_have_committed = False
                            raise ChatGPTPreSendError(
                                "ChatGPT prompt changed or its send button was unavailable"
                            )

                        result: dict[str, Any] = {
                            "status": "sent",
                            "page_url": page.url,
                            "send_method": "button",
                        }
                        if started_wait is not None:
                            try:
                                result["started_wait_seconds"] = round(
                                    started_wait(), 3
                                )
                            except Exception as error:
                                raise ChatGPTPostSendError(
                                    f"sub-agent did not acknowledge start after Send: {error}"
                                ) from error
                        if DEBUG_UI_HOLD_FILE.is_file():
                            result["debug_hold_seconds"] = DEBUG_UI_HOLD_SECONDS
                            for _ in range(DEBUG_UI_HOLD_SECONDS):
                                if not DEBUG_UI_HOLD_FILE.is_file():
                                    break
                                page.wait_for_timeout(1000)
                        return result
                    finally:
                        try:
                            context.close()
                        except PlaywrightError:
                            if not click_may_have_committed:
                                raise
        except (ChatGPTRateLimitError, ChatGPTPostSendError):
            raise
        except ChatGPTPreSendError as error:
            last_error = error
        except PlaywrightError as error:
            if click_may_have_committed:
                return {
                    "status": "sent_ambiguous",
                    "page_url": url,
                    "send_method": "button",
                    "warning": f"browser failed after Send may have started: {error}",
                }
            last_error = ChatGPTPreSendError(
                f"Playwright could not automate ChatGPT before Send: {error}"
            )
        except Exception as error:
            if click_may_have_committed:
                return {
                    "status": "sent_ambiguous",
                    "page_url": url,
                    "send_method": "button",
                    "warning": f"automation failed after Send may have started: {error}",
                }
            raise

        if attempt < attempts:
            sleep(min(2.0, 0.5 * attempt))

    raise RuntimeError(
        f"ChatGPT pre-send automation failed after {attempts} attempts: {last_error}"
    ) from last_error


def _find_chrome_executable(configured: str) -> Path:
    if configured:
        executable = Path(configured).expanduser().resolve()
        if not executable.is_file():
            raise FileNotFoundError(
                f"configured Chrome executable does not exist: {executable}"
            )
        return executable

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.append(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        )
    elif sys.platform == "win32":
        for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if value := os.getenv(root):
                candidates.append(Path(value) / "Google/Chrome/Application/chrome.exe")
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            if found := shutil.which(name):
                candidates.append(Path(found))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            bundled_chromium = Path(playwright.chromium.executable_path)
        if bundled_chromium.is_file():
            return bundled_chromium.resolve()
    except ImportError:
        pass
    raise FileNotFoundError(
        "Chrome or Playwright Chromium was not found; "
        "set MCP_CHATGPT_BROWSER_EXECUTABLE"
    )


def open_login_browser(
    *, url: str, profile_dir: Path, browser_executable: str, no_sandbox: bool
) -> None:
    """Open ordinary Chrome so login is not performed under Playwright control."""
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    executable = _find_chrome_executable(browser_executable)
    print(
        "A normal Chrome window will open with the dedicated MCP profile. "
        "Log in to ChatGPT, then close that Chrome window to finish setup."
    )
    command = [
        str(executable),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if no_sandbox:
        command.append("--no-sandbox")
    command.append(url)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Chrome login process exited with code {result.returncode}")


def send_subagent_task(
    input_path: str,
    output_path: str,
    *,
    wait_for_completion: bool,
) -> dict[str, Any]:
    """Send one allocator-issued sub-agent task and optionally wait for its file result."""
    from config import CONFIG

    normalized_input = normalize_workspace_relative_path(input_path)
    normalized_output = normalize_workspace_relative_path(output_path)
    if normalized_input == normalized_output:
        raise ValueError("input_path and output_path must be different files")
    validate_subagent_path_pair(
        normalized_input, normalized_output, CONFIG.tasks_dirname
    )
    prompt = render_subagent_prompt(
        CONFIG.chatgpt_prompt_file,
        normalized_input,
        normalized_output,
    )

    output_file: Path | None = None
    output_baseline: tuple[int, int, int] | None = None
    if wait_for_completion:
        normalize_workspace_file(
            CONFIG.workspace_root, normalized_input, must_exist=True
        )
        output_file = normalize_workspace_file(
            CONFIG.workspace_root, normalized_output, must_exist=False
        )
        output_baseline = _file_signature(output_file)

    result = send_prompt(
        prompt,
        url=CONFIG.chatgpt_url,
        profile_dir=CONFIG.chatgpt_profile_dir,
        browser_channel=CONFIG.chatgpt_browser_channel,
        headless=CONFIG.chatgpt_browser_headless,
        timeout_seconds=CONFIG.chatgpt_browser_timeout_seconds,
        verification_markers=(
            normalized_input,
            normalized_output,
            SUBAGENT_STARTED_SENTINEL,
            SUBAGENT_COMPLETED_SENTINEL,
        ),
        started_wait=(
            (
                lambda: _wait_for_file_completion(
                    output_file,
                    output_baseline,
                    SUBAGENT_START_TIMEOUT_SECONDS,
                    SUBAGENT_STARTED_SENTINEL,
                )
            )
            if output_file is not None
            else None
        ),
    )
    if output_file is not None:
        waited = _wait_for_file_completion(
            output_file,
            None,
            CONFIG.chatgpt_completion_timeout_seconds,
            SUBAGENT_COMPLETED_SENTINEL,
        )
        result.update(
            status="completed",
            completion_file=str(output_file),
            completion_wait_seconds=round(waited, 3),
        )
    result.update(input_path=normalized_input, output_path=normalized_output)
    return result


def main() -> None:
    from config import CONFIG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("login", "send", "send-and-wait"))
    parser.add_argument(
        "--input-path", help="workspace-relative task file used by the send action"
    )
    parser.add_argument(
        "--output-path", help="workspace-relative result file used by the send action"
    )
    args = parser.parse_args()
    if args.action == "login":
        open_login_browser(
            url=CONFIG.chatgpt_url,
            profile_dir=CONFIG.chatgpt_profile_dir,
            browser_executable=CONFIG.chatgpt_browser_executable,
            no_sandbox=CONFIG.chatgpt_browser_no_sandbox,
        )
        return
    if not args.input_path or not args.output_path:
        parser.error("send requires both --input-path and --output-path")
    result = send_subagent_task(
        args.input_path,
        args.output_path,
        wait_for_completion=args.action == "send-and-wait",
    )
    print(result)


if __name__ == "__main__":
    main()
