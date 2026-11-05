"""Plott et opptak fra recordings/.

    python plot_recording.py                        # nyeste opptak
    python plot_recording.py recordings/x.wav       # et bestemt opptak
    python plot_recording.py --all                  # alle, lagret som PNG
    python plot_recording.py --save                 # lagre PNG ved siden av WAV-en

To paneler over samme tidsakse: bolgeformen oppe, spektrogrammet nede.
Overskriften leser metadataene fra JSON-filen ved siden av opptaket, hvis den er der.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

OUT_DIR = Path(__file__).resolve().parent / "recordings"

# Blekk og flater. En aksentfarge til bolgeformen, resten nedtonet gratt:
# ett datasett per panel, sa fargen trenger ikke skille noe fra noe.
INK = "#1f2428"
INK_MUTED = "#5b6670"
GRID = "#e3e7ea"
ACCENT = "#2f6fd0"
FULL_SCALE = 32768.0
DB_FLOOR = -90.0  # spektrogrammets dynamiske omrade under toppen


def load(path: Path) -> tuple[np.ndarray, int, dict]:
    """Samplene som float i -1..1, samplingsraten, og metadataene."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"feil: {path.name} er ikke 16-bits PCM")
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / FULL_SCALE

    meta_path = path.with_suffix(".json")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return samples, rate, meta


def envelope(
    samples: np.ndarray, t: np.ndarray, *, columns: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Minste og storste verdi per pikselkolonne, og tiden midt i hver kolonne."""
    if samples.size <= columns:
        return samples, samples, t
    per = samples.size // columns
    used = per * columns
    block = samples[:used].reshape(columns, per)
    centres = t[:used:per] + (per / 2) * (t[1] - t[0])
    return block.min(axis=1), block.max(axis=1), centres


def subtitle(samples: np.ndarray, rate: int, meta: dict) -> str:
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    peak_db = 20 * np.log10(peak) if peak > 0 else DB_FLOOR
    parts = [
        f"{samples.size / rate:.2f} s",
        f"{rate / 1000:g} kHz",
        f"topp {peak_db:.1f} dBFS",
    ]
    distance = meta.get("distance_mm")
    if isinstance(distance, (int, float)):
        parts.append(f"avstand {int(distance)} mm")
    f1, f2 = meta.get("sweep_f1"), meta.get("sweep_f2")
    if f1 and f2:
        parts.append(f"sveip {f1:g}-{f2:g} Hz")
    return "   ·   ".join(parts)


def plot(path: Path, *, show: bool = True, save: bool = True) -> Path | None:
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    samples, rate, meta = load(path)
    if samples.size == 0:
        raise SystemExit(f"feil: {path.name} er tomt")
    t = np.arange(samples.size) / rate

    fig, (wave_ax, spec_ax) = plt.subplots(
        2,
        1,
        figsize=(10, 6.2),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 1.6], "hspace": 0.18},
    )
    fig.patch.set_facecolor("white")

    label = meta.get("label", path.stem)
    fig.text(
        0.065, 0.955, f"{label}  —  {path.name}", ha="left", fontsize=13, color=INK
    )
    fig.text(
        0.065,
        0.915,
        subtitle(samples, rate, meta),
        ha="left",
        fontsize=9.5,
        color=INK_MUTED,
    )

    # --- bolgeform ---
    # 50 000 punkter blir en solid klump. Tegn min/max per pikselkolonne i
    # stedet: samme omriss, og de enkelte periodene roter det ikke til.
    lo, hi, t_env = envelope(samples, t, columns=1100)
    wave_ax.fill_between(t_env, lo, hi, color=ACCENT, linewidth=0, alpha=0.9)
    wave_ax.set_ylabel("amplitude", fontsize=9.5, color=INK_MUTED)
    wave_ax.set_ylim(-1.05, 1.05)
    wave_ax.axhline(0, color=GRID, linewidth=1, zorder=0)

    # --- spektrogram: storrelse, sa en enkelt lysstyrkerampe (ingen regnbue) ---
    nfft = 1024
    spec, _, _, _ = spec_ax.specgram(
        samples,
        NFFT=nfft,
        Fs=rate,
        noverlap=nfft * 3 // 4,
        window=np.hanning(nfft),
        cmap="magma",
        vmin=None,
        vmax=None,
        scale="dB",
    )
    top = float(np.max(10 * np.log10(np.maximum(spec, 1e-20))))
    for image in spec_ax.get_images():
        image.set_clim(top + DB_FLOOR, top)
    spec_ax.set_ylabel("frekvens (Hz)", fontsize=9.5, color=INK_MUTED)
    spec_ax.set_xlabel("tid (s)", fontsize=9.5, color=INK_MUTED)
    spec_ax.set_ylim(0, rate / 2)

    bar = fig.colorbar(spec_ax.get_images()[-1], ax=spec_ax, pad=0.015, aspect=30)
    bar.set_label("niva (dB, relativt toppen)", fontsize=9, color=INK_MUTED)
    bar.outline.set_visible(False)
    bar.ax.tick_params(labelsize=8, colors=INK_MUTED, length=0)

    for ax in (wave_ax, spec_ax):
        ax.tick_params(labelsize=8.5, colors=INK_MUTED, length=0)
        for side in ax.spines.values():
            side.set_visible(False)
    # Rutenett bare pa bolgeformen: over spektrogrammet legger det seg som
    # hvite streker tvers over dataene.
    wave_ax.grid(True, color=GRID, linewidth=0.7)
    wave_ax.set_axisbelow(True)
    wave_ax.set_xlim(0, t[-1])

    fig.subplots_adjust(top=0.865, left=0.095, right=0.985, bottom=0.085)

    png = None
    if save:
        png = path.with_suffix(".png")
        fig.savefig(png, dpi=150, facecolor="white")
        print(f"lagret {png}")
    if show:
        plt.show()
    else:
        plt.close(fig)
    return png


def newest(out_dir: Path) -> Path:
    files = sorted(out_dir.glob("*.wav"))
    if not files:
        raise SystemExit(
            f"feil: ingen opptak i {out_dir}. Kjor `python rig_recieve.py` forst."
        )
    return max(files, key=lambda p: p.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("files", nargs="*", type=Path, help="WAV-filer (ellers nyeste)")
    parser.add_argument("--all", action="store_true", help="alle opptak i mappen")
    parser.add_argument("--dir", type=Path, default=OUT_DIR, help="mappe med opptak")
    parser.add_argument("--save", action="store_true", help="lagre PNG, ikke vis vindu")
    args = parser.parse_args()

    if args.all:
        paths = sorted(args.dir.glob("*.wav"))
        if not paths:
            print(f"feil: ingen opptak i {args.dir}", file=sys.stderr)
            return 1
    elif args.files:
        paths = args.files
    else:
        paths = [newest(args.dir)]

    for path in paths:
        if not path.exists():
            print(f"feil: {path} finnes ikke", file=sys.stderr)
            return 1
        plot(path, show=not args.save and len(paths) == 1, save=args.save or len(paths) > 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
