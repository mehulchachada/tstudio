"""Entry point so `python -m lightbox` works."""

import sys

from lightbox.cli import main

if __name__ == "__main__":
    sys.exit(main())
