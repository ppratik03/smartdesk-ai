from pathlib import Path
from unittest.mock import MagicMock

from smartdesk import retrieval
from smartdesk.retrieval import HybridRetriever, load_knowledge_base

KNOWLEDGE_BASE = Path(__file__).parents[1] / "data" / "knowledge_base.json"


def test_default_knowledge_base_loads_from_checkout() -> None:
    assert load_knowledge_base() == load_knowledge_base(KNOWLEDGE_BASE)


def test_chroma_reuses_index_and_versions_changed_content(tmp_path, monkeypatch) -> None:
    entries = load_knowledge_base()
    store = MagicMock()
    store.get.return_value = {"ids": [entry.id for entry in entries]}
    chroma = MagicMock(return_value=store)
    monkeypatch.setattr(retrieval, "Chroma", chroma)
    monkeypatch.setattr(retrieval, "OpenAIEmbeddings", MagicMock())

    retrieval.create_chroma_dense_search(entries, tmp_path)
    initial_name = chroma.call_args.kwargs["collection_name"]
    relevance = chroma.call_args.kwargs["relevance_score_fn"]
    assert relevance(0.0) == 1.0
    assert relevance(2.0) == 0.0
    assert 0.0 < relevance(0.5) < 1.0
    store.add_documents.assert_not_called()
    store.delete.assert_not_called()

    retrieval.create_chroma_dense_search(list(reversed(entries)), tmp_path)
    assert chroma.call_args.kwargs["collection_name"] == initial_name

    updated = [entries[0].model_copy(update={"answer": "Updated synthetic answer"}), *entries[1:]]
    store.get.return_value = {"ids": []}
    retrieval.create_chroma_dense_search(updated, tmp_path)
    assert chroma.call_args.kwargs["collection_name"] != initial_name
    assert len(store.add_documents.call_args.kwargs["ids"]) == len(entries)
    store.delete.assert_not_called()


def test_chroma_resumes_partial_index(tmp_path, monkeypatch) -> None:
    entries = load_knowledge_base()
    store = MagicMock()
    store.get.return_value = {"ids": [entry.id for entry in entries[1:]]}
    monkeypatch.setattr(retrieval, "Chroma", MagicMock(return_value=store))
    monkeypatch.setattr(retrieval, "OpenAIEmbeddings", MagicMock())

    retrieval.create_chroma_dense_search(entries, tmp_path)

    assert store.add_documents.call_args.kwargs["ids"] == [entries[0].id]
    document = store.add_documents.call_args.args[0][0]
    assert "How do I reset my password?" in document.page_content
    store.delete.assert_not_called()


def test_knowledge_base_has_both_domains_and_deliberate_gaps() -> None:
    entries = load_knowledge_base(KNOWLEDGE_BASE)
    domains = {entry.domain for entry in entries}
    all_content = " ".join(f"{entry.question} {entry.answer}" for entry in entries).lower()

    assert len(entries) >= 30
    assert {"IT", "HR", "Onboarding", "Payroll"} <= domains
    assert "monitor flickering" not in all_content
    assert "office parking" not in all_content


def test_hybrid_retriever_finds_grounded_password_answer() -> None:
    entries = load_knowledge_base(KNOWLEDGE_BASE)
    dense_search = lambda _query, _limit: [("it-password-reset", 0.91)]
    retriever = HybridRetriever(entries, dense_search)

    results = retriever.search("How do I reset my company password?")

    assert results[0].entry.id == "it-password-reset"
    assert results[0].answerable
    assert results[0].confidence >= 0.55


def test_sparse_match_can_outrank_a_dense_only_match() -> None:
    entries = load_knowledge_base(KNOWLEDGE_BASE)
    dense_search = lambda _query, _limit: [
        ("it-device-repair", 0.94),
        ("it-password-reset", 0.52),
    ]
    retriever = HybridRetriever(entries, dense_search)

    results = retriever.search("How do I reset my company password?")

    assert results[0].entry.id == "it-password-reset"
    assert results[0].answerable


def test_hybrid_retriever_abstains_for_uncovered_topic() -> None:
    entries = load_knowledge_base(KNOWLEDGE_BASE)
    dense_search = lambda _query, _limit: [("it-device-repair", 0.18)]
    retriever = HybridRetriever(entries, dense_search)

    results = retriever.search("Where can I get office parking permits?")

    assert results
    assert not results[0].answerable


def test_sparse_match_alone_is_not_treated_as_confident() -> None:
    entries = load_knowledge_base(KNOWLEDGE_BASE)
    retriever = HybridRetriever(entries, lambda _query, _limit: [])

    results = retriever.search("How do I reset my company password?")

    assert results[0].entry.id == "it-password-reset"
    assert not results[0].answerable


def test_blank_query_returns_no_results() -> None:
    retriever = HybridRetriever(load_knowledge_base(KNOWLEDGE_BASE), lambda _query, _limit: [])

    assert retriever.search("  !!!  ") == []
