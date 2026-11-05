"""Hent et opptak fra riggen og lagre det i recordings/.

    python rig_recieve.py                      # trigg en maling, lagre opptaket
    python rig_recieve.py --label tom_rigg     # gi opptaket et navn
    python rig_recieve.py --count 5            # fem malinger etter hverandre
    python rig_recieve.py --listen             # vent pa knappetrykk pa kortet
    python rig_recieve.py --plot               # plott med en gang etterpa

Firmwaren spiller en eksponentiell sinussveip pa hoyttaleren og tar opp
mikrofonen samtidig. Etter malingen sender den hele opptaket hit som
base64-biter. Her settes bitene sammen til en WAV-fil med en JSON-fil ved
siden som holder metadataene (sveip, avstand, toppniva).

Bare ett program kan holde kortets port: stopp `atech monitor` forst.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

from atech import ActionRejected, Board, NoAcknowledgement, NoBoardFoundError
from atech.runtime.transport import PortUnavailableError

OUT_DIR = Path(__file__).resolve().parent / "recordings"

# Hendelsene firmwaren sender rundt et opptak.
BEGIN = "rec_begin"
CHUNK = "rec_chunk"
END = "rec_end"
DONE = "measure_done"

# Hvor lenge vi venter pa at noe skal skje for vi gir opp.
TRIGGER_TIMEOUT = 5.0  # pa ack-en for `measure`
BEGIN_TIMEOUT = 15.0  # fra maling startet til rec_begin kommer
TRANSFER_TIMEOUT = 60.0  # fra rec_begin til rec_end
IDLE_TIMEOUT = 5.0  # stillhet midt i en overforing


class TransferError(RuntimeError):
    """Opptaket kom ikke helt fram."""


class Recording:
    """Bitene som kommer inn mellom rec_begin og rec_end."""

    def __init__(self, meta: dict) -> None:
        self.meta = meta
        self.rate = int(meta.get("rate", 48000))
        self.expected = int(meta.get("samples", 0))
        self.label = str(meta.get("label", "meas"))
        self.run = int(meta.get("run", 0))
        self.chunks: dict[int, bytes] = {}

    def add(self, value) -> None:
        # {"i": 0, "d": "<base64>"} — indeksen er med slik at tapte biter synes.
        if not isinstance(value, dict):
            raise TransferError(f"ugyldig rec_chunk: {value!r}")
        index = int(value["i"])
        self.chunks[index] = base64.b64decode(value["d"])

    def pcm(self, chunk_count: int) -> bytes:
        missing = [i for i in range(chunk_count) if i not in self.chunks]
        if missing:
            raise TransferError(
                f"{len(missing)} av {chunk_count} biter kom aldri "
                f"(forste: {missing[0]}). Opptaket lagres ikke."
            )
        return b"".join(self.chunks[i] for i in range(chunk_count))


def checksum(pcm: bytes) -> int:
    """Samme sum firmwaren regner: 32-bits sum av samplene som usignerte ord."""
    total = 0
    for i in range(0, len(pcm), 2):
        total = (total + (pcm[i] | (pcm[i + 1] << 8))) & 0xFFFFFFFF
    return total


def save(rec: Recording, pcm: bytes, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{stamp}_{rec.label}_run{rec.run:03d}"
    wav_path = out_dir / f"{stem}.wav"

    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rec.rate)
        wav.writeframes(pcm)

    meta = dict(rec.meta)
    meta.update(
        {
            "file": wav_path.name,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "samples_received": len(pcm) // 2,
            "duration_s": round((len(pcm) // 2) / rec.rate, 4),
        }
    )
    wav_path.with_suffix(".json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return wav_path


def collect(board: Board, out_dir: Path, *, wait_for_begin: float) -> Path:
    """Vent pa ett opptak og skriv det til disk. Returner filstien."""
    rec: Recording | None = None
    deadline = time.monotonic() + wait_for_begin
    last_seen = time.monotonic()

    # board.events() sluker sine egne tidsavbrudd og blokkerer i det uendelige
    # hvis kortet tier. Her trengs tidsavbruddene, sa transporten drives direkte.
    while True:
        event = board.transport.recv(timeout=0.1)
        now = time.monotonic()
        if event is None or event.key not in (BEGIN, CHUNK, END, DONE):
            if rec is None and now > deadline:
                raise TransferError(
                    "kortet sendte ikke noe opptak. Kjorer det firmwaren med "
                    "opptaksoverforing? `atech check` viser hva som svarte."
                )
            if rec is not None and (now > deadline or now - last_seen > IDLE_TIMEOUT):
                raise TransferError(
                    f"overforingen stoppet etter {len(rec.chunks)} biter"
                )
            continue

        if event.key == BEGIN:
            rec = Recording(event.value)
            deadline = now + TRANSFER_TIMEOUT
            last_seen = now
            secs = rec.expected / rec.rate if rec.rate else 0
            print(
                f"  opptak #{rec.run} '{rec.label}': {rec.expected} sampler "
                f"({secs:.2f} s @ {rec.rate} Hz), toppniva {rec.meta.get('peak_level')} dB"
            )
        elif event.key == CHUNK and rec is not None:
            rec.add(event.value)
            last_seen = now
            print(f"\r  mottar... {len(rec.chunks)} biter", end="", flush=True)
        elif event.key == END and rec is not None:
            print()
            value = event.value if isinstance(event.value, dict) else {}
            pcm = rec.pcm(int(value.get("chunks", 0)))
            got = checksum(pcm)
            want = int(value.get("checksum", got))
            if got != want:
                raise TransferError(
                    f"sjekksummen stemmer ikke (kortet: {want}, her: {got})"
                )
            if rec.expected and len(pcm) // 2 != rec.expected:
                raise TransferError(
                    f"forventet {rec.expected} sampler, fikk {len(pcm) // 2}"
                )
            return save(rec, pcm, out_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", default="meas", help="navn pa malingen")
    parser.add_argument("--count", type=int, default=1, help="antall malinger")
    parser.add_argument(
        "--listen",
        action="store_true",
        help="ikke trigg noe; vent pa at knappen pa kortet trykkes",
    )
    parser.add_argument("--port", default=None, help="f.eks. COM3 (ellers forste kort)")
    parser.add_argument("--out", default=str(OUT_DIR), help="mappe for opptakene")
    parser.add_argument(
        "--plot", action="store_true", help="plott hvert opptak nar det er lagret"
    )
    args = parser.parse_args()
    out_dir = Path(args.out)

    try:
        board = Board.connect(port=args.port)
    except NoBoardFoundError as exc:
        print(f"feil: {exc}", file=sys.stderr)
        return 2
    except PortUnavailableError as exc:
        print(f"feil: {exc}", file=sys.stderr)
        return 2

    saved: list[Path] = []
    with board:
        if args.listen:
            print("venter pa knappetrykk pa kortet (Ctrl-C for a avslutte)")
        try:
            while args.listen or len(saved) < args.count:
                if not args.listen:
                    print(f"maling {len(saved) + 1} av {args.count}: sender 'measure'")
                    try:
                        board.call(
                            "measure", {"label": args.label}, timeout=TRIGGER_TIMEOUT
                        )
                    except ActionRejected as exc:
                        print(f"feil: kortet avviste malingen: {exc}", file=sys.stderr)
                        return 1
                    except NoAcknowledgement as exc:
                        print(f"feil: kortet svarte ikke: {exc}", file=sys.stderr)
                        return 2
                try:
                    path = collect(
                        board,
                        out_dir,
                        wait_for_begin=3600.0 if args.listen else BEGIN_TIMEOUT,
                    )
                except TransferError as exc:
                    print(f"feil: {exc}", file=sys.stderr)
                    return 1
                saved.append(path)
                print(f"  lagret {path}")
                if args.plot:
                    # Plottes med en gang, ikke til slutt: i --listen kommer
                    # "til slutt" aldri.
                    import plot_recording

                    plot_recording.plot(path, show=False)
        except KeyboardInterrupt:
            print("\navbrutt")

    if not saved:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
