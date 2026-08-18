# External triggers smoke runbook

Release checklist for durable external triggers (Funnel `:443` + Serve `:8443`).

## Automated

```bash
cd backend && uv run pytest tests/test_inbound_events.py tests/test_webhook_security.py -q
cd apps/desktop/src-tauri && cargo test reachability -- --nocapture
```

## Host smoke

1. Clean Host install (or wipe ingress prefs): External triggers off.
2. Connect Tailscale. **Settings → Availability → Enable external triggers**.
3. Confirm:
   - Status becomes **Configured — waiting for first event** (not Verified).
   - `tailscale funnel status` shows `/api/v1/webhooks` and `/api/v1/push` on `:443` → current backend port.
   - Private Serve remains on `:8443` for devices.
4. Public probe (from outside the machine if possible):
   - `curl -i https://<host>.ts.net/api/v1/webhooks/composio` → 401/405/400 (route exists), not connection failure.
   - `curl -i https://<host>.ts.net/api/v1/health` → not publicly exposed (404 / connection depending on Funnel path mount).
5. Fire a Composio trigger and a Google Calendar change:
   - Availability → **Verified — event received**.
   - Mongo `inbound_events` row `status=processed`.
   - Matching automation produces one `trigger_instances` row (duplicate delivery / push+poll still once).
6. Crash recovery:
   - After a receipt lands as `pending`/`processing`, kill the backend, restart Host.
   - Confirm the event reaches `processed` exactly once (lease reclaim).
7. Disable External triggers:
   - Funnel paths removed (`off`, not `reset`).
   - Provider callbacks cleared / no longer pointing at the Host.
   - Status returns to **Off — polling still active**.

## Contributor without Funnel

Set `EXTERNAL_INGRESS_BASE_URL=https://…` to any tunnel that forwards `/api/v1/webhooks` and `/api/v1/push`, then `POST /api/v1/ingress/external` with that base URL (device auth).
