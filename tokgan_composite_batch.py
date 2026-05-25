#!/usr/bin/env python3
"""
tokgan_composite_batch.py — Batch driver for tokgan_composite.py.

Walks the k3s_queue outputs directory, pairs each shape JSON with
the matching input video (under input_videos_action_short_pexels/)
and the visualisation video in the same hash dir, then invokes
tokgan_composite.py once per triple.

Defaults are wired for the user's local layout:
  inputs:  /Users/sam/Documents/tokgan/k3s_queue/input_videos_action_short_pexels/
  outputs: /Users/sam/Documents/tokgan/k3s_queue/outputs/

Skips pairs whose composite.mp4 already exists (pass --force to
re-render). Continues on per-clip failures and prints a summary at
the end so a long run isn't lost to one bad clip.

The per-pair orchestrator drives AE end-to-end (osascript build +
in-process render + ffmpeg transcode + intermediate cleanup), so
disk usage stays bounded to roughly one clip's intermediate at a
time. Expect ~1 min/clip for short 4K clips.
"""

import argparse
import glob
import os
import subprocess
import sys
import time


DEFAULT_OUTPUTS = "/Users/sam/Documents/tokgan/k3s_queue/outputs"
DEFAULT_INPUTS  = "/Users/sam/Documents/tokgan/k3s_queue/input_videos_action_short_pexels"


def find_triples(outputs_root, inputs_root):
    """Yield (input_video, json_path, viz_video_or_None) per hash dir.

    JSON filenames look like `{input_stem}__{timestamp}.json`; the
    input video is `{input_stem}.mp4` under inputs_root; the viz
    video is the only `{input_stem}__*.mp4` in the hash dir that
    isn't itself an output of this pipeline.
    """
    for hash_dir in sorted(glob.glob(os.path.join(outputs_root, "*"))):
        if not os.path.isdir(hash_dir):
            continue
        for json_path in sorted(glob.glob(os.path.join(hash_dir, "*.json"))):
            name = os.path.basename(json_path)
            # Skip our own generated sidecars and composite-mode outputs.
            if name.endswith("_data.json") or "__composite" in name:
                continue
            if "__" not in name:
                continue
            input_stem = name.split("__", 1)[0]
            input_video = os.path.join(inputs_root, input_stem + ".mp4")
            if not os.path.isfile(input_video):
                continue
            viz = None
            for mp4 in sorted(glob.glob(os.path.join(hash_dir, input_stem + "__*.mp4"))):
                base = os.path.basename(mp4)
                if "__composite" in base or "__relabeled" in base:
                    continue
                viz = mp4
                break
            yield input_video, json_path, viz


def main():
    p = argparse.ArgumentParser(
        prog="tokgan_composite_batch",
        description=(
            "Batch driver: walk outputs/*/*.json and run "
            "tokgan_composite.py for each (input_video, json, viz) "
            "triple. Skips pairs whose __composite.mp4 already "
            "exists unless --force. Continues on per-clip failure."
        ),
        epilog=(
            "AE must be installed and the script-write pref enabled "
            "(see tokgan_composite.py --help). For ~70 clips at 4K "
            "expect ~70 minutes total. Disk peak is ~1 GB (one clip's "
            "ProRes/Lossless intermediate, deleted after transcode)."
        ),
    )
    p.add_argument("--outputs", default=DEFAULT_OUTPUTS,
                   help=f"Outputs root. Default: {DEFAULT_OUTPUTS}")
    p.add_argument("--inputs", default=DEFAULT_INPUTS,
                   help=f"Inputs root.  Default: {DEFAULT_INPUTS}")
    p.add_argument("--force", action="store_true",
                   help="Re-render even if __composite.mp4 already exists.")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after this many pairs (for incremental testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="List discovered triples without invoking the orchestrator.")
    p.add_argument("--keep-intermediates", action="store_true",
                   help="Pass --keep-intermediates through to each per-pair run.")
    p.add_argument("--ae-timeout", type=int, default=600,
                   help="Per-pair AE timeout (s). Default 600.")
    p.add_argument("--ae-restart-every", type=int, default=10,
                   help=(
                       "Quit and relaunch AE every N successful renders to "
                       "flush AE's memory leaks across sustained batches. "
                       "Default 10. Set to 0 to disable the recycle."
                   ))
    args = p.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    orch = os.path.join(here, "tokgan_composite.py")
    if not os.path.isfile(orch):
        sys.exit(f"ERROR: per-pair orchestrator not found: {orch}")

    triples = list(find_triples(args.outputs, args.inputs))
    pairs_to_run = triples[:args.limit] if args.limit else triples
    print(f"Discovered {len(triples)} triples; running {len(pairs_to_run)}.")

    if args.dry_run:
        for i, (iv, j, v) in enumerate(pairs_to_run, 1):
            print(
                f"  [{i:>3}] input={os.path.basename(iv)}"
                f"  json={os.path.basename(j)}"
                f"  viz={os.path.basename(v) if v else '(none)'}"
            )
        return

    done_count = 0
    skip_count = 0
    fail_count = 0
    failures = []
    start = time.time()

    for i, (input_video, json_path, viz_video) in enumerate(pairs_to_run, 1):
        hash_dir = os.path.dirname(json_path)
        input_stem = os.path.splitext(os.path.basename(input_video))[0]
        composite_mp4 = os.path.join(hash_dir, input_stem + "__composite.mp4")

        print(f"\n=== [{i}/{len(pairs_to_run)}] {input_stem} ===")
        if os.path.isfile(composite_mp4) and not args.force:
            print(f"  SKIP: already done ({composite_mp4})")
            skip_count += 1
            continue

        cmd = [
            sys.executable, orch,
            "--input-video", input_video,
            "--json", json_path,
            "--ae-timeout", str(args.ae_timeout),
        ]
        if viz_video:
            cmd += ["--viz-video", viz_video]
        if args.keep_intermediates:
            cmd += ["--keep-intermediates"]

        try:
            subprocess.run(cmd, check=True)
            done_count += 1
        except subprocess.CalledProcessError as e:
            print(f"  FAIL: orchestrator returned {e.returncode}")
            fail_count += 1
            failures.append(input_stem)
            # Continue with the next pair rather than aborting the batch.

        # Memory hygiene: AE accumulates leaks across sustained renders
        # and starts crashing after N clips in a row. Forcibly quit it
        # every `--ae-restart-every` successes so it relaunches fresh
        # on the next osascript dispatch. 0 disables the recycle.
        if (args.ae_restart_every > 0
                and done_count > 0
                and done_count % args.ae_restart_every == 0
                and i < len(pairs_to_run)):
            print(f"  [recycle] {done_count} clips since last AE restart — "
                  f"quitting AE so it relaunches fresh")
            subprocess.run(
                ["osascript", "-e",
                 'tell application "Adobe After Effects 2025" to quit'],
                capture_output=True, timeout=30
            )
            # Wait for AE to actually exit; force-kill after 20s if hung
            for _ in range(20):
                if not subprocess.run(
                    ["pgrep", "-f",
                     "Adobe After Effects 2025.app/Contents/MacOS/After Effects"],
                    capture_output=True
                ).stdout:
                    break
                time.sleep(1)
            else:
                subprocess.run(
                    ["pkill", "-9", "-f",
                     "Adobe After Effects 2025.app/Contents/MacOS/After Effects"],
                    capture_output=True
                )
                time.sleep(2)
            print(f"  [recycle] AE quit; next clip will relaunch it")

    elapsed = time.time() - start
    print(
        f"\nBatch summary: {done_count} done, {skip_count} skipped, "
        f"{fail_count} failed in {elapsed:.0f}s"
    )
    if failures:
        print("Failed clips:")
        for s in failures:
            print(f"  - {s}")
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
