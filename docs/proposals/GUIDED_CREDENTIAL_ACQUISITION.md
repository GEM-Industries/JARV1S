# Guided Credential Acquisition

**Status:** Proposed
**Date:** 2026-06-11
**Related:** `docs/research/ONBOARDING_EXPERIENCE_REVIEW.md`, `docs/proposals/LOCAL_FIRST_INTEGRATIONS.md`, `backend/core/credentials/`, `backend/core/setup/`

---

## Problem

Almost every JARV1S capability used to hide behind "create an account at a third-party console, find the API key page, paste a string into `.env`, restart." **Main assistant LLM setup no longer uses `.env`** — it persists to `system_config` + `CredentialStore` via the setup wizard ([KEYLESS_ONBOARDING_LANES](./built/KEYLESS_ONBOARDING_LANES.md)). Remaining API-key capabilities (Exa, Cartesia, Composio, Anthropic/reasoning tier) still benefit from guided acquisition.

Claude Code / Cowork-class products feel effortless because *one* account unlocks everything. JARV1S can't do that without becoming a hosted service, which violates its zero-trust premise. The question is: how close can we get while every credential stays on the user's machine?

The idea of "Jarvis creates the accounts for you" via browser control is attractive but has hard edges that shape the design:

- **Account creation autonomy is mostly off the table.** Signups involve CAPTCHAs, email/phone verification, payment details, and ToS that frequently prohibit automated registration. Mainstream agent products (Operator/Atlas-class) explicitly refuse to create accounts or enter passwords — for good reasons.
- **Credentials must never transit the LLM.** Browser-agent ecosystems converged on the same patterns: persistent browser profiles for session reuse, credential injection that bypasses the model (Browser Use `sensitive_data`, 1Password agentic autofill), and human-in-the-loop handoff at login/2FA walls.
- **Most of the pain isn't navigation — it's knowing where to go.** The user doesn't need a robot to click for them nearly as much as they need the *exact* key-creation URL, a picture of what to expect, and a paste box that validates and stores the result correctly.

So: ship the cheap deterministic layer first, the agentic layer as a progressive enhancement.

---

## Design

Three tiers. Each tier is independently shippable and each higher tier degrades to the one below it.

### Tier 0 — Provider playbooks (deterministic, ship first)

A playbook is a small YAML file per provider describing how to acquire its credential:

```yaml
# backend/core/credentials/playbooks/exa.yaml
provider: exa
label: Exa (web search)
secret: EXA_API_KEY
account_required: true
free_tier: "1,000 searches/month, no card"
steps:
  - "Sign in or create an account"
  - "Open the API Keys page"
  - "Create a key and copy it"
key_page: "https://dashboard.exa.ai/api-keys"
signup_page: "https://dashboard.exa.ai/signup"
key_format: "^[a-f0-9-]{36}$"          # paste-time validation
verify:                                  # post-paste live check
  method: GET
  url: "https://api.exa.ai/..."
  auth: bearer
```

The frontend renders this as a `CredentialWidget`: capability framing ("Web search needs an Exa account — free tier covers normal use"), deep link that opens the key page, paste box with format validation, live verify call, then storage through `CredentialStore` (keyring/encrypted file — never `.env`). Integration status flips immediately; no restart (the Integration Gate's lazy factories already re-read config via `integrations.reset()`).

This single tier eliminates the `.env` junk drawer (friction #26), the "which URL do I even go to" problem, and the restart cycle — for every API-key provider — at the cost of one YAML file each. Playbooks are community-maintainable, which fits an open-source project: a stale deep link is a one-line PR.

**Setup becomes capability-led, not provider-led.** The wizard/Settings shows "Web search — not configured → [Set up, ~2 min]" instead of a wall of env var names. Each lane reports configured/missing/invalid using the playbook's `verify` block, replacing today's fail-at-first-use behavior (friction #11, #12).

### Tier 1 — Co-browse assist

For consoles where the key page is several ambiguous steps deep (Google Cloud is the canonical offender), Tier 0's deep link isn't enough. Tier 1 opens the page in a JARV1S-driven browser window **on the user's machine** (Playwright context, headed) while Jarvis narrates and highlights:

- Jarvis drives navigation between steps and visually marks the next control ("click *Create credentials* → *OAuth client ID*").
- The **user** performs login, 2FA, CAPTCHA, and any payment step — the agent detects these walls and waits, exactly the human-handoff pattern the browser-agent ecosystem standardized on.
- At the end, with an explicit consent prompt ("Jarvis wants to read the API key shown on this page to save it securely"), the harness reads the key from the DOM and writes it straight to `CredentialStore`. The key never enters chat history, model context, or logs.

Playbooks gain an optional `assist:` section (per-step URLs/selectors/expected page markers). Selector drift fails soft: any step that can't be matched falls back to showing the Tier 0 instructions for that step.

### Tier 2 — Agentic navigation (opt-in, narrow)

Full agent-driven navigation (browser-use-style) for remaining API-key consoles and the Advanced/fork OAuth-app-creation path. Official Google connect no longer uses Cloud Console — see [LOCAL_FIRST_INTEGRATIONS.md](./LOCAL_FIRST_INTEGRATIONS.md). The old `INTEGRATION_SETUP.md` "4-hour ceremony" is Advanced only.

Hard constraints, non-negotiable:

1. **No autonomous account creation.** The agent assists *inside* an account the user authenticates; signups remain Tier 0/1.
2. **No password/2FA/payment handling, ever.** Wall detected → pause → user acts → resume. No credential injection into login forms even "helpfully."
3. **Headed and narrated.** The user watches every action; a persistent stop control kills the session.
4. **Consent-gated secret reads.** Reading any value classified as a secret goes through the existing `require_consent()` flow before the DOM read happens.
5. **Domain allowlist per playbook.** The session cannot navigate off the provider's domains.

The LLM plans and acts on *page structure*; secrets flow harness → `CredentialStore` without model visibility — the same boundary the `sensitive_data` patterns enforce.

Tier 2 is justified by remaining cloud-console ceremonies (forks, Advanced BYO clients, other providers). Official Google connect is already provider sign-in; if later product clients cover the rest, Tier 2 may never need to ship.

---

## Where this lives

| Piece | Location | Notes |
|---|---|---|
| Playbook schema + loader | `backend/core/credentials/playbooks/` | Pydantic model; YAML per provider |
| Verify runner | `backend/core/credentials/verify.py` | shared by setup state + Settings lanes |
| REST | `GET /setup/credentials/lanes`, `POST /setup/credentials/{provider}` | thin over the above |
| `CredentialWidget` | frontend SDUI registry | same pattern as `OAuthWidget`/`ConnectWidget` |
| Browser harness (Tier 1/2) | new `backend/core/browser/` | Playwright; one session class, consent-gated `read_secret()` |

Tier 0 has **no new dependencies**. Tier 1/2 add Playwright, isolated behind one module, lazily imported.

---

## What NOT to build

| Idea | Why skip |
|---|---|
| Autonomous account signup | CAPTCHA/ToS/payment walls make it brittle and legally murky; assist-not-impersonate is the stable design point |
| Storing user passwords so Jarvis can log in | Catastrophic blast radius; session-persistence via browser profile achieves re-auth-free assists without holding passwords |
| Generic "do anything" browser tool exposed to the LLM | This proposal is a *setup* surface with per-playbook allowlists, not a general browsing capability — that's a separate (riskier) proposal |
| Screenshot-to-LLM secret pages | Secrets must not enter model context in any modality; DOM reads are consent-gated and harness-local |
| Hosted relay that acquires keys server-side | That's just rebuilding Composio; defeats the purpose |

---

## Phases

**Phase 1 — Playbooks + CredentialWidget (Tier 0)**
- Schema, loader, verify runner; playbooks for: Exa, OpenWeather, Cartesia, DeepInfra, OpenRouter, Groq, Anthropic
- Capability-led lanes in setup/Settings; storage via `CredentialStore`; hot-apply via `integrations.reset()`

**Phase 2 — Co-browse assist (Tier 1)**
- Playwright harness, headed sessions, step highlighting, consent-gated key capture
- `assist:` sections for Google Cloud + Azure AD playbooks

**Phase 3 — Agentic flows (Tier 2, opt-in)**
- Agent-planned navigation under the constraint set above
- **Target:** remaining OAuth-app-creation ceremonies (Advanced/forks, other providers); re-evaluate after more product clients ship

---

## Tradeoffs

- **Playbooks rot.** Providers redesign dashboards. Mitigations: format-validation + live verify catch staleness immediately; deep links rot slower than selectors; community PRs are cheap. Tier ordering puts the most rot-resistant layer first.
- **Playwright is a heavy optional dependency.** Confined to Tier 1+; Tier 0 covers the majority of providers with zero added weight.
- **Assist flows are slower than an expert.** Fine — the audience is people for whom the alternative is abandonment, not 30 seconds saved.
