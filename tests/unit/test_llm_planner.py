import json

import pandas as pd
import pytest

from app.agent.llm_client import OpenAIPlannerClient, PlannerProviderError, ScriptedPlannerClient
from app.agent.planner import HostedPlanner, configured_planner


def test_hosted_planner_sends_profile_without_cell_examples():
    class CaptureClient(ScriptedPlannerClient):
        def propose_plan(self, context):
            self.context = context
            return {"operations": [{"type": "trim_whitespace", "columns": ["customer"],
                "arguments": {}, "reason": "profile reports outer whitespace"}]}

    client = CaptureClient({})
    planner = HostedPlanner(client)
    frame = pd.DataFrame({"customer": [" Alice "]})
    operations = planner.propose(frame, "prepare")
    assert len(operations) == 1
    assert operations[0].requires_approval is True
    assert "examples" not in str(client.context)
    assert "Alice" not in json.dumps(client.context)


def test_hosted_planner_rejects_unsupported_tool():
    class Fake:
        def propose_plan(self, context):
            return {"operations": [{"type": "run_shell", "columns": [], "arguments": {}, "reason": "no"}]}

    with pytest.raises(Exception):
        HostedPlanner(Fake()).propose(pd.DataFrame({"x": [1]}), "test")


def test_openai_client_requires_key_and_planner_requires_sharing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PREPPILOT_PLANNER", "openai")
    monkeypatch.setenv("PREPPILOT_ALLOW_DATA_TO_LLM", "0")
    with pytest.raises(PlannerProviderError, match="explicitly permit"):
        configured_planner()
    monkeypatch.setenv("PREPPILOT_ALLOW_DATA_TO_LLM", "1")
    with pytest.raises(PlannerProviderError, match="OPENAI_API_KEY"):
        configured_planner()


def test_openai_response_json_arguments_and_usage(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({"status": "completed", "output_text": json.dumps({"operations": [
            {"type": "trim_whitespace", "columns": ["name"], "arguments": "{}", "reason": "observed"}]}),
            "usage": {"input_tokens": 4, "output_tokens": 2}}).encode()

    monkeypatch.setattr("app.agent.llm_client.urlopen", lambda *args, **kwargs: Response())
    result = OpenAIPlannerClient(api_key="test-key", model="test-model").propose_plan({"objective": "x"})
    assert result["operations"][0]["arguments"] == {}
