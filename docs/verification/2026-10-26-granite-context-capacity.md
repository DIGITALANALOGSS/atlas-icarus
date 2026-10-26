# Granite context-capacity qualification — 2026-10-26

## Result

Passed a native /completion capacity smoke test with 4608 input tokens,
one generated token, and no reported truncation.

The server was launched with requested context 8192.
This test establishes successful input processing above the former
4096-token configuration; it does not qualify the entire 8192-token window.

## Measured evidence

```json
{
  "status": "passed",
  "test": "native_completion_over_4096_tokens",
  "submitted_prompt_tokens": 4608,
  "reported_input_tokens": 4608,
  "truncated": false,
  "generated_tokens": 1,
  "elapsed_seconds": 120.17,
  "minimum_sampled_available_memory_mib": 8114,
  "document_understanding_verified": false,
  "vane_research_verified": false,
  "timings": {
    "prompt_n": 4608,
    "cache_n": 0,
    "prompt_ms": 120152.522,
    "predicted_n": 1,
    "predicted_ms": 0.001
  }
}
```

## Interpretation and limitations

- Prompt caching was explicitly disabled for this test.
- Input processing dominated the measured runtime.
- Available memory figures are periodic samples, not continuous monitoring.
- Synthetic repeated tokens were used; semantic comprehension was not tested.
- Native completion was tested, not the chat template or Vane workflow.
- No claim is made about PDF, image, OCR, or vision analysis.
- No claim is made that Vane research timeouts or cancellation are fixed.
- One successful run is not sustained-load qualification.

## Next qualification work

1. Verify meaningful information use across a long input.
2. Measure prompt-cache reuse without assuming it is effective.
3. Trace Vane research stages and propagate failures promptly.
4. Requalify source-backed research after the context change.
5. Preserve the larger-context, document, vision, and voice requirements.

Credential rotation and Qwen dependency migration remain outstanding.
