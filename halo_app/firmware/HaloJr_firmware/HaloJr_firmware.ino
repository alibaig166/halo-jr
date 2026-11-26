// Halo Jr. firmware - ESP32 + Adafruit PCA9685
// Upload this ONCE. After that everything (levels, speed, directions, gimbal, tracking)
// is controlled from the web app on the laptop. The ESP32 only moves servos and the light.
//
// Talks over USB *and* Wi-Fi at the same time:
//   - The ESP32 makes its own Wi-Fi called "HaloJr" (password halojr123). Connect the laptop
//     to it and pick "Wi-Fi: HaloJr" in the app. No router or internet needed (works at uni).
//   - Optional: put your home Wi-Fi below and it also joins that network.
//
// Library: "Adafruit PWM Servo Driver Library"
// Board:   ESP32 Dev Module, Serial 115200
//
// Commands (one per line, sent by the web app):
//   T ms a0 a1 ... a15             move channels 0..15 to angle (0..180) over ms milliseconds, all together.
//                                  Use -1 for a channel you don't want to move.
//   L duty                         lamp light brightness 0..255 (LED / MOSFET on LIGHT_PIN)
//   C min max                      servo pulse range in microseconds (e.g. C 500 2400)
//   X                              release all servos (they go limp)
//   R                              report current angles
//   ?                              hello

#include <Wire.h>
#include <WiFi.h>
#include <Adafruit_PWMServoDriver.h>

// ---- Wi-Fi ----
const char* AP_SSID   = "HaloJr";       // the ESP32's own network
const char* AP_PASS   = "halojr123";    // at least 8 characters
const char* HOME_SSID = "";             // optional: your router's name, or leave ""
const char* HOME_PASS = "";
const int   TCP_PORT  = 3333;
WiFiServer server(TCP_PORT);
WiFiClient client;
String netLine;
bool pcaFound = false;

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

const int NCH = 16;
const int LIGHT_PIN = 25;          // optional: gate of a logic-level MOSFET driving the lamp LED

int pulseMin = 500, pulseMax = 2400;
float cur[NCH], fromA[NCH], toA[NCH];
unsigned long t0[NCH], dur[NCH];
bool active[NCH];    // channel has received at least one target
bool moving[NCH];
unsigned long lastTick = 0;
String line;

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

void writeAngle(int ch, float a) {
  a = clampf(a, 0, 180);
  float us = pulseMin + a * (pulseMax - pulseMin) / 180.0;
  pwm.writeMicroseconds(ch, (uint16_t)(us + 0.5));
}

void report(Print& out) {
  out.print("P");
  for (int i = 0; i < NCH; i++) {
    out.print(" ");
    if (active[i]) out.print(cur[i], 1); else out.print("-1");
  }
  out.println();
}

void hello(Print& out) {
  out.print("HALO OK v3 (PCA9685 ");
  out.print(pcaFound ? "found" : "NOT FOUND - check SDA D21, SCL D22, VCC 3V3, GND");
  out.print(") wifi ");
  out.print(AP_SSID);
  out.print(" ");
  out.print(WiFi.softAPIP());
  if (WiFi.status() == WL_CONNECTED) { out.print(" home "); out.print(WiFi.localIP()); }
  out.println();
}

// every reply goes back to whoever sent the command (USB or Wi-Fi)
void handle(String s, Print& out) {
  s.trim();
  if (s.length() == 0) return;
  char c = toupper(s.charAt(0));
  const char* p = s.c_str() + 1;

  if (c == 'T') {
    float v[NCH + 1];
    int n = 0;
    char* end;
    while (n < NCH + 1) {
      float x = strtof(p, &end);
      if (end == p) break;
      v[n++] = x;
      p = end;
    }
    if (n < 2) { out.println("ERR T"); return; }
    unsigned long ms = (unsigned long)max(0.0f, v[0]);
    for (int i = 0; i < n - 1 && i < NCH; i++) {
      float a = v[i + 1];
      if (a < 0) continue;
      a = clampf(a, 0, 180);
      if (!active[i] || ms == 0) {          // first command for a channel: go there directly
        active[i] = true;
        cur[i] = fromA[i] = toA[i] = a;
        moving[i] = false;
        writeAngle(i, a);
      } else {
        fromA[i] = cur[i];
        toA[i] = a;
        t0[i] = millis();
        dur[i] = ms;
        moving[i] = true;
      }
    }
    out.println("OK");
  } else if (c == 'L') {
    int d = constrain(atoi(p), 0, 255);
    ledcWrite(LIGHT_PIN, d);
    out.println("OK");
  } else if (c == 'C') {
    int a = 0, b = 0;
    if (sscanf(p, "%d %d", &a, &b) == 2 && a >= 300 && b <= 2800 && a < b) { pulseMin = a; pulseMax = b; out.println("OK"); }
    else out.println("ERR C");
  } else if (c == 'X') {
    for (int i = 0; i < NCH; i++) { pwm.setPWM(i, 0, 4096); moving[i] = false; active[i] = false; }
    out.println("OK");
  } else if (c == 'R') {
    report(out);
  } else if (c == '?') {
    hello(out);
  } else {
    out.println("ERR ?");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Wire.begin(21, 22);
  Wire.beginTransmission(0x40);
  pcaFound = Wire.endTransmission() == 0;
  pwm.begin();
  pwm.setOscillatorFrequency(27000000);
  pwm.setPWMFreq(50);
  ledcAttach(LIGHT_PIN, 5000, 8);   // ESP32 Arduino core 3.x / 4.x
  ledcWrite(LIGHT_PIN, 0);
  for (int i = 0; i < NCH; i++) { active[i] = false; moving[i] = false; }

  // Wi-Fi: own network always, home network too if set
  WiFi.mode(strlen(HOME_SSID) ? WIFI_AP_STA : WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  if (strlen(HOME_SSID)) {
    WiFi.begin(HOME_SSID, HOME_PASS);
    unsigned long t = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t < 8000) delay(200);
  }
  WiFi.setSleep(false);              // no power-save: servo commands arrive without lag
  server.begin();
  server.setNoDelay(true);
  hello(Serial);
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { handle(line, Serial); line = ""; }
    else if (ch != '\r' && line.length() < 240) line += ch;
  }

  // Wi-Fi: one laptop at a time; a new connection replaces the old one
  if (server.hasClient()) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    WiFiClient c = server.accept();
#else
    WiFiClient c = server.available();
#endif
    if (client && client.connected()) client.stop();
    client = c;
    client.setNoDelay(true);
    netLine = "";
  }
  while (client && client.connected() && client.available()) {
    char ch = client.read();
    if (ch == '\n') { handle(netLine, client); netLine = ""; }
    else if (ch != '\r' && netLine.length() < 240) netLine += ch;
  }
  unsigned long now = millis();
  if (now - lastTick >= 10) {
    lastTick = now;
    for (int i = 0; i < NCH; i++) {
      if (!moving[i]) continue;
      float p = dur[i] ? (float)(now - t0[i]) / dur[i] : 1.0;
      if (p >= 1.0) { p = 1.0; moving[i] = false; }
      float k = 0.5 - 0.5 * cos(PI * p);
      cur[i] = fromA[i] + (toA[i] - fromA[i]) * k;
      writeAngle(i, cur[i]);
    }
  }
}
