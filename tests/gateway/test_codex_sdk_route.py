from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class TestGatewayCodexRoute:
    @pytest.mark.asyncio
    async def test_prepare_message_marks_and_strips_codex_prefix(self):
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.adapters = {}
        runner._model = "gpt-5.4"
        runner._base_url = ""
        runner._has_setup_skill = lambda: False

        event = SimpleNamespace(
            text="@CODEX:computer_use open calculator",
            media_urls=[],
            media_types=[],
            message_type="text",
            reply_to_text=None,
            reply_to_message_id=None,
        )
        source = SimpleNamespace(
            chat_type="dm",
            thread_id=None,
            user_name=None,
            platform="telegram",
            chat_id="chat-1",
        )

        message = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

        assert message == "open calculator"
        assert getattr(event, "_hermes_codex_route") == {
            "mode": "computer_use",
            "prompt": "open calculator",
        }

    @pytest.mark.asyncio
    async def test_run_agent_uses_codex_route_directly(self, monkeypatch):
        runner = object.__new__(gateway_run.GatewayRunner)
        runner.adapters = {}
        runner._ephemeral_system_prompt = ""
        runner._prefill_messages = []
        runner._reasoning_config = None
        runner._service_tier = None
        runner._provider_routing = {}
        runner._fallback_model = None
        runner._running_agents = {}
        runner._pending_model_notes = {}
        runner._session_db = None
        runner._agent_cache = {}
        runner._session_model_overrides = {}
        runner._agent_cache_lock = None
        runner.hooks = SimpleNamespace(loaded_hooks=False)
        runner.config = SimpleNamespace(streaming=None)
        runner._get_proxy_url = lambda: None
        runner._is_session_run_current = lambda session_key, generation: True
        runner._release_running_agent_state = lambda session_key: None
        runner._update_runtime_status = lambda status: None
        runner._enforce_agent_cache_cap = lambda: None

        async def _run_in_executor_with_context(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        runner._run_in_executor_with_context = _run_in_executor_with_context

        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setattr(gateway_run, "_env_path", "/tmp/unused")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)

        import hermes_cli.tools_config as tools_config
        monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

        invoked = {}

        def _fake_run_codex_turn(**kwargs):
            invoked.update(kwargs)
            return {
                "final_response": "codex-ok",
                "messages": [
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "codex-ok"},
                ],
                "api_calls": 0,
                "failed": False,
                "error": None,
                "usage": {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 2},
            }

        monkeypatch.setattr("tools.codex_sdk_tool.run_codex_turn", _fake_run_codex_turn)

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="user-1",
        )

        result = await runner._run_agent(
            message="say hi",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:telegram:dm:12345",
            codex_route={"mode": "standard", "prompt": "say hi"},
        )

        assert result["final_response"] == "codex-ok"
        assert result["model"] == "codex-sdk:standard"
        assert result["input_tokens"] == 3
        assert invoked["user_message"] == "say hi"
        assert invoked["mode"] == "standard"
