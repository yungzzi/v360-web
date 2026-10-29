"""Run the stdlib-only USB client from a checkout (also usable on Windows Python 3.10+).

The HTTP server requires the project's installed Python dependencies.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from v360_web.cli import main

raise SystemExit(main())
