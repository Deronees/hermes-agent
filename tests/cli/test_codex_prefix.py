import pytest


def _import_cli():
    import hermes_cli.config as config_mod

    if not hasattr(config_mod, "save_env_value_secure"):
        config_mod.save_env_value_secure = lambda key, value: {
            "success": True,
            "stored_as": key,
            "validated": False,
        }

    import cli as cli_mod

    return cli_mod


class _FakeCLI:
    def __init__(self):
        self.session_id = "session-codex"
        self.tool_progress_mode = "all"
        self.conversation_history = []
        self.agent = None

    def _preprocess_images_with_vision(self, query, images, announce=False):
        return query

    def _ensure_runtime_credentials(self):
        raise AssertionError("standard runtime credentials should not be used for @CODEX quiet mode")


def test_main_quiet_query_routes_codex_and_prints_result(monkeypatch, capsys):
    cli_mod = _import_cli()
    fake_cli = _FakeCLI()

    monkeypatch.setattr(cli_mod, "HermesCLI", lambda **kwargs: fake_cli)
    monkeypatch.setattr(cli_mod, "_parse_skills_argument", lambda skills: [])
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda: None)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *args, **kwargs: None)

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda config, platform: {"core"})
    monkeypatch.setattr(
        "tools.codex_sdk_tool.run_codex_task",
        lambda prompt, mode="standard", cwd=None: {
            "success": True,
            "final_response": f"codex:{mode}:{prompt}",
        },
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(query="@CODEX explain this", quiet=True)

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "codex:standard:explain this" in captured.out
    assert "session_id: session-codex" in captured.err


def test_main_quiet_query_routes_codex_computer_use(monkeypatch, capsys):
    cli_mod = _import_cli()
    fake_cli = _FakeCLI()

    monkeypatch.setattr(cli_mod, "HermesCLI", lambda **kwargs: fake_cli)
    monkeypatch.setattr(cli_mod, "_parse_skills_argument", lambda skills: [])
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda: None)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *args, **kwargs: None)

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda config, platform: {"core"})
    monkeypatch.setattr(
        "tools.codex_sdk_tool.run_codex_task",
        lambda prompt, mode="standard", cwd=None: {
            "success": True,
            "final_response": f"codex:{mode}:{prompt}",
        },
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(query="@CODEX:computer_use open calc", quiet=True)

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "codex:computer_use:open calc" in captured.out
    assert "session_id: session-codex" in captured.err
