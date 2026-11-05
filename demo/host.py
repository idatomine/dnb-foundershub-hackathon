"""Vertsprogram for pipeline-demoen. Samme logikk kjører mot simulert og ekte kort.

    python demo/host.py --sim     # simulert kort, ingen maskinvare
    python demo/host.py           # ekte kort på USB, etter `atech upload demo`

Simulatoren tester Python-siden og meldingsformatet. Den kjører IKKE firmwaren,
så den beviser ikke at C++-koden eller modulene virker.
"""

from __future__ import annotations

import argparse
import json
import threading
import time

from atech import ActionRejected, Board, NoAcknowledgement
from atech.runtime.transport import MockTransport

TEMPERATURE = "th_temperature"
BUTTON = "btn"
LIMIT_C = 28.0

# Handlingene `atech describe demo` oppgir for dette prosjektet.
SIM_ACTIONS = {"led_fill", "led_pixel", "led_clear"}


class SimTransport(MockTransport):
    """Svarer som firmwaren: ack på kjente handlinger, ok:false på ukjente."""

    def send(self, action) -> None:
        super().send(action)
        wire = json.loads(self.sent_wire[-1])
        key = wire["action"]
        print(f"  [simkort] mottok {self.sent_wire[-1].decode().strip()}")
        if key in SIM_ACTIONS:
            payload = {"action": key, "ok": True}
        else:
            payload = {"action": key, "ok": False, "error": "unknown action"}
        self.push_wire(json.dumps({"type": "ack", "payload": payload}))


def event_line(event_type: str, key: str, value) -> str:
    return json.dumps(
        {"type": "event", "payload": {"event_type": event_type, "key": key, "value": value}}
    )


def play_scenario(transport: SimTransport) -> None:
    """Temperaturen stiger over grensen, så trykker noen på knappen."""
    for temp in (24.5, 26.0, 27.5, 29.0, 30.5):
        transport.push_wire(event_line("sensor", TEMPERATURE, temp))
        time.sleep(0.2)
    transport.push_wire(event_line("button", BUTTON, 1))


def run(board: Board, stop_after_button: bool) -> None:
    alarm = False
    for event in board.events(keys=[TEMPERATURE, BUTTON], poll_interval=0.1):
        print(f"hendelse: {event.key} = {event.value}")
        if event.key == BUTTON and event.value == 1:
            # Firmwaren har allerede slukket lyset. Her oppdateres bare tilstanden.
            alarm = False
            print("  knapp trykket: alarm kvittert")
            if stop_after_button:
                return
        elif event.key == TEMPERATURE:
            too_warm = event.value > LIMIT_C
            if too_warm and not alarm:
                board.call("led_fill", {"r": 255, "g": 0, "b": 0})
                alarm = True
                print(f"  over {LIMIT_C} °C: rødt lys bekreftet av kortet")


def check_error_handling(board: Board) -> None:
    """En ukjent handling skal gi feil, ikke late som den virket."""
    try:
        board.call("finnes_ikke")
    except ActionRejected as exc:
        print(f"ukjent handling avvist som forventet: {exc}")
    else:
        raise SystemExit("FEIL: ukjent handling ble ikke avvist")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", action="store_true", help="bruk simulert kort")
    args = parser.parse_args()

    if not args.sim:
        with Board.connect() as board:
            try:
                run(board, stop_after_button=False)
            except NoAcknowledgement as exc:
                raise SystemExit(f"Kortet svarte ikke: {exc}")
            except ActionRejected as exc:
                raise SystemExit(f"Kortet avviste handlingen: {exc}")
        return

    transport = SimTransport()
    with Board(transport) as board:
        threading.Thread(target=play_scenario, args=(transport,), daemon=True).start()
        run(board, stop_after_button=True)
        check_error_handling(board)
    print("Simulering fullført.")


if __name__ == "__main__":
    main()
