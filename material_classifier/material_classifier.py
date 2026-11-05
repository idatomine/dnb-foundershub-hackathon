#!/usr/bin/env python3
"""Small-data acoustic material classifier: WAV + JSON -> features -> Random Forest.

Usage:
    python material_classifier.py inspect --wav example/rom_test_run001.wav
    python material_classifier.py train --data data --out results
    python material_classifier.py predict --model results/material_rf.joblib --wav NEW.wav

Training only requires a material label in each JSON (or a material folder).
Optional object IDs enable more robust evaluation on unseen physical objects.
Without object IDs, validation is recording-level and may overestimate accuracy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import joblib
import numpy as np
import soundfile as sf
from scipy.fft import dct
from scipy.signal import resample_poly, stft
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, classification_report,
    confusion_matrix, f1_score,
)

SR = 16_000
CLASSES = ("plastic", "metal", "wood", "textile", "glass")
ALIASES = {
    "plast": "plastic", "plastic": "plastic",
    "metall": "metal", "metal": "metal",
    "wood": "wood", "tre": "wood",
    "textile": "textile", "fabric": "textile", "stoff": "textile",
    "glass": "glass", "glas": "glass",
}
BAND_EDGES = (200, 400, 800, 1200, 2000, 3000, 4500, 6000, 7500)
TARGET_MM = 150
TOLERANCE_MM = 10
EPS = 1e-12


def canonical_material(value: object) -> str | None:
    if value is None:
        return None
    return ALIASES.get(str(value).strip().casefold())


def load_metadata(wav_path: Path) -> dict:
    json_path = wav_path.with_suffix(".json")
    if not json_path.is_file():
        return {}
    with json_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict):
        raise ValueError(f"JSON metadata should be an object: {json_path}")
    return meta


def read_audio(wav_path: Path) -> np.ndarray:
    x, actual_sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    if x.size == 0:
        raise ValueError(f"Empty recording: {wav_path}")
    x = x.mean(axis=1).astype(np.float64)
    if not np.all(np.isfinite(x)):
        raise ValueError(f"Non-finite audio data: {wav_path}")
    if actual_sr != SR:
        gcd = math.gcd(int(actual_sr), SR)
        x = resample_poly(x, SR // gcd, int(actual_sr) // gcd)
    if x.size < 1024:
        raise ValueError(f"Recording is too short: {wav_path}")
    return x


def select_sweep_window(x: np.ndarray, sweep_seconds: float) -> np.ndarray:
    """Choose the fixed-length window with maximum total signal energy.

    We do not claim this separates direct speaker sound from target reflections.
    Consistent hardware and distance are essential for this first baseline.
    """
    duration = float(sweep_seconds)
    if not 0.15 <= duration <= 3.0:
        duration = 0.8
    n = min(x.size, max(1024, int(round(duration * SR))))
    if n == x.size:
        segment = x.copy()
    else:
        squared = x * x
        integral = np.concatenate(([0.0], np.cumsum(squared)))
        energies = integral[n:] - integral[:-n]
        start = int(np.argmax(energies))
        segment = x[start:start + n].copy()
    segment -= np.mean(segment)
    return segment


def mel_filterbank(freqs: np.ndarray, low: float = 200, high: float = 7500,
                   n_mels: int = 24) -> np.ndarray:
    low = max(low, float(freqs[0]))
    high = min(high, float(freqs[-1]))
    def hz_to_mel(hz):
        return 2595 * np.log10(1 + np.asarray(hz) / 700)
    def mel_to_hz(mel):
        return 700 * (10 ** (np.asarray(mel) / 2595) - 1)
    points = mel_to_hz(np.linspace(hz_to_mel(low), hz_to_mel(high), n_mels + 2))
    banks = np.zeros((n_mels, len(freqs)), dtype=np.float64)
    for i in range(n_mels):
        left, center, right = points[i:i + 3]
        banks[i] = np.maximum(0, np.minimum(
            (freqs - left) / max(center - left, EPS),
            (right - freqs) / max(right - center, EPS),
        ))
        banks[i] /= max(banks[i].sum(), EPS)
    return banks


def extract_features(wav_path: Path, metadata: dict | None = None) -> dict[str, float]:
    """Numerical descriptors of a recorded sweep, for fixed-geometry experiments.

    Extracted solely from WAV; metadata is used for sweep length only.
    Distance, filenames, time, object ID, and label are NOT model inputs.
    """
    metadata = metadata or {}
    x = read_audio(wav_path)
    try:
        sweep_seconds = float(metadata.get("sweep_seconds", 0.8))
    except (ValueError, TypeError):
        sweep_seconds = 0.8
    x = select_sweep_window(x, sweep_seconds)

    nperseg = 512
    freqs, _, z = stft(
        x, fs=SR, window="hann", nperseg=nperseg,
        noverlap=384, boundary=None, padded=False,
    )
    power = np.abs(z) ** 2
    selected = (freqs >= 200) & (freqs <= 7500)
    freqs = freqs[selected]
    power = power[selected]
    if power.size == 0 or np.sum(power) < 1e-15:
        raise ValueError(f"Silent / unusable audio: {wav_path}")

    feat: dict[str, float] = {}
    total_mean_power = float(np.mean(power))
    for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:]):
        indices = (freqs >= lo) & (freqs < hi if hi < BAND_EDGES[-1] else freqs <= hi)
        band = float(np.mean(power[indices])) if np.any(indices) else EPS
        feat[f"band_{lo}_{hi}_log_power"] = float(np.log10(band + EPS))
        feat[f"band_{lo}_{hi}_relative_db"] = float(
            10.0 * np.log10((band + EPS) / (total_mean_power + EPS))
        )

    feat["rms_log"] = float(np.log10(np.sqrt(np.mean(x * x)) + EPS))
    feat["crest_factor"] = float(np.max(np.abs(x)) / (np.sqrt(np.mean(x * x)) + EPS))
    feat["zero_cross_rate"] = float(np.mean(np.signbit(x[1:]) != np.signbit(x[:-1])))
    frame_power = np.sum(power, axis=0) + EPS
    psd_shape = power / frame_power[None, :]
    centroid = np.sum(freqs[:, None] * psd_shape, axis=0)
    bandwidth = np.sqrt(np.sum((freqs[:, None] - centroid[None, :]) ** 2 * psd_shape, axis=0))
    flatness = np.exp(np.mean(np.log(power + EPS), axis=0)) / (np.mean(power, axis=0) + EPS)
    cumulative = np.cumsum(power, axis=0)
    rolloff_idxs = np.argmax(cumulative >= 0.85 * frame_power[None, :], axis=0)
    rolloff = freqs[rolloff_idxs]
    # Frame-to-frame spectral change; captures differences in sweep envelope.
    flux = np.sqrt(np.sum(np.diff(psd_shape, axis=1) ** 2, axis=0))
    descriptors = {"centroid_hz": centroid, "bandwidth_hz": bandwidth,
                   "flatness": flatness, "rolloff85_hz": rolloff, "flux": flux}
    for name, values in descriptors.items():
        feat[f"{name}_mean"] = float(np.mean(values))
        feat[f"{name}_std"] = float(np.std(values))

    filterbank = mel_filterbank(freqs)
    mel_power = filterbank @ power
    cepstra = dct(np.log(mel_power + EPS), type=2, axis=0, norm="ortho")
    # MFCC 0 mostly encodes overall amplitude; retain 1-10 as spectral shape.
    for i in range(1, 11):
        feat[f"mfcc_{i:02d}_mean"] = float(np.mean(cepstra[i]))
        feat[f"mfcc_{i:02d}_std"] = float(np.std(cepstra[i]))

    if any(not np.isfinite(v) for v in feat.values()):
        raise ValueError(f"Non-finite features: {wav_path}")
    return feat


def describe_audio(wav_path: Path, meta: dict) -> None:
    x = read_audio(wav_path)
    feat = extract_features(wav_path, meta)
    print(f"File: {wav_path}")
    print(f"Processed sample rate: {SR} Hz")
    print(f"Original/processed duration: {len(x) / SR:.3f} s")
    print(f"Audio peak: {np.max(np.abs(x)):.3f} (full-scale amplitude)")
    print(f"Features extracted: {len(feat)}")
    if "distance_mm" in meta:
        distance = float(meta["distance_mm"])
        print(f"Recorded distance: {distance:.0f} mm")
        if not TARGET_MM - TOLERANCE_MM <= distance <= TARGET_MM + TOLERANCE_MM:
            print("NOTE: Distance differs from the suggested initial 140-160 mm setup.")
    for name in ("rms_log", "centroid_hz_mean", "flatness_mean", "rolloff85_hz_mean"):
        print(f"  {name}: {feat[name]:.4f}")
    print("No material prediction is possible until the model is trained on labeled examples.")


def find_labels_and_groups(root: Path, wav_path: Path, meta: dict) -> tuple[str, str | None]:
    """Get material from JSON or folder; physical-object ID is entirely optional."""
    relative = wav_path.relative_to(root)
    parts = relative.parts
    folder_label = canonical_material(parts[0]) if len(parts) > 1 else None
    # `label` in the original acquisition JSON is a free-form test name (rom_test).
    # Only `material` is the supervised ground-truth label.
    json_label = canonical_material(meta.get("material"))
    if folder_label and json_label and folder_label != json_label:
        raise ValueError(f"Conflicting folder/JSON material labels: {wav_path}")
    material = json_label or folder_label
    if material is None:
        raise ValueError(f"Unknown material: {wav_path}. Add material to JSON or use data/<material>/.")
    object_id = meta.get("object_id")
    # Optional object subfolders: data/wood/wood_01/run001.wav
    if object_id is None and len(parts) >= 3 and folder_label is not None:
        object_id = parts[1]
    if object_id is None or not str(object_id).strip():
        return material, None
    return material, f"{material}:{str(object_id).strip()}"


def collect_dataset(root: Path):
    if not root.is_dir():
        raise ValueError(f"Training folder does not exist: {root}")
    paths = sorted(root.rglob("*.wav")) + sorted(root.rglob("*.WAV"))
    if not paths:
        raise ValueError(f"No WAV files in {root}")
    records, feature_rows, errors = [], [], []
    for wav_path in paths:
        try:
            meta = load_metadata(wav_path)
            material, object_id = find_labels_and_groups(root, wav_path, meta)
            features = extract_features(wav_path, meta)
            distance = meta.get("distance_mm")
            if distance is not None:
                distance = float(distance)
                if not TARGET_MM - TOLERANCE_MM <= distance <= TARGET_MM + TOLERANCE_MM:
                    print(f"Distance note: {wav_path.name}: {distance:.0f} mm (suggested 140-160 mm)")
            else:
                print(f"Distance unavailable: {wav_path.name}. Record distance_mm in JSON for quality control.")
            feature_rows.append(features)
            records.append({"wav": str(wav_path), "material": material,
                            "object_id": object_id, "distance_mm": distance})
        except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{wav_path}: {exc}")
    if errors:
        print("\nProblematic training files:", file=sys.stderr)
        for msg in errors[:25]:
            print("  " + msg, file=sys.stderr)
        raise ValueError("Correct the files above; no partial dataset was trained.")
    names = sorted(feature_rows[0])
    X = np.asarray([[row[n] for n in names] for row in feature_rows], dtype=np.float64)
    y = np.asarray([rec["material"] for rec in records])
    groups = np.asarray([rec["object_id"] for rec in records], dtype=object)
    return X, y, groups, names, records


def object_stratified_folds(y: np.ndarray, groups: np.ndarray, maximum: int = 5):
    """Balanced grouped folds: one or more unseen objects *per class* in each fold.

    Each unique object must have exactly one true class. Suitable for small,
    balanced experimental datasets of a few distinct objects per material.
    """
    groups_by_class: dict[str, list[str]] = defaultdict(list)
    for cls in sorted(set(y)):
        for group in sorted(set(groups[y == cls])):
            groups_by_class[cls].append(group)
    minimum = min(len(v) for v in groups_by_class.values())
    if minimum < 2:
        return []
    k = min(maximum, minimum)
    rng = np.random.default_rng(42)
    test_groups_per_fold: list[set[str]] = [set() for _ in range(k)]
    for cls in sorted(groups_by_class):
        obj = np.asarray(groups_by_class[cls], dtype=object)
        rng.shuffle(obj)
        for fold_id, chunk in enumerate(np.array_split(obj, k)):
            test_groups_per_fold[fold_id].update(chunk.tolist())
    splits = []
    for fold_groups in test_groups_per_fold:
        test = np.flatnonzero(np.isin(groups, list(fold_groups)))
        train = np.flatnonzero(~np.isin(groups, list(fold_groups)))
        assert len(test) and len(train)
        assert set(groups[train]).isdisjoint(groups[test])
        assert set(y[train]) == set(y[test]) == set(y)
        splits.append((train, test))
    return splits


def make_model(n_trees: int = 300) -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=n_trees, max_features="sqrt",
        min_samples_leaf=2, class_weight="balanced_subsample",
        random_state=42, n_jobs=-1,
    )


def write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def write_confusion_plot(path: Path, y_true: np.ndarray, y_pred: np.ndarray) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cm = confusion_matrix(y_true, y_pred, labels=CLASSES)
    fig, ax = plt.subplots(figsize=(6.0, 5.0))
    ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(CLASSES)), CLASSES)
    ax.set_yticks(range(len(CLASSES)), CLASSES)
    ax.set_xlabel("Predicted material")
    ax.set_ylabel("True material")
    ax.set_title("Cross-validation predictions")
    for i in range(len(CLASSES)):
        for j in range(len(CLASSES)):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center", color="black")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def train(data: Path, out: Path, n_trees: int, max_folds: int) -> None:
    X, y, groups, names, records = collect_dataset(data)
    missing = sorted(set(CLASSES) - set(y))
    if missing:
        raise ValueError(f"Need all {len(CLASSES)} classes. Missing: {', '.join(missing)}")
    if any(n < 2 for n in Counter(y).values()):
        raise ValueError("Need at least 2 recordings per class; 10–20 is a better starting target.")

    out.mkdir(parents=True, exist_ok=True)
    # Grouped validation is allowed ONLY if every recording has a trustworthy object ID.
    has_all_object_ids = all(g is not None for g in groups)
    if has_all_object_ids:
        n_objects = {label: len(set(groups[y == label])) for label in CLASSES}
        splits = object_stratified_folds(y, groups, maximum=max_folds)
        cv_type = "object-grouped" if splits else "unavailable: too few objects"
    else:
        n_objects = None
        n_folds = min(max_folds, min(Counter(y).values()))
        splits = list(StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42).split(X, y))
        cv_type = "recording-level stratified (may be optimistic)"

    lines = ["ACOUSTIC MATERIAL CLASSIFIER — RANDOM FOREST", "",
             f"Classes: {', '.join(CLASSES)}", f"Recordings: {len(y)}",
             f"Acoustic features: {len(names)}", "",
             "Recordings per material: " + str(dict(Counter(y))),
             "Unique physical objects per material: " + (str(n_objects) if n_objects is not None else "unknown (object_id not provided)"),
             "Validation: " + cv_type,
             "Distance and JSON material labels are NOT classifier features.",
             "These features describe the recorded signal, not a separated reflection.", ""]
    distances = [rec["distance_mm"] for rec in records if rec["distance_mm"] is not None]
    if distances:
        print(f"Measured distance range: {min(distances):.0f}-{max(distances):.0f} mm")
        lines.append(f"Measured distance range: {min(distances):.0f}-{max(distances):.0f} mm")
        if max(distances) - min(distances) > 20:
            lines.append("WARNING: Distance varies by >20 mm; it may reduce accuracy.")

    metrics = {"classes": list(CLASSES), "n_recordings": int(len(y)),
               "n_unique_objects": (int(len(set(groups))) if has_all_object_ids else None),
               "recordings_per_class": dict(Counter(y)), "objects_per_class": n_objects,
               "cv_type": cv_type}
    if splits:
        preds = np.empty(y.shape, dtype=object)
        for i, (train_idx, test_idx) in enumerate(splits, 1):
            fold_model = make_model(n_trees)
            fold_model.fit(X[train_idx], y[train_idx])
            preds[test_idx] = fold_model.predict(X[test_idx])
            print(f"Fold {i}/{len(splits)}: {len(train_idx)} train, {len(test_idx)} test; {cv_type}")
        metrics.update({"evaluated": True, "cv_folds": len(splits),
                        "accuracy": float(accuracy_score(y, preds)),
                        "balanced_accuracy": float(balanced_accuracy_score(y, preds)),
                        "macro_f1": float(f1_score(y, preds, average="macro"))})
        lines.extend([f"Folds: {len(splits)}", f"Accuracy: {metrics['accuracy']:.3f}",
                      f"Balanced accuracy: {metrics['balanced_accuracy']:.3f}",
                      f"Macro F1: {metrics['macro_f1']:.3f}", "",
                      classification_report(y, preds, labels=list(CLASSES), zero_division=0)])
        if cv_type.startswith("recording-level"):
            lines.extend(["WARNING: Recording-level validation can overestimate performance",
                          "when repeats of the same physical sample appear in training and test.",
                          "Do NOT report this as accuracy on previously unseen objects."])
        else:
            lines.append("Validation used previously unseen physical objects.")
        write_csv(out / "cv_predictions.csv",
                  ["wav", "object_id", "true_material", "predicted_material", "correct"],
                  [[r["wav"], r["object_id"] or "", r["material"], str(pred), int(r["material"] == pred)]
                   for r, pred in zip(records, preds)])
        write_confusion_plot(out / "confusion_matrix.png", y, preds)
    else:
        metrics.update({"evaluated": False, "cv_folds": 0, "accuracy": None})
        lines.extend(["VALIDATION NOT POSSIBLE: need at least two distinct physical",
                      "objects for every class to evaluate on unseen objects."])
        print("WARNING: Insufficient distinct objects for grouped evaluation; saving model only.")

    model = make_model(n_trees)
    model.fit(X, y)
    bundle = {"model": model, "feature_names": names, "classes": list(model.classes_),
              "sample_rate": SR, "version": 1, "recommended_distance_mm": TARGET_MM,
              "note": "Fixed-geometry pilot; class vote fractions are not calibrated probabilities."}
    joblib.dump(bundle, out / "material_rf.joblib")
    feature_importances = sorted(zip(names, model.feature_importances_), key=lambda pair: -pair[1])
    write_csv(out / "feature_importance.csv", ["feature", "importance"], feature_importances)
    (out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nModel and reports saved to: {out.resolve()}")


def predict(model_path: Path, wav_path: Path) -> None:
    if not model_path.is_file():
        raise ValueError(f"Model not found: {model_path}. Train it first.")
    bundle = joblib.load(model_path)  # Only load a joblib file you trust.
    if bundle.get("version") != 1:
        raise ValueError("Unsupported model format.")
    meta = load_metadata(wav_path)
    feat = extract_features(wav_path, meta)
    names = bundle["feature_names"]
    vector = np.array([[feat[name] for name in names]], dtype=np.float64)
    model = bundle["model"]
    label = str(model.predict(vector)[0])
    probabilities = model.predict_proba(vector)[0]
    ranking = sorted(zip(model.classes_, probabilities), key=lambda x: -x[1])
    print(f"Recording: {wav_path}")
    if meta.get("distance_mm") is not None:
        distance = float(meta["distance_mm"])
        print(f"Distance: {distance:.0f} mm")
        if not 140 <= distance <= 160:
            print("WARNING: Outside initial 140-160 mm range; interpretation is less reliable.")
    else:
        print("WARNING: No distance_mm in JSON; cannot confirm matching geometry.")
    print(f"Prediction: {label.upper()}")
    print("Random Forest class vote fractions (not calibrated probabilities):")
    for class_name, score in ranking:
        print(f"  {class_name:8s}: {score * 100:5.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_cmd = sub.add_parser("inspect", help="Check a WAV and extract features (does not predict).")
    inspect_cmd.add_argument("--wav", required=True, type=Path)
    train_cmd = sub.add_parser("train", help="Train on labeled WAV + JSON recordings.")
    train_cmd.add_argument("--data", type=Path, default=Path("data"))
    train_cmd.add_argument("--out", type=Path, default=Path("results"))
    train_cmd.add_argument("--trees", type=int, default=300)
    train_cmd.add_argument("--folds", type=int, default=5)
    predict_cmd = sub.add_parser("predict", help="Classify a new WAV after training.")
    predict_cmd.add_argument("--model", type=Path, default=Path("results/material_rf.joblib"))
    predict_cmd.add_argument("--wav", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            describe_audio(args.wav, load_metadata(args.wav))
        elif args.command == "train":
            if args.trees < 10 or args.folds < 2:
                raise ValueError("--trees must be >=10 and --folds >=2")
            train(args.data, args.out, args.trees, args.folds)
        elif args.command == "predict":
            predict(args.model, args.wav)
    except (ValueError, FileNotFoundError, OSError, KeyError, RuntimeError) as exc:
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
