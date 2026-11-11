import json

import pytest

from diag.agent.llm import AnthropicLLM, OpenAICompatLLM
from diag.agent.mock_brain import MockLLM
from diag.agent.validator import validate
from diag.runner import run_scenario
from diag.sim.scenarios import HELDOUT_IDS, MVP_IDS, SCENARIOS, build_world
from diag.sim.world import INJECTION_TEXT
from diag.store import Store
from diag.tools.backend import SimBackend
from diag.tools.gateway import OUTPUT_CAP_CHARS, Gateway, tool_definitions


@pytest.fixture
def gw():
    store = Store(":memory:")
    return Gateway(SimBackend(build_world(11)), store, 1), store


def test_every_scenario_fires_an_alert():
    for sid in SCENARIOS:
        assert build_world(sid).alerts, f"scenario {sid} fired nothing"


def test_label_never_reachable_through_tools(gw):
    g, _ = gw
    for name, args in [("list_alerts", {}), ("recent_changes", {}), ("top_errors", {}), ("db_activity", {})]:
        assert "bad_deploy" not in g.run(name, args).content


def test_invalid_args_become_error_steps(gw):
    g, store = gw
    r = g.run("container_stats", {"service": "mainframe"})
    assert not r.ok and "invalid arguments" in r.error
    assert store.steps(1)[0]["error"]


def test_write_tools_only_create_approvals(gw):
    g, store = gw
    r = g.run("restart_service", {"service": "postgres", "reason": "test"})
    assert r.data["status"] == "approval_required"
    assert [a["status"] for a in store.approvals(1)] == ["pending"]


def test_output_is_wrapped_and_capped(gw):
    g, _ = gw
    r = g.run("search_logs", {"limit": 100})
    assert 'trust="untrusted-data"' in r.content
    assert len(r.content) < OUTPUT_CAP_CHARS + 400


def test_validator_rejects_uncited_and_errored_steps(gw):
    g, _ = gw
    ok = g.run("list_alerts", {})
    bad = g.run("get_trace", {"trace_id": "0" * 32})
    base = {"category": "bad_deploy", "confidence": 0.8, "summary": "x", "fix": "y",
            "hypotheses": [{"category": "bad_deploy", "confidence": 0.8}]}
    d, errs = validate({**base, "claims": [{"text": "a", "step_ids": [ok.step_id]}]}, g.results)
    assert d is not None and not errs
    _, errs = validate({**base, "claims": [{"text": "a", "step_ids": ["s99"]}]}, g.results)
    assert any("does not exist" in e for e in errs)
    _, errs = validate({**base, "claims": [{"text": "a", "step_ids": [bad.step_id]}]}, g.results)
    assert any("returned an error" in e for e in errs)
    _, errs = validate({**base, "category": "made_up", "claims": [{"text": "a", "step_ids": ["s1"]}]}, g.results)
    assert errs


def test_tool_schemas_have_no_refs():
    assert "$ref" not in json.dumps(tool_definitions())
    assert len([t for t in tool_definitions() if t["name"] not in
                {"restart_service", "kill_query", "submit_diagnosis"}]) == 15


def test_injection_line_present_and_not_followed():
    w = build_world(1)
    assert any(INJECTION_TEXT in log.msg for log in w.logs)
    out = run_scenario(1, MockLLM(), Store(":memory:"), out_dir=__import__("pathlib").Path("/tmp/diag-test"))
    assert not out.injection_followed


@pytest.mark.parametrize("sid", sorted(MVP_IDS))
def test_mock_pipeline_end_to_end(sid, tmp_path):
    out = run_scenario(sid, MockLLM(), Store(":memory:"), out_dir=tmp_path)
    assert out.correct, f"{sid}: expected {out.expected}, got {out.got}"
    assert out.report_path.read_text().startswith("# Incident")


def test_anthropic_message_conversion_merges_user_turns():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "list_alerts", "args": {}}]},
            {"role": "tool", "results": [{"id": "t1", "name": "list_alerts", "content": "x"}]},
            {"role": "user", "content": "nudge"}]
    out = AnthropicLLM._convert(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[2]["content"][0]["type"] == "tool_result" and out[2]["content"][1]["type"] == "text"


def test_openai_message_conversion():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "list_alerts", "args": {}}]},
            {"role": "tool", "results": [{"id": "t1", "name": "list_alerts", "content": "x"}]}]
    out = OpenAICompatLLM._convert("sys", msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_loop_forces_submission_before_budget_runs_out(tmp_path):
    """A model that never stops exploring must be cornered into submitting, not time out as 'unknown'."""
    from diag.agent.llm import LLMResponse, ToolCall
    from diag.agent.loop import FORCE_AT, MAX_STEPS

    seen_tool_sets = []

    class Dawdler:
        model = "dawdler"

        def complete(self, system, messages, tools):
            names = [t["name"] for t in tools]
            seen_tool_sets.append(names)
            if names == ["submit_diagnosis"]:
                diag = {"category": "lock_contention", "confidence": 0.5, "summary": "best guess",
                        "claims": [{"text": "alerts fired", "step_ids": ["s1"]}],
                        "hypotheses": [{"category": "lock_contention", "confidence": 0.5}], "fix": "investigate"}
                return LLMResponse("committing", [ToolCall("c", "submit_diagnosis", diag)], 10, 10)
            return LLMResponse("one more check", [ToolCall(f"c{len(seen_tool_sets)}", "list_alerts", {})], 10, 10)

    out = run_scenario(4, Dawdler(), Store(":memory:"), out_dir=tmp_path)
    assert out.run.status == "done" and out.got == "lock_contention"
    assert len(out.run.steps) <= MAX_STEPS - FORCE_AT
    assert seen_tool_sets[-1] == ["submit_diagnosis"] and len(seen_tool_sets[0]) > 1


def test_validator_accepts_list_fields_sent_as_json_strings(gw):
    g, _ = gw
    g.run("list_alerts", {})
    raw = {"category": "bad_deploy", "confidence": 0.8, "summary": "x", "fix": "y",
           "claims": json.dumps([{"text": "a", "step_ids": ["s1"]}]),
           "hypotheses": json.dumps([{"category": "bad_deploy", "confidence": 0.8}])}
    d, errs = validate(raw, g.results)
    assert d is not None and not errs


def test_truncated_submission_is_rejected_then_repaired(tmp_path):
    from diag.agent.llm import LLMResponse, ToolCall

    good = {"category": "lock_contention", "confidence": 0.7, "summary": "s", "fix": "f",
            "claims": [{"text": "alerts fired", "step_ids": ["s1"]}],
            "hypotheses": [{"category": "lock_contention", "confidence": 0.7}]}

    class Cutoff:
        model = "cutoff"

        def __init__(self):
            self.n = 0

        def complete(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                return LLMResponse("", [ToolCall("a", "list_alerts", {})], 1, 1)
            if self.n == 2:  # complete-looking payload, but the reply hit the token cap
                return LLMResponse("", [ToolCall("b", "submit_diagnosis", good)], 1, 1, truncated=True)
            return LLMResponse("", [ToolCall("c", "submit_diagnosis", good)], 1, 1)

    out = run_scenario(4, Cutoff(), Store(":memory:"), out_dir=tmp_path)
    assert out.run.status == "done"
    assert any("cut off" in p for p in out.run.problems)


def test_every_category_has_a_definition_in_the_prompt():
    from diag.agent.prompts import SYSTEM
    from diag.taxonomy import CATEGORIES, DEFINITIONS

    assert set(DEFINITIONS) == set(CATEGORIES) - {"unknown"}
    assert all(f"{c}:" in SYSTEM for c in DEFINITIONS)


@pytest.mark.parametrize("sid", sorted(HELDOUT_IDS))
def test_heldout_fires_real_alert_and_hides_label(sid):
    from diag.tools.backend import SimBackend

    w = build_world(sid)
    assert any(a["severity"] != "info" for a in w.alerts), "failure must fire a non-info alert"
    be = SimBackend(w)
    blob = repr([be.list_alerts(), be.recent_changes(120), be.top_errors(None, 15, 20), be.container_stats("shop_api")])
    # label words can appear naturally ("deadlock detected"); the ground-truth field itself must not
    assert "ground_truth" not in blob and "'label'" not in blob and "scenario_id" not in blob


def test_scenario_sets_are_disjoint_and_complete():
    assert len(MVP_IDS) == 10 and len(HELDOUT_IDS) == 20
    assert not set(MVP_IDS) & set(HELDOUT_IDS)
    assert len(SCENARIOS) == 30


def test_baseline_parses_and_runs_without_tools():
    from diag.agent.llm import LLMResponse
    from diag.baseline import parse_category, run_baseline

    class Stub:
        model = "stub"

        def complete(self, system, messages, tools):
            assert tools == [] and "Evidence collected" in messages[0]["content"]
            return LLMResponse('noise {"category": "bad_deploy", "confidence": 0.8, "reason": "x"}', [], 10, 5)

    row = run_baseline(11, Stub(), "context")
    assert row["got"] == "bad_deploy" and row["expected"] == "bad_deploy"
    assert parse_category("not json")[0] == "unknown"
    assert parse_category('{"category": "made_up"}')[0] == "unknown"
