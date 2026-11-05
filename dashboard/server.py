"""Local dashboard for enriching the scanned room with acoustic material data.

    python dashboard/server.py            # serves http://localhost:8765

Stdlib only, no install needed. Reads data/room_scan.json (the base scan —
swap this for the real Polycam-derived export, see README.md for the
schema), overlays any saved enrichment from data/enriched_room.json, and
serves the static frontend in static/.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
ROOM_SCAN_PATH = DATA_DIR / "room_scan.json"
ENRICHED_PATH = DATA_DIR / "enriched_room.json"
MATERIALS_PATH = DATA_DIR / "materials.json"
USER_SURFACES_PATH = DATA_DIR / "user_surfaces.json"
CLOUD_PATH = DATA_DIR / "cloud.bin"
CLOUD_META_PATH = DATA_DIR / "cloud.json"
RECORDINGS_DIR = ROOT.parent / "recordings"
MEASUREMENTS_DIR = ROOT.parent / "malinger"
# URL prefix -> folder holding rig recordings (wav + json, optional png/jpg).
RECORDING_DIRS = {"recordings": RECORDINGS_DIR, "malinger": MEASUREMENTS_DIR}
MATERIAL_ALIASES = {"fabric": "textile", "metall": "metal", "tekstil": "textile", "tre": "wood",
                    "plast": "plastic", "glass": "glass", "luft": "air"}
# JSON keys the models write their per-material probabilities under.
MODEL_KEYS = {"vision": ("vision",), "sound": ("sound", "audio", "acoustic", "sound_model", "acoustic_model")}
RECORDING_META_KEYS = ("run", "label", "material", "distance_mm", "tilt", "orientation", "sweep_f1", "sweep_f2",
                       "sweep_seconds", "rate", "peak_level", "recorded_at", "duration_s")
CATEGORIES = {"wall", "floor", "ceiling", "window", "door", "furniture", "other"}

PORT = 8765

_lock = threading.Lock()

EMPTY_ENRICHMENT = {
    "material": None,
    "fabric": None,
    "friction_coeff": None,
    "density_kg_m3": None,
    "thickness_mm": None,
    "absorption_alpha": {"125": None, "250": None, "500": None, "1000": None, "2000": None, "4000": None},
    "source": "unmeasured",
    "notes": "",
    "updated_at": None,
}


def _load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _probabilities(raw) -> dict | None:
    if isinstance(raw, dict) and isinstance(raw.get("probabilities"), dict):
        raw = raw["probabilities"]
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, value in raw.items():
        if isinstance(value, (int, float)):
            name = MATERIAL_ALIASES.get(str(key).lower(), str(key).lower())
            out[name] = out.get(name, 0.0) + float(value)
    return out or None


def _build_record(prefix: str, stem: str, related: list[str], meta: dict,
                  is_mock: bool = False) -> dict:
    """One recording as the browser sees it: file urls, metadata, model output."""
    def url(ext):
        return next((f"/{prefix}/{quote(f)}" for f in related if f.lower().endswith(ext)), None)
    predictions = {}
    for model, keys in MODEL_KEYS.items():
        probs = next((p for p in (_probabilities(meta.get(k)) for k in keys) if p), None)
        if probs:
            predictions[model] = probs
    return {
        "id": f"{prefix}/{stem}",
        "spectrogram": url(".png"),
        "audio": url(".wav"),
        "image": url(".jpg"),
        "meta": {k: meta[k] for k in RECORDING_META_KEYS if k in meta},
        "predictions": predictions,
        "mock": is_mock,
    }


def latest_recording() -> dict | None:
    """The newest recording in any folder, whether or not it belongs to an object.

    This is what the live panel shows: a measurement appears the moment it is
    written, without anyone having to link it or click anything.
    """
    newest = None
    for prefix, folder in RECORDING_DIRS.items():
        if not folder.is_dir():
            continue
        files = sorted(p.name for p in folder.iterdir())
        for meta_path in folder.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(meta, dict):
                continue
            stem = meta_path.stem
            related = [f for f in files if f.rsplit(".", 1)[0] == stem or f.startswith(stem + "_")]
            # Sort on the recording's own timestamp, falling back to the file's,
            # so renamed files keep their place in time.
            when = meta.get("recorded_at") or ""
            key = (when, meta_path.stat().st_mtime)
            if newest is None or key > newest[0]:
                record = _build_record(prefix, stem, related, meta)
                record["object"] = meta.get("object")
                newest = (key, record)
    return newest[1] if newest else None


def load_recordings(surface_ids: set[str]) -> dict[str, list]:
    """Group rig recordings by object. A recording belongs to an object when its JSON has
    "object": "<id>", when data/recording_links.json maps its file stem to one or more ids,
    when its "label" is an object id, or when an id appears in its file name."""
    by_object: dict[str, list] = {}
    link_file = _load_json(DATA_DIR / "recording_links.json", {})
    links, mock = link_file.get("links", {}), bool(link_file.get("mock", False))
    for prefix, folder in RECORDING_DIRS.items():
        if not folder.is_dir():
            continue
        files = sorted(p.name for p in folder.iterdir())
        for meta_path in sorted(folder.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            stem = meta_path.stem
            related = [f for f in files if f.rsplit(".", 1)[0] == stem or f.startswith(stem + "_")]
            targets, is_mock = [], False
            if meta.get("object") in surface_ids:
                targets = [meta["object"]]
            elif stem in links:
                linked = links[stem] if isinstance(links[stem], list) else [links[stem]]
                targets, is_mock = [t for t in linked if t in surface_ids], mock
            elif meta.get("label") in surface_ids:
                targets = [meta["label"]]
            else:
                hit = next((sid for sid in sorted(surface_ids, key=len, reverse=True)
                            if any(sid in f for f in related)), None)
                targets = [hit] if hit else []
            if not targets:
                continue
            record = _build_record(prefix, stem, related, meta, is_mock)
            for target in targets:
                by_object.setdefault(target, []).append(record)
    for records in by_object.values():
        records.sort(key=lambda r: r["meta"].get("recorded_at") or "")
    return by_object


def object_predictions(records: list) -> dict:
    """Latest output of each model among an object's recordings."""
    out = {}
    for record in records:
        for model, probs in record["predictions"].items():
            out[model] = {"probabilities": probs, "recording": record["id"],
                          "recorded_at": record["meta"].get("recorded_at")}
    return out


def load_room() -> dict:
    """Base scan merged with any saved enrichment, keyed by surface id."""
    base = _load_json(ROOM_SCAN_PATH, {"room": {}, "surfaces": []})
    base["room"] = {**base.get("room", {}), "cloud": _load_json(CLOUD_META_PATH, None)}
    for surface in _load_json(USER_SURFACES_PATH, []):
        surface["user_placed"] = True
        base["surfaces"].append(surface)
    enriched = _load_json(ENRICHED_PATH, {})
    recordings = load_recordings({s["id"] for s in base.get("surfaces", [])})
    mock_predictions = _load_json(DATA_DIR / "mock_predictions.json", {})
    for surface in base.get("surfaces", []):
        surface["recordings"] = recordings.get(surface["id"], [])
        surface["predictions"] = object_predictions(surface["recordings"])
        for model, probs in mock_predictions.get(surface["id"], {}).items():
            if model not in surface["predictions"] and _probabilities(probs):
                surface["predictions"][model] = {"probabilities": _probabilities(probs), "mock": True}
        overlay = enriched.get(surface["id"])
        surface["enrichment"] = {**EMPTY_ENRICHMENT, **(overlay or {})}
        if overlay and "absorption_alpha" in overlay:
            surface["enrichment"]["absorption_alpha"] = {
                **EMPTY_ENRICHMENT["absorption_alpha"],
                **overlay["absorption_alpha"],
            }
    return base


def save_enrichment(surface_id: str, patch: dict) -> dict:
    with _lock:
        enriched = _load_json(ENRICHED_PATH, {})
        current = {**EMPTY_ENRICHMENT, **enriched.get(surface_id, {})}
        current["absorption_alpha"] = {
            **EMPTY_ENRICHMENT["absorption_alpha"],
            **current.get("absorption_alpha", {}),
        }
        for key, value in patch.items():
            if key == "absorption_alpha" and isinstance(value, dict):
                current["absorption_alpha"] = {**current["absorption_alpha"], **value}
            else:
                current[key] = value
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        enriched[surface_id] = current
        _save_json(ENRICHED_PATH, enriched)
        return current


def _clean_geometry(g) -> dict:
    """Validate a hand-placed box: centre and size in metres, rotation about the vertical axis."""
    center = [float(v) for v in g["center_m"]]
    size = [abs(float(v)) for v in g["size_m"]]
    if len(center) != 3 or len(size) != 3:
        raise ValueError("center_m and size_m need 3 numbers")
    return {
        "type": "bbox",
        "center_m": center,
        "size_m": [max(v, 0.02) for v in size],
        "rotation_deg": float(g.get("rotation_deg", 0)),
    }


def create_surface(body: dict) -> dict:
    label = str(body.get("label") or "Object").strip()[:60] or "Object"
    category = body.get("category") if body.get("category") in CATEGORIES else "other"
    with _lock:
        surfaces = _load_json(USER_SURFACES_PATH, [])
        taken = {s["id"] for s in surfaces} | {s["id"] for s in _load_json(ROOM_SCAN_PATH, {}).get("surfaces", [])}
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "object"
        n = 1
        while f"{slug}_{n}" in taken:
            n += 1
        surface = {
            "id": f"{slug}_{n}",
            "label": label,
            "category": category,
            "cv_guess": None,
            "geometry": _clean_geometry(body["geometry"]),
        }
        surfaces.append(surface)
        _save_json(USER_SURFACES_PATH, surfaces)
    return surface


def update_surface(surface_id: str, body: dict):
    with _lock:
        surfaces = _load_json(USER_SURFACES_PATH, [])
        for surface in surfaces:
            if surface["id"] == surface_id:
                if body.get("label"):
                    surface["label"] = str(body["label"]).strip()[:60]
                if body.get("category") in CATEGORIES:
                    surface["category"] = body["category"]
                if "geometry" in body:
                    surface["geometry"] = _clean_geometry(body["geometry"])
                _save_json(USER_SURFACES_PATH, surfaces)
                return surface
    return None


def delete_surface(surface_id: str) -> bool:
    with _lock:
        surfaces = _load_json(USER_SURFACES_PATH, [])
        kept = [s for s in surfaces if s["id"] != surface_id]
        if len(kept) == len(surfaces):
            return False
        _save_json(USER_SURFACES_PATH, kept)
        enriched = _load_json(ENRICHED_PATH, {})
        if enriched.pop(surface_id, None) is not None:
            _save_json(ENRICHED_PATH, enriched)
        return True


def export_pyroomacoustics() -> dict:
    """Per-surface alpha per octave band, for surfaces that have at least one value."""
    room = load_room()
    out = []
    for surface in room["surfaces"]:
        alpha = surface["enrichment"]["absorption_alpha"]
        if not any(v is not None for v in alpha.values()):
            continue
        out.append(
            {
                "id": surface["id"],
                "label": surface["label"],
                "category": surface["category"],
                "material": surface["enrichment"]["material"],
                "source": surface["enrichment"]["source"],
                "complete": all(v is not None for v in alpha.values()),
                "absorption": alpha,
            }
        )
    return {
        "note": "Octave-band absorption coefficients, center frequencies in Hz. "
        "Bands above 4 kHz are not covered by this rig. Missing bands are null, "
        "not 0 (0 would mean a perfect reflector). Surfaces with source "
        "'unmeasured' or 'estimated' are literature guesses, not measurements — "
        "see dashboard/README.md.",
        "surfaces": out,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        print("[dashboard]", fmt % args)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # Without this the browser caches API replies by its own heuristics, and
        # the live panel keeps polling its way back to the same stale answer.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", rel):
            self.send_error(404)
            return
        file_path = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in file_path.parents and file_path != STATIC_DIR.resolve():
            self.send_error(404)
            return
        if not file_path.exists():
            self.send_error(404)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
        }.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # Edited constantly while developing; a cached copy means the browser
        # keeps running yesterday's page until someone hard-reloads.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_recording(self, folder: Path, name: str):
        types = {".png": "image/png", ".jpg": "image/jpeg", ".wav": "audio/wav", ".json": "application/json"}
        name = unquote(name)
        path = folder / name
        if ("/" in name or "\\" in name or name.startswith(".") or path.suffix.lower() not in types
                or path.resolve().parent != folder.resolve() or not path.is_file()):
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types[path.suffix.lower()])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/room":
            self._send_json(load_room())
        elif parsed.path == "/api/latest":
            self._send_json(latest_recording() or {})
        elif parsed.path == "/api/materials":
            self._send_json(_load_json(MATERIALS_PATH, {"presets": []}))
        elif parsed.path == "/api/cloud" and CLOUD_PATH.exists():
            body = CLOUD_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path.split("/")[1] in RECORDING_DIRS and parsed.path.count("/") == 2:
            prefix, name = parsed.path.split("/")[1:]
            self._send_recording(RECORDING_DIRS[prefix], name)
        elif parsed.path == "/api/export/pyroomacoustics":
            self._send_json(export_pyroomacoustics())
        elif parsed.path == "/" or not parsed.path.startswith("/api/"):
            self._send_static(parsed.path)
        else:
            self.send_error(404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, status=400)
            return None

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self._read_body()
        if body is None:
            return
        if parsed.path == "/api/surfaces":
            try:
                self._send_json(create_surface(body), status=201)
            except (KeyError, ValueError, TypeError):
                self._send_json({"error": "bad geometry"}, status=400)
            return
        geometry = re.fullmatch(r"/api/surfaces/([A-Za-z0-9_-]+)/geometry", parsed.path)
        if geometry:
            try:
                updated = update_surface(geometry.group(1), body)
            except (KeyError, ValueError, TypeError):
                self._send_json({"error": "bad geometry"}, status=400)
                return
            if updated is None:
                self._send_json({"error": "not a hand-placed object"}, status=404)
            else:
                self._send_json(updated)
            return
        match = re.fullmatch(r"/api/surfaces/([A-Za-z0-9_-]+)", parsed.path)
        if not match:
            self.send_error(404)
            return
        room = load_room()
        if not any(s["id"] == match.group(1) for s in room["surfaces"]):
            self._send_json({"error": "unknown surface id"}, status=404)
            return
        self._send_json(save_enrichment(match.group(1), body))

    def do_DELETE(self):
        match = re.fullmatch(r"/api/surfaces/([A-Za-z0-9_-]+)", urlparse(self.path).path)
        if match and delete_surface(match.group(1)):
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not a hand-placed object"}, status=404)


def main():
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
