#!/usr/bin/env python3
"""
tokgan_composite.py — End-to-end shell-driven AE composite render.

For one (input_video, shape_json) pair:

  1. ffprobe the input video for fps / dimensions / exact stream
     duration (NOT the container's format.duration, which can over-
     report by a frame or two and cause AE to render a tail of held
     frames after the footage ends).
  2. Generate a composite-mode .jsx + sidecar via tokgan_json_to_ae.py
     with --auto-render and --output-mov. The .jsx will build the
     TokganComposite comp (footage at bottom; per-person-coloured
     shape layer on top in MULTIPLY blend; motion blur centred via
     shutter phase -90), add a render queue item, and call
     renderQueue.render() in-process.
  3. Drive AE via `osascript ... DoScriptFile ...`. AppleScript
     DoScript is synchronous, and AE's renderQueue.render() blocks
     until done, so this single subprocess waits for the .mov to
     land on disk before returning.
  4. ffmpeg-transcode the AE-written .mov (ProRes or Lossless,
     whichever output module template was available) to H.264 mp4.
  5. Delete the .mov immediately (multi-GB on 4K clips). Also delete
     the sidecar and the .aep unless --keep-intermediates is set.
  6. Optionally also relabel a visualisation video's declared fps to
     match the input video's fps, no recompression.

The result is a single .mp4 deliverable per pair, end-to-end, with
no manual After Effects interaction.

Cleanup side-mode (manual cleanup after a partial / failed run):

  python3 tokgan_composite.py --cleanup PATH.jsx

Deletes the sidecar (_data.json) and .aep that sit next to the named
.jsx, gated on the matching .mp4 being present and at least 1 MB.
"""

import argparse
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys


SAFETY_RENDER_MIN_BYTES = 1_000_000


# ----------------------------- ffprobe ---------------------------------

def _ffmpeg_cfr_frame_count(path):
    """Count frames the way `ffmpeg -i in.mp4 out%04d.png` would —
    constant-frame-rate output padded to the container duration.
    `-vsync cfr -f null -` produces the same count without writing
    any files. This is the count Tokgan's mp4 -> EXR pipeline uses,
    so it's also the count the JSON shape data is keyed on.
    """
    out = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", path,
            "-map", "0:v:0", "-vsync", "cfr", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=False,
    )
    # ffmpeg writes its progress to stderr; the final line has the
    # total frame count.
    import re
    matches = re.findall(r"frame=\s*(\d+)", out.stderr)
    if not matches:
        raise RuntimeError(f"ffmpeg frame count failed for {path}: {out.stderr[-500:]}")
    return int(matches[-1])


def ffprobe_video(path):
    """Return dict(width, height, fps, duration, nb_frames).

    Duration is taken from the video stream (not format), because the
    container's format.duration over-reports by up to a few frames on
    Pexels mp4s — which would cause AE to render extra held frames
    after the footage ends. nb_frames comes from stream metadata
    without decoding the whole file; verified against -count_frames
    on the pilot clip.
    """
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    out = subprocess.check_output(cmd, text=True)
    info = json.loads(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = video["r_frame_rate"].split("/")
    fps = float(num) / float(den)

    # Use the CFR ffmpeg-extracted frame count, NOT the video
    # stream's nb_frames metadata. They differ when the container's
    # duration is longer than the video stream's duration — ffmpeg's
    # PNG extraction (and Tokgan's mp4 -> EXR pipeline, and players
    # like VMRV2 / QuickTime) pad to the container duration, so the
    # JSON shape data covers the padded count, not the raw decoded
    # count. Aligning on the padded count keeps shapes in sync with
    # the footage frame-by-frame.
    nb_frames = _ffmpeg_cfr_frame_count(path)
    duration = (nb_frames + 2) / fps

    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "duration": duration,
        "nb_frames": nb_frames,
    }


def _ffmpeg_rate(fps):
    """Pass exact-integer fps as 'N', non-integer as a wide decimal.
    ffmpeg accepts both; the wide decimal keeps fractional rates exact."""
    if abs(fps - round(fps)) < 1e-9:
        return str(int(round(fps)))
    return f"{fps:.10f}"


# ----------------------------- AE driver -------------------------------

def detect_ae_app():
    """Find the newest AE install under /Applications and return
    (app_name, aerender_path_or_None).

    AE 2024+ installs into the directory
    /Applications/Adobe After Effects YYYY/, with the .app bundle and
    the aerender binary as siblings inside that directory. The app
    name AppleScript expects is the directory name (also matches the
    .app's basename without the .app suffix).
    """
    install_dirs = sorted(
        glob.glob("/Applications/Adobe After Effects [0-9]*")
    )
    install_dirs = [
        d for d in install_dirs
        if os.path.isdir(d) and "Render Engine" not in d
    ]
    if not install_dirs:
        sys.exit(
            "ERROR: No 'Adobe After Effects YYYY' install found under "
            "/Applications. Pass --ae-app NAME to override."
        )
    newest = install_dirs[-1]
    app_name = os.path.basename(newest)  # e.g. "Adobe After Effects 2025"
    aerender = os.path.join(newest, "aerender")
    return app_name, (aerender if os.path.isfile(aerender) else None)


def osascript_do_script_file(ae_app_name, jsx_path, marker_path, timeout_s):
    """Tell AE to run the given .jsx via AppleScript DoScriptFile,
    then poll for the .jsx's completion marker.

    AE 2025's DoScriptFile returns before the script is finished (the
    AppleScript bridge in AE 25.x is effectively async despite the
    docs). We can't trust osascript's exit to mean "render done", so
    the .jsx writes `marker_path` at the end (empty on success;
    non-empty body on script error) and we poll for it.

    First-launch dialogs ('Show Home Screen', 'Send Anonymous Usage
    Data?', etc.) block AE before our script ever runs. Disable them
    once in AE's Settings > General — see README.
    """
    import time

    # Idempotency: clear any stale marker before dispatch so polling
    # can't false-positive on a previous run's artefact.
    if os.path.isfile(marker_path):
        os.remove(marker_path)

    applescript = (
        f'tell application "{ae_app_name}"\n'
        f'    activate\n'
        f'    DoScriptFile "{jsx_path}"\n'
        f'end tell'
    )
    print(f"$ osascript -e <DoScriptFile {jsx_path!r} in {ae_app_name!r}>")
    proc = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(
            f"ERROR: osascript dispatch failed (rc={proc.returncode}).\n"
            f"stderr:\n{proc.stderr.strip()}"
        )

    deadline = time.time() + timeout_s
    print(f"Waiting up to {timeout_s}s for AE to complete (marker: {marker_path})...")
    last_print = 0
    while time.time() < deadline:
        if os.path.isfile(marker_path):
            with open(marker_path) as f:
                body = f.read().strip()
            if body:
                sys.exit(
                    "ERROR: AE script reported failure:\n" + body
                )
            print(f"AE finished after {int(time.time() - (deadline - timeout_s))}s")
            return
        now = time.time()
        if now - last_print >= 15:
            print(f"  ...still waiting ({int(deadline - now)}s left)")
            last_print = now
        time.sleep(2)
    sys.exit(
        f"ERROR: timed out after {timeout_s}s waiting for AE marker.\n"
        f"Likely AE is still showing first-launch dialogs — dismiss them, "
        f"then check Settings > General > Show Home Screen on Startup."
    )


# ----------------------------- ffmpeg ----------------------------------

def transcode_to_h264(mov_path, mp4_path, crf=16, vframes=None):
    """Transcode the AE intermediate (.mov, ProRes or Lossless) to a
    streamable H.264 mp4. Audio passes through as AAC if present;
    ffmpeg silently ignores -c:a when there's no audio stream.

    `vframes` caps the output frame count. The AE side renders one
    extra frame past the visible target so motion blur on the actual
    last visible frame has a 'future' keyframe to interpolate against;
    -vframes trims that extra here.

    TODO: occasionally the trimmed final frame still shows asymmetric
    motion blur when the input video stops abruptly (the second-to-
    last footage frame's exposure window reaches into the trimmed
    extra). Consider rendering N+2 and trimming both ends if/when
    this becomes a quality issue. For now the user can manually
    trim the last frame in post.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", mov_path,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", "192k",
    ]
    if vframes is not None:
        cmd += ["-vframes", str(vframes)]
    cmd.append(mp4_path)
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)


def resample_input_to_viz_timing(input_path, viz_fps, viz_nb_frames, out_path):
    """Re-encode the input video to viz's fps + frame count so the JSON
    shape data (which was generated from the viz's frame sequence via
    mp4 -> EXR -> rotobot) aligns frame-for-frame with the footage we
    feed into AE.

    ffmpeg's `fps={viz_fps}` filter drops or duplicates frames as
    needed; `-frames:v viz_nb_frames` trims/pads to the exact count.
    Encoded with x264 at CRF 18 (visually lossless) using `veryfast`
    preset since this is a throwaway intermediate.
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", f"fps={_ffmpeg_rate(viz_fps)}",
        "-frames:v", str(viz_nb_frames),
        "-c:v", "libx264", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        out_path,
    ]
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)


def relabel_viz_fps(viz_path, target_fps, out_path, target_nb_frames=None):
    """No-recompression relabel of a viz video to the target fps.

    `ffmpeg -r N -i ... -c copy ...` alone does NOT actually change the
    container's reported r_frame_rate — it only re-mux remarks PTS,
    leaving the muxed fps tag unchanged. Going through the raw H.264
    elementary stream (h264_mp4toannexb) loses the container's timing
    metadata, then we re-mux at the target fps which actually sets
    r_frame_rate on the output.

    If `target_nb_frames` is given, also trim the relabelled output to
    that many frames so it matches the input video's duration exactly
    (viz often has 1-3 trailing frames from Tokgan's mp4 -> EXR pipe).
    """
    import tempfile
    tmp_h264 = tempfile.mktemp(suffix=".h264", prefix="tokgan_relabel_")
    try:
        step1 = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", viz_path,
            "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
            "-f", "h264", tmp_h264,
        ]
        print("$ " + " ".join(shlex.quote(c) for c in step1))
        subprocess.run(step1, check=True)

        step2 = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-r", _ffmpeg_rate(target_fps),
            "-i", tmp_h264,
            "-c", "copy",
        ]
        if target_nb_frames is not None:
            step2 += ["-frames:v", str(target_nb_frames)]
        step2.append(out_path)
        print("$ " + " ".join(shlex.quote(c) for c in step2))
        subprocess.run(step2, check=True)
    finally:
        if os.path.exists(tmp_h264):
            os.remove(tmp_h264)


# ----------------------------- main flow -------------------------------

def composite_mode(args):
    if not os.path.isfile(args.input_video):
        sys.exit(f"ERROR: --input-video not found: {args.input_video}")
    if not os.path.isfile(args.json):
        sys.exit(f"ERROR: --json not found: {args.json}")
    if args.viz_video and not os.path.isfile(args.viz_video):
        sys.exit(f"ERROR: --viz-video not found: {args.viz_video}")

    ae_app_name = args.ae_app or detect_ae_app()[0]
    if not shutil.which("osascript"):
        sys.exit("ERROR: osascript not on PATH (macOS only).")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ERROR: ffmpeg / ffprobe must be on PATH.")

    info = ffprobe_video(args.input_video)
    input_stem = os.path.splitext(os.path.basename(args.input_video))[0]
    print(f"Input video: {args.input_video}")
    print(
        f"  {info['width']}x{info['height']} @ {info['fps']:.6g} fps, "
        f"{info['nb_frames']} frames = {info['duration']:.6f}s"
    )

    # If a viz video is given, IT is the timing authority — the JSON
    # shape data was generated from its frame sequence, so any drift
    # between input fps/frames and viz fps/frames will show as
    # shape-vs-footage misalignment. Resample the input to match viz.
    if args.viz_video:
        viz_info = ffprobe_video(args.viz_video)
        print(f"Viz video:   {args.viz_video}")
        print(
            f"  {viz_info['width']}x{viz_info['height']} @ "
            f"{viz_info['fps']:.6g} fps, {viz_info['nb_frames']} frames"
        )
        if (viz_info["fps"] == info["fps"]
                and viz_info["nb_frames"] == info["nb_frames"]):
            print("  input already matches viz timing; no resample needed")
            footage_path = args.input_video
        else:
            print("  resampling input -> viz timing")
            # Use a stem-unique temp name so AE's session-restore on
            # a previous clip's .aep never points at the same /tmp
            # path that the current clip's cleanup just deleted (which
            # would show a "missing footage" dialog and block the
            # script).
            footage_path = os.path.join(
                "/tmp", f"tokgan_resampled_{input_stem}.mp4"
            )
            resample_input_to_viz_timing(
                args.input_video, viz_info["fps"], viz_info["nb_frames"],
                footage_path,
            )
        canonical_fps = viz_info["fps"]
        canonical_nb_frames = viz_info["nb_frames"]
        canonical_duration = viz_info["nb_frames"] / viz_info["fps"]
    else:
        footage_path = args.input_video
        canonical_fps = info["fps"]
        canonical_nb_frames = info["nb_frames"]
        canonical_duration = info["duration"]

    print(f"AE app:      {ae_app_name}")

    out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.json))
    os.makedirs(out_dir, exist_ok=True)

    composite_stem = input_stem + "__composite"
    jsx_path    = os.path.join(out_dir, composite_stem + ".jsx")
    sidecar     = os.path.join(out_dir, composite_stem + "_data.json")
    aep_path    = os.path.join(out_dir, composite_stem + ".aep")
    mov_path    = os.path.join(out_dir, composite_stem + ".mov")
    mp4_path    = os.path.join(out_dir, composite_stem + ".mp4")
    marker_path = os.path.join(out_dir, composite_stem + ".done")

    # Stale-output cleanup: re-runs should start from scratch so we
    # don't half-render onto a previous .mov or trip AE's "file exists"
    # behaviour. We deliberately do NOT touch the input video / json /
    # viz video here — only our own outputs.
    for p in (mov_path, mp4_path, marker_path):
        if os.path.isfile(p):
            os.remove(p)

    # 1. Generate .jsx + sidecar with auto-render configured.
    here = os.path.dirname(os.path.abspath(__file__))
    converter = os.path.join(here, "tokgan_json_to_ae.py")
    # Default shift is 0 across all fps once the input has been
    # CFR-resampled to viz timing. The 60-fps-class -2 rule was
    # observed under the OLD frame-counting (raw stream nb_frames)
    # and likely fixed a different bug; with viz-as-authority +
    # CFR counts it should no longer be needed. Pass --shape-time-shift
    # explicitly on the CLI to override per-clip.
    effective_shift = args.shape_time_shift

    cmd = [
        sys.executable, converter,
        "--force",
        "--fps", _ffmpeg_rate(canonical_fps),
        "--input-video", os.path.abspath(footage_path),
        "--shutter-angle", str(args.shutter_angle),
        "--shutter-phase", str(args.shutter_phase),
        "--duration", f"{canonical_duration:.6f}",
        "--auto-render",
        "--output-mov", mov_path,
        "--nb-frames", str(canonical_nb_frames),
        "--shape-time-shift", str(effective_shift),
        os.path.abspath(args.json),
        jsx_path,
    ]
    if args.shape_time_shift_seconds is not None:
        cmd += ["--shape-time-shift-seconds", str(args.shape_time_shift_seconds)]
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)

    # 2. Optional viz-video fps relabel (runs in parallel with the AE
    # render — but we keep it serial for simpler error handling).
    # Viz relabel removed: the viz IS the canonical timing, and we
    # resampled the input to match it above, so no per-clip relabel
    # is meaningful. The original viz file in outputs/{hash}/ already
    # plays back at the canonical fps.

    # 3. Drive AE end-to-end (build + queue + render in-process).
    print()
    print(f"--- Driving {ae_app_name} via osascript DoScriptFile ---")
    osascript_do_script_file(ae_app_name, jsx_path, marker_path, args.ae_timeout)
    # Marker has been consumed; remove it so cleanup is tidy.
    if os.path.isfile(marker_path):
        os.remove(marker_path)

    if not os.path.isfile(mov_path):
        sys.exit(
            f"ERROR: AE returned but the expected .mov was not written:\n"
            f"  {mov_path}\n"
            "Check AE for an error dialog or the ExtendScript console."
        )
    mov_size = os.path.getsize(mov_path)
    print(f"AE wrote {mov_path}  ({mov_size:,} bytes)")

    # 4. Transcode the intermediate to H.264 mp4. The AE-side render
    # produced canonical_nb_frames + 1 frames so the *visible* last
    # frame has proper motion-blur sampling; -vframes trims that
    # extra here so the output exactly matches the viz's frame count
    # (the timing authority).
    print()
    print("--- Transcoding intermediate to H.264 mp4 (trim to "
          f"{canonical_nb_frames} frames) ---")
    transcode_to_h264(mov_path, mp4_path, crf=args.mp4_crf,
                      vframes=canonical_nb_frames)

    if not os.path.isfile(mp4_path):
        sys.exit(f"ERROR: ffmpeg returned 0 but {mp4_path} is missing.")
    mp4_size = os.path.getsize(mp4_path)
    print(f"Wrote {mp4_path}  ({mp4_size:,} bytes)")

    # 5. Cleanup. .mov is the multi-GB intermediate — always deleted on
    # success. Sidecar + .aep are deleted unless --keep-intermediates.
    print()
    print("--- Cleanup ---")
    reclaimed = mov_size
    os.remove(mov_path)
    print(f"Deleted {mov_path}  ({mov_size:,} bytes)")

    if args.keep_intermediates:
        print(f"Kept sidecar + .aep (--keep-intermediates)")
    else:
        for target in (sidecar, aep_path):
            if os.path.isfile(target):
                size = os.path.getsize(target)
                os.remove(target)
                reclaimed += size
                print(f"Deleted {target}  ({size:,} bytes)")

    # Also delete the resampled-input temp file if we created one
    if args.viz_video and footage_path.startswith("/tmp/") and os.path.isfile(footage_path):
        size = os.path.getsize(footage_path)
        os.remove(footage_path)
        reclaimed += size
        print(f"Deleted {footage_path}  ({size:,} bytes, resampled input)")

    print(f"Reclaimed {reclaimed:,} bytes")
    print()
    print(f"DONE  ->  {mp4_path}")


def cleanup_mode(jsx_path):
    if not jsx_path.endswith(".jsx"):
        sys.exit("ERROR: --cleanup expects a .jsx path")
    if not os.path.isfile(jsx_path):
        sys.exit(f"ERROR: not a file: {jsx_path}")

    stem = os.path.splitext(jsx_path)[0]
    sidecar = stem + "_data.json"
    aep = stem + ".aep"
    mov = stem + ".mov"
    mp4 = stem + ".mp4"

    if not os.path.isfile(mp4):
        sys.exit(
            f"ERROR: refusing to clean up - render output missing: {mp4}\n"
            "Run the orchestrator first; it cleans up on success."
        )
    mp4_size = os.path.getsize(mp4)
    if mp4_size < SAFETY_RENDER_MIN_BYTES:
        sys.exit(
            f"ERROR: refusing to clean up - render output too small "
            f"({mp4_size:,} bytes < {SAFETY_RENDER_MIN_BYTES:,}): {mp4}\n"
            "This usually means the render did not finish."
        )

    reclaimed = 0
    for target in (sidecar, aep, mov):
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
            "End-to-end shell-driven AE composite render. Builds the "
            "TokganComposite comp (input video + per-person shape layer "
            "in MULTIPLY blend, motion blur centred via -90 shutter "
            "phase), drives AE via osascript to render, then transcodes "
            "to H.264 mp4 and cleans up. No manual AE interaction."
        ),
        epilog=(
            "Requires: macOS, ffmpeg + ffprobe on PATH, Adobe After "
            "Effects YYYY installed under /Applications. The AE app is "
            "auto-detected (newest version) and brought to the "
            "foreground by AppleScript while it renders."
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
            "Comp shutter phase in degrees. Default -90 - pairs with "
            "the default 180 shutter angle to centre motion blur around "
            "each source frame instead of trailing it."
        ),
    )
    p.add_argument(
        "--shape-time-shift",
        type=float,
        default=0.0,
        help=(
            "Shift every shape keyframe by this many SOURCE FRAMES. "
            "Default 0 — empirically 24/25/29.97 fps clips align "
            "correctly with shift=0 once the input is CFR-resampled "
            "to viz timing. 60-fps-class clips (fps >= 55) auto-apply "
            "-2 via the orchestrator's per-fps rule when this stays at "
            "the default 0."
        ),
    )
    p.add_argument(
        "--shape-time-shift-seconds",
        type=float,
        default=None,
        help=(
            "Shift every shape keyframe by this many SECONDS, "
            "overriding the auto-derived per-clip shift. Leave unset "
            "to use the per-clip slack-based default (--shape-time-shift-auto)."
        ),
    )
    p.add_argument(
        "--mp4-crf",
        type=int,
        default=16,
        help="x264 CRF for the H.264 mp4 transcode. Default 16 (visually lossless).",
    )
    p.add_argument(
        "--keep-intermediates",
        action="store_true",
        help=(
            "Keep the sidecar (_data.json) and .aep after a successful "
            "render. The .mov is always deleted because it's multi-GB on "
            "4K clips and the .mp4 supersedes it."
        ),
    )
    p.add_argument(
        "--ae-app",
        default=None,
        help=(
            "Override AE app name (e.g. 'Adobe After Effects 2025'). "
            "Default: newest match under /Applications."
        ),
    )
    p.add_argument(
        "--ae-timeout",
        type=int,
        default=600,
        help=(
            "Seconds to wait for AE to finish (launch + build + "
            "render). Default 600. Bump higher for very long clips."
        ),
    )
    p.add_argument(
        "--cleanup",
        metavar="PATH.jsx",
        default=None,
        help=(
            "Side-mode for manual cleanup of a partial / failed run: "
            "delete the sidecar (_data.json), .aep, and .mov next to "
            "the named .jsx. Refuses unless the matching .mp4 exists "
            f"and is >= {SAFETY_RENDER_MIN_BYTES:,} bytes."
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
