# Onboarding Experience Review

This documents the current clean-room onboarding flow from download to the first useful JARV1S turn. Each step records what the user is trying to do, what currently happens, what works, and the issues worth changing.

For the user expectations and mental model behind these findings, see [JARVIS_USER_MENTAL_MODEL.md](./JARVIS_USER_MENTAL_MODEL.md).

For the proposed product direction, see [IDEAL_ONBOARDING_EXPERIENCE.md](../proposals/IDEAL_ONBOARDING_EXPERIENCE.md).

## Review Run

- Date: 2026-07-24
- Machine / OS: Apple Silicon Mac / macOS version not recorded
- Release: `v0.2.3` (`7af5ec1`)
- Starting point: GitHub release page
- Goal: first useful JARV1S turn

## Flow

1. Download the DMG
2. Install JARV1S
3. Launch bundled services
4. Start setup
5. Choose cloud or local AI
6. Choose a cloud provider
7. Add and validate an API key
8. Finish setup
9. Reach the first useful outcome
10. Explore optional capabilities

The local-AI branch still needs review.

## 1. Download the DMG

**User goal:** Get the correct app for this Mac.

**Current experience:** Open the latest GitHub release and select **Download for macOS (Apple Silicon)**.

**Works well:** The release, beta status, platform requirement, and primary download are clear. The browser shows progress and time remaining.

**Issue — medium:** The DMG is 761 MB, creating a multi-minute wait before the user opens the app.

**Change:** Reduce the artifact size. Until then, state the size and expected download time beside the download instruction.

## 2. Install JARV1S

**User goal:** Move JARV1S into Applications and open it.

**Current experience:** The mounted DMG shows only the JARV1S app icon.

**Resolved — pending release verification:** The release pipeline now builds the standard macOS DMG layout with JARV1S on the left, an Applications alias on the right, and a clear drag arrow. The artifact validator mounts the DMG and fails if the Applications alias is missing.

**Completed:** Added the drag-to-Applications layout, JARV1S-styled background, Retina assets, app and volume icons, and a release regression check. Confirm on the next signed release artifact.

## 3. Launch Bundled Services

**User goal:** Open JARV1S and reach setup without managing infrastructure.

**Current experience:** JARV1S automatically checks prerequisites, prepares dependencies, starts MongoDB and the Host, and waits for health.

**Works well:** Startup is automatic and exposes its current phase.

### Resolved implementation issues

1. **Packaged Python is now relocatable.** The release runtime no longer ships a venv. It installs locked dependencies into a flat python-build-standalone tree at `runtime/python`. The Host invokes that bundled interpreter by absolute path, so users do not need Python, uv, Homebrew, Node, Docker, or a repo checkout. Packaged launch removes inherited `PYTHONHOME` and `VIRTUAL_ENV` values and preflights `import encodings` before starting the Host.
2. **Generated logs no longer write inside the app bundle.** Snapshots, prompt dumps, and generated logs use `JARVIS_DATA_DIR/logs` (`~/Library/Application Support/JARV1S/logs` when packaged). Snapshot directory creation is lazy rather than occurring during module import.
3. **Generated caches no longer write inside the app bundle.** FastEmbed, utterance, and MCP caches use `JARVIS_DATA_DIR/cache`; Home Assistant bootstrap data also uses the writable app data directory. Repo-only wake-word feedback is skipped when the packaged runtime has no training tree.

### Verification completed

- Rebuilt the runtime without a venv and confirmed `runtime/venv` is absent.
- Copied the runtime away from the build tree, including a Tauri-like symlink-dereferenced copy, and successfully imported the standard library and packaged dependencies.
- Passed the bundled MongoDB smoke test through the relocated interpreter using an empty, space-bearing temporary data path.
- Built and installed the unsigned app at `/Applications/JARV1S.app`; it reached `healthy` / `ready`, reported the database up, and completed startup without system Python on `PATH`.
- Confirmed generated caches appeared under `~/Library/Application Support/JARV1S/cache` and no `backend/logs` or `backend/.cache` directories were created inside the app bundle.
- Added mounted-DMG checks for the Applications alias and bundled imports (`encodings`, `fastapi`, `uvicorn`, and `motor`).

### Remaining error-experience polish

- Raw diagnostics can still dominate the primary screen.
- States such as `[ok] · checking`, `[ok] · running`, and `boot 1/7 failed · failed` can still look contradictory or duplicated.
- Host exits during startup now surface recent backend log lines instead of implying a port conflict; broader status UX cleanup is still open.

**Completed:** Relocatable packaged Python, data/cache path routing out of the app bundle, inherited Python-environment isolation, fail-fast runtime preflight, relocated smoke coverage, and clearer backend-exit failure detail.

## 4. Start Setup

**User goal:** Understand what must be configured before using JARV1S.

**Current experience:** A welcome screen says there is one required step and asks the user to start setup.

**Works well:** Optional integrations are deferred, the decision load is low, and there is one 44 px primary action.

**Issues — medium:**

- “Model provider” and “Pick how JARV1S should answer” describe implementation rather than the user's task.
- The screen does not state prerequisites, expected time, or that the choice can be changed later.
- Ollama, LM Studio, and llama.cpp appear before they are explained.
- The outlined primary action looks faint, while nested borders compete for attention.

**Change:** Frame the task in plain language:

- Title: **Choose your AI setup**
- Description: **JARV1S needs an AI model to understand and respond. Use a cloud service for the quickest setup, or connect a local model already running on this Mac. You can change this later.**
- Action: **Choose an AI option**

## 5. Choose Cloud or Local AI

**User goal:** Decide whether JARV1S uses an online AI service or software running on this Mac.

**Current experience:** Two large choices are shown. Cloud AI is recommended as the fastest path; local AI lists its required runtimes.

**Works well:** This is a clear two-option decision with a recommended path and useful prerequisites.

**Issues — low:**

- “Assistant brain,” “cloud provider,” and “local runtime” expose internal terminology.
- “Private and free” is too absolute.
- The recommended badge and Back button are very faint.

**Change:** Use **Choose where JARV1S runs AI** and describe local AI as: **Runs on this Mac. No cloud API key required. Requires Ollama, LM Studio, or llama.cpp already running.**

## 6. Choose a Cloud Provider

**User goal:** Pick the simplest suitable service without already understanding the provider market.

**Current experience:** Cerebras, Google AI Studio, and OpenRouter are listed with short descriptions and default model identifiers. One row is preselected and **Continue** advances to API-key setup.

**Works well:** Three options are manageable, rows are easy to scan, preview status is disclosed, and a default model is selected automatically.

**Issues — medium:**

- No provider is explained as the recommended default, so the user must interpret “fast responses,” “good all-round,” and “flexible model gateway.”
- Raw model identifiers add complexity even though the user is told model selection is automatic.
- Account requirements and possible usage costs are not set before selection.
- Selection relies mainly on a subtle border, and **Continue** does not confirm which provider will be used.
- “Preview” is repeated for Cerebras.

**Change:** Mark the intended default as **Recommended**, hide model identifiers from this step, state the meaningful account/cost tradeoff, add an explicit selected indicator, and label the action **Continue with [provider]**.

## 7. Add and Validate an API Key

**User goal:** Get the selected provider working without exposing their credential.

**Current experience:** The screen identifies the provider, links to its API-key page, accepts a hidden key, validates it, and stores it only after validation succeeds.

**Works well:** There is one clear task, a persistent input label, a provider-specific acquisition link, secure password input, and a disabled action until a value is entered.

**Issues — medium:**

- The external acquisition step is underspecified: the user may need to create an account, review billing, generate a key, and return to JARV1S.
- “Stored securely” is vague. The packaged Mac path encrypts the credential locally using a key protected by macOS Keychain.
- `gemma-4-31b` is implementation detail and does not help the user complete this step.
- **Connect** does not communicate that JARV1S will first verify the key.

**Change:** Briefly explain what happens after opening the provider link, replace the security claim with **Encrypted on this Mac using macOS Keychain**, remove the model identifier, and label the action **Verify and connect**.

## 8. Finish Setup

**User goal:** Know setup succeeded and start using JARV1S.

**Current experience:** A success screen confirms readiness, suggests a first message, and offers one **Open chat** action.

**Works well:** The completion state is unmistakable, the check icon provides positive closure, and the starter prompt removes blank-page uncertainty. No additional celebration is necessary.

**Issue — medium:** An orange warning flashed briefly before the success screen and disappeared before it could be read. A transient warning creates doubt at the highest-confidence moment and gives the user no chance to understand or act on it.

The setup wizard can replace the whole flow with its orange **Waiting for JARV1S** state whenever any service is temporarily reported as down. The exact warning shown during this run was not captured.

**Change:** Do not present transient initialization as a warning. Keep a stable **Finishing setup…** state while readiness settles. If a real warning requires action, keep it visible until resolved or acknowledged. Give **Open chat** stronger primary emphasis.

## 9. Reach the First Useful Outcome

**User goal:** Understand what JARV1S can do now and make it useful for a real need.

**Current experience:** The Home screen is almost entirely blank. A faint text field sits at the bottom left, **Enable microphone** is the strongest action, and Apps and Settings are available in the top navigation.

**What is ready:** The configured AI provider supports text conversation. Apps and voice are optional capabilities that still need separate setup.

**Issues — high:**

- The empty canvas provides no orientation, examples, or next step.
- The visual hierarchy implies that microphone access is the primary path, while the product logic waits for a successful text turn before offering optional voice setup.
- **Enable microphone** does not explain whether the user can immediately talk or whether transcription, speech output, and device selection still need configuration.
- The text field is visually secondary even though text is the only fully configured interaction.
- Apps and Settings expose places rather than outcomes; the user still has to determine what should be connected and why.
- A visible red stop control suggests recording or danger even when there is nothing to stop.
- Sending an arbitrary message proves the model works, but it does not demonstrate why JARV1S is valuable.

**Product goal:** The goal should be a first useful outcome, not merely a first message. A text turn is the quickest proof that the core works; capability setup should then follow the user's intended use rather than asking them to configure every integration upfront.

**Change:** Teach from the empty state:

- Title: **What would you like JARV1S to help with?**
- Description: **Text chat is ready. Start with a message, or add apps and voice when you need them.**
- Make the text composer the primary action and provide a small set of prompts that only use currently available capabilities.
- Offer **Connect an app** and **Set up voice** as clearly optional secondary paths.
- Explain that voice requires microphone permission and audio setup before requesting access.
- Hide inactive stop or recording controls until they are relevant.
- After the first successful turn, recommend the next capability contextually based on what the user tried to accomplish.

### Discover What JARV1S Can Do

**Observed experience:** Once Cartesia was connected, the first voice conversation worked well. When asked what it could do, JARV1S described managing schedules, filtering communications, controlling the environment, and remembering personal context even though those capabilities were not all connected.

The user then found **Apps**, which exposed 21 built-in groups. This made capabilities more visible but introduced tool counts, providers, health states, and categories such as Agents and Attention. Calendar appeared enabled while also disconnected, degraded, and requiring authentication.

**What works:** Successful voice interaction demonstrates the product's potential, and Apps provides a place to inspect capability health and setup.

**Issues — high:**

- JARV1S describes theoretical capabilities as if they are currently usable.
- The prompt includes available tool namespaces but not a clear live summary of what is connected and healthy.
- **My Apps** counts loaded built-in capability groups, not only apps the user has connected.
- **Enabled** means code is loaded, while the user reasonably interprets it as ready to use.
- Tool counts and internal categories overwhelm users who only want to understand possible outcomes.
- Discovery currently depends on exploring navigation and interpreting implementation state.

**Product principle:** Preserve discovery without adding a forced product tour. Teach just enough at the moment of intent, using live capability state.

**Change:**

- When asked what it can do, JARV1S should distinguish **Ready now** from **Connect to unlock** and offer a few relevant example requests.
- Feed live capability readiness into the dynamic turn context. Do not hard-code a list in the persona prompt.
- When a request needs an unavailable capability, explain the missing connection and offer the correct setup action.
- On the empty Home screen, show a few starter actions based only on capabilities that are currently healthy.
- In Apps, replace **My Apps** with clear **Ready** and **Needs setup** groups. Treat **Enabled** as an advanced plugin state, not user-facing readiness.
- Lead with outcomes such as calendar, messages, research, and smart home. Move tool counts, provider metadata, and detailed health under secondary details.

**Behavior invariant:** When users ask about abilities, JARV1S should separate immediately usable capabilities from those requiring setup because capability claims are interpreted as promises that will work now.

## 10. Explore Optional Capabilities

### Voice

**User goal:** Enable the microphone, say “JARV1S,” and receive a response.

**Current experience:** After microphone permission, the primary control changes to **Mute**, implying voice is ready. Saying “JARV1S” moves the status through **Detected** and **Listening**, then silently returns to idle without a transcript, response, or error.

**What happened in this run:** Cartesia was not selected. JARV1S selected its supported local transcription path, but the helper at `127.0.0.1:9091` was unavailable. The Host logged `Voice turn committed without streaming transcript; dropping turn.` (Historical note: that path was `local_streaming` / Parakeet; product STT is now `apple_speech`.)

Cartesia is one available transcription path, not the only supported path. The user should not need to understand this distinction before trying the microphone.

**Works well:** Wake-word detection succeeds and state changes provide immediate feedback that audio was heard.

**Issue — blocker for voice:**

- Microphone permission is treated as voice readiness even when no transcription backend is usable.
- **Mute**, **Detected**, and **Listening** imply a complete voice pipeline.
- Failed transcription silently discards the user's speech.
- No message explains what is missing or links to voice setup.
- The user cannot tell that transcription and optional spoken replies are separate capabilities.

**Change:**

- Gate the microphone action on end-to-end voice-input readiness, not browser audio permission alone.
- When transcription is unavailable, show **Set up voice** instead of **Enable microphone**.
- Offer a simple choice between working local transcription and Cartesia, including any install, download, account, or cost requirement.
- Only request microphone permission after a transcription path is ready.
- If transcription fails during use, stop listening and persist: **I heard “JARV1S,” but speech transcription is not available. Set up voice or continue with text.**
- Never silently drop user input.

#### Voice and Audio Settings

**Current experience:** One long settings page contains device selection, Cartesia credentials, transcription selection, spoken replies, wake-phrase testing, owner voice recognition, and tool sounds.

**Works well:** Transcription and spoken replies are technically separate, text replies remain available, and wake-phrase testing is distinguished from owner voice enrollment.

**Issues — high:**

- The page is organized around subsystems rather than the sequence needed to complete voice setup.
- **Audio ready** appears even when transcription is unavailable, and its description still says to enable microphone access.
- Local transcription appears selected without showing that its service is unreachable.
- Connecting Cartesia selects cloud transcription, but the next optional step—choosing how JARV1S speaks—is below the fold with a faint **Add a voice** action.
- “Voice” refers to three different concepts: voice input, JARV1S's speaking voice, and the owner's wake-word profile.
- A new user must already have a Cartesia voice ID or know how to provide a suitable cloning clip. There is no default voice, provider voice picker, or guided acquisition path.
- Success messages accumulate instead of clearly advancing the user to the next decision.

**Change:** Replace the long settings form with a short guided sequence:

1. **Microphone** — choose and test the input device.
2. **Transcription** — select a healthy local service or connect Cartesia.
3. **Replies** — use text replies, choose a licensed default/provider voice, use an existing voice ID, or clone a recording as an advanced option.
4. **Recognize me** — optionally enroll the owner's voice for wake-word filtering.

Show health beside each option, guide the user directly to the next incomplete step, and provide one end-to-end **Test voice conversation** action. Keep device processing and tool cues as secondary settings.

### Model Settings

**User goal:** Confirm the active AI setup or switch providers without breaking JARV1S.

**Current experience:** The page summarizes the active provider and model, offers four preset cards, opens credential setup when needed, and keeps custom configuration under **Advanced**.

**Works well:** The active state is visible, unavailable choices disclose that they need a key or runtime, changes are explicitly deferred until the next message, and advanced model details use progressive disclosure.

**Issues — medium:**

- Card titles are inconsistent dimensions: **Fast** and **Recommended** are benefits, while **OpenRouter** and **Local** identify providers or deployment modes.
- Cerebras is **Active** while Google AI Studio is **Recommended**, with no explanation of whether or why the user should switch.
- Speed, stability, privacy, account requirements, and possible usage cost are not presented in a comparable format.
- Raw model identifiers add noise for users who only want a safe default.
- “This JARV1S host” is infrastructure language; this packaged experience is running on the user's Mac.

**Change:** Use provider names as every card title and separate badges for **Active**, **Recommended**, **Fast**, **Preview**, and **Needs setup**. Add one consistent **Best for** line per option, explain why the default is recommended, and keep model identifiers under **Advanced**. Replace “this JARV1S host” with “this Mac.”

### Credentials Settings

**User goal:** Add a capability they understand and want to use.

**Current experience:** The page lists Exa search, Composio, and Background agents as credential cards with identical **Add key** actions. Expanding a card reveals a password field but no acquisition guidance. External-trigger status appears at the bottom.

**Works well:** Stored and missing states are visually separated, secrets are masked, replacement and removal are supported, and Cartesia is correctly directed to Voice & Audio.

**Issues — high:**

- The page starts with credentials rather than the user value each credential unlocks.
- It does not say that all three are optional and core text chat works without them.
- There are no provider signup links, account or billing expectations, or instructions for obtaining a key.
- **Background agents** requires an Anthropic API key, but Anthropic is not named in the visible card or key form. A user who selected another model provider would not expect this dependency.
- **Exa search upgrade** does not explain the current search behavior or what becomes better.
- Composio requires a platform key before individual apps can be connected through OAuth, but this two-stage setup is not explained.
- **Add key** stores a credential without communicating validation.
- **External triggers: Off — calendar and Gmail automations still poll every minute** is contradictory and unrelated to credential management.
- “This JARV1S host” and “stored securely” are vague infrastructure language.

**Change:**

- Keep this page as an advanced credential inventory, but initiate setup contextually from the capability or app the user is trying to use.
- Name both capability and provider: **Enhanced web research — Exa**, **App connections — Composio**, and **Background agents — Anthropic**.
- For each card, state **Optional**, what works without it, what connecting enables, account/cost expectations, and a provider-specific **Get key** link.
- Replace **Add key** with **Verify and connect** and only persist validated credentials.
- Explain that Composio setup precedes individual app authorization.
- Move external-trigger status to Availability or automation settings and explain precisely what “Off” disables.
- Replace host language with **this Mac** and describe the actual storage protection.

### Availability Settings

**User goal:** Decide whether JARV1S should keep running locally, work from a phone, or receive immediate updates from connected services.

**Current experience:** One page combines Tailscale private access, phone pairing, instant service updates, launch at login, menu-bar background behavior, status refresh, and restart controls.

**Works well:** Remote access uses a private network rather than exposing the Mac publicly, dependencies are sequenced, phone pairing is blocked until private access is ready, sleep risk is visible, and instant updates are identified as optional.

**Issues — medium:**

- The page does not first say that normal use on this Mac is already ready and every remote feature is optional.
- Four separate jobs—background runtime, remote access, phone pairing, and service-event delivery—compete in one long flow.
- Tailscale is introduced without explaining what it is, that it requires an account and installation on both devices, or why JARV1S uses it.
- **JARV1S running** beside **Setup needed** mixes local and remote status.
- The sleep warning has no direct recovery action.
- Phone pairing does not explain how the phone opens or installs the JARV1S companion before scanning.
- **Instant updates** does not identify which connected services support them or why Tailscale is a prerequisite.
- Multiple unavailable controls make the page feel broken before private access is configured.
- **Restart JARV1S** is a troubleshooting action presented alongside routine preferences.
- “This JARV1S host” is infrastructure language.

**Change:**

- Start with **JARV1S is ready on this Mac. Remote access and background operation are optional.**
- Separate the page into:
  1. **Run in background** — launch at login, menu-bar behavior, and sleep handling.
  2. **Use from other devices** — explain Tailscale, install/connect it, then reveal phone pairing.
  3. **Instant updates** — show only when a connected service can use them and explain the fallback behavior.
- Show distinct statuses for local runtime, Tailscale connection, private access, and phone pairing.
- Add an action for sleep configuration and explain behavior when the Mac sleeps.
- Explain how to open the phone companion before showing its QR code.
- Move restart and detailed diagnostics under troubleshooting.
- Replace host language with **this Mac**.

### Smart Home

**User goal:** Clicking **Home** conventionally means returning to the product's main screen.

**Current experience:** **Home** opens a **Smart Home** panel containing a Home Assistant connection form. The panel immediately assumes an existing Home Assistant instance and asks for its URL and a long-lived access token.

**Works well for existing Home Assistant users:** The panel names the required credential, warns that refresh tokens will not work, and provides numbered instructions and direct links.

**Issues — high:**

- The navigation label **Home** does not match its destination and violates the standard meaning of Home navigation.
- The panel does not explain that Home Assistant is optional or what connecting it enables.
- There is no path for someone who does not already use Home Assistant.
- Home Assistant has multiple installation methods; the UI assumes installation and jumps directly to advanced credential setup.
- `127.0.0.1:8123` is suggested even though it only works when Home Assistant runs on the same Mac.
- A long instruction list and credential form appear at once, increasing cognitive load.
- External profile-navigation instructions can become stale as Home Assistant changes.

**Change:**

- Reserve **Home** for the main JARV1S workspace. Place Home Assistant under **Apps**, or label a dedicated destination **Smart Home**.
- Start with: **Connect Home Assistant to control devices, scenes, and sensors. This is optional and JARV1S works without it.**
- Ask **Do you already use Home Assistant?**
  - **Connect existing Home Assistant**
  - **Learn how to set up Home Assistant**
  - **Not now**
- Link new users to Home Assistant's official installation chooser rather than prescribing Docker or another unsupported installation path.
- For existing users, split setup into URL discovery/validation first, then token acquisition and connection.
- Do not prefill localhost. Offer it as an example alongside `homeassistant.local` and explain when each applies.

## Cross-Cutting Lessons

- Use the user's task language before product or infrastructure terminology.
- Follow standard platform conventions instead of relying on prior knowledge.
- Keep packaged code read-only and generated data in app-owned writable directories.
- Make status truthful: completed, running, and failed must be distinct.
- Error messages should identify the failing component and offer an action that can help.
- Recommend a safe default when the user lacks information to compare choices.
- Surface the size and progress of first-run downloads.
- Design empty states to teach the next valuable action.
- Distinguish what is ready now from optional capabilities that still need setup.
- Only present controls as ready when the complete user-facing capability is healthy.
- Never discard an attempted action without visible feedback and recovery.
- Describe capabilities from live readiness state, not the theoretical tool inventory.
- Prefer contextual teaching over forced tours and exhaustive setup checklists.
