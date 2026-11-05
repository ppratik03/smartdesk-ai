import argparse
import json
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Literal

from chromadb.errors import ChromaError
from openai import APIError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from smartdesk.config import PROJECT_ROOT, load_settings
from smartdesk.retrieval import (
    DenseSearch,
    HybridRetriever,
    KnowledgeEntry,
    create_chroma_dense_search,
    load_knowledge_base,
)

DEFAULT_EVALUATION = PROJECT_ROOT / "data" / "evaluation_queries.json"


class EvaluationQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    split: Literal["dev", "test"]
    domain: Literal["IT", "HR", "Onboarding", "Payroll"]
    kind: Literal["exact", "paraphrase", "unsupported", "near_miss", "ambiguous"]
    query: str = Field(min_length=1)
    expected_ids: list[str]


class EvaluationRow(BaseModel):
    query_id: str
    split: str
    mode: str
    supported: bool
    top1_correct: bool
    hit_at_k: bool
    answerable: bool
    ranked_ids: list[str]
    top_score: float


def load_evaluation_queries(
    entries: Sequence[KnowledgeEntry], path: Path = DEFAULT_EVALUATION,
) -> list[EvaluationQuery]:
    queries = TypeAdapter(list[EvaluationQuery]).validate_python(
        json.loads(path.read_text(encoding="utf-8"))
    )
    if not queries or len({query.id for query in queries}) != len(queries):
        raise ValueError("Evaluation queries must be nonempty with unique IDs")
    known_ids = {entry.id for entry in entries}
    if any(not set(query.expected_ids) <= known_ids for query in queries):
        raise ValueError("Evaluation labels reference unknown knowledge entries")
    return queries


def evaluate_retrieval(
    entries: Sequence[KnowledgeEntry], queries: Sequence[EvaluationQuery], dense_search: DenseSearch,
) -> list[EvaluationRow]:
    @cache
    def cached_dense_search(query: str, limit: int) -> Sequence[tuple[str, float]]:
        return dense_search(query, limit)

    retrievers = {
        "dense": HybridRetriever(entries, cached_dense_search, dense_weight=1.0),
        "bm25": HybridRetriever(
            entries, lambda _query, _limit: [], dense_weight=0.0, minimum_dense_score=0.0,
        ),
        "hybrid": HybridRetriever(entries, cached_dense_search),
    }
    rows: list[EvaluationRow] = []
    for query in queries:
        for mode, retriever in retrievers.items():
            results = retriever.search(query.query)
            ranked_ids = [result.entry.id for result in results]
            rows.append(EvaluationRow(
                query_id=query.id,
                split=query.split,
                mode=mode,
                supported=bool(query.expected_ids),
                top1_correct=bool(ranked_ids and ranked_ids[0] in query.expected_ids),
                hit_at_k=bool(set(ranked_ids) & set(query.expected_ids)),
                answerable=bool(results and results[0].answerable),
                ranked_ids=ranked_ids,
                top_score=results[0].confidence if results else 0.0,
            ))
    return rows


def summarize(rows: Sequence[EvaluationRow]) -> dict[str, dict[str, float | int | None]]:
    def rate(matches: int, count: int) -> float | None:
        return matches / count if count else None

    report: dict[str, dict[str, float | int | None]] = {}
    for split, mode in sorted({(row.split, row.mode) for row in rows}):
        group = [row for row in rows if row.split == split and row.mode == mode]
        supported = [row for row in group if row.supported]
        unsupported = [row for row in group if not row.supported]
        report[f"{split}/{mode}"] = {
            "query_count": len(group),
            "supported_count": len(supported),
            "unsupported_count": len(unsupported),
            "top1_accuracy_supported": rate(sum(row.top1_correct for row in supported), len(supported)),
            "recall_at_5_supported": rate(sum(row.hit_at_k for row in supported), len(supported)),
            "answer_rate_supported": rate(sum(row.answerable for row in supported), len(supported)),
            "correct_answer_rate_supported": rate(
                sum(row.answerable and row.top1_correct for row in supported), len(supported),
            ),
            "false_answer_rate_unsupported": rate(
                sum(row.answerable for row in unsupported), len(unsupported),
            ),
            "abstention_rate_all": rate(sum(not row.answerable for row in group), len(group)),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate labels or evaluate live retrieval")
    parser.add_argument("--live", action="store_true", help="Call OpenAI; may incur API charges")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / ".data/evaluation_report.json")
    args = parser.parse_args()
    try:
        entries = load_knowledge_base()
        queries = load_evaluation_queries(entries)
        print(f"Validated {len(queries)} labeled synthetic queries")
        if not args.live:
            print("No API calls made. Use --live to evaluate real retrieval.")
            return 0
        settings = load_settings()
        settings.require_openai()
        rows = evaluate_retrieval(
            entries, queries, create_chroma_dense_search(entries, settings.chroma_persist_directory),
        )
        metrics = summarize(rows)
        payload = {
            "dataset": "data/evaluation_queries.json",
            "embedding_model": "text-embedding-3-small",
            "top_k": 5,
            "confidence_threshold": 0.55,
            "minimum_dense_score": 0.35,
            "hybrid_dense_weight": 0.60,
            "metrics": metrics,
            "rows": [row.model_dump() for row in rows],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(metrics, indent=2))
        print("Saved synthetic evaluation results. Thresholds were not tuned on the test split.")
        return 0
    except (OSError, ValueError, APIError, ChromaError) as error:
        print(f"Evaluation failed ({type(error).__name__}); check local settings and API access.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())