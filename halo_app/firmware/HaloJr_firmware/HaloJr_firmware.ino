// Halo Jr. firmware - ESP32 + Adafruit PCA9685
// Upload this ONCE. After that everything (levels, speed, directions, gimbal, tracking)
// is controlled from the web app on the laptop. The ESP32 only moves servos and the light.
//
// Library: "Adafruit PWM Servo Driver Library"
// Board:   ESP32 Dev Module, Serial 115200
//
// Commands (one per line, sent by the web app):
//   T ms a0 a1 a2 a3 a4 a5 a6 a7   move channels 0..7 to angle (0..180) over ms milliseconds, all together.
//                                  Use -1 for a channel you don't want to move.
//   L duty                         lamp light brightness 0..255 (LED / MOSFET on LIGHT_PIN)
//   C min max                      servo pulse range in microseconds (e.g. C 500 2400)
//   X                              release all servos (they go limp)
//   R                              report current angles
//   ?                              hello

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

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

void report() {
  Serial.print("P");
  for (int i = 0; i < NCH; i++) {
    Serial.print(" ");
    if (active[i]) Serial.print(cur[i], 1); else Serial.print("-1");
  }
  Serial.println();
}

void handle(String s) {
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
    if (n < 2) { Serial.println("ERR T"); return; }
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
    Serial.println("OK");
  } else if (c == 'L') {
    int d = constrain(atoi(p), 0, 255);
    ledcWrite(LIGHT_PIN, d);
    Serial.println("OK");
  } else if (c == 'C') {
    int a = 0, b = 0;
    if (sscanf(p, "%d %d", &a, &b) == 2 && a >= 300 && b <= 2800 && a < b) { pulseMin = a; pulseMax = b; Serial.println("OK"); }
    else Serial.println("ERR C");
  } else if (c == 'X') {
    for (int i = 0; i < NCH; i++) { pwm.setPWM(i, 0, 4096); moving[i] = false; active[i] = false; }
    Serial.println("OK");
  } else if (c == 'R') {
    report();
  } else if (c == '?') {
    Serial.println("HALO OK v2");
  } else {
    Serial.println("ERR ?");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Wire.begin(21, 22);
  Wire.beginTransmission(0x40);
  bool found = Wire.endTransmission() == 0;
  pwm.begin();
  pwm.setOscillatorFrequency(27000000);
  pwm.setPWMFreq(50);
  ledcAttach(LIGHT_PIN, 5000, 8);   // ESP32 Arduino core 3.x / 4.x
  ledcWrite(LIGHT_PIN, 0);
  for (int i = 0; i < NCH; i++) { active[i] = false; moving[i] = false; }
  Serial.println(found ? "HALO OK v2 (PCA9685 found)" : "HALO OK v2 (PCA9685 NOT FOUND - check SDA D21, SCL D22, VCC 3V3, GND)");
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { handle(line); line = ""; }
    else if (ch != '\r' && line.length() < 240) line += ch;
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
