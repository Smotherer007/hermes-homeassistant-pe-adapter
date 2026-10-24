"""`python -m hermes_pe` runs the adapter with the same flags as the module itself."""

import asyncio
import sys

from .bridge import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
