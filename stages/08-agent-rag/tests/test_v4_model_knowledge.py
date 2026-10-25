import asyncio
import importlib.util
import json
import re
from pathlib import Path

import pytest
from rag_agent.config import Config


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAGE = Path(__file__).parents[1]
accept = load_module("accept_v4_model", STAGE / "scripts/accept-v4-model.py")
audit = load_module("audit_v4_knowledge", STAGE / "scripts/audit-v4-knowledge.py")


def load_config(monkeypatch, tmp_path, **overrides):
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    key = tmp_path / "key"
    key.write_text("private-key", encoding="utf-8")
    values = {"AGENT_SERVICE_TOKEN_FILE": str(token), "AGENT_LLM_API_KEY_FILE": str(key),
              "AGENT_LLM_MODEL": "candidate-x", "AGENT_LLM_BASE_URL": "https://model.example/v1",
              "RAG_DATABASE_URL": "postgresql://db/rag", "AGENT_REDIS_URL": "redis://cache",
              "OPS_URL": "http://127.0.0.1:8094"}
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return Config.load()


def test_v4_assistant_switch_defaults_off(monkeypatch, tmp_path):
    cfg = load_config(monkeypatch, tmp_path)
    assert cfg.v4_assistant_enabled is False
    assert cfg.model_role == "assistant"


def test_v4_assistant_switch_parses_explicit_true(monkeypatch, tmp_path):
    cfg = load_config(monkeypatch, tmp_path, V4_ASSISTANT_ENABLED="true")
    assert cfg.v4_assistant_enabled is True


def test_v4_assistant_switch_rejects_invalid_value(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="v4_assistant_enabled"):
        load_config(monkeypatch, tmp_path, V4_ASSISTANT_ENABLED="maybe")


def test_thinking_is_explicit_optional_and_auditable(monkeypatch,tmp_path):
    monkeypatch.delenv('RAG_MODEL_THINKING',raising=False)
    assert load_config(monkeypatch,tmp_path).provider_profile()['thinking']=='provider_default'
    cfg=load_config(monkeypatch,tmp_path,RAG_MODEL_THINKING='disabled')
    assert cfg.model_thinking=='disabled' and cfg.provider_profile()['thinking']=='disabled'
    with pytest.raises(ValueError,match='thinking'):
        load_config(monkeypatch,tmp_path,RAG_MODEL_THINKING='silent')


def test_model_role_is_external_and_registered(monkeypatch, tmp_path):
    cfg = load_config(monkeypatch, tmp_path, AGENT_MODEL_ROLE="assistant-qa")
    assert cfg.model_role == "assistant-qa"
    assert cfg.provider_profile()["role"] == "assistant-qa"


def test_provider_profile_never_exposes_credentials():
    cfg = Config("candidate-x", "https://user:pass@model.example:8443/v1", "private-model-key",
                 "s" * 32, "postgresql://private", "redis://private")
    profile = cfg.provider_profile()
    assert profile["destination"] == "https://model.example:8443"
    encoded = json.dumps(profile)
    assert "private-model-key" not in encoded and "user" not in encoded and "pass" not in encoded
    assert profile["output_budget"] == cfg.max_tokens and profile["capabilities"]["usage"] is True


def test_eval_fixtures_are_fixed_synthetic_q01_q10(capsys):
    fixtures = accept.build_fixtures()
    assert [row["id"] for row in fixtures] == ["Q0%d" % i for i in range(1, 10)] + ["Q10"]
    assert all("http" not in row["prompt"] for row in fixtures)
    assert accept.main(["--check-only"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "check_only" and output["fixtures"] == 10


def test_eval_refuses_to_run_without_private_config(monkeypatch, tmp_path):
    for name in accept.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert accept.main(["--run", "--rounds", "3", "--output", str(tmp_path / "out.json")]) == 2


def test_eval_sample_records_tools_usage_and_unknown_cost_without_retry():
    cfg = Config("candidate-x", "https://model.example/v1", "private-model-key",
                 "s" * 32, "postgresql://private", "redis://private")
    call = {"id": "call_a", "type": "function",
            "function": {"name": "dispatch_ops_task", "arguments": '{"intent": "按台账id ASC定位第二台，查询当前状态"}'}}
    replies = [{"role": "assistant", "content": None, "tool_calls": [call],
                "finish_reason": "tool_calls", "usage": {"total_tokens": 5}},
               {"role": "assistant", "content": "合成答案", "finish_reason": "stop",
                "usage": {"total_tokens": 9}}]

    class FakeProvider:
        def __init__(self):
            self.seen, self.calls = [], 0
        async def complete(self, messages, schemas=None):
            self.seen.append(list(messages)); self.calls += 1
            return replies.pop(0)

    provider = FakeProvider()
    record = asyncio.run(accept.run_sample(cfg, provider, accept.FIXTURES[1], 1, 3))
    assert record["status"] == "ok" and '512.4' in record["output"]
    assert provider.calls == 2 and len(record["steps"]) == 2
    assert record["usage"]['total_tokens'] == 14
    assert record["cost"] == {"amount": None, "currency": None, "reason": "pricing_not_configured"}
    assert 'SYN-INV-0002' in provider.seen[1][-1]["content"]


def test_manifest_pins_content_hash_for_every_entry():
    entries = json.loads((STAGE / "knowledge/sources.json").read_text(encoding="utf-8-sig"))
    for entry in entries:
        assert re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", "")), entry["path"]


@pytest.mark.asyncio
async def test_eval_first_item_wording_locates_actual_first_device():
    ops=accept.FixtureOps(None,{'id':'Q08'})
    task=await ops.dispatch('读取台账按id ASC排序第一项，返回遥测')
    assert next(c['result']['device_id'] for c in task['tool_calls'] if c['name']=='get_telemetry')==11


def test_audit_script_is_read_only_and_reports_content_evidence(tmp_path):
    document = tmp_path / "a.md"
    document.write_text("# 标题\n\n正文", encoding="utf-8")
    digest = audit.load_ingest().document_sha256(document)
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps([{"path": "a.md", "title": "A", "sha256": digest}]),
                        encoding="utf-8")
    report = tmp_path / "audit.json"
    code = audit.main(["--manifest", str(manifest), "--root", str(tmp_path),
                       "--report", str(report), "--skip-qdrant",
                       "--embedding-path", str(tmp_path / "missing-model")])
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["status"] == "ok" and data["read_only"] is True
    assert "content_sha256" in data["evidence"] and "mtime" in data["evidence"]
    assert data["expected_version"] is None and data["chunking"]["kind"] == "unavailable"
    assert data["documents"][0]["hash_status"] == "match"
