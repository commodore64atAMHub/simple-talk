#!/usr/bin/env python3
"""Compiled/binary entry point for the simple-talk server."""

import sys

from stserver.__main__ import main

if __name__ == "__main__":
    sys.exit(main())