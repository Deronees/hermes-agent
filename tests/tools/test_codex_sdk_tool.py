import sys
import textwrap
from pathlib import Path

import pytest

import tools.codex_sdk_tool as codex_tool


def _write_bridge(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_bridge.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _config(tmp_path: Path, **overrides):
    base = {
        "enabled": True,
        "node_command": sys.executable,
        "codex_command": sys.executable,
        "timeout_seconds": 10,
        "allowed_roots": [str(tmp_path)],
        "modes": {
            "standard": {
                "config_overrides": {},
                "env": {},
            },
            "computer_use": {
                "enabled": False,
                "config_overrides": {},
                "env": {},
            },
        },
    }
    base.update(overrides)
    return {"codex_sdk": base}


def test_parse_codex_command_prefix_variants():
    route = codex_tool.parse_codex_command_prefix("@CODEX fix this")
    assert route == {"mode": "standard", "prompt": "fix this"}

    route = codex_tool.parse_codex_command_prefix("@CODEX:computer_use open Safari")
    assert route == {"mode": "computer_use", "prompt": "open Safari"}

    route = codex_tool.parse_codex_command_prefix("  @codex\nreview the diff")
    assert route == {"mode": "standard", "prompt": "review the diff"}


def test_parse_codex_command_prefix_rejects_non_prefix():
    assert codex_tool.parse_codex_command_prefix("please use @CODEX later") is None

    route = codex_tool.parse_codex_command_prefix("@CODEX:weird do it")
    assert route["error"].startswith("Unsupported @CODEX mode")

    route = codex_tool.parse_codex_command_prefix("@CODEX")
    assert route["error"] == "@CODEX requires a prompt."


def test_run_codex_task_fake_bridge_success(tmp_path, monkeypatch):
    bridge = _write_bridge(
        tmp_path,
        """
        import json, sys

        req = json.loads(sys.stdin.readline())
        assert req["prompt"] == "hello from hermes"
        print(json.dumps({"type": "thread.started", "thread_id": "thread_123"}), flush=True)
        print(json.dumps({"type": "progress", "text": "Running command: ls"}), flush=True)
        print(json.dumps({"type": "file.changed", "path": "src/app.py"}), flush=True)
        print(json.dumps({
            "type": "completed",
            "thread_id": "thread_123",
            "final_response": "done",
            "changed_files": ["src/app.py"],
            "usage": {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 7},
            "items": [
                {
                    "id": "cmd_1",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "",
                    "status": "completed"
                }
            ]
        }), flush=True)
        """,
    )
    monkeypatch.setattr(codex_tool, "_BRIDGE_SCRIPT", bridge)
    monkeypatch.setattr(codex_tool, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(codex_tool, "_resolve_codex_auth", lambda: ("token", "https://api.openai.com/v1", "test"))

    events = []

    def _progress(event_type, name=None, preview=None, args=None, **_kwargs):
        events.append((event_type, name, preview, args or {}))

    result = codex_tool.run_codex_task(
        "hello from hermes",
        cwd=str(tmp_path),
        progress_callback=_progress,
    )

    assert result["success"] is True
    assert result["final_response"] == "done"
    assert result["codex_thread_id"] == "thread_123"
    assert result["changed_files"] == ["src/app.py"]
    assert result["usage"]["input_tokens"] == 11
    assert result["summary_items"] == ["command (completed): ls"]
    assert any(event[0] == "tool.started" and event[1] == "codex_run_task" for event in events)


def test_run_codex_task_rejects_outside_allowed_roots(tmp_path, monkeypatch):
    bridge = _write_bridge(tmp_path, "raise SystemExit(0)\n")
    other_root = tmp_path / "allowed"
    other_root.mkdir()
    monkeypatch.setattr(codex_tool, "_BRIDGE_SCRIPT", bridge)
    monkeypatch.setattr(
        codex_tool,
        "load_config",
        lambda: _config(tmp_path, allowed_roots=[str(other_root)]),
    )
    monkeypatch.setattr(codex_tool, "_resolve_codex_auth", lambda: ("token", "https://api.openai.com/v1", "test"))

    result = codex_tool.run_codex_task("blocked", cwd=str(tmp_path))

    assert result["success"] is False
    assert "allowed_roots" in result["error"]


def test_run_codex_task_computer_use_requires_enablement(tmp_path, monkeypatch):
    bridge = _write_bridge(tmp_path, "raise SystemExit(0)\n")
    monkeypatch.setattr(codex_tool, "_BRIDGE_SCRIPT", bridge)
    monkeypatch.setattr(codex_tool, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(codex_tool, "_resolve_codex_auth", lambda: ("token", "https://api.openai.com/v1", "test"))

    result = codex_tool.run_codex_task("open the browser", mode="computer_use", cwd=str(tmp_path))

    assert result["success"] is False
    assert "computer_use mode is disabled" in result["error"]


def test_run_codex_task_bridge_failure(tmp_path, monkeypatch):
    bridge = _write_bridge(
        tmp_path,
        """
        import json, sys
        _ = json.loads(sys.stdin.readline())
        print(json.dumps({"type": "failed", "error": "bridge exploded"}), flush=True)
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(codex_tool, "_BRIDGE_SCRIPT", bridge)
    monkeypatch.setattr(codex_tool, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(codex_tool, "_resolve_codex_auth", lambda: ("token", "https://api.openai.com/v1", "test"))

    result = codex_tool.run_codex_task("hello", cwd=str(tmp_path))

    assert result["success"] is False
    assert "bridge exploded" in result["error"]
