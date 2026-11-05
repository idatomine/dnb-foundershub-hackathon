"""Ett knappetrykk -> lydopptak + bilde + materialgjetning, alt i malinger/.

    python maaling.py                      # vent pa knapp 1 pa kortet
    python maaling.py --merkelapp gardin   # gi malingene et navn
    python maaling.py --trigg              # start fra laptopen i stedet for knappen
    python maaling.py --antall 5           # stopp etter fem malinger

Trykk pa knappen i port 1: kortet spiller sveipen og sender opptaket hit, sa
tas det et bilde med webkameraet, og CLIP-modellen gjetter materialet ut fra
det bildet. Hver maling gir fire filer med samme navn i malinger/:

    <navn>.wav    lyden
    <navn>.json   maledata fra kortet + materialgjetningen
    <navn>.png    bolgeform og spektrogram
    <navn>.jpg    bildet modellen sa pa

Bildet tas forst nar hele opptaket er mottatt. Tar vi det underveis, slutter
vi a lese USB-porten i et sekund, og kortet (som gir opp etter 250 ms) mister
biter av opptaket.

Bare ett program kan holde kortets port om gangen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from atech import ActionRejected, Board, NoAcknowledgement, NoBoardFoundError
from atech.runtime.transport import PortUnavailableError

import plot_recording
import rig_recieve

# vision_material laster CLIP-modellen ved import (7-10 s). Det skjer en gang
# her, ikke per maling.
import vision_material

OUT_DIR = Path(__file__).resolve().parent / "malinger"


def siste_bilde(mappe: Path) -> Path:
    """Det nyeste bildet i mappa. Modellen jobber alltid pa sist tatt bilde."""
    bilder = list(mappe.glob("*.jpg"))
    if not bilder:
        raise FileNotFoundError(f"ingen bilder i {mappe}")
    return max(bilder, key=lambda p: p.stat().st_mtime)


def ta_bilde(stem: str, out_dir: Path, kamera: int) -> Path:
    """Tar ett bilde og lagrer det ved siden av opptaket."""
    bilde = vision_material.capture_webcam_image(kamera)
    sti = out_dir / f"{stem}.jpg"
    bilde.save(sti)
    return sti


def klassifiser(bilde_sti: Path) -> dict:
    """Materialsannsynligheter for bildet, summerer til 1."""
    start = time.perf_counter()
    sannsyn = vision_material.predict_material(bilde_sti)
    ms = (time.perf_counter() - start) * 1000
    print(f"  {vision_material.norwegian_line(sannsyn)}   ({ms:.0f} ms)")
    return sannsyn


def skriv_resultat(wav: Path, bilde: Path, sannsyn: dict) -> None:
    """Legger bildet og materialgjetningen inn i opptakets JSON-fil."""
    meta_sti = wav.with_suffix(".json")
    meta = json.loads(meta_sti.read_text(encoding="utf-8"))
    beste = max(sannsyn, key=sannsyn.get)
    meta.update(
        {
            "image": bilde.name,
            "vision": {k: round(v, 4) for k, v in sannsyn.items()},
            "vision_material": beste,
            "vision_confidence": round(sannsyn[beste], 4),
        }
    )
    meta_sti.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def en_maling(board: Board, stem: str, out_dir: Path, kamera: int, vent: float) -> Path:
    """Tar imot ett opptak, tar bildet, klassifiserer og skriver alt til disk."""
    wav = rig_recieve.collect(board, out_dir, wait_for_begin=vent, stem=stem)
    print(f"  lyd:   {wav.name}")

    bilde = ta_bilde(stem, out_dir, kamera)
    print(f"  bilde: {bilde.name}")

    sannsyn = klassifiser(siste_bilde(out_dir))
    skriv_resultat(wav, bilde, sannsyn)
    plot_recording.plot(wav, show=False)
    return wav


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--merkelapp", default="maaling", help="navn pa malingene")
    parser.add_argument(
        "--trigg",
        action="store_true",
        help="start malingen fra laptopen i stedet for a vente pa knappen",
    )
    parser.add_argument("--antall", type=int, default=0, help="stopp etter N malinger")
    parser.add_argument("--kamera", type=int, default=0, help="kameraindeks")
    parser.add_argument("--port", default=None, help="f.eks. COM3")
    parser.add_argument("--out", default=str(OUT_DIR), help="mappe for malingene")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        board = Board.connect(port=args.port)
    except (NoBoardFoundError, PortUnavailableError) as exc:
        print(f"feil: {exc}", file=sys.stderr)
        return 2

    antall = 0
    with board:
        if not args.trigg:
            print("venter pa knapp 1 pa kortet (Ctrl-C for a avslutte)")
        try:
            while args.antall == 0 or antall < args.antall:
                if args.trigg:
                    print(f"maling {antall + 1}: sender 'measure'")
                    try:
                        board.call(
                            "measure",
                            {"label": args.merkelapp},
                            timeout=rig_recieve.TRIGGER_TIMEOUT,
                        )
                    except ActionRejected as exc:
                        print(f"feil: kortet avviste malingen: {exc}", file=sys.stderr)
                        return 1
                    except NoAcknowledgement as exc:
                        print(f"feil: kortet svarte ikke: {exc}", file=sys.stderr)
                        return 2

                antall += 1
                stem = f"{datetime.now():%Y%m%d_%H%M%S}_{args.merkelapp}_{antall:03d}"
                try:
                    en_maling(
                        board,
                        stem,
                        out_dir,
                        args.kamera,
                        vent=3600.0 if not args.trigg else rig_recieve.BEGIN_TIMEOUT,
                    )
                except rig_recieve.TransferError as exc:
                    # En tapt maling skal ikke rive ned lyttingen: meld fra og
                    # vent pa neste trykk.
                    print(f"  feil: {exc}", file=sys.stderr)
                    antall -= 1
                    if args.trigg:
                        return 1
        except KeyboardInterrupt:
            print("\navbrutt")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
