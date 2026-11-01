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

## Bounded window playback

Uncached recordings now prepare the aligned 30-second window containing the chosen offset. Input seeking happens before decoding; output duration is capped at the recording tail. Existing complete previews retain priority. Independent window names preserve concurrent clients at different positions; parent recording leases and active-file guards cover all windows. Window files share the existing preview quota and cleanup eligibility, and require the same media authentication as full previews.

The browser subtracts the window start for player seeking and adds it for camera-local timeline display. At a window end it prepares the next window automatically, stopping at the original end. First preparation is bounded to one window, but uncached window boundaries can still pause while the next window is prepared. Background prefetch and seamless concatenation are follow-up work, not implemented in this first stage. Full-resolution originals remain unchanged.

Local integration checks cover a 65-second synthetic recording: middle window 30 seconds, final window 5 seconds, invalid/out-of-range offsets, authenticated access, cache reuse, cleanup without removing originals. Edge fixture acceptance selected 12:00:58, then automatically opened window start=60 (5-second tail), readyState=4. This is synthetic acceptance; server timing and physical camera footage acceptance are separate.

## One-window lookahead

Playback's playing event requests only the next window of the same already-saved recording. A detached poll never blocks player controls or shows foreground-job progress. At the boundary the player reuses the in-flight/result promise. A low-priority prepare occupies the same single worker/operation lock, never another encoder. Duplicate prefetch is rejected. Foreground jobs cancel that worker and acquire the lock with a bounded wait; live requests also request cancellation. Leaving playback requests cancellation, and abandoned network requests remain bounded to one window and the existing preparation deadline. A new selected position replaces the queued intent; waiting for prefetch does not force preparation of obsolete intent.

Cache, source integrity verification, image quality and CPU/memory caps are unchanged. Preparing a window before the old one ends reduces boundary waiting but is not gapless playback: file/player replacement still has loading overhead, and a late seek or a busy server can outpace preparation.

Acceptance: backend worker checks cover duplicate prefetch and foreground cancellation; local Edge fixture progressed from offset 28 to window start=30 at readyState=4 and showed next-window readiness. Physical mobile and real-camera footage acceptance must be reported separately.

## Half-hour ready archive and 15 GiB combined budget

Server mode checks recent SD metadata every 30 minutes. The previous daily transfer cap is removed for unlimited mobile data, while the local lightweight profile retains its limits. Each server cycle remains bounded to 10 transfer attempts / 64 MiB, 10 local preparation attempts and a cooperative five-minute work window. No accelerated ten-minute backlog wakeups remain; foreground interruptions retry on the normal interval. Individual SDK/media operation deadlines and retry backoff remain in force. Battery readings are not used as a guessed low-battery cutoff: calibration is still pending, and no additional connection is made just to read battery.

After bounded downloads close their camera sessions, the same background worker prepares full browser-compatible files, newest saved originals first. Existing saved originals are not downloaded again. Failed preparation has its own backoff. Preparation is local work but still cooperatively yields to foreground viewing. Fifteen GiB is a combined limit for originals, full/window previews and live cache, not fifteen GiB per folder. Under pressure the oldest eligible recording and all its browser files are evicted together. Pins, leases, active responses and foreign files remain protected; impossible admission fails before deleting eligible originals. The 8 GiB system free-space reserve remains. Pair cleanup prioritizes device recording time so historical backfill cannot displace newer footage merely because it was downloaded later.

The default timeline shows complete browser-ready recordings. The alternate all-known view permits explicitly selecting recent or unprepared device entries. Releasing in a gap snaps to the nearest real interval and starts that clip; a tie selects the later clip. Readiness count, oldest ready timestamp and span are exposed; span is not a promise of a complete SD backup or continuous recording. The 15 GiB budget is estimated to cover roughly 14-16 days at two observed daily workloads, not a guarantee under increased activity. Source and browser quality remain unchanged.

Local checks: combined quota/pair eviction/pin preservation, preparation without repeat downloads even after historical daily usage, bounded failed preparations, half-hour scheduling and snapping tests. Edge synthetic fixture snapped 12:03:00 to 12:00:00 and played a pre-generated full preview. Cloud camera acceptance must be recorded separately.

## Forward-only gap snapping and sparse timeline explanation

When the selected second is inside a recording, preserve the exact position. Otherwise choose the first later interval in the current source filter. No later interval means pause and show an end-of-day message; never fall back to a preceding clip. The date chip explicitly labels its number as discovered entries, and the timeline heading gives ready vs discovered counts in the ready-only view.

Read-only production diagnosis on 2026-11-01: today's 231 discovered entries included 91 originals and 27 complete browser previews; the ready-only timeline therefore showed 27 intervals. All dates combined: 463 originals, 31 ready, 186 pending including 13 failures. The previous half-hour cycle saved 10 and prepared 10 successfully. Those numbers are a point-in-time observation, not future completeness or a next-day catch-up promise. Existing originals can be prepared without new camera downloads; older SD files can be backfilled while they remain on the device. Missing/invalid catalog entries and overwritten/unrecorded footage remain distinct limitations.
