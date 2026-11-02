// カードをかざすと UID を16進で打ち込み、最後に Enter を送る。
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

MFRC522DriverI2C driver{kRfidAddress, Wire};
MFRC522 rfid{driver};
USBHIDKeyboard keyboard;

String lastUid;
uint32_t lastSeenAt = 0;
uint32_t resultAt = 0;
uint32_t lastRetryAt = 0;
bool rfidFound = false;
bool showingResult = false;

void show(uint16_t background, const char* big, const String& small = "") {
  M5.Display.fillScreen(background);
  M5.Display.setTextColor(TFT_WHITE, background);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::lgfxJapanGothic_24);
  M5.Display.drawString(big, M5.Display.width() / 2, M5.Display.height() / 2 - 14);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString(small, M5.Display.width() / 2, M5.Display.height() / 2 + 22);
}

bool probeRfid() {
  Wire.beginTransmission(kRfidAddress);
  return Wire.endTransmission() == 0;
}

void connectRfid() {
  rfidFound = probeRfid();
  if (rfidFound) {
    rfid.PCD_Init();
    show(TFT_DARKGREEN, "かざす", "ready");
  } else {
    show(TFT_RED, "接続なし", "Unit RFID2");
  }
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

  Wire.begin(M5.getPin(m5::ex_i2c_sda), M5.getPin(m5::ex_i2c_scl), 100000);
  connectRfid();
}

void loop() {
  M5.update();
  uint32_t now = millis();

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
    show(TFT_DARKGREEN, "かざす", "ready");
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

  show(TFT_BLUE, "読んだ", uid.substring(uid.length() - 6));
  showingResult = true;
  resultAt = now;
}
