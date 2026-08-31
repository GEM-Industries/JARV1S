---
name: release-notes
description: >-
  Write and freeze JARV1S user-facing changelog entries and GitHub Release
  notes from CHANGELOG.md. Use when bumping a version, publishing or promoting
  a desktop release, editing GitHub Releases, or when the user mentions
  changelog, release notes, or CHANGELOG.md.
---

# Release Notes

`CHANGELOG.md` at the repo root is the record. GitHub Release bodies are a copy of that version's section plus install/SHA-256. Do not keep a second notes file.

## Audience

Write for someone opening the Mac app or a GitHub Release. Not git logs, graphify, lockfiles, CI, or internals.

## How to phrase

Lead with what they would notice, then why if it is not obvious. “You can change an alarm by name” not `replace_alert`. “Calendar on your Mac” not EventKit. One notable difference per bullet. Skip signing/CI unless the app would not run or update without it.

## Format

Keep a Changelog 1.1.0. Latest version first. ISO dates. Only non-empty `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`. 5–12 user-visible bullets per version. Imperative or present tense is fine; be consistent within a section.

## When to write

- Notable product work: add a bullet under `[Unreleased]`.
- Version bump (required): move `[Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD`, leave an empty `[Unreleased]`, update compare links. If Unreleased is empty, draft from `git log <prev-tag>..HEAD` and rewrite — never paste the log.
- `task desktop:release:publish` / `promote` fail if `CHANGELOG.md` has no `## [version]` section. Fix the file; do not pass `--notes` around it.

## GitHub body

Private: `Private beta (invite-only).` then the changelog section, then the install footer (DMG, not the Source code zip/tar GitHub auto-attaches). Public: the same without the beta line. Assets stay the DMG (plus private updater channel). Do not upload a source archive.

## Skip

git-cliff, semantic-release, conventional-commit enforcement, updater `latest.json` notes (install is silent today).
