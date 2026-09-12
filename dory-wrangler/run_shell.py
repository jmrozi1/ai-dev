#!/usr/bin/env python3
"""Entry point for the Dory-wrangler chat shell.

`dory-wrangler` is not a legal Python package name, so this bootstraps `src/`
onto the path and hands over. Standard library only; there is nothing to install.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dory_wrangler.serve import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
