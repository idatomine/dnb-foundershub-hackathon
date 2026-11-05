# CLAUDE.md — hardware orientation for this repo

Read this before touching the firmware. It describes the physical device, every
module on it, which GPIO each driver pin lands on, how the firmware is laid out,
the serial protocol, and the traps that compile clean but fail on hardware.

## 1. What the device is

An **acoustic measurement rig**. One button press (or a serial command) plays an
exponential sine sweep (200 Hz -> 7.5 kHz, 0.8 s, 10 ms raised-cosine fades,
16 kHz / 16-bit mono) out of the speaker while the microphone records the same
window (50 ms pre-roll, 200 ms post-roll). After the
sweep the whole recording is streamed to the laptop over USB as base64 JSON
chunks (`rec_begin` / `rec_chunk` / `rec_end`), where `rig_recieve.py` writes it
to `recordings/` as a WAV with a JSON sidecar. The screen shows the peak level
in dBFS, the nearest distance the time-of-flight sensor sees, and the
orientation sensor's pose and tilt angle.

Power and host link: USB-C (port 8). USB only (no WiFi).

## 2. The board

- MCU: ESP32-S3, "Atech 14-Port Board". PlatformIO `esp32-s3-devkitc-1`,
  Arduino, `espressif32@~6.4.0`, `-DARDUINO_USB_MODE=1
  -DARDUINO_USB_CDC_ON_BOOT=1`, monitor 115200, upload 460800.
- 14 ports, two GPIOs each. Port 8 = USB-C, port 12 = Reset (reserved).
- All modules on 3.3 V (budget 500 mA; catalog worst case 270 mA, estimate).

| port | side | GPIOs | occupant |
|---|---|---|---|
| port_1 | left | 9, 8 | Button 1 (start a measurement) |
| port_2 | left | 5, 4 | Button 2 (resend last recording) |
| port_3 | left | 17, 18 | Orientation sensor (IMU, ICM-40608/LSM6DSOX) |
| port_4 | left | 16, 15 | free |
| port_5 | left | 11, 10 | Color display (with 6) |
| port_6 | left | 13, 12 | Color display (with 5) |
| port_7 | top | 6, 7 | free |
| port_8 | — | — | USB-C (reserved) |
| port_9 | right | 40, 41 | Speaker (with 10) |
| port_10 | right | 1, 2 | Speaker (with 9) |
| port_11 | right | 43, 44 | Distance sensor |
| port_12 | — | — | Reset (reserved) |
| port_13 | right | 39, 38 | Microphone (with 14) |
| port_14 | right | 36, 35 | Microphone (with 13) |

Screen and button on the left edge (user's side); speaker, distance sensor
and microphone on the right edge, facing the space being measured.

## Microphone and speaker sample rate

Both I2S drivers run at **16 kHz**: `Microphone::SAMPLE_RATE` is 16000 and
`Speaker::DEFAULT_SAMPLE_RATE` is 16000, so they are already in step and nothing
needs re-clocking. The sweep therefore tops out at 7.5 kHz, below the 8 kHz
Nyquist limit.

**Do not re-clock the microphone's I2S to 48 kHz.** An
`i2s_set_clk(I2S_NUM_1, 48000, ...)` in `setup()` compiles, returns `ESP_OK`,
and leaves the microphone returning **digital silence** — `microphone_level`
reads exactly 0 and every recorded sample is zero. On this board it also left
the distance sensor failing `begin()`. Both came back the moment the call was
removed. The old `main.cpp` did exactly this, which is why that firmware never
captured audio.
