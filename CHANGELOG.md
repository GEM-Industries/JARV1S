# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Replace or cancel an alarm or reminder by name or clock time instead of requiring an internal id.
- Saying "another" or "as well" still adds a new alarm rather than overwriting one.

## [0.6.0] - 2026-08-31

### Added

- Agent Home: a persistent identity and working style for JARV1S.
- Native macOS calendar for events and scheduling.
- Pair speakers to rooms so replies can land in the right place.
- Named work you can pick back up by title after a long task.
- Pair a remote satellite speaker from the Host.

### Changed

- Follow-up questions only when the enrolled owner's voice matches.
- Unsolicited JARV1S speech is separate from a reply in conversation.

## [0.5.0] - 2026-08-18

### Added

- Structured capability calls, so actions JARV1S takes have clearer results.
- Public AGPL source on GEM-Industries, promoted from a sanitized snapshot.

## 0.4.0 - 2026-08-09

### Added

- Home Assistant product setup from the app.
- Voice admission, so only allowed voices can talk to JARV1S.

## 0.3.0 - 2026-08-02

### Changed

- Shared holographic look across startup, onboarding, and the main stage.

### Fixed

- Local Ollama models run inside the signed app.

## 0.2.4 - 2026-07-24

### Fixed

- More reliable packaged runtime and conversation turns.
- Updater metadata so installed apps can find this version.

## 0.2.3 - 2026-07-23

### Fixed

- Cleaner app, tray, and web icons without a baked-in shadow.

## 0.2.2 - 2026-07-23

### Added

- JARV1S app icon.

## 0.2.1 - 2026-07-22

### Added

- Wake-phrase check.
- Call-aware audio, so JARV1S does not talk over a phone call.

## 0.2.0 - 2026-07-21

### Added

- Maps location with GPS context.

### Changed

- Direct conversation turns.
- Packaged Host no longer needs Redis.

## 0.1.2 - 2026-07-17

### Fixed

- More reliable first-run onboarding and app startup.

## 0.1.1 - 2026-07-17

### Added

- Phone companion, so you can use JARV1S from your phone.

## 0.1.0 - 2026-07-16

### Added

- First packaged macOS Host for Apple Silicon.
- Wake-word enrollment.
- Reach other household devices from the Host.

[unreleased]: https://github.com/GEM-Industries/JARV1S/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/GEM-Industries/JARV1S/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/GEM-Industries/JARV1S/releases/tag/v0.5.0
