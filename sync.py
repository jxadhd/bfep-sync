#!/usr/bin/env python3
"""Drop-in replacement for the original sync.py entry point.

Same flags (--apply, --limit, --after) plus --provider and --retries.
"""
import sys

from bfep_sync.__main__ import main

sys.exit(main())
