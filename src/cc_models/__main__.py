"""`python -m cc_models` entrypoint — same CLI as the `cc-models` script.

Guarded so importing this module never executes main() (an unguarded
``raise SystemExit(main())`` launches the TUI on bare ``import``).
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
