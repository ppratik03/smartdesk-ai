import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smartdesk.evaluation import (
    EvaluationQuery,
    evaluate_retrieval,
    load_evaluation_queries,
    summarize,
)
from smartdesk.retrieval import load_knowledge_base


def test_dataset_is_balanced_and_labels_exist() -> None:
    queries = load_evaluation_queries(load_knowledge_base())
    assert len(queries) == 48
    assert sum(query.split == "dev" for query in queries) == 24
    assert sum(query.split == "test" for query in queries) == 24
    assert sum(bool(query.expected_ids) for query in queries) == 36
    assert {query.domain for query in queries} == {"IT", "HR", "Onboarding", "Payroll"}


def test_metrics_count_wrong_answers_and_cache_dense_calls() -> None:
    queries = [
        EvaluationQuery(id="known", split="dev", domain="IT", kind="exact",
                        query="reset password", expected_ids=["it-password-reset"]),
        EvaluationQuery(id="gap", split="dev", domain="IT", kind="unsupported",
                        query="office parking", expected_ids=[]),
    ]
    dense_search = MagicMock(return_value=[("it-password-reset", 0.9)])
    rows = evaluate_retrieval(load_knowledge_base(), queries, dense_search)
    metrics = summarize(rows)
    assert len(rows) == 6
    assert dense_search.call_count == 2
    assert metrics["dev/hybrid"]["top1_accuracy_supported"] == 1.0
    assert metrics["dev/hybrid"]["recall_at_5_supported"] == 1.0
    assert metrics["dev/dense"]["false_answer_rate_unsupported"] == 1.0
    assert metrics["dev/hybrid"]["false_answer_rate_unsupported"] == 0.0


def test_missing_population_rates_are_not_misreported_as_zero() -> None:
    query = EvaluationQuery(id="known", split="test", domain="IT", kind="exact",
                            query="reset password", expected_ids=["it-password-reset"])
    metrics = summarize(evaluate_retrieval(
        load_knowledge_base(), [query], lambda _query, _limit: [("it-password-reset", 0.9)],
    ))
    assert metrics["test/hybrid"]["false_answer_rate_unsupported"] is None


def test_unknown_label_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{
        "id": "bad", "split": "dev", "domain": "IT", "kind": "exact",
        "query": "anything", "expected_ids": ["nonexistent"],
    }]), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load_evaluation_queries(load_knowledge_base(), path)