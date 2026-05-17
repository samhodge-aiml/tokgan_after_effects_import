# tokgan_ae_import

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
usage: tokgan_json_to_ae [-h] [-f] input [output]

Convert a Tokgan JSON shape file into an After Effects loader (.jsx) plus a
compact sidecar data JSON. Run the resulting .jsx in AE via File > Scripts >
Run Script File... to build a single shape layer holding one animated Bezier
path per object.

positional arguments:
  input        Path to input Tokgan JSON file
  output       Path to write the AE loader .jsx (default: same dir/name as
               the input with .jsx extension).

options:
  -h, --help   show this help message and exit
  -f, --force  Rebuild the sidecar data file even if it's newer than the input.
```

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
tokgan_ae_import/
├── README.md
├── LICENSE                                       MIT
├── tokgan_json_to_ae.py                          The converter
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
