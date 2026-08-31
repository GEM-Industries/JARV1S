---
name: query-jarvis-data
description: Query JARV1S MongoDB turn traces, telemetry, and triggers. Use when investigating turns, latency, tool failures, delivery/audio issues, reminders, or where runtime data lives. Defaults to the desktop app database when the host is running.
---

# Query JARV1S Data

Live runtime data: turns, trigger fires, automation rules, telemetry. Prefer the CLI over ad-hoc Mongo snippets.

**Critical:** desktop app and dev Docker are **separate databases**. Always check `status` first.

```bash
cd backend
source "$HOME/.zprofile" 2>/dev/null || true
source "$HOME/.zshrc" 2>/dev/null || true

uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py status
uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py rules -q "bedroom"
uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py search "fade"
uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py rule rule-ABC
uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py turn turn-ABC
uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py eou --hours 24
```

`--source app|dev` (default `app`). `--owner` defaults to `DEFAULT_USER_ID` (`local`). Run from `backend/` so `uv` + motor resolve.

## Sources

| Flag | Use when | Connection |
|------|----------|------------|
| `app` | User means the desktop/host app | `~/Library/Application Support/JARV1S/run/mongodb-0.sock` |
| `dev` | `task dev` / Docker | `mongodb://localhost:27018` |

App data/logs:

```text
~/Library/Application Support/JARV1S/   # mongo/, run/*.sock, …
~/Library/Logs/JARV1S/                  # host.log, mongod.log
```

If the app is quit, the socket is down — start JARV1S (do not invent a second mongod against the data dir unless you know what you are doing).

## Commands

| Command | Purpose |
|---------|---------|
| `status` | Which source + key collection counts |
| `recent [--hours N]` | Recent user turns (`turn_runs`) |
| `turn <id>` | Header (`called` vs `routed_tools`) plus capability/args/status/spoken. `routed_tools` is semantic matches only — always-on is not listed. Timings are one line; stages stay in `eou`. |
| `eou [--hours N]` | EOU submit-latency scorecard by `endpointing_profile` |
| `rules [-q REGEX] [--enabled-only]` | List rules |
| `rule <id>` | Full rule + recent instances |
| `search <regex> [--role user]` | Grep conversation content |

## Collections

- `conversations` — transcript/trace (`metadata.turn_id`, `metadata.turn_type`)
- `turn_runs` — timings only (`response_ms`, stages, modality, node)
- `trigger_rules` — schedules/automations (`origin.original_local_time` = user clock; `fire_at` is UTC)
- `trigger_instances` — fires; follow `turn_ids[]` into conversations

Also useful: `background_tasks`, `protocols`, `habits`, `automation_fired`.

Not in Mongo: live WS diagnostics; prompt dumps under logs when enabled; HA entity state in HA storage.

## Investigation pattern

1. `status` — confirm `source: app` (or force `--source app`)
2. `rules -q …` / `search …` — find the rule or turn
3. `turn <id>` — classify from the ledger **before** proposing a plugin or process change: routing/selection, call shape, domain evidence, provider/auth, policy, or runtime (see `.cursor/rules/plugin-tool-conventions.mdc`). Compare `called` to `routed_tools`. Always-on tools can appear in `called` without being routed.
4. `rg` the **capability and mechanism** (`control_lights`, `transition`, `replace_alert`, omit/`due_at`) in `backend/evals` and `backend/tests`. A matching eval **id** or plugin test means that class is already gated. A new live turn that still fails means the gate is the wrong layer or too narrow — do not add a second souvenir case.
5. This chat is not the issue inventory. Domain failures close in pytest. Selection failures close in one generalized eval plus an adjacent negative.

## DB writes

Prefer product/API tools. Direct fixes: same `--source` as the running app, filter by `owner_id` + stable id, `$set`/`$unset`, set `updated_at`, read back `matched`/`modified`. Never broad-update by name alone. Do not mutate traces unless asked. Do not export `oauth_tokens`, `ws_device_credentials`, or raw webhooks.

For one-off Python, reuse the app socket URL from `sources.app_source().mongodb_url` or pass `--source app` and inspect with the CLI first.

For local schema changes, migrate and verify the supported app database. Dev data is disposable and should be recreated instead of carrying compatibility migrations. Do not keep startup backfills, inferred legacy ownership, or old identifier fallbacks.

## Pitfalls

- Wrong DB is the #1 failure after switching to the desktop app.
- `turn_runs` has no text; `conversations` has no stage timings.
- Failed rule updates can leave an old enabled rule + a new one with the same name.
- `spoken=''` is valid (tool-first), not missing data.

## Client observability runbook

Bounded `client.diagnostics` breadcrumbs land in the rotated host log (and desktop diagnostics export `client` section). They are **not** stored in Mongo.

Grep host log:

```bash
rg "ClientDiag" ~/Library/Logs/JARV1S/host.log
```

Allowlisted events: `transport_transition`, `mic_acquire`, `mic_interrupted`, `mic_flatline`, `playback_summary`, `playback_failed`, `notification_failed`, `location_unavailable`.

| Symptom | Look for |
|---------|----------|
| Backend generated TTS but client silent | Turn has TTS/`audio.playback_end` in traces; check `playback_summary` — `render_completed` + `context_state=running` means WebAudio finished but may still be silent (desktop WKWebView CoreAudio teardown after background/idle). Missing summary or `resume_failed` / non-running `context_state` is a client playback failure. Full app relaunch if keep-alive could not prevent a dead session. |
| Transport delivery gap | `transport_transition` with `phase=closed` / `heartbeat_timeout` before any playback event for that turn |
| Client rendered (WebAudio finished) | `playback_summary` `outcome=render_completed` — does **not** prove the user heard audio |
| Silent / wedged mic | `mic_flatline` (`reason=no_frames` or `flatline`) after `mic_acquire`; only one per acquire window |
| Connection flap | `transport_transition` open/close with `recovery` / reconnect count; check `client_surface` / device kind on the session |
| Desktop weather/nearby has no location | `location_unavailable` with `reason=denied` / `unavailable` / `timeout` on the desktop node |

Correlate with trusted server fields on each log line: `connection_id`, `node_id`, device kind, optional `turn_id`. Prefer desktop export when events never reached the backend (local ring snapshot under `client`).
