# Atlas: Vane integration and context recovery checkpoint

Session: 2026-10-26
Status: integration selected; implementation and qualification incomplete.

## Product requirements — preserve full scope

- Vane is the selected research engine for Atlas integration.
- Research must share Atlas conversation, evidence, and saved-result handling.
- Support quick, balanced, and deep research without blocking ordinary voice.
- Use the selected Kokoro voice through Atlas, not Vane's playback voice.
- Support substantial typed and spoken input.
- Support images, screenshots, PDFs, scanned documents, tables, diagrams,
  and code/configuration extraction.
- Preserve original documents and source/page references.
- Provide OCR plus real vision-model routing where required.
- Support larger active context and durable document retrieval.
- Whole-document review must track coverage, not silently sample passages.
- No silent truncation or claims of verified citations without evidence.
- 8192 tokens is an initial runtime qualification target, not the final
  large-document capacity requirement.

## Verified findings

- Atlas research currently routes explicit /search requests through:
  conversation_coordinator.py -> conversation_research.research_answer.
- Existing research uses SearXNG snippets and an Atlas model synthesis call.
- Existing implementation selects three shortened snippets.
- For LlamaCppConversationModel, synthesis is capped at 96 output tokens.
- Relevant modules:
  services/atlas-control/app/conversation_research.py
  services/atlas-control/app/conversation_search.py
- Existing tests:
  services/atlas-control/tests/test_conversation_research.py
  services/atlas-control/tests/test_conversation_search.py
- Host and running-container research/coordinator hashes matched before
  the interruption; reverify after any image change.
- Coordinator default turn lease is 90 seconds; this is not proof of
  every effective caller or proxy deadline.

## Vane deployment and connectivity

- Existing container: atlas-vane-trial
- Published address: host loopback port 3000
- Data volume: atlas-vane-trial-data, mounted at /home/vane/data
- Vane network: atlas-research-search_default
- Atlas original network: atlas-icarus_default
- Atlas was additionally attached to Vane's network and provider access
  from Atlas was verified.
- Persist that additional network membership in deployment configuration;
  do not assume a manual attachment survives container recreation.
- Inside Vane, provider discovery via 127.0.0.1:3000 was refused, while
  discovery via its container hostname succeeded.
- Do not hardcode transient container IPs, hostnames based on container IDs,
  or process IDs.

## Measured results

- Speed-mode Docker definition API call: 37.78 seconds.
- Answer included [1], but returned sources were empty.
- This is not qualified source-backed research.
- Installed classifier can skip search for general-knowledge questions.
- No runtime trace proved that classification for the specific test.
- Current Docker Engine version research was interrupted once and another
  attempt reached a 180.1-second client timeout.
- Backend cancellation was NOT verified.
- Direct external SearXNG test from Vane:
  20 results, 0.93 seconds, two unresponsive engines reported.
- Configured SEARXNG_API_URL origin: http://search:8080
- Direct test of that configured destination:
  HTTP 200, 20 results, 0.55 seconds.
- Basic search connectivity passed; full research workflow did not qualify.
- Granite logs showed several calls spending approximately 40–53 seconds
  in prompt processing, with additional output-generation time.
- Those log tasks were not correlated to a single research request.

## Confirmed context rejection

Granite log:
request (6302 tokens) exceeds the available context size (4096 tokens)

This proves at least one oversized request was rejected.
It does not prove that task caused the later research timeout.

## Latest recovery state observed

- Atlas Control and external SearXNG were running after the interruption.
- Qwen was the sole observed llama-server process:
  Qwen3-4B-Instruct-2507-Q4_K_M.gguf
  host 0.0.0.0, port 8080, context 4096, parallel 1.
- Granite was offline.
- Existing Vane container was stopped with exit 255, then docker start
  atlas-vane-trial succeeded and its status showed Up.
- Exit 255 cause was not established.
- These are last-observed states, not guarantees of current health.

## Saved Granite runtime record

Directory:
~/.local/state/atlas-model-qualification/granite-coding-runtime/

Existing files:
- process.json
- model.key
- server.log

process.json fields:
alias, binary, context_size, cpu_threads, key_file, log_file,
model, parallel, pid, port

Verified non-secret values:
- alias: atlas-granite-test
- binary: /home/freedome/atlas-runtimes/llama.cpp/build-cpu/bin/llama-server
- context_size: 4096
- port: 8081

model.key existed, was nonempty, and had mode 0600.
Never commit or print its contents.
Treat saved PID as historical, not evidence of a running process.

No saved atlas-granite-context candidate/rollback directory was found.
No 8192-token configuration was confirmed prepared, launched, or qualified.

## Deployment preservation

atlas-control was launched with multiple Compose override files, including
voice, tool, proposal, job-lookup, research, and research-budget overrides.
Do not recreate it from compose.yaml alone.
Recover the exact file list from existing container Compose labels.
Atlas Control had no source bind mount; repository edits alone do not
update its running image.

## Exact next work

1. Read only necessary non-secret fields from the saved Granite process
   record, including model, cpu_threads, parallel, key_file, and log_file.
   Do not repeat hardware/network discovery unless evidence changes.
2. Confirm Granite is absent and its intended port is available.
3. Prepare a private, repeatable launcher and original/candidate settings.
4. Qualify context 8192 with progress, memory monitoring, health checks,
   and a documented recovery path.
5. Verify an input larger than 4096 tokens plus output headroom.
6. Diagnose and handle Vane research failures explicitly; do not merely
   lengthen the research timeout.
7. Implement and test the selectable Vane adapter behind the existing
   research entry point; keep current SearXNG behavior selectable.
8. Persist deployment/network changes and establish repeatable recovery.
9. Keep document ingestion, vision, larger context, background jobs,
   cancellation, citation validation, and Kokoro integration in scope.

## Outstanding security and migration work

- Rotate the exposed Granite credential and update affected clients.
- Verify old credential rejection and new credential acceptance.
- Move remaining Qwen dependencies before stopping Qwen or freeing 8080.
- Do not claim current credential permissions constitute rotation.

## Evidence and exclusions

- Local API reports:
  ~/.local/state/atlas-vane-integration/
- Granite server log: runtime directory above.
- Reports saved by earlier commands were processed summaries, not raw API
  responses. They use answer rather than the API's message field.
- Do not infer missing raw fields by rereading those summaries.
- This checkpoint contains no API keys or private process-record contents.
- This document does not back up model files, Docker volumes, private
  launch records, or unrelated modified/untracked repository work.
- Full integration is NOT complete.
