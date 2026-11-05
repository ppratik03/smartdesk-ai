from unittest.mock import MagicMock

import pytest

from smartdesk.answers import (
    ABSTENTION_MESSAGE,
    AnswerDraft,
    EvidenceQuote,
    generate_grounded_answer,
)
from smartdesk.retrieval import HybridRetriever, load_knowledge_base


@pytest.fixture
def password_results():
    retriever = HybridRetriever(
        load_knowledge_base(), lambda _query, _limit: [("it-password-reset", 0.9)],
    )
    return retriever.search("How do I reset my company password?")


def test_answer_renders_only_verified_excerpts(password_results) -> None:
    entry = password_results[0].entry
    generate = MagicMock(return_value=AnswerDraft(
        status="answered", citations=[EvidenceQuote(entry_id=entry.id, quote=entry.answer)],
    ))
    answer = generate_grounded_answer("reset password", password_results, generate)
    assert answer.status == "answered"
    assert answer.message == entry.answer
    assert answer.sources[0].entry_id == "it-password-reset"
    assert answer.sources[0].excerpt == entry.answer


@pytest.mark.parametrize("query", ["office parking", "   "])
def test_no_evidence_or_blank_input_never_calls_model(query) -> None:
    generate = MagicMock()
    answer = generate_grounded_answer(query, [], generate)
    assert answer.status in {"abstained", "clarification"}
    assert not answer.sources
    generate.assert_not_called()


def test_low_confidence_skips_model(password_results) -> None:
    results = [result.model_copy(update={"answerable": False}) for result in password_results]
    generate = MagicMock()
    assert generate_grounded_answer("reset password", results, generate).status == "abstained"
    generate.assert_not_called()


@pytest.mark.parametrize("entry_id,quote", [
    ("unknown-source", "An invented policy says everyone gets unlimited leave."),
    ("it-password-reset", "An invented policy says everyone gets unlimited leave."),
])
def test_invalid_citations_fail_closed(password_results, entry_id, quote) -> None:
    generate = MagicMock(return_value=AnswerDraft(
        status="answered", citations=[EvidenceQuote(entry_id=entry_id, quote=quote)],
    ))
    answer = generate_grounded_answer("reset password", password_results, generate)
    assert answer.status == "error"
    assert not answer.sources
    assert quote not in answer.message


def test_model_can_abstain_despite_retrieval_confidence(password_results) -> None:
    generate = MagicMock(return_value=AnswerDraft(status="abstained", citations=[]))
    answer = generate_grounded_answer("What is my actual password?", password_results, generate)
    assert answer.status == "abstained"
    assert answer.message == ABSTENTION_MESSAGE


@pytest.mark.parametrize("status", ["answered", "abstained"])
def test_inconsistent_draft_fails_closed(password_results, status) -> None:
    citations = [] if status == "answered" else [EvidenceQuote(
        entry_id=password_results[0].entry.id, quote=password_results[0].entry.answer,
    )]
    generate = MagicMock(return_value=AnswerDraft(status=status, citations=citations))
    assert generate_grounded_answer("reset password", password_results, generate).status == "error"


def test_model_failure_does_not_expose_exception_contents(password_results) -> None:
    generate = MagicMock(side_effect=ValueError("synthetic-secret-do-not-display"))
    answer = generate_grounded_answer("reset password", password_results, generate)
    assert answer.status == "error"
    assert "synthetic-secret-do-not-display" not in answer.message