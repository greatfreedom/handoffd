# Changelog

## 0.1.0 — first public preparation (unreleased)

First release of handoffd as a Claude Code plugin. macOS + Warp only, experimental. Runtime messages are Chinese.

### Origin and how much of the original code is kept
The code is the author's own handoffd MVP, taken verbatim as the first import commit `6f60537` (from the author's local research repository, commit `e3e2fcd`; 82 files, 14,954 added lines). Everything after that is deletions and small edits. Measured with `git diff --numstat 6f60537 HEAD` (tree at the time of the docs commit `76ff842`, `prep/` excluded):

| Area | Original lines | Removed or changed (lines deleted) | Kept unchanged |
|---|---|---|---|
| `cdm/` (daemon, hooks, state machine) | 5,267 | 6 | 99.9% |
| `bin/` | 250 | 2 | 99.2% |
| `tools/` | 65 | 0 | 100% |
| `tests/` | 8,810 | 24 | 99.7% |
| whole import (incl. docs) | 14,954 | 478 (386 of them are the deleted `BUILD_REPORT.md`) | 96.8% (99.4% without `BUILD_REPORT.md`) |

Added after the import: 904 lines in total, of which 424 are the verbatim PoC baselines in `tests/fixtures/poc/`, about 100 are the new tests `X1–X3`, and the rest are the plugin files, `LICENSE`, README and the rewritten `assets/voice/README.md`.

### Removed from the import (must not be redistributed)
- `assets/tones/*` (cut from Apple's built-in ringtones), all `assets/voice/**/*.mp3` (generated from superseded phrases), `BUILD_REPORT.md` (internal build log).

### Changed
- `required_env` defaults to `[]` (was a personal API-key variable that blocked B's window on other machines). Test `X1`.
- `ListAgents` is a read-only tool for B and read-only sessions, so B can find A's session name. Test `X2`.
- Default alerts use the macOS system sounds Glass and Ping and no voice clips (product default change; old default assertions were updated accordingly, none loosened). Test `X3`; rotation/voice tests use placeholder files in `tests/fixtures/audio/`.
- Tests `F1/F3/F4/F5` now read the PoC single-lock baselines from `tests/fixtures/poc/` instead of a path outside the repository.
- `statusline_tap.sh` finds `node` with `command -v`; README/voice README no longer contain machine-specific paths.

### Added
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `hooks/hooks.json` (11 events, exec form), `skills/handoffd/SKILL.md`.
- `LICENSE` (MIT), bilingual `README.md` (kept within the existing 150-line check H72), `docs/INSTALL.md` (full install, uninstall, known limitations), this file.
