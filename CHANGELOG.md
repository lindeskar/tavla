# Changelog

## [1.3.0](https://github.com/lindeskar/tavla/compare/v1.2.0...v1.3.0) (2026-11-23)


### Features

* a finished session on no issue stays under Unlinked for 10 days instead of 3, and unlinked_days in config.toml sets how long ([ae3f1f2](https://github.com/lindeskar/tavla/commit/ae3f1f23489d96f09caddffb4f02b1223f9abdae))
* the keys 1-9 filter the board and the list on a parent issue, numbered on its cards and group heading; the same key or esc shows everything again ([4287cf6](https://github.com/lindeskar/tavla/commit/4287cf60bff31d3bac78f1cf80fdf9b6b078c123))
* u opens a picker with the statuses of the selected issue's project, where a number key or enter sets one; unlinking a session moves from u to U ([7eb627a](https://github.com/lindeskar/tavla/commit/7eb627adc53517eb05b58378b3fef476f55b8c34))

## [1.2.0](https://github.com/lindeskar/tavla/compare/v1.1.0...v1.2.0) (2026-11-18)


### Features

* a sub-issue's card names its parent issue in the parent's colour, search matches the parent's title and repo#N, and the list view groups by parent issue (g) ([160539d](https://github.com/lindeskar/tavla/commit/160539d6dcc914dec1ed29bf37887f11662de669))
* attaching opens a Ghostty tab when tavla runs in Ghostty, or when terminal = "ghostty" is set in the config file ([705fc5b](https://github.com/lindeskar/tavla/commit/705fc5b7c6ec29b2aeb7b4e5bc31faf216b7885b))
* the board is drawn from the cached GitHub data the moment tavla starts, and keeps its cached project statuses when GitHub can't be reached ([98581f6](https://github.com/lindeskar/tavla/commit/98581f622c09406a00512f86cef4ef23d6c36bd5))


### Bug Fixes

* cards show their parent issue on the first refresh after upgrading, not only once the cached metadata from the older version has expired ([7f9dab1](https://github.com/lindeskar/tavla/commit/7f9dab1aad3ce0633dbcdf5bac642388e6796b90))

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
