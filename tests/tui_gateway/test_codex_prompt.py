import threading
import time
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tools.codex_sdk_tool as codex_tool


_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value=Path("/tmp/hermes_test"))),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib

        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
        mod._methods.clear()
        importlib.reload(mod)


@pytest.mark.parametrize(
    ("text", "expected_mode"),
    [
        ("@CODEX hello world", "standard"),
        ("@CODEX:computer_use open calculator", "computer_use"),
    ],
)
def test_prompt_submit_routes_codex_and_updates_history(server, monkeypatch, text, expected_mode):
    sid = "codex-session"
    history_lock = threading.Lock()
    server._sessions[sid] = {
        "agent": MagicMock(),
        "history": [],
        "history_lock": history_lock,
        "history_version": 0,
        "attached_images": [],
        "session_key": sid,
        "cols": 80,
        "running": False,
    }

    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_on_tool_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_on_tool_complete", lambda *_args, **_kwargs: None)

    fake_result = {
        "final_response": "codex says hi",
        "messages": [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "codex says hi"},
        ],
        "failed": False,
        "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2},
    }

    captured = {}

    def _fake_run_codex_turn(**kwargs):
        captured.update(kwargs)
        return fake_result

    monkeypatch.setattr(codex_tool, "run_codex_turn", _fake_run_codex_turn)
    resp = server.handle_request(
        {
            "id": "r1",
            "method": "prompt.submit",
            "params": {"session_id": sid, "text": text},
        }
    )

    assert resp["result"]["status"] == "streaming"

    for _ in range(100):
        with history_lock:
            if not server._sessions[sid]["running"]:
                break
        time.sleep(0.01)

    with history_lock:
        assert server._sessions[sid]["history"] == fake_result["messages"]
        assert server._sessions[sid]["history_version"] == 1
    assert captured["mode"] == expected_mode
