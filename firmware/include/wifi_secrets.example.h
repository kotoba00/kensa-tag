// これを wifi_secrets.h という名前で写して書き換える。wifi_secrets.h は git に入れない。
#pragma once

// 2.4GHz の Wi-Fi（ESP32 は 5GHz を使えない）
#define KENSA_WIFI_SSID "ここに SSID"
#define KENSA_WIFI_PASS "ここにパスワード"

// 2つ目（使わなければ消すか、// で止める）。届く方に自分でつなぐ
#define KENSA_WIFI_SSID2 "ここに 2つ目の SSID"
#define KENSA_WIFI_PASS2 "ここに 2つ目のパスワード"

// サーバー。IP か、Mac の名前に .local を付けたもの（DNS は使わない）
#define KENSA_SERVER_HOST "192.168.1.20"
#define KENSA_SERVER_PORT 18300

// 名前（.local）で見つからないときに使う IP。無ければ // で止める
#define KENSA_SERVER_IP "192.168.1.20"
