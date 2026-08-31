# Local-First Integration Connections

**Status:** Phase 1 built (EventKit V0). Phase 2 done. Phase 3 done (official Google Desktop client).  
**Date:** 2026-08-29  
**Related:** `built/INTEGRATION_SETUP.md`, `INTEGRATION_FOUNDRY.md`, `GUIDED_CREDENTIAL_ACQUISITION.md`

This proposal supersedes the two-tier auth default in `INTEGRATION_SETUP.md`.
Foundry remains a possible later source of reviewed plugins, not the connection
architecture.

## Shape

- **Job:** Ask JARV1S about the apps already used on this Mac and get a useful answer without setup documentation or developer tools.
- **Moments:** “What is next on my calendar?” on a new Mac should lead to one macOS permission and a verified answer; a denied or later-revoked permission should explain exactly how to restore access; after V0, “connect Gmail” should open Google sign-in rather than Google Cloud Console.
- **Axes:** identity (provider account and local Mac), consent (OAuth grant versus action approval), truth (provider grant, local permission, and loaded tools must project as one connection state).
- **Record:** the OS permission or provider grant is authoritative; `CredentialStore` holds delegated secrets; `AuthManager` holds non-secret account/grant metadata; lifecycle derives `IntegrationView`; domain plugins own user-shaped tools and action consent.
- **Owner:** extend `core/auth`, `core/integrations/lifecycle`, and the existing domain plugins. The desktop Host owns OS permissions and native helpers. Do not introduce a second integration framework.
- **Cut:** V0 is EventKit read/search through the existing Calendar tools, moment-of-intent permission, one verified read, truthful Apps status, and disconnect. It is not calendar mutation, direct OAuth, browser automation, another provider, Foundry, or a hosted auth service.
- **Invariant:** When an official JARV1S build can connect directly, setup must not require a developer console or third-party broker; when it cannot, JARV1S must name the trade-off instead of silently changing the custody model.

## Decision

JARV1S should own **application identity**, never user passwords, and locally
safeguard delegated OAuth tokens instead of operating a hosted data plane.

For providers that support public/native clients:

1. The JARV1S project registers and verifies the OAuth application once.
2. The official desktop build ships its public client ID. A public client ID is an identifier, not a secret.
3. The Mac opens the provider's system-browser consent page using authorization code + PKCE.
4. The provider redirects to a loopback listener or verified app URI.
5. The Host exchanges the code directly, stores access and refresh tokens in Keychain-backed storage, and calls the provider API directly.

```text
User -> provider sign-in -> local JARV1S Host -> provider API
                              |
                              +-> Keychain-backed token storage
```

No JARV1S server needs to receive tokens or API payloads. The maintainer still owns provider verification, branding, quota, privacy-policy, and incident-response obligations. That is materially smaller than becoming an auth or integration provider.

Self-managed OAuth remains an advanced option for forks and users who want an independent provider registration. It should not be the default product path.

Connection locality and inference locality are separate. A direct connection
keeps credentials and provider traffic off a JARV1S server, but calendar or
email content still leaves the Mac when the configured model is hosted. Apps
must disclose both **connection** (`On this Mac`, `Direct`, `Cloud connector`)
and **processing** (`Local model`, or the named model provider). Google
restricted-scope review must be assessed against the complete data path, not
only token storage.

## What the leading platforms actually do

Apple, Google, and Microsoft feel zero-setup because they already own the operating-system or account identity:

- Apple apps expose typed App Intents to Siri, while Apple can use OS-held accounts and permissions. App Intents do not give JARV1S a global toolbox for invoking every installed app.
- Google Gemini uses the user's existing Google identity and first-party access to Workspace.
- Microsoft Copilot uses the signed-in Microsoft identity and Graph permissions.
- ChatGPT and Claude still use OAuth for third-party connectors; their advantage is that their organizations register the OAuth apps once and present one consistent connection flow.

The transferable pattern is not privileged access. It is:

- one product-owned application identity;
- local or account-native credential custody;
- typed, user-shaped capabilities;
- setup at the moment of intent;
- explicit permission and action boundaries.

## Connection ladder

Each service should use the least-privileged supported path that satisfies the
capability. The model-facing plugin stays stable when the provider underneath
changes.

### 1. OS-native data

Use supported local frameworks where the OS already manages the account.

First target: EventKit behind the existing `calendar` plugin. A user who already has iCloud, Google, or Exchange calendars configured on macOS grants JARV1S Calendar access once. There is no provider OAuth ceremony and JARV1S never receives the account password or provider refresh token. The native component owning EventKit must carry the Calendar entitlement and usage description, and its visible bundle identity must own the macOS TCC grant.

This is the closest reproducible version of Apple's advantage.

### 2. Direct local OAuth

Use a JARV1S-owned public/native OAuth registration, local PKCE exchange, local token storage, and direct provider API calls.

Initial candidates:

- **Google:** desktop OAuth supports loopback redirects and PKCE. Calendar scopes are sensitive; Gmail read/modify scopes are restricted and require verification. Verification and policy obligations still apply even when tokens stay local.
- **Microsoft:** a multitenant public-client registration supports system-browser PKCE and device code without a client secret. The current JARV1S `/consumers` authority only supports personal accounts; work/school support requires a compatible multitenant registration and `/common` authority.
- **Slack:** public-client PKCE is supported for desktop user scopes. Desktop redirects cannot request bot scopes. Zero-setup bot installation needs a confidential exchange or named broker; Socket Mode can localize later events but does not remove that initial requirement. PKCE tokens rotate and expire after 30 days.
- **GitHub:** use authorization code + PKCE for an interactive desktop app and reserve device flow for headless use. Device flow has additional phishing risk. A GitHub App may offer finer repository permissions but JARV1S must never ship its private key in the desktop client.

Do not ship provider secrets in the desktop app. A secret embedded in an open-source/native client is not confidential.

### 3. Local app adapters

Use supported local automation for applications that expose no suitable API:

- named Shortcuts with explicit input/output contracts;
- narrow Apple Events for scriptable applications;
- the smallest native component that can own the required framework and OS permission.

These adapters should implement existing domain tools. Do not expose `click`, `type`, AppleScript, or arbitrary shell operations as integration tools.

### 4. Dedicated browser session

For a service with no practical API or local framework, use a visible, dedicated JARV1S browser profile:

- the user signs in and completes MFA manually;
- cookies remain in a per-service profile and are erased on disconnect;
- deterministic Playwright locators and accessibility snapshots are preferred;
- arbitrary navigation, JavaScript, uploads, and network inspection are unavailable;
- domains, downloads, supported actions, and profile permissions are allowlisted in code;
- deterministic code extracts a small typed result; raw page instructions are not passed to the model;
- password entry, account recovery, CAPTCHA, payments, and permission changes trigger human takeover;
- consequential actions use the existing JARV1S consent flow immediately before commit.

Browser control is an adapter behind a small semantic plugin, not a general model-facing browser. Stagehand or visual computer use may assist locator recovery in experiments, but should not become the primary executor.

This path is more privileged and less reliable than scoped OAuth. It must not be used to evade provider policy or app review.

### 5. Explicit cloud connector

Composio remains useful for breadth, hosted triggers, and providers whose OAuth posture is impractical. It should be presented as **Cloud connector — powered by Composio**, with clear disclosure that Composio stores provider credentials and processes tool traffic.

It is an opt-in custody mode, not an invisible implementation detail and not a prerequisite for Apps.

Cloud tool execution must fail closed: an absent allowlist cannot mount every
tool, unreviewed mutations cannot execute, and policy is enforced at execution
rather than inferred from routing metadata. Hosted triggers also require public
ingress and an awake Host unless a separate hosted receiver exists; that
availability trade-off is distinct from ordinary connected-app use.

Self-hosting Nango does not remove the application-registration problem: either the JARV1S project still owns each OAuth app or every user must create one. It can operate token lifecycle infrastructure, but does not create a third answer to provider identity.

## One user experience

The user should see one Apps connection flow regardless of the underlying layer:

```text
Ask to use capability
  -> Apps detail explains value and custody
  -> Connect
  -> OS permission / provider sign-in / manual browser login
  -> Verify a real read
  -> Ready
```

Apps may show a small connection label:

- **On this Mac** — OS framework or local app adapter
- **Direct** — provider OAuth and API calls terminate on this Mac
- **Browser session** — isolated local browser authority; experimental
- **Cloud connector** — credentials or traffic are handled by a named service
- **Advanced** — user-owned OAuth app or custom MCP server

Do not ask users to choose an auth architecture before they understand the capability. Select the safest supported default and put alternatives under connection details.

`core/integrations/lifecycle` becomes the only connection mutation owner. Apps, voice widgets, OAuth callbacks, startup reconciliation, and disconnect all call it. `IntegrationView` remains the single derived read model; it should not become another durable record. It needs derived fields for connection mode, credential/data processor, selected account, granted capabilities, and provider/OS permission state.

## Capability and authorization boundaries

OAuth connection, tool availability, and approval to act are different:

1. **Connection grant:** provider/OS permits access.
2. **Capability exposure:** JARV1S offers only reviewed tools whose required scopes are currently granted.
3. **Action consent:** JARV1S decides whether this specific read or mutation may execute.

Required changes to the current contract:

- Request scopes when a capability is first needed instead of unioning all ecosystem scopes at initial connection.
- Compare exact provider scope identifiers; do not normalize URL scopes by final path segment.
- Store access tokens, refresh tokens, and OAuth client secrets in Keychain-backed `CredentialStore`, not plaintext MongoDB. Mongo may retain non-secret account/grant metadata.
- Key grants and caches by provider + immutable provider subject, not provider or mutable email alone. Account choice must be explicit when more than one grant exists.
- Serialize refresh per grant and persist rotated refresh tokens atomically before invalidating the previous token.
- Disconnect should stop dependent work, revoke upstream access when supported, clear local credentials/caches, and then update tool availability.
- Auto-bridged MCP/Composio tools must not bypass mutation consent. An absent allowlist mounts nothing; unreviewed bridges are read-only; enabled writes require explicit tool allowlisting and execution-time risk policy.
- Credentials, browser cookies, authorization codes, PKCE verifiers, and raw secrets never enter prompts, tool results, traces, frontend storage, or background-worker environments.

Domain plugins continue to own human-shaped tools and target validation. This proposal does not replace the existing plugin contract with generic browser or OAuth tools.

## Experiments

### Experiment A — EventKit calendar provider

Put EventKit in the smallest native component whose JARV1S bundle identity can
own the Calendar permission, and place it behind the current `calendar` plugin.
V0 implements read and search only.

Test:

- iCloud, Google, and Exchange calendars already configured in Calendar;
- permission grant, denial, revocation, and app update;
- read/search behavior, recurring events, and adapter identifier stability;
- sync delay;
- voice latency versus direct Google/Microsoft APIs.

Ship bar: from a clean request to the first verified answer in under two
minutes, with no documentation, CLI, restart, or developer console; at least
95% correct completion over real dogfood requests; actionable permission
failures.

### Experiment B — Official direct Google connection

After V0, register a JARV1S Desktop OAuth client, use loopback PKCE, and remove client-secret entry from the default Google flow.

Choose one capability EventKit cannot satisfy, such as Gmail or cross-platform
Calendar support. Do not build a second Google Calendar path merely to prove
OAuth. Start with the narrowest useful scopes; Gmail restricted scopes should
be added only after verification, privacy-policy, model-processing, and
security-assessment obligations are confirmed.

Test:

- clean-install setup time;
- incremental scope grant and partial denial;
- refresh rotation, revocation, multiple accounts, and Workspace admin restrictions;
- official build versus fork/BYO credentials;
- provider quota and abuse handling.

Success means “Connect Google” is a normal provider consent flow with no Cloud Console instructions.

### Later evidence gate — one read-heavy browser integration

Choose a low-risk service with no good API, such as order status or a utility portal. Do not start with email, banking, social posting, purchases, or password management.

Build two or three semantic read tools with a dedicated Playwright profile. Compare deterministic locators against Stagehand-assisted locator recovery. Stop on MFA, changed domains, unapproved navigation, or unexpected page state.

Run this only after V0 and one direct-OAuth provider are dogfooded. It determines whether browser adapters are a maintainable long-tail layer; it does not justify a general computer-use runtime.

## Refactor sequence

### Phase 1 — V0 EventKit vertical slice

- Route Calendar read/search through EventKit when the user selects the Mac calendar connection.
- Request permission at the moment of intent, verify one real read, and project status through `IntegrationView`.
- Keep Google/Microsoft providers available; do not add calendar mutation yet.

**Status:** done.

### Phase 2 — make direct OAuth trustworthy

- Make lifecycle the sole connect/disconnect/reconcile mutation path.
- Project all Apps and voice status through `IntegrationView`.
- Move OAuth tokens and secrets into Keychain-backed storage. This is an acceptance gate before an official OAuth client ships.
- Fix account-keying, exact scope checks, refresh locking, revocation, and incremental consent.
- Keep existing tool names and provider clients stable.

**Status:** done.

### Phase 3 — prove one direct provider

- Ship an official direct Google Desktop client for one capability not already covered by EventKit.
- Replace default Google Cloud setup instructions with one provider sign-in; retain BYO credentials under Advanced.
- Relabel Composio as an explicit cloud connector and make bridged execution fail closed.
- Use dogfood evidence before committing to another direct provider, the browser experiment, or Foundry.

**Status:** done. Official Google Desktop identity is a CI-bundled `product_oauth.json` (`JARVIS_PRODUCT_OAUTH` path). Connect Gmail is Google sign-in when that file is present; otherwise Advanced. Composio Apps copy is “Cloud connector”; un-allowlisted Composio mounts nothing.

## What not to build

- A hosted JARV1S OAuth broker unless a target provider strictly requires a confidential backend.
- Browser automation that creates developer projects for every user as the default path. It makes a bad setup flow less manual but keeps every user responsible for app registration and verification.
- A generic computer-use tool available to the foreground model.
- A single universal integration abstraction that erases whether authority comes from the OS, direct OAuth, a browser session, or a cloud broker.
- A new durable “connection” collection while provider grants and broker state remain authoritative.
- Automatic mounting of every MCP write tool.

## Risks

- The official JARV1S project becomes responsible for OAuth verification, branding, shared quota, abuse reports, and provider policy changes.
- Public client IDs can be copied. Official builds should be signed and identified where providers support attestation, but public-client security must never depend on keeping the ID secret.
- Google restricted Gmail scopes may impose recurring verification or security-assessment cost. This may delay direct Gmail even when Calendar is ready.
- OS-native providers are platform-specific and may be stale while the Mac sleeps.
- Browser sessions are bearer authority with broader practical access than narrow OAuth scopes. They remain experimental and locally isolated.
- Some services prohibit AI or voice use regardless of transport. Spotify's current developer policy is a product constraint, not an auth problem.

## Sources

- [RFC 8252 — OAuth 2.0 for Native Apps](https://www.rfc-editor.org/rfc/rfc8252.html)
- [RFC 9700 — OAuth 2.0 Security Best Current Practice](https://www.rfc-editor.org/rfc/rfc9700.html)
- [Google OAuth for Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google OAuth best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)
- [Google restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Microsoft desktop app configuration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration)
- [Slack PKCE](https://docs.slack.dev/authentication/using-pkce)
- [Apple EventKit calendar access](https://developer.apple.com/documentation/EventKit/accessing-calendar-using-eventkit-and-eventkitui)
- [Apple App Intents](https://developer.apple.com/documentation/appintents)
- [Playwright persistent browser contexts](https://playwright.dev/docs/api/class-browsertype)
- [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/index)
- [Composio managed versus custom auth](https://docs.composio.dev/docs/authentication/custom-app-vs-managed-app)
