"""Ett knappetrykk -> lydopptak + bilde + materialgjetning, alt i malinger/.

    python maaling.py                        # vent pa knapp 1 pa kortet
    python maaling.py --merkelapp gardin     # gi malingene et navn
    python maaling.py --objekt gardin_mork   # knytt dem til et objekt i dashbordet
    python maaling.py --trigg                # start fra laptopen i stedet for knappen
    python maaling.py --antall 5             # stopp etter fem malinger

Trykk pa knappen i port 1: kortet spiller sveipen og sender opptaket hit, sa
tas det et bilde med webkameraet. Begge modellene gjetter materialet - CLIP fra
bildet, random forest fra lyden - og begge sannsynlighetene skrives inn i
JSON-fila som "vision" og "audio". Med --objekt dukker malingen opp pa
dashbordet (http://localhost:8765) med bildet og begge gjetningene.

Hver maling gir fire filer med samme navn i malinger/:

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

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "malinger"

# Lydklassifikatoren ligger i en undermappe uten pakkefil.
sys.path.insert(0, str(ROOT / "material_classifier"))
import material_classifier  # noqa: E402

LYDMODELL = ROOT / "material_classifier" / "results" / "material_rf.joblib"

# Lydmodellen har klassen "air" i tillegg til synsmodellens fem.
NORSK = {
    "textile": "TEKSTIL", "metal": "METALL", "wood": "TRE",
    "plastic": "PLAST", "glass": "GLASS", "air": "LUFT",
}


def siste_bilde(mappe: Path) -> Path:
    """Det nyeste bildet i mappa. Modellen jobber alltid pa sist tatt bilde."""
    bilder = list(mappe.glob("*.jpg"))
    if not bilder:
        raise FileNotFoundError(f"ingen bilder i {mappe}")
    return max(bilder, key=lambda p: p.stat().st_mtime)


def siste_opptak(mappe: Path) -> Path:
    """Det nyeste lydopptaket i mappa. Lydmodellen jobber alltid pa det siste."""
    opptak = list(mappe.glob("*.wav"))
    if not opptak:
        raise FileNotFoundError(f"ingen opptak i {mappe}")
    return max(opptak, key=lambda p: p.stat().st_mtime)


def rangert(sannsyn: dict) -> str:
    """F.eks. 'METALL 38.4% | TEKSTIL 22.2% | ...', hoyest forst."""
    rekke = sorted(sannsyn.items(), key=lambda par: -par[1])
    return " | ".join(f"{NORSK.get(k, k.upper())} {v * 100:.1f}%" for k, v in rekke)


def ta_bilde(stem: str, out_dir: Path, kamera: int) -> Path:
    """Tar ett bilde og lagrer det ved siden av opptaket."""
    bilde = vision_material.capture_webcam_image(kamera)
    sti = out_dir / f"{stem}.jpg"
    bilde.save(sti)
    return sti


def klassifiser_bilde(bilde_sti: Path) -> dict:
    """Materialsannsynligheter fra bildet, summerer til 1."""
    start = time.perf_counter()
    sannsyn = vision_material.predict_material(bilde_sti)
    ms = (time.perf_counter() - start) * 1000
    print(f"  bilde:  {rangert(sannsyn)}   ({ms:.0f} ms)")
    return sannsyn


def klassifiser_lyd(bundle, wav_sti: Path) -> dict:
    """Materialsannsynligheter fra lyden, summerer til 1."""
    start = time.perf_counter()
    sannsyn = material_classifier.predict_proba(bundle, wav_sti)
    ms = (time.perf_counter() - start) * 1000
    print(f"  lyd:    {rangert(sannsyn)}   ({ms:.0f} ms)")
    return sannsyn


def skriv_resultat(
    wav: Path, bilde: Path, syn: dict, lyd: dict | None, objekt: str | None = None
) -> None:
    """Legger begge gjetningene inn i opptakets JSON-fil."""
    meta_sti = wav.with_suffix(".json")
    meta = json.loads(meta_sti.read_text(encoding="utf-8"))
    beste_syn = max(syn, key=syn.get)
    meta.update(
        {
            "image": bilde.name,
            "vision": {k: round(v, 4) for k, v in syn.items()},
            "vision_material": beste_syn,
            "vision_confidence": round(syn[beste_syn], 4),
        }
    )
    if objekt:
        # Dashbordet knytter opptaket til dette objektet. "object" er regelen
        # serveren sjekker forst, sa den slar alt i recording_links.json.
        meta["object"] = objekt
    if lyd:
        beste_lyd = max(lyd, key=lyd.get)
        meta.update(
            {
                "audio": {k: round(v, 4) for k, v in lyd.items()},
                "audio_material": beste_lyd,
                "audio_confidence": round(lyd[beste_lyd], 4),
            }
        )
    meta_sti.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def en_maling(
    board: Board, stem: str, out_dir: Path, kamera: int, vent: float, bundle,
    objekt: str | None = None,
) -> Path:
    """Tar imot ett opptak, tar bildet, klassifiserer begge og skriver til disk."""
    wav = rig_recieve.collect(board, out_dir, wait_for_begin=vent, stem=stem)
    bilde = ta_bilde(stem, out_dir, kamera)
    print(f"  filer:  {wav.name}, {bilde.name}")

    # Begge modellene jobber pa det sist lagrede av hver sort.
    syn = klassifiser_bilde(siste_bilde(out_dir))
    lyd = klassifiser_lyd(bundle, siste_opptak(out_dir)) if bundle else None

    skriv_resultat(wav, bilde, syn, lyd, objekt)
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
    parser.add_argument(
        "--objekt",
        default=None,
        help="id-en til objektet i dashbordet, f.eks. gardin_mork. Da dukker "
             "malingen opp der av seg selv.",
    )
    parser.add_argument("--port", default=None, help="f.eks. COM3")
    parser.add_argument("--out", default=str(OUT_DIR), help="mappe for malingene")
    parser.add_argument(
        "--lydmodell", type=Path, default=LYDMODELL, help="trent .joblib-modell"
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Lydmodellen lastes en gang. Mangler den, kjorer vi videre med bare bildet
    # i stedet for a stoppe malingen.
    try:
        bundle = material_classifier.load_bundle(args.lydmodell)
    except ValueError as exc:
        print(f"advarsel: ingen lydmodell ({exc}). Bare bildet klassifiseres.")
        bundle = None

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
                        bundle=bundle,
                        objekt=args.objekt,
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
