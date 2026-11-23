# mistral-vibe-relay

[README на русском](README.ru.md)

you pay for a mistral vibe plan, and the plan comes with a second, much larger monthly
allowance (`vibe_budget`: $300/month on Pro) next to the regular API allowance
(`api_budget`: $30/month). plain `api.mistral.ai` spends the small one. this relay sits in
front of the API, signs each request the way Mistral's own Vibe CLI does, and spends the
big one - from any OpenAI-compatible client, without the CLI and without its agent
overhead.

it also fixes the one thing that bites normal clients: Mistral answers with block-shaped
content (`[{"type":"thinking",…},{"type":"text",…}]`), not with a string, and it rejects
`reasoning_content` replayed in the assistant history with `422 extra_forbidden`. the
relay translates both directions.

## what it does

- adds `metadata: {call_source: "vibe_code", …}` to every `/v1/chat/completions` body, so
  the account's Vibe Code monthly budget is charged instead of the API budget - the same
  marker the official `vibe` CLI sends
- strips `reasoning_content` / `reasoning` / `thinking` from the history before dispatch,
  because Mistral answers `422 extra_forbidden` on them
- collapses block content in the history to plain text, which Mistral always accepts
- rewrites the answer back to plain OpenAI shape: `content: "…"` plus
  `reasoning_content: "…"`, in both streaming and non-streaming replies
- keeps the caller's own metadata keys, only fills in the billing ones
- passes `/v1/models`, `/v1/billing/budget`, `/v1/billing/subscription` and any other path
  through untouched, so budget checks keep working through the relay
- one bearer key for clients (`RELAY_CLIENT_KEY`), an optional health endpoint, and a
  structured JSON log on stderr with what was rewritten per request

## what it is not

- not a mistral product, not affiliated with them
- not a hosted service - runs on your machine, your key, your plan
- not a credential bundle - nothing private ships in this repo

## straight talk

the marker is a client claiming to be Vibe CLI. Mistral bills it from the Vibe Code
allowance today; if they start checking the client identity harder, the lane dies and every
request falls back to the API budget (the relay logs what it rewrote, so you will see it).
using a coding-plan allowance to serve arbitrary API traffic is a gray zone in the terms -
same club as every subscription relay. your call.

## requirements

- python 3.11+ (no dependencies, no build step)
- a Mistral API key with a plan that carries a Vibe Code allowance
  (`GET /v1/billing/budget` shows `vibe_budget.initial_budget`; `0` means your plan has none)

## quick start

```bash
export MISTRAL_API_KEY=...            # your key, stays on your machine
python3 relay.py                      # listens on 127.0.0.1:18999
curl -s localhost:18999/healthz
```

point any OpenAI client at it:

```bash
curl -s localhost:18999/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mistral-large-4","messages":[{"role":"user","content":"hi"}]}'
```

check that the right wallet pays:

```bash
curl -s localhost:18999/v1/billing/budget -H "Authorization: Bearer anything"
```

## configuration

everything is environment variables; defaults are safe for a personal box.

| variable | default | meaning |
|---|---|---|
| `MISTRAL_API_KEY` | - | upstream key sent as `Authorization: Bearer` (name comes from `RELAY_UPSTREAM_KEY_ENV`) |
| `RELAY_UPSTREAM_KEY_ENV` | `MISTRAL_API_KEY` | which env var holds the upstream key |
| `RELAY_UPSTREAM` | `https://api.mistral.ai` | upstream base URL |
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `18999` | bind address |
| `RELAY_SIGN` | `1` | inject the Vibe billing metadata |
| `RELAY_NORMALIZE` | `1` | rewrite block content / reasoning fields |
| `RELAY_CLIENT_KEY` | empty | if set, clients must send `Authorization: Bearer <that>` |

## systemd (user unit)

```bash
./install.sh            # writes ~/.config/systemd/user/mistral-vibe-relay.service
systemctl --user enable --now mistral-vibe-relay
systemctl --user status mistral-vibe-relay
```

the unit reads `~/.config/mistral-vibe-relay/env` (mode 600) for the key, so the key does
not live in the unit file or in your shell history.

## how the billing switch works

intercepted from the official CLI: every `/v1/chat/completions` it sends carries

```json
"metadata": {
  "call_source": "vibe_code",
  "call_type": "main_call",
  "client_name": "vibe_programmatic",
  "agent_version": "3.1.4",
  "host_kind": "local"
}
```

measured on one account with identical requests (mistral-large-4, 4816 input + 16 output):

| request | `api_budget` delta | `vibe_budget` delta |
|---|---|---|
| with `metadata.call_source=vibe_code` | 0.0% | +0.001135% ($0.0034) |
| without metadata | +0.04036% ($0.0121) | 0.0% |

`metadata` is what flips the wallet; `User-Agent` is not needed. the full evidence,
including the `422 extra_forbidden` probes and the streaming shapes, is in
[docs/CONTRACT.md](docs/CONTRACT.md).

## prompt caching

Mistral caches prompt prefixes automatically: no `cache_control` markers, no extra fields.
The relay does not interfere - the billing `metadata` lives outside the prompt - and it
forwards `prompt_cache_key` untouched if a client sends one. Measured through the relay on
a 5745-token prompt repeated three times:

| run | prompt tokens | cached tokens | billed input |
|---|---|---|---|
| 1 (cold) | 5745 | 0 | full price |
| 2 | 5745 | 5632 | 2% of the prompt at full price |
| 3 | 5745 | 5632 | same |

Cached input is $0.07/M against $0.70/M for `mistral-large-4`, so a stable system prompt
costs a tenth of what it looks like. Every request is logged with
`usage: {prompt, completion, cached}`, so a cache miss is visible without a billing page.

## tests

```bash
python3 -m unittest discover -s tests -v    # 17 tests, no network
```

## license

MIT
