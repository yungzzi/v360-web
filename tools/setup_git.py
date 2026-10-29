"""Install the user's repository-local Git policy; leave unrelated hooks intact."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
git_dir = Path(
    subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=ROOT, text=True).strip()
)
hooks = git_dir / "hooks"
hooks.mkdir(exist_ok=True)
for name in ["pre-commit", "pre-merge-commit"]:
    target = hooks / name
    if target.exists() and "# v360-web managed hook" not in target.read_text():
        raise RuntimeError(f"Refusing to overwrite an unrelated Git hook: {name}")
    target.write_bytes((ROOT / ".githooks" / name).read_bytes())
    target.chmod(0o755)
for key, value in [
    ("core.hooksPath", str(hooks)),
    ("merge.ff", "false"),
    ("branch.main.mergeOptions", "--no-ff"),
]:
    subprocess.run(["git", "config", key, value], cwd=ROOT, check=True)
print("Feature-branch policy installed; all merges must explicitly use --no-ff.")
