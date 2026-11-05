from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from pydantic import SecretStr

from smartdesk.answers import AnswerDraft, EvidenceQuote
from smartdesk.config import Settings
from smartdesk.retrieval import KnowledgeEntry, SearchResult
from smartdesk.tickets import GitHubTicketClient, TicketDraft
from smartdesk.workflow import SmartDeskWorkflow, WorkflowError


@pytest.fixture
def services():
    entry = KnowledgeEntry(
        id="it-reset",
        domain="IT",
        category="Account",
        question="Reset password?",
        answer="Use the fictional account portal to reset your password.",
    )
    result = SearchResult(
        entry=entry, dense_score=0.8, sparse_score=1.0, confidence=0.88, answerable=True
    )
    search = MagicMock(return_value=[result])
    generate = MagicMock(
        return_value=AnswerDraft(
            status="answered", citations=[EvidenceQuote(entry_id=entry.id, quote=entry.answer)]
        )
    )
    settings = Settings(
        openai_api_key=None,
        github_token=SecretStr("synthetic-token"),
        github_repository="example/helpdesk",
        chroma_persist_directory=Path(".data/chroma"),
        chat_model="synthetic",
    )
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 201
    response.headers = {}
    response.json.return_value = {
        "number": 12,
        "title": "Synthetic monitor",
        "state": "open",
        "html_url": "https://github.com/example/helpdesk/issues/12",
        "labels": [],
    }
    session.request.return_value = response
    workflow = SmartDeskWorkflow(search, generate, GitHubTicketClient(settings, session))
    return workflow, search, generate, session


@pytest.fixture
def draft():
    return TicketDraft(
        title="Synthetic monitor", description="A fictional monitor flickers.", domain="IT"
    )


def approval(state):
    return {
        "action": "confirm",
        "fingerprint": state["fingerprint"],
        "approved": True,
        "synthetic_data_only": True,
    }


def test_supported_question_returns_cited_answer_without_github(services):
    workflow, search, generate, session = services
    result = workflow.ask("How do I reset my password?", "question-1")
    assert result["stage"] == "answered"
    assert result["answer"].sources[0].entry_id == "it-reset"
    assert result["escalation_offered"] is False
    search.assert_called_once()
    generate.assert_called_once()
    session.request.assert_not_called()


def test_unsupported_question_only_offers_escalation(services):
    workflow, search, generate, session = services
    search.return_value = []
    result = workflow.ask("Office parking?", "question-1")
    assert result["stage"] == "escalation"
    assert result["escalation_offered"] is True
    assert result["draft"] is None
    generate.assert_not_called()
    session.request.assert_not_called()


def test_blank_question_clarifies_without_api_calls(services):
    workflow, search, generate, session = services
    assert workflow.ask("  ", "blank")["stage"] == "clarification"
    search.assert_not_called()
    generate.assert_not_called()
    session.request.assert_not_called()


def test_draft_interrupts_and_cancel_never_writes(services, draft):
    workflow, search, generate, session = services
    state = workflow.draft_ticket(draft, "ticket-1")
    assert state["stage"] == "review"
    assert "public" in state["preview"]
    assert workflow.review("ticket-1", {"action": "cancel"})["stage"] == "cancelled"
    search.assert_not_called()
    generate.assert_not_called()
    session.request.assert_not_called()


@pytest.mark.parametrize(
    "decision",
    [
        "yes",
        {"action": "confirm"},
        {"action": "confirm", "approved": "true", "synthetic_data_only": True},
        {
            "action": "confirm",
            "fingerprint": "stale",
            "approved": True,
            "synthetic_data_only": True,
        },
    ],
)
def test_invalid_approval_reinterrupts_without_write(services, draft, decision):
    workflow, _, _, session = services
    workflow.draft_ticket(draft, "ticket-1")
    result = workflow.review("ticket-1", decision)
    assert result["stage"] == "review"
    assert result["error"] is not None
    session.request.assert_not_called()
    assert workflow.review("ticket-1", {"action": "cancel"})["stage"] == "cancelled"


def test_matching_confirmation_submits_once_and_resume_is_idempotent(services, draft):
    workflow, _, _, session = services
    state = workflow.draft_ticket(draft, "ticket-1")
    decision = approval(state)
    result = workflow.review("ticket-1", decision)
    assert result["stage"] == "submitted"
    assert result["issue"].number == 12
    assert workflow.review("ticket-1", decision)["issue"].number == 12
    assert workflow.snapshot("ticket-1")["stage"] == "submitted"
    assert session.request.call_count == 1


def test_edit_requires_fresh_confirmation(services, draft):
    workflow, _, _, session = services
    state = workflow.draft_ticket(draft, "ticket-1")
    stale = approval(state)
    edited = TicketDraft(
        title="Edited synthetic monitor", description=draft.description, domain="IT"
    )
    result = workflow.review("ticket-1", {"action": "edit", "draft": edited.model_dump()})
    assert result["draft"].title == edited.title
    assert result["fingerprint"] != state["fingerprint"]
    assert workflow.review("ticket-1", stale)["stage"] == "review"
    session.request.assert_not_called()
    assert workflow.review("ticket-1", approval(result))["stage"] == "submitted"
    assert session.request.call_count == 1


def test_followup_chat_cannot_replace_pending_review(services, draft):
    workflow, _, _, session = services
    workflow.draft_ticket(draft, "ticket-1")
    with pytest.raises(WorkflowError, match="thread already exists"):
        workflow.ask("yes create the issue", "ticket-1")
    assert workflow.snapshot("ticket-1")["stage"] == "review"
    session.request.assert_not_called()


def test_uncertain_submission_never_retries(services, draft):
    workflow, _, _, session = services
    session.request.side_effect = requests.Timeout("synthetic-private-content")
    state = workflow.draft_ticket(draft, "ticket-1")
    result = workflow.review("ticket-1", approval(state))
    assert result["stage"] == "uncertain"
    assert "synthetic-private-content" not in result["error"]
    assert workflow.review("ticket-1", approval(state))["stage"] == "uncertain"
    assert session.request.call_count == 1


def test_status_route_is_read_only(services):
    workflow, search, generate, session = services
    session.request.return_value.status_code = 200
    assert workflow.lookup(12, "status-1")["stage"] == "status"
    assert session.request.call_args.args[0] == "GET"
    search.assert_not_called()
    generate.assert_not_called()


def test_retrieval_and_lookup_errors_are_sanitized(services):
    workflow, search, _, session = services
    search.side_effect = ValueError("synthetic-private-content")
    state = workflow.ask("Password reset?", "question-1")
    assert state["stage"] == "error"
    assert "synthetic-private-content" not in state["error"]
    session.request.return_value.status_code = 404
    assert workflow.lookup(12, "status-1")["stage"] == "error"


def test_missing_github_configuration_does_not_block_answers(services, draft):
    _, search, generate, _ = services
    workflow = SmartDeskWorkflow(search, generate)
    assert workflow.ask("Password reset?", "question-1")["stage"] == "answered"
    assert workflow.draft_ticket(draft, "ticket-1")["stage"] == "error"


def test_duplicate_draft_cannot_start_second_workflow(services, draft):
    workflow, _, _, session = services
    workflow.draft_ticket(draft, "ticket-1")
    assert workflow.draft_ticket(draft, "ticket-2")["stage"] == "error"
    session.request.assert_not_called()


def test_model_can_abstain_even_when_retrieval_is_confident(services):
    workflow, _, generate, session = services
    generate.return_value = AnswerDraft(status="abstained", citations=[])
    result = workflow.ask("What is my personal leave balance?", "question-1")
    assert result["stage"] == "escalation"
    assert result["escalation_offered"] is True
    session.request.assert_not_called()


def test_free_text_confirmation_is_only_a_question_on_new_thread(services):
    workflow, _, _, session = services
    assert workflow.ask("yes create a ticket", "question-1")["action"] == "question"
    session.request.assert_not_called()


def test_confirm_cannot_silently_ignore_edited_draft(services, draft):
    workflow, _, _, session = services
    state = workflow.draft_ticket(draft, "ticket-1")
    edited = TicketDraft(title="Edited synthetic issue", description=draft.description, domain="IT")
    decision = {**approval(state), "draft": edited.model_dump()}
    assert workflow.review("ticket-1", decision)["stage"] == "review"
    session.request.assert_not_called()


def test_edited_draft_cannot_start_second_workflow(services, draft):
    workflow, _, _, session = services
    workflow.draft_ticket(draft, "ticket-1")
    edited = TicketDraft(title="Edited synthetic issue", description=draft.description, domain="IT")
    workflow.review("ticket-1", {"action": "edit", "draft": edited.model_dump()})
    assert workflow.draft_ticket(edited, "ticket-2")["stage"] == "error"
    session.request.assert_not_called()


def test_edit_cannot_adopt_another_pending_draft(services, draft):
    workflow, _, _, session = services
    workflow.draft_ticket(draft, "ticket-1")
    other = TicketDraft(
        title="Other synthetic issue", description="Fictional payroll problem.", domain="Payroll"
    )
    workflow.draft_ticket(other, "ticket-2")
    state = workflow.review("ticket-1", {"action": "edit", "draft": other.model_dump()})
    assert state["stage"] == "review"
    assert state["draft"] == draft
    session.request.assert_not_called()


def test_cancelled_review_cannot_be_approved_later(services, draft):
    workflow, _, _, session = services
    state = workflow.draft_ticket(draft, "ticket-1")
    workflow.review("ticket-1", {"action": "cancel"})
    assert workflow.review("ticket-1", approval(state))["stage"] == "cancelled"
    session.request.assert_not_called()


def test_definite_submission_failure_does_not_retry_on_resume(services, draft):
    workflow, _, _, session = services
    session.request.return_value.status_code = 403
    state = workflow.draft_ticket(draft, "ticket-1")
    assert workflow.review("ticket-1", approval(state))["stage"] == "error"
    assert workflow.review("ticket-1", approval(state))["stage"] == "error"
    assert session.request.call_count == 1
