from collections.abc import Callable, Sequence
from threading import RLock
from typing import Literal, TypedDict, cast

from chromadb.errors import ChromaError
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from openai import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from smartdesk.answers import DraftGenerator, GroundedAnswer, generate_grounded_answer
from smartdesk.retrieval import SearchResult
from smartdesk.tickets import (
    PUBLIC_TICKET_WARNING,
    GitHubTicketClient,
    IssueInfo,
    TicketDraft,
    TicketError,
    TicketWorkflow,
)

Search = Callable[[str], Sequence[SearchResult]]


class WorkflowState(TypedDict, total=False):
    action: Literal["question", "ticket", "status"]
    query: str
    results: list[SearchResult]
    answer: GroundedAnswer | None
    draft: TicketDraft | None
    ticket_key: str
    fingerprint: str
    preview: str
    issue_number: int
    issue: IssueInfo | None
    escalation_offered: bool
    stage: Literal[
        "retrieval",
        "answered",
        "escalation",
        "clarification",
        "review",
        "submit",
        "submitted",
        "cancelled",
        "uncertain",
        "status",
        "error",
    ]
    error: str | None


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    action: Literal["confirm", "edit", "cancel"]
    fingerprint: str = ""
    approved: bool = Field(default=False, strict=True)
    synthetic_data_only: bool = Field(default=False, strict=True)
    draft: TicketDraft | None = None


class WorkflowError(ValueError):
    pass


class SmartDeskWorkflow:
    def __init__(
        self,
        search: Search,
        generate: DraftGenerator,
        ticket_client: GitHubTicketClient | None = None,
    ) -> None:
        self._search = search
        self._generate = generate
        self._client = ticket_client
        self._tickets: dict[str, TicketWorkflow] = {}
        self._lock = RLock()
        builder = StateGraph(WorkflowState)
        builder.add_node("route", lambda state: {})
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("answer", self._answer)
        builder.add_node("offer", self._offer)
        builder.add_node("draft_ticket", self._draft_ticket)
        builder.add_node("review", self._review)
        builder.add_node("submit", self._submit)
        builder.add_node("lookup", self._lookup)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            lambda state: state["action"],
            {
                "question": "retrieve",
                "ticket": "draft_ticket",
                "status": "lookup",
            },
        )
        builder.add_conditional_edges(
            "retrieve",
            lambda state: "stop" if state["error"] else "answer",
            {
                "stop": END,
                "answer": "answer",
            },
        )
        builder.add_conditional_edges(
            "answer",
            lambda state: "offer" if state["stage"] == "escalation" else "stop",
            {
                "offer": "offer",
                "stop": END,
            },
        )
        builder.add_conditional_edges(
            "draft_ticket",
            lambda state: "review" if state["stage"] == "review" else "stop",
            {
                "review": "review",
                "stop": END,
            },
        )
        builder.add_conditional_edges(
            "review",
            lambda state: state["stage"],
            {
                "review": "review",
                "submit": "submit",
                "cancelled": END,
                "error": END,
            },
        )
        for node in ("offer", "submit", "lookup"):
            builder.add_edge(node, END)
        self._graph = builder.compile(checkpointer=MemorySaver())

    @staticmethod
    def _config(thread_id: str) -> dict:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise WorkflowError("A nonempty workflow thread ID is required")
        return {"configurable": {"thread_id": thread_id}}

    def snapshot(self, thread_id: str) -> WorkflowState:
        with self._lock:
            return cast(WorkflowState, self._graph.get_state(self._config(thread_id)).values)

    def _begin(self, state: WorkflowState, thread_id: str) -> WorkflowState:
        with self._lock:
            config = self._config(thread_id)
            if self._graph.get_state(config).values:
                raise WorkflowError(
                    "This thread already exists. Resume its review or use a new thread ID."
                )
            self._graph.invoke(
                {
                    "results": [],
                    "answer": None,
                    "draft": None,
                    "issue": None,
                    "escalation_offered": False,
                    "error": None,
                    **state,
                },
                config,
            )
            return self.snapshot(thread_id)

    def ask(self, query: str, thread_id: str) -> WorkflowState:
        if not isinstance(query, str):
            raise WorkflowError("The question must be text")
        return self._begin({"action": "question", "query": query}, thread_id)

    def draft_ticket(self, draft: TicketDraft, thread_id: str) -> WorkflowState:
        validated = TicketDraft.model_validate(draft.model_dump())
        return self._begin({"action": "ticket", "draft": validated}, thread_id)

    def lookup(self, number: int, thread_id: str) -> WorkflowState:
        if type(number) is not int or number < 1:
            raise WorkflowError("Issue number must be a positive integer")
        return self._begin({"action": "status", "issue_number": number}, thread_id)

    def review(self, thread_id: str, decision: object) -> WorkflowState:
        with self._lock:
            config = self._config(thread_id)
            snapshot = self._graph.get_state(config)
            if not snapshot.values:
                raise WorkflowError("No ticket review exists for this thread")
            if not snapshot.next:
                return cast(WorkflowState, snapshot.values)
            if snapshot.next != ("review",):
                raise WorkflowError("This thread is not waiting for ticket review")
            self._graph.invoke(Command(resume=decision), config)
            return self.snapshot(thread_id)

    def _retrieve(self, state: WorkflowState) -> WorkflowState:
        if not state["query"].strip():
            return {"results": [], "stage": "retrieval", "error": None}
        try:
            return {"results": list(self._search(state["query"])), "stage": "retrieval"}
        except (OSError, ValueError, APIError, ChromaError):
            return {
                "stage": "error",
                "error": "Knowledge retrieval is unavailable. Please try again.",
            }

    def _answer(self, state: WorkflowState) -> WorkflowState:
        answer = generate_grounded_answer(state["query"], state["results"], self._generate)
        stage = "escalation" if answer.status == "abstained" else answer.status
        return {
            "answer": answer,
            "stage": stage,
            "error": answer.message if stage == "error" else None,
        }

    @staticmethod
    def _offer(state: WorkflowState) -> WorkflowState:
        return {"escalation_offered": True}

    def _draft_ticket(self, state: WorkflowState) -> WorkflowState:
        if self._client is None:
            return {"stage": "error", "error": "GitHub tickets are not configured."}
        draft = state["draft"]
        if draft is None or draft.draft_id in self._tickets:
            return {
                "stage": "error",
                "error": "Use a new draft or resume its existing review thread.",
            }
        self._tickets[draft.draft_id] = TicketWorkflow(self._client, draft)
        return {
            "ticket_key": draft.draft_id,
            "stage": "review",
            "preview": draft.preview(self._client.repository),
            "fingerprint": draft.fingerprint(self._client.repository),
        }

    def _review(self, state: WorkflowState) -> WorkflowState:
        response = interrupt(
            {
                "kind": "ticket_review",
                "warning": PUBLIC_TICKET_WARNING,
                "draft": state["draft"].model_dump(),
                "fingerprint": state["fingerprint"],
                "preview": state["preview"],
            }
        )
        workflow = self._tickets.get(state["ticket_key"])
        if workflow is None:
            return {
                "stage": "error",
                "error": "Ticket review state is unavailable. No submission was made.",
            }
        try:
            decision = ReviewDecision.model_validate(response)
            if decision.action == "cancel":
                workflow.cancel()
                return {"stage": "cancelled", "error": None}
            if decision.action == "edit":
                if decision.draft is None:
                    raise TicketError("An edited draft is required")
                owner = self._tickets.get(decision.draft.draft_id)
                if owner is not None and owner is not workflow:
                    raise TicketError("This draft already belongs to another workflow")
                workflow.replace_draft(decision.draft)
                self._tickets[workflow.draft.draft_id] = workflow
                return {
                    "draft": workflow.draft,
                    "stage": "review",
                    "error": None,
                    "preview": workflow.draft.preview(workflow.client.repository),
                    "fingerprint": workflow.draft.fingerprint(workflow.client.repository),
                }
            if decision.draft is not None:
                raise TicketError("Edits must be reviewed before confirmation")
            workflow.confirm(
                decision.fingerprint,
                approved=decision.approved,
                synthetic_data_only=decision.synthetic_data_only,
            )
            return {"stage": "submit", "error": None}
        except (ValidationError, TicketError):
            return {
                "stage": "review",
                "error": "Review the current draft and explicitly confirm, edit, or cancel.",
            }

    def _submit(self, state: WorkflowState) -> WorkflowState:
        workflow = self._tickets.get(state["ticket_key"])
        if (
            workflow is None
            or workflow.draft.fingerprint(workflow.client.repository) != state["fingerprint"]
        ):
            return {
                "stage": "error",
                "error": "The confirmed ticket draft is unavailable or has changed.",
            }
        try:
            issue = workflow.submit()
            return {"issue": issue, "stage": "submitted", "error": None}
        except TicketError:
            if workflow.state == "uncertain":
                return {
                    "stage": "uncertain",
                    "error": "Submission outcome is unknown. Check GitHub for the draft ID before any new submission.",
                }
            return {
                "stage": "error",
                "error": "Ticket submission failed. Check repository access and permissions.",
            }

    def _lookup(self, state: WorkflowState) -> WorkflowState:
        if self._client is None:
            return {"stage": "error", "error": "GitHub tickets are not configured."}
        try:
            return {"issue": self._client.get_issue(state["issue_number"]), "stage": "status"}
        except (TicketError, ValueError):
            return {
                "stage": "error",
                "error": "Issue lookup failed. Check its number and repository access.",
            }
