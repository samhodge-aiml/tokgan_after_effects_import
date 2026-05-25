# tokgan_after_effects_import

Import Tokgan ML shape data directly into Adobe After Effects as native shape
layers, bypassing the Silhouette → Adobe Premiere/AE export round-trip used
by the sibling [tokgan_silhouette_import][sib] tool.

The output is **one** AE shape layer named *Tokgan Shapes* containing one
Vector Group per source object. Each group has:

* a Bezier path animated frame-by-frame,
* a hold-stepped opacity track driven by which source frames the object
  exists in,
* a fill.

Because every group is a true shape-layer path (not a mask), AE's regular
**motion blur switch** produces sub-frame interpolated blur — which was the
original motivation. The layer is created with motion blur already enabled.

[sib]: https://github.com/samhodge-aiml/tokgan_silhouette_import

## Requirements

* Python 3.8+ (standard library only — no `pip install` step).
* After Effects 2024+ (tested on AE 25.6 / Mac).
* The AE preference **Allow Scripts to Write Files and Access Network**
  must be enabled — `Settings > Scripting & Expressions`. Without it the
  loader cannot read its sidecar JSON.

## Quick start

```bash
# Convert a JSON to an AE loader + sidecar
python3 tokgan_json_to_ae.py data/9961755_uhd_2160x4096_25fps_48frames.json

# This writes two files next to the JSON:
#   data/9961755_uhd_2160x4096_25fps_48frames.jsx
#   data/9961755_uhd_2160x4096_25fps_48frames_data.json
```

Then in After Effects:

1. Open (or create) a composition whose dimensions match the JSON's
   `resolution` field. The loader uses `app.project.activeItem` if it's a
   comp; otherwise it creates a new `TokganShapes` comp.
2. `File > Scripts > Run Script File…` and pick the generated `.jsx`.
3. Wait — large clips can take 20–60 seconds for the path-keyframe writes.
   A progress line goes to the ExtendScript Toolkit console (`$.writeln`)
   when complete:
   ```
   [Tokgan] data fps=24 comp fps=25 scale=0.96
   ```

To replace an existing import without manual cleanup, just re-run the
loader. It removes the previous *Tokgan Shapes* layer before building the
new one.

### Usage

```
$ python3 tokgan_json_to_ae.py --help
usage: tokgan_json_to_ae [-h] [-f] [--keep-background] [--fps FPS]
                         [--input-video INPUT_VIDEO]
                         [--shutter-angle SHUTTER_ANGLE]
                         [--shutter-phase SHUTTER_PHASE] [--duration DURATION]
                         input [output]
```

Key flags:

- `-f / --force` — rebuild the sidecar even if it's newer than the input.
- `--keep-background` — import "background" persons (max-over-time bbox
  dim < `max(comp_w, comp_h) / 20`) as a single neutral grey fill
  instead of dropping them.
- `--fps N` — override the JSON's declared fps. Tokgan exports stamp
  every file as `fps=24` even when the source video runs at e.g. 25 fps,
  which makes the imported shapes play ~4% faster than the footage.
- `--input-video PATH` / `--shutter-angle N` / `--shutter-phase N` /
  `--duration N` — switch to *composite mode*: the generated `.jsx`
  imports the footage, builds a `TokganComposite` comp at the video's
  dimensions/fps, drops the Tokgan shape layer on top in MULTIPLY blend,
  enables motion blur, and saves the project as `.aep` next to the
  `.jsx`. See the *Composite render* section below for the orchestrator
  that wires these flags up automatically.

## What the loader does, in order

1. Locates its sidecar `_data.json` next to itself via `parent.getFiles()`
   (avoids the ExtendScript File-constructor quirks with `:` and spaces in
   filenames).
2. Reads + parses the sidecar (`JSON.parse`, with `eval("("+raw+")")`
   fallback for ExtendScript builds that lack `JSON`).
3. Uses the active comp, or creates one with the JSON's resolution / fps /
   duration if none is active.
4. Removes any pre-existing *Tokgan Shapes* layer for clean re-runs.
5. For each source object, adds a Vector Group whose Contents holds a Path
   and a Fill. The path bezier is written in one batched
   `setValuesAtTimes(...)` call, with a per-frame fallback. The group's
   `Transform > Opacity` gets hold-stepped 0/100 keys around every visible
   segment.
6. Resets the layer's anchor and position to `(0,0)` so JSON pixel coords
   land at comp pixel coords with no half-resolution offset.
7. Scales every keyframe time by `data.fps / comp.frameRate` so each source
   frame lands on one comp frame regardless of declared fps.
8. Enables `layer.motionBlur` and `comp.motionBlur`.

## Example

`data/9961755_uhd_2160x4096_25fps_48frames.json` is a 23 MB sample from the
`tokgan_silhouette_import` repo — 48 frames at 25 fps, 63 body-part shapes
on a single subject. Run the converter, drop the `.jsx` into AE, and you'll
get a 2160 × 4096 comp populated with motion-blur-ready animated shape
paths.

The source footage for this clip is Pexels video **9961755** (UHD 2160 ×
4096, 25 fps); download from <https://www.pexels.com> and import as an
image sequence or video to match against the imported shapes.

## Compositing

To use the imported shape layer as an animated alpha matte on footage:

1. Drop your footage into the same comp, below the *Tokgan Shapes* layer.
2. On the footage layer, set the **Track Matte** column to *Tokgan Shapes*
   (defaults to Alpha Matte).

Motion blur on the shape layer carries through to the matte edges. For
naturally-centered blur, set the comp's **Shutter Phase** to half the
negative **Shutter Angle** (e.g. `-90°` when shutter is `180°`) under
`Composition Settings > Advanced`.

## Composite render (end-to-end, shell-driven)

`tokgan_composite.py` orchestrates a full multiply-blend composite of
the coloured shape layer over an input video, **with zero manual AE
interaction**. AE launches via `osascript`, builds the comp, renders
in-process, then the orchestrator transcodes the AE intermediate to
H.264 mp4 and cleans up.

```
python3 tokgan_composite.py \
    --input-video pexels_7327400_3840x2160_27s.mp4 \
    --json "pexels_7327400_3840x2160_27s__2026-05-22 23:48:36.json" \
    --viz-video "pexels_7327400_..._auto-depth-prepass.mp4"
```

What happens:

1. `ffprobe` reads the input video's resolution / fps / **exact frame
   count** (`stream.nb_frames`). The comp duration is computed as
   `(nb_frames - 1) / fps` so the render produces exactly `nb_frames`
   frames — no trailing black frame from the container's
   `format.duration` over-reporting or AE's inclusive-boundary +1.
2. `tokgan_json_to_ae.py` runs with `--input-video`, `--fps`,
   `--shutter-angle 180`, `--shutter-phase -90`, `--duration`,
   `--auto-render`, `--output-mov`. The generated *composite-mode*
   `.jsx` creates a fresh **TokganComposite** comp at the input
   video's dims/fps, imports the footage at the bottom, adds the
   shape layer on top in MULTIPLY blend, enables motion blur, adds
   a render queue item with a ProRes (or Lossless fallback) output
   module pointed at the `.mov` path, and calls
   `app.project.renderQueue.render()` in-process.
3. `osascript DoScriptFile` dispatches the `.jsx` to AE. The
   orchestrator polls for a completion marker `{stem}__composite.done`
   that the `.jsx` writes at end-of-script (empty on success, error
   body on failure). On error the marker body is surfaced as a shell
   error — no AE dialogs to dismiss.
4. `ffmpeg -r {fps} -i viz.mp4 -c copy viz__relabeled.mp4` relabels
   the visualisation video's container fps without re-encoding (only
   when `--viz-video` is given).
5. `ffmpeg` transcodes the `.mov` to H.264 `.mp4` (CRF 16, yuv420p,
   x264 slow preset, `+faststart`). The `.mov` is then deleted
   (multi-GB on 4K). Sidecar + `.aep` are also deleted unless
   `--keep-intermediates` is set.

### One-time AE setup

After Effects 2025 needs two settings enabled before the orchestrator
can drive it:

- **Settings → Scripting & Expressions → Allow Scripts to Write Files
  and Access Network** — otherwise our `.jsx` can't write the `.mov`,
  the `.aep`, or its completion marker (the orchestrator times out
  silently).
- **Settings → General → Show Home Screen on Startup** (uncheck) —
  optional; AE will run scripts even from the Home Screen, but
  disabling this lets AE launch directly to the workspace and feels
  faster.

If the Camera Raw plugin throws an error at launch (a recurring AE
2025 issue), update it via Creative Cloud Desktop App → Updates, or
disable it for our pipeline (we never import RAW images):

```
sudo mv "/Library/Application Support/Adobe/Plug-Ins/CC/File Formats/Camera Raw.plugin" \
        "/Library/Application Support/Adobe/Plug-Ins/CC/File Formats/Camera Raw.plugin.disabled"
```

### Cleanup side-mode

For a partial / failed run where the orchestrator's own auto-cleanup
didn't fire, remove intermediates manually:

```
python3 tokgan_composite.py --cleanup path/to/{stem}__composite.jsx
```

This refuses unless `{stem}__composite.mp4` is on disk and at least
1 MB (sanity gate that the render actually completed).

### Known issue (TODO)

The composite renders **N+1 frames** in AE and `ffmpeg -vframes N`
trims the extra in the transcode step, so the kept N frames each have
proper bidirectional motion-blur sampling. In rare cases the trimmed
final frame can still show a held / asymmetric blur if the input video
ends abruptly with a hard motion stop (the second-to-last footage
frame's exposure window samples into the last frame's "future" data,
which is the trimmed extra). If you spot one of these, just trim one
more frame off the rendered mp4 manually — see the TODO marker in
[tokgan_composite.py](tokgan_composite.py).

## Batch driver

`tokgan_composite_batch.py` walks a Tokgan k3s_queue layout and runs
the per-pair orchestrator for every triple it finds. Defaults are
wired for the user's local layout:

```
python3 tokgan_composite_batch.py [--limit N] [--force] [--dry-run]
```

- `--dry-run` lists discovered triples without running anything.
- `--limit N` stops after N pairs (good for incremental testing).
- `--force` re-renders even when the composite.mp4 already exists.

The driver skips pairs whose `__composite.mp4` is already on disk and
continues on per-clip failure, printing a summary (done / skipped /
failed) at the end. Disk peak stays bounded to roughly one clip's
intermediate (~1 GB on 4K) since each pair cleans up before the next
starts. Expect ~1 minute per short 4K clip end-to-end.

## AE 25.6 quirks the loader works around

While building this, I tripped on a handful of After Effects 25.6
ExtendScript quirks worth knowing about if you hack on the loader:

* `app.beginSuppressDialogs()` takes **zero** parameters in 25.6 (older
  docs say a boolean). Passing one throws `requires 0 parameters`. The
  loader doesn't call it at all.
* `JSON` is **undefined** in this ExtendScript build. The loader falls
  back to `eval("(" + raw + ")")` for the sidecar.
* AE invalidates earlier `Property` references when **sibling**
  `addProperty` calls mutate the parent group. The loader does all
  `addProperty` calls first, then re-walks `numProperties` to acquire the
  pathItem fresh — otherwise `pathProp.setValueAtTime(…)` throws
  `Object is invalid`.
* AE caches compiled `.jsx` bytecode by path. If you edit a loader and
  AE keeps replaying the old error, quit + relaunch AE or save to a
  fresh filename.
* `addProperty` / `canAddProperty` will hard-crash AE on undocumented
  match names — stick to the documented `ADBE …` strings.

## File layout

```
tokgan_after_effects_import/
├── README.md
├── LICENSE                                       MIT
├── tokgan_json_to_ae.py                          The converter
├── tokgan_composite.py                           Per-pair composite orchestrator
├── tokgan_composite_batch.py                     Queue walker / batch driver
└── data/
    └── 9961755_uhd_2160x4096_25fps_48frames.json Example input
```

After a run, two more files appear next to the input:

```
data/9961755_uhd_2160x4096_25fps_48frames.jsx        AE loader script
data/9961755_uhd_2160x4096_25fps_48frames_data.json  Sidecar (gitignored)
```

## License

MIT — see [LICENSE](LICENSE).
