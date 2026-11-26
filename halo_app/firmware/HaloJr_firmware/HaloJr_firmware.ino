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
//   W ip {json}                    send a WiZ bulb command (UDP 38899) to a bulb on the ESP32's Wi-Fi
//   F                              find WiZ bulbs on the ESP32's Wi-Fi (answers "WIZ <ip>" lines)
//   N name|password                rename the ESP32's Wi-Fi (saved; password 8+ chars, or empty = open)

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include <Adafruit_PWMServoDriver.h>

// ---- ESP32 -> PCA9685 wiring ----
// GPIO 34/35 are input-only on the ESP32 (they can't drive I2C), so I2C uses 32/33.
const int SDA_PIN = 32;            // -> PCA9685 SDA
const int SCL_PIN = 33;            // -> PCA9685 SCL

// ---- Wi-Fi ----
// The ESP32's own network. Default HaloJr / halojr123; change it from the app (Settings) without
// re-uploading. Tip: give it your HOME Wi-Fi's name and password and a WiZ bulb set up at home
// will join it by itself anywhere (it thinks it's home).
String apSsid = "HaloJr";
String apPass = "halojr123";
Preferences prefs;
WiFiUDP udp;
const int WIZ_PORT = 38899;
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
  out.print("HALO OK v4 (PCA9685 ");
  out.print(pcaFound ? "found" : "NOT FOUND - check SDA GPIO32, SCL GPIO33, VCC 3V3, GND");
  out.print(") wifi ");
  out.print(apSsid);
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
  } else if (c == 'W') {                          // W 192.168.4.2 {"method":"setPilot",...}
    String rest = s.substring(1);
    rest.trim();
    int sp = rest.indexOf(' ');
    IPAddress ip;
    if (sp < 0 || !ip.fromString(rest.substring(0, sp))) { out.println("ERR W"); return; }
    String msg = rest.substring(sp + 1);
    for (int k = 0; k < 2; k++) {                // UDP can drop a packet, so send twice
      udp.beginPacket(ip, WIZ_PORT);
      udp.print(msg);
      udp.endPacket();
    }
    out.println("OK");
  } else if (c == 'F') {                          // find bulbs on our own Wi-Fi
    IPAddress bc = WiFi.softAPIP();
    bc[3] = 255;
    while (udp.parsePacket() > 0) { while (udp.available()) udp.read(); }   // drop old replies
    IPAddress seen[10];
    int nSeen = 0;
    unsigned long start = millis();
    unsigned long lastSend = 0;
    while (millis() - start < 1800) {
      if (millis() - lastSend > 500) {
        udp.beginPacket(bc, WIZ_PORT);
        udp.print("{\"method\":\"getPilot\",\"params\":{}}");
        udp.endPacket();
        lastSend = millis();
      }
      if (udp.parsePacket() > 0) {
        IPAddress from = udp.remoteIP();
        while (udp.available()) udp.read();
        bool dup = false;
        for (int k = 0; k < nSeen; k++) if (seen[k] == from) dup = true;
        if (!dup && nSeen < 10) { seen[nSeen++] = from; out.print("WIZ "); out.println(from); }
      }
      delay(5);
    }
    out.print("OK found ");
    out.println(nSeen);
  } else if (c == 'N') {                          // N name|password
    String rest = s.substring(1);
    rest.trim();
    int bar = rest.indexOf('|');
    String ns = bar < 0 ? rest : rest.substring(0, bar);
    String np = bar < 0 ? "" : rest.substring(bar + 1);
    if (ns.length() == 0 || ns.length() > 32 || (np.length() > 0 && np.length() < 8)) { out.println("ERR N (name 1-32 chars, password 8+ chars or empty)"); return; }
    apSsid = ns;
    apPass = np;
    prefs.putString("ssid", apSsid);
    prefs.putString("pass", apPass);
    out.println("OK Wi-Fi is now " + apSsid);
    WiFi.softAP(apSsid.c_str(), apPass.length() ? apPass.c_str() : NULL);
  } else {
    out.println("ERR ?");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.beginTransmission(0x40);
  pcaFound = Wire.endTransmission() == 0;
  pwm.begin();
  pwm.setOscillatorFrequency(27000000);
  pwm.setPWMFreq(50);
  ledcAttach(LIGHT_PIN, 5000, 8);   // ESP32 Arduino core 3.x / 4.x
  ledcWrite(LIGHT_PIN, 0);
  for (int i = 0; i < NCH; i++) { active[i] = false; moving[i] = false; }

  // Wi-Fi: own network always, home network too if set
  prefs.begin("halo", false);
  apSsid = prefs.getString("ssid", apSsid);
  apPass = prefs.getString("pass", apPass);
  WiFi.mode(strlen(HOME_SSID) ? WIFI_AP_STA : WIFI_AP);
  WiFi.softAP(apSsid.c_str(), apPass.length() ? apPass.c_str() : NULL);
  if (strlen(HOME_SSID)) {
    WiFi.begin(HOME_SSID, HOME_PASS);
    unsigned long t = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t < 8000) delay(200);
  }
  WiFi.setSleep(false);              // no power-save: servo commands arrive without lag
  server.begin();
  server.setNoDelay(true);
  udp.begin(38900);                  // WiZ bulbs answer to this port
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
