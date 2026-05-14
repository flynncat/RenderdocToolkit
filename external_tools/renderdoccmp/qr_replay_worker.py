"""qrenderdoc --python worker: GPU-replay a capture and dump RT + textures.

Runs inside qrenderdoc.exe's embedded Python 3.6 interpreter.  This is the
"Plan B1" backend: instead of bundling a matching-version Python or talking
the RenderDoc TCP protocol, we drive qrenderdoc with its own --python flag
so the renderdoc module already loaded inside the host process can be used
directly.

Inputs are passed via a JSON job spec.  The path to the JSON is taken from
``QR_JOB_JSON_PATH`` env var (Chinese-path safe).  Outputs are PNG files
plus a ``manifest.json`` written next to them.

Calling convention:

  qrenderdoc.exe --python <this-file>

with environment::

  QR_JOB_JSON_PATH = "<path to job-spec JSON>"

Job-spec JSON schema::

  {
    "capture": "absolute path to .rdc",
    "output_dir": "absolute dir for PNGs & manifest",
    "mode": "perf" | "cmp",
    "max_draws": int (optional, default 200) - cap dump count
    "event_ids": [int, ...] (optional) - if present, only dump these EIDs
  }

Manifest JSON schema (written to <output_dir>/manifest.json)::

  {
    "ok": bool,
    "error": str or null,
    "renderdoc_version": str,
    "driver": str,
    "frame_number": int,
    "frame_file_offset": int,
    "texture_count": int,
    "draws": [
      {
        "eid": int,
        "name": str,
        "primitive_count": int,
        "rt_png": "rt_<eid>.png" or null,
        "rt_resource_id": str or null,
        "textures": [
          { "resource_id": str, "width": int, "height": int,
            "format": str, "png": "tex_<rid>.png" or null,
            "bind_point": str or null }
        ]
      }, ...
    ],
    "textures": [
      { "resource_id": str, "width": int, "height": int,
        "format": str, "png": "tex_<rid>.png" or null }
    ]
  }

The worker MUST call sys.exit(0) at the end so qrenderdoc's pythonExited
flag is set and the main UI window is never opened.
"""

# Python 3.6 compatible: no f-strings with formatting in some edges, no walrus,
# no positional-only.  Keep it conservative.

import json
import os
import sys
import time
import traceback


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _early_log(msg):
    """Best-effort logging that won't crash if logfile isn't ready yet."""
    try:
        sys.stderr.write("[qr_replay_worker] " + str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _is_success(rd, result):
    """Cross-version success check.

    v1.36 returns objects that str() as ``<Result: 'Success'>`` while v1.41+
    return ``ResultCode.Succeeded``.  We accept either substring and also
    fall back to comparing against the ResultCode enum.
    """
    if result is None:
        return False
    s = str(result).lower()
    if "success" in s or "succeeded" in s:
        return True
    if hasattr(rd, "ResultCode"):
        try:
            ok = rd.ResultCode.Succeeded
            if hasattr(result, "code") and result.code == ok:
                return True
            if result == ok:
                return True
        except Exception:
            pass
    return False


def _safe_name(s):
    """Make a string safe for use in a filename."""
    out = []
    for ch in str(s):
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:60] or "x"


def _format_name(fmt):
    try:
        if hasattr(fmt, "Name"):
            return fmt.Name()
        return str(fmt)
    except Exception:
        return "?"


def _collect_leaves(actions, out):
    for a in actions:
        if a.children:
            _collect_leaves(a.children, out)
        else:
            out.append(a)
    return out


def _action_is_draw(rd, a):
    """Return True if this action is a real drawcall.

    Prefer the ``ActionFlags.Drawcall`` bit (set by RenderDoc for any draw
    that issues primitives, indexed or otherwise) and fall back to
    ``numIndices``/``numInstances`` for older builds that lack the flag.
    """
    try:
        flags = getattr(a, "flags", None)
        if flags is not None and hasattr(rd, "ActionFlags"):
            try:
                if int(flags) & int(rd.ActionFlags.Drawcall):
                    return True
            except Exception:
                pass
        n_idx = getattr(a, "numIndices", 0) or 0
        n_ins = getattr(a, "numInstances", 0) or 0
        return bool(n_idx > 0 or n_ins > 0)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Texture/RT dump primitives
# -----------------------------------------------------------------------------

class Dumper(object):
    def __init__(self, rd, controller, out_dir):
        self.rd = rd
        self.controller = controller
        self.out_dir = out_dir
        # Cache of (prefix, rid_int) -> relative png filename to avoid
        # dumping the same texture more than once across draws.
        self._dumped = {}
        # rid_int -> TextureDescription (persistent or live IDs both end up
        # in the same map since GetTextures() returns them all).
        self._tex_lookup = {}
        for t in controller.GetTextures():
            try:
                rid = int(t.resourceId)
                self._tex_lookup[rid] = t
            except Exception:
                pass

    def lookup_tex(self, resource_id):
        """Look up a TextureDescription by any form of resource id."""
        try:
            rid = int(resource_id)
        except Exception:
            return None
        return self._tex_lookup.get(rid)

    def save_texture(self, resource_id, prefix="tex"):
        """SaveTexture to ``<out_dir>/<prefix>_<rid>.png``.

        Accepts either a ResourceId object or an int.  Returns the relative
        filename on success, ``None`` on failure (including the "metadata
        lookup missed but the SaveTexture call may still work" path which
        we treat as failure to keep manifest clean).
        """
        # Normalise to int for caching & for filename.  This also lets us
        # short-circuit zero/none.
        try:
            rid = int(resource_id)
        except Exception:
            return None
        if rid == 0:
            return None

        cached = self._dumped.get((prefix, rid))
        if cached is not None:
            return cached

        # Use the ResourceId object directly when we have it (works for both
        # persistent and live IDs).  If int(resource_id) was the original
        # ResourceId, ``resource_id`` may already be the object.
        rid_obj = resource_id if hasattr(resource_id, "__class__") and \
            resource_id.__class__.__name__ == "ResourceId" else None

        tex = self._tex_lookup.get(rid)
        if rid_obj is None and tex is not None:
            rid_obj = tex.resourceId

        # Skip depth/stencil — readback as PNG is meaningless.
        if tex is not None:
            fname = _format_name(tex.format).upper()
            if "DEPTH" in fname or "STENCIL" in fname:
                self._dumped[(prefix, rid)] = None
                return None
            if not (tex.width and tex.height):
                self._dumped[(prefix, rid)] = None
                return None

        save = self.rd.TextureSave()
        if rid_obj is not None:
            save.resourceId = rid_obj
        else:
            # last resort: attempt to set via int (some bindings accept this)
            try:
                save.resourceId = resource_id
            except Exception:
                self._dumped[(prefix, rid)] = None
                return None
        save.destType = self.rd.FileType.PNG
        save.mip = 0
        save.slice.sliceIndex = 0
        try:
            save.typeCast = self.rd.CompType.UNorm
        except Exception:
            pass

        rel = "{prefix}_{rid}.png".format(prefix=prefix, rid=rid)
        out = os.path.join(self.out_dir, rel)
        try:
            ok = self.controller.SaveTexture(save, out)
        except Exception as exc:
            _early_log("SaveTexture raised for rid=" + str(rid) + ": " + str(exc))
            ok = False
        if _is_success(self.rd, ok) and os.path.exists(out) and os.path.getsize(out) > 0:
            self._dumped[(prefix, rid)] = rel
            return rel
        try:
            if os.path.exists(out) and os.path.getsize(out) == 0:
                os.unlink(out)
        except Exception:
            pass
        self._dumped[(prefix, rid)] = None
        return None


# -----------------------------------------------------------------------------
# Pipeline-state inspection across API back-ends
# -----------------------------------------------------------------------------

def _extract_rt_from_action(action):
    """Return the dominant colour RT ResourceId from an Action.

    Modern RenderDoc (v1.30+) exposes ``action.outputs`` and
    ``action.depthOut`` directly on each draw action, populated based on the
    actual API state at that event.  This is far more robust than walking
    the GL/Vulkan/D3D pipeline-state struct and works uniformly across
    APIs.  We take the first non-zero colour output.  If every colour slot
    is zero (e.g. a depth-only/shadow-map pass), we return None — the
    caller decides whether to fall back to ``depthOut`` (we don't, because
    depth as PNG is meaningless).
    """
    outs = getattr(action, "outputs", None)
    if outs is None:
        return None
    for o in outs:
        try:
            if int(o) != 0:
                return o  # return the ResourceId object, not the int
        except Exception:
            continue
    return None


def _extract_descriptor_textures(rd, controller):
    """Return a list of bound ImageSampler texture ResourceIds for the
    *current* event (caller must have already called SetFrameEvent).

    We iterate ``GetDescriptorAccess()`` returning the descriptor accesses
    touched by this event, then materialise the ResourceIds via
    ``GetDescriptors(store, ranges)`` for each unique descriptorStore.
    Returns ``[{"resource_id": ResourceId, "stage": int, "index": int}, ...]``
    de-duplicated by resource id.
    """
    out = []
    seen = set()
    try:
        accesses = controller.GetDescriptorAccess()
    except Exception:
        return out
    if not accesses:
        return out

    # Group by store + remember which DA each range comes from so we can
    # decorate the resulting Descriptor with stage/index metadata.
    by_store = {}
    for da in accesses:
        try:
            store = da.descriptorStore
            store_key = int(store) if hasattr(store, "__int__") else id(store)
            entry = by_store.setdefault(store_key, {"store": store, "das": []})
            entry["das"].append(da)
        except Exception:
            continue

    for entry in by_store.values():
        store = entry["store"]
        das = entry["das"]
        ranges = []
        for da in das:
            try:
                r = rd.DescriptorRange()
                if hasattr(r, "offset"):
                    r.offset = int(da.byteOffset)
                if hasattr(r, "descriptorSize"):
                    r.descriptorSize = int(da.byteSize)
                if hasattr(r, "count"):
                    r.count = 1
                ranges.append(r)
            except Exception:
                ranges.append(None)
        try:
            descs = controller.GetDescriptors(store, [r for r in ranges if r])
        except Exception:
            continue

        # Pair up DA <-> Descriptor 1:1 (best-effort: same order as ranges)
        for da, desc in zip(das, descs):
            try:
                dtype = int(desc.type)
            except Exception:
                dtype = None
            # ImageSampler == 3 in v1.36 (DescriptorType.ImageSampler).  Try
            # the named enum first so we stay forward-compatible.
            is_image = False
            try:
                if hasattr(rd, "DescriptorType"):
                    is_image = (dtype == int(rd.DescriptorType.ImageSampler))
            except Exception:
                pass
            if not is_image and dtype != 3:
                continue
            rid_obj = getattr(desc, "resource", None)
            try:
                rid_int = int(rid_obj)
            except Exception:
                continue
            if rid_int == 0 or rid_int in seen:
                continue
            seen.add(rid_int)
            try:
                stage = int(da.stage)
            except Exception:
                stage = None
            try:
                idx = int(da.index)
            except Exception:
                idx = None
            out.append({
                "resource_id": rid_obj,
                "rid_int": rid_int,
                "stage": stage,
                "index": idx,
            })
    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run():
    job_path = os.environ.get("QR_JOB_JSON_PATH", "")
    if not job_path or not os.path.isfile(job_path):
        _early_log("QR_JOB_JSON_PATH missing or invalid: " + repr(job_path))
        return {"ok": False, "error": "missing QR_JOB_JSON_PATH"}, None

    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)
    capture = job.get("capture") or ""
    output_dir = job.get("output_dir") or ""
    max_draws = int(job.get("max_draws") or 200)
    only_eids = set(int(x) for x in (job.get("event_ids") or []))

    if not os.path.isfile(capture):
        return {"ok": False, "error": "capture not found: " + capture}, output_dir
    if not output_dir:
        return {"ok": False, "error": "no output_dir"}, None
    os.makedirs(output_dir, exist_ok=True)

    log_path = os.path.join(output_dir, "qr_worker.log")

    def log(msg):
        s = "[qr_replay_worker] " + str(msg)
        try:
            sys.stderr.write(s + "\n"); sys.stderr.flush()
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except Exception:
            pass

    log("job: capture=" + repr(capture) + " out=" + repr(output_dir)
        + " max_draws=" + str(max_draws) + " only_eids=" + str(len(only_eids)))

    import renderdoc as rd
    log("renderdoc version: " + rd.GetVersionString())

    cap = rd.OpenCaptureFile()
    st = cap.OpenFile(capture, "rdc", None)
    log("OpenFile=" + str(st))
    if not _is_success(rd, st):
        return {"ok": False, "error": "OpenFile failed: " + str(st)}, output_dir

    opts = rd.ReplayOptions()
    t0 = time.time()
    res, controller = cap.OpenCapture(opts, None)
    log("OpenCapture=" + str(res) + " took " + str(round(time.time() - t0, 2)) + "s")
    if not _is_success(rd, res) or controller is None:
        return {"ok": False, "error": "OpenCapture failed: " + str(res)}, output_dir

    api_props = controller.GetAPIProperties()
    log("API=" + str(api_props.pipelineType))

    dumper = Dumper(rd, controller, output_dir)
    log("textures total=" + str(len(dumper._tex_lookup)))

    # Enumerate leaf draw actions.
    leaves = []
    _collect_leaves(controller.GetRootActions(), leaves)
    log("leaf actions=" + str(len(leaves)))

    target_actions = []
    for a in leaves:
        if not _action_is_draw(rd, a):
            continue
        if only_eids and a.eventId not in only_eids:
            continue
        target_actions.append(a)

    if not only_eids and max_draws > 0:
        # When the caller didn't pre-select EIDs, prefer the heaviest draws by
        # primitive count so the most visually meaningful ones are dumped first.
        def _weight(a):
            return ((getattr(a, "numIndices", 0) or 0)
                    * max(1, getattr(a, "numInstances", 0) or 1))
        target_actions.sort(key=_weight, reverse=True)
        target_actions = target_actions[:max_draws]
        # Restore chronological order so the manifest reads naturally.
        target_actions.sort(key=lambda a: a.eventId)

    # In "cmp" mode we don't need per-draw RT/binding dumps — only a full
    # texture roster.  Skip the per-draw loop entirely so the cmp pre-pass
    # is fast and predictable.
    mode = (job.get("mode") or "perf").lower()
    if mode == "cmp":
        target_actions = []
        log("cmp mode: skipping per-draw replay, will dump full texture roster")

    log("draws to dump=" + str(len(target_actions)))

    # ---- per-draw replay & dump ----
    sf = controller.GetStructuredFile()
    draws_manifest = []
    t_replay = time.time()
    for idx, a in enumerate(target_actions):
        try:
            controller.SetFrameEvent(a.eventId, True)

            rt_rid_obj = _extract_rt_from_action(a)
            rt_png = None
            rt_rid_str = None
            if rt_rid_obj is not None:
                rt_png = dumper.save_texture(
                    rt_rid_obj, prefix="rt_eid{0}".format(a.eventId))
                try:
                    rt_rid_str = str(int(rt_rid_obj))
                except Exception:
                    rt_rid_str = str(rt_rid_obj)

            tex_binds = _extract_descriptor_textures(rd, controller)
            tex_entries = []
            # Limit per-draw texture dump count so we don't explode I/O.
            for tb in tex_binds[:8]:
                rid_obj = tb["resource_id"]
                rid_int = tb.get("rid_int", 0)
                png = dumper.save_texture(rid_obj, prefix="tex")
                t = dumper.lookup_tex(rid_int) if rid_int else None
                tex_entries.append({
                    "resource_id": str(rid_int) if rid_int else str(rid_obj),
                    "width": t.width if t else 0,
                    "height": t.height if t else 0,
                    "format": _format_name(t.format) if t else "",
                    "png": png,
                    "bind_point": "stage{0}_idx{1}".format(
                        tb.get("stage"), tb.get("index")),
                })

            name = a.GetName(sf) if sf else ""
            # ``Action.eventId`` is RenderDoc's API-side event ID space, but
            # the XML-fallback path keys draws by chunkIndex (the position
            # in the structured chunk stream).  Emit *both* so the caller
            # can correlate either way.
            chunk_index = -1
            try:
                chunk_index = int(getattr(a, "chunkIndex", -1))
            except Exception:
                pass
            if chunk_index < 0:
                ev_list = getattr(a, "events", None)
                if ev_list and len(ev_list) > 0:
                    try:
                        chunk_index = int(getattr(ev_list[-1], "chunkIndex", -1))
                    except Exception:
                        chunk_index = -1
            draws_manifest.append({
                "eid": int(a.eventId),
                "chunk_index": int(chunk_index),
                "name": name,
                "primitive_count": int(getattr(a, "numIndices", 0) or 0),
                "instance_count": int(getattr(a, "numInstances", 0) or 0),
                "rt_resource_id": rt_rid_str,
                "rt_png": rt_png,
                "textures": tex_entries,
            })
        except Exception as exc:
            log("draw eid=" + str(a.eventId) + " error: " + str(exc))
            log(traceback.format_exc())

        if idx and idx % 25 == 0:
            log("  ... {0}/{1} draws dumped, elapsed {2}s".format(
                idx, len(target_actions), round(time.time() - t_replay, 1)))

    log("all draws done in " + str(round(time.time() - t_replay, 1)) + "s")

    # Build the capture-global texture roster: metadata for every texture,
    # but only include the PNG path if we *already* dumped it as part of
    # per-draw bindings.  Greedy dumping of all 2000+ textures crashes
    # certain RenderDoc builds (EGLImage / multisample / placeholder data
    # can fault inside SaveTexture).  Callers (cmp) get an explicit budget
    # via job["max_extra_textures"] to opt-in to additional dumps.
    tex_roster = []
    extra_budget = int(job.get("max_extra_textures") or 0)
    extra_done = 0
    for rid, t in dumper._tex_lookup.items():
        fname = _format_name(t.format)
        if "DEPTH" in fname.upper() or "STENCIL" in fname.upper():
            continue
        if not (t.width and t.height):
            continue
        png = dumper._dumped.get(("tex", rid))
        if png is None and extra_done < extra_budget:
            # Skip 1x1 / tiny placeholder textures; they're not visually
            # useful and may be backed by Internal::Initial Contents stubs.
            if t.width >= 4 and t.height >= 4:
                try:
                    png = dumper.save_texture(t.resourceId, prefix="tex")
                    if png:
                        extra_done += 1
                except Exception as exc:
                    log("extra dump rid=" + str(rid) + " err: " + str(exc))
        tex_roster.append({
            "resource_id": str(rid),
            "width": t.width,
            "height": t.height,
            "format": fname,
            "png": png,
        })

    log("texture roster size=" + str(len(tex_roster))
        + " extra_dumped=" + str(extra_done) + "/" + str(extra_budget))

    try:
        controller.Shutdown()
    except Exception:
        pass
    try:
        cap.Shutdown()
    except Exception:
        pass

    return {
        "ok": True,
        "error": None,
        "renderdoc_version": rd.GetVersionString(),
        "driver": str(api_props.pipelineType),
        "texture_count": len(dumper._tex_lookup),
        "draws": draws_manifest,
        "textures": tex_roster,
    }, output_dir


def main():
    out_dir_for_manifest = None
    payload = None
    try:
        payload, out_dir_for_manifest = run()
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    if out_dir_for_manifest is None:
        out_dir_for_manifest = os.environ.get("QR_JOB_OUTPUT_DIR", "")
    if out_dir_for_manifest:
        try:
            os.makedirs(out_dir_for_manifest, exist_ok=True)
            with open(os.path.join(out_dir_for_manifest, "manifest.json"),
                      "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            _early_log("failed to write manifest: " + str(exc))


main()
sys.exit(0)
