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
