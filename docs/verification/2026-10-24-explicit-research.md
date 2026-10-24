# Explicit research checkpoint — October 24, 2026

## Implemented
- Opt-in, non-streaming /search conversation command.
- Only the explicit query is sent to the configured SearXNG endpoint.
- Answer generation receives the query and search evidence, not saved
  conversation history or memory.
- Search response capture is bounded; redirects are rejected.
- Research evidence is limited to three sources and 400 snippet characters
  per source.
- Llama.cpp research answer allowance is capped at 96 generated tokens.
- Search and answer generation share a turn deadline.
- Answer generation exposes no tools; existing publication checks remain.
- Source URLs are appended by application code.

## Recorded verification
- Latest service test run: 363 passed in 5.00 seconds.
- Backend-to-SearXNG home-page request: HTTP 200.
- Earlier SearXNG JSON search: HTTP 200 with 20 results.
- Initial live research attempt failed; model logs showed generation followed
  by cancellation around the configured request timeout.
- After reducing the research workload, the owner demonstrated a completed
  browser request with numbered citations, three sources, and the status
  "Atlas replied. Conversation saved."
- Waiting feedback was observed at 32 seconds; exact completion latency
  was not measured.

## Deployment
- Image tag: atlas-control-research:budget-fix-20261024
- Existing Compose overrides were preserved during activation.
- Local deployment and rollback plans remain outside this repository under
  the owner's atlas-backups directory.
- This commit records source and evidence; it is not a self-contained
  deployment package.

## Limitations
- Search snippets only; result pages are not fetched.
- Citations and claim support are not independently validated.
- Prompt-injection resistance is not established by this checkpoint.
- Research is not integrated into the separate voice-streaming path.
- A shared async deadline does not prove termination of synchronous model
  work running in a background thread.
- Vane and LibreChat were not installed or integrated by this change.
