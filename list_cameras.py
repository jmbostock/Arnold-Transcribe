#!/usr/bin/env python3
"""
Discover Ring camera UUID prefixes from existing processed events.
Prints a camera_names.json snippet you can paste in to map names.

Usage:
    python3 list_cameras.py /home/bostock/ring_events/processed/
    python3 list_cameras.py /home/bostock/ring_events/processed/ --min-clips 3
"""
import json, sys, argparse
from pathlib import Path
from collections import defaultdict

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('processed_dir', help='Path to ring_events/processed/')
    parser.add_argument('--min-clips', type=int, default=1,
                        help='Only show UUIDs seen in at least this many clips')
    args = parser.parse_args()

    processed = Path(args.processed_dir)
    if not processed.exists():
        print(f"Directory not found: {processed}", file=sys.stderr)
        sys.exit(1)

    uuid_clips  = defaultdict(list)   # uuid_prefix -> list of clip filenames
    uuid_events = defaultdict(set)    # uuid_prefix -> set of event IDs

    for event_json in sorted(processed.glob('*/event.json')):
        event_id = event_json.parent.name
        try:
            data = json.loads(event_json.read_text())
        except Exception:
            continue
        for clip in data.get('clips', []):
            name = clip.get('clip_name', '')
            import re
            m = re.match(r'Ring_\d{8}_\d{4}_([a-f0-9-]+)\.mp4', name, re.I)
            if m:
                pfx = m.group(1)[:8]
                uuid_clips[pfx].append(name)
                uuid_events[pfx].add(event_id)

    if not uuid_clips:
        print("No clips found. Check processed_dir path.")
        return

    filtered = {k: v for k, v in uuid_clips.items() if len(v) >= args.min_clips}

    print("\n=== Discovered Camera UUID Prefixes ===\n")
    print(f"{'UUID Prefix':<14} {'Clips':>6} {'Events':>7}  Sample clip")
    print("-" * 70)
    for pfx, clips in sorted(filtered.items(), key=lambda x: -len(x[1])):
        sample = clips[0]
        print(f"{pfx:<14} {len(clips):>6} {len(uuid_events[pfx]):>7}  {sample}")

    print("\n=== Paste into camera_names.json (fill in friendly names) ===\n")
    snippet = {pfx: f"<REPLACE with camera name>" for pfx in sorted(filtered.keys())}
    print(json.dumps(snippet, indent=2))
    print()

if __name__ == '__main__':
    main()
