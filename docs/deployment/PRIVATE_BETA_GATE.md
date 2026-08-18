# Private Beta Release Gate

Checklist before inviting macOS private-beta users to an always-on home Host + Tailscale setup.

**Status: NOT PASSED.** Creating this checklist does not complete the gate. Do not invite beta
users until every applicable item below has recorded evidence and an owner has approved release.

Evidence below was collected on 2026-07-15 against the local `main` candidate. Clean-machine and
signed-release checks must be repeated against the exact tagged artifact.

## Clean-machine install

- [ ] Fresh Apple Silicon macOS 14+ account
- [ ] No repo checkout, Docker, Node, Python, or `task` required
- [ ] Install from notarized `.dmg` without Gatekeeper blocks
- [ ] App starts bundled MongoDB + backend with an empty data directory
- [ ] Startup either becomes ready within 30 seconds or identifies the exited/timed-out child, retains logs, and Retry recovers
- [ ] Google AI Studio connects with the recommended stable model and completes the first text turn
- [ ] Cerebras connects with the preview model and completes the first text turn
- [ ] The post-first-text-turn voice nudge can be skipped; text chat remains fully usable and setup remains available in Settings → Voice & Audio
- [ ] Signed WebView microphone permission succeeds; denial shows System Settings recovery and Retry works
- [ ] A validated Cartesia key selects Cartesia STT; “Jarvis, what time is it?” produces a visible transcript and text response with spoken replies disabled
- [ ] Enabling optional TTS by selecting/cloning a Cartesia voice produces a spoken response; clearing the voice returns to text-only replies
- [ ] Settings → Voice & Audio owner wake enrollment completes (three “Jarvis” samples plus two natural requests); wake accepts owner and rejects another speaker without restart
- [ ] Fresh install has no packaged personal speaker profile; wake still works pre-enrollment (accept-all Stage 2b)
- [ ] Existing installs treat obsolete `.npy` speaker profiles as unenrolled and can re-enroll into the TitaNet `.npz` format

## Host reliability

- [ ] Quit window hides to menu bar when “Keep running in menu bar on close” is enabled
- [ ] Tray **Restart Host** recovers from a killed backend
- [ ] Launch at login starts the Host after reboot
- [x] Mongo data survives Host restart
  - Evidence: packaged Host restarted in 24s; Mongo collection counts were unchanged and bundled
    MongoDB, backend, local health, and tailnet health all returned healthy.
- [ ] Mongo data survives an `N-1` → `N` update
- [ ] 24–72h soak: alarms, automations, reconnect delivery, provider blips

## Remote access

- [ ] Tailscale installed; Settings → Availability reaches **Private access ready** (Serve URL shown as address other devices use)
  - Partial evidence: Tailscale Serve on `:8443` survived Host restart and returned HTTP 200;
    Settings UI confirmation remains.
- [ ] Rooms & devices can mint a room speaker and issue a phone/browser pairing code / QR link
  - Partial evidence: a phone/browser pairing code and pairing URL were issued successfully;
    room-speaker minting remains.
- [ ] Phone on the tailnet pairs and completes a text turn over `wss://`
- [ ] Room speaker comes online in Rooms & devices after config paste + restart
  - Partial evidence: `jarvis-satellite-1` currently reports online with mic/speaker capabilities;
    clean provisioning and restart remain.
- [ ] Removing access force-disconnects a device and blocks reconnect
- [x] Unauthenticated REST calls to credentials/history/setup mutations fail with 401 off-localhost
  - Evidence: live requests through the tailnet Serve URL returned 401 for credentials, history,
    and an LLM setup mutation; ingress/auth regression suite passed 30 tests.

## Updates and support

- [x] `vX.Y.Z` tag matches `backend/pyproject.toml`
  - Evidence: `v0.1.2` and all packaged application versions resolve to `0.1.2`.
- [x] Versioned GitHub Release + rolling updater channel artifacts published
- [ ] `N-1` → `N` update succeeds with `JARVIS_ENABLE_AUTO_UPDATE=1`
- [ ] Broken update leaves local data intact
- [ ] Diagnostics export works (metadata-first)

## Automated candidate checks

Run `task release:candidate` before creating a release tag. The task runs the backend,
frontend, desktop, satellite, dependency-audit, and consolidated offline eval checks.

The results below are the last recorded baseline, not evidence for the current uncommitted candidate.
Replace them with output from the exact tagged commit before release.

- [x] Backend lock sync + full suite: **1183 passed**
- [x] Frontend clean production build on Node 20
- [x] Desktop Rust suite: **7 passed**
- [x] Desktop doctor: Rust check + bundled MongoDB service smoke passed
- [x] Production dependency audit: frontend and desktop report **0 vulnerabilities**
- [x] Packaged Host reports database, LLM, and voice services healthy
- [x] Current packaged bundle contains no personal speaker profile

### 2026-07-17 uncommitted-candidate verification

This is engineering evidence only; it does not replace the signed-DMG clean-machine gate.

- Backend: **1219 passed**, 6 existing Pydantic deprecation warnings
- Frontend: **7 passed**; TypeScript + Vite production build passed with existing bundle/chunk warnings
- Desktop Rust: **12 passed**
- Desktop release-script tests: **4 passed**
- Desktop startup shell and production frontend builds passed
- Bundled-service smoke: MongoDB passed through bundled Python with an empty, space-bearing data path
- Frontend and desktop production dependency audits: **0 vulnerabilities**
- Changed backend files: Ruff passed
- Release shell syntax and `v0.1.2` version/tag verification passed
- `shellcheck` and `actionlint` were unavailable locally

## Eval gate (behavior-changing releases)

- [x] `task be:eval-wakeword`: 92% primary recall; 3/222 feedback-negative false accepts
- [x] Enrolled Stage 2b replay: 86% primary recall; 2/222 feedback-negative false accepts
- [x] Enrolled free-speech gate: 1.810h, 1 false accept, **0.552 FA/hr** (limit 1.0)
- [x] `task be:eval-stt`: 20/20 fixtures, 0 flagged
- [x] `task be:latency`: 3/3 live text turns; p50 first response **733ms**, p90 **1852ms**
- [x] `task be:eval-routing`: completed successfully; 0.86 hit rate / 0.89 recall
- [x] `task be:eval-agent`: all 14 P0 mock cases passed

External auto-update remains disabled until the signed update path passes the manual checks below.

The P0 agent behavior gate now runs in PR CI. The full offline voice/STT/routing/agent ladder
runs in `task release:candidate` and the tag release workflow; live latency and physical
satellite checks remain release evidence rather than hermetic CI jobs.

## Product evidence

Do not add Host-side cohort reporting during the technical beta. A single household cannot
measure a cohort, and combining reports would introduce a telemetry and consent system that the
beta does not otherwise need.

Collect explicit opt-in feedback for three decisions:

- Pairing: did the tester pair a phone and complete a first remote turn?
- Retention: did they use the phone surface again, and what blocked them?
- Reachability: were failures caused by setup, reconnect, or lack of Tailscale access?

Consider a production native app only after physical-device validation of the web companion passes
and repeated mobile use shows that browser constraints are the limiting factor. Consider a managed
relay only when reachability failures, rather than setup or product value, are the recurring
blocker.

## Current release blockers

- No Developer ID signing identity or Apple notarization credentials are configured locally.
- GitHub Actions has no release secrets or updater channel variables.
- No updater private key, desktop tag, workflow run, or GitHub Release exists yet.
- The repository is private; unauthenticated app updates need a separately accessible artifact
  host or an explicit distribution decision.
- Clean-machine, signed microphone, enrollment, phone pairing, revocation, diagnostics, reboot,
  update-failure, and soak checks still require manual evidence.
- A fresh-user install and complete onboarding pass from the exact signed, notarized DMG has not
  been performed for the current candidate.

## Explicit non-goals for this beta

- Public internet voice endpoint
- Cloud-hosted brain
- Household multi-user accounts
- Native always-listening phone app
- Windows/Linux Host packages
