"""Streng test av simuleringen. Kjør `atech build demo` først, så `python demo/full_sim_test.py`.

Kjører SDK-ens ekte Board og SerialTransport mot en falsk seriell port som svarer
med formatene i den genererte main.cpp. Kjører ikke firmwaren. Testen er laget for
modulene i demo/project.yaml og må endres når prosjektet endres.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from types import SimpleNamespace

import serial

DEMO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO))

import atech.runtime.board as board_mod  # noqa: E402
import atech.runtime.transport as transport_mod  # noqa: E402
from atech import ActionRejected, Board, NoAcknowledgement  # noqa: E402
from atech.runtime.transport import PortUnavailableError, SerialTransport  # noqa: E402

import host  # noqa: E402

MAIN_CPP = (DEMO / "build" / "src" / "main.cpp").read_text(encoding="utf-8")


def fw_temp(value: float) -> bytes:
    return (
        '{"type":"event","payload":{"event_type":"sensor","key":"th_temperature","value":'
        f'{value:.2f},"unit":"C","source":"aht20"}}}}\r\n'
    ).encode()


def fw_hum(value: float) -> bytes:
    return (
        '{"type":"event","payload":{"event_type":"sensor","key":"th_humidity","value":'
        f'{value:.2f},"unit":"%","source":"aht20"}}}}\r\n'
    ).encode()


def fw_btn(value: int) -> bytes:
    return (
        '{"type":"event","payload":{"event_type":"button","key":"btn","value":'
        f'{value},"source":"button"}}}}\n'
    ).encode()


BOOT = (
    b'{"type":"boot","payload":{"reset_reason":"POWERON","free_heap":245678,"modules":['
    b'{"instance":"btn","module":"button","ok":true,"detectable":false},'
    b'{"instance":"th","module":"aht20","ok":true,"detectable":true},'
    b'{"instance":"led","module":"neopixel","ok":true,"detectable":false}]}}\r\n'
)


class FakeSerial:
    """Falsk port og firmwaremodell. Instillinger settes på klassen før åpning."""

    silent = False
    busy = False
    reject_all = False
    last = None

    def __init__(self, port=None, baudrate=None, timeout=0.05, write_timeout=None, exclusive=None):
        if FakeSerial.busy:
            raise serial.SerialException(f"could not open port '{port}': PermissionError(13, 'Access is denied.')")
        self.exclusive = exclusive
        self.baudrate = baudrate
        self.timeout = min(timeout or 0.05, 0.05)
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.written: list[bytes] = []
        self.led = None
        self.is_open = True
        self.fail_next_read = False
        self.reopened = 0
        FakeSerial.last = self

    def feed(self, data: bytes) -> None:
        with self.cond:
            self.buf.extend(data)
            self.cond.notify_all()

    @property
    def in_waiting(self) -> int:
        with self.cond:
            return len(self.buf)

    def read(self, size: int = 1) -> bytes:
        if self.fail_next_read:
            self.fail_next_read = False
            raise serial.SerialException("device disconnected")
        with self.cond:
            if not self.buf:
                self.cond.wait(self.timeout)
            out = bytes(self.buf[:size])
            del self.buf[:size]
            return out

    def write(self, data: bytes) -> int:
        self.written.append(data)
        if not FakeSerial.silent:
            threading.Thread(target=self._handle, args=(data,), daemon=True).start()
        return len(data)

    def _handle(self, data: bytes) -> None:
        message = json.loads(data.decode())
        key = message["action"]
        ok = key in {"led_fill", "led_pixel", "led_clear"} and not FakeSerial.reject_all
        if ok and key == "led_fill":
            self.led = json.loads(message["value"])
        if ok and key == "led_clear":
            self.led = None
        payload = {"action": key, "ok": ok}
        if not ok:
            payload["error"] = "unknown action"
        self.feed((json.dumps({"type": "ack", "payload": payload}) + "\r\n").encode())

    def press_button(self) -> None:
        """Som i project.yaml: firmwaren slukker lyset selv ved trykk."""
        self.led = None
        self.feed(fw_btn(1))

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def open(self) -> None:
        self.is_open = True
        self.reopened += 1

    def setDTR(self, value) -> None:
        pass

    def setRTS(self, value) -> None:
        if value:
            self.feed(BOOT)


def reset_fake() -> None:
    FakeSerial.silent = False
    FakeSerial.busy = False
    FakeSerial.reject_all = False


def with_board():
    reset_fake()
    return Board(SerialTransport("FAKE"))


def run_host(board: Board, stop_after_button: bool = True, seconds: float = 8.0) -> dict:
    """Kjør host.run med tidsgrense, så en feil ikke henger testen."""
    result: dict = {}
    output = io.StringIO()
    # Ikke redirect_stdout: en tråd som må henge, ville holdt den aktiv for alltid.
    host.print = lambda *args, **kwargs: output.write(" ".join(map(str, args)) + "\n")

    def target() -> None:
        try:
            host.run(board, stop_after_button=stop_after_button)
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    result["finished"] = not thread.is_alive()
    result["output"] = output.getvalue()
    return result


def actions(fake: FakeSerial) -> list[str]:
    return [json.loads(w)["action"] for w in fake.written]


# ---------------------------------------------------------- simulator mot firmware


def test_f1_formats_exist_in_main_cpp():
    for fragment in (
        r'{\"type\":\"event\",\"payload\":{\"event_type\":\"button\",\"key\":\"btn\",\"value\":',
        r'{\"type\":\"event\",\"payload\":{\"event_type\":\"sensor\",\"key\":\"th_temperature\",\"value\":',
        r'{\"type\":\"event\",\"payload\":{\"event_type\":\"sensor\",\"key\":\"th_humidity\",\"value\":',
    ):
        assert fragment in MAIN_CPP, f"mangler i main.cpp: {fragment}"


def test_f2_sim_actions_match_firmware():
    firmware = set(re.findall(r'if \(key == "([a-z_]+)"\)', MAIN_CPP))
    assert firmware == host.SIM_ACTIONS, f"firmware {firmware} != simulator {host.SIM_ACTIONS}"


def test_f3_host_keys_exist_in_firmware():
    assert f'\\"key\\":\\"{host.TEMPERATURE}\\"' in MAIN_CPP
    assert f'\\"key\\":\\"{host.BUTTON}\\"' in MAIN_CPP


def test_f4_sim_button_type_matches_firmware():
    sim = host.event_line("button", host.BUTTON, 1)
    transport = host.SimTransport()
    played = []
    original = transport.push_wire
    transport.push_wire = lambda line: (played.append(line), original(line))[1]
    saved = host.time.sleep
    host.time.sleep = lambda _: None
    try:
        host.play_scenario(transport)
    finally:
        host.time.sleep = saved
    button_lines = [json.loads(l) for l in played if '"btn"' in l]
    assert button_lines, f"ingen knapphendelse i simulatoren: {sim}"
    types = {l["payload"]["event_type"] for l in button_lines}
    assert types == {"button"}, f"simulatoren sender event_type {types}, firmwaren sender 'button'"


def test_f5_user_loop_compiled_into_firmware():
    loop = MAIN_CPP[MAIN_CPP.index("---- user loop ----"):]
    assert "btn.wasPressed()" in loop and "led.clear()" in loop and "led.show()" in loop


def test_f6_unknown_action_message_matches_firmware():
    assert '"unknown action"' in MAIN_CPP


# ---------------------------------------------------- SDK-ens ekte USB-kode


def test_u1_full_scenario_over_serial_transport():
    with with_board() as board:
        fake = FakeSerial.last
        for temp in (24.5, 26.0, 27.5, 29.0, 30.5):
            fake.feed(fw_temp(temp))
            fake.feed(fw_hum(40.0))
        def later():
            for _ in range(100):
                if fake.led is not None:
                    break
                time.sleep(0.02)
            assert_led.append(fake.led)
            fake.press_button()
        assert_led: list = []
        threading.Thread(target=later, daemon=True).start()
        result = run_host(board)
        assert result["finished"], f"host stoppet ikke: {result['output']}"
        assert "error" not in result, result.get("error")
        assert assert_led == [{"r": 255, "g": 0, "b": 0}], f"lys under alarm: {assert_led}"
        assert fake.led is None, "lyset skal være slukket etter knapp"
        assert actions(fake) == ["led_fill"], f"forventet én handling: {actions(fake)}"
        assert "th_humidity" not in result["output"], "fuktighet skal filtreres bort"


def test_u2_noise_split_lines_and_crlf():
    with with_board() as board:
        fake = FakeSerial.last
        fake.feed(b"E (123) i2c: log line from a driver\r\n")
        fake.feed(b"{not json}\n")
        fake.feed(b'{"type":"other","payload":{}}\n')
        line = fw_temp(21.5)
        fake.feed(line[:17])
        time.sleep(0.1)
        fake.feed(line[17:])
        events = iter(board.events(keys=[host.TEMPERATURE], poll_interval=0.1))
        got: list = []
        thread = threading.Thread(target=lambda: got.append(next(events)), daemon=True)
        thread.start()
        thread.join(3)
        assert got, "hendelsen kom ikke frem"
        assert got[0].value == 21.5 and got[0].module_type == "aht20"


def test_u3_wire_format_of_action():
    with with_board() as board:
        board.call("led_fill", {"r": 1, "g": 2, "b": 3})
        wire = FakeSerial.last.written[0]
        assert wire.endswith(b"\n") and wire.count(b"\n") == 1
        text = wire.decode()
        assert text.index('"action"') < text.index('"value"'), "firmwaren leser action før value"
        message = json.loads(text)
        assert isinstance(message["value"], str), "value skal være streng"
        assert json.loads(message["value"]) == {"r": 1, "g": 2, "b": 3}


def test_u4_rejected_action():
    with with_board() as board:
        try:
            board.call("finnes_ikke", timeout=1)
        except ActionRejected as exc:
            assert "unknown action" in str(exc)
        else:
            raise AssertionError("skulle vært avvist")


def test_u5_silent_board_times_out():
    with with_board() as board:
        FakeSerial.silent = True
        start = time.monotonic()
        try:
            board.call("led_clear", timeout=0.5)
        except NoAcknowledgement:
            assert 0.4 < time.monotonic() - start < 2
        else:
            raise AssertionError("skulle gitt tidsavbrudd")


def test_u6_boot_report():
    with with_board() as board:
        report = board.diagnostics(timeout=2)
        assert report and report["reset_reason"] == "POWERON"
        assert [m["instance"] for m in report["modules"]] == ["btn", "th", "led"]


def test_u7_port_busy():
    reset_fake()
    FakeSerial.busy = True
    try:
        SerialTransport("FAKE")
    except PortUnavailableError as exc:
        assert "in use" in str(exc)
    else:
        raise AssertionError("skulle gitt PortUnavailableError")
    finally:
        reset_fake()


def test_u8_reconnects_after_usb_drop():
    with with_board() as board:
        fake = FakeSerial.last
        fake.fail_next_read = True
        deadline = time.monotonic() + 3
        while fake.reopened == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.reopened >= 1, "porten ble ikke åpnet på nytt"
        fake.feed(fw_temp(22.0))
        while board.latest(host.TEMPERATURE) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert board.latest(host.TEMPERATURE).value == 22.0


# ----------------------------------------------------------- logikken i host.py


def test_h1_exactly_at_limit_gives_no_alarm():
    with with_board() as board:
        fake = FakeSerial.last
        fake.feed(fw_temp(host.LIMIT_C))
        fake.feed(fw_btn(1))
        result = run_host(board)
        assert result["finished"] and actions(fake) == []


def test_h2_no_repeat_then_new_alarm_after_button():
    with with_board() as board:
        fake = FakeSerial.last
        result_box = {}
        def drive():
            fake.feed(fw_temp(29.0))
            fake.feed(fw_temp(30.0))
            fake.feed(fw_temp(31.0))
            time.sleep(0.3)
            result_box["first"] = actions(fake)
            fake.feed(fw_btn(1))
            fake.feed(fw_btn(0))
            fake.feed(fw_temp(29.5))
            time.sleep(0.3)
            result_box["second"] = actions(fake)
            board.close()
        threading.Thread(target=drive, daemon=True).start()
        run_host(board, stop_after_button=False, seconds=1.2)
        assert result_box.get("first") == ["led_fill"], result_box
        assert result_box.get("second") == ["led_fill", "led_fill"], result_box


def test_h3_release_alone_does_not_clear_alarm():
    with with_board() as board:
        fake = FakeSerial.last
        fake.feed(fw_temp(29.0))
        fake.feed(fw_btn(0))
        fake.feed(fw_temp(30.0))
        result = run_host(board, stop_after_button=False, seconds=0.8)
        assert "knapp trykket" not in result["output"]
        assert actions(fake) == ["led_fill"]


def real_main(firmware_setup) -> str:
    """Kjør host.main uten --sim, slik den kjører mot ekte kort."""
    reset_fake()
    firmware_setup()
    saved_discover, saved_argv = board_mod.discover_ports, sys.argv
    board_mod.discover_ports = lambda: [SimpleNamespace(device="FAKE")]
    sys.argv = ["host.py"]
    timer = threading.Timer(0.3, lambda: FakeSerial.last.feed(fw_temp(29.0)))
    timer.start()
    try:
        with redirect_stdout(io.StringIO()):
            host.main()
    except SystemExit as exc:
        return str(exc.code)
    finally:
        timer.cancel()
        board_mod.discover_ports, sys.argv = saved_discover, saved_argv
        reset_fake()
    return "ingen SystemExit"


def test_h4_real_path_silent_board_gives_clear_message():
    message = real_main(lambda: setattr(FakeSerial, "silent", True))
    assert message.startswith("Kortet svarte ikke"), message


def test_h5_real_path_rejected_action_gives_clear_message():
    message = real_main(lambda: setattr(FakeSerial, "reject_all", True))
    assert "Kortet" in message, message


def test_h6_no_board_gives_clear_error():
    saved = board_mod.discover_ports
    board_mod.discover_ports = lambda: []
    try:
        Board.connect()
    except board_mod.NoBoardFoundError as exc:
        assert "No Atech board" in str(exc)
    else:
        raise AssertionError("skulle feilet uten kort")
    finally:
        board_mod.discover_ports = saved


def main() -> int:
    transport_mod.serial.Serial = FakeSerial
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"OK    {name}")
        except BaseException as exc:  # noqa: BLE001
            failed += 1
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            print(f"FEIL  {name}\n      {detail[:400]}")
    print(f"\n{len(tests) - failed} av {len(tests)} bestått")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
