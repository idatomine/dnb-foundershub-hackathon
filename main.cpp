// Acoustic measurement rig: exponential sine sweep out of the speaker,
// simultaneous microphone capture held in memory; after each measurement the
// full recording is streamed over USB as base64 JSON chunks (rec_begin / rec_chunk / rec_end).
#include <Arduino.h>
#include "transport.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include <driver/i2s.h>
#include <math.h>
#include "mbedtls/base64.h"
#include "pins.h"
#include "modules/shared/atech_helpers.h"
#include "modules/audio/speaker.h"
#include "modules/audio/microphone.h"
#include "modules/display/st7735_tft.h"
#include "modules/input/button.h"
#include "modules/distance/vl53l5cx.h"

Speaker speaker(PIN_SPEAKER_B, PIN_SPEAKER_A, PIN_SPEAKER_D);
Microphone microphone(PIN_MICROPHONE_A, PIN_MICROPHONE_B, PIN_MICROPHONE_C, PIN_MICROPHONE_D);
ST7735_TFT st7735_tft(PIN_ST7735_TFT_D, PIN_ST7735_TFT_B, PIN_ST7735_TFT_C, PIN_ST7735_TFT_A);
ButtonModule button_module(PIN_BUTTON_MODULE_A, PIN_BUTTON_MODULE_B);
VL53L5CX_Sensor vl53l5cx_distance(Wire);

static bool distOk = false;
static EveryTimer distTimer;
static int16_t lastDistMm = -1;     // -1 = unknown
static char idleL1[40] = "Ready";
static char idleL2[40] = "press button";
static uint16_t idleColor = ST7735_TFT::COLOR_GREEN;

// ---------------- measurement parameters ----------------
static const float    SWEEP_VOLUME   = 0.5f;     // playback volume 0.0..1.0 (1.0 clips)
static const uint32_t SAMPLE_RATE    = 48000;    // both I2S peripherals
static const float    SWEEP_F1       = 200.0f;
static const float    SWEEP_F2       = 20000.0f;
static const float    SWEEP_SECONDS  = 0.8f;
static const float    FADE_SECONDS   = 0.010f;
static const uint32_t PRE_ROLL_MS    = 50;
static const uint32_t POST_ROLL_MS   = 200;
// samples still queued in the speaker's DMA after writeSamples() returns (8 x 256 frames)
static const uint32_t DMA_LAG_SAMPLES = 2048;

static const uint32_t SWEEP_N   = (uint32_t)(SWEEP_SECONDS * SAMPLE_RATE);           // 38400
static const uint32_t PRE_N     = PRE_ROLL_MS * SAMPLE_RATE / 1000;                  // 2400
static const uint32_t POST_N    = POST_ROLL_MS * SAMPLE_RATE / 1000;                 // 9600
static const uint32_t REC_MAX_N = PRE_N + SWEEP_N + POST_N + DMA_LAG_SAMPLES + 4096;

// ---------------- USB dump parameters ----------------
static const uint32_t DUMP_CHUNK = 1024;   // samples per JSON line (2048 bytes -> 2732 base64 chars)

static int16_t* sweepBuf = nullptr;
static int16_t* recBuf = nullptr;
static volatile uint32_t recCount = 0;
static volatile bool recActive = false;

static bool micOk = false, spkOk = false;
static int runCounter = 0;
static volatile bool busy = false;
static char pendingLabel[40];
static volatile bool startRequested = false;

// Measurement result handed back from the worker task to loop()
static volatile bool resultReady = false;
static char resultLabel[40];
static int resultN = 0;
static float resultPeakDb = 0;
static int resultPeakRaw = 0;
static uint32_t resultSamples = 0;     // number of recorded samples
static uint32_t resultPlayStart = 0;   // sample index where playback was started

// ---------------- helpers ----------------
static void micTap(const int16_t* s, int count, void*) {
  if (!recActive) return;
  uint32_t c = recCount;
  for (int i = 0; i < count && c < REC_MAX_N; i++) recBuf[c++] = s[i];
  recCount = c;
  if (c >= REC_MAX_N) recActive = false;
}

static void generateSweep() {
  const double T = SWEEP_SECONDS;
  const double L = log(SWEEP_F2 / SWEEP_F1);
  const double K = 2.0 * M_PI * SWEEP_F1 * T / L;
  const uint32_t fadeN = (uint32_t)(FADE_SECONDS * SAMPLE_RATE);
  for (uint32_t i = 0; i < SWEEP_N; i++) {
    double t = (double)i / SAMPLE_RATE;
    double v = sin(K * (exp(t * L / T) - 1.0));
    double g = 1.0;
    if (i < fadeN) g = 0.5 - 0.5 * cos(M_PI * i / fadeN);
    else if (i >= SWEEP_N - fadeN) g = 0.5 - 0.5 * cos(M_PI * (SWEEP_N - 1 - i) / fadeN);
    sweepBuf[i] = (int16_t)lround(v * g * 32000.0);
  }
}

static void distText(char* out, size_t n) {
  if (lastDistMm < 0) snprintf(out, n, "Dist: --");
  else if (lastDistMm >= 32767) snprintf(out, n, "Dist: none");
  else snprintf(out, n, "Dist: %d mm", (int)lastDistMm);
}

static void showText(const char* l1, const char* l2, uint16_t color) {
  st7735_tft.clear();
  st7735_tft.displayText(l1, 4, 8, 2, color);
  if (l2 && l2[0]) st7735_tft.displayText(l2, 4, 32, 2, ST7735_TFT::COLOR_WHITE);
  char d[24];
  distText(d, sizeof(d));
  st7735_tft.displayText(d, 4, 60, 1, ST7735_TFT::COLOR_CYAN);
  st7735_tft.display();
}

// remember what the idle screen says so the distance line can refresh under it
static void showIdle(const char* l1, const char* l2, uint16_t color) {
  strncpy(idleL1, l1, sizeof(idleL1) - 1);
  strncpy(idleL2, l2 ? l2 : "", sizeof(idleL2) - 1);
  idleColor = color;
  showText(idleL1, idleL2, idleColor);
}

static void sanitizeLabel(const char* in, char* out, size_t outSize) {
  size_t j = 0;
  for (size_t i = 0; in[i] && j + 1 < outSize && j < 24; i++) {
    char c = in[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_') out[j++] = c;
    else if (c == ' ') out[j++] = '_';
  }
  out[j] = 0;
  if (j == 0) strncpy(out, "meas", outSize);
}

// Pull "label":"..." out of the command's JSON value (or a bare string value)
static void labelFromValue(const char* v, char* out, size_t outSize) {
  const char* k = strstr(v, "\"label\"");
  char raw[48] = {0};
  if (k) {
    const char* q = strchr(k + 7, ':');
    if (q) q = strchr(q, '"');
    if (q) {
      q++;
      size_t j = 0;
      while (*q && *q != '"' && j + 1 < sizeof(raw)) raw[j++] = *q++;
      raw[j] = 0;
    }
  } else if (v[0] && v[0] != '{') {
    strncpy(raw, v, sizeof(raw) - 1);
  }
  sanitizeLabel(raw, out, outSize);
}

// ---------------- the measurement, on its own task ----------------
static void measureTask(void*) {
  char label[40];
  strncpy(label, pendingLabel, sizeof(label));

  // 1. recording starts; wait for the 50 ms pre-roll (counted in samples)
  recCount = 0;
  recActive = true;
  uint32_t t0 = millis();
  while (recCount < PRE_N && millis() - t0 < 1000) vTaskDelay(1);
  uint32_t playStart = recCount;

  // 2. play the sweep (blocks until the last chunk is queued to DMA)
  speaker.setVolume(SWEEP_VOLUME);
  speaker.writeSamples(sweepBuf, SWEEP_N);
  static const int16_t silence[256] = {0};
  speaker.writeSamples(silence, 256);

  // 3. keep recording 200 ms past the sweep's end
  uint32_t target = playStart + SWEEP_N + POST_N + DMA_LAG_SAMPLES;
  if (target > REC_MAX_N) target = REC_MAX_N;
  t0 = millis();
  while (recCount < target && millis() - t0 < 3000) vTaskDelay(1);
  recActive = false;
  uint32_t n = recCount;

  int peak = 0;
  for (uint32_t i = 0; i < n; i++) { int a = abs((int)recBuf[i]); if (a > peak) peak = a; }

  strncpy(resultLabel, label, sizeof(resultLabel));
  resultN = ++runCounter;
  resultPeakRaw = peak;
  resultPeakDb = peak > 0 ? 20.0f * log10f(peak / 32768.0f) : -120.0f;
  resultSamples = n;
  resultPlayStart = playStart;
  resultReady = true;
  vTaskDelete(nullptr);
}

static void requestMeasure(const char* label) {
  if (busy) {
    Serial.println("{\"type\":\"event\",\"payload\":{\"key\":\"measure_busy\",\"value\":true}}");
    return;
  }
  if (!sweepBuf || !recBuf || !spkOk || !micOk) {
    Serial.println("{\"type\":\"event\",\"payload\":{\"key\":\"measure_error\",\"value\":\"audio not ready\"}}");
    return;
  }
  strncpy(pendingLabel, label, sizeof(pendingLabel));
  startRequested = true;
}

// ---------------- stream the recording to the PC over USB ----------------
// Sends: rec_begin (metadata) -> rec_chunk x N (base64 of little-endian int16) -> rec_end.
// Runs from loop(), so nothing else (distance events etc.) is interleaved with the dump.
static void dumpRecording(const char* label, int run, uint32_t n, uint32_t playStart) {
  if (!recBuf || n == 0) return;
  uint32_t chunks = (n + DUMP_CHUNK - 1) / DUMP_CHUNK;

  Serial.setTxTimeoutMs(200);   // block briefly instead of silently dropping bytes during the dump
  Serial.printf("{\"type\":\"event\",\"payload\":{\"key\":\"rec_begin\",\"value\":{\"label\":\"%s\",\"run\":%d,"
                "\"samples\":%u,\"chunks\":%u,\"rate\":%u,\"play_start\":%u,\"sweep_n\":%u,"
                "\"sweep_f1\":%.1f,\"sweep_f2\":%.1f,\"fade_s\":%.4f}}}\n",
                label, run, (unsigned)n, (unsigned)chunks, (unsigned)SAMPLE_RATE, (unsigned)playStart,
                (unsigned)SWEEP_N, SWEEP_F1, SWEEP_F2, FADE_SECONDS);

  static unsigned char b64[((DUMP_CHUNK * 2 + 2) / 3) * 4 + 4];
  for (uint32_t c = 0; c < chunks; c++) {
    uint32_t start = c * DUMP_CHUNK;
    uint32_t cnt = (n - start < DUMP_CHUNK) ? (n - start) : DUMP_CHUNK;
    size_t olen = 0;
    mbedtls_base64_encode(b64, sizeof(b64), &olen,
                          (const unsigned char*)(recBuf + start), cnt * sizeof(int16_t));
    Serial.printf("{\"type\":\"event\",\"payload\":{\"key\":\"rec_chunk\",\"value\":{\"run\":%d,\"i\":%u,\"d\":\"",
                  run, (unsigned)c);
    Serial.write(b64, olen);
    Serial.print("\"}}}\n");
    if ((c & 7) == 7) Serial.flush();
  }
  Serial.printf("{\"type\":\"event\",\"payload\":{\"key\":\"rec_end\",\"value\":{\"run\":%d,\"chunks\":%u}}}\n",
                run, (unsigned)chunks);
  Serial.flush();
  Serial.setTxTimeoutMs(0);
}

static const char* resetReasonStr() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXT";
    case ESP_RST_SW: return "SOFTWARE";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    default: return "UNKNOWN";
  }
}

void setup() {
  Serial.setRxBufferSize(8192);
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  p2p_transport_setup();
  Wire.begin(PIN_VL53L5CX_DISTANCE_SDA, PIN_VL53L5CX_DISTANCE_SCL); Wire.setClock(400000);

  speaker.begin(SAMPLE_RATE);          // I2S0, 48 kHz, 16-bit, mono duplicated to L/R
  spkOk = true;
  microphone.begin();                  // I2S1, then re-clocked to 48 kHz below
  // Re-clock the microphone's I2S to 48 kHz (32-bit slots, 24-bit data, kept as 16-bit mono)
  micOk = (i2s_set_clk(I2S_NUM_1, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_STEREO) == ESP_OK);
  st7735_tft.begin();
  st7735_tft.setRotation(3);
  button_module.begin();
  distOk = vl53l5cx_distance.begin();

  sweepBuf = (int16_t*)heap_caps_malloc(SWEEP_N * sizeof(int16_t), MALLOC_CAP_8BIT);
  recBuf = (int16_t*)heap_caps_malloc(REC_MAX_N * sizeof(int16_t), MALLOC_CAP_8BIT);
  if (sweepBuf) generateSweep();
  microphone.onAudio(micTap, nullptr);
  speaker.setVolume(SWEEP_VOLUME);

  showIdle("Ready", "press button", ST7735_TFT::COLOR_GREEN);

  Serial.printf("{\"type\":\"boot\",\"payload\":{\"reset_reason\":\"%s\",\"free_heap\":%u,\"modules\":["
                "{\"instance\":\"speaker\",\"module\":\"speaker\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"microphone\",\"module\":\"microphone\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"st7735_tft\",\"module\":\"st7735_tft\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"button_module\",\"module\":\"button_module\",\"ok\":true,\"detectable\":false},"
                "{\"instance\":\"vl53l5cx_distance\",\"module\":\"vl53l5cx_distance\",\"ok\":%s,\"detectable\":false}]}}\n",
                resetReasonStr(), (unsigned)ESP.getFreeHeap(), distOk ? "true" : "false");
}

void loop() {
  p2p_transport_loop();

  button_module.update();
  bool pressed = button_module.wasPressed();

  P2PCommand cmd;
  while (p2p_next_command(&cmd)) {
    if (strcmp(cmd.action, "measure") == 0) {
      char label[40];
      labelFromValue(cmd.value, label, sizeof(label));
      requestMeasure(label);
    } else if (strcmp(cmd.action, "measure_empty") == 0) {
      requestMeasure("empty");
    }
  }
  if (pressed) requestMeasure("meas");

  if (startRequested && !busy) {
    startRequested = false;
    busy = true;
    showText("Measuring...", pendingLabel, ST7735_TFT::COLOR_YELLOW);
    if (xTaskCreatePinnedToCore(measureTask, "measure", 8192, nullptr, 3, nullptr, 1) != pdPASS) {
      busy = false;
      showIdle("Error", "no task", ST7735_TFT::COLOR_RED);
    }
  }

  if (resultReady) {
    resultReady = false;
    char line2[48];
    snprintf(line2, sizeof(line2), "%s #%d", resultLabel, resultN);
    char pk[48];
    snprintf(pk, sizeof(pk), "%.1f dB", resultPeakDb);
    char dj[16];
    if (lastDistMm < 0 || lastDistMm >= 32767) snprintf(dj, sizeof(dj), "null");
    else snprintf(dj, sizeof(dj), "%d", (int)lastDistMm);
    Serial.printf("{\"type\":\"event\",\"payload\":{\"key\":\"measure_done\",\"value\":{\"label\":\"%s\",\"run\":%d,\"peak_level\":%.1f,\"peak_raw\":%d,\"distance_mm\":%s,\"saved\":\"usb\"}}}\n",
                  resultLabel, resultN, resultPeakDb, resultPeakRaw, dj);

    showText("Sending...", line2, ST7735_TFT::COLOR_YELLOW);
    dumpRecording(resultLabel, resultN, resultSamples, resultPlayStart);

    busy = false;   // only accept a new measurement once the dump is finished
    showIdle(line2, pk, ST7735_TFT::COLOR_GREEN);
  }

  // distance: refreshed twice a second, never redrawn over a running measurement
  if (distTimer.ready(500)) {
    int16_t d = (distOk && vl53l5cx_distance.isReady()) ? vl53l5cx_distance.getMinDistance() : -1;
    if (d != lastDistMm) {
      lastDistMm = d;
      if (!busy && !startRequested) showText(idleL1, idleL2, idleColor);
    }
    if (d < 0) Serial.println("{\"type\":\"event\",\"payload\":{\"key\":\"min_distance\",\"value\":null}}");
    else if (d >= 32767) Serial.println("{\"type\":\"event\",\"payload\":{\"key\":\"min_distance\",\"value\":\"none\"}}");
    else Serial.printf("{\"type\":\"event\",\"payload\":{\"key\":\"min_distance\",\"value\":%d}}\n", (int)d);
  }

  delay(1);
}