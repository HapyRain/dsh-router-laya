#!/usr/bin/env bash
# Flapping-link mode: short sleep, many attempts, resume accumulates bytes across windows.
set -u
cd "$(dirname "$0")/.."
for i in $(seq 1 200); do
  echo "=== attempt $i/200 $(date +%H:%M:%S) ==="
  if node weights/fetch.mjs; then
    echo "=== DOWNLOAD COMPLETE ==="
    ls -la weights/model/ weights/model/encoder weights/model/tokenizer 2>/dev/null
    exit 0
  fi
  sleep 10
done
echo "FATAL: 200 attempts exhausted"
exit 1
