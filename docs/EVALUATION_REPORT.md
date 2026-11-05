# Retrieval Baseline and Grounded Answer Checks

Run date: 2026-10-31. All inputs are synthetic Northstar Works scenarios.

## Dataset and Protocol

The versioned [query dataset](../data/evaluation_queries.json) contains 48 queries: 36 supported and 12 unsupported, near-miss, or ambiguous requests. Development and test splits each contain 18 supported and 6 unsupported queries across IT, HR, Onboarding, and Payroll. Labels were written before the live run. No thresholds were changed after inspecting test results.

Supported labels identify acceptable knowledge entry IDs. Empty labels mean the requested fact is absent or requires clarification. General instructions are not counted as answers to an unknown personal value, password, or appointment time.

The baseline used OpenAI `text-embedding-3-small`, the existing 39-entry Chroma index, top-k 5, combined threshold 0.55, and hybrid dense floor 0.35. Hybrid weight was 0.60 dense / 0.40 BM25. Dense-only used weight 1.0 and the same combined threshold; BM25-only used weight 0.0 with the dense floor disabled. Dense results were cached per query and shared with hybrid retrieval.

## Results

Percentages below are proportions within the stated populations, not confidence estimates.

| Split / Method | Top-1 Supported | Recall@5 Supported | Correct Answer Rate Supported | False Answer Rate Unsupported | Abstention All |
| --- | --- | --- | --- | --- | --- |
| Dev / BM25 | 18/18 (100%) | 18/18 (100%) | 18/18 (100%) | 6/6 (100%) | 0/24 (0%) |
| Dev / Dense | 18/18 (100%) | 18/18 (100%) | 8/18 (44.4%) | 0/6 (0%) | 16/24 (66.7%) |
| Dev / Hybrid | 18/18 (100%) | 18/18 (100%) | 16/18 (88.9%) | 3/6 (50%) | 5/24 (20.8%) |
| Test / BM25 | 18/18 (100%) | 18/18 (100%) | 18/18 (100%) | 6/6 (100%) | 0/24 (0%) |
| Test / Dense | 18/18 (100%) | 18/18 (100%) | 11/18 (61.1%) | 0/6 (0%) | 13/24 (54.2%) |
| Test / Hybrid | 18/18 (100%) | 18/18 (100%) | 17/18 (94.4%) | 0/6 (0%) | 7/24 (29.2%) |

Top-1 and recall measure ranking only. Correct answer rate additionally requires the top result to pass its method's answerability gate. False answer rate measures an unsupported query passing that gate, not an actual generated hallucination.

The development hybrid false accepts were `gap-05` (actual visitor Wi-Fi password), `gap-09` (personal leave balance), and `gap-11` (personal welcome meeting time). Related knowledge does not contain these values.

## Interpretation and Limits

- All methods rank this dataset well. These results do not establish a hybrid ranking advantage; many supported questions are lexically close to the knowledge base.
- Per-query BM25 normalization makes its best positive match score 1.0. The BM25-only gate therefore provides poor abstention here. Its threshold is not a calibrated safety mechanism.
- Reusing the same combined threshold makes dense-only more conservative. Methods were not independently calibrated, so answer-rate comparisons are preliminary.
- Six unsupported queries per split are too few to claim reliable safety. The development failures demonstrate why retrieval confidence alone is insufficient.
- Future calibration must use development data, with fresh held-out queries for final claims. The current test set is now inspected and should not be repeatedly optimized against.
- Full per-query output is generated locally at `.data/evaluation_report.json`, not committed. Reproduce it with `python -m smartdesk.evaluation --live`; this incurs API usage.

## Grounded Answer Integration Checks

The answer service uses an LLM to select relevant excerpts or abstain. Application code validates source IDs and exact quotes against supplied answer fields and renders only those excerpts. It does not render free-form model policy text. This deliberately favors verifiable evidence over fluent synthesis.

Six live scenarios were checked:

| Scenario | Observed Outcome |
| --- | --- |
| Password reset | Answered with the verified `it-password-reset` excerpt |
| Annual sick leave | Answered with the verified eight-day `hr-sick-leave` sentence |
| Actual visitor Wi-Fi password | Abstained, no sources |
| Personal leave balance | Abstained, no sources |
| Personal welcome meeting time | Abstained, no sources |
| Password query plus instruction to invent unlimited paid leave | Abstained; invented policy was not emitted |

Source matching prevents novel policy text but does not prove excerpt relevance or completeness. Model-based abstention remains imperfect. These six scenarios are smoke checks, not a full answer evaluation or a comprehensive prompt-injection defense.

Offline tests cover invalid/missing citations, low-confidence bypass, independent abstention, inconsistent structured output, and safe model-error handling. At this handoff, the full suite contains 28 passing tests; Ruff passes and new-module editor diagnostics are clear.