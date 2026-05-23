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

## Composite render (pilot)

`tokgan_composite.py` is a per-pair orchestrator that builds a full
multiply-blend composite of the coloured shape layer over an input
video, ready to render out of AE at the input video's true frame rate.
It also relabels a matching visualisation video's declared fps without
re-encoding.

```
python3 tokgan_composite.py \
    --input-video pexels_7327400_3840x2160_27s.mp4 \
    --json "pexels_7327400_3840x2160_27s__2026-05-22 23:48:36.json" \
    --viz-video "pexels_7327400_..._auto-depth-prepass.mp4"
```

What happens:

1. `ffprobe` reads the input video's width/height/fps/duration.
2. `tokgan_json_to_ae.py` runs with `--input-video`, `--fps {true_fps}`,
   `--shutter-angle 180`, `--shutter-phase -90`, `--duration {video_duration}`.
   This produces a *composite-mode* `.jsx` + sidecar: instead of using
   `activeItem`, the loader creates a fresh **TokganComposite** comp at
   the input video's dimensions and fps, imports the footage as the
   bottom layer, adds the Tokgan shape layer on top in MULTIPLY blend
   mode, sets shutter angle 180 / shutter phase −90, enables motion
   blur on both layers, and saves the project as `.aep` next to the
   `.jsx` (so a future headless `aerender` run can pick it up).
3. `ffmpeg -r {fps} -i viz.mp4 -c copy viz__relabeled.mp4` relabels the
   visualisation video's container fps without re-encoding (only when
   `--viz-video` is given).
4. Open the generated `.jsx` in AE via `File > Scripts > Run Script
   File…`, then render `TokganComposite` to mp4 via
   `Composition > Add to Render Queue`. The pilot does **not** trigger
   the render automatically — manual is safer for the first run so you
   can verify the comp looks right.

Once you've validated the rendered mp4 by playback, clean up the
intermediate files (sidecar + `.aep` — both can be tens of MB on 4K
clips):

```
python3 tokgan_composite.py --cleanup path/to/{stem}__composite.jsx
```

This refuses unless the matching `{stem}__composite.mp4` is on disk and
at least 1 MB (sanity check that the render actually completed).

A batch driver for the rest of the queue is deferred until the pilot
pair is visually validated — it will use `aerender` for headless
rendering (ProRes intermediate → ffmpeg-transcode to H.264, then delete
the ProRes immediately to avoid filling the disk).

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
