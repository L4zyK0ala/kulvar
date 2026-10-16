# Changelog

All notable changes to Kulvar are listed here. Versions follow [Semantic Versioning](https://semver.org/); while the version is 0.x, minor releases may include breaking changes, which are always called out.

## [0.2.0] - 2026-10-16

Kulvar can now log strength workouts on its own, so Hevy becomes optional. This release also adds heart-rate targets on the watch, coach memory, medications, blood value trends, morning readiness, backups and a test suite.

### Added

- **Strength without Hevy**
  - A live workout screen for the phone: exercise animation, sets pre-filled from the plan with last time's values, a rest timer with sound and vibration, and the screen kept awake.
  - Watch-first logging: Kulvar builds a draft from the Garmin recording, which you confirm in Telegram or edit.
  - A routine editor with an exercise catalog that includes free-exercise-db (876 exercises with images).
  - Progressive overload suggestions that respect the weights you have at home.
  - The full Hevy history is imported, and a "Hevy / Kulvar" switch per profile chooses where strength data comes from.
- **Strength on Garmin and Strava**
  - Planned strength days go to Garmin as workouts with exercise steps and animations.
  - Reps and weights the watch miscounted are corrected on Garmin.
  - The Strava activity from the watch gets the routine name and the exercise list, without a duplicate upload.
- **Coach**
  - Heart-rate targets in plans (`155`, `120-155`, `Z2`), using each profile's lactate threshold read from Garmin; the watch alerts outside the range.
  - Coach notes the coach keeps up to date, and proposed instruction changes you approve or reject.
  - The coach can build routines for your equipment.
- **How did it feel:** a 1–10 rating (RPE) and a note per workout. The coach sees them and the weekly session load.
- **Morning readiness:** after a poor night, Kulvar offers to make the day's hard run easy (30% shorter, Z2 cap, workout replaced on Garmin). At most one Telegram message a day.
- **Health**
  - Medications and supplements with Telegram reminders, specific weekdays, an end date worked out from the pack size, and a coach review based on your blood tests.
  - Blood value trends extracted from lab reports.
  - Several documents per lab test.
- **Reliability**
  - Nightly backups with retention, an optional second copy on another disk, and `scripts/restore_backup.py`.
  - Telegram alerts when a data source keeps failing.
  - A pytest suite run by GitHub Actions on every push.
- The app can be added to a phone's home screen (web app manifest).
- Demo data covers the new features.

### Changed

- Long Claude jobs (lab analysis, supplement review) run in the background, so they no longer hit proxy timeouts.
- Two profiles syncing at the same time no longer fail with "database is locked".
- The deprecated `datetime.utcnow()` is no longer used. The tests treat deprecation warnings as errors.

### Upgrading from 0.1.0

- No manual steps: new tables and columns are created on startup.
- New optional settings are listed in `.env.example`: `KULVAR_BACKUP_*` and `KULVAR_SYNC_ALERT_HOURS`.
- **Strava:** to get exercise lists in your Strava activities, reconnect Strava once under Settings → "Allow Kulvar to write to Strava", and turn off Hevy's own Strava upload.

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

[0.2.0]: https://github.com/L4zyK0ala/kulvar/releases/tag/v0.2.0
[0.1.0]: https://github.com/L4zyK0ala/kulvar/releases/tag/v0.1.0
