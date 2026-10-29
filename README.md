# V360 Web

A planned web client for a user-owned V360 Pro 4G camera.

## Workspace

- Development and runtime: WSL Ubuntu.
- Project root: `/home/yungzzi/workspace/04_tools-and-scripts/v360-web`.
- APK inputs and existing reverse-engineering reports remain in the Windows `inverse V360 Pro` analysis directory. They have not been moved or copied here.

## Planned scope

- Authenticated web access, on-demand live view, and PTZ control.
- Synchronization of recordings already saved on the camera SD card.
- A replaceable PPCS transport adapter.
- Media inspection and processing, recording index, retry, and retention.

## Current status

Only this workspace and README have been created. No application scaffold or device connection is implemented yet.
Static APK analysis exists separately. Actual 4G connection, normal device authentication, wake behavior, and recording retrieval remain to be validated.

Remote operation is a project constraint: routine development should not require physical access to the camera or technical work by family members. Existing camera settings should be preserved during initial verification.
