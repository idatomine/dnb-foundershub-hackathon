"""Bilde -> sannsynlighet for materiale (tekstil, metall, tre, plast, glass) med CLIP zero-shot.

    python vision_material.py path/to/image.jpg
    python vision_material.py --webcam

Ingen trening: en ferdigtrent CLIP-modell sammenligner bildet med tekstbeskrivelser.
predict_material() returnerer {"textile": p, "metal": p, "wood": p, "plastic": p, "glass": p} med sum 1.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

MODEL_ID = "openai/clip-vit-large-patch14-336"


def _model_is_cached() -> bool:
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    hub_cache = Path(os.environ.get("HF_HUB_CACHE", hf_home / "hub"))
    snapshots = hub_cache / f"models--{MODEL_ID.replace('/', '--')}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


# Ligger modellen i cachen, kjør offline. Ellers sjekker transformers huben ved
# hver oppstart og henger når nettet er tregt. Må settes før transformers importeres.
if _model_is_cached():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import pipeline  # noqa: E402

# CLIP-etikett (naturlig engelsk) -> nøkkel i resultatet.
LABELS = {"fabric": "textile", "metal": "metal", "wood": "wood", "plastic": "plastic", "glass": "glass"}
TEMPLATES = ["a photo of a {} object", "an object made of {}"]
NORWEGIAN = {"textile": "TEKSTIL", "metal": "METALL", "wood": "TRE", "plastic": "PLAST", "glass": "GLASS"}

CAPTURES_DIR = Path(__file__).resolve().parent / "captures"

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

# Lastes én gang ved import, ikke per kall.
_classifier = pipeline("zero-shot-image-classification", model=MODEL_ID, device=DEVICE)


def _to_rgb_image(image) -> Image.Image:
    """Godtar PIL.Image, filsti (str/Path) eller BGR-ramme fra OpenCV."""
    if isinstance(image, (str, Path)):
        image = Image.open(image)
    elif isinstance(image, np.ndarray):
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        else:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(image)
    if not isinstance(image, Image.Image):
        raise TypeError(f"Ukjent bildetype: {type(image).__name__}")
    return image.convert("RGB")


def predict_material(image) -> dict:
    """Returnerer {"textile": float, "metal": float, "wood": float, "plastic": float, "glass": float} med sum 1."""
    image = _to_rgb_image(image)
    totals = {key: 0.0 for key in LABELS.values()}

    # Softmax per mal (pipelinen gjør det), så snitt over malene.
    for template in TEMPLATES:
        results = _classifier(image, candidate_labels=list(LABELS), hypothesis_template=template)
        for result in results:
            totals[LABELS[result["label"]]] += result["score"] / len(TEMPLATES)

    total = sum(totals.values())
    return {key: value / total for key, value in totals.items()}


def capture_webcam_image(camera_index: int = 0, warmup_frames: int = 10) -> Image.Image:
    """Tar ett bilde fra webkameraet. De første rammene kastes så eksponeringen stabiliseres."""
    camera = cv2.VideoCapture(camera_index)
    try:
        if not camera.isOpened():
            raise RuntimeError(
                f"Får ikke åpnet kamera {camera_index}. På macOS: gi terminalen/IDE-en "
                "kameratilgang i Systeminnstillinger > Personvern og sikkerhet > Kamera, "
                "og start terminalen/IDE-en på nytt."
            )
        for _ in range(warmup_frames):
            camera.read()
        ok, frame = camera.read()
        if not ok or frame is None:
            raise RuntimeError(f"Kamera {camera_index} ga ingen ramme.")
    finally:
        camera.release()
    return _to_rgb_image(frame)


def norwegian_line(probs: dict) -> str:
    """F.eks. 'TRE 81.2% | METALL 12.0% | TEKSTIL 4.1% | PLAST 1.5% | GLASS 1.2%', høyest først."""
    ranked = sorted(probs.items(), key=lambda item: item[1], reverse=True)
    return " | ".join(f"{NORWEGIAN[key]} {value * 100:.1f}%" for key, value in ranked)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("image", nargs="?", help="sti til bilde")
    source.add_argument("--webcam", action="store_true", help="ta bilde med webkameraet")
    parser.add_argument("--camera", type=int, default=0, help="kameraindeks (standard 0)")
    args = parser.parse_args()

    if args.webcam:
        image = capture_webcam_image(args.camera)
        CAPTURES_DIR.mkdir(exist_ok=True)
        path = CAPTURES_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jpg"
        image.save(path)
        print(f"Lagret {path}")
    else:
        image = args.image

    start = time.perf_counter()
    probs = predict_material(image)
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(json.dumps(probs, indent=2))
    print(f"Inferens: {elapsed_ms:.0f} ms ({DEVICE})")
    print(norwegian_line(probs))


if __name__ == "__main__":
    main()
