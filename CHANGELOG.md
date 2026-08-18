# Changelog

All notable changes to GridConnect are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-08-18

First packaged release. The project is renamed from *Arlec Plug Monitor* to **GridConnect**
so that the app, the container, the database and the environment variables all agree.

### Fixed

- **Editing a plug always failed.** The edit dialog posted the record's `id` back in the
  PATCH body, which the strict request model rejected with `422 Unprocessable Entity`.
  Saving any change to a plug was impossible. The model now accepts and ignores `id`, and
  the dashboard no longer sends it.
- **The plug picker reset every five seconds.** Rebuilding the option list on each refresh
  discarded the user's selection in the timer and schedule forms, so a slow typist would
  submit against the wrong plug.
- **Live load counted plugs that were offline.** The headline figure summed each device's
  most recent stored reading, including plugs that had dropped off Wi-Fi hours earlier.
  It now counts only plugs that are online with a fresh reading, and last-known values are
  labelled as such in the plug list.
- **Hourly roll-ups discarded earlier energy.** `archive_readings` overwrote an existing
  bucket rather than adding to it, so a late sample landing in an already-archived hour
  destroyed that hour's recorded energy. Buckets now accumulate, with a sample-weighted
  average for power.
- **A single background failure stopped all polling.** An unhandled exception in the
  schedule, timer, poll or archive step killed the loop task silently, and the service kept
  serving a frozen dashboard until restart. Each step is now isolated, logged, and surfaced
  as a *Service problem* alert.
- **Schedules could be skipped entirely.** A schedule fired only on an exact minute match,
  so a slow poll cycle or a brief restart across that minute lost the occurrence. Schedules
  now fire within a bounded catch-up window without replaying much older occurrences.
- **Non-Docker installs crashed on startup.** `tzdata` was missing from `requirements.txt`,
  so any host without a system zoneinfo database (Windows, slim images) failed every
  timezone lookup. It is now a pinned dependency.
- `host.docker.internal` is now mapped via `extra_hosts` so relay hosts resolve on Linux
  Docker, not only Docker Desktop.

### Added

- **DPS channel inspector.** `POST /api/devices/{id}/dps` reads a plug's live DPS map; the
  dashboard shows every raw channel with a guess at its meaning and applies a new mapping in
  one step. This is the fix for a metering plug that reports `0 W` under real load.
- **Voltage and current DPS and scales are editable in the UI.** Previously only the switch,
  watts and energy channels were exposed, so a mis-scaled voltage could not be corrected
  from the dashboard at all.
- **Sustained-load alerting** for a plug held near its rating for a whole window — the
  pattern that overheats a plug, as distinct from a momentary peak.
- **Supply-voltage alerting** against a configurable nominal voltage and tolerance.
- **Monthly budget alerts.** Budgets could be set and were displayed, but were never
  enforced; they now raise and clear an alert.
- Power (W) chart mode alongside energy (kWh), plus x-axis time labels, nice-number axis
  scaling and a screen-reader-friendly data table.
- Light and dark themes following the system setting, with a manual override.
- Weekday chips for schedules, and a form-based peak/off-peak editor replacing the raw JSON
  textarea.
- Settings split into General, Plugs, Automation and Data panels.
- Non-blocking toasts and a typed-confirmation dialog in place of `alert`/`prompt`/`confirm`.
- PWA icon and a complete web manifest; the service worker no longer caches API responses
  and cleans up superseded caches.
- Container `HEALTHCHECK`, image labels, and a `/api/health` `version` field.
- `tests/test_api.py` covering the HTTP contract the dashboard depends on.

### Changed

- `GET /api/health` reports `"service": "gridconnect"` (previously `"arlec-plug-monitor"`).
  **Update any uptime monitor that matches on this string.**
- CSV export is now named `gridconnect-trends.csv`, backups `gridconnect-backup.db`.
- The dashboard keeps the last good data on screen during a connection failure and backs off
  its retries, instead of replacing the device list with an error string.
- Polling pauses while the browser tab is hidden.
