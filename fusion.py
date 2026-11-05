"""Slår sammen sannsynligheter fra bilde (CLIP) og lyd (Random Forest).

    python fusion.py malinger/plast_bord.json
    python fusion.py malinger/plast_bord.json --write      # lagrer "audio" og "combined" i JSON-en
    python fusion.py malinger/plast_bord.json --w-vision 0.5 --floor 0.01

JSON-filen må ha "vision" (lagret av maaling.py) og en WAV med samme navn ved
siden av. Lydmodellen må være trent først:

    cd material_classifier && python material_classifier.py train

Kombinasjonen (Bayes med vekting):
    kombinert ∝ bilde^w_vision × (lyd + gulv)
- w_vision < 1 demper bildet. CLIP er ofte overdrevent sikker på utseendet,
  mens lyden måler selve materialet.
- gulvet hindrer at en 0 fra lyden sletter et materiale helt (Random Forest
  gir stemmeandeler, som kan være nøyaktig 0).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ("textile", "metal", "wood", "plastic", "glass")
DEFAULT_MODEL = ROOT / "material_classifier" / "results" / "material_rf.joblib"


def _normalize(p: dict) -> dict:
    s = sum(p.values())
    if s <= 0:
        return {k: 1 / len(p) for k in p}
    return {k: v / s for k, v in p.items()}


def combine(vision: dict, audio: dict, floor: float = 0.01, w_vision: float = 0.5) -> dict:
    """Returnerer kombinert sannsynlighet for de fem materialene, sum 1."""
    v = _normalize({k: float(vision.get(k, 0.0)) for k in KEYS})
    a = _normalize({k: float(audio.get(k, 0.0)) + floor for k in KEYS})
    return _normalize({k: (v[k] ** w_vision) * a[k] for k in KEYS})


def audio_proba(wav_path: Path, model_path: Path = DEFAULT_MODEL) -> dict:
    """Lyd -> dict med de fem materialene, fra den trente Random Forest-modellen."""
    if not model_path.is_file():
        raise FileNotFoundError(
            f"Fant ikke lydmodellen: {model_path}\n"
            "Tren den først: cd material_classifier && python material_classifier.py train"
        )
    sys.path.insert(0, str(ROOT / "material_classifier"))
    import joblib
    import numpy as np
    from material_classifier import extract_features, load_metadata

    bundle = joblib.load(model_path)
    feat = extract_features(wav_path, load_metadata(wav_path))
    vector = np.array([[feat[n] for n in bundle["feature_names"]]], dtype=np.float64)
    model = bundle["model"]
    scores = dict(zip((str(c) for c in model.classes_), model.predict_proba(vector)[0]))
    return {k: float(scores.get(k, 0.0)) for k in KEYS}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", type=Path, help="måle-JSON med 'vision' (fra maaling.py)")
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--floor", type=float, default=0.01)
    ap.add_argument("--w-vision", type=float, default=0.5)
    ap.add_argument("--write", action="store_true", help="lagre 'audio' og 'combined' i JSON-filen")
    args = ap.parse_args()

    meta = json.loads(args.json.read_text(encoding="utf-8"))
    vision = meta.get("vision")
    if not vision:
        print(f"{args.json} mangler 'vision'. Kjør maaling.py først.", file=sys.stderr)
        return 1
    wav = args.json.with_suffix(".wav")
    audio = audio_proba(wav, args.model)
    combined = combine(vision, audio, args.floor, args.w_vision)

    result = {
        "vision": {k: round(float(vision.get(k, 0.0)), 4) for k in KEYS},
        "audio": {k: round(v, 4) for k, v in audio.items()},
        "combined": {k: round(v, 4) for k, v in combined.items()},
    }
    print(json.dumps(result, indent=2))

    if args.write:
        meta["audio"] = result["audio"]
        meta["combined"] = result["combined"]
        args.json.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
