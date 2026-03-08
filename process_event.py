#!/usr/bin/env python3
"""
Process a Ring event: extract audio, transcribe with Whisper, analyze with Claude Sonnet.
Usage: python3 process_event.py <work_dir> [--event-id EVENT_ID]
"""
import os, sys, json, subprocess, requests, re, shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

PST = timezone(timedelta(hours=-8))

def to_pst(date_str, time_str):
    dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
                  int(time_str[:2]), int(time_str[2:]), tzinfo=timezone.utc)
    return dt.astimezone(PST).strftime('%Y-%m-%dT%H:%M:%S')

WHISPER_URL   = os.environ.get('WHISPER_URL', 'http://10.0.1.202:9876')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OWUI_URL      = os.environ.get('OWUI_URL', 'http://10.0.1.32:3000')
OWUI_TOKEN    = os.environ.get('OWUI_TOKEN', '')
OWUI_KNOWLEDGE_NAME = 'Ring Events'
PROCESSED_DIR = '/home/bostock/ring_events/processed'
INBOX_DIR     = '/home/bostock/ring_events/inbox'

# ---------------------------------------------------------------------------
# Camera name resolution
# ---------------------------------------------------------------------------
# Load camera_names.json from the same directory as this script.
# Keys are the first 8 chars of the Ring clip UUID; values are friendly names.
# Outdoor camera detection uses the _outdoor_cameras list in the same file.
_CAMERA_NAMES_FILE = Path(__file__).parent / 'camera_names.json'
_camera_config = {}
_camera_names  = {}       # uuid_prefix -> friendly name
_outdoor_names = set()    # set of friendly names that are outdoor cameras

def _load_camera_names():
    global _camera_config, _camera_names, _outdoor_names
    if not _CAMERA_NAMES_FILE.exists():
        return
    try:
        _camera_config = json.loads(_CAMERA_NAMES_FILE.read_text())
        _camera_names  = {k: v for k, v in _camera_config.items() if not k.startswith('_')}
        _outdoor_names = set(_camera_config.get('_outdoor_cameras', []))
    except Exception as e:
        print(f"Warning: could not load camera_names.json: {e}")

_load_camera_names()

def resolve_camera_name(uuid_prefix: str) -> str:
    """Return friendly name for a UUID prefix, or 'Camera_<prefix>' if unmapped."""
    return _camera_names.get(uuid_prefix, f'Camera_{uuid_prefix}')

def is_outdoor_camera(friendly_name: str) -> bool:
    """True if this camera is listed as outdoor in camera_names.json."""
    # If no outdoor list configured, treat all Ring cameras as outdoor by default.
    if not _outdoor_names:
        return True
    return friendly_name in _outdoor_names

def whisper_prompt_for_camera(friendly_name: str) -> str:
    """Return a context-tuned initial_prompt for Whisper based on camera location."""
    if 'door' in friendly_name.lower() or 'front' in friendly_name.lower():
        return (
            "Front door security camera. Visitors, delivery drivers, residents arriving. "
            "Doorbell ringing, knocking, greetings, package delivery instructions."
        )
    if 'drive' in friendly_name.lower() or 'garage' in friendly_name.lower():
        return (
            "Driveway security camera. Vehicles arriving and departing. "
            "Residents, visitors, delivery trucks. Car doors, conversations near vehicles."
        )
    if 'back' in friendly_name.lower() or 'yard' in friendly_name.lower() or 'garden' in friendly_name.lower():
        return (
            "Backyard security camera. Outdoor conversation. "
            "Family members, guests, service workers in the yard or garden."
        )
    if 'gate' in friendly_name.lower() or 'side' in friendly_name.lower():
        return (
            "Side gate security camera. People entering or exiting through the gate. "
            "Outdoor conversation near fence or gate."
        )
    # Generic outdoor fallback
    return (
        "Outdoor security camera at a residential property. "
        "People talking outside. May include ambient noise, wind, traffic."
    )

# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------
def extract_audio(mp4_path, wav_path, outdoor: bool = True):
    container_mp4 = str(mp4_path).replace('/home/bostock/ring_events', '/data/ring_events')
    container_wav = str(wav_path).replace('/home/bostock/ring_events', '/data/ring_events')

    if outdoor:
        # Outdoor filter chain — aggressive noise reduction for wind/traffic:
        #   highpass=200Hz   — remove wind / low-frequency rumble
        #   lowpass=8000Hz   — cap above usable speech range (keeps sibilants)
        #   afftdn=-25dB     — FFT noise reduction (slightly stronger than default)
        #   agate            — noise gate: suppress background between speech bursts
        #   dynaudnorm       — normalize volume so quiet speech is audible
        audio_filter = (
            "highpass=f=200,"
            "lowpass=f=8000,"
            "afftdn=nf=-25,"
            "agate=threshold=0.015:ratio=4:attack=10:release=250,"
            "dynaudnorm=g=5"
        )
    else:
        # Indoor — lighter chain, preserve more natural sound
        audio_filter = "afftdn=nf=-15,dynaudnorm=g=5"

    result = subprocess.run(
        ['docker', 'exec', 'n8n', 'ffmpeg', '-y', '-i', container_mp4,
         '-vn', '-af', audio_filter, '-ar', '16000', '-ac', '1',
         '-c:a', 'pcm_s16le', container_wav],
        capture_output=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.decode()[-200:]}")

# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------
def transcribe(wav_path, camera_name: str = 'Unknown Camera'):
    initial_prompt = whisper_prompt_for_camera(camera_name)
    with open(wav_path, 'rb') as f:
        resp = requests.post(
            f'{WHISPER_URL}/transcribe',
            params={
                'language':       'en',
                'beam_size':      10,
                'best_of':        5,
                'temperature':    0.0,
                'vad_filter':     'true',
                'initial_prompt': initial_prompt,
            },
            files={'file': ('audio.wav', f, 'audio/wav')},
            timeout=300
        )
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Claude Sonnet analysis
# ---------------------------------------------------------------------------
def analyze_with_sonnet(event_id, clips_meta, combined_transcript):
    """
    clips_meta: list of dicts with keys camera, clip_name, clip_timestamp, transcript, duration_seconds
    Returns structured analysis dict.
    Quality-first: uses Claude Sonnet 4.6 with no transcript truncation.
    """
    if not ANTHROPIC_API_KEY:
        return {'error': 'No ANTHROPIC_API_KEY set'}

    # Build per-camera breakdown for the prompt
    camera_lines = []
    seen = {}
    for c in clips_meta:
        cam = c['camera']
        dur = c.get('duration_seconds', 0)
        seen[cam] = seen.get(cam, 0) + dur
    for cam, dur in sorted(seen.items()):
        camera_lines.append(f"  - {cam}: {round(dur)}s of audio")

    total_duration = sum(c.get('duration_seconds', 0) for c in clips_meta)
    clip_count     = len(clips_meta)

    prompt = f"""You are analyzing security camera footage from the Arnold residential property.
Study the transcript carefully and return a JSON object with EXACTLY these fields.

IMPORTANT RULES:
- exact_quotes must be VERBATIM text copied directly from the transcript — never paraphrase.
- key_moments must include the timestamp and camera name, e.g. "[22:05 - Front Door] Resident said 'I'll be right there'"
- summary should be 3-5 sentences covering what happened, who was involved, and any notable details.
- per_camera_activity maps each camera name to a 1-2 sentence description of what that camera captured.

Return ONLY valid JSON. No markdown, no explanation, no code fences.

JSON SCHEMA:
{{
  "summary": "string (3-5 sentences describing the full event)",
  "persons_detected": number,
  "activity_type": "delivery | visitor | service_worker | resident | suspicious | unknown",
  "sentiment": "routine | suspicious | urgent",
  "per_camera_activity": {{
    "<camera_name>": "string describing what this specific camera captured"
  }},
  "exact_quotes": [
    {{
      "quote": "verbatim text from transcript",
      "timestamp": "HH:MM",
      "camera": "camera name",
      "context": "brief note on who said it or what was happening"
    }}
  ],
  "key_moments": [
    "string with timestamp and camera, e.g. [22:05 - Front Door] Person arrived and rang doorbell"
  ],
  "recommendations": "string (any security observations or follow-up notes)"
}}

Event ID: {event_id}
Cameras active ({len(seen)} total):
{chr(10).join(camera_lines)}
Clip count: {clip_count}, Total audio: {round(total_duration)}s

Full transcript (all clips, chronological):
{combined_transcript}"""

    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key':           ANTHROPIC_API_KEY,
            'anthropic-version':   '2023-06-01',
            'content-type':        'application/json'
        },
        json={
            'model':      'claude-sonnet-4-6',
            'max_tokens': 4096,
            'system':     (
                'You analyze security camera transcripts for a residential property. '
                'Return ONLY valid JSON matching the schema provided. No markdown, no preamble.'
            ),
            'messages': [{'role': 'user', 'content': prompt}]
        },
        timeout=120
    )
    resp.raise_for_status()
    raw = resp.json()['content'][0]['text'].strip()
    # Strip accidental markdown fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)

# ---------------------------------------------------------------------------
# Transcript formatting helpers
# ---------------------------------------------------------------------------
def format_combined_transcript(clips):
    """
    Build the combined transcript used for analysis and storage.
    Each entry includes: full timestamp, friendly camera name, clip filename, and transcript.
    This gives exact file references for navigation.
    """
    lines = []
    for c in sorted(clips, key=lambda x: x['clip_timestamp']):
        if not c.get('transcript', '').strip():
            continue
        ts      = c['clip_timestamp']          # 2026-03-07T22:05:00
        time    = ts[11:16] if len(ts) >= 16 else ts
        camera  = c['camera']
        clipname = c['clip_name']
        lines.append(
            f"[{time} | {camera} | {clipname}]\n{c['transcript'].strip()}"
        )
    return '\n\n'.join(lines)

def write_transcript_txt(f_obj, event_id, event_ts, clips, analysis, processed_dir):
    """
    Write a rich transcript.txt to f_obj.
    Includes file system paths so OpenWebUI queries return navigable references.
    """
    cameras = sorted(set(c['camera'] for c in clips))

    f_obj.write(f"EVENT: {event_id}\n")
    f_obj.write(f"Timestamp: {event_ts}\n")
    f_obj.write(f"Processed at: {datetime.now(PST).strftime('%Y-%m-%dT%H:%M:%S %Z')}\n")
    f_obj.write(f"Source files: {processed_dir}/event.json\n")
    f_obj.write(f"Cameras ({len(cameras)}): {', '.join(cameras)}\n")
    f_obj.write(f"Clips: {len(clips)}, Total audio: {sum(c.get('duration_seconds',0) for c in clips):.0f}s\n")
    f_obj.write("\n")

    if analysis.get('summary'):
        f_obj.write("SUMMARY\n")
        f_obj.write("=" * 60 + "\n")
        f_obj.write(analysis['summary'] + "\n\n")

    if analysis.get('per_camera_activity'):
        f_obj.write("PER-CAMERA ACTIVITY\n")
        f_obj.write("=" * 60 + "\n")
        for cam, desc in analysis['per_camera_activity'].items():
            f_obj.write(f"  {cam}: {desc}\n")
        f_obj.write("\n")

    if analysis.get('exact_quotes'):
        f_obj.write("EXACT QUOTES\n")
        f_obj.write("=" * 60 + "\n")
        for q in analysis['exact_quotes']:
            f_obj.write(
                f"  [{q.get('timestamp','?')} | {q.get('camera','?')}] "
                f"\"{q.get('quote','')}\"\n"
                f"    Context: {q.get('context','')}\n"
            )
        f_obj.write("\n")

    if analysis.get('key_moments'):
        f_obj.write("KEY MOMENTS\n")
        f_obj.write("=" * 60 + "\n")
        for m in analysis['key_moments']:
            f_obj.write(f"  • {m}\n")
        f_obj.write("\n")

    f_obj.write("FULL TRANSCRIPT\n")
    f_obj.write("=" * 60 + "\n")
    f_obj.write("Format: [TIME | CAMERA | CLIP FILENAME]\n\n")

    for c in sorted(clips, key=lambda x: x['clip_timestamp']):
        ts      = c['clip_timestamp']
        time    = ts[11:16] if len(ts) >= 16 else ts
        camera  = c['camera']
        clipname = c['clip_name']
        dur     = c.get('duration_seconds', 0)
        tx      = c.get('transcript', '').strip()

        f_obj.write(f"[{time} | {camera} | {clipname}]  ({dur:.0f}s)\n")
        if tx:
            f_obj.write(tx + "\n")
        else:
            f_obj.write("(no speech detected)\n")
        f_obj.write("\n")

# ---------------------------------------------------------------------------
# Main event processor
# ---------------------------------------------------------------------------
def process_event(work_dir, event_id=None):
    work_dir = Path(work_dir)
    if not event_id:
        event_id = work_dir.name

    mp4_files = sorted(work_dir.glob('Ring_*.mp4'))
    if not mp4_files:
        print(f"No MP4 files found in {work_dir}")
        sys.exit(1)

    print(f"Processing event {event_id}: {len(mp4_files)} clips")

    clips = []
    for i, mp4 in enumerate(mp4_files):
        wav = mp4.with_suffix('.wav')
        print(f"  [{i+1}/{len(mp4_files)}] {mp4.name}", flush=True)

        m = re.match(r'Ring_(\d{8})_(\d{4})_([a-f0-9-]+)\.mp4', mp4.name, re.I)
        clip_ts  = to_pst(m.group(1), m.group(2)) if m else ''
        uuid_pfx = m.group(3)[:8] if m else mp4.stem
        cam_name = resolve_camera_name(uuid_pfx)
        outdoor  = is_outdoor_camera(cam_name)

        if not wav.exists():
            try:
                extract_audio(mp4, wav, outdoor=outdoor)
            except Exception as e:
                print(f"    FFmpeg failed: {e}")
                clips.append({
                    'camera': cam_name, 'clip_name': mp4.name,
                    'clip_timestamp': clip_ts, 'transcript': '',
                    'duration_seconds': 0, 'error': str(e),
                    'outdoor': outdoor,
                })
                continue

        try:
            result     = transcribe(wav, camera_name=cam_name)
            transcript = result.get('transcript', '')
            duration   = result.get('duration_seconds', 0)
            print(f"    [{cam_name}] {duration:.0f}s: {transcript[:80]}")
        except Exception as e:
            print(f"    Whisper failed: {e}")
            transcript = ''
            duration   = 0

        clips.append({
            'camera':           cam_name,
            'clip_name':        mp4.name,
            'clip_timestamp':   clip_ts,
            'transcript':       transcript,
            'duration_seconds': duration,
            'outdoor':          outdoor,
        })

        if wav.exists():
            wav.unlink()

    cameras        = sorted(set(c['camera'] for c in clips))
    total_duration = sum(c.get('duration_seconds', 0) for c in clips)
    event_ts       = clips[0]['clip_timestamp'] if clips else datetime.now(PST).isoformat()

    combined = format_combined_transcript(clips)

    print(f"\nAnalyzing with Claude Sonnet 4.6 (full transcript, no truncation)...", flush=True)
    try:
        analysis = analyze_with_sonnet(event_id, clips, combined)
    except Exception as e:
        print(f"  Sonnet analysis failed: {e}")
        analysis = {'error': str(e)}

    event_dir = Path(PROCESSED_DIR) / event_id
    event_dir.mkdir(parents=True, exist_ok=True)

    event_record = {
        'event_id':           event_id,
        'event_timestamp':    event_ts,
        'processed_at':       datetime.now().isoformat(),
        'cameras_triggered':  cameras,
        'clip_count':         len(clips),
        'total_audio_seconds': round(total_duration),
        'analysis':           analysis,
        'combined_transcript': combined,
        'clips':              clips,
    }

    with open(event_dir / 'event.json', 'w') as f:
        json.dump(event_record, f, indent=2)

    with open(event_dir / 'transcript.txt', 'w') as f:
        write_transcript_txt(f, event_id, event_ts, clips, analysis, str(event_dir))

    print(f"\nWritten to {event_dir}/")
    print(f"Summary: {analysis.get('summary', 'N/A')[:120]}")

    zip_path = Path(INBOX_DIR) / f"Ring_{event_id}.zip"
    if zip_path.exists():
        shutil.copy2(zip_path, event_dir / zip_path.name)
        zip_path.unlink()
        print(f"Archived ZIP to processed/{event_id}/")

    if OWUI_TOKEN:
        try:
            upload_to_openwebui(event_dir, event_id)
        except Exception as e:
            print(f"OpenWebUI upload failed (non-fatal): {e}")

    return event_record


# ---------------------------------------------------------------------------
# OpenWebUI upload
# ---------------------------------------------------------------------------
def upload_to_openwebui(event_dir, event_id):
    headers = {'Authorization': f'Bearer {OWUI_TOKEN}'}
    base    = OWUI_URL

    # Find or create the knowledge collection
    resp        = requests.get(f'{base}/api/v1/knowledge/', headers=headers, timeout=10)
    collections = resp.json().get('items', []) if resp.ok else []
    collection  = next((c for c in collections if c['name'] == OWUI_KNOWLEDGE_NAME), None)
    if not collection:
        resp = requests.post(
            f'{base}/api/v1/knowledge/create', headers=headers,
            json={'name': OWUI_KNOWLEDGE_NAME,
                  'description': 'Security camera event transcripts from Arnold property'},
            timeout=10
        )
        resp.raise_for_status()
        collection = resp.json()
    collection_id = collection['id']

    # Upload transcript.txt (contains exact quotes, file paths, per-camera breakdown)
    transcript_path = Path(event_dir) / 'transcript.txt'
    with open(transcript_path, 'rb') as f:
        resp = requests.post(
            f'{base}/api/v1/files/', headers=headers,
            files={'file': (f'transcript_{event_id}.txt', f, 'text/plain')},
            timeout=60
        )
    resp.raise_for_status()
    file_id = resp.json()['id']

    # Add file to collection
    resp = requests.post(
        f'{base}/api/v1/knowledge/{collection_id}/file/add',
        headers=headers, json={'file_id': file_id}, timeout=30
    )
    resp.raise_for_status()
    print(f"Uploaded transcript to OpenWebUI '{OWUI_KNOWLEDGE_NAME}' collection (file_id={file_id})")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('work_dir',    help='Directory containing MP4 files')
    parser.add_argument('--event-id', help='Event ID (default: directory name)')
    args = parser.parse_args()
    process_event(args.work_dir, args.event_id)
