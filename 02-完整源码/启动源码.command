#!/bin/zsh
set -eu
cd "$(dirname "$0")"
exec .venv/bin/python desktop.py
