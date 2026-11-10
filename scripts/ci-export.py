#!/usr/bin/env python3
"""Trusted default-branch entry point for a user-selected source server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nifi_deploy.ci import run_export


if __name__ == '__main__':
    try:
        run_export()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
