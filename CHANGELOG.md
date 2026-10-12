# Changelog

All notable changes to Kulvar are listed here. Versions follow [Semantic Versioning](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, which are always called out.

## [0.1.0] - 2026-10-12

First public release.

### Added

- Dashboard combining Garmin, Strava, Hevy and MyFitnessPal, with Garmin/Strava duplicates merged
- Multiple profiles on one server, each with its own accounts, goals, coach and Telegram bot
- Weekly target tracking (default: 3 runs including a Saturday long run, plus 2 strength sessions) and a race calendar with build, taper, race-week and recovery phases
- Optional AI coach on the Claude API, with a monthly budget and per-message cost tracking; suggested workouts can be pushed to the Garmin calendar
- Telegram bot per profile: two-way chat with the coach, done / skipped / move-to-tomorrow buttons, a 21:00 evening check and a Sunday plan for the next week
- Health records: check-up reminders, lab document analysis, and doctor's notes that override the coach's rules
- Activity details (route map, heart-rate and elevation charts, splits, aerobic decoupling), Hevy workouts, body measurement charts and MyFitnessPal nutrition
- English and Turkish UI, chosen per profile; light and dark mode; mobile layout
- Docker image for amd64 and arm64, published to `ghcr.io/l4zyk0ala/kulvar`
- Demo data generator (`scripts/seed_demo.py`) and translation checker (`scripts/check_i18n.py`)

[0.1.0]: https://github.com/L4zyK0ala/kulvar/releases/tag/v0.1.0
