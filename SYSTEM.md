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
    ↓  ffmpeg (inside n8n container) — noise-filtered WAV
    ↓  HTTP POST
Whisper API ←→ 10.0.1.202:9876 (GPU: RTX 5060 16GB, large-v3)
    ↓  transcript per clip
Claude Haiku API ←→ api.anthropic.com
    ↓  structured JSON analysis
processed/YYYYMMDD_HHMM/
    ├── event.json
    └── transcript.txt
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
- **Settings**: beam_size=10, vad_filter=True, best_of=5, temperature=0.0

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
- **Timezone**: Transcript timestamps are UTC; property is Pacific Time. System prompt converts automatically.

---

## Folder Layout (10.0.1.176)

```
/home/bostock/ring_events/
├── inbox/                ← DROP ZIPs HERE
├── working/
│   └── YYYYMMDD_HHMM/   ← extracted MP4s (temp)
├── processed/
│   └── YYYYMMDD_HHMM/
│       ├── event.json
│       ├── transcript.txt
│       └── Ring_YYYYMMDD_HHMM.zip  ← archived source
├── process_event.py      ← pipeline script
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
  "cameras_triggered": ["Clip_dab532e6", "..."],
  "clip_count": 39,
  "total_audio_seconds": 2102,
  "analysis": {
    "summary": "...",
    "persons_detected": 4,
    "activity_type": "visitor",
    "sentiment": "routine",
    "key_moments": ["..."],
    "recommendations": "..."
  },
  "combined_transcript": "...",
  "clips": [...]
}
```

### transcript.txt
Human-readable: event header, AI summary, then timestamped transcript per clip.

---

## Timestamps

All clip timestamps are **UTC**. The property is in **Pacific Time (UTC-8 PST / UTC-7 PDT)**. Arnold Transcribe converts automatically.

---

## Audio Pipeline

**ffmpeg filter chain** (noise reduction for outdoor Ring cameras):
- `highpass=f=200` — removes wind/rumble
- `lowpass=f=8000` — cuts noise above speech range
- `afftdn=nf=-20` — FFT noise reduction
- `dynaudnorm=g=5` — normalizes volume

Output: 16kHz mono PCM WAV → Whisper large-v3.

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

- Ring filenames: `Ring_YYYYMMDD_HHMM_UUID.mp4` — no camera name; IDs are first 8 chars of UUID
- Whisper server is not managed by systemd — restart manually after reboots on 10.0.1.202
- ffmpeg and unzip are only inside the n8n Docker container, not the host
- OpenWebUI OWUI tokens expire; `run_event.sh` re-authenticates on each run
- The n8n workflow (`dQnyO8sAim0Xn3m9`) exists but is inactive — `run_event.sh` is the reliable path
