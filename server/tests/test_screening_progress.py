from types import SimpleNamespace

import inspect

from server.app.screening.progress import aggregate_run, summarize_progress


def item(status, llm_status="not_requested"):
    return SimpleNamespace(status=status,llm_status=llm_status)


def test_progress_waits_for_llm_and_preserves_rule_success_on_degradation():
    pending=summarize_progress([item("scored","queued")],1)
    assert pending==(0,0,0,"llm_scoring")

    succeeded=summarize_progress([item("scored","succeeded")],1)
    assert succeeded==(1,1,0,"completed")

    degraded=summarize_progress([item("scored","failed")],1)
    assert degraded==(1,1,0,"partial")


def test_progress_combines_rule_failures_and_llm_states_truthfully():
    mixed=summarize_progress([item("failed"),item("scored","skipped"),item("scored","running")],3)
    assert mixed==(2,1,1,"llm_scoring")

    terminal=summarize_progress([item("failed"),item("scored","skipped"),item("scored","succeeded")],3)
    assert terminal==(3,2,1,"partial")


def test_aggregate_run_only_aggregates_and_does_not_transition_applications():
    source = inspect.getsource(aggregate_run)

    assert "transition_application_record" not in source
    assert "Application.stage" not in source
