#!/usr/bin/env python3
"""
tokgan_composite.py — Per-pair pilot orchestrator for the AE composite render.

For one (input_video, shape_json) pair:

  1. ffprobes the input video for fps / dimensions / duration.
  2. Invokes tokgan_json_to_ae.py with --input-video / --fps /
     --shutter-angle / --shutter-phase / --duration so the generated
     .jsx + sidecar build a TokganComposite comp in AE (footage layer
     at the bottom; shape layer on top in MULTIPLY blend; motion blur
     centred via shutter phase -90).
  3. Optionally relabels a visualisation video's fps to match the input
     video without recompression (ffmpeg -r N -i ... -c copy ...).
  4. Prints AE next-step instructions plus the cleanup command.

The pilot does NOT auto-trigger the AE render — the user runs the .jsx
manually in AE, verifies the comp, then renders via the Render Queue.
A future batch driver will instead drive aerender headlessly.

Cleanup side-mode:

  python3 tokgan_composite.py --cleanup PATH.jsx

Deletes the sidecar (_data.json) and .aep that sit next to the named
.jsx, but only when the corresponding .mp4 is present and at least 1 MB
(the heuristic for "the render actually completed"). Always reports
bytes reclaimed.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys


SAFETY_RENDER_MIN_BYTES = 1_000_000


def ffprobe_video(path):
    """Return dict(width, height, fps_float, duration_seconds)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    out = subprocess.check_output(cmd, text=True)
    info = json.loads(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = video["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    duration = float(info["format"].get("duration") or video.get("duration") or 0.0)
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "duration": duration,
    }


def relabel_viz_fps(viz_path, target_fps, out_path):
    """ffmpeg -r N -i in.mp4 -c copy out.mp4 — relabels the declared
    frame rate without re-encoding. -r BEFORE -i reinterprets the input
    cadence; -c copy muxes the existing H.264 stream unchanged."""
    cmd = [
        "ffmpeg", "-y",
        "-r", _ffmpeg_rate(target_fps),
        "-i", viz_path,
        "-c", "copy",
        out_path,
    ]
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)


def _ffmpeg_rate(fps):
    """Pass exact-integer fps as 'N', non-integer as 'N/1' fraction.
    ffmpeg accepts both but the rational form preserves precision."""
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.10f}"


def composite_mode(args):
    if not os.path.isfile(args.input_video):
        sys.exit(f"ERROR: --input-video not found: {args.input_video}")
    if not os.path.isfile(args.json):
        sys.exit(f"ERROR: --json not found: {args.json}")
    if args.viz_video and not os.path.isfile(args.viz_video):
        sys.exit(f"ERROR: --viz-video not found: {args.viz_video}")

    info = ffprobe_video(args.input_video)
    print(f"Input video: {args.input_video}")
    print(
        f"  {info['width']}x{info['height']} @ {info['fps']:.6g} fps, "
        f"{info['duration']:.3f}s"
    )

    out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.json))
    os.makedirs(out_dir, exist_ok=True)

    input_stem = os.path.splitext(os.path.basename(args.input_video))[0]
    composite_stem = input_stem + "__composite"
    jsx_path = os.path.join(out_dir, composite_stem + ".jsx")
    expected_mp4 = os.path.join(out_dir, composite_stem + ".mp4")

    here = os.path.dirname(os.path.abspath(__file__))
    converter = os.path.join(here, "tokgan_json_to_ae.py")
    cmd = [
        sys.executable, converter,
        "--force",
        "--fps", _ffmpeg_rate(info["fps"]),
        "--input-video", os.path.abspath(args.input_video),
        "--shutter-angle", str(args.shutter_angle),
        "--shutter-phase", str(args.shutter_phase),
        "--duration", f"{info['duration']:.6f}",
        os.path.abspath(args.json),
        jsx_path,
    ]
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)

    if args.viz_video:
        viz_stem = os.path.splitext(os.path.basename(args.viz_video))[0]
        relabel_out = os.path.join(out_dir, viz_stem + "__relabeled.mp4")
        relabel_viz_fps(args.viz_video, info["fps"], relabel_out)
        print(f"Wrote {relabel_out}  ({os.path.getsize(relabel_out):,} bytes)")

    print()
    print("Next steps in After Effects:")
    print(f"  1. File > Scripts > Run Script File...  ->  {jsx_path}")
    print(f"     (the .jsx imports footage, builds TokganComposite, saves "
          f"{composite_stem}.aep)")
    print( "  2. Composition > Add to Render Queue, choose H.264 output,")
    print(f"     render to {expected_mp4}")
    print( "  3. Play back the rendered mp4 and confirm:")
    print( "     - per-person hues multiply over the footage")
    print( "     - motion blur is centred on each frame (shutter phase -90)")
    print(f"     - playback matches the input video at {info['fps']:.6g} fps")
    print()
    print(f"When done: python3 {os.path.basename(sys.argv[0])} --cleanup {jsx_path}")
    print(f"  (deletes sidecar + .aep once {composite_stem}.mp4 is on disk)")


def cleanup_mode(jsx_path):
    if not jsx_path.endswith(".jsx"):
        sys.exit("ERROR: --cleanup expects a .jsx path")
    if not os.path.isfile(jsx_path):
        sys.exit(f"ERROR: not a file: {jsx_path}")

    stem = os.path.splitext(jsx_path)[0]
    sidecar = stem + "_data.json"
    aep = stem + ".aep"
    mp4 = stem + ".mp4"

    if not os.path.isfile(mp4):
        sys.exit(
            f"ERROR: refusing to clean up — render output missing: {mp4}\n"
            "Render the comp in AE first, then re-run --cleanup."
        )
    mp4_size = os.path.getsize(mp4)
    if mp4_size < SAFETY_RENDER_MIN_BYTES:
        sys.exit(
            f"ERROR: refusing to clean up — render output too small "
            f"({mp4_size:,} bytes < {SAFETY_RENDER_MIN_BYTES:,}): {mp4}\n"
            "This usually means the render did not finish."
        )

    reclaimed = 0
    for target in (sidecar, aep):
        if os.path.isfile(target):
            size = os.path.getsize(target)
            os.remove(target)
            reclaimed += size
            print(f"Deleted {target}  ({size:,} bytes)")
        else:
            print(f"Skipped {target}  (not present)")
    print(f"Reclaimed {reclaimed:,} bytes")


def main():
    p = argparse.ArgumentParser(
        prog="tokgan_composite",
        description=(
            "Build an AE composite (input video + Tokgan shape layer in "
            "MULTIPLY blend, motion blur centred via -90 shutter phase) "
            "for a single pair of files. Generates the .jsx + sidecar via "
            "tokgan_json_to_ae.py with --input-video; optionally relabels "
            "a visualisation video's declared fps without re-encoding."
        ),
        epilog=(
            "Pilot workflow: run this tool, then in AE run the generated "
            ".jsx, then render via Render Queue. After validating the "
            "rendered mp4, run --cleanup to reclaim the sidecar + .aep "
            "(both can be 10s of MB on 4K clips)."
        ),
    )
    p.add_argument("--input-video", help="Path to input video (mp4).")
    p.add_argument("--json", help="Path to Tokgan shape JSON.")
    p.add_argument(
        "--viz-video",
        default=None,
        help=(
            "Optional path to the visualisation video whose declared fps "
            "should be relabelled (no re-encoding) to match the input "
            "video's fps. Output is named <viz_stem>__relabeled.mp4 next "
            "to the JSON."
        ),
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help="Where to write outputs. Default: same directory as --json.",
    )
    p.add_argument(
        "--shutter-angle",
        type=float,
        default=180.0,
        help="Comp shutter angle in degrees. Default 180 (standard cinema).",
    )
    p.add_argument(
        "--shutter-phase",
        type=float,
        default=-90.0,
        help=(
            "Comp shutter phase in degrees. Default -90 — pairs with the "
            "default 180 shutter angle to centre motion blur around each "
            "source frame instead of trailing it."
        ),
    )
    p.add_argument(
        "--cleanup",
        metavar="PATH.jsx",
        default=None,
        help=(
            "Side-mode: delete the sidecar (_data.json) and .aep next to "
            "the named .jsx. Refuses unless the matching .mp4 exists and "
            f"is >= {SAFETY_RENDER_MIN_BYTES:,} bytes (sanity gate that "
            "the render actually completed). Always reports bytes "
            "reclaimed."
        ),
    )
    args = p.parse_args()

    if args.cleanup:
        cleanup_mode(args.cleanup)
        return

    missing = []
    if not args.input_video:
        missing.append("--input-video")
    if not args.json:
        missing.append("--json")
    if missing:
        p.error("missing required: " + ", ".join(missing))

    composite_mode(args)


if __name__ == "__main__":
    main()
