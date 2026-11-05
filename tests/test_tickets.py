from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from pydantic import SecretStr, ValidationError

from smartdesk.config import Settings
from smartdesk.tickets import (
    PUBLIC_TICKET_WARNING,
    GitHubTicketClient,
    TicketConfirmation,
    TicketDraft,
    TicketError,
    TicketWorkflow,
    TicketWriteUncertain,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openai_api_key=None,
        github_token=SecretStr("synthetic-test-token"),
        github_repository="example/synthetic-helpdesk",
        chroma_persist_directory=Path(".data/chroma"),
        chat_model="synthetic-model",
    )


@pytest.fixture
def draft() -> TicketDraft:
    return TicketDraft(
        title="Synthetic monitor problem", description="A fictional monitor flickers.", domain="IT"
    )


def response(status: int = 201, **changes) -> MagicMock:
    result = MagicMock(spec=requests.Response)
    result.status_code = status
    result.headers = {}
    result.json.return_value = {
        "number": 12,
        "title": "Synthetic monitor problem",
        "state": "open",
        "html_url": "https://github.com/example/synthetic-helpdesk/issues/12",
        "labels": [],
        "updated_at": "2026-10-31T12:00:00Z",
        **changes,
    }
    return result


def confirm(workflow: TicketWorkflow) -> None:
    workflow.confirm(
        workflow.draft.fingerprint(workflow.client.repository),
        approved=True,
        synthetic_data_only=True,
    )


def test_unconfirmed_or_cancelled_draft_never_writes(settings, draft) -> None:
    session = MagicMock(spec=requests.Session)
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    with pytest.raises(TicketError, match="confirm"):
        workflow.submit()
    confirm(workflow)
    workflow.cancel()
    with pytest.raises(TicketError):
        workflow.submit()
    session.request.assert_not_called()


@pytest.mark.parametrize("approved,synthetic", [(False, True), (True, False), (1, True)])
def test_explicit_approval_and_synthetic_acknowledgement_required(
    settings, draft, approved, synthetic
) -> None:
    session = MagicMock(spec=requests.Session)
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    with pytest.raises(TicketError):
        workflow.confirm(
            draft.fingerprint(settings.github_repository),
            approved=approved,
            synthetic_data_only=synthetic,
        )
    session.request.assert_not_called()


def test_edit_invalidates_approval(settings, draft) -> None:
    session = MagicMock(spec=requests.Session)
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    old_fingerprint = draft.fingerprint(settings.github_repository)
    confirm(workflow)
    workflow.replace_draft(
        TicketDraft(title="Edited synthetic title", description=draft.description, domain="IT")
    )
    with pytest.raises(TicketError):
        workflow.submit()
    with pytest.raises(TicketError):
        workflow.confirm(old_fingerprint, approved=True, synthetic_data_only=True)
    session.request.assert_not_called()


def test_rejected_reconfirmation_revokes_prior_approval(settings, draft) -> None:
    session = MagicMock(spec=requests.Session)
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    confirm(workflow)
    with pytest.raises(TicketError):
        workflow.confirm(
            draft.fingerprint(settings.github_repository), approved=False, synthetic_data_only=True
        )
    with pytest.raises(TicketError):
        workflow.submit()
    session.request.assert_not_called()


def test_client_boundary_rejects_other_repository(settings, draft) -> None:
    session = MagicMock(spec=requests.Session)
    client = GitHubTicketClient(settings, session)
    approval = TicketConfirmation(
        repository="other/repo",
        fingerprint=draft.fingerprint("other/repo"),
        approved=True,
        synthetic_data_only=True,
    )
    with pytest.raises(TicketError):
        client.create_issue(draft, approval)
    session.request.assert_not_called()


def test_successful_submission_is_reused_and_has_trace_marker(settings, draft) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response()
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    confirm(workflow)
    issue = workflow.submit()
    assert workflow.submit() == issue
    assert workflow.state == "submitted"
    assert issue.number == 12
    assert session.request.call_count == 1
    call = session.request.call_args
    assert call.args == ("POST", "https://api.github.com/repos/example/synthetic-helpdesk/issues")
    assert draft.draft_id in call.kwargs["json"]["body"]
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["timeout"] == (5, 20)
    assert "labels" not in call.kwargs["json"]


@pytest.mark.parametrize(
    "failure", [requests.Timeout("synthetic-secret"), requests.ConnectionError("synthetic-secret")]
)
def test_uncertain_network_write_is_not_retried(settings, draft, failure) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = failure
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    confirm(workflow)
    with pytest.raises(TicketWriteUncertain) as caught:
        workflow.submit()
    assert "synthetic-secret" not in str(caught.value)
    assert workflow.state == "uncertain"
    with pytest.raises(TicketError):
        workflow.submit()
    with pytest.raises(TicketError):
        workflow.replace_draft(draft)
    assert session.request.call_count == 1


@pytest.mark.parametrize(
    "api_response",
    [
        response(503),
        response(408),
        response(200),
        response(302),
        response(html_url="https://outside.example/issues/12"),
    ],
)
def test_server_error_or_invalid_success_blocks_resubmission(settings, draft, api_response) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = api_response
    workflow = TicketWorkflow(GitHubTicketClient(settings, session), draft)
    confirm(workflow)
    with pytest.raises(TicketWriteUncertain):
        workflow.submit()
    with pytest.raises(TicketError):
        workflow.submit()
    assert session.request.call_count == 1


@pytest.mark.parametrize("number", [0, -1, True, "12", "https://github.com/other/repo/issues/12"])
def test_status_number_validation_prevents_requests(settings, number) -> None:
    session = MagicMock(spec=requests.Session)
    with pytest.raises(ValueError):
        GitHubTicketClient(settings, session).get_issue(number)
    session.request.assert_not_called()


def test_status_uses_configured_repository_and_handles_closed_issue(settings) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response(200, state="closed", labels=[{"name": "resolved"}])
    issue = GitHubTicketClient(settings, session).get_issue(12)
    assert issue.state == "closed"
    assert issue.labels == ["resolved"]
    assert session.request.call_args.args == (
        "GET",
        "https://api.github.com/repos/example/synthetic-helpdesk/issues/12",
    )


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "access denied"),
        (403, "access denied"),
        (404, "not found"),
        (429, "rate limit"),
        (422, "HTTP 422"),
    ],
)
def test_api_errors_are_safe_and_not_retried(settings, status, message) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response(status)
    with pytest.raises(TicketError, match=message):
        GitHubTicketClient(settings, session).get_issue(12)
    assert session.request.call_count == 1
    session.request.return_value.json.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"pull_request": {}},
        {"number": 13},
        {"number": True},
        {"html_url": "https://github.com/other/repo/issues/12"},
        {"state": "unknown"},
    ],
)
def test_invalid_status_responses_fail_closed(settings, changes) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response(200, **changes)
    with pytest.raises(TicketError, match="invalid issue"):
        GitHubTicketClient(settings, session).get_issue(12)


def test_missing_token_blocks_client_and_preview_is_offline(settings, draft) -> None:
    missing = Settings(
        openai_api_key=None,
        github_token=None,
        github_repository=settings.github_repository,
        chroma_persist_directory=Path(".data/chroma"),
        chat_model="synthetic-model",
    )
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubTicketClient(missing)
    assert PUBLIC_TICKET_WARNING in draft.preview(settings.github_repository)
    assert draft.description in draft.preview(settings.github_repository)


@pytest.mark.parametrize("field", ["title", "description"])
def test_blank_draft_fields_are_rejected(field) -> None:
    values = {"title": "Synthetic issue", "description": "Fictional problem", "domain": "IT"}
    values[field] = "   "
    with pytest.raises(ValidationError):
        TicketDraft(**values)


def test_status_network_error_is_sanitized(settings) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = requests.ConnectionError("synthetic-private-content")
    with pytest.raises(TicketError) as caught:
        GitHubTicketClient(settings, session).get_issue(12)
    assert "synthetic-private-content" not in str(caught.value)


def test_rate_limit_header_on_forbidden_response(settings) -> None:
    session = MagicMock(spec=requests.Session)
    api_response = response(403)
    api_response.headers = {"X-RateLimit-Remaining": "0"}
    session.request.return_value = api_response
    with pytest.raises(TicketError, match="rate limit"):
        GitHubTicketClient(settings, session).get_issue(12)


@pytest.mark.parametrize(
    "repository", ["other/repo/extra", "../repo", "owner/..", "https://github.com/owner/repo"]
)
def test_invalid_configured_repository_is_rejected(settings, repository) -> None:
    invalid = Settings(
        openai_api_key=None,
        github_token=settings.github_token,
        github_repository=repository,
        chroma_persist_directory=Path(".data/chroma"),
        chat_model="synthetic-model",
    )
    session = MagicMock(spec=requests.Session)
    with pytest.raises(ValueError):
        GitHubTicketClient(invalid, session)
    session.request.assert_not_called()
