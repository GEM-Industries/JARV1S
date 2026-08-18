# Multi-Device Reachability

Phase 10.7 reachability: how browser, phone, and room-speaker (satellite) clients reach the JARV1S backend over `/api/v1/ws`.

Satellite protocol, audio path, and Pi operations: [SATELLITE.md](../SATELLITE.md).

External triggers smoke checklist: [EXTERNAL_TRIGGERS_SMOKE.md](./EXTERNAL_TRIGGERS_SMOKE.md).

Reachability gets clients to the backend. **Per-device WebSocket auth** is required for non-localhost clients unless `DEVICE_AUTH_DEV_BYPASS=true` in development (localhost only).

## Product surfaces (Jarvis Host)

| Surface | Where | Job |
|---------|-------|-----|
| **Availability** | Settings → Availability | Keep this Mac on; enable **private access** (Tailscale Serve `:8443`) for phones/satellites; optionally enable **External triggers** (Tailscale Funnel `:443`, webhook/push paths only) |
| **Rooms & devices** | Home → Manage (room speakers), or open the Presence overlay | Online/offline devices, assign room, add room speaker, pair phone/browser, remove access |
| **Smart Home** | StatusBar → Home | Lights and HA rooms only; room speakers deep-link out to Rooms & devices |

Each remote client needs:
1. A durable **device token**
   - **Room speaker:** **Rooms & devices → Add room speaker** (mints once via `POST /api/v1/device-auth/satellites` and shows a copyable `backend_ws_url` + token). CLI recovery: `task devices:satellite-token`
   - **Browser/phone:** pair from **Rooms & devices** or **Settings → Availability** (QR / code). CLI recovery: `task devices:pair-code`
2. A short-lived **WS ticket** minted via `POST /api/v1/device-auth/ws-ticket` before each WebSocket connect (clients do this automatically)

Phone pairing links open the focused companion surface before the authenticated Host setup UI.
The user confirms the one-time code, then the shared React client assigns a sensible device name,
stores the normal device cookie, and connects over the existing WebSocket protocol. The phone
surface does not configure or supervise the Host.

**Local Host trust boundary:** the packaged Host does not pair with itself or create a `ws_device_credentials` row. Direct loopback requests are trusted and classified server-side as `kind=desktop`. Contributor mode also allows direct loopback when `DEVICE_AUTH_DEV_BYPASS=true` and classifies it as `kind=browser`. WebSocket origins are validated on both paths. Forwarded, LAN, tailnet, and public clients always need a token + ticket.

**Revocation:** Rooms & devices → Remove access (`POST /presence/devices/{device_id}/revoke`) soft-deletes the credential and force-disconnects live sessions. Rooms & devices lists **current access only** (online + needs reconnect); revoked tombstones are not shown. Re-pairing a phone retires older offline phone credentials for that owner (a currently connected phone is left alone). CLI `task devices:revoke` marks revoked for the next connect.

**Turn-origin delivery:** when multiple nodes are online for one owner, spoken replies from a user-initiated turn stay on the node that asked (`connection_id`). Kitchen speaker questions are not answered on the browser just because it connected last. If that node disconnects mid-turn, the reply is not rerouted elsewhere. Background protocol runs without a live origin use owner-default routing. Proactive announcements resolve via the presence endpoint router.

**REST auth:** browsers receive a 30-day `HttpOnly`, `SameSite=Strict` device cookie after pairing; it is also `Secure` over Tailscale/HTTPS (local Host HTTP is loopback-only). Satellites and tooling use `Authorization: Bearer <device_token>` (or `X-Device-Token`). Only pairing consumption, health/version, constrained OAuth callbacks, and signature-verified webhooks are public. WS ticket minting and setup require a paired device, except for the direct local Host trust boundary.

## Modes

| Mode | Backend bind | Client URL | Notes |
|------|--------------|------------|-------|
| **Local dev** | `127.0.0.1:8000` | `ws://localhost:8000/api/v1/ws` | Default `task be:dev`. Browser uses Vite proxy; localhost auth bypass with default `DEVICE_AUTH_DEV_BYPASS`. |
| **LAN/dev** | `0.0.0.0:8000` | `ws://<brain-hostname>.local:8000/api/v1/ws` | `task be:dev:lan`. Satellites on same Wi‑Fi; prefer mDNS over raw DHCP IPs. |
| **Private tailnet** | `127.0.0.1:8000` + Tailscale Serve `:8443` | `wss://<machine>.<tailnet>.ts.net:8443/api/v1/ws` | Preferred for phone + room speaker off-LAN. Host app enables Serve from Availability. Existing dogfood satellite configs need a one-time URL refresh to include `:8443`. |
| **Public reverse proxy** | `127.0.0.1:8000` + TLS edge | `wss://<domain>/api/v1/ws` | Require TLS before any remote client. |
| **External triggers (optional)** | Funnel `:443` path mounts | `https://<machine>.<tailnet>.ts.net/api/v1/webhooks/*` and `/api/v1/push/*` only | Not for phones. Enable via Availability → External triggers. Local Funnel status alone does not prove provider events are arriving. |

## Private access on the Host (recommended)

Prefer **Tailscale Serve** so browsers get trusted HTTPS without exposing a public port. Do not bind the packaged Host to `0.0.0.0` or keep a LAN `:8001` proxy for dogfood.

1. Install Tailscale on the Mac (brain), phone, and Pi.
2. Enable HTTPS certificates in the tailnet admin console.
3. In the Host app: **Settings → Availability** → follow the primary CTA (`Install Tailscale` → `Finish sign-in` → **Enable private access**). The app runs Serve on **HTTPS 8443** against the loopback backend port (`enable_host_serve`); if Tailscale needs admin consent, the app opens the consent URL and retries once.
4. When ready, Availability shows **Private access ready** and the share URL labeled **Address other devices use** (`https://<host>.ts.net:8443`).
5. Restrict the Host with tailnet grants/ACLs to the invited owner devices. The desktop Host status verifies that Serve points at the active loopback backend port; a MagicDNS name alone is not treated as configured Serve.
6. Optionally enable **External triggers** on the same Availability page. That configures Funnel on **HTTPS 443** for `/api/v1/webhooks` and `/api/v1/push` only, then reconciles Composio/Calendar callback URLs. Status pills distinguish Off / Configured / Verified / Needs attention — Funnel configured is not the same as events verified.
7. Clients use `wss://` on the Serve port:

```toml
backend_url = "wss://jarvis-brain.example.ts.net:8443/api/v1/ws"
```

When you mint a room speaker from **Rooms & devices**, the Host returns this `backend_ws_url` for you (preferring the Serve origin when private access is ready). Paste it into the Pi config with the one-time `device_token`.

Tailnet IPs live in `100.64.0.0/10` (CGNAT). The satellite client rejects plaintext `ws://` to tailnet targets unless `JARVIS_SATELLITE_ALLOW_INSECURE_WS=1` is set deliberately.

WireGuard-only setups follow the same rule: use a TLS terminator or Tailscale Serve; do not expose plaintext `ws://` across untrusted networks.

Tailscale identity headers are not application authorization. JARV1S continues to require its device credential, and only accepts forwarded client addresses from a loopback proxy peer.

**Contributor / recovery Serve:** if the app cannot enable Serve after consent, you can still run `tailscale serve --bg --https=8443 http://127.0.0.1:8000` on the brain host. Prefer the in-app path for dogfood. For contributor external triggers without Funnel, set `EXTERNAL_INGRESS_BASE_URL` to any public HTTPS origin that forwards `/api/v1/webhooks` and `/api/v1/push`.

## LAN / dev (contributors)

1. Start databases: `task db`
2. Bind on the LAN: `task be:dev:lan`
3. Point the satellite at the brain host:

```toml
# ~/.jarvis-satellite/config.toml
backend_url = "ws://MacBook-Pro.local:8000/api/v1/ws"
```

Use a raw `192.168.x.x` address only when mDNS is unavailable and the router has a DHCP reservation for the brain host.

4. Prefer minting from **Rooms & devices → Add room speaker** on a running Host. CLI recovery on the brain host:

```bash
task devices:satellite-token -- --node-id jarvis-satellite-1 --node-label "Bedroom Satellite"
```

5. Add `device_token` (and the returned `backend_ws_url` when using the API/UI) to satellite config, then deploy:

```bash
task sat:deploy
```

`task sat:deploy` derives `ws://<brain-hostname>.local:8000/api/v1/ws` for new satellite configs. Set `SATELLITE_BACKEND_URL` only when you need a tailnet, public, or otherwise custom URL.

Phone/browser on the same machine: use Vite (`task fe:dev`) at `http://<lan-host>:5173` — the dev proxy forwards `/api` (including WebSocket) to the backend.

Smoke test:

```bash
task be:latency -- --url ws://MacBook-Pro.local:8000/api/v1/ws --device-token "$JARVIS_DEVICE_TOKEN" --text "ping"
```

Browser/phone: issue a code or QR link from **Rooms & devices** or **Settings → Availability**, then open the link or enter the code in the pairing banner. `task devices:pair-code` remains a recovery path.

## Public reverse proxy

Terminate TLS at Caddy, Nginx, or Traefik. Keep Uvicorn on `127.0.0.1:8000`.

Example Caddyfile fragment:

```caddy
jarvis.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy handles WebSocket upgrades automatically. For Nginx, forward `Upgrade` and `Connection` headers and use long `proxy_read_timeout` for voice sessions.

Backend startup behind a proxy:

```bash
BACKEND_PROXY_HEADERS=1 BACKEND_FORWARDED_ALLOW_IPS=127.0.0.1 task be:dev
```

Set production origins in `.env`:

```bash
FRONTEND_ORIGIN=https://jarvis.example.com
BACKEND_CORS_ORIGINS=["https://jarvis.example.com"]
```

Satellite:

```toml
backend_url = "wss://jarvis.example.com/api/v1/ws"
```

## Environment variables

`BACKEND_*` variables below are read by `Taskfile.yml`, not by backend
`pydantic-settings`. Export them in the shell that runs `task`; uncommenting them
in `.env` will not affect `task be:dev`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKEND_HOST` | `127.0.0.1` | Uvicorn bind host (`task be:dev`) |
| `BACKEND_PORT` | `8000` | Uvicorn bind port |
| `BACKEND_PROXY_HEADERS` | `0` | Set `1` when behind a trusted reverse proxy |
| `BACKEND_FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs allowed to send `X-Forwarded-*` |
| `JARVIS_SATELLITE_ALLOW_INSECURE_WS` | unset | Escape hatch for deliberate plaintext `ws://` experiments |
| `DEVICE_AUTH_REQUIRED` | `true` | Reject WebSocket connects without a valid ticket |
| `DEVICE_AUTH_DEV_BYPASS` | `true` in dev | Allow direct localhost API/WS access during contributor development; ignored outside development |
| `JARVIS_DEVICE_TOKEN` | unset | Durable token for `task be:latency` and tooling |

## Satellite URL guardrails

The satellite validates `backend_url` when starting the live client (`SatelliteClient`), not when parsing config. That lets `--list-devices` and `--dry-run-audio` run on the Pi even if the configured backend URL is not yet reachable.

Plaintext `ws://` is allowed for loopback, RFC1918 LAN IPs, and `.local` hosts. Tailnet (`100.64.0.0/10`, `*.ts.net`) and public hosts require `wss://` unless `JARVIS_SATELLITE_ALLOW_INSECURE_WS=1` is set deliberately.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Satellite gets `404 page not found` minting a WS ticket | Config points at Funnel `:443` (`wss://….ts.net/…` without `:8443`) after External triggers cleared Serve on 443 | Use Serve URL `wss://….ts.net:8443/api/v1/ws` — Rooms & devices → Copy address when private access is ready |
| Satellite cannot connect | Backend still on localhost / private access not ready | Finish **Enable private access** in Availability, or `task be:dev:lan` for LAN contrib |
| Satellite worked yesterday, then cannot mint ticket / no route to host | Brain host DHCP IP changed while satellite config used a raw IP | Use Serve `wss://…ts.net` or `ws://<brain-hostname>.local:8000/api/v1/ws`, or add a router DHCP reservation |
| Browser WS fails on HTTPS page | Mixed content (`ws://` from `https://`) | Serve UI + API over HTTPS; use `wss://` |
| WS connects then drops | Proxy missing upgrade headers | Add WebSocket proxy config |
| Phone cannot reach brain off-LAN | Not on tailnet / Serve not enabled | Finish private access in Availability |
| Satellite rejects `ws://100.x.x.x` | Tailnet plaintext guardrail | Use `wss://` via Tailscale Serve |
| WS closes `1008 auth required` | No ticket on remote client | Provision device token; client mints ticket before connect |
| WS closes `1008 invalid ticket` | Expired/reused/revoked ticket | Re-mint ticket; re-pair if token revoked |
| Browser pairing banner on LAN | Expected — bypass is localhost-only | Pair from Rooms & devices / Availability (or `task devices:pair-code`) |
| Minted `backend_ws_url` is `ws://127.0.0.1:8000` | UI called the API over loopback before Serve was ready | Enable private access first, then mint again (Host prefers Serve origin when available) |

## Legacy `sat:proxy`

`task sat:proxy` relayed `0.0.0.0:8001 → 127.0.0.1:8000`. Prefer Tailscale Serve (Host Availability) or `task be:dev:lan` and point satellites directly at port `8000`. Do not keep a permanent `:8001` proxy for dogfood.
