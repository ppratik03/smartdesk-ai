from pathlib import Path
from uuid import uuid4

import streamlit as st
from chromadb.errors import ChromaError
from openai import APIError
from pydantic import ValidationError

from smartdesk.answers import create_answer_generator
from smartdesk.config import load_settings
from smartdesk.retrieval import (
    HybridRetriever,
    create_chroma_dense_search,
    load_knowledge_base,
)
from smartdesk.tickets import (
    PUBLIC_TICKET_WARNING,
    GitHubTicketClient,
    IssueInfo,
    TicketDraft,
)
from smartdesk.workflow import SmartDeskWorkflow

ROOT = Path(__file__).resolve().parent


@st.cache_resource(show_spinner=False)
def live_resources():
    settings = load_settings()
    settings.require_openai()
    entries = load_knowledge_base()
    retriever = HybridRetriever(
        entries,
        create_chroma_dense_search(entries, settings.chroma_persist_directory),
    )
    return retriever.search, create_answer_generator(settings)


def workspace() -> dict:
    if "workspace" not in st.session_state:
        previous = st.session_state.get("workspaces", {}).get("Live services")
        if previous is not None:
            st.session_state["workspace"] = previous
            return previous
        settings = load_settings()
        settings.require_openai()

        def search(query):
            retrieve, _ = live_resources()
            return retrieve(query)

        def generate(query, context):
            _, answer = live_resources()
            return answer(query, context)

        client = GitHubTicketClient(settings) if settings.github_token is not None else None
        workflow = SmartDeskWorkflow(search, generate, client)
        st.session_state["workspace"] = {
            "workflow": workflow,
            "messages": [],
            "tickets": [],
            "active": None,
            "compose": False,
        }
    return st.session_state["workspace"]


def ask_question(current: dict, query: str) -> None:
    thread_id = str(uuid4())
    current["messages"].append({"role": "user", "text": query})
    with st.spinner("Checking Northstar knowledge..."):
        current["workflow"].ask(query, thread_id)
    current["messages"].append({"role": "assistant", "thread": thread_id})


def show_issue(issue: IssueInfo) -> None:
    st.markdown(f"**Issue #{issue.number}**")
    st.write(issue.title)
    st.caption(f"{issue.state.upper()} | {', '.join(issue.labels) or 'No labels'}")
    if issue.updated_at:
        st.caption(f"Updated {issue.updated_at}")
    st.link_button("Open GitHub issue", issue.url, icon=":material/open_in_new:")


def show_answer(state: dict) -> None:
    answer = state.get("answer")
    if answer is None:
        st.error(state.get("error") or "The request could not be completed.")
        return
    st.write(answer.message)
    if answer.sources:
        with st.expander(f"Sources ({len(answer.sources)})"):
            for source in answer.sources:
                st.markdown(f"**{source.domain} / {source.entry_id}**")
                st.write(source.question)
                st.caption(source.excerpt)


def conversation(current: dict) -> None:
    for message in current["messages"]:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.write(message["text"])
                continue
            state = current["workflow"].snapshot(message["thread"])
            show_answer(state)
            if state.get("escalation_offered") and st.button(
                "Draft ticket",
                key=f"escalate-{message['thread']}",
                icon=":material/add:",
                disabled=current["compose"] or current["active"] is not None,
            ):
                current["compose"] = True
                st.rerun()


def compose_ticket(current: dict) -> None:
    st.subheader("New ticket")
    st.warning(PUBLIC_TICKET_WARNING)
    with st.form("new-ticket"):
        title = st.text_input("Title", max_chars=200)
        domain = st.selectbox("Team", ["IT", "HR", "Onboarding", "Payroll"])
        description = st.text_area("Synthetic description", max_chars=4000, height=150)
        preview = st.form_submit_button("Review draft", icon=":material/visibility:")
    if preview:
        try:
            draft = TicketDraft(title=title, description=description, domain=domain)
            thread_id = str(uuid4())
            state = current["workflow"].draft_ticket(draft, thread_id)
            if state["stage"] == "review":
                current["active"] = thread_id
                current["compose"] = False
                st.rerun()
            st.error(state.get("error") or "Ticket review is unavailable.")
        except ValidationError:
            st.error("Enter a title and synthetic description.")
    if st.button("Discard draft", icon=":material/close:"):
        current["compose"] = False
        st.rerun()


def edit_ticket(current: dict, state: dict) -> None:
    draft = state["draft"]
    revision = state["fingerprint"]
    with st.expander("Edit draft"):
        with st.form(f"edit-{revision}"):
            title = st.text_input("Title", draft.title, max_chars=200)
            teams = ["IT", "HR", "Onboarding", "Payroll"]
            domain = st.selectbox("Team", teams, index=teams.index(draft.domain))
            description = st.text_area("Synthetic description", draft.description, max_chars=4000)
            save = st.form_submit_button("Save changes", icon=":material/save:")
        if save:
            try:
                edited = TicketDraft(title=title, description=description, domain=domain)
                current["workflow"].review(
                    current["active"],
                    {
                        "action": "edit",
                        "draft": edited.model_dump(),
                    },
                )
                st.rerun()
            except ValidationError:
                st.error("Enter a title and synthetic description.")


def ticket_result(current: dict, state: dict) -> None:
    if state.get("issue"):
        st.success("GitHub issue created.")
        show_issue(state["issue"])
    elif state["stage"] == "cancelled":
        st.info("Draft cancelled. No issue was created.")
    else:
        st.error(state.get("error") or "Submission could not be completed.")
        st.caption(f"Draft ID: {state['draft'].draft_id}")
    if state["stage"] != "uncertain" and st.button("Done", icon=":material/check:"):
        current["tickets"].append(current["active"])
        current["active"] = None
        st.rerun()


def review_ticket(current: dict) -> None:
    thread_id = current["active"]
    state = current["workflow"].snapshot(thread_id)
    if state["stage"] != "review":
        ticket_result(current, state)
        return
    draft = state["draft"]
    st.subheader("Review ticket")
    st.warning(PUBLIC_TICKET_WARNING)
    st.caption(load_settings().github_repository)
    st.markdown(f"**{draft.title}**")
    st.caption(draft.domain)
    st.write(draft.description)
    if state.get("error"):
        st.error(state["error"])
    edit_ticket(current, state)
    acknowledged = st.checkbox(
        "This ticket contains synthetic data only", key=f"ack-{state['fingerprint']}"
    )
    columns = st.columns(2)
    if columns[0].button(
        "Confirm public issue",
        disabled=not acknowledged,
        type="primary",
        icon=":material/send:",
    ):
        with st.spinner("Submitting reviewed ticket..."):
            current["workflow"].review(
                thread_id,
                {
                    "action": "confirm",
                    "fingerprint": state["fingerprint"],
                    "approved": True,
                    "synthetic_data_only": acknowledged,
                },
            )
        st.rerun()
    if columns[1].button("Cancel", icon=":material/close:"):
        current["workflow"].review(thread_id, {"action": "cancel"})
        st.rerun()


def sidebar(current: dict) -> None:
    with st.sidebar:
        st.subheader("Northstar Works")
        st.caption("IT / HR / ONBOARDING / PAYROLL")
        st.warning("Questions may incur OpenAI charges. Public issues require confirmation.")
        if st.button("New ticket", icon=":material/add:", disabled=current["active"] is not None):
            current["compose"] = True
            st.rerun()
        st.divider()
        st.subheader("Ticket status")
        with st.form("status-lookup"):
            number = st.number_input("Issue number", min_value=1, step=1, value=1)
            lookup = st.form_submit_button("Check status", icon=":material/search:")
        if lookup:
            thread_id = str(uuid4())
            current["workflow"].lookup(int(number), thread_id)
            current["status_thread"] = thread_id
        if current.get("status_thread"):
            state = current["workflow"].snapshot(current["status_thread"])
            if state.get("issue"):
                show_issue(state["issue"])
            else:
                st.error(state.get("error") or "Issue not found.")
        if current["tickets"]:
            st.divider()
            st.subheader("Recent tickets")
            for thread_id in reversed(current["tickets"][-5:]):
                state = current["workflow"].snapshot(thread_id)
                if state.get("issue"):
                    show_issue(state["issue"])


def main() -> None:
    st.set_page_config(
        page_title="SmartDesk AI", page_icon="assets/smartdesk-mark.svg", layout="wide"
    )
    st.markdown(
        """<style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
    :root { --desk-green: #16705b; --desk-ink: #20332d; }
    html, body, input, textarea, button, p, h1, h2, h3, label {font-family:'IBM Plex Sans',sans-serif; letter-spacing:0;}
    .stApp {background:linear-gradient(180deg,#f3f7f5 0px,#fff 220px); color:var(--desk-ink);}
    [data-testid="stSidebar"] {background:#edf2f4; border-right:1px solid #d6dfe1;}
    .block-container {max-width:1120px; padding-top:4.75rem; padding-bottom:3rem;}
    h1 {font-size:1.8rem !important;} h2,h3 {font-size:1.2rem !important;}
    [data-testid="stChatMessage"] {border-radius:6px; background:#f4f7f6;}
    button[kind="primary"] {background:var(--desk-green); border-color:var(--desk-green);}
    @media(max-width:640px) {.block-container {padding-left:1rem; padding-right:1rem;}}
    </style>""",
        unsafe_allow_html=True,
    )
    mark, heading = st.columns([1, 12], vertical_alignment="center")
    mark.image(str(ROOT / "assets" / "smartdesk-mark.svg"), width=42)
    heading.title("SmartDesk AI")
    try:
        with st.spinner("Preparing help desk..."):
            current = workspace()
    except (OSError, ValueError, APIError, ChromaError):
        st.error("Services could not be initialized. Check local configuration and network access.")
        return
    sidebar(current)
    if not current["messages"]:
        st.subheader("Northstar help desk")
        entries = {entry.id: entry for entry in load_knowledge_base()}
        suggestions = [
            ("Password reset", entries["it-password-reset"].question),
            ("Sick leave", entries["hr-sick-leave"].question),
            ("Office parking", "How do I get an office parking permit?"),
        ]
        for column, (label, query) in zip(st.columns(3), suggestions, strict=True):
            if column.button(label, icon=":material/chat:", use_container_width=True):
                ask_question(current, query)
                st.rerun()
    conversation(current)
    if current["active"] is not None:
        review_ticket(current)
    elif current["compose"]:
        compose_ticket(current)
    if query := st.chat_input("Ask Northstar Works"):
        ask_question(current, query)
        st.rerun()


if __name__ == "__main__":
    main()
