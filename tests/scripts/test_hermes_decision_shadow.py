"""Phase A contracts: real config/resolver/SDK, fake HTTP transport, never inference."""

import asyncio
import copy
import io
import json
import logging
import runpy
import sys

import httpx
import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize(
    "case,reason,recommendation",
    [
        ("disabled", "disabled", "ABSTAIN"),
        ("yes", "recommended", "YES"),
        ("no", "recommended", "NO"),
        ("abstain", "model_abstained", "ABSTAIN"),
        ("timeout", "timeout", "ABSTAIN"),
        ("deadline", "timeout", "ABSTAIN"),
        ("cancel", "cancelled", "ABSTAIN"),
        ("http_error", "provider_error", "ABSTAIN"),
        ("redirect", "provider_error", "ABSTAIN"),
        ("malformed", "malformed_response", "ABSTAIN"),
        ("empty", "empty_response", "ABSTAIN"),
        ("unknown", "unknown_candidate", "ABSTAIN"),
        ("candidates", "candidate_mismatch", "ABSTAIN"),
        ("policy", "policy_revision_mismatch", "ABSTAIN"),
        ("binding", "request_mismatch", "ABSTAIN"),
        ("duplicate", "malformed_response", "ABSTAIN"),
        ("score", "malformed_response", "ABSTAIN"),
        ("truncated", "truncated_response", "ABSTAIN"),
        ("tools", "malformed_response", "ABSTAIN"),
        ("wrong_role", "malformed_response", "ABSTAIN"),
        ("unsupported", "unsupported_task", "ABSTAIN"),
        ("oversize", "invalid_request", "ABSTAIN"),
        ("rule", "rule_sufficient", "ABSTAIN"),
        ("wrong_route", "unsupported_route", "ABSTAIN"),
        ("stale_policy", "policy_revision_mismatch", "ABSTAIN"),
    ],
)
def test_shadow_contract_never_applies_or_retries(tmp_path, monkeypatch, caplog, case, reason, recommendation):
    from scripts import hermes_decision_shadow as shadow

    home = tmp_path / "home"
    home.mkdir()
    settings = {
        "enabled": case != "disabled",
        "provider": "custom",
        "model": "qwen-hermes",
        "base_url": "http://192.168.1.8:1234/v1",
        "api_mode": "chat_completions",
        "policy_revision": shadow.POLICY_REVISION,
        "timeout": 2 if case == "deadline" else 10,
    }
    if case == "wrong_route":
        settings["base_url"] = "http://127.0.0.1:1235/v1"
    if case == "stale_policy":
        settings["policy_revision"] = "old-policy"
    config = home / "config.yaml"
    config.write_text(json.dumps({"auxiliary": {shadow.TASK_KEY: settings}}), encoding="utf-8")
    config_before = config.read_bytes()
    secret = "sk-" + "a" * 48
    payload = {
        "project_id": "hermes-agent-desktop-control",
        "target_id": "local",
        "source_revision": "a" * 40,
        "decision_kind": "retrieval_needed",
        "policy_revision": shadow.POLICY_REVISION,
        "allowed_candidates": ["YES", "NO", "ABSTAIN"],
        "bounded_evidence": "조사: exact_symbol 실패. 외부 수정 금지. API_KEY=" + secret,
    }
    if case == "unsupported":
        payload["decision_kind"] = "worker_routing"
    if case == "oversize":
        payload["bounded_evidence"] = "x" * 4001
    if case == "rule":
        payload["rule_decided"] = True
    before = copy.deepcopy(payload)
    requests = []
    settled = []

    async def handle(_transport, request):
        requests.append(request)
        assert str(request.url) == "http://192.168.1.8:1234/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer no-key-required"
        body = json.loads(request.content)
        assert body["model"] == "qwen-hermes"
        assert "tools" not in body
        assert secret not in request.content.decode()
        evidence = json.loads(body["messages"][1]["content"])
        assert "exact_symbol" in evidence["bounded_evidence"]
        assert "외부 수정 금지" in evidence["bounded_evidence"]
        if case == "timeout":
            raise httpx.ReadTimeout("sensitive transport error " + secret)
        if case == "deadline":
            try:
                await asyncio.Event().wait()
            finally:
                settled.append(True)
        if case == "cancel":
            raise asyncio.CancelledError()
        if case == "http_error":
            return httpx.Response(503, json={"error": {"message": secret}})
        if case == "redirect":
            return httpx.Response(307, headers={"location": "https://example.invalid/"})
        answer = {
            key: evidence[key]
            for key in ("request_id", "decision_kind", "policy_revision", "allowed_candidates")
        }
        answer["recommendation"] = {"no": "NO", "abstain": "ABSTAIN", "unknown": "RUN"}.get(case, "YES")
        answer["raw_score"] = True if case == "score" else 0.37
        if case == "candidates":
            answer["allowed_candidates"] = ["YES", "NO", "RUN"]
        if case == "policy":
            answer["policy_revision"] = "old-policy"
        if case == "binding":
            answer["request_id"] = "b" * 64
        content = json.dumps(answer)
        if case == "malformed":
            content = "not JSON " + secret
        if case == "empty":
            content = ""
        if case == "duplicate":
            content = content[:-1] + ', "recommendation": "NO"}'
        message = {"role": "assistant", "content": content}
        if case == "wrong_role":
            message["role"] = "user"
        if case == "tools":
            message["tool_calls"] = [{"id": "x", "type": "function", "function": {"name": "deploy", "arguments": "{}"}}]
        return httpx.Response(200, json={
            "id": "test-only", "object": "chat.completion", "created": 0, "model": "qwen-hermes",
            "choices": [{"index": 0, "finish_reason": "length" if case == "truncated" else "stop", "message": message}],
        })

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    token = set_hermes_home_override(home)
    try:
        result = asyncio.run(shadow.recommend(payload))
    finally:
        reset_hermes_home_override(token)
    assert result.recommendation == recommendation, result
    assert result.reason_code == reason, result
    assert result.abstained == (recommendation == "ABSTAIN")
    assert result.shadow is True and result.applied is False
    assert result.raw_score == (0.37 if recommendation != "ABSTAIN" else None)
    assert payload == before
    assert config.read_bytes() == config_before
    no_call = {"disabled", "unsupported", "oversize", "rule", "wrong_route", "stale_policy"}
    assert len(requests) == (0 if case in no_call else 1)
    assert secret not in repr(result) + caplog.text
    if case == "deadline":
        assert settled == [True]


def test_cli_real_profile_scope_a_b_a_and_disabled_no_stdin(tmp_path, monkeypatch, caplog):
    from agent.secret_scope import (
        is_multiplex_active, reset_secret_scope, set_multiplex_active, set_secret_scope,
    )
    from scripts import hermes_decision_shadow as shadow

    caplog.set_level(logging.DEBUG, logger="openai._base_client")
    homes = [tmp_path / name for name in ("a", "b")]
    original_files = {}
    for index, home in enumerate(homes):
        home.mkdir()
        config = {
            "model": {"provider": "openai", "model": "not-the-shadow-model"},
            "auxiliary": {shadow.TASK_KEY: {
                "enabled": index == 0, "provider": "custom", "model": "qwen-hermes",
                "base_url": "http://192.168.1.8:1234/v1", "api_mode": "chat_completions",
                "policy_revision": shadow.POLICY_REVISION, "timeout": 10,
            }},
        }
        path = home / "config.yaml"
        path.write_text(json.dumps(config), encoding="utf-8")
        original_files[path] = path.read_bytes()
    payload = {
        "project_id": "hermes-agent-desktop-control", "target_id": "local", "source_revision": "a" * 40,
        "decision_kind": "retrieval_needed", "policy_revision": shadow.POLICY_REVISION,
        "allowed_candidates": ["YES", "NO", "ABSTAIN"], "bounded_evidence": "Investigate exact_error without retry.",
    }
    requests = []

    async def handle(_transport, request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer no-key-required"
        assert "x-private-token" not in request.headers
        bound = json.loads(json.loads(request.content)["messages"][1]["content"])
        answer = {key: bound[key] for key in ("request_id", "decision_kind", "policy_revision", "allowed_candidates")}
        answer["recommendation"] = "NO"
        return httpx.Response(200, json={
            "id": "test-only", "object": "chat.completion", "created": 0, "model": "qwen-hermes",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}],
        })

    class UnreadableInput(io.StringIO):
        def read(self, *args):
            raise AssertionError("Disabled shadow must not read stdin")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        for home, expected in ((homes[0], "NO"), (homes[1], "ABSTAIN"), (homes[0], "NO")):
            home_token = set_hermes_home_override(home)
            secret_token = set_secret_scope({"OPENAI_API_KEY": "must-not-borrow-" + home.name})
            output = io.StringIO()
            stdin = io.StringIO(json.dumps(payload)) if expected == "NO" else UnreadableInput()
            prior_log_disable = logging.root.manager.disable
            try:
                with monkeypatch.context() as cli:
                    cli.setattr(sys, "argv", [shadow.__file__])
                    cli.setattr(sys, "stdin", stdin)
                    cli.setattr(sys, "stdout", output)
                    with pytest.raises(SystemExit) as finished:
                        runpy.run_path(shadow.__file__, run_name="__main__")
                    assert finished.value.code == 0
            finally:
                logging.disable(prior_log_disable)
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
            result = json.loads(output.getvalue())
            assert result["recommendation"] == expected
            assert result["applied"] is False
            assert "must-not-borrow" not in output.getvalue() + caplog.text
            assert payload["bounded_evidence"] not in output.getvalue() + caplog.text
    finally:
        set_multiplex_active(multiplex)
    assert len(requests) == 2
    for path, original in original_files.items():
        assert path.read_bytes() == original
