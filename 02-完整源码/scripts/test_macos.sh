#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
chflags -R nohidden .venv
TEST_DATA=$(mktemp -d -t yingku-tests)
trap 'rm -rf "$TEST_DATA"' EXIT
export YINGKU_DATA_DIR="$TEST_DATA/data"
export YINGKU_SETTINGS_PATH="$TEST_DATA/ui.ini"
export YINGKU_DISABLE_STARTUP_TASKS=1
export QT_QPA_PLATFORM="offscreen:configfile=tests/offscreen-screen.json"
.venv/bin/python -m compileall -q app.py desktop.py maintenance.py maintenance_ui.py mac_smoke.py privacy.py privacy_ui.py background_tasks.py actor_search.py scrolling.py player.py iina_cleanup.py discovery.py public_sources.py insights_ui.py actor_media.py media_ui.py extra_sources.py
.venv/bin/python -m unittest discover -s tests -v
