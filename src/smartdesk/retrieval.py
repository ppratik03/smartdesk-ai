import hashlib
import json
import re
from collections.abc import Callable, Sequence
from math import sqrt
from pathlib import Path
from typing import Annotated

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from rank_bm25 import BM25Okapi


class KnowledgeEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    domain: str
    category: str
    question: str
    question_aliases: list[str] = Field(default_factory=list)
    answer: str
    keywords: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    entry: KnowledgeEntry
    dense_score: float
    sparse_score: float
    confidence: float
    answerable: bool


DenseSearch = Callable[[str, int], Sequence[tuple[str, float]]]
DEFAULT_KNOWLEDGE_BASE = Path(__file__).resolve().parents[2] / "data" / "knowledge_base.json"
_ENTRY_LIST = TypeAdapter(Annotated[list[KnowledgeEntry], Field(min_length=1)])


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def load_knowledge_base(path: Path = DEFAULT_KNOWLEDGE_BASE) -> list[KnowledgeEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = _ENTRY_LIST.validate_python(data)
    identifiers = [entry.id for entry in entries]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Knowledge base entry IDs must be unique")
    return entries


def create_chroma_dense_search(
    entries: Sequence[KnowledgeEntry],
    persist_directory: Path,
    collection_name: str = "smartdesk_knowledge",
) -> DenseSearch:
    embedding_model = "text-embedding-3-small"
    index_content = json.dumps(
        {
            "model": embedding_model,
            "schema": 1,
            "entries": [entry.model_dump() for entry in sorted(entries, key=lambda entry: entry.id)],
        },
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(index_content.encode("utf-8")).hexdigest()[:16]
    vector_store = Chroma(
        collection_name=f"{collection_name}_{fingerprint}",
        embedding_function=OpenAIEmbeddings(model=embedding_model),
        persist_directory=str(persist_directory),
        collection_metadata={"hnsw:space": "l2"},
        relevance_score_fn=lambda distance: min(1.0, max(0.0, 1.0 - distance / sqrt(2))),
    )
    existing_ids = set(vector_store.get().get("ids", []))
    missing_entries = [entry for entry in entries if entry.id not in existing_ids]

    documents = [
        Document(
            page_content=(
                f"Question: {entry.question}\n"
                f"Alternative questions: {'; '.join(entry.question_aliases)}\n"
                f"Answer: {entry.answer}\n"
                f"Category: {entry.category}\n"
                f"Keywords: {', '.join(entry.keywords)}"
            ),
            metadata={"entry_id": entry.id, "domain": entry.domain},
        )
        for entry in missing_entries
    ]
    if documents:
        vector_store.add_documents(documents, ids=[entry.id for entry in missing_entries])

    def search(query: str, limit: int) -> Sequence[tuple[str, float]]:
        matches = vector_store.similarity_search_with_relevance_scores(query, k=limit)
        return [
            (document.metadata["entry_id"], float(score))
            for document, score in matches
        ]

    return search


class HybridRetriever:
    def __init__(
        self,
        entries: Sequence[KnowledgeEntry],
        dense_search: DenseSearch,
        confidence_threshold: float = 0.55,
        minimum_dense_score: float = 0.35,
        dense_weight: float = 0.60,
        top_k: int = 5,
    ) -> None:
        if not entries:
            raise ValueError("At least one knowledge entry is required")
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not 0 <= minimum_dense_score <= 1:
            raise ValueError("minimum_dense_score must be between 0 and 1")
        if not 0 <= dense_weight <= 1:
            raise ValueError("dense_weight must be between 0 and 1")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        self.entries = list(entries)
        self.dense_search = dense_search
        self.confidence_threshold = confidence_threshold
        self.minimum_dense_score = minimum_dense_score
        self.dense_weight = dense_weight
        self.top_k = top_k
        self.bm25 = BM25Okapi(
            [
                tokenize(
                    " ".join(
                        [entry.question, *entry.question_aliases, entry.answer, *entry.keywords]
                    )
                )
                for entry in entries
            ]
        )

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        result_limit = self.top_k if limit is None else limit
        if result_limit < 1:
            raise ValueError("limit must be at least 1")

        raw_sparse_scores = self.bm25.get_scores(query_tokens)
        maximum_sparse_score = max(
            (max(0.0, float(score)) for score in raw_sparse_scores),
            default=0.0,
        )
        sparse_scores = {
            entry.id: max(0.0, float(score)) / maximum_sparse_score
            if maximum_sparse_score > 0
            else 0.0
            for entry, score in zip(self.entries, raw_sparse_scores, strict=True)
        }
        dense_scores = {
            entry_id: min(1.0, max(0.0, float(score)))
            for entry_id, score in self.dense_search(query, result_limit)
        }

        ranked: list[SearchResult] = []
        for entry in self.entries:
            dense_score = dense_scores.get(entry.id, 0.0)
            sparse_score = sparse_scores[entry.id]
            confidence = (
                self.dense_weight * dense_score
                + (1.0 - self.dense_weight) * sparse_score
            )
            ranked.append(
                SearchResult(
                    entry=entry,
                    dense_score=dense_score,
                    sparse_score=sparse_score,
                    confidence=confidence,
                    answerable=(
                        dense_score >= self.minimum_dense_score
                        and confidence >= self.confidence_threshold
                    ),
                )
            )

        ranked.sort(key=lambda result: (-result.confidence, result.entry.id))
        return ranked[:result_limit]
