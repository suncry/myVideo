#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
# Keep signing away from Finder's live inspection of Desktop bundles.
STAGE=$(mktemp -d -t yingku-build)
trap 'rm -rf "$STAGE"' EXIT
chflags -R nohidden .venv
swiftc -emit-library native/Authentication.swift -o native/libyingku-auth.dylib -framework LocalAuthentication -framework AppKit
swiftc native/Authenticate.swift -o native/yingku-auth -framework LocalAuthentication
swiftc native/VideoDefaults.swift -o native/yingku-video-defaults -framework AppKit -framework UniformTypeIdentifiers
swiftc native/IINASession.swift -o native/yingku-iina-session -framework AppKit -framework ApplicationServices
.venv/bin/python scripts/build_icon.py
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath "$STAGE" 影库-Mac.spec
/usr/bin/chflags -R nohidden "$STAGE/影库.app"
/usr/bin/xattr -dr com.apple.FinderInfo "$STAGE/影库.app"
/usr/bin/xattr -dr com.apple.ResourceFork "$STAGE/影库.app"
/usr/bin/codesign --force --deep --sign - "$STAGE/影库.app"
/usr/bin/codesign --verify --deep --strict "$STAGE/影库.app"
mkdir -p ../运行版
# The signed archive does not acquire Finder bundle metadata during browsing.
/usr/bin/ditto -c -k --norsrc --noextattr --keepParent "$STAGE/影库.app" ../运行版/影库-Mac-v2.8.1.zip
