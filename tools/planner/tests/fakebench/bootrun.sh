#!/bin/bash
# fake launcher for the hunt.py tests: no ssh, no bench
exec python3 "$(dirname "$0")/fakebench.py" bootrun "$@"
