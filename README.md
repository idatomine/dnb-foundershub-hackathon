# Satselaget: what is it made of?

Point the rig at an object and press the button. The rig plays a sound sweep and records the echo, the laptop takes a photo, and two models each guess the material: **textile, metal, wood, plastic or glass**. The guesses are combined into one answer, and the dashboard shows the result on the object in a 3D scan of the room.

The image sees what things *look like*. The sound hears what they *are*. A plastic table with a wood-grain pattern fools the camera, but not necessarily the sound.

## How it fits together

```text
 button 1 on the board
        │
        ▼
 the rig (rig/)  ── sweep 200 Hz–7.5 kHz from the speaker, the microphone records
        │ USB
        ▼
 maaling.py  ──►  malinger/<name>.wav + .json + .png
        │
        ├─► webcam ─► vision_material.py (CLIP)             ─► "vision"
        │
        └─► fusion.py ─► material_classifier (Random Forest) ─► "audio"
                     └─► Bayes: image × sound               ─► "combined"
                                                                 │
                                                                 ▼
                                                 dashboard/ (3D room in the browser)
```

| Part | File | What it does |
|---|---|---|
| Firmware | `rig/project.yaml` | Sweep, recording, screen and sensors on the Atech board. See `CLAUDE.md` for ports and pitfalls. |
| Receiving | `rig_recieve.py` | Fetches the recording over USB and saves WAV + JSON to `recordings/`. |
| Plotting | `plot_recording.py` | Waveform and spectrogram of a recording. |
| Measuring | `maaling.py` | One button press: sound + photo + image guess, saved to `malinger/`. |
| Image model | `vision_material.py` | CLIP zero-shot. No training. |
| Sound model | `material_classifier/` | Random Forest trained on our own recordings. Has its own [README](material_classifier/README.md). |
| Fusion | `fusion.py` | Combines image and sound into one probability. |
| Dashboard | `dashboard/` | 3D scan of the room with material data per object. Has its own [README](dashboard/README.md). |

Every part uses the same five keys: `textile`, `metal`, `wood`, `plastic`, `glass`.

## Setup

Requires Python 3.10 or newer. Tested with 3.12 on a Mac with Apple Silicon (the image model runs on the GPU via `mps`), but it also works on CPU. Run everything from the repo root.

### 1. Get the code and create a virtual environment

macOS or Linux:

```bash
git clone https://github.com/idatomine/satselag-hacakthon.git
cd satselag-hacakthon
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt -r material_classifier/requirements.txt
```

Windows (PowerShell). `py -0p` lists the Python versions you have:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r material_classifier/requirements.txt
```

Activate the environment in every new terminal. If you already have the repo, `git pull` and then run `pip install` again.

`python --version` should show 3.10 or newer. If you get `No matching distribution found for atech`, your Python is too old: delete `.venv` and create it again with a newer version.

### 2. Download the image model (about 1.7 GB, once)

```bash
python -c "from transformers import pipeline; pipeline('zero-shot-image-classification', model='openai/clip-vit-large-patch14-336'); print('done')"
```

The model is stored in the Hugging Face cache in your home folder, not in the repo:

```text
~/.cache/huggingface/hub/models--openai--clip-vit-large-patch14-336
```

If the network is slow, a teammate can AirDrop you this folder. Put it in the same place and you skip the download. Once the model is in the cache, `vision_material.py` runs fully offline.

### 3. Give the terminal camera access (macOS)

```bash
python -c "import cv2; c=cv2.VideoCapture(0); print('camera ok' if c.read()[0] else 'camera FAILED'); c.release()"
```

If you get `camera FAILED`: go to System Settings → Privacy & Security → Camera, turn on the terminal app you use, restart the terminal and try again.

### 4. Upload the firmware to the board

Connect the board with a USB-C cable that carries data, and run:

```bash
atech validate rig
atech build rig
atech upload rig
```

The first `atech build` downloads the ESP32 compiler, about 2.4 GB, once. Only one program can use the board's port at a time, so stop `atech monitor` before running the scripts below.

## Take a measurement

```bash
python maaling.py --merkelapp curtain     # waits for button 1 on the board
python maaling.py --trigg                 # start from the laptop instead
python maaling.py --antall 5              # stop after five measurements
```

Each measurement creates four files with the same name in `malinger/`: the sound (`.wav`), the measurement data and image guess (`.json`), a plot (`.png`) and the photo the model looked at (`.jpg`).

Sound only, without a photo: `python rig_recieve.py --label empty_rig --plot` saves to `recordings/`.

## The image model: `vision_material.py`

A pretrained CLIP model compares the image with text such as "an object made of wood". No training of our own.

```bash
python vision_material.py test.jpg      # from an image file
python vision_material.py --webcam      # takes a photo with the webcam, saved to captures/
```

```text
{"textile": 0.05, "metal": 0.86, "wood": 0.01, "plastic": 0.05, "glass": 0.03}
Inferens: 715 ms (mps)
METALL 86.0% | TEKSTIL 5.0% | PLAST 5.0% | GLASS 3.0% | TRE 1.0%
```

From code:

```python
from vision_material import predict_material, capture_webcam_image

p = predict_material(capture_webcam_image())   # or predict_material("test.jpg")
# {"textile": ..., "metal": ..., "wood": ..., "plastic": ..., "glass": ...}, sums to 1
```

The model loads once on import (7–10 seconds). After that each image takes about 0.5 seconds.

**For best results:** let the object fill most of the frame, use a plain background (a wooden table behind it can make the model answer "wood"), and use good, even lighting.

## The sound model: `material_classifier/`

Extracts 49 sound features from the sweep and classifies them with a Random Forest. It has to be trained on our own recordings, sorted into `material_classifier/data/<material>/`:

```bash
cd material_classifier
python material_classifier.py train --data data --out results
python material_classifier.py predict --model results/material_rf.joblib --wav new.wav
```

Measure at the same distance every time, about 150 mm. Details in [material_classifier/README.md](material_classifier/README.md).

## Combining image and sound: `fusion.py`

```bash
python fusion.py malinger/plast_bord.json            # print vision, audio and combined
python fusion.py malinger/plast_bord.json --write    # save them into the JSON file
python fusion.py malinger/plast_bord.json --w-vision 0.3
```

The combination is a weighted Bayes update:

```text
combined ∝ image^w_vision × (sound + floor)
```

- `--w-vision` (default 0.5) weakens the image. CLIP is often overconfident about appearance, while the sound measures the material itself. A lower value gives the sound more say.
- `--floor` (default 0.01) stops a 0 from the sound model from wiping out a material completely.

Requires a trained sound model in `material_classifier/results/`.

## The dashboard

```bash
python dashboard/server.py      # open http://localhost:8765
```

Click an object in the 3D scan to see and enter material data. See [dashboard/README.md](dashboard/README.md).

## Testing without a board

```bash
python demo/host.py --sim
atech validate demo
atech build demo
python demo/full_sim_test.py
```

Everything is fine when `host.py` prints `Simulering fullført.`, `validate` prints `ok`, `build` prints `SUCCESS` and the test prints `20 av 20 bestått`. The tests use a simulated board and don't run the firmware, so modules and uploading have to be tested on the real board.

## Status and limitations

- **The sound model is a pilot.** It's trained on 14 recordings, with only one each of glass, metal and wood, so there isn't enough data to measure how accurate it is. Recording 10–20 times per material at a fixed distance and retraining is what will help most.
- **The sound model also knows "air"** (no object). `fusion.py` only looks at the five materials and assumes something is in front of the rig.
- **The distance varies a lot** in the existing recordings (66–1943 mm), and the distance sensor reported `null` in some measurements.
- **The dashboard partly shows made-up numbers** from `dashboard/data/mock_predictions.json` where the models haven't produced real results yet.

## Troubleshooting

| Problem | Fix |
|---|---|
| `camera FAILED` | Give the terminal camera access (step 3). If the camera light turns on but it doesn't work, try `--camera 1` (`--kamera 1` in `maaling.py`). |
| `ModuleNotFoundError` | The environment isn't active. Run `source .venv/bin/activate`. |
| `Fant ikke lydmodellen` (sound model not found) | Train the sound model first (see above). |
| The port is busy | Only one program can use the board. Stop `atech monitor` or the other script. |
| `command not found: ❯` | You copied the `❯` prompt character too. Type the command without it. |
| "Redirects are currently not supported in MacOs" | Harmless message from torch. Ignore it. |

## Built with Atech

Atech is a modular electronics kit: modules slot into numbered ports on an ESP32 board, no soldering. The prompt from atech.dev is in `CLAUDE.md`, together with the rig's ports and known pitfalls. Open Claude Code in the repo root to keep building. Set the API key as the environment variable `ANTHROPIC_API_KEY`, never in the code or in git.

| File | Source |
|---|---|
| `CLAUDE.md` | The prompt from atech.dev/docs, plus the rig's hardware description |
| `demo/AGENTS.md`, `demo/CLAUDE.md`, `demo/README.md` | Generated by `atech new` |
| `docs/llms.txt` | atech.dev/llms.txt |
| `.mcp.json` | The address of Atech's MCP connection |

Help: `atech guide`, [atech.dev/docs](https://atech.dev/docs) and [Discord](https://discord.gg/B6kuhUW27S).
