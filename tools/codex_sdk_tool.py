"""Codex SDK adapter and internal tool for explicit @CODEX routing."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from hermes_cli.config import load_config
from tools.registry import registry, tool_result

logger = logging.getLogger(__name__)

_BRIDGE_SCRIPT = Path(
    os.getenv("HERMES_CODEX_SDK_BRIDGE_SCRIPT", Path(__file__).with_name("codex_sdk_bridge.mjs"))
).resolve()
_CODEX_TOOL_NAME = "codex_run_task"

CODEX_RUN_TASK_SCHEMA = {
    "name": _CODEX_TOOL_NAME,
    "description": (
        "Run a one-shot Codex task through the local Codex SDK bridge. "
        "Use mode='standard' for normal coding/reasoning tasks, or "
        "mode='computer_use' when the host Codex profile is configured for "
        "computer-use workflows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Task prompt to send to Codex.",
            },
            "mode": {
                "type": "string",
                "enum": ["standard", "computer_use"],
                "description": "Execution mode. Defaults to 'standard'.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory for the Codex run.",
            },
        },
        "required": ["prompt"],
    },
}


class CodexTaskHandle:
    """Interruptible handle for a single Codex SDK subprocess."""

    def __init__(self, *, model: str = "codex-sdk") -> None:
        self.model = model
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._interrupted = False
        self._interrupt_reason = ""
        self._last_activity_desc = "starting Codex task"
        self._last_activity_ts = time.time()

    def set_process(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = proc

    def clear_process(self) -> None:
        with self._lock:
            self._process = None

    def mark_activity(self, text: str) -> None:
        if text:
            self._last_activity_desc = str(text)
        self._last_activity_ts = time.time()

    def interrupt(self, reason: str | None = None) -> None:
        with self._lock:
            self._interrupted = True
            self._interrupt_reason = str(reason or "Interrupted")
            proc = self._process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass

        def _force_kill() -> None:
            time.sleep(1.0)
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        threading.Thread(target=_force_kill, daemon=True).start()

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    @property
    def interrupt_reason(self) -> str:
        return self._interrupt_reason or "Interrupted"

    def get_activity_summary(self) -> dict[str, Any]:
        return {
            "api_call_count": 0,
            "max_iterations": 1,
            "current_tool": _CODEX_TOOL_NAME,
            "last_activity_desc": self._last_activity_desc,
            "last_activity_ts": self._last_activity_ts,
        }


def get_codex_sdk_config() -> dict[str, Any]:
    config = load_config().get("codex_sdk", {})
    return config if isinstance(config, dict) else {}


def codex_sdk_enabled() -> bool:
    return bool(get_codex_sdk_config().get("enabled", True))


def parse_codex_command_prefix(text: str) -> dict[str, Any] | None:
    """Parse an explicit @CODEX prefix at the very start of the message."""
    if not isinstance(text, str):
        return None

    candidate = text.lstrip()
    if not candidate:
        return None
    if not candidate[:6].upper() == "@CODEX":
        return None

    suffix = candidate[6:]
    if suffix and suffix[0] not in {":", " ", "\t", "\r", "\n"}:
        return None

    mode = "standard"
    if suffix.startswith(":"):
        mode_token, _, suffix = suffix[1:].partition(" ")
        if "\n" in mode_token:
            mode_token, _, extra = mode_token.partition("\n")
            suffix = extra + (" " + suffix if suffix else "")
        mode_token = mode_token.strip().lower()
        if not mode_token:
            return {"mode": None, "prompt": "", "error": "@CODEX mode is missing."}
        if mode_token not in {"standard", "computer_use"}:
            return {
                "mode": None,
                "prompt": "",
                "error": f"Unsupported @CODEX mode '{mode_token}'. Use 'standard' or 'computer_use'.",
            }
        mode = mode_token

    prompt = suffix.lstrip()
    if not prompt:
        return {"mode": mode, "prompt": "", "error": "@CODEX requires a prompt."}
    return {"mode": mode, "prompt": prompt}


def _resolve_cwd(cwd: str | None) -> Path:
    raw = str(cwd or os.getenv("TERMINAL_CWD") or os.getcwd()).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if not path.exists():
        raise ValueError(f"Working directory does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Working directory is not a directory: {path}")
    return path


def _resolve_allowed_roots(raw_roots: Iterable[Any]) -> list[Path]:
    roots: list[Path] = []
    for root in raw_roots or []:
        if root is None:
            continue
        path = Path(str(root)).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        roots.append(path)
    return roots


def _cwd_is_allowed(cwd: Path, roots: list[Path]) -> bool:
    if not roots:
        return True
    for root in roots:
        if cwd == root or root in cwd.parents:
            return True
    return False


def _resolve_executable(command: str) -> str | None:
    raw = str(command or "").strip()
    if not raw:
        return None
    expanded = os.path.expanduser(raw)
    if os.path.sep in expanded or (os.path.altsep and os.path.altsep in expanded):
        return expanded if os.path.isfile(expanded) else None
    return shutil.which(expanded)


def _resolve_codex_auth() -> tuple[str | None, str | None, str | None]:
    from hermes_cli.auth import (
        DEFAULT_CODEX_BASE_URL,
        _import_codex_cli_tokens,
        get_codex_auth_status,
        resolve_codex_runtime_credentials,
    )

    status = get_codex_auth_status()
    api_key = status.get("api_key") if isinstance(status, dict) else None
    base_url = DEFAULT_CODEX_BASE_URL
    source = status.get("source") if isinstance(status, dict) else None

    try:
        creds = resolve_codex_runtime_credentials()
        api_key = creds.get("api_key") or api_key
        base_url = creds.get("base_url") or base_url
        source = creds.get("source") or source
    except Exception:
        cli_tokens = _import_codex_cli_tokens()
        if cli_tokens and cli_tokens.get("access_token"):
            api_key = cli_tokens["access_token"]
            source = source or "~/.codex/auth.json"

    return api_key, base_url, source


def _normalize_mode_config(config: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = config.get("modes", {})
    modes = modes if isinstance(modes, dict) else {}
    mode_config = modes.get(mode, {})
    return mode_config if isinstance(mode_config, dict) else {}


def _build_mode_env(mode_config: dict[str, Any]) -> dict[str, str]:
    merged = dict(os.environ)
    overrides = mode_config.get("env", {})
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if value is None:
                continue
            merged[str(key)] = str(value)
    return merged


def _make_request(
    *,
    prompt: str,
    mode: str,
    cwd: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    mode_config = _normalize_mode_config(config, mode)
    node_command = str(config.get("node_command") or "node").strip() or "node"
    codex_command = str(config.get("codex_command") or "codex").strip() or "codex"
    node_path = _resolve_executable(node_command)
    if not node_path:
        raise RuntimeError(f"Node.js executable not found: {node_command}")
    codex_path = _resolve_executable(codex_command)
    if not codex_path:
        raise RuntimeError(f"Codex executable not found: {codex_command}")
    if not _BRIDGE_SCRIPT.is_file():
        raise RuntimeError(f"Codex SDK bridge script not found: {_BRIDGE_SCRIPT}")

    roots = _resolve_allowed_roots(config.get("allowed_roots") or [])
    if not _cwd_is_allowed(cwd, roots):
        allowed = ", ".join(str(root) for root in roots) or "(none configured)"
        raise RuntimeError(f"Working directory {cwd} is outside codex_sdk.allowed_roots: {allowed}")

    if mode == "computer_use" and not bool(mode_config.get("enabled", False)):
        raise RuntimeError("Codex computer_use mode is disabled in codex_sdk.modes.computer_use.enabled.")

    api_key, base_url, _auth_source = _resolve_codex_auth()
    if not api_key:
        raise RuntimeError("OpenAI Codex authentication was not found. Run `hermes auth login openai-codex` or `codex login` first.")

    request = {
        "action": "run",
        "prompt": prompt,
        "mode": mode,
        "sdk_options": {
            "codex_path": codex_path,
            "api_key": api_key,
            "base_url": base_url,
            "config": mode_config.get("config_overrides") or {},
            "env": _build_mode_env(mode_config),
        },
        "thread_options": {
            "workingDirectory": str(cwd),
            "skipGitRepoCheck": True,
            "approvalPolicy": "never",
            "sandboxMode": "danger-full-access",
            "networkAccessEnabled": True,
        },
        "bridge_metadata": {
            "cwd": str(cwd),
            "node_path": node_path,
            "bridge_script": str(_BRIDGE_SCRIPT),
        },
    }
    return request


def _truncate_preview(text: str, *, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _summarize_item(item: dict[str, Any]) -> str | None:
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type == "command_execution":
        command = _truncate_preview(item.get("command", ""), limit=120)
        status = str(item.get("status") or "")
        if command:
            return f"command ({status or 'unknown'}): {command}"
    if item_type == "file_change":
        changes = item.get("changes") or []
        if isinstance(changes, list):
            return f"file change: {len(changes)} file(s)"
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "")
        if server or tool:
            return f"mcp: {server}.{tool}".strip(".")
    if item_type == "web_search":
        query = _truncate_preview(item.get("query", ""), limit=120)
        if query:
            return f"web search: {query}"
    if item_type == "todo_list":
        items = item.get("items") or []
        if isinstance(items, list):
            done = sum(1 for entry in items if isinstance(entry, dict) and entry.get("completed"))
            return f"todo list: {done}/{len(items)} complete"
    if item_type == "error":
        message = _truncate_preview(item.get("message", ""), limit=120)
        if message:
            return f"error: {message}"
    return None


def _emit_tool_progress(
    callback: Callable[..., Any] | None,
    event_type: str,
    preview: str,
    *,
    mode: str,
    cwd: Path,
    extra_args: dict[str, Any] | None = None,
) -> None:
    if not callback:
        return
    args = {"mode": mode, "cwd": str(cwd)}
    if extra_args:
        args.update(extra_args)
    try:
        callback(event_type, _CODEX_TOOL_NAME, preview, args)
    except Exception:
        logger.debug("tool progress callback failed", exc_info=True)


def run_codex_task(
    prompt: str,
    *,
    mode: str = "standard",
    cwd: str | None = None,
    progress_callback: Callable[..., Any] | None = None,
    control: CodexTaskHandle | None = None,
) -> dict[str, Any]:
    """Run a single Codex SDK task via the Node bridge."""
    prompt = str(prompt or "").strip()
    mode = str(mode or "standard").strip().lower() or "standard"
    if mode not in {"standard", "computer_use"}:
        return {
            "success": False,
            "final_response": "",
            "codex_thread_id": None,
            "mode": mode,
            "cwd": "",
            "changed_files": [],
            "summary_items": [],
            "usage": None,
            "error": f"Unsupported mode '{mode}'. Use 'standard' or 'computer_use'.",
        }
    if not prompt:
        return {
            "success": False,
            "final_response": "",
            "codex_thread_id": None,
            "mode": mode,
            "cwd": "",
            "changed_files": [],
            "summary_items": [],
            "usage": None,
            "error": "Prompt is required.",
        }

    cfg = get_codex_sdk_config()
    if not bool(cfg.get("enabled", True)):
        return {
            "success": False,
            "final_response": "",
            "codex_thread_id": None,
            "mode": mode,
            "cwd": "",
            "changed_files": [],
            "summary_items": [],
            "usage": None,
            "error": "codex_sdk is disabled in config.yaml.",
        }

    try:
        resolved_cwd = _resolve_cwd(cwd)
        request = _make_request(prompt=prompt, mode=mode, cwd=resolved_cwd, config=cfg)
    except Exception as exc:
        return {
            "success": False,
            "final_response": "",
            "codex_thread_id": None,
            "mode": mode,
            "cwd": str(cwd or ""),
            "changed_files": [],
            "summary_items": [],
            "usage": None,
            "error": str(exc),
        }

    node_path = request["bridge_metadata"]["node_path"]
    timeout_seconds = float(cfg.get("timeout_seconds") or 900)
    run_handle = control or CodexTaskHandle(model=f"codex-sdk:{mode}")
    run_handle.mark_activity(f"starting {_CODEX_TOOL_NAME}")

    _emit_tool_progress(
        progress_callback,
        "tool.started",
        f"Codex ({mode})",
        mode=mode,
        cwd=resolved_cwd,
    )

    proc = subprocess.Popen(
        [node_path, str(_BRIDGE_SCRIPT)],
        cwd=str(resolved_cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    run_handle.set_process(proc)

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stderr_chunks: list[str] = []
    changed_files: list[str] = []
    changed_seen: set[str] = set()
    summary_items: list[str] = []
    items: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    final_response = ""
    thread_id: str | None = None
    completed_payload: dict[str, Any] | None = None
    failed_error: str | None = None
    stdout_done = False
    stderr_done = False

    def _reader(stream: Any, kind: str) -> None:
        try:
            for raw_line in iter(stream.readline, ""):
                lines.put((kind, raw_line))
        finally:
            lines.put((kind, None))

    stdout_thread = threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as exc:
        proc.kill()
        run_handle.clear_process()
        return {
            "success": False,
            "final_response": "",
            "codex_thread_id": None,
            "mode": mode,
            "cwd": str(resolved_cwd),
            "changed_files": [],
            "summary_items": [],
            "usage": None,
            "error": f"Failed to send request to Codex bridge: {exc}",
        }

    started_at = time.monotonic()
    while True:
        if run_handle.interrupted:
            try:
                proc.terminate()
            except Exception:
                pass
        if timeout_seconds > 0 and (time.monotonic() - started_at) > timeout_seconds:
            try:
                proc.kill()
            except Exception:
                pass
            failed_error = f"Codex task timed out after {int(timeout_seconds)} seconds."
            break
        try:
            kind, raw_line = lines.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and stdout_done and stderr_done:
                break
            continue

        if raw_line is None:
            if kind == "stdout":
                stdout_done = True
            else:
                stderr_done = True
            if proc.poll() is not None and stdout_done and stderr_done:
                break
            continue

        if kind == "stderr":
            text = raw_line.rstrip()
            if text:
                stderr_chunks.append(text)
            continue

        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            failed_error = f"Codex bridge emitted invalid JSON: {exc}"
            try:
                proc.kill()
            except Exception:
                pass
            break

        event_type = str(event.get("type") or "")
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "") or thread_id
            if thread_id:
                run_handle.mark_activity(f"started thread {thread_id}")
            continue
        if event_type == "progress":
            text = str(event.get("text") or "").strip()
            if text:
                run_handle.mark_activity(text)
                _emit_tool_progress(
                    progress_callback,
                    "tool.started",
                    _truncate_preview(text),
                    mode=mode,
                    cwd=resolved_cwd,
                )
            continue
        if event_type == "file.changed":
            path = str(event.get("path") or "").strip()
            if path and path not in changed_seen:
                changed_seen.add(path)
                changed_files.append(path)
                run_handle.mark_activity(f"changed {path}")
                _emit_tool_progress(
                    progress_callback,
                    "tool.started",
                    _truncate_preview(f"changed {path}"),
                    mode=mode,
                    cwd=resolved_cwd,
                    extra_args={"path": path},
                )
            continue
        if event_type == "completed":
            completed_payload = event
            items = event.get("items") or []
            if isinstance(items, list):
                for item in items:
                    summary = _summarize_item(item)
                    if summary:
                        summary_items.append(summary)
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
            final_response = str(event.get("final_response") or "")
            thread_id = str(event.get("thread_id") or "") or thread_id
            for path in event.get("changed_files") or []:
                if path not in changed_seen:
                    changed_seen.add(path)
                    changed_files.append(path)
            run_handle.mark_activity("completed Codex task")
            continue
        if event_type == "failed":
            failed_error = str(event.get("error") or "Codex task failed.")
            break

    run_handle.clear_process()
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    if run_handle.interrupted:
        result = {
            "success": False,
            "final_response": "",
            "codex_thread_id": thread_id,
            "mode": mode,
            "cwd": str(resolved_cwd),
            "changed_files": changed_files,
            "summary_items": summary_items,
            "usage": usage,
            "error": run_handle.interrupt_reason,
            "interrupted": True,
        }
    elif failed_error:
        stderr_text = "\n".join(chunk for chunk in stderr_chunks if chunk.strip())
        result = {
            "success": False,
            "final_response": "",
            "codex_thread_id": thread_id,
            "mode": mode,
            "cwd": str(resolved_cwd),
            "changed_files": changed_files,
            "summary_items": summary_items,
            "usage": usage,
            "error": stderr_text or failed_error,
        }
    elif completed_payload is None:
        stderr_text = "\n".join(chunk for chunk in stderr_chunks if chunk.strip())
        result = {
            "success": False,
            "final_response": "",
            "codex_thread_id": thread_id,
            "mode": mode,
            "cwd": str(resolved_cwd),
            "changed_files": changed_files,
            "summary_items": summary_items,
            "usage": usage,
            "error": stderr_text or "Codex bridge exited without a completion event.",
        }
    else:
        result = {
            "success": True,
            "final_response": final_response,
            "codex_thread_id": thread_id,
            "mode": mode,
            "cwd": str(resolved_cwd),
            "changed_files": changed_files,
            "summary_items": summary_items,
            "usage": usage,
            "items": items,
            "error": None,
        }

    try:
        _emit_tool_progress(
            progress_callback,
            "tool.completed",
            "",
            mode=mode,
            cwd=resolved_cwd,
            extra_args={
                "duration": max(time.monotonic() - started_at, 0.0),
                "is_error": not bool(result.get("success")),
            },
        )
    except Exception:
        logger.debug("tool completion callback failed", exc_info=True)

    return result


def run_codex_turn(
    *,
    user_message: str,
    prompt: str,
    conversation_history: list[dict[str, Any]] | None = None,
    mode: str = "standard",
    cwd: str | None = None,
    progress_callback: Callable[..., Any] | None = None,
    tool_start_callback: Callable[..., Any] | None = None,
    tool_complete_callback: Callable[..., Any] | None = None,
    control: CodexTaskHandle | None = None,
) -> dict[str, Any]:
    """Wrap a Codex SDK run in a run_conversation-like result shape."""
    history = list(conversation_history or [])
    tool_call_id = f"codex_{uuid.uuid4().hex[:10]}"
    tool_args = {"mode": mode, "cwd": str(cwd or os.getenv("TERMINAL_CWD") or os.getcwd())}
    if tool_start_callback:
        try:
            tool_start_callback(tool_call_id, _CODEX_TOOL_NAME, dict(tool_args))
        except Exception:
            logger.debug("tool_start_callback failed", exc_info=True)

    raw_result = run_codex_task(
        prompt,
        mode=mode,
        cwd=cwd,
        progress_callback=progress_callback,
        control=control,
    )
    raw_result_json = json.dumps(raw_result, ensure_ascii=False)

    if tool_complete_callback:
        try:
            tool_complete_callback(tool_call_id, _CODEX_TOOL_NAME, dict(tool_args), raw_result_json)
        except Exception:
            logger.debug("tool_complete_callback failed", exc_info=True)

    if raw_result.get("interrupted"):
        messages = history + [{"role": "user", "content": user_message}]
        return {
            "final_response": "Operation interrupted.",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "failed": False,
            "interrupted": True,
            "interrupt_message": raw_result.get("error"),
            "error": None,
            "response_previewed": False,
            "usage": raw_result.get("usage"),
        }

    final_response = str(raw_result.get("final_response") or "")
    if not raw_result.get("success"):
        error_text = str(raw_result.get("error") or "Codex task failed.")
        if not final_response:
            final_response = f"Error: {error_text}"
    assistant_message = {"role": "assistant", "content": final_response}
    messages = history + [{"role": "user", "content": user_message}, assistant_message]
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": 0,
        "completed": bool(raw_result.get("success")),
        "failed": not bool(raw_result.get("success")),
        "error": raw_result.get("error"),
        "response_previewed": False,
        "usage": raw_result.get("usage"),
        "codex_thread_id": raw_result.get("codex_thread_id"),
        "changed_files": raw_result.get("changed_files", []),
        "summary_items": raw_result.get("summary_items", []),
    }


def codex_run_task(args: dict[str, Any], **_kwargs) -> str:
    payload = run_codex_task(
        args.get("prompt", ""),
        mode=args.get("mode", "standard"),
        cwd=args.get("cwd"),
    )
    return tool_result(payload)


registry.register(
    name=_CODEX_TOOL_NAME,
    toolset="codex",
    schema=CODEX_RUN_TASK_SCHEMA,
    handler=codex_run_task,
    check_fn=codex_sdk_enabled,
    description=CODEX_RUN_TASK_SCHEMA["description"],
    emoji="🧠",
)
