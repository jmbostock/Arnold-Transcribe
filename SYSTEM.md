# Arnold Transcribe — System Documentation

Automated pipeline: Ring camera ZIP → audio extraction → Whisper transcription → Claude analysis → OpenWebUI RAG.

Drop a ZIP in `inbox/` and everything else happens automatically.

---

## Architecture

```
Ring App (bulk export)
    ↓  ZIP per visit
inbox/Ring_YYYYMMDD_HHMM.zip
    ↓  systemd path watcher (ring-event-watcher.path)
    ↓  run_event.sh
    ↓  docker exec n8n unzip
working/YYYYMMDD_HHMM/*.mp4
    ↓  process_event.py
    ↓  ffmpeg (inside n8n container) — noise-filtered WAV (outdoor/indoor aware)
    ↓  HTTP POST
Whisper API ←→ 10.0.1.202:9876 (GPU: RTX 5060 16GB, large-v3)
    ↓  transcript per clip (camera-aware initial_prompt, VAD, beam=10, best_of=5)
Claude Sonnet 4.6 API ←→ api.anthropic.com
    ↓  structured JSON: summary, exact quotes, per-camera breakdown
processed/YYYYMMDD_HHMM/
    ├── event.json
    └── transcript.txt  (exact quotes + file paths for navigation)
    ↓  HTTP POST
OpenWebUI "Ring Events" knowledge collection ←→ 10.0.1.32:3000
    ↓  queryable via
"Arnold Transcribe" model workspace (Claude Sonnet + RAG)
```

---

## Machines

| Host | Role |
|------|------|
| `10.0.1.176` | Proxmox — main server, runs n8n Docker, stores all data |
| `10.0.1.202` | GPU workstation — runs faster-whisper transcription server |
| `10.0.1.32` | OpenWebUI (v0.8.8) — query interface for transcripts |
| `10.0.1.168` | Home Assistant (HAOS) — Ring integration (currently offline) |

---

## Automation — Systemd Path Watcher

A path unit watches `inbox/` and fires the processor whenever a ZIP is dropped there.

```
/etc/systemd/system/ring-event-watcher.path     ← inotify watch on inbox/
/etc/systemd/system/ring-event-processor.service ← runs run_event.sh
/home/bostock/ring_events/run_event.sh           ← orchestration script
```

**Status:**
```bash
systemctl status ring-event-watcher.path
tail -f ~/ring_events/events.log
```

**To use:** drop `Ring_YYYYMMDD_HHMM.zip` into `~/ring_events/inbox/` — done.

---

## Camera Name Mapping

Ring export filenames contain only a UUID — no human-readable camera name.
`camera_names.json` maps the first 8 chars of each UUID to a friendly name.

### Setup

1. **Discover UUIDs** from existing events:
   ```bash
   python3 list_cameras.py /home/bostock/ring_events/processed/
   ```
   This prints a table of UUID prefixes with clip counts and a JSON snippet to paste.

2. **Edit `camera_names.json`** with your friendly names:
   ```json
   {
     "dab532e6": "Front Door",
     "abc12345": "Driveway",
     "def67890": "Backyard",
     "f1a2b3c4": "Side Gate",
     "_outdoor_cameras": ["Front Door", "Driveway", "Backyard", "Side Gate"]
   }
   ```
   `_outdoor_cameras` controls which cameras get the aggressive outdoor noise-reduction
   filter chain. If the list is empty or absent, all cameras are treated as outdoor.

3. Copy `camera_names.json` to the server:
   ```bash
   scp camera_names.json bostock@10.0.1.176:/home/bostock/ring_events/camera_names.json
   ```

---

## Services

### n8n (10.0.1.176)
- **Container**: `n8n`
- **URL**: http://10.0.1.176:5678
- **Config**: `/home/bostock/n8n/docker-compose.yml`
- **Dockerfile**: `/home/bostock/n8n/Dockerfile` — node:22-alpine + ffmpeg + unzip
- **Key**: ffmpeg and unzip are only available inside this container, not on the host.
  All audio extraction and unzip operations run via `docker exec n8n ...`

```bash
cd /home/bostock/n8n
docker compose down && docker compose up -d --build
docker logs n8n -f
```

### Whisper Server (10.0.1.202)
- **Path**: `/home/bostock/whisper-server/server.py`
- **URL**: http://10.0.1.202:9876
- **Model**: large-v3, CUDA, float32
- **Parameters sent per clip**: beam_size=10, best_of=5, temperature=0.0, vad_filter=true
- **initial_prompt**: camera-location-aware (Front Door / Driveway / Backyard / Side Gate / generic outdoor)

```bash
# Check
curl http://10.0.1.202:9876/health

# Start (after reboot — not a systemd service)
ssh bostock@10.0.1.202
cd ~/whisper-server
nohup ~/.local/bin/uvicorn server:app --host 0.0.0.0 --port 9876 > ~/whisper-server.log 2>&1 &
```

### OpenWebUI (10.0.1.32)
- **URL**: http://10.0.1.32:3000 (v0.8.8)
- **Model**: "Arnold Transcribe" — Claude Sonnet 4.6, RAG over Ring Events collection
- **Knowledge collection**: "Ring Events" (ID: `edc64ab9-c92a-4230-9b37-727cba8d3610`)
- **Note**: OWUI auth tokens are session-based (expire). `run_event.sh` re-authenticates each run.
- **Timezone**: Transcript timestamps are Pacific Time (converted from UTC).

---

## Folder Layout (10.0.1.176)

```
/home/bostock/ring_events/
├── inbox/                ← DROP ZIPs HERE
├── working/
│   └── YYYYMMDD_HHMM/   ← extracted MP4s (temp)
├── processed/
│   └── YYYYMMDD_HHMM/
│       ├── event.json          ← full structured data
│       ├── transcript.txt      ← navigable transcript for OpenWebUI
│       └── Ring_YYYYMMDD_HHMM.zip  ← archived source
├── process_event.py      ← pipeline script
├── list_cameras.py       ← discover UUID→name mappings from existing events
├── camera_names.json     ← UUID prefix → friendly camera name mapping
├── run_event.sh          ← called by systemd, orchestrates full run
├── ring-event-watcher.path      ← systemd path unit (copy to /etc/systemd/system/)
├── ring-event-processor.service ← systemd service unit
└── SYSTEM.md             ← this file
```

Container mount: `/home/bostock/ring_events` → `/data/ring_events` inside n8n.

---

## Output Format

### event.json
```json
{
  "event_id": "20260307_2205",
  "event_timestamp": "2026-03-07T22:05:00",
  "processed_at": "...",
  "cameras_triggered": ["Front Door", "Driveway"],
  "clip_count": 39,
  "total_audio_seconds": 2102,
  "analysis": {
    "summary": "3-5 sentence description of the full event...",
    "persons_detected": 4,
    "activity_type": "visitor",
    "sentiment": "routine",
    "per_camera_activity": {
      "Front Door": "Resident greeted a visitor at the door...",
      "Driveway": "A vehicle pulled in and parked..."
    },
    "exact_quotes": [
      {
        "quote": "verbatim text from transcript",
        "timestamp": "22:05",
        "camera": "Front Door",
        "context": "Resident speaking to visitor"
      }
    ],
    "key_moments": [
      "[22:05 | Front Door] Visitor rang doorbell and was greeted by resident"
    ],
    "recommendations": "..."
  },
  "combined_transcript": "...",
  "clips": [...]
}
```

### transcript.txt
Rich human-readable format uploaded to OpenWebUI:
- **Header**: event ID, timestamps, file system path (`processed/YYYYMMDD_HHMM/event.json`)
- **Summary**: 3-5 sentence AI summary
- **Per-Camera Activity**: what each named camera captured
- **Exact Quotes**: verbatim text with timestamp + camera name
- **Key Moments**: bulleted timeline
- **Full Transcript**: all clips with `[TIME | CAMERA NAME | CLIP FILENAME]` headers

This means an OpenWebUI RAG result will always tell you the clip filename and
camera that produced the quote — you can go directly to the source video.

---

## Timestamps

All clip timestamps from Ring filenames are **UTC**. Arnold Transcribe converts to **Pacific Time (UTC-8 PST / UTC-7 PDT)** automatically. All output timestamps are Pacific Time.

---

## Audio Pipeline

**Outdoor cameras** (listed in `_outdoor_cameras` in `camera_names.json`):
```
highpass=f=200       — removes wind / low-frequency rumble
lowpass=f=8000       — caps above usable speech range
afftdn=nf=-25        — FFT noise reduction (stronger than default)
agate=threshold=...  — noise gate: suppresses background between speech
dynaudnorm=g=5       — normalizes volume so quiet speech is audible
```

**Indoor cameras** (not in outdoor list):
```
afftdn=nf=-15        — lighter noise reduction
dynaudnorm=g=5       — volume normalization
```

Output: 16kHz mono PCM WAV → Whisper large-v3.

---

## Analysis Model

**Claude Sonnet 4.6** (`claude-sonnet-4-6`) — quality-first choice.
- No transcript truncation — full event analyzed regardless of length
- max_tokens: 4096 — room for thorough structured output
- Extracts verbatim exact quotes, per-camera breakdown, 3-5 sentence summaries

---

## Manual Run (if needed)

```bash
curl http://10.0.1.202:9876/health  # confirm Whisper is up

cd /home/bostock/ring_events
WHISPER_URL=http://10.0.1.202:9876 \
ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY /home/bostock/n8n/docker-compose.yml | cut -d= -f2) \
python3 process_event.py working/YYYYMMDD_HHMM --event-id YYYYMMDD_HHMM
```

---

## Known Issues

- Ring filenames contain device UUIDs, not camera names — configure `camera_names.json` to resolve friendly names
- Whisper server is not managed by systemd — restart manually after reboots on 10.0.1.202
- ffmpeg and unzip are only inside the n8n Docker container, not the host
- OpenWebUI OWUI tokens expire; `run_event.sh` re-authenticates on each run
- The n8n workflow (`dQnyO8sAim0Xn3m9`) exists but is inactive — `run_event.sh` is the reliable path
