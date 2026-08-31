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

[unreleased]: https://github.com/GEM-Industries/JARV1S/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/GEM-Industries/JARV1S/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/GEM-Industries/JARV1S/releases/tag/v0.5.0
