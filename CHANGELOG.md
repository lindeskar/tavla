# Changelog

## [1.1.0](https://github.com/lindeskar/tavla/compare/v1.0.0...v1.1.0) (2026-11-17)


### Features

* a card keeps its deleted sessions under a Deleted divider in the sessions panel, and enter (or tavla resume) brings one back under the same id ([2ee1f9c](https://github.com/lindeskar/tavla/commit/2ee1f9c5f6cbea836602bdf3d2b1c0ea1a6bc51c))
* search (/) also finds issues closed in the last 30 days and other finished issues that left the board, so a follow-up session can be started on them ([7d0e0ef](https://github.com/lindeskar/tavla/commit/7d0e0efcd6610c7b0598806cced1123e90d94326))


### Bug Fixes

* a blank line sets the Deleted divider off from the live sessions above it ([f51b8b8](https://github.com/lindeskar/tavla/commit/f51b8b843608d8c4381c3126af7b3305294b5be2))

## [1.0.0](https://github.com/lindeskar/tavla/compare/v0.1.0...v1.0.0) (2026-11-13)


### ⚠ BREAKING CHANGES

* tavla needs Python 3.14 or newer

### Features

* a session links or deletes itself with /tavla:link and /tavla:done (tavla link|rm --self), and deleting a session closes its attached iTerm tab ([e7d8822](https://github.com/lindeskar/tavla/commit/e7d882216c39f3d5fe1aa181c37396c62edc4918))


### Build System

* tavla needs Python 3.14 or newer ([2c43b18](https://github.com/lindeskar/tavla/commit/2c43b18d5ad864e207fb9c5c4cf786a32aaf60f1))

## 0.1.0 (2026-11-13)


### Features

* Tavla, a terminal kanban of GitHub issues with the Claude Code sessions working on each ([404fd81](https://github.com/lindeskar/tavla/commit/404fd816a8cb1e08d037dd02cffb290c0ee31ace))
