# History timeline and snapshots

## Scope

Device-local date navigation, current-month known recording dates, day / 6-hour / 1-hour timeline, direct clip selection and time lookup. Playback cursor follows the selected clip. Green means a verified local original still exists; amber means only catalog metadata is available. Clicking amber explicitly starts download followed by playback preparation. The existing list download button now also continues to playback after download.

The timeline reads SQLite and checks local files only; it never initiates discovery, wakes the camera or runs FFmpeg. A separate explicit catalog-update button retains that responsibility. All day markers are independent of the list's 10-item pagination. Responses are bounded to the newest 10,000 markers with an explicit truncation flag. Current-month date counts are known catalog entries, not proof of complete SD coverage.

Start timestamps remain camera-local strings. No timezone conversion is applied. Marker widths use device-reported duration as an approximate hint (positive values up to one hour); unknown duration is shown as a start marker. Minimum display width makes short clips visible, so visual width is not precise at full-day scale. Empty areas are not a guarantee that the SD card contains no recording. Seeking is clamped to the actual browser-reported file duration.

Live and replay snapshots use the current decoded browser frame and trigger a JPEG download to the viewing device. They do not record video, use the microphone, contact the camera again or create server files.

## Validation boundaries

Authenticated API tests cover device isolation, dates, full-day vs paginated lists, missing/invalid duration and private-field exclusion. Browser acceptance uses an isolated fixture database with synthetic dates and an existing camera sample, not a new camera connection. Edge playback, timeline selection, date changes, playback cursor and mobile-width overflow were checked. Snapshot generation and download initiation were observed; the browser automation download-event wait timed out, so downloaded-file persistence is not yet accepted. Actual Android/iOS playback is still unverified.

Talkback and auxiliary recordings are explicitly excluded. Thumbnail grids, battery calibration, bounded live reconnection and camera settings remain follow-up work. No server retention or transfer-budget increase is part of this change.

## Drag-to-seek revision

Playback is above the timeline. The always-visible blue cursor is a native range control supporting mouse, touch and keyboard. Input updates selection and pauses playback; release commits one seek/download operation. Gaps preserve selection and report the next known recording without silently downloading it. Previous/next-clip buttons allow explicit gap skipping. Same-file seeks reuse the loaded player. File management and backup settings are collapsed, preserving download/pin capabilities outside the main viewing flow.

GitHub synchronization is source archiving, not a prerequisite for this locally built Docker Hub release process. The server runs an already-installed compose.yaml with a digest-pinned image. compose.ops.yaml is an explicitly selected, network-disabled backup overlay with an additional backups bind mount, not a second resident backend.

## Release and preparation diagnosis (2026-11-01)

The old range handler paused on pointerdown but only resumed on change. A press/release without a value change (including dragging back to the starting value) therefore left the video paused. It also disabled/dropped new timeline selections while a job ran. The release handler now commits once per gesture regardless of value changes, handles either native change/pointerup ordering and retains the latest pending selection while busy. Cancelled gestures do not submit. Keyboard input remains supported. Pending selections are discarded on logout/date/tab changes.

Six JavaScript regression checks exercise the real production handlers in a DOM event harness, including both release orderings, unchanged release, cancel and busy selection. Run `node tests/replay_controls.cjs` from the checkout (Windows may pipe the file into Node and supply the absolute app.js path).

The screenshot-selected 06:33:37 lies inside the saved clip starting 06:32:51, whose directory duration is 106 seconds. Slow preparation at this time is not explained by a catalog gap. The original video is HEVC, 1920x1080, around 15 fps. Native FFmpeg runs within the app's existing 1 CPU/768 MiB container limits. Full-file conversion and independent output verification precede browser playback. The following single bounded 15-second encode-only samples ran on the same server/container/source (not whole-file or repeated throughput estimates): 1280x720 veryfast 6.952s; 1280x720 ultrafast 6.556s; 960x540 superfast 4.993s. These do not justify claiming an unchanged-quality large speedup, so this patch preserves the output profile and resource caps.

Conversion now reads FFmpeg progress and reports processed/total recording seconds plus elapsed waiting time. Validation remains a separate indeterminate stage. Result metadata records conversion and validation elapsed times. Existing validated caches remain reusable; originals are unchanged. Starting browser playback before the whole clip is prepared remains follow-up work, not part of this fix.
