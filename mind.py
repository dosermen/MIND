#!/usr/bin/env python3
"""Compatibility entry point for the MIND package."""
import sys

from mindlib import *
from mindlib import (_as_list, _lossy_fm, _mem_contract,
                     _mcp_dispatch, _mcp_tools, _resolve_mem_paths,
                     _run_sync, _split_top, _veto_settings)
from mindlib.cli import main

if __name__ == "__main__":
    sys.exit(main())
