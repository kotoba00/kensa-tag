// カードをかざすと UID を16進で、画面のボタンを押して QR を読むと中身を出す。
// USB 版（既定）はキーボードとして打ち込み、判断は画面側に任せる。
// Wi-Fi 版（KENSA_WIFI）はサーバーへ直接送り、返ってきた結果をこの画面に出す。モニターの無い工程用。

#include <M5Unified.h>
#include <MFRC522DriverI2C.h>
#include <MFRC522v2.h>
#include <Wire.h>

#ifdef KENSA_WIFI
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiMulti.h>

#include <deque>
#include <vector>
#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#error "include/wifi_secrets.example.h を include/wifi_secrets.h に写して、Wi-Fi とサーバーを書いてください"
#endif
#else
#include <USB.h>
#include <USBHIDKeyboard.h>
#endif

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

String lastUid;
uint32_t lastSeenAt = 0;
uint32_t resultAt = 0;
uint32_t resultShowMs = kResultShowMs;
uint32_t lastRetryAt = 0;
bool rfidFound = false;
bool showingResult = false;
String qrBuffer;
uint32_t qrLastByteAt = 0;
uint32_t qrLastTrigAt = 0;
bool qrScanning = false;
bool qrSent = false;

void show(uint16_t background, const String& big, const String& small = "", const String& note = "") {
  M5.Display.fillScreen(background);
  M5.Display.setTextColor(TFT_WHITE, background);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::lgfxJapanGothic_24);
  M5.Display.drawString(big, M5.Display.width() / 2, M5.Display.height() / 2 - 14);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString(small, M5.Display.width() / 2, M5.Display.height() / 2 + 22);
  if (!note.isEmpty()) M5.Display.drawString(note, M5.Display.width() / 2, M5.Display.height() / 2 + 42);
}

void holdResult(uint32_t now, uint32_t ms = kResultShowMs) {
  showingResult = true;
  resultAt = now;
  resultShowMs = ms;
}

String tail(const String& text) { return text.substring(text.length() > 6 ? text.length() - 6 : 0); }

#ifdef KENSA_WIFI

// ---- Wi-Fi 版: 送り先・工程の設定・送信待ち ----

constexpr size_t kQueueMax = 200;
constexpr uint32_t kRetryMs = 3000;
constexpr uint32_t kBunnoShowMs = 4000;

struct Station {
  String code;
  String name;
  String phase;
};

struct Pending {
  String eventId;
  String raw;
  String process;
  String phase;
  uint32_t readMs;
};

Preferences prefs;
WiFiMulti wifi;
uint32_t nextWifiAt = 0;
std::vector<Station> stations;
Station current;
std::deque<Pending> pending;
String deviceId;
String bootId;
uint32_t counter = 0;
uint32_t nextSendAt = 0;
uint32_t nextFlowAt = 0;
IPAddress serverIp;
bool serverKnown = false;
String netNote;  // つながらない理由。待ち画面の下に出す
bool menuOpen = false;
size_t menuIndex = 0;

// USB の先がつながっていても読んでいないと、Serial は空きが出るまで待ち続ける。
// 本体が止まらないよう、空きがあるときだけ書く
void logLine(const String& line) {
  if (Serial.availableForWrite() > static_cast<int>(line.length()) + 2) Serial.println(line);
}

String phaseLabel(const String& phase) {
  if (phase == "start") return "開始";
  if (phase == "end") return "終了";
  return "";
}

void drawMenu();

void showIdle() {
  if (menuOpen) {
    drawMenu();
    return;
  }
  if (!rfidFound) {
    show(TFT_RED, "接続なし", "Unit RFID2");
    return;
  }
  uint16_t bg = pending.empty() ? TFT_DARKGREEN : TFT_ORANGE;
  M5.Display.fillScreen(bg);
  M5.Display.setTextColor(TFT_WHITE, bg);
  M5.Display.setTextDatum(middle_center);
  int cx = M5.Display.width() / 2;
  M5.Display.setFont(&fonts::lgfxJapanGothic_16);
  M5.Display.drawString(current.name.isEmpty() ? "工程が未設定" : current.name, cx, 24);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString(phaseLabel(current.phase), cx, 44);
  M5.Display.setFont(&fonts::lgfxJapanGothic_24);
  M5.Display.drawString("かざす", cx, 76);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  String status = WiFi.status() != WL_CONNECTED ? "Wi-Fi なし" : netNote.isEmpty() ? "Wi-Fi OK" : netNote;
  if (!pending.empty()) status = "送信待ち " + String(pending.size()) + " 件";
  M5.Display.drawString(status, cx, 112);
}

void showBunno(int bunno, const String& orderNo) {
  M5.Display.fillScreen(TFT_DARKGREEN);
  M5.Display.setTextColor(TFT_WHITE, TFT_DARKGREEN);
  M5.Display.setTextDatum(middle_center);
  int cx = M5.Display.width() / 2;
  M5.Display.setFont(&fonts::lgfxJapanGothic_16);
  M5.Display.drawString("分納", cx, 16);
  // 離れた場所からでも番号が読めるように、画面いっぱいに出す
  M5.Display.setFont(&fonts::Font7);
  M5.Display.setTextSize(bunno < 10 ? 1.6 : 1.1);
  M5.Display.drawString(String(bunno), cx, 66);
  M5.Display.setTextSize(1);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString("注文 " + orderNo, cx, 116);
}

void showOutcome(const String& outcome, const String& orderNo, int bunno, const String& phase, uint32_t now) {
  String order = orderNo.isEmpty() ? "" : "注文 " + orderNo;
  String who = bunno > 0 ? order + " / " + String(bunno) : order;
  if (outcome == "assigned" || outcome == "already_assigned") {
    showBunno(bunno, orderNo);
    holdResult(now, kBunnoShowMs);
    return;
  }
  if (outcome == "order_set") {
    show(TFT_NAVY, "カードを", order + " かざす");
  } else if (outcome == "bound") {
    show(TFT_DARKGREEN, "紐付けOK", order);
  } else if (outcome == "recorded") {
    // 開始は緑、終了は青で、離れていても見分けられるようにする
    if (phase == "start") show(TFT_DARKGREEN, "開始", who);
    else if (phase == "end") show(TFT_BLUE, "終了", who);
    else show(TFT_DARKGREEN, "記録OK", who);
  } else if (outcome == "in_use") {
    // 製品に付いたままのカードかもしれない。付け替えるなら置き直してもう一度かざす
    show(TFT_ORANGE, "使用中", "注文 " + orderNo + (bunno > 0 ? " / " + String(bunno) : ""), "付け替えは再タッチ");
  } else if (outcome == "already_bound") {
    show(TFT_ORANGE, "紐付け済み", order);
  } else if (outcome == "already_recorded") {
    show(TFT_ORANGE, "記録済み", who);
  } else if (outcome == "debounced") {
    show(TFT_DARKGREY, "読んだ", "二度読みは数えない");
  } else if (outcome == "no_order") {
    show(TFT_RED, "先にQR", "記録票の QR を読む");
  } else if (outcome == "unbound") {
    show(TFT_RED, "未登録", "プレスで紐付けがいる");
  } else if (outcome == "wrong_process") {
    show(TFT_RED, "使えません", "この工程はカードだけ");
  } else if (outcome == "unknown_process") {
    show(TFT_RED, "工程の設定", "3回押して選び直す");
  } else {
    show(TFT_RED, "読めない", "カードか QR か不明");
  }
  holdResult(now, outcome == "order_set" || outcome == "in_use" ? 8000 : kResultShowMs);
}

bool resolveServer() {
  String host = KENSA_SERVER_HOST;
  if (host.endsWith(".local")) {
    // 工場のルーターは壊れた DNS を配ることがあるので、DNS を使わず mDNS で探す
    IPAddress ip = MDNS.queryHost(host.substring(0, host.length() - 6), 2000);
    logLine("mDNS " + host + " -> " + ip.toString());
    if (ip != IPAddress()) {
      serverIp = ip;
      serverKnown = true;
      return true;
    }
  } else if (serverIp.fromString(host)) {
    serverKnown = true;
    return true;
  }
#ifdef KENSA_SERVER_IP
  // 名前で見つからないときの予備
  if (serverIp.fromString(KENSA_SERVER_IP)) {
    serverKnown = true;
    return true;
  }
#endif
  netNote = "サーバーが見つからない";
  return false;
}

String baseUrl() { return "http://" + serverIp.toString() + ":" + String(KENSA_SERVER_PORT); }

void saveStation() {
  prefs.putString("code", current.code);
  prefs.putString("name", current.name);
  prefs.putString("phase", current.phase);
}

bool fetchFlow() {
  if (WiFi.status() != WL_CONNECTED || (!serverKnown && !resolveServer())) return false;
  HTTPClient http;
  http.setConnectTimeout(1500);
  http.setTimeout(2500);
  http.begin(baseUrl() + "/api/flow");
  int code = http.GET();
  JsonDocument doc;
  bool ok = code == 200 && !deserializeJson(doc, http.getString());
  http.end();
  logLine("GET " + baseUrl() + "/api/flow -> " + String(code));
  if (code < 0) serverKnown = false;
  if (!ok) {
    netNote = serverIp.toString() + " 応答 " + String(code);
    return false;
  }
  netNote = "";
  stations.clear();
  // 開始と終了のある工程も1つにまとめる。どちらかはサーバーが読んだ順で決める
  for (JsonObject p : doc["processes"].as<JsonArray>()) {
    stations.push_back({p["code"].as<String>(), p["name"].as<String>(), "point"});
  }
  if (stations.empty()) return false;
  // 工程の名前はサーバーの一覧に合わせる。一覧から消えた工程なら先頭に戻す
  const Station* match = nullptr;
  for (const Station& st : stations) {
    if (st.code == current.code) match = &st;
  }
  current = match ? *match : stations[0];
  saveStation();
  return true;
}

void enqueue(const String& raw, uint32_t now) {
  if (pending.size() >= kQueueMax) pending.pop_front();
  pending.push_back({deviceId + "-" + bootId + "-" + String(++counter), raw, current.code, current.phase, now});
  nextSendAt = 0;
}

void sendNext(uint32_t now) {
  if (pending.empty() || now < nextSendAt) return;
  if (WiFi.status() != WL_CONNECTED || (!serverKnown && !resolveServer())) {
    nextSendAt = now + kRetryMs;
    if (!showingResult && !qrScanning) showIdle();
    return;
  }
  const Pending& p = pending.front();
  JsonDocument body;
  body["event_id"] = p.eventId;
  body["raw"] = p.raw;
  body["device_id"] = deviceId;
  body["process"] = p.process;
  body["phase"] = p.phase;
  body["age_ms"] = now - p.readMs;
  String json;
  serializeJson(body, json);

  HTTPClient http;
  http.setConnectTimeout(1500);
  http.setTimeout(2500);
  http.begin(baseUrl() + "/api/scan");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(json);
  String reply = code > 0 ? http.getString() : "";
  http.end();
  logLine("POST /api/scan -> " + String(code));
  netNote = code < 0 || code >= 500 ? serverIp.toString() + " 応答 " + String(code) : "";

  if (code < 0 || code >= 500) {
    // つながらない・サーバーの不調は、順番を保ったまま送り直す
    if (code < 0) serverKnown = false;
    nextSendAt = now + kRetryMs;
    if (!showingResult && !qrScanning) showIdle();
    return;
  }
  pending.pop_front();
  JsonDocument res;
  if (code != 200 || deserializeJson(res, reply)) {
    // 中身の誤りは送り直しても直らない。止めずに次へ進む
    show(TFT_RED, "送信エラー", String(code));
    holdResult(now);
    return;
  }
  if (res["replayed"] | false) return;
  showOutcome(res["outcome"] | "", res["order_no"] | "", res["bunno"] | 0, res["phase"] | "", now);
}

void openMenu() {
  if (stations.empty() && !fetchFlow()) {
    show(TFT_RED, "一覧なし", "サーバーにつながらない");
    holdResult(millis());
    return;
  }
  menuOpen = true;
  menuIndex = 0;
  for (size_t i = 0; i < stations.size(); i++) {
    if (stations[i].code == current.code) menuIndex = i;
  }
  showingResult = false;
  drawMenu();
}

void drawMenu() {
  const Station& s = stations[menuIndex];
  M5.Display.fillScreen(TFT_NAVY);
  M5.Display.setTextColor(TFT_WHITE, TFT_NAVY);
  M5.Display.setTextDatum(middle_center);
  int cx = M5.Display.width() / 2;
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString("工程を選ぶ " + String(menuIndex + 1) + "/" + String(stations.size()), cx, 14);
  M5.Display.setFont(&fonts::lgfxJapanGothic_16);
  M5.Display.drawString(s.name, cx, 56);
  M5.Display.setFont(&fonts::lgfxJapanGothic_12);
  M5.Display.drawString(phaseLabel(s.phase), cx, 78);
  M5.Display.drawString("押す=次  長押し=決定", cx, 114);
}

void handleMenu() {
  if (M5.BtnA.wasHold()) {
    current = stations[menuIndex];
    saveStation();
    menuOpen = false;
    show(TFT_DARKGREEN, "設定した", current.name);
    holdResult(millis());
    return;
  }
  if (M5.BtnA.wasDecideClickCount()) {
    menuIndex = (menuIndex + 1) % stations.size();
    drawMenu();
  }
}

void emit(const String& raw, uint32_t now) {
  enqueue(raw, now);
  show(TFT_DARKGREY, "送信中", tail(raw));
  holdResult(now);
}

void setupOutput() {
  Serial.setTxTimeoutMs(0);
  prefs.begin("kensa");
  current = {prefs.getString("code", ""), prefs.getString("name", ""), prefs.getString("phase", "point")};
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  deviceId = "atom-" + mac.substring(6);
  bootId = String(esp_random(), HEX);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // 省電力で応答が遅れるのを避ける
  // 書いてある Wi-Fi のうち、届くものにつなぐ。工場と事務所の両方を書いておける
  wifi.addAP(KENSA_WIFI_SSID, KENSA_WIFI_PASS);
#ifdef KENSA_WIFI_SSID2
  wifi.addAP(KENSA_WIFI_SSID2, KENSA_WIFI_PASS2);
#endif
  wifi.run(5000);
  MDNS.begin(deviceId.c_str());
}

void serviceOutput(uint32_t now) {
  if (WiFi.status() != WL_CONNECTED && now >= nextWifiAt) {
    nextWifiAt = now + 5000;
    wifi.run(1000);
  }
  if (stations.empty() && now >= nextFlowAt) {
    nextFlowAt = now + 5000;
    if (fetchFlow() && !showingResult && !qrScanning && !menuOpen) showIdle();
  }
  sendNext(now);
}

bool menuStep() {
  if (menuOpen) {
    handleMenu();
    return true;
  }
  // 3回続けて押すと工程を選び直す。QR を読むときの押し方と区別する
  if (M5.BtnA.wasDecideClickCount() && M5.BtnA.getClickCount() >= 3) {
    openMenu();
    return menuOpen;
  }
  return false;
}

#else

// ---- USB 版: キーボードとして打ち込む ----

USBHIDKeyboard keyboard;

void showIdle() {
  if (rfidFound) {
    show(TFT_DARKGREEN, "かざす", "ready");
  } else {
    show(TFT_RED, "接続なし", "Unit RFID2");
  }
}

void emit(const String& raw, uint32_t now) {
  keyboard.print(raw);
  keyboard.write(KEY_RETURN);
  show(TFT_BLUE, "読んだ", tail(raw));
  holdResult(now);
}

void setupOutput() {
  keyboard.begin();
  USB.begin();
}

void serviceOutput(uint32_t) {}

bool menuStep() { return false; }

#endif

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
  emit(qrBuffer, now);
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

  setupOutput();

  pinMode(kQrTrigPin, OUTPUT);
  digitalWrite(kQrTrigPin, HIGH);
  Serial1.begin(kQrBaud, SERIAL_8N1, kQrRxPin, kQrTxPin);

  Wire.begin(M5.getPin(m5::ex_i2c_sda), M5.getPin(m5::ex_i2c_scl), 100000);
  connectRfid();
}

void loop() {
  M5.update();
  uint32_t now = millis();

  if (!menuStep()) pollQr(now);
  serviceOutput(now);

  if (!rfidFound) {
    if (now - lastRetryAt > kRfidRetryMs) {
      lastRetryAt = now;
      connectRfid();
    }
    delay(20);
    return;
  }

  if (showingResult && now - resultAt > resultShowMs) {
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

  emit(uid, now);
}
