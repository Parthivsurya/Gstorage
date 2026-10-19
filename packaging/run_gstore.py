"""Frozen-app entry point for PyInstaller.

Kept separate from ``gstore.__main__`` so PyInstaller has a plain script to
analyze (no ``if __name__ == '__main__'`` guard to trip over).
"""

import sys

from gstore.app import main

if __name__ == "__main__":
    sys.exit(main())
