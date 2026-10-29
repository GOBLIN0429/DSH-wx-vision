#!/usr/bin/env bash
# Build the Swift helpers into bin/.
#
# They are plain command-line binaries (no app bundle), so every one of them
# needs a module cache it can write to. The cache lives inside the repo rather
# than the system temp dir because the DSH file sandbox may refuse writes there.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/src"
BIN="$HERE/bin"
CACHE="$HERE/.build-cache"

mkdir -p "$BIN" "$CACHE"

TOOLS=(wxwatch wxtool wxgeom ocrjson winlist wpid activate)

for tool in "${TOOLS[@]}"; do
  if [[ ! -f "$SRC/$tool.swift" ]]; then
    printf 'missing source: %s\n' "$SRC/$tool.swift" >&2
    exit 1
  fi
  printf 'building %s ...\n' "$tool"
  swiftc -O -module-cache-path "$CACHE" -o "$BIN/$tool" "$SRC/$tool.swift"
done

chmod +x "$BIN"/*
printf '\nbuilt into %s:\n' "$BIN"
ls -1 "$BIN"
