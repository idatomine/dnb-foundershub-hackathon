# Acoustic material classifier — 5 materials

**Classes:** `plastic`, `metal`, `wood`, `textile`, `glass`.

This is a baseline **Random Forest** classifier based on 49 features from each WAV sweep. It is not pretrained: collect your own labeled WAV/JSON measurements first.

## 1. Measure

Use the same room, speaker volume, microphone gain, recording setup, and angle. For the initial experiment, aim for **150 mm ± 10 mm** and use the real `distance_mm` value in each JSON. Record at least **10–20 measurements per material** for a first pilot, but try to obtain multiple physical samples per material when possible. This is not a guarantee of performance.

Each WAV file should have a matching JSON with the **same filename stem**. The only *new* JSON field required is `material`.

```json
{
  "run": 1,
  "label": "rom_test",
  "material": "glass",
  "rate": 16000,
  "distance_mm": 149,
  "sweep_f1": 200,
  "sweep_f2": 7500,
  "sweep_seconds": 0.8,
  "file": "run001.wav"
}
```

Keep your other original JSON fields too. `label` can remain a generic test name; `material` is the ground-truth label.

## 2. Folder layout

You can organize files as follows:

```
data/
  plastic/
    run001.wav
    run001.json
  metal/
    run001.wav
    run001.json
  wood/
    run001.wav
    run001.json
  textile/
    run001.wav
    run001.json
  glass/
    run001.wav
    run001.json
```

You may instead put all WAV+JSON pairs directly under `data/`, as long as each JSON provides `material`. Extra subfolders are allowed.

**`air` / `nothing` is not a sixth class here.** Store a no-object recording separately as a *baseline* for a possible future signal-processing improvement; do not include it under `data/` in this five-class training run.

## 3. Installation

Python 3.10+:

```bash
python -m pip install -r requirements.txt
```

## 4. Inspect your existing example

```bash
python material_classifier.py inspect --wav example/rom_test_run001.wav
```

This example was recorded at 623 mm and is *not* appropriate as a labeled 150 mm training recording. It is included only to verify that WAV parsing/feature extraction works.

## 5. Train your five-class model

```bash
python material_classifier.py train --data data --out results
```

It saves `material_rf.joblib`, `report.txt`, `metrics.json`, `feature_importance.csv`, and, when validation is possible, `confusion_matrix.png` and `cv_predictions.csv`.

**Evaluation limitations:**

- If no `object_id` is supplied, the code evaluates by separating *recordings*, not physical objects. This is a quick sanity check, but accuracy may be optimistic, especially when many WAVs come from one physical sample.
- If you later provide a consistent `object_id` in every JSON, or place files in `data/material/object_id/` subfolders, the code automatically switches to *unseen-object* validation. If fewer than 2 distinct objects exist in any material, it skips validation instead of inventing a generalization score.
- The Random Forest uses WAV-derived features, not distance or material as inputs.

## 6. Predict a new WAV

```bash
python material_classifier.py predict --model results/material_rf.joblib --wav new_recording.wav
```

Provide `new_recording.json` alongside it if possible so the code can check the distance and extract the correct sweep duration. The output prints the class prediction and vote fractions (not calibrated probabilities).

## Important scientific limitation

The baseline uses a high-energy sweep segment, not a fully isolated object reflection. At 15 cm there can be substantial direct speaker-to-microphone leakage. Changing object size, thickness, mounting, or angle may change the measured sound. A second phase could use the actual emitted sweep and a no-object baseline for stronger signal processing.
