import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from smartdesk.config import Settings, load_settings

PUBLIC_TICKET_WARNING = (
    "This GitHub issue will be public. Use synthetic data only. "
    "Do not include credentials, employee identities, payslips, or medical details."
)


class TicketError(Exception):
    pass


class TicketWriteUncertain(TicketError):
    pass


class TicketDraft(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    draft_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    domain: Literal["IT", "HR", "Onboarding", "Payroll"]

    def fingerprint(self, repository: str) -> str:
        content = json.dumps(
            {"repository": repository, "draft": self.model_dump()},
            sort_keys=True,
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def preview(self, repository: str) -> str:
        return (
            f"{PUBLIC_TICKET_WARNING}\n\nRepository: {repository}\n"
            f"Title: {self.title}\nDomain: {self.domain}\n\n{self.description}\n\n"
            f"Draft: {self.draft_id}\nConfirmation fingerprint: {self.fingerprint(repository)}"
        )


@dataclass(frozen=True)
class TicketConfirmation:
    repository: str
    fingerprint: str
    approved: bool
    synthetic_data_only: bool

    def validate(self, draft: TicketDraft, repository: str) -> None:
        if self.approved is not True or self.synthetic_data_only is not True:
            raise TicketError("Explicit approval and synthetic-data acknowledgement are required")
        if self.repository != repository or self.fingerprint != draft.fingerprint(repository):
            raise TicketError("Confirmation does not match the current draft and repository")


class IssueInfo(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    number: int = Field(gt=0, strict=True)
    title: str
    state: Literal["open", "closed"]
    url: str
    labels: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class GitHubTicketClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        repository = settings.github_repository
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
            part in {".", ".."} for part in repository.split("/")
        ):
            raise ValueError("GitHub repository must use a valid owner/repository format")
        self.repository = repository
        self._session = session if session is not None else requests.Session()
        self._headers = {
            "Authorization": f"Bearer {settings.require_github().get_secret_value()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._base_url = f"https://api.github.com/repos/{repository}/issues"

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            response = self._session.request(
                method,
                url,
                headers=self._headers,
                json=payload,
                timeout=(5, 20),
                allow_redirects=False,
            )
        except requests.RequestException:
            if method == "POST":
                raise TicketWriteUncertain(
                    "Submission outcome is unknown. Check GitHub for the draft ID before retrying."
                ) from None
            raise TicketError(
                "GitHub could not be reached. Please try the status lookup later."
            ) from None
        expected_status = 201 if method == "POST" else 200
        if response.status_code == expected_status:
            return response
        if method == "POST" and (
            response.status_code >= 500
            or response.status_code == 408
            or 200 <= response.status_code < 400
        ):
            raise TicketWriteUncertain(
                "GitHub submission outcome is uncertain. Check for the draft ID before retrying."
            )
        if response.status_code == 429 or response.headers.get("X-RateLimit-Remaining") == "0":
            raise TicketError("GitHub rate limit reached. Try again after the limit resets.")
        if response.status_code in {401, 403}:
            raise TicketError("GitHub access denied. Check token scope and repository permissions.")
        if response.status_code == 404:
            raise TicketError("Issue or repository not found, or the token cannot access it.")
        raise TicketError(f"GitHub rejected the request (HTTP {response.status_code}).")

    def _parse_issue(
        self, response: requests.Response, expected_number: int | None = None
    ) -> IssueInfo:
        try:
            data = response.json()
            if not isinstance(data, dict) or "pull_request" in data:
                raise ValueError("Response is not an issue")
            issue = IssueInfo(
                number=data["number"],
                title=data["title"],
                state=data["state"],
                url=data["html_url"],
                labels=[label["name"] for label in data.get("labels", [])],
                updated_at=data.get("updated_at"),
            )
            parsed_url = urlsplit(issue.url)
            expected_path = f"/{self.repository}/issues/{issue.number}"
            if (
                parsed_url.scheme != "https"
                or parsed_url.netloc != "github.com"
                or parsed_url.path.casefold() != expected_path.casefold()
                or parsed_url.query
                or parsed_url.fragment
                or (expected_number is not None and issue.number != expected_number)
            ):
                raise ValueError("Response refers to a different issue")
            return issue
        except (ValueError, KeyError, TypeError):
            raise TicketError("GitHub returned an invalid issue response.") from None

    def get_issue(self, number: int) -> IssueInfo:
        if type(number) is not int or number < 1:
            raise ValueError("Issue number must be a positive integer")
        return self._parse_issue(self._request("GET", f"{self._base_url}/{number}"), number)

    def create_issue(self, draft: TicketDraft, confirmation: TicketConfirmation) -> IssueInfo:
        confirmation.validate(draft, self.repository)
        payload = {
            "title": draft.title,
            "body": (
                f"Synthetic SmartDesk AI capstone ticket\n\nDomain: {draft.domain}\n\n"
                f"{draft.description}\n\nSmartDesk draft ID: {draft.draft_id}"
            ),
        }
        response = self._request("POST", self._base_url, payload)
        try:
            return self._parse_issue(response)
        except TicketError:
            raise TicketWriteUncertain(
                "GitHub accepted submission but its response could not be verified. "
                "Check for the draft ID; do not resubmit automatically."
            ) from None


class TicketWorkflow:
    def __init__(self, client: GitHubTicketClient, draft: TicketDraft) -> None:
        self.client = client
        self._draft = draft
        self.state: Literal[
            "draft", "confirmed", "submitting", "submitted", "cancelled", "failed", "uncertain"
        ] = "draft"
        self._confirmation: TicketConfirmation | None = None
        self._issue: IssueInfo | None = None

    @property
    def draft(self) -> TicketDraft:
        return self._draft

    def replace_draft(self, draft: TicketDraft) -> None:
        if self.state not in {"draft", "confirmed", "failed"}:
            raise TicketError("This submission cannot be edited; review its outcome first")
        self._draft = draft
        self._confirmation = None
        self.state = "draft"

    def confirm(self, fingerprint: str, *, approved: bool, synthetic_data_only: bool) -> None:
        if self.state not in {"draft", "confirmed"}:
            raise TicketError("This draft cannot be confirmed in its current state")
        self._confirmation = None
        self.state = "draft"
        confirmation = TicketConfirmation(
            repository=self.client.repository,
            fingerprint=fingerprint,
            approved=approved,
            synthetic_data_only=synthetic_data_only,
        )
        confirmation.validate(self.draft, self.client.repository)
        self._confirmation = confirmation
        self.state = "confirmed"

    def cancel(self) -> None:
        if self.state not in {"draft", "confirmed"}:
            raise TicketError("An attempted submission cannot be cancelled locally")
        self._confirmation = None
        self.state = "cancelled"

    def submit(self) -> IssueInfo:
        if self.state == "submitted" and self._issue is not None:
            return self._issue
        if self.state != "confirmed" or self._confirmation is None:
            raise TicketError("Review and explicitly confirm the current draft before submission")
        self._confirmation.validate(self.draft, self.client.repository)
        self.state = "submitting"
        try:
            self._issue = self.client.create_issue(self.draft, self._confirmation)
        except TicketError as error:
            self.state = "uncertain" if isinstance(error, TicketWriteUncertain) else "failed"
            raise
        self.state = "submitted"
        return self._issue


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview synthetic tickets or read GitHub issue status"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--title", required=True)
    preview.add_argument("--description", required=True)
    preview.add_argument("--domain", choices=["IT", "HR", "Onboarding", "Payroll"], required=True)
    status = subparsers.add_parser("status")
    status.add_argument("number", type=int)
    status.add_argument(
        "--live", action="store_true", help="Read from the configured GitHub repository"
    )
    args = parser.parse_args()
    try:
        settings = load_settings()
        if args.command == "preview":
            draft = TicketDraft(title=args.title, description=args.description, domain=args.domain)
            print(draft.preview(settings.github_repository))
        elif args.live:
            print(GitHubTicketClient(settings).get_issue(args.number).model_dump_json(indent=2))
        else:
            print("No API calls made. Add --live for a read-only status lookup.")
        return 0
    except TicketError as error:
        print(str(error))
        return 1
    except (ValidationError, ValueError):
        print(
            "Ticket operation failed. Check draft fields, issue number, local settings, and access."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
