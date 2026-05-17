#!/usr/bin/env python3
"""
tokgan_json_to_ae.py — Tokgan JSON → After Effects shape-layer importer.

Produces two files:
  1. A small After Effects .jsx loader script
  2. A sidecar .json data file (placed next to the .jsx)

The loader is small and parses fast; the data file is read by AE at runtime
via JSON.parse (with an eval fallback for ExtendScript builds that lack JSON).
Path keyframes are bulk-written with Property.setValuesAtTimes() which is
dramatically faster than per-frame setValueAtTime calls on long clips.

In AE:
    File > Scripts > Run Script File... > pick the .jsx
    (the sidecar .json must sit next to the .jsx)

The loader builds ONE shape layer ("Tokgan Shapes") containing one Vector
Group per source object. Each group has a Bezier path animated frame-by-frame
plus a hold-stepped opacity track driven by which source frames the object
exists in. The loader retimes all keys to the active comp's frame rate so
each source frame lands on one comp frame regardless of the JSON's declared
fps. Motion blur is enabled on the layer.

Use `python3 tokgan_json_to_ae.py --help` for invocation details.

Coordinate convention:
    Both the JSON and AE use Y-down, origin top-left pixel coordinates.
    Bezier handles in JSON are absolute (left_x/left_y, right_x/right_y);
    AE expects them as deltas from the vertex, so we subtract.
"""

import json
import os
import sys


def round_pt(x, y, ndigits=2):
    return [round(x, ndigits), round(y, ndigits)]


def build_shape_record(obj_name, obj, height, start_frame, end_frame, fps):
    """Produce a compact dict: name, closed, times, verts, inT, outT, opacity.

    JSON coords are screen-style (Y-down, origin top-left) — same as AE —
    despite an upstream docstring claiming Nuke convention. No Y flip.
    """
    times, verts_all, in_all, out_all = [], [], [], []
    path_frames = []  # source frame numbers that produced a key
    for fkey in sorted(obj.get("frames", {}).keys(), key=int):
        pts = obj["frames"][fkey].get("points", [])
        if not pts:
            continue
        verts, in_t, out_t = [], [], []
        for p in pts:
            x, y = p["x"], p["y"]
            verts.append(round_pt(x, y))
            if "left_x" in p:
                in_t.append(round_pt(p["left_x"] - x, p["left_y"] - y))
            else:
                in_t.append([0.0, 0.0])
            if "right_x" in p:
                out_t.append(round_pt(p["right_x"] - x, p["right_y"] - y))
            else:
                out_t.append([0.0, 0.0])
        t = (int(fkey) - start_frame) / fps
        times.append(round(t, 6))
        verts_all.append(verts)
        in_all.append(in_t)
        out_all.append(out_t)
        path_frames.append(int(fkey))

    # Opacity: drive 0 when the shape doesn't exist in a source frame, 100
    # when it does. The shape "exists" iff visibility[f]==1 (preferred) or
    # iff f has path data (fallback when visibility is absent). Each visible
    # segment emits four hold-stepped keys so AE shows a clean on/off step.
    visibility = obj.get("visibility", {})
    if visibility:
        exists = sorted(int(k) for k, v in visibility.items() if v)
    else:
        exists = list(path_frames)

    opacity = []
    if exists:
        # Build contiguous segments
        segments = []
        s = exists[0]; e = exists[0]
        for f in exists[1:]:
            if f == e + 1:
                e = f
            else:
                segments.append((s, e))
                s, e = f, f
        segments.append((s, e))

        def _t(frame):
            return round((frame - start_frame) / fps, 6)

        raw = []
        for seg_start, seg_end in segments:
            if seg_start > start_frame:
                raw.append((_t(seg_start - 1), 0))
            raw.append((_t(seg_start), 100))
            raw.append((_t(seg_end), 100))
            if seg_end < end_frame:
                raw.append((_t(seg_end + 1), 0))
        if segments[0][0] > start_frame and not any(t == 0.0 for t, _ in raw):
            raw.append((0.0, 0))

        seen = {}
        for t, v in raw:
            seen[t] = v
        opacity = [[t, v] for t, v in sorted(seen.items())]

    return {
        "name": obj_name,
        "closed": bool(obj.get("closed", True)),
        "times": times,
        "verts": verts_all,
        "inT": in_all,
        "outT": out_all,
        "opacity": opacity,
    } if times else None


LOADER_TEMPLATE = r"""// Auto-generated AE loader for Tokgan shape data.
// Sidecar JSON ({data_basename}) is expected next to this script.
(function() {{
    var STEP = "init";
    try {{
        STEP = "locate-script";
        var scriptFile = new File($.fileName);
        var parentDir = scriptFile.parent;
        var expectedName = "{data_basename}";

        STEP = "find-sidecar";
        // Use getFiles() to enumerate the directory rather than constructing
        // a File from a path string — robust to ':' / spaces in basenames.
        var siblings = parentDir.getFiles();
        var dataFile = null;
        for (var fi = 0; fi < siblings.length; fi++) {{
            var f = siblings[fi];
            if (f instanceof File && f.displayName === expectedName) {{
                dataFile = f;
                break;
            }}
        }}
        if (!dataFile) {{
            // Fallback: ask the user to pick it.
            dataFile = File.openDialog("Locate sidecar: " + expectedName);
            if (!dataFile) return;
        }}

        STEP = "read-sidecar (" + dataFile.fsName + ", " + dataFile.length + " bytes)";
        dataFile.encoding = "UTF-8";
        if (!dataFile.open("r")) {{
            throw new Error("File.open returned false for " + dataFile.fsName);
        }}
        var raw = dataFile.read();
        dataFile.close();
        if (!raw || raw.length === 0) {{
            throw new Error("File.read returned empty string");
        }}

        STEP = "json-parse (" + raw.length + " chars)";
        var t0 = (new Date()).getTime();
        var data;
        if (typeof JSON !== "undefined" && JSON.parse) {{
            data = JSON.parse(raw);
        }} else {{
            // Polyfill: JSON is valid JS; eval inside parens.
            // Safe here because the data is produced by our own Python.
            data = eval("(" + raw + ")");
        }}
        var tParse = ((new Date()).getTime() - t0) / 1000;

        STEP = "init-comp";
        app.beginUndoGroup("Import Tokgan Shapes");

        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) {{
            comp = app.project.items.addComp(
                "TokganShapes",
                data.width, data.height, 1,
                data.duration, data.fps
            );
        }}
        comp.motionBlur = true;
        var tBuild = (new Date()).getTime();

        function mkShape(v, i, o, c) {{
            var s = new Shape();
            s.vertices = v;
            s.inTangents = i;
            s.outTangents = o;
            s.closed = c;
            return s;
        }}

        function setPathKeys(pathProp, times, shapes) {{
            try {{
                pathProp.setValuesAtTimes(times, shapes);
            }} catch (e) {{
                for (var i = 0; i < times.length; i++) {{
                    pathProp.setValueAtTime(times[i], shapes[i]);
                }}
            }}
        }}

        STEP = "create-shape-layer";
        // Remove a previous "Tokgan Shapes" layer so re-runs replace cleanly.
        for (var rl = comp.numLayers; rl >= 1; rl--) {{
            if (comp.layer(rl).name === "Tokgan Shapes") comp.layer(rl).remove();
        }}
        // Single shape layer holding every imported path as a sub-group.
        var layer = comp.layers.addShape();
        layer.name = "Tokgan Shapes";
        var root = layer.property("ADBE Root Vectors Group");

        // Re-time keys to the *comp's* frame rate so each source frame
        // lands on one comp frame, regardless of the fps the JSON declared.
        // (Common when the JSON metadata fps disagrees with the comp.)
        var fpsScale = data.fps / comp.frameRate;
        $.writeln("[Tokgan] data fps=" + data.fps + " comp fps=" + comp.frameRate + " scale=" + fpsScale);

        STEP = "build-groups";
        var shapes = data.shapes;
        for (var s = 0; s < shapes.length; s++) {{
            var sd = shapes[s];
            STEP = "group " + (s + 1) + "/" + shapes.length + " (" + sd.name + ")";

            var grp = root.addProperty("ADBE Vector Group");
            grp.name = sd.name;
            var inside = grp.property("ADBE Vectors Group");

            // AE 25.6 invalidates earlier property references when subsequent
            // addProperty calls mutate the parent group. Do ALL the addProperty
            // calls first, then walk the contents to (re)acquire the live
            // pathItem reference and from it the bezier Path child.
            inside.addProperty("ADBE Vector Shape - Group");
            inside.addProperty("ADBE Vector Graphic - Fill");
            var pathItem = null;
            for (var pp = 1; pp <= inside.numProperties; pp++) {{
                if (inside.property(pp).matchName === "ADBE Vector Shape - Group") {{
                    pathItem = inside.property(pp);
                    break;
                }}
            }}
            var pathProp = pathItem.property("ADBE Vector Shape");

            var shapeObjs = new Array(sd.times.length);
            var scaledTimes = new Array(sd.times.length);
            for (var i = 0; i < sd.times.length; i++) {{
                shapeObjs[i] = mkShape(sd.verts[i], sd.inT[i], sd.outT[i], sd.closed);
                scaledTimes[i] = sd.times[i] * fpsScale;
            }}

            setPathKeys(pathProp, scaledTimes, shapeObjs);

            // Per-group opacity for visibility — lives under the group's
            // Transform, NOT layer.transform (which would affect everything).
            if (sd.opacity.length > 0) {{
                var op = grp.property("ADBE Vector Transform Group").property("ADBE Vector Group Opacity");
                for (var j = 0; j < sd.opacity.length; j++) {{
                    op.setValueAtTime(sd.opacity[j][0] * fpsScale, sd.opacity[j][1]);
                    op.setInterpolationTypeAtKey(
                        op.numKeys,
                        KeyframeInterpolationType.HOLD,
                        KeyframeInterpolationType.HOLD
                    );
                }}
            }}
        }}

        STEP = "layer-transform-and-motion-blur";
        // Put the layer's origin at comp (0,0) so absolute pixel coords in
        // the path data line up with comp coords (no half-resolution shift).
        layer.transform.anchorPoint.setValue([0, 0]);
        layer.transform.position.setValue([0, 0]);
        layer.motionBlur = true;

        var tDone = ((new Date()).getTime() - tBuild) / 1000;
        app.endUndoGroup();

        $.writeln(
            "[Tokgan import] " + shapes.length + " layers; " +
            "JSON parse " + tParse.toFixed(2) + "s; " +
            "AE build " + tDone.toFixed(2) + "s"
        );
    }} catch (err) {{
        try {{ app.endUndoGroup(); }} catch (e2) {{}}
        alert(
            "Tokgan import failed.\n" +
            "Step: " + STEP + "\n" +
            "Error: " + err.toString() + "\n" +
            "Line:  " + (err.line || "?") + "\n" +
            "File:  " + (err.fileName || "?")
        );
    }}
}})();
"""


def main():
    import argparse
    p = argparse.ArgumentParser(
        prog="tokgan_json_to_ae",
        description=(
            "Convert a Tokgan JSON shape file into an After Effects loader "
            "(.jsx) plus a compact sidecar data JSON. Run the resulting "
            ".jsx in AE via File > Scripts > Run Script File... to build a "
            "single shape layer holding one animated Bezier path per object."
        ),
        epilog=(
            "Notes: each source frame lands on one comp frame regardless of "
            "the JSON's declared fps. Enable 'Allow Scripts to Write Files "
            "and Access Network' in AE's preferences (Scripting & "
            "Expressions) before running the loader."
        ),
    )
    p.add_argument("input", help="Path to input Tokgan JSON file")
    p.add_argument(
        "output",
        nargs="?",
        help=(
            "Path to write the AE loader .jsx (default: same dir/name as "
            "the input with .jsx extension). The sidecar will be written "
            "next to it as <output_stem>_data.json."
        ),
    )
    p.add_argument(
        "-f", "--force",
        action="store_true",
        help="Rebuild the sidecar data file even if it's newer than the input.",
    )
    args = p.parse_args()

    in_path = args.input
    out_jsx = args.output or os.path.splitext(in_path)[0] + ".jsx"
    out_data = os.path.splitext(out_jsx)[0] + "_data.json"

    # Skip the expensive data-file rebuild if it already exists and is at
    # least as new as the input — only the loader template needs regenerating
    # for iterations on the AE-side script.
    data_is_current = (
        not args.force
        and os.path.exists(out_data)
        and os.path.getmtime(out_data) >= os.path.getmtime(in_path)
    )

    if data_is_current:
        print(f"Reusing existing data file (newer than input): {out_data}")
        n_shapes = "?"  # not loaded; reported as ? in the summary
    else:
        with open(in_path) as f:
            data = json.load(f)

        res = data.get("resolution", [data.get("width", 2160), data.get("height", 4096)])
        width, height = int(res[0]), int(res[1])
        fps = float(data.get("fps", 24))

        all_frames = set()
        for obj in data["objects"].values():
            for fkey in obj.get("frames", {}).keys():
                all_frames.add(int(fkey))
        start_frame = min(all_frames) if all_frames else 1
        end_frame = max(all_frames) if all_frames else 48
        n_frames = end_frame - start_frame + 1
        duration = n_frames / fps

        shapes = []
        for obj_name, obj in data["objects"].items():
            rec = build_shape_record(obj_name, obj, height, start_frame, end_frame, fps)
            if rec:
                shapes.append(rec)

        payload = {
            "width": width,
            "height": height,
            "fps": fps,
            "duration": duration,
            "shapes": shapes,
        }

        with open(out_data, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        n_shapes = len(shapes)

    loader = LOADER_TEMPLATE.format(data_basename=os.path.basename(out_data))
    with open(out_jsx, "w") as f:
        f.write(loader)

    print(f"Wrote {out_jsx}  ({os.path.getsize(out_jsx):,} bytes)")
    print(f"Data {out_data}  ({os.path.getsize(out_data):,} bytes)")
    print(f"  Shapes: {n_shapes}")


if __name__ == "__main__":
    main()
