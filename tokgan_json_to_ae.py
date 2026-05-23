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

import colorsys
import json
import os
import sys


# Persons whose maximum-over-time bounding-box max dimension is smaller
# than max(comp_w, comp_h) / BACKGROUND_DIM_DIVISOR are treated as
# background figures: by default dropped from the import; with
# --keep-background they are imported but share one neutral-grey fill.
BACKGROUND_DIM_DIVISOR = 20

# Bump when the sidecar payload schema changes in a way the loader
# would have to handle. Sidecars whose stored value differs from this
# constant are rebuilt on the next run regardless of mtime, so older
# sidecars without `color` (v2), `inputVideo` (v3), or `autoRender`
# (v4) don't quietly stay around.
PAYLOAD_SCHEMA = 4

BACKGROUND_GREY = [0.5, 0.5, 0.5]


def round_pt(x, y, ndigits=2):
    return [round(x, ndigits), round(y, ndigits)]


def person_id_of(obj_name, obj):
    """Canonical int person id. Prefer obj['person_id']; fall back to
    parsing the 'pK' prefix in the object's name."""
    pid = obj.get("person_id")
    if pid is not None:
        return int(pid)
    prefix = obj_name.split(":", 1)[0]
    return int(prefix.lstrip("p"))


def hue_palette(n):
    """n evenly spaced hues at full saturation/value, as 3-float RGB
    lists in 0..1. n==0 returns []. Hue 0 (pure red) is always present
    when n>=1 so the single-person sample renders identically to today."""
    if n <= 0:
        return []
    return [
        [round(c, 6) for c in colorsys.hsv_to_rgb(i / n, 1.0, 1.0)]
        for i in range(n)
    ]


def classify_persons(objects, width, height):
    """Return (person_color, foreground_ids, background_ids).

    person_color maps int person_id -> [r,g,b] (foreground hue or
    BACKGROUND_GREY). foreground_ids and background_ids are sorted
    lists of ints. Classification uses the per-person maximum across
    all frames of max(bbox_w, bbox_h); the threshold is
    max(width, height) / BACKGROUND_DIM_DIVISOR.
    """
    threshold = max(width, height) / BACKGROUND_DIM_DIVISOR

    # (pid, frame) -> [minx, maxx, miny, maxy]
    per_pf = {}
    for obj_name, obj in objects.items():
        pid = person_id_of(obj_name, obj)
        for fkey, fr in obj.get("frames", {}).items():
            pts = fr.get("points")
            if not pts:
                continue
            key = (pid, int(fkey))
            box = per_pf.get(key)
            for p in pts:
                x, y = p["x"], p["y"]
                if box is None:
                    box = [x, x, y, y]
                    per_pf[key] = box
                else:
                    if x < box[0]: box[0] = x
                    if x > box[1]: box[1] = x
                    if y < box[2]: box[2] = y
                    if y > box[3]: box[3] = y

    max_size = {}
    for (pid, _f), box in per_pf.items():
        sz = max(box[1] - box[0], box[3] - box[2])
        if sz > max_size.get(pid, 0.0):
            max_size[pid] = sz

    foreground_ids = sorted(p for p, s in max_size.items() if s >= threshold)
    background_ids = sorted(p for p, s in max_size.items() if s < threshold)

    palette = hue_palette(len(foreground_ids))
    person_color = {pid: palette[i] for i, pid in enumerate(foreground_ids)}
    for pid in background_ids:
        person_color[pid] = BACKGROUND_GREY

    return person_color, foreground_ids, background_ids


def build_shape_record(obj_name, obj, height, start_frame, end_frame, fps):
    """Produce a compact dict: name, closed, times, verts, inT, outT, opacity.

    JSON coords are screen-style (Y-down, origin top-left) — same as AE —
    despite an upstream docstring claiming Nuke convention. No Y flip.

    Frames whose index is outside [start_frame, end_frame] are dropped.
    This lets the caller cap a Tokgan JSON that happens to be one frame
    longer than the source video — the shape data must end on the same
    frame the footage does, or the composite layers visibly desync at
    the tail.
    """
    times, verts_all, in_all, out_all = [], [], [], []
    path_frames = []  # source frame numbers that produced a key
    for fkey in sorted(obj.get("frames", {}).keys(), key=int):
        fi = int(fkey)
        if fi < start_frame or fi > end_frame:
            continue
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
        exists = sorted(
            int(k) for k, v in visibility.items()
            if v and start_frame <= int(k) <= end_frame
        )
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
        for idx, (seg_start, seg_end) in enumerate(segments):
            if seg_start > start_frame:
                raw.append((_t(seg_start - 1), 0))
            raw.append((_t(seg_start), 100))
            raw.append((_t(seg_end), 100))
            # Trailing off-frame only emitted for non-final segments
            # (gap between segments). The final segment's shape holds
            # opacity 100 through the end of the comp so the shape
            # doesn't blink off one frame before the footage ends
            # when its Tokgan tracking ran short by a frame.
            is_final = (idx == len(segments) - 1)
            if not is_final and seg_end < end_frame:
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
    // Best-effort: silence in-script dialogs (does not affect AE's
    // first-launch prompts — those are gated by user preferences).
    try {{ app.beginSuppressDialogs(); }} catch (eSD) {{
        try {{ app.beginSuppressDialogs(true); }} catch (eSD2) {{}}
    }}
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

        var compositeMode = !!data.inputVideo;
        var comp;
        if (compositeMode) {{
            STEP = "composite-safety-checks";
            // Permitted starting states for composite-mode:
            //   (a) fresh AE (no project open)
            //   (b) our own .aep already open (re-run on same clip)
            //   (c) some *other* __composite.aep open from a prior batch
            //       iteration — close it silently and proceed
            // Anything else (foreign project, dirty work) is bailed
            // with a clear error so we never relocate the user's
            // saved file path via app.project.save(newPath).
            var openFile = app.project.file;
            var isOurAep = openFile && data.aepPath &&
                           openFile.fsName === data.aepPath;
            var isPriorComposite = openFile &&
                /__composite\.aep$/i.test(openFile.name);
            if (!isOurAep) {{
                if (app.project.dirty && !isPriorComposite) {{
                    throw new Error(
                        "AE has unsaved changes in a foreign project. " +
                        "Save or discard them before running tokgan_composite."
                    );
                }}
                if (openFile && !isPriorComposite) {{
                    throw new Error(
                        "AE has another project open: " + openFile.fsName +
                        " - close it (File > Close Project) before running " +
                        "tokgan_composite, otherwise app.project.save() would " +
                        "relocate that project's file path."
                    );
                }}
                if (isPriorComposite) {{
                    // Silent close — the prior composite was re-saved at
                    // the end of its own run, so we discard any in-memory
                    // changes since (none, typically).
                    try {{
                        app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
                    }} catch (eClose) {{
                        $.writeln("[Tokgan] couldn't close prior composite: " + eClose.toString());
                    }}
                }}
            }}

            STEP = "composite-comp-cleanup";
            // Remove a prior TokganComposite from re-runs so we don't pile
            // up duplicates in the project panel.
            for (var ri = app.project.numItems; ri >= 1; ri--) {{
                var it = app.project.item(ri);
                if (it instanceof CompItem && it.name === "TokganComposite") it.remove();
            }}
            // Clear any leftover render queue items (Done from prior
            // runs or stale "Queued" entries from failed runs). After
            // the safety checks above, the project is either fresh or
            // our own .aep — no user queue items to preserve.
            for (var qi = app.project.renderQueue.numItems; qi >= 1; qi--) {{
                app.project.renderQueue.item(qi).remove();
            }}
            STEP = "composite-import-footage (" + data.inputVideo + ")";
            var footFile = new File(data.inputVideo);
            if (!footFile.exists) throw new Error("Input video not found: " + data.inputVideo);
            var foot = app.project.importFile(new ImportOptions(footFile));
            STEP = "composite-create-comp";
            comp = app.project.items.addComp(
                "TokganComposite",
                data.width, data.height, 1,
                data.duration, data.fps
            );
            // Add footage first → it lands at index 1 (top). The shape
            // layer is added next via addShape(), which inserts at the
            // new top and pushes footage down to index 2.
            comp.layers.add(foot);
        }} else {{
            comp = app.project.activeItem;
            if (!(comp instanceof CompItem)) {{
                comp = app.project.items.addComp(
                    "TokganShapes",
                    data.width, data.height, 1,
                    data.duration, data.fps
                );
            }}
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
            var fillItem = null;
            for (var pp = 1; pp <= inside.numProperties; pp++) {{
                var mn = inside.property(pp).matchName;
                if (mn === "ADBE Vector Shape - Group") {{
                    pathItem = inside.property(pp);
                }} else if (mn === "ADBE Vector Graphic - Fill") {{
                    fillItem = inside.property(pp);
                }}
            }}
            var pathProp = pathItem.property("ADBE Vector Shape");
            if (fillItem && sd.color) {{
                fillItem.property("ADBE Vector Fill Color").setValue(sd.color);
            }}

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

        if (compositeMode) {{
            STEP = "composite-blend-and-shutter";
            // Multiply blend tints the underlying footage by each shape's
            // per-person hue; non-shape pixels are left unchanged because
            // the layer is transparent outside its shapes (multiply with
            // alpha is treated as no-op).
            layer.blendingMode = BlendingMode.MULTIPLY;
            if (typeof data.shutterAngle === "number") {{
                comp.shutterAngle = data.shutterAngle;
            }}
            if (typeof data.shutterPhase === "number") {{
                // Negative phase = half the negative angle centres the
                // blur around the source frame rather than trailing it.
                comp.shutterPhase = data.shutterPhase;
            }}
        }}

        var tDone = ((new Date()).getTime() - tBuild) / 1000;
        app.endUndoGroup();

        if (compositeMode && data.aepPath) {{
            STEP = "save-aep (" + data.aepPath + ")";
            try {{
                var aepFile = new File(data.aepPath);
                app.project.save(aepFile);
                $.writeln("[Tokgan] saved project to " + data.aepPath);
            }} catch (saveErr) {{
                $.writeln("[Tokgan] aep save failed: " + saveErr.toString());
            }}
        }}

        if (compositeMode && data.autoRender && data.outputMov) {{
            STEP = "auto-render-queue";
            // Auto-render: add a render queue item, pick the best
            // available output module, and trigger render() in-process.
            // app.project.renderQueue.render() blocks AE until done, so
            // an AppleScript DoScript driver naturally waits for the
            // .mov to land before returning control to the shell.
            var rqItem = app.project.renderQueue.items.add(comp);
            var outModule = rqItem.outputModule(1);

            STEP = "auto-render-output-module-template";
            // Try ProRes first (smaller intermediates), fall back to
            // the Lossless template which is always present. The shell
            // orchestrator transcodes to H.264 mp4 afterwards either way.
            var templateUsed = null;
            var templateCandidates = [
                "Apple ProRes 422 HQ",
                "ProRes 422 HQ",
                "Apple ProRes 422",
                "Lossless"
            ];
            for (var tc = 0; tc < templateCandidates.length; tc++) {{
                try {{
                    outModule.applyTemplate(templateCandidates[tc]);
                    templateUsed = templateCandidates[tc];
                    break;
                }} catch (tplErr) {{ /* try next */ }}
            }}
            if (templateUsed === null) {{
                throw new Error("No usable output module template found");
            }}
            $.writeln("[Tokgan] output module template: " + templateUsed);

            STEP = "auto-render-output-file";
            outModule.file = new File(data.outputMov);

            if (typeof data.nbFrames === "number" && data.nbFrames > 0) {{
                STEP = "auto-render-time-span";
                // Render ONE extra frame past the user-visible target.
                // ffmpeg trims it in the transcode step. Rationale:
                // motion blur on AE's last rendered frame samples the
                // exposure window [t-0.5/fps, t+0.5/fps]; the post-
                // frame half needs a "future" keyframe to interpolate
                // against, otherwise the shape (and footage) freeze
                // for the back half of the window, producing weak /
                // asymmetric blur on the final frame. By rendering
                // N+1 frames and trimming the last in transcode, the
                // surviving N frames each have proper bidirectional
                // motion-blur sampling.
                rqItem.timeSpanStart = 0;
                rqItem.timeSpanDuration = (data.nbFrames + 1) / comp.frameRate;
                $.writeln(
                    "[Tokgan] timeSpan set to " +
                    rqItem.timeSpanDuration.toFixed(6) +
                    "s (render " + (data.nbFrames + 1) +
                    " frames, ffmpeg will trim to " + data.nbFrames + ")"
                );
            }}

            STEP = "auto-render-render";
            var tRender = (new Date()).getTime();
            app.project.renderQueue.render();
            var tRendered = ((new Date()).getTime() - tRender) / 1000;
            $.writeln(
                "[Tokgan] render done in " + tRendered.toFixed(1) + "s -> " +
                data.outputMov
            );

            // render() marks queue items as "Done", which dirties the
            // project. Re-save so a subsequent re-run's safety check
            // sees a clean project (and so the .aep on disk records
            // the completed-queue state).
            if (data.aepPath) {{
                STEP = "auto-render-resave-aep";
                try {{ app.project.save(new File(data.aepPath)); }} catch (eRS) {{}}
            }}
        }}

        $.writeln(
            "[Tokgan import] " + shapes.length + " layers; " +
            "JSON parse " + tParse.toFixed(2) + "s; " +
            "AE build " + tDone.toFixed(2) + "s"
        );

        // Completion marker: AE's AppleScript DoScript may or may not
        // wait for the script to finish (it's effectively async in
        // AE 2025), so the shell-side orchestrator polls for this
        // file's existence instead of trusting the osascript return.
        // Empty file = success; non-empty = error message.
        if (data && data.markerPath) {{
            try {{
                var doneFile = new File(data.markerPath);
                doneFile.encoding = "UTF-8";
                if (doneFile.open("w")) {{ doneFile.write(""); doneFile.close(); }}
            }} catch (eDone) {{ /* best-effort */ }}
        }}
    }} catch (err) {{
        try {{ app.endUndoGroup(); }} catch (e2) {{}}
        var failMsg = (
            "Tokgan import failed.\n" +
            "Step: " + STEP + "\n" +
            "Error: " + err.toString() + "\n" +
            "Line:  " + (err.line || "?") + "\n" +
            "File:  " + (err.fileName || "?")
        );
        // Mirror the success-path marker write so the orchestrator
        // doesn't time out on a script failure. Non-empty content =
        // error; Python reads it back as the failure reason.
        try {{
            if (typeof data !== "undefined" && data && data.markerPath) {{
                var errFile = new File(data.markerPath);
                errFile.encoding = "UTF-8";
                if (errFile.open("w")) {{ errFile.write(failMsg); errFile.close(); }}
            }}
        }} catch (eMarker) {{}}
        // In auto-render mode, neither alert (would hang the
        // AppleScript driver on a modal) nor throw (would pop an AE
        // script-error dialog) is acceptable. The marker file written
        // above is the canonical error channel - osascript exits
        // cleanly and Python reads the marker content as the failure
        // reason. In shape-only mode keep alert() for visibility.
        var autoRunning = false;
        try {{ autoRunning = !!(typeof data !== "undefined" && data && data.autoRender); }} catch (eg) {{}}
        if (autoRunning) {{
            $.writeln("[Tokgan ERROR] " + failMsg);
            // Deliberately do not re-throw - marker file carries the error.
        }} else {{
            alert(failMsg);
        }}
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
    p.add_argument(
        "--keep-background",
        action="store_true",
        help=(
            "Import 'background' persons (max-over-time bbox dim < "
            "max(comp_w, comp_h)/%d) as a single neutral-grey fill "
            "instead of dropping them. Off by default to save AE "
            "keyframe-writing time on crowded clips." % BACKGROUND_DIM_DIVISOR
        ),
    )
    p.add_argument(
        "--fps",
        type=float,
        default=None,
        help=(
            "Override the fps declared in the JSON metadata. Tokgan "
            "exports stamp every file with fps=24 even when the source "
            "video runs at e.g. 25 fps, which makes the shapes play "
            "~4%% faster than the matching footage in AE. Pass the "
            "footage's true fps (e.g. --fps 25) to fix the timing."
        ),
    )
    p.add_argument(
        "--input-video",
        default=None,
        help=(
            "Switch to composite mode: the generated .jsx will import "
            "this footage, create a new comp 'TokganComposite' matching "
            "its width/height/fps/duration, add the shape layer on top "
            "in MULTIPLY blend mode, set shutter angle/phase for "
            "correctly centred motion blur, and save the project to "
            "<out_jsx_stem>.aep so a future headless aerender run can "
            "pick it up. Without this flag the loader keeps its "
            "shape-layer-only behaviour."
        ),
    )
    p.add_argument(
        "--shutter-angle",
        type=float,
        default=180.0,
        help=(
            "Shutter angle in degrees written into the comp when "
            "--input-video is set. Default 180 (standard cinema). "
            "Ignored without --input-video."
        ),
    )
    p.add_argument(
        "--shutter-phase",
        type=float,
        default=-90.0,
        help=(
            "Shutter phase in degrees written into the comp when "
            "--input-video is set. Default -90 (centres motion blur "
            "around each source frame; pair with shutter-angle 180). "
            "Ignored without --input-video."
        ),
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help=(
            "Override the comp duration in seconds. Default is the "
            "shape data's span (last_frame - first_frame + 1) / fps. "
            "In composite mode the orchestrator passes the input "
            "video's true duration here so the comp covers the full "
            "footage even when the shape data is slightly shorter."
        ),
    )
    p.add_argument(
        "--auto-render",
        action="store_true",
        help=(
            "Composite mode only: extend the .jsx so it adds a render "
            "queue item and triggers renderQueue.render() in-process. "
            "Combined with --output-mov, the .jsx writes a ProRes or "
            "Lossless intermediate to that path. The orchestrator "
            "drives this end-to-end via osascript DoScriptFile and "
            "then ffmpeg-transcodes to H.264 mp4. Without this flag "
            "the .jsx still builds the comp + queue but the user has "
            "to hit Render manually."
        ),
    )
    p.add_argument(
        "--output-mov",
        default=None,
        help=(
            "Composite + --auto-render only: where the .jsx tells AE "
            "to write the intermediate .mov (ProRes or Lossless, "
            "depending on which output module template the install "
            "happens to have). The orchestrator then transcodes this "
            "to .mp4 and deletes the .mov."
        ),
    )
    p.add_argument(
        "--nb-frames",
        type=int,
        default=None,
        help=(
            "Composite + --auto-render only: exact number of frames "
            "to render. The .jsx sets rqItem.timeSpanDuration to "
            "(N-0.5)/fps so AE renders exactly N frames regardless "
            "of how it snaps comp.duration to a frame boundary."
        ),
    )
    args = p.parse_args()

    in_path = args.input
    out_jsx = args.output or os.path.splitext(in_path)[0] + ".jsx"
    out_data = os.path.splitext(out_jsx)[0] + "_data.json"

    # Reuse the sidecar only if (a) the user didn't force, (b) it's newer
    # than the input, AND (c) it was written by a code build that produced
    # the same payload schema. The schema check makes adding new fields
    # (like `color`) safe — older sidecars get rebuilt automatically.
    sidecar_schema_ok = False
    if os.path.exists(out_data):
        try:
            with open(out_data) as _sf:
                sidecar_schema_ok = json.load(_sf).get("schema") == PAYLOAD_SCHEMA
        except (OSError, ValueError):
            sidecar_schema_ok = False

    # Flags that change sidecar contents without touching the input's
    # mtime would otherwise let an mtime-only cache check quietly serve
    # a stale sidecar that ignores the flag. Force rebuild whenever any
    # of them is set.
    data_is_current = (
        not args.force
        and not args.keep_background
        and args.fps is None
        and args.input_video is None
        and args.duration is None
        and not args.auto_render
        and args.output_mov is None
        and sidecar_schema_ok
        and os.path.getmtime(out_data) >= os.path.getmtime(in_path)
    )

    summary_extra = ""

    if data_is_current:
        print(f"Reusing existing data file (newer than input): {out_data}")
        n_shapes = "?"  # not loaded; reported as ? in the summary
    else:
        with open(in_path) as f:
            data = json.load(f)

        res = data.get("resolution", [data.get("width", 2160), data.get("height", 4096)])
        width, height = int(res[0]), int(res[1])
        json_fps = float(data.get("fps", 24))
        fps = float(args.fps) if args.fps else json_fps
        if args.fps and args.fps != json_fps:
            print(f"  fps override: JSON says {json_fps}, using {fps}")

        all_frames = set()
        for obj in data["objects"].values():
            for fkey in obj.get("frames", {}).keys():
                all_frames.add(int(fkey))
        start_frame = min(all_frames) if all_frames else 1
        end_frame = max(all_frames) if all_frames else 48
        # Don't truncate shape data in composite mode — the .jsx caps
        # the render window via rqItem.timeSpanDuration. Keeping the
        # full JSON shape range (including any trailing keyframes
        # past the input video's last frame) means AE's motion-blur
        # sampling at the final rendered frame can interpolate from
        # data on BOTH sides of t=(N-1)/fps, instead of holding the
        # last keyframe value through the exposure window.
        n_frames = end_frame - start_frame + 1
        shape_duration = n_frames / fps
        # In composite mode the orchestrator-supplied --duration is
        # authoritative: the comp follows the footage exactly so we
        # don't render a black tail when the shape JSON happens to
        # span more frames than the source video. Shape keyframes
        # beyond comp end are simply not rendered by AE.
        # In shape-only mode we still default to max so the user can
        # run the .jsx into their own comp without truncating shapes.
        if args.duration is None:
            duration = shape_duration
        elif args.input_video is not None:
            duration = args.duration
        else:
            duration = max(shape_duration, args.duration)

        person_color, fg_ids, bg_ids = classify_persons(
            data["objects"], width, height
        )
        bg_set = set(bg_ids)

        shapes = []
        for obj_name, obj in data["objects"].items():
            pid = person_id_of(obj_name, obj)
            if pid in bg_set and not args.keep_background:
                continue
            rec = build_shape_record(obj_name, obj, height, start_frame, end_frame, fps)
            if rec:
                rec["color"] = person_color[pid]
                shapes.append(rec)

        payload = {
            "schema": PAYLOAD_SCHEMA,
            "width": width,
            "height": height,
            "fps": fps,
            "duration": duration,
            "shapes": shapes,
        }

        if args.input_video:
            payload["inputVideo"] = os.path.abspath(args.input_video)
            payload["shutterAngle"] = float(args.shutter_angle)
            payload["shutterPhase"] = float(args.shutter_phase)
            payload["aepPath"] = os.path.abspath(
                os.path.splitext(out_jsx)[0] + ".aep"
            )
            if args.auto_render:
                if not args.output_mov:
                    sys.exit(
                        "ERROR: --auto-render requires --output-mov PATH"
                    )
                payload["autoRender"] = True
                payload["outputMov"] = os.path.abspath(args.output_mov)
                # Marker file the orchestrator polls for, so it
                # doesn't have to trust osascript's return semantics.
                payload["markerPath"] = os.path.abspath(
                    os.path.splitext(out_jsx)[0] + ".done"
                )
                if args.nb_frames is not None:
                    payload["nbFrames"] = int(args.nb_frames)

        with open(out_data, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        n_shapes = len(shapes)
        bg_state = "kept" if args.keep_background else "dropped"
        summary_extra = (
            f"  Persons: {len(fg_ids)} foreground, "
            f"{len(bg_ids)} background ({bg_state})\n"
        )
        if args.input_video:
            summary_extra += (
                f"  Composite mode: footage={os.path.basename(args.input_video)} "
                f"fps={fps} duration={duration:.3f}s "
                f"shutter={args.shutter_angle}/{args.shutter_phase}\n"
            )
            if args.auto_render:
                summary_extra += (
                    f"  Auto-render: ON -> "
                    f"{os.path.basename(args.output_mov)} "
                    f"(via in-script renderQueue.render())\n"
                )
        if fg_ids:
            summary_extra += "  Hues:\n"
            for pid in fg_ids:
                r, g, b = person_color[pid]
                summary_extra += (
                    f"    p{pid}: ({int(round(r*255))},{int(round(g*255))},{int(round(b*255))})\n"
                )

    loader = LOADER_TEMPLATE.format(data_basename=os.path.basename(out_data))
    with open(out_jsx, "w") as f:
        f.write(loader)

    print(f"Wrote {out_jsx}  ({os.path.getsize(out_jsx):,} bytes)")
    print(f"Data {out_data}  ({os.path.getsize(out_data):,} bytes)")
    print(f"  Shapes: {n_shapes}")
    if summary_extra:
        print(summary_extra, end="")


if __name__ == "__main__":
    main()
