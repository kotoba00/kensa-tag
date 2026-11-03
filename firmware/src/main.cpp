// カードをかざすと UID を16進で、画面のボタンを押して QR を読むと中身を打ち込み、最後に Enter を送る。
// 読み取りの判断はすべて画面側に任せ、この機器は覚えない。

#include <M5Unified.h>
#include <MFRC522DriverI2C.h>
#include <MFRC522v2.h>
#include <USB.h>
#include <USBHIDKeyboard.h>
#include <Wire.h>

namespace {

constexpr uint8_t kRfidAddress = 0x28;
constexpr uint32_t kSameCardQuietMs = 1500;
constexpr uint32_t kResultShowMs = 1500;
constexpr uint32_t kRfidRetryMs = 2000;
constexpr int kQrRxPin = 5;
constexpr int kQrTxPin = 6;
constexpr int kQrTrigPin = 7;
constexpr uint32_t kQrBaud = 115200;
constexpr uint32_t kQrTrigPulseMs = 20;
constexpr uint32_t kQrRetriggerMs = 700;
constexpr uint32_t kQrQuietMs = 50;  // QR の出力に行末が無いので、途切れたら1件とする
constexpr size_t kQrMaxLength = 64;

MFRC522DriverI2C driver{kRfidAddress, Wire};
MFRC522 rfid{driver};
USBHIDKeyboard keyboard;

String lastUid;
uint32_t lastSeenAt = 0;
uint32_t resultAt = 0;
uint32_t lastRetryAt = 0;
bool rfidFound = false;
bool showingResult = false;
String qrBuffer;
uint32_t qrLastByteAt = 0;
uint32_t qrLastTrigAt = 0;
bool qrScanning = false;
bool qrSent = false;

void show(uint16_t background, const char* big, const String& small = "") {
  M5.Display.fillScreen(background);
  M5.Display.setTextColor(TFT_WHITE, background);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::lgfxJapanGothic_24);
  M5.Display.drawString(big, M5.Display.width() / 2, M5.Display.height() / 2 - 14);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString(small, M5.Display.width() / 2, M5.Display.height() / 2 + 22);
}

void showIdle() {
  if (rfidFound) {
    show(TFT_DARKGREEN, "かざす", "ready");
  } else {
    show(TFT_RED, "接続なし", "Unit RFID2");
  }
}

bool probeRfid() {
  Wire.beginTransmission(kRfidAddress);
  return Wire.endTransmission() == 0;
}

void connectRfid() {
  rfidFound = probeRfid();
  if (rfidFound) {
    rfid.PCD_Init();
  }
  showIdle();
}

void showRead(const String& text, uint32_t now) {
  show(TFT_BLUE, "読んだ", text.substring(text.length() > 6 ? text.length() - 6 : 0));
  showingResult = true;
  resultAt = now;
}

void pollQr(uint32_t now) {
  // ボタンを押している間は読めるまで出し直す。読めたら離すまで次を出さない
  bool held = M5.BtnA.isPressed();
  if (held && !qrSent && (!qrScanning || now - qrLastTrigAt > kQrRetriggerMs)) {
    if (!qrScanning) show(TFT_NAVY, "QR", "読み取り中");
    qrScanning = true;
    qrLastTrigAt = now;
    digitalWrite(kQrTrigPin, LOW);
    delay(kQrTrigPulseMs);
    digitalWrite(kQrTrigPin, HIGH);
  }
  if (!held) {
    if (qrScanning && !qrSent) showIdle();
    qrScanning = false;
    qrSent = false;
  }

  while (Serial1.available()) {
    int c = Serial1.read();
    if (c > 0x20 && c < 0x7F && qrBuffer.length() < kQrMaxLength) qrBuffer += static_cast<char>(c);
    qrLastByteAt = now;
  }
  if (qrBuffer.isEmpty() || now - qrLastByteAt < kQrQuietMs) return;
  keyboard.print(qrBuffer);
  keyboard.write(KEY_RETURN);
  showRead(qrBuffer, now);
  qrBuffer = "";
  qrSent = true;
}

String uidHex() {
  String out;
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) out += '0';
    out += String(rfid.uid.uidByte[i], HEX);
  }
  out.toUpperCase();
  return out;
}

}  // namespace

void setup() {
  auto config = M5.config();
  M5.begin(config);
  M5.Display.setRotation(0);
  M5.Display.setBrightness(120);

  keyboard.begin();
  USB.begin();

  pinMode(kQrTrigPin, OUTPUT);
  digitalWrite(kQrTrigPin, HIGH);
  Serial1.begin(kQrBaud, SERIAL_8N1, kQrRxPin, kQrTxPin);

  Wire.begin(M5.getPin(m5::ex_i2c_sda), M5.getPin(m5::ex_i2c_scl), 100000);
  connectRfid();
}

void loop() {
  M5.update();
  uint32_t now = millis();

  pollQr(now);

  if (!rfidFound) {
    if (now - lastRetryAt > kRfidRetryMs) {
      lastRetryAt = now;
      connectRfid();
    }
    delay(20);
    return;
  }

  if (showingResult && now - resultAt > kResultShowMs) {
    showingResult = false;
    if (!qrScanning) showIdle();
  }

  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
    delay(20);
    return;
  }

  String uid = uidHex();
  rfid.PICC_HaltA();

  // かざしたままのカードを1回に数える
  bool repeated = uid == lastUid && now - lastSeenAt < kSameCardQuietMs;
  lastUid = uid;
  lastSeenAt = now;
  if (repeated) return;

  keyboard.print(uid);
  keyboard.write(KEY_RETURN);

  showRead(uid, now);
}
