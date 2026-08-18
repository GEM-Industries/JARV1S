# Ideal JARV1S Onboarding Experience

**Status:** Product direction  
**Related:** [User Mental Model](../research/JARVIS_USER_MENTAL_MODEL.md) · [Current Experience Review](../research/ONBOARDING_EXPERIENCE_REVIEW.md)

## Goal

Get the user into one useful, personal interaction with JARV1S as quickly as
possible without explaining the whole product.

The user should feel that they are meeting an assistant, not configuring an AI
platform.

## Experience in one sentence

JARV1S wakes up, invites the user to speak, learns what matters through a short
conversation, completes one useful action, and reveals additional capabilities
only when they become relevant.

## Product assumption

JARV1S must own a working baseline conversation stack.

The user should not need to choose an LLM, STT provider, TTS provider, model, or
API key before the first conversation. The baseline may ultimately be bundled,
managed, or selected automatically, but that is a product implementation
decision rather than an onboarding decision.

JARV1S should select the best available processing automatically and clearly
tell the user when a request needs information to leave the Mac.

Provider selection and custom local runtimes remain available later for users
who want control.

## The experience

### 1. JARV1S wakes up

The first launch opens with a brief technical boot sequence inspired by Iron
Man.

It exists to establish identity and anticipation, not to expose diagnostics.

Show a small number of truthful system events:

```text
INITIALIZING CORE
SECURE MEMORY AVAILABLE
CONVERSATION SYSTEM READY
JARV1S ONLINE
```

The sequence should:

- reflect real startup progress;
- take no longer than the actual wait;
- remain visually calm and readable;
- provide **Details** for technical diagnostics;
- transition directly into the first interaction.

Do not show raw logs, internal service names, repeated states, or technical
errors on the primary screen.

Never delay a ready product to finish the animation. If real startup takes more
than 10 seconds, show meaningful progress instead of extending the ceremony.

If startup fails, leave the theatrical mode and state plainly:

> JARV1S could not finish starting. Restart JARV1S or view details.

Returning launches should be nearly immediate. The full ceremony belongs to
the first meeting, not every app open.

### 2. The user chooses to begin

After startup, the interface becomes quiet.

```text
JARV1S is ready.

[ Start with voice ]
  Start with text
```

JARV1S does not speak automatically. The user chooses the moment and
interaction mode.

Selecting **Start with voice** explains the immediate purpose before macOS requests
permission:

> Talk naturally with JARV1S. Your microphone is active only while JARV1S is
> listening. JARV1S chooses the best available processing and tells you when
> information needs to leave this Mac.

```text
[ Continue ]
  Use text instead
```

Only then request microphone permission.

If permission is declined or voice is unavailable, continue in text without
restarting or presenting an error flow.

### 3. The first conversation personalizes JARV1S

The first conversation replaces a profile form.

JARV1S begins with a brief identity and capability frame:

> Good evening. I'm JARV1S, your AI assistant on this Mac. I can help you
> think, remember, and take action. When something needs another app, I'll ask
> before connecting it.

Use information already available from the Mac rather than asking the user to
repeat it:

> Should I call you Geoff?

Then move directly to intent:

> What would make today easier?

The interface offers a few dependable starting points without reading out a
feature list:

```text
[ Plan something ]
[ Remember something ]
[ Remind me later ]

Or tell me in your own words.
```

The conversation should:

- ask one question at a time;
- accept short, long, or uncertain answers;
- use the user's own words when responding;
- extract useful profile facts without exposing fields;
- allow **Skip for now** at every point;
- avoid asking about personality, occupation, interests, or communication
  preferences unless they are volunteered;
- infer preferences gradually through normal use.

JARV1S should become personal through the relationship, not through an
onboarding questionnaire. When JARV1S stores a durable personal fact, show what
was remembered and offer **Change** and **Forget**.

### 4. JARV1S creates the first useful outcome

The user's answer determines the next step.

If the requested outcome is ready, JARV1S completes the smallest safe,
meaningful action. Examples include:

- creating a reminder;
- remembering a preference;
- answering with live context;
- helping structure the user's day;
- drafting a short plan;
- preparing an action for confirmation.

If the outcome needs an unconnected service, JARV1S does not fail or redirect
the user into Settings. First complete any useful part that does not require the
connection:

> I can help structure your day now. To include your existing events, I need
> access to your calendar.

```text
[ Connect calendar ]
  Not now
```

The connection screen explains:

- what connecting enables;
- what information JARV1S can access;
- who provides the connection;
- whether an account or cost is involved.

It then offers one familiar sign-in or permission action. Technical provider
details remain secondary.

If the user chooses **Not now**, JARV1S should still provide immediate value
without the integration where possible.

Every completed action shows a concise, reversible receipt:

```text
Reminder set for tomorrow at 8:00 AM.

[ Change ] [ Undo ]
```

### 5. The workspace emerges from the conversation

There is no separate “setup complete” screen.

JARV1S provides a brief completion beat:

> We're set. I'll learn how you work as we go, and you can review what I
> remember at any time.

The conversation then transitions naturally into the normal workspace. It
retains the first useful result and offers at most two relevant next actions:

```text
What would you like to do next?

[ Plan tomorrow ]
[ Add another reminder ]
```

Suggestions come from:

- what the user said;
- what is ready now;
- what they just completed.

They must not be a generic feature list.

## Discovery after onboarding

JARV1S should reveal depth gradually through use.

Wonder should come from competence, continuity, and relevant discoveries—not
from uncertainty about readiness, privacy, memory, or whether an action worked.

### Let the user discover

- that JARV1S remembers useful context;
- that follow-up requests can refine previous work;
- that repeated actions can become routines;
- that connected services expand what JARV1S can do;
- that proactive assistance can be enabled when it becomes valuable;
- that the assistant can be shaped through corrections and preferences.

### Explain at the moment of need

- why a permission is required;
- why a requested capability is unavailable;
- what connecting a service unlocks;
- what data leaves the Mac;
- what JARV1S has chosen to remember and how to remove it;
- whether a provider requires an account or payment;
- what consequential action JARV1S is about to take.

### Keep out of the primary experience

- model identifiers and context windows;
- STT and TTS terminology;
- plugin state and tool counts;
- Composio and integration topology;
- Host, database, sidecar, and runtime terminology;
- health probes and raw diagnostics;
- remote-access infrastructure;
- exhaustive capability inventories.

These can exist under advanced settings and diagnostics.

## How additional capabilities are introduced

Additional setup begins from intent, not from a checklist.

The pattern is always:

1. The user asks for an outcome.
2. JARV1S checks live capability readiness.
3. If ready, JARV1S performs the action.
4. If unavailable, JARV1S explains the single missing connection.
5. The user connects it or continues without it.
6. JARV1S verifies the capability end to end and resumes the original request.

Examples:

- “What is on my calendar?” → connect calendar.
- “Play my focus playlist.” → connect music.
- “Turn off the lights.” → connect an existing smart home.
- “Speak your replies.” → choose a voice.
- “Use this from my phone.” → set up remote access and pairing.

A secondary **Connections** area can support deliberate exploration. It should
group capabilities by outcome and show:

- **Ready**
- **Available to connect**
- **Needs attention**

It should not use loaded plugins as the definition of “My Apps.”

## When JARV1S is allowed to claim a capability

Capability descriptions must reflect the user's live setup.

When asked “What can you do?”, JARV1S should answer in this order:

1. A few useful things available now.
2. Possibilities relevant to the user's expressed goals.
3. One optional connection that would unlock the most value.

JARV1S must not describe an installed namespace as a working capability.

## Experience invariants

- **Ready** means a real interaction can complete end to end.
- A visible primary action works now.
- Permissions follow explicit user intent.
- Text is always available as a fallback.
- No required setup exists without a clear explanation of its value.
- No information is requested merely because it may be useful later.
- JARV1S never silently drops an attempted action.
- Completed actions are inspectable and reversible where possible.
- Stored personal facts are visible and removable.
- Technical theatre can stylize truthful state but cannot hide failure.
- The user can leave onboarding and continue later.
- The first session demonstrates value before presenting breadth.

## What this is not

This is not:

- a product tour;
- a feature carousel;
- a full profile questionnaire;
- a provider-selection wizard;
- a capability checklist;
- a mandatory voice flow;
- an attempt to connect every service on the first day.

## Validation

Test the experience through observed clean-install sessions before rebuilding
the wider settings experience.

The first-run experience succeeds when:

- the user reaches the invitation without interpreting startup as a failure;
- the first voice or text exchange works without technical configuration;
- the user completes or receives one useful outcome;
- the user can inspect or undo the first action;
- microphone denial or voice failure falls back cleanly to text;
- the user can explain what JARV1S can do now;
- the user understands why an additional connection is being requested;
- the user does not need to visit Settings to complete the first useful task.

Useful measures:

- time to first successful exchange;
- time to first useful outcome;
- onboarding abandonment;
- microphone permission acceptance and recovery;
- contextual connection completion;
- capability-claim failures;
- return rate after the first useful outcome.

## Research basis

This direction follows established onboarding principles:

- [Apple Onboarding](https://developer.apple.com/design/human-interface-guidelines/onboarding):
  keep onboarding fast, optional, interactive, and based on reasonable
  defaults.
- [Apple Privacy](https://developer.apple.com/design/human-interface-guidelines/privacy):
  request protected access at the moment the user chooses the related feature.
- [Apple design foundations](https://developer.apple.com/videos/play/wwdc2025/359/):
  show only what is necessary to begin and reveal depth progressively.
- [AirPods setup](https://support.apple.com/en-gb/104989): use one obvious
  connection action and keep manual complexity behind a fallback.
- [Nielsen Norman Group on contextual help](https://www.nngroup.com/articles/onboarding-tutorials/):
  teach in context rather than pushing an upfront tutorial.
- [Nielsen Norman Group on empty states](https://www.nngroup.com/articles/empty-state-interface-design/):
  use empty space to create confidence and guide the first action.
- [ChatGPT Voice](https://help.openai.com/en/articles/8400625-voice-chat-faq):
  enter voice explicitly, request microphone access, and begin talking
  immediately.
