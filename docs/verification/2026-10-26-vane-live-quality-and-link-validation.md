# Vane live qualification and link-validation checkpoint

Date: 2026-10-26

## Live test outcome

An isolated process inside the existing Atlas Control container loaded
the committed research adapter and made one live Vane research request.
No running-service activation or image replacement was performed.

- Adapter commit under test: b022021.
- Mode: speed.
- Chat model: atlas-granite-test.
- Embedding model: Xenova/all-MiniLM-L6-v2.
- Request deadline: 300 seconds.
- Reported completion time: 272.60 seconds.
- Adapter status: completed_mapping_checked.
- Conversation/database lease and publication: not tested.
- Claims independently verified: no.
- Backend cancellation verified: no.

The task requested the official Docker documentation page for
docker compose config and a two-sentence explanation.

## Research quality failed

The generated answer described a Reddit post as the official documentation.
Its generated Reddit hyperlink differed from the URL returned for source [1].
The original adapter checked citation-number bounds but did not reject
that generated-link mismatch.

Completion and valid source-number bounds did not establish factual accuracy,
source authority, URL fidelity, or successful task fulfillment.

## Follow-up implementation

Added checks for ordinary HTTP(S) Markdown links:

- Reject generated link destinations absent from returned source URLs.
- Reject a link followed immediately by a numeric citation when the
  destination differs from that numbered source.
- Retain support for matching links and number-only citations.
- Added five offline regression tests.

This is a bounded consistency check, not a complete Markdown parser,
semantic evidence verifier, or authoritative-source selection policy.

## Test results reported from the local test container

- Focused research/Vane/link suite: 37 passed in 0.93 seconds.
- Full Atlas Control suite: 400 passed in 3.50 seconds.
- No live Vane retest was performed after the link-validation change.

## Private evidence

~/.local/state/atlas-vane-integration/adapter-live-20261026T182059Z-1241/result.json

The private result and request guard remain local.
This document does not back them up or authorize another request.

## Remaining work

- Enforce and qualify task-appropriate authoritative-source requirements.
- Measure research-stage timing before attributing the 272.60-second latency.
- Establish the effective conversation, proxy, and backend deadlines.
- Qualify backend cancellation separately from client request closure.
- Preserve the existing Compose override chain and network membership.
- Qualify a candidate image before any running-service activation.
- Keep document, vision, larger-context, background research, and voice
  requirements in scope.
- Credential rotation and Qwen dependency migration remain outstanding.
