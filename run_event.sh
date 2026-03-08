#!/usr/bin/env bash
# Called by systemd path unit when inbox/ changes.
# Processes all ZIPs found in inbox/.
set -euo pipefail

INBOX="/home/bostock/ring_events/inbox"
WORKING="/home/bostock/ring_events/working"

shopt -s nullglob
ZIPS=("$INBOX"/Ring_*.zip)

if [ ${#ZIPS[@]} -eq 0 ]; then
    exit 0
fi

ANTHROPIC_API_KEY="$(grep ANTHROPIC_API_KEY /home/bostock/n8n/docker-compose.yml | cut -d= -f2)"
OWUI_TOKEN="$(curl -sf -X POST http://10.0.1.32:3000/api/v1/auths/signin \
  -H 'Content-Type: application/json' \
  -d '{"email":"bostock@gmail.com","password":"dL^7fD>9qaZi,3P"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")" || OWUI_TOKEN=""

for ZIP_FILE in "${ZIPS[@]}"; do
    ZIP_NAME="$(basename "$ZIP_FILE")"
    EVENT_ID="$(echo "$ZIP_NAME" | sed 's/^Ring_//;s/\.zip$//')"
    WORK_DIR="$WORKING/$EVENT_ID"

    echo "[$(date -Iseconds)] Starting event $EVENT_ID"

    mkdir -p "$WORK_DIR"
    CONTAINER_ZIP="${ZIP_FILE/\/home\/bostock\/ring_events//data/ring_events}"
    CONTAINER_WORK="${WORK_DIR/\/home\/bostock\/ring_events//data/ring_events}"
    docker exec n8n unzip -q "$CONTAINER_ZIP" -d "$CONTAINER_WORK/"

    WHISPER_URL=http://10.0.1.202:9876 \
    ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    OWUI_TOKEN="$OWUI_TOKEN" \
    python3 /home/bostock/ring_events/process_event.py "$WORK_DIR" --event-id "$EVENT_ID"

    echo "[$(date -Iseconds)] Done: $EVENT_ID"
done
