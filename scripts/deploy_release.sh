#!/usr/bin/env bash
# One-shot release deploy for dsh-router-laya with per-step GitHub connectivity retry.
# The link flaps (SSL_ERROR_SYSCALL mid-handshake), so EVERY step retries independently --
# reachability alone is not enough (first run died on push after a successful probe).
# All steps are idempotent; rerunning is always safe.
set -uo pipefail
cd "$(dirname "$0")"

REPO_API="https://api.github.com/repos/HapyRain/dsh-router-laya"
UPLOAD_API="https://uploads.github.com/repos/HapyRain/dsh-router-laya"
SRC="${DSH_ROUTER_LAYA_SRC:?set DSH_ROUTER_LAYA_SRC to the checkpoint dir}"
TAG="v2.1.0"

retry() { # retry <attempts> <sleep_s> <label> <cmd...>
  local n="$1" wait="$2" label="$3"; shift 3
  for a in $(seq 1 "$n"); do
    if "$@"; then echo "  ok: $label"; return 0; fi
    echo "  $label failed (attempt $a/$n), retry in ${wait}s"; sleep "$wait"
  done
  echo "FATAL: $label failed after $n attempts"; return 1
}

echo "=== push main (retry 8 x 20s) ==="
retry 8 20 "push main" git push -u origin main || exit 1
echo "=== push tag (retry 8 x 20s) ==="
retry 8 20 "push tag" git push origin "$TAG" || exit 1

echo "=== credential (kept in memory only) ==="
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p' | tr -d '\r')
[ -n "$TOKEN" ] || { echo "FATAL: no stored github credential"; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

echo "=== create release $TAG (if absent; retry 5) ==="
RID=""
for a in 1 2 3 4 5; do
  REL=$(curl -s --max-time 30 -H "$AUTH" "$REPO_API/releases/tags/$TAG")
  if echo "$REL" | grep -q '"id"'; then
    RID=$(echo "$REL" | python -c "import json,sys;print(json.load(sys.stdin)['id'])")
    echo "  release exists, id=$RID"; break
  fi
  cat > /tmp/rel_body.json <<'EOF'
{"tag_name":"v2.1.0","target_commitish":"main","name":"v2.1.0 — judge checkpoint (laya-router-7q)",
 "body":"Judge checkpoint for the dsh-router-laya auto-tier plugin (7-question fine-tune, Q7 consistency 99.0%).\n\nAssets are fetched automatically by `npx dsh-router-laya setup` (source 1 of 3; HF + hf-mirror are the fallbacks). sha256 manifest ships in the package at weights/manifest.json.",
 "draft":false,"prerelease":false}
EOF
  RID=$(curl -s --max-time 30 -X POST -H "$AUTH" -H "Content-Type: application/json" -d @/tmp/rel_body.json "$REPO_API/releases" | python -c "import json,sys;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
  if [ -n "$RID" ]; then echo "  release created, id=$RID"; break; fi
  echo "  release create failed (attempt $a/5), retry in 20s"; sleep 20
done
[ -n "$RID" ] || { echo "FATAL: release unavailable"; exit 1; }

echo "=== upload assets (flat names; model streamed last) ==="
# NOTE: uploads MUST use --data-binary. The `-T file -X POST` combination dies on schannel
# renegotiation to uploads.github.com (5/5 failures live-verified); --data-binary returns 201.
# --data-binary buffers the 842MB model in RAM, which this class of machine tolerates.
upload() { # $1 asset name, $2 source path
  local name="$1" file="$2"
  local fsize=$(stat -c %s "$file" 2>/dev/null || wc -c < "$file")
  for a in 1 2 3 4 5; do
    local resp
    resp=$(curl -s --max-time 1800 -X POST -H "$AUTH" -H "Content-Type: application/octet-stream" \
      --data-binary @"$file" "$UPLOAD_API/releases/$RID/assets?name=$name")
    if echo "$resp" | grep -q '"state": *"uploaded"'; then echo "  ok: $name"; return 0; fi
    if echo "$resp" | grep -q 'already_exists'; then
      # idempotent rerun: same name + same size counts as done
      local got
      got=$(curl -s --max-time 30 -H "$AUTH" "$REPO_API/releases/$RID/assets" | python -c "
import json,sys
print(next((a['size'] for a in json.load(sys.stdin) if a['name']=='$name'), -1))")
      if [ "$got" = "$fsize" ]; then echo "  ok (already exists, size matches): $name"; return 0; fi
      echo "  $name exists with wrong size ($got != $fsize) -- deleting and refetching"
      local aid
      aid=$(curl -s --max-time 30 -H "$AUTH" "$REPO_API/releases/$RID/assets" | python -c "
import json,sys
print(next((a['id'] for a in json.load(sys.stdin) if a['name']=='$name'), ''))")
      [ -n "$aid" ] && curl -s --max-time 30 -X DELETE -H "$AUTH" "$REPO_API/releases/assets/$aid" -o /dev/null
    else
      echo "  $name upload error: $(echo "$resp" | head -c 150)"
    fi
    echo "  retry $a/5 in 20s"; sleep 20
  done
  echo "FATAL: asset upload failed: $name"; return 1
}
curl -s --max-time 30 -X DELETE -H "$AUTH" "$REPO_API/releases/assets/589909841" -o /dev/null && echo "  probe asset removed"
upload "encoder__config.json"             "$SRC/encoder/config.json"             || exit 1
upload "rl_agent_config.json"             "$SRC/rl_agent_config.json"            || exit 1
upload "tokenizer__tokenizer.json"        "$SRC/tokenizer/tokenizer.json"        || exit 1
upload "tokenizer__tokenizer_config.json" "$SRC/tokenizer/tokenizer_config.json" || exit 1
upload "model.safetensors"                "$SRC/model.safetensors"               || exit 1

echo "=== topics (retry 5) ==="
for a in 1 2 3 4 5; do
  curl -s --max-time 30 -X PUT -H "$AUTH" -H "Accept: application/vnd.github+json" \
    -d '{"names":["dsh-plugin","cordis-plugin","dsh","deepseek","dsh-category-ui","router","auto-tier"]}' \
    "$REPO_API/topics" | grep -q '\[' && { echo "  ok: topics"; break; }
  sleep 20
done

echo "=== DEPLOY COMPLETE ==="
for a in 1 2 3; do
  FINAL=$(curl -s --max-time 30 -H "$AUTH" "$REPO_API/releases/tags/$TAG")
  if echo "$FINAL" | grep -q '"assets"'; then
    echo "$FINAL" | python -c "import json,sys;d=json.load(sys.stdin);print('release:',d['html_url']);print('assets:',[(a['name'],a['size']) for a in d['assets']])"
    break
  fi
  sleep 15
done
