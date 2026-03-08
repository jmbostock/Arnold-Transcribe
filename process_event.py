#!/usr/bin/env python3
"""
Process a Ring event: extract audio, transcribe with Whisper, analyze with Claude Haiku.
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

WHISPER_URL = os.environ.get('WHISPER_URL', 'http://10.0.1.202:9876')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OWUI_URL = os.environ.get('OWUI_URL', 'http://10.0.1.32:3000')
OWUI_TOKEN = os.environ.get('OWUI_TOKEN', '')
OWUI_KNOWLEDGE_NAME = 'Ring Events'
PROCESSED_DIR = '/home/bostock/ring_events/processed'
INBOX_DIR = '/home/bostock/ring_events/inbox'

def extract_audio(mp4_path, wav_path):
    # Map host paths to container paths
    container_mp4 = str(mp4_path).replace('/home/bostock/ring_events', '/data/ring_events')
    container_wav = str(wav_path).replace('/home/bostock/ring_events', '/data/ring_events')
    # Audio filter chain for outdoor Ring camera audio:
    #   highpass=200Hz  — remove wind/low-frequency rumble
    #   lowpass=8000Hz  — remove high-freq noise above speech range
    #   afftdn          — FFT noise reduction
    #   dynaudnorm      — normalize volume so quiet speech is audible
    audio_filter = "highpass=f=200,lowpass=f=8000,afftdn=nf=-20,dynaudnorm=g=5"
    result = subprocess.run(
        ['docker', 'exec', 'n8n', 'ffmpeg', '-y', '-i', container_mp4,
         '-vn', '-af', audio_filter, '-ar', '16000', '-ac', '1',
         '-c:a', 'pcm_s16le', container_wav],
        capture_output=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.decode()[-200:]}")

def transcribe(wav_path):
    with open(wav_path, 'rb') as f:
        resp = requests.post(
            f'{WHISPER_URL}/transcribe',
            params={
                'language': 'en',
                'beam_size': 10,
                'initial_prompt': 'Security camera footage. People talking outside near a house.'
            },
            files={'file': ('audio.wav', f, 'audio/wav')},
            timeout=180
        )
    resp.raise_for_status()
    return resp.json()

def analyze_with_haiku(event_id, cameras, clip_count, total_duration, transcript):
    if not ANTHROPIC_API_KEY:
        return {'error': 'No ANTHROPIC_API_KEY set'}

    prompt = f"""Analyze this security camera event and return a JSON object with exactly these fields:
- summary: string (1-2 sentences describing the event)
- persons_detected: number (estimate from transcript context, 0 if unclear)
- activity_type: string (e.g. "delivery", "visitor", "service_worker", "resident", "unknown")
- sentiment: string ("routine", "suspicious", "urgent")
- key_moments: array of strings (notable quotes or events from transcript)
- recommendations: string (any security notes)

Event ID: {event_id}
Cameras: {', '.join(cameras)}
Clips: {clip_count}, Total audio: {total_duration}s

Transcript:
{transcript[:8000]}"""

    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        json={
            'model': 'claude-haiku-4-5-20251001',
            'max_tokens': 1024,
            'system': 'You analyze security camera transcripts. Return ONLY valid JSON, no markdown.',
            'messages': [{'role': 'user', 'content': prompt}]
        },
        timeout=30
    )
    resp.raise_for_status()
    raw = resp.json()['content'][0]['text']
    raw = raw.replace('```json', '').replace('```', '').strip()
    return json.loads(raw)

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
        clip_ts = to_pst(m.group(1), m.group(2)) if m else ''
        clip_id = m.group(3)[:8] if m else mp4.stem

        if not wav.exists():
            try:
                extract_audio(mp4, wav)
            except Exception as e:
                print(f"    FFmpeg failed: {e}")
                clips.append({'camera': f'Clip_{clip_id}', 'clip_name': mp4.name,
                               'clip_timestamp': clip_ts, 'transcript': '',
                               'duration_seconds': 0, 'error': str(e)})
                continue

        try:
            result = transcribe(wav)
            transcript = result.get('transcript', '')
            duration = result.get('duration_seconds', 0)
            print(f"    → {duration:.0f}s: {transcript[:80]}")
        except Exception as e:
            print(f"    Whisper failed: {e}")
            transcript = ''
            duration = 0

        clips.append({
            'camera': f'Clip_{clip_id}',
            'clip_name': mp4.name,
            'clip_timestamp': clip_ts,
            'transcript': transcript,
            'duration_seconds': duration
        })

        if wav.exists():
            wav.unlink()

    cameras = list(set(c['camera'] for c in clips))
    total_duration = sum(c.get('duration_seconds', 0) for c in clips)
    event_ts = clips[0]['clip_timestamp'] if clips else datetime.now().isoformat()

    combined = '\n\n'.join(
        f"[{c['clip_timestamp'][11:16]} - {c['camera']}]\n{c['transcript'].strip()}"
        for c in sorted(clips, key=lambda x: x['clip_timestamp'])
        if c.get('transcript', '').strip()
    )

    print(f"\nAnalyzing with Claude Haiku...", flush=True)
    try:
        analysis = analyze_with_haiku(event_id, cameras, len(clips), total_duration, combined)
    except Exception as e:
        print(f"  Haiku failed: {e}")
        analysis = {'error': str(e)}

    event_dir = Path(PROCESSED_DIR) / event_id
    event_dir.mkdir(parents=True, exist_ok=True)

    event_record = {
        'event_id': event_id,
        'event_timestamp': event_ts,
        'processed_at': datetime.now().isoformat(),
        'cameras_triggered': cameras,
        'clip_count': len(clips),
        'total_audio_seconds': round(total_duration),
        'analysis': analysis,
        'combined_transcript': combined,
        'clips': clips
    }

    with open(event_dir / 'event.json', 'w') as f:
        json.dump(event_record, f, indent=2)

    with open(event_dir / 'transcript.txt', 'w') as f:
        f.write(f"EVENT: {event_id}\n")
        f.write(f"Timestamp: {event_ts}\n")
        f.write(f"Cameras: {', '.join(cameras)}\n")
        f.write(f"Clips: {len(clips)}, Duration: {total_duration:.0f}s\n\n")
        if 'summary' in analysis:
            f.write(f"SUMMARY: {analysis['summary']}\n\n")
        f.write("TRANSCRIPT:\n")
        f.write(combined)

    print(f"\nWritten to {event_dir}/")
    print(f"Summary: {analysis.get('summary', 'N/A')}")

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


def upload_to_openwebui(event_dir, event_id):
    headers = {'Authorization': f'Bearer {OWUI_TOKEN}'}
    base = OWUI_URL

    # Find or create the knowledge collection
    collections = requests.get(f'{base}/api/v1/knowledge/', headers=headers, timeout=10).json().get('items', [])
    collection = next((c for c in collections if c['name'] == OWUI_KNOWLEDGE_NAME), None)
    if not collection:
        resp = requests.post(f'{base}/api/v1/knowledge/create', headers=headers,
                             json={'name': OWUI_KNOWLEDGE_NAME,
                                   'description': 'Security camera event transcripts from Arnold property'},
                             timeout=10)
        resp.raise_for_status()
        collection = resp.json()
    collection_id = collection['id']

    # Upload transcript.txt
    transcript_path = Path(event_dir) / 'transcript.txt'
    with open(transcript_path, 'rb') as f:
        resp = requests.post(f'{base}/api/v1/files/', headers=headers,
                             files={'file': (f'transcript_{event_id}.txt', f, 'text/plain')},
                             timeout=30)
    resp.raise_for_status()
    file_id = resp.json()['id']

    # Add file to collection
    resp = requests.post(f'{base}/api/v1/knowledge/{collection_id}/file/add',
                         headers=headers, json={'file_id': file_id}, timeout=30)
    resp.raise_for_status()
    print(f"Uploaded transcript to OpenWebUI '{OWUI_KNOWLEDGE_NAME}' collection")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('work_dir', help='Directory containing MP4 files')
    parser.add_argument('--event-id', help='Event ID (default: directory name)')
    args = parser.parse_args()
    process_event(args.work_dir, args.event_id)
