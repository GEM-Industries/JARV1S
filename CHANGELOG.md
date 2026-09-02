# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.2] - 2026-09-03

### Fixed

- After you enroll your voice, other people talking nearby no longer keep JARV1S listening. The 4-second wait is for your voice only. If you start talking in that window, JARV1S keeps listening until you finish.

### Removed

- Timed jobs go through reminders, alarms, and deferred instructions. The extra Rules hatch is gone.

## [0.6.1] - 2026-09-02

### Changed

- You can change or cancel an alarm or reminder by saying its name or time (“move my wake-up alarm”, “cancel the bins reminder”) instead of an id.
- “Set another alarm as well” still adds a new alarm. It does not overwrite the one you already have.
- Changing a repeating alarm or reminder without saying “from now on” or “all of them” only changes the next one.
- When JARV1S asks to confirm something, saying yes or no does it (or cancels it) right away.
- You can change or delete an event automation (new email, calendar heads-up) by name.
- Spoken replies use a slightly more distant tone.

### Fixed

- Remote access through Funnel keeps webhook paths, so Gmail and phone events still reach JARV1S.
- A Gmail automation only fires for mail that arrives while JARV1S is watching. If it was off, it does not replay the inbox.

## [0.6.0] - 2026-08-31

### Added

- Agent Home: you can write who JARV1S is and how it should talk, and it keeps that.
- Connect Gmail with Google sign-in in the app. Official Mac builds include the JARV1S Google app, so after you are invited as a tester you tap Connect instead of pasting a client ID.
- JARV1S can use the Calendar app on your Mac, not only Google or Outlook.
- You can put a speaker in a room so JARV1S answers from that room.
- Background work (for example a coding job) keeps a name, so you can come back to it later (“keep going on the checkout PR”).
- You can pair a satellite speaker that is not next to the Mac.

### Changed

- After you enroll your voice, a follow-up only counts if it is you. Other voices do not continue the conversation.
- When JARV1S speaks on its own (a reminder or a notice), that shows separately from a reply to you.

## [0.5.0] - 2026-08-18

### Added

- When JARV1S does something for you, the transcript shows a short receipt of what it did.
- Source is public on GEM-Industries under AGPL.

## 0.4.0 - 2026-08-09

### Added

- Connect Home Assistant from the app: find it on the network and sign in.
- Enroll your voice so JARV1S can tell it is you, and only treat your speech as a turn.

## 0.3.0 - 2026-08-02

### Added

- JARV1S can listen on this Mac (Apple Speech) and speak with a local voice, without a cloud speech account.
- A live stage while you talk, with the same look as the rest of the app.
- The Mac can share its location, so Maps can use where you are.
- Home: household rooms and devices in one place.

### Changed

- Startup and first-run setup match that look.

### Fixed

- A local Ollama model can run inside the signed Mac app.

## 0.2.4 - 2026-07-24

### Fixed

- The packaged Mac app starts and holds a conversation more reliably.
- JARV1S is more careful about files and commands on your Mac.
- Installed copies can find this version when they check for updates.

## 0.2.3 - 2026-07-23

### Fixed

- Dock, menu bar, and website icons are the same JARV1S mark, without a fake Mac shadow.

## 0.2.2 - 2026-07-23

### Added

- JARV1S icon in the Dock.

## 0.2.1 - 2026-07-22

### Added

- In Settings, you can test whether saying “Jarvis” is heard, without starting a conversation.
- The mic pauses while you are on a phone or conference call, so JARV1S does not talk over it.

## 0.2.0 - 2026-07-21

### Added

- JARV1S can look up places and use your location.

### Changed

- Replies go through one model path, so a turn is simpler.
- The Mac app no longer needs Redis.

## 0.1.2 - 2026-07-17

### Fixed

- First-run setup and Host startup are more reliable (services, credentials, and voice settings).

## 0.1.1 - 2026-07-17

### Added

- Use JARV1S from your phone, with a layout meant for a phone screen.

## 0.1.0 - 2026-07-16

### Added

- First Mac app you can install (Apple Silicon).
- Teach JARV1S your wake word.
- Reach other devices in the house from the Host.

[unreleased]: https://github.com/GEM-Industries/JARV1S/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/GEM-Industries/JARV1S/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/GEM-Industries/JARV1S/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/GEM-Industries/JARV1S/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/GEM-Industries/JARV1S/releases/tag/v0.5.0
