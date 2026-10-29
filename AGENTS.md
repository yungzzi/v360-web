# Repository instructions

## Git workflow (explicit user requirement)

- Manage this project with Git.
- Create a new feature branch for development. Never make ordinary implementation commits on `main`.
- Merge a feature into `main` only after its stated scope is complete and the relevant checks pass.
- Every merge must explicitly use `git merge --no-ff`. Preserve the merge commit.
- Keep incomplete work on its feature branch and explain what remains.

## Project boundaries

- Canonical checkout: `/home/yungzzi/workspace/04_tools-and-scripts/v360-web` in WSL Ubuntu.
- The original APK and REA analysis evidence remain in the Windows V360 analysis directory.
- Keep APKs, proprietary SDK binaries, signing keys, tokens, device credentials and runtime data out of Git. Use `.private/`, `.tools/`, or `data/` as appropriate.
- Distinguish static source evidence, automated software checks and actual camera acceptance. Simulated bridge checks are not camera acceptance.
- The user is away from the camera. Routine development must not require physical camera access or technical work by family members.
- Initial remote validation should preserve camera configuration and release sessions after use.

## User-facing preferences

- Use a light interface as the default; the user dislikes the dark theme.
- Explain progress through functions and observable results. Minimize audio/video jargon;
  explain a necessary term briefly instead of assuming specialist knowledge.
- Battery level and charging status are requested future features. Avoid frequent new
  camera connections just to refresh battery readings on this solar/battery device.
- Clearly distinguish stopping viewing, logging out, and shutting down the local backend.
