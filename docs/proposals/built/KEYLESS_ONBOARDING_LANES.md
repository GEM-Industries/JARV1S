# Keyless Onboarding And Setup Lanes

**Status:** Built (Phases 1–4)  
**Date:** 2026-06-24  
**Updated:** 2026-07  
**Superseded by:** [ARCHITECTURE.md](../../ARCHITECTURE.md) for normative runtime behavior; [ROADMAP.md](../../ROADMAP.md) for what ships next.  
**Related:** `docs/proposals/GUIDED_CREDENTIAL_ACQUISITION.md`, `backend/core/setup/`, `backend/core/credentials/`, `backend/core/auth/`, `backend/core/integrations/`

---

## Problem

The previous guided-credential plan improves the old `.env` workflow, but it still optimizes a chore: deep link, create key, paste, validate. That is better than terminal setup, but it is not the right target for product onboarding.

The better invariant is:

> JARV1S should require no user secrets for the default local assistant path. When a capability truly needs the user's account, the user should only consent in the provider's browser flow.

There is a hard floor. Code should not create accounts, bypass consent, handle passwords, complete 2FA, solve CAPTCHAs, or pay bills. Those are user identity decisions. Everything else should be carried by the product.

---

## Built Behavior

Phases 1–4 shipped the capability-lane substrate and the first keyless defaults. Normative behavior lives in `backend/core/setup/`:

- **`CapabilityLaneStatus`** — computed lanes (`llm`, `voice_input`, `voice_output`, `weather`, `search`, `integrations`, `smart_home`, `satellites`) exposed on `SetupStateResponse.capability_lanes`.
- **`ResolvedLlmConfig`** — main assistant LLM resolves from persisted `system_config.llm_config` plus `CredentialStore` secrets. Main LLM provider/model/base URL are **not** read from `.env` or `Settings`; background-agent credentials also resolve only from CredentialStore. Environment configuration remains for infrastructure and developer-only overrides.
- **Setup wizard** — managed on-device baseline, cloud path, and Advanced external-runtime discovery (`GET /setup/llm/local/discover`). Provider switching uses transactional `POST /setup/llm/activate`; `POST /setup/runtime/initialize` is reserved for startup recovery.
- **`CredentialStore`** — cloud LLM keys and Cartesia voice keys for setup/runtime; `resolve_llm_api_key()` reads stored secrets only (not env) on the product path.
- **Voice runtime config** — STT provider selection persists in `system_config.voice_config` and can be changed in-app under **Integrations → Voice & Keys**. Changes apply to the next voice turn without a backend restart.
- **Weather** — Open-Meteo default, keyless lane.
- **Search** — built-in DDGS → optional SearXNG / Exa behind `jarvis.search.web`.
- **Voice lanes** — Apple Speech input is ready when its helper reports permission and assets available; Cartesia input/output report `configured` only when a stored Cartesia key exists.
- **Google/Microsoft** — first-party OAuth metadata path; browser consent without per-user Cloud Console setup in the normal product flow.

---

## Design (unchanged intent)

### 1. Capability Setup Lanes

| Lane type | User action | Examples |
|---|---|---|
| `keyless` | None | Open-Meteo weather, built-in DDGS search |
| `local_service` | Install/start local sidecar | Ollama/LM Studio/llama.cpp, SearXNG, local STT |
| `api_key_optional` | Paste key only for upgrade | Exa search, Cartesia voice |
| `oauth_consent` | Browser consent only | Google Calendar/Gmail, Microsoft calendar/mail |
| `brokered_connect` | Broker consent link | Composio long-tail apps |
| `manual_handoff` | Human setup remains | Home Assistant device commissioning, Tailscale login |

Lane status values: `ready`, `configured`, `optional`, `degraded`, `needs_action`.

### 2. Keyless Defaults First

Weather uses Open-Meteo. Search works keyless via built-in DDGS; optional SearXNG and Exa remain upgrades. LLM setup offers a local zero-key path (Ollama, LM Studio, llama.cpp) or cloud providers through the wizard.

### 3. First-Party OAuth Apps

JARV1S ships first-party Google/Microsoft app metadata; users authorize accounts in the browser. Public ingress remains for push/webhooks, not for local account connect.

### 4. Credential Store For Unavoidable Secrets

`CredentialStore` holds host-only secrets that cannot be keyless or consent-only. Integration factories resolve `config_keys` lazily so saved keys apply without restart.

### 5. Brokers Stay Optional

Composio remains the convenience path for long-tail integrations. No Nango/Pipedream/Arcade migration in this slice.

---

## Phases (shipped)

### Phase 1 — Lane Substrate And Keyless Weather ✅

- `compute_capability_lanes()` in `backend/core/setup/lanes.py`
- Open-Meteo weather default
- Lazy `config_keys` resolution in `IntegrationManager`

### Phase 2 — Consent-Only Google/Microsoft ✅

- First-party OAuth app metadata; browser consent in the normal path

### Phase 3 — Search Provider Ladder ✅

- `plugins/search/` with DDGS baseline + optional SearXNG/Exa + `FallbackSearchClient`
- Search lane reports `ready` by default; configured optional providers surface as upgrades

### Phase 4 — Local Defaults In Setup ✅

- Local LLM discovery, presets (`ollama`, `lmstudio`, `llamacpp`), `llm_config` persistence, two-lane setup wizard
- Split voice lanes (`voice_input`, `voice_output`); runtime-specific no-model guidance in the wizard
- No-env cleanup: main LLM env vars removed from `Settings` and `.env.example`; setup is wizard + Mongo + `CredentialStore` only

### Deferred

- Co-browse/agentic credential acquisition from `GUIDED_CREDENTIAL_ACQUISITION.md`
- Relay-brokered OAuth or inference
- Broker migration from Composio
- Automated Tailscale/Funnel provisioning
- Local TTS as a first-class lane
- CodeAct compatibility scoring for marginal local models

---

## What Not To Build

| Idea | Why skip |
|---|---|
| Browser automation for account creation | Violates the user identity boundary and will break on CAPTCHA/2FA/payment. |
| Credential playbooks as the primary UX | Useful fallbacks only; still make the user do provider setup work. |
| New auth broker integration before lane substrate | Adds platform churn without fixing first-run UX. |
| Public tunnel requirement for account connect | Desktop loopback OAuth removes this for local account authorization. |
| Inference relay in local-first onboarding | Belongs to a clearly labeled convenience tier, not the zero-key path. |
