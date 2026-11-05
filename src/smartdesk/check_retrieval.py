import argparse

from chromadb.errors import ChromaError
from openai import APIError

from smartdesk.config import load_settings
from smartdesk.retrieval import HybridRetriever, create_chroma_dense_search, load_knowledge_base


def main() -> int:
    parser = argparse.ArgumentParser(description="Check settings and optionally run live retrieval")
    parser.add_argument("--live", action="store_true", help="Call OpenAI; may incur API charges")
    args = parser.parse_args()
    try:
        settings = load_settings()
        entries = load_knowledge_base()
        print(f"Knowledge base: {len(entries)} entries")
        print(f"OpenAI configured: {settings.openai_api_key is not None}")
        print(f"GitHub token configured: {settings.github_token is not None}")
        if not args.live:
            print("No API calls made. Use --live to check embeddings and retrieval.")
            return 0
        settings.require_openai()
        dense_search = create_chroma_dense_search(entries, settings.chroma_persist_directory)
        retriever = HybridRetriever(entries, dense_search)
        for query in (
            "How do I reset my company password?",
            "Where can I get office parking permits?",
            "My monitor is flickering. What should I do?",
        ):
            result = retriever.search(query)[0]
            print(
                f"{query}\n  {result.entry.id}: dense={result.dense_score:.3f}, "
                f"combined={result.confidence:.3f}, answerable={result.answerable}"
            )
        print("Live smoke check complete; these examples do not calibrate thresholds.")
        return 0
    except (OSError, ValueError, APIError, ChromaError) as error:
        print(f"Check failed ({type(error).__name__}). Check local configuration and API access.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())