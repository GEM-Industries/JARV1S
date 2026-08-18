# JARV1S User Mental Model

This document describes the likely mental model of a new JARV1S user, the
expectations created by the product, and the experience from download to first
use. It is a product hypothesis based on the current implementation and the
clean-room run in [ONBOARDING_EXPERIENCE_REVIEW.md](./ONBOARDING_EXPERIENCE_REVIEW.md).
It should be validated with observed onboarding sessions and user interviews.

**Product direction:** [Ideal JARV1S Onboarding Experience](../proposals/IDEAL_ONBOARDING_EXPERIENCE.md)

## Core framing

The user does not think they are configuring an AI platform. They think they
are hiring an assistant.

They expect one coherent product that can understand requests, remember useful
context, connect to their services, and take actions. They do not expect to
assemble independent models, plugins, credentials, local services, and
integration providers.

## Likely initial audience

The current beta user is probably:

- technically curious and comfortable downloading an app from GitHub;
- familiar with ChatGPT, Claude, Siri, Alexa, or similar assistants;
- willing to follow a guided API-key step;
- interested in automation, voice, privacy, or connecting several services;
- not necessarily a developer or infrastructure operator;
- unwilling to learn JARV1S internals unless something fails.

Downloading from GitHub creates some tolerance for beta roughness. A packaged
Mac app still creates the expectation that required infrastructure is included
and routine setup will use familiar sign-in and permission flows.

## What the name promises

“JARV1S” implies more than an AI chat interface. A new user is likely to expect:

- one personal assistant rather than a collection of tools;
- natural text and voice interaction;
- awareness of their apps, preferences, and environment;
- memory across conversations;
- the ability to take actions, not only answer questions;
- proactive help and automation;
- technical complexity to be handled by the product.

Capability statements are therefore interpreted as promises. “I can manage
your calendar” means the user's calendar will work now, not that calendar tools
are installed but still require authentication.

## What the user probably understands

Most users understand that:

- AI can answer questions and generate content;
- cloud AI sends requests to an online service;
- a local model may offer more privacy;
- connecting Google or another app usually requires signing in and granting
  permission;
- microphone access is required for voice.

Most users will not naturally distinguish:

- JARV1S from its language-model provider;
- a provider from a model;
- an API key from an account subscription;
- OAuth from a platform credential such as Composio;
- a loaded plugin from a connected and healthy capability;
- microphone permission from wake-word detection, transcription, and speech
  output;
- the primary model from separate Cartesia, Anthropic, Exa, or Home Assistant
  dependencies;
- the desktop app from its Host, database, sidecars, and remote-access layer.

These are valid implementation concepts, but they are not a reasonable
prerequisite for using a personal assistant.

## Journey and likely internal state

### 1. Download

**What they see:** A large packaged Mac application on a GitHub release.

**What they think:** “This is beta software, but it is a real desktop product.”

The large download increases both commitment and expectation. Its size suggests
that the required runtime is included.

### 2. First boot

**What they see:** JARV1S checking prerequisites, starting services, and
reporting boot phases.

**What they think initially:** “It is setting itself up.”

If raw diagnostics dominate, that changes to: “Is this a finished application
or a developer project?” The user does not need to understand MongoDB, Host
health, runtime initialization, or boot-stage names. They need to know whether
startup is progressing normally, requires action, or has failed.

### 3. Choosing AI

**What they see:** Cloud or local AI, provider choices, model names, and an API
key step.

**What they expected:** JARV1S already contains or provides its AI.

Likely questions include:

- Why does JARV1S need another AI service?
- Will an existing ChatGPT or Google subscription work?
- Does this require another paid account?
- Which option is safest, cheapest, or best supported?
- Where will my data go?
- If I choose local, will JARV1S install it?

Cloud versus local is understandable at a high level. Provider and model
selection is not meaningful without costs, privacy implications, and a clear
default. An API key can feel like a developer credential rather than normal
product setup.

### 4. Setup completes

**What they see:** “You are ready.”

**What they think:** “JARV1S is configured.”

They do not interpret this as “the database and language model are ready, while
most assistant capabilities remain unconfigured.” This is the first major
difference between system state and user understanding.

### 5. First workspace

**What they see:** An almost empty workspace, a text field, navigation, and a
prominent microphone action.

**What they need:** Evidence of what makes JARV1S useful.

Without orientation, they fall back to familiar chatbot tests such as “Hello”
or “What can you do?” This proves that the model responds but does not
demonstrate the product's value. The likely reaction is: “This appears to be
another chatbot. Where is the JARV1S part?”

### 6. Trying voice

**What they see:** “Enable microphone,” followed by wake and listening states.

**What they think:** “After granting permission, I can speak to JARV1S.”

They do not know that microphone access, wake-word detection, transcription,
and spoken replies are separate capabilities. A silently discarded voice turn
is therefore experienced as an unreliable assistant, not as a missing
transcription service.

### 7. Asking about capabilities

**What they ask:** “What can you do?”

**What they mean:** “What can you do for me, with my current setup, right now?”

Theoretical capability descriptions are taken literally. If JARV1S mentions
calendar, email, or smart-home actions before those services are connected, the
next failed request damages trust in every future capability claim.

### 8. Opening Apps

**What they expect:** Connected apps and services they can connect through a
familiar sign-in flow.

They naturally interpret:

- **My Apps** as apps they connected;
- **Enabled** as ready to use;
- **Connected** as authorized and working;
- **Degraded** as something that previously worked and is now broken.

Plugin groups, tool counts, provider metadata, and overlapping health states
force the user to infer JARV1S's architecture. An app shown as enabled,
disconnected, and degraded appears contradictory.

### 9. Opening Settings

**What they expect:** Preferences for an already working product.

**What they find:** Places where major capabilities must be assembled through
additional providers, credentials, local services, and network configuration.

The resulting mental model becomes: “JARV1S is a shell around several products
that I must connect and maintain.” Configuration now feels like prerequisite
work rather than an investment justified by demonstrated value.

## Emotional arc

The likely progression is:

1. **Excitement:** “A real personal JARV1S.”
2. **Commitment:** A large download and installation.
3. **Uncertainty:** Technical startup and provider decisions.
4. **Relief:** Setup reports success.
5. **Anticlimax:** The first workspace does not explain the value.
6. **Curiosity:** The user tries voice or asks about capabilities.
7. **Confusion:** Promised capabilities are not ready.
8. **Self-doubt:** “Did I configure it incorrectly?”
9. **Distrust:** Labels, statuses, and claims no longer feel reliable.
10. **Abandonment:** Further configuration is not worth the uncertain payoff.

## Fundamental user questions

At every point, the product must let the user form accurate answers to:

1. What can JARV1S do for me?
2. What can it do right now?
3. What must I connect to unlock something else?

The current experience does not answer these consistently. This uncertainty is
the central user pain.

## Product-user contract

The person to design for is:

> A technically curious person who wants to delegate parts of their digital
> life through one assistant, expects ordinary sign-in and permission flows,
> and assumes JARV1S will explain or absorb the remaining complexity.

They are willing to invest in configuration after experiencing value. They are
not motivated to configure an abstract inventory of possible capabilities.

The intended contract should be:

> Tell JARV1S what you want to accomplish. It either does it now or clearly
> explains and guides the one setup needed to unlock it.

## Validation questions

Future observed sessions should test these assumptions:

- What did the user expect JARV1S to do before downloading it?
- Did they expect an account, subscription, or separate AI provider?
- What did “You are ready,” “Enabled,” and “My Apps” mean to them?
- What was the first useful task they wanted to delegate?
- Did they understand why that task worked or failed?
- At what point did they feel responsible for diagnosing the product?
- Which setup effort felt justified after seeing value?
- Which concepts or provider names did they already understand?
