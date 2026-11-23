# Wire contract: what Mistral actually sends and accepts

Measured 2026-11-23 against `api.mistral.ai` on a Pro (`INDIVIDUAL`) account. Every claim
below is a live probe, not documentation reading.

## 1. Two wallets, one endpoint

`GET /v1/billing/budget` (auth works with both `Authorization: Bearer <key>` and
`x-api-key: <key>`) returns:

```json
{
  "vibe_budget": {"usage_percentage": 0.0,  "initial_budget": 300.0, "currency": "USD",
                  "reset_at": "2026-12-01T00:00:00Z", "payg_enabled": false},
  "api_budget":  {"usage_percentage": 1.31, "initial_budget": 30.0,  "currency": "USD",
                  "reset_at": "2026-12-01T00:00:00Z", "payg_enabled": false}
}
```

`GET /v1/billing/subscription` adds `{plan_name, payg_enabled, vibe_payg_enabled, enterprise}`.
`initial_budget: 0` means the plan has no allowance of that kind, not "0% used".

## 2. The field that picks the wallet

Two identical requests (model `mistral-large-4`, 4816 input + 16 output tokens, same key,
same minute apart, budget polled for 100 s after each):

| body | api_budget delta | vibe_budget delta |
|---|---|---|
| `"metadata": {"call_source":"vibe_code","call_type":"main_call",…}` | 0.0% | +0.001135% ($0.0034) |
| no `metadata` | +0.04036% ($0.0121) | 0.0% |

`User-Agent` was tested separately and does **not** switch the wallet: the same request with
`User-Agent: mistral-client-python/Mistral-Vibe/3.1.4` and no metadata still billed the API
budget. `metadata.call_source` alone is sufficient.

The official CLI sends exactly this object (captured by pointing its `[[providers]] api_base`
at a local HTTP logger):

```json
"metadata": {
  "agent_entrypoint": "programmatic",
  "agent_version": "3.1.4",
  "client_name": "vibe_programmatic",
  "call_type": "main_call",
  "call_source": "vibe_code",
  "host_kind": "local",
  "message_id": "user-turn-…"
}
```

Billing is asynchronous: a delta appears 5-40 s after the request and can be attributed to
the wrong neighbour if requests are fired back to back. Poll the budget for at least a
minute, with a pause of the same length between requests, before drawing conclusions.

## 3. Answer shape: blocks, not strings

Non-streaming, `reasoning_effort: high`, `mistral-large-4`:

```json
{"choices": [{"message": {"role": "assistant", "tool_calls": null,
  "content": [
    {"type": "thinking", "thinking": [{"type": "text", "text": "…"}], "closed": true},
    {"type": "text", "text": "4"}
  ]}}]}
```

There is no `reasoning_content` field on the way out. A client that expects
`content` to be a string gets an array and either crashes or shows nothing.

Streaming: `delta.content` arrives both as `""` (string) and as the same block list, in
separate frames. Text and thinking must be separated per frame.

## 4. History: `reasoning_content` is rejected

Replaying an assistant turn with the OpenAI-standard reasoning field:

```
POST /v1/chat/completions
{"messages":[{"role":"user","content":"2+2?"},
             {"role":"assistant","content":"4","reasoning_content":"два плюс два"},
             {"role":"user","content":"а 3+3?"}]}

422 {"detail":[{"type":"extra_forbidden",
                "loc":["body","messages",1,"assistant","reasoning_content"],
                "msg":"Extra inputs are not permitted"}]}
```

Replaying the block form (`content` as a `thinking` + `text` list) answers `200`, so blocks
are accepted in history while `reasoning_content` is not. The relay strips the former and
collapses the latter: text only, always accepted.

## 5. What the relay guarantees

| input | behaviour |
|---|---|
| assistant history with `reasoning_content` / `reasoning` / `thinking` | stripped before dispatch |
| assistant history with block `content` | collapsed to its text blocks |
| caller's own `metadata` keys | kept; billing keys are filled in, never overwritten by caller values |
| non-stream answer | `content` string + `reasoning_content` string |
| stream answer | same split per `delta`, usage frame passed through untouched |
| any other path (`/v1/models`, `/v1/billing/*`, …) | byte-for-byte pass-through |
| upstream 4xx/5xx | status and body returned to the client unchanged |

## 6. Prompt cache works without markers

Mistral caches prompt prefixes on its own; no `cache_control`, no `prompt_cache_key` needed
(the key is accepted, not required). Through the relay, a 5745-token prompt sent three times
in a row:

| run | `prompt_tokens` | `prompt_tokens_details.cached_tokens` |
|---|---|---|
| 1 | 5745 | 0 |
| 2 | 5745 | 5632 |
| 3 | 5745 | 5632 |

The billing `metadata` object does not sit in the prompt, so it cannot break the prefix
match. Cached input is priced at $0.07/M against $0.70/M for `mistral-large-4`.

## 7. Reproduce

```bash
# wallet check before and after
curl -s https://api.mistral.ai/v1/billing/budget -H "Authorization: Bearer $KEY"

# signed request
curl -s https://api.mistral.ai/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"mistral-large-4","max_tokens":16,
       "messages":[{"role":"user","content":"hi"}],
       "metadata":{"call_source":"vibe_code","call_type":"main_call"}}'

# rejected history
curl -s https://api.mistral.ai/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"mistral-large-4","messages":[
        {"role":"user","content":"2+2?"},
        {"role":"assistant","content":"4","reasoning_content":"x"},
        {"role":"user","content":"3+3?"}]}'
```
