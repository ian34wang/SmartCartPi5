# SmartCart_Pi5：智慧購物車

大同大學電機系畢業專題。推車上的 Raspberry Pi 5 接條碼掃描器、秤重、光流與姿態感測器、門口 BLE Beacon、前方相機，
用「**掃碼 + 秤重比對**」防損、用「**光流 + IMU 里程計**」與「**地上定位點 + 貨架消失點**」在店面平面圖上標出推車位置，
觸控畫面提供健康護照、錢包管家、找貨、優惠。

---

## 一分鐘現況（2026-11-23）

**結論**：購物主流程已在推車上實機跑通（11-16），四個硬體全部接真的、沒有任何模擬路徑。11-23 加上的找貨（平面圖＋推車位置）、
健康護照、錢包管家、優惠頁程式都完成、自動化測試全過；**定位這一塊（里程計準度、相機定位點、消失點）還沒用實際推車驗證過**，
月底前剩下的主要是現場工作。

| 部分 | 程式 | 驗證程度 |
|---|---|---|
| 硬體與驅動（UART、條碼、BLE、相機） | 完成 | ✅ 實機 |
| 購物流程狀態機（登入 → 進場 → 掃碼秤重 → 結帳 → 出場 → 登出） | 完成 | ✅ 實機（11-16 跑通） |
| 觸控 UI 14 頁、會員偏好、交易紀錄、店員協助 | 完成 | ✅ 實機 |
| 門口 BLE 進出場判定 | 完成，11-16 校正 | ✅ 實機（建議用新版校正重算一次，見待辦 1） |
| 秤重、光流、航向校正 | 完成，11-16 校正 | ✅ 實機 |
| 健康護照、錢包管家、優惠（規則式） | 完成 | ⚠️ 只有自動化測試；展示商品的標籤、貨架還沒建檔 |
| 找貨頁（平面圖、路線、推車位置） | 完成，11-26 換成實際場地（CAD 圖） | ⚠️ 只有自動化測試 |
| 里程計準度（光流 + IMU） | 完成，軌跡工具已有 | ⚠️ 只用校正時同一份錄音重播過，沒有獨立驗證 |
| 相機定位點、消失點修航向 | 完成，11-26 重新校正內參（完整視野） | ✅ 靜態實測（標記 0.6~1.5 m 全部偵測、相機高 0.49~0.52 m）；⚠️ 還沒在 app 裡推著走過 |
| LLM 推薦、異常偵測、依推車位置重新規劃路線 | — | 未開始 |

**接下來要做的**（照順序）：

1. **門口參數用新版校正重算**：`python3 -m tools.calibrate_gate_beacons --from-data data/gate_calibration/20261116-200255`。
   新版多了確認時間 $T_c$ 與「執行期記錄不能來回跳」的檢查；用目前參數重播 11-16 之後的 9 份記錄，有 3 份會來回跳
   （含 11-23 那份），建議參數全部 0 次（判讀「勉強」，可行遲滯範圍 0.75 dB）。看過建議值再回答 y 寫回。
2. **里程計軌跡驗證**（不用等場地）：`python3 -m tools.track_odometry --grid-m 0.3`，推直線、L 形、繞一圈，跟捲尺量的終點比
   （見〈定位：里程計〉）。11-16 三趟直推的終點都偏右 3~7%，這次要量終點的左右偏差。
3. **展示商品建檔**：`python3 -m tools.product_admin meta <條碼>` 設貨架（`L1`~`R4`）、成分標籤、折扣、搭配。
4. **相機動態驗證**：靜態比對 11-26 做完（見〈現場驗證〉）。下一步開 app 從門口推進長廊，看找貨頁的推車點在定位點對齊、
   推到貨架停車點時落在對的位置（〈現場驗證〉第 5 步）。
5. **整趟實機彩排**：進門 → 找貨 → 掃碼 → 結帳 → 出場。

---

## 系統架構

### 硬體

| 元件 | 接在哪 | 做什麼 |
|---|---|---|
| MCU：SAM-IoT WG（SAMD21） | UART → Pi 5（`/dev/ttyAMA0`，115200，20 Hz） | 收集下面三個感測器、打包送給 Pi |
| PMW3901 光流 | MCU（SPI） | 推車對地面的位移（里程計） |
| BNO080 IMU | MCU | 航向角（相對航向，開機為 0） |
| HX711 + 荷重元 | MCU | 籃子重量（秤重比對） |
| Raspberry Pi 5 | — | 全部的軟體 |
| USB 條碼掃描器 | Pi USB（evdev） | 商品條碼、會員卡、店員工作證 |
| 門口 BLE Beacon ×2 | Pi 內建藍牙 | 門內 GATE-INSIDE（Arduino UNO R4 WiFi）、門外 GATE-OUTSIDE（ESP32） |
| Camera Module 3 Wide（imx708_wide） | Pi CSI（相機 0） | 前方相機：地上定位點標記、貨架消失點；裝成轉 180°，軟體翻回來 |
| Logitech C270 | USB（相機 1） | 拍購物車內，監視備檔用；app 目前沒用到，之後若加神經網路視覺辨識從這裡接 |
| 7 吋觸控螢幕 | Pi DSI | 720×1280 直立 |

MCU 腳位已經用滿，所以門口偵測不經過 MCU，直接用 Pi 的藍牙掃 Beacon，車上不用多裝東西。

### 資料流

```
 MCU ──UART 20 Hz──▶ drivers/uart_receiver ─┬─ hx711_raw ─▶ core/shopping/weight ─▶ 狀態機（秤重比對）
                                             ├─ yaw ───────▶ core/gate/monitor（航向交叉驗證）
                                             └─ dx,dy,yaw ─▶ core/positioning/odometry ─▶ 推車位置
 條碼掃描器 ─evdev─▶ drivers/barcode_scanner ──────────────▶ 狀態機（會員碼／商品碼／店員證）
 門口 Beacon ─BLE─▶ drivers/ble_beacon_scanner ─▶ core/gate/monitor ─▶ 狀態機（進場／出場）
 前方相機 ─CSI─▶ drivers/front_camera ─▶ core/store/vision ─▶ 定位點對齊、消失點修航向

 ui/workers.py（背景執行緒）把以上事件交給 ui/bridge.py 的 Bridge
   Bridge ─▶ core/shopping/state_machine（流程）、database（資料）、core/store/layout（平面圖）
   Bridge ◀─▶ 觸控畫面 ui/templates/index.html（pywebview）
```

### 單一實體路徑原則

**整個系統只走真實硬體，沒有任何模擬、退回或旁路。** 四個裝置都是 app 啟動時的硬性條件，任何一個接不上就印出原因、結束（exit 1）。
以前每個裝置都有「接不到就退回模擬」的路徑（掃描器的鍵盤模式、秤重展示模式、開發面板的閘門按鈕、手打商品重量……），結果有兩個問題：
出問題時第一件事是先搞清楚自己走在哪一條路上；更糟的是，**硬體沒在運作時畫面看起來一切正常**。寧可開不起來，也不要假裝正常。
所以畫面右上角的 `DEV` 只是唯讀診斷面板，沒有任何能製造事件的按鈕；商品重量也只能在推車的秤上現場量。

### 目錄結構

```
SmartCartPi5/
├── config.json                 全域設定（讀寫都經過 core/config.py）
├── requirements.txt
├── core/                       演算法與流程（純邏輯，不碰硬體，可以用錄音或合成資料測）
│   ├── config.py               專案路徑、config.json 讀寫、視覺預設值
│   ├── angles.py               角度包裝 wrap／unwrap
│   ├── recording.py            UART 錄音格式（秤重、光流校正、軌跡共用）
│   ├── shopping/               購物
│   │   ├── state_machine.py    購物流程狀態機
│   │   ├── cart.py             購物清單
│   │   ├── weight.py           HX711 讀數 → 公克
│   │   ├── weight_calibration.py  秤重校正的計算
│   │   └── health_passport.py  健康護照規則
│   ├── positioning/            定位
│   │   ├── odometry.py         里程計（光流 + IMU）
│   │   ├── track.py            行駛軌跡：重播、誤差指標、畫圖
│   │   └── flow_calibration.py 光流與航向校正的計算
│   ├── gate/                   門口 BLE 判定
│   │   ├── detector.py         RSSI 平滑、雙 Beacon 差分、航向交叉驗證
│   │   ├── model.py            判定規則的唯一標準（即時、校正、分析共用）
│   │   ├── monitor.py          即時版本（一筆一筆處理並記錄）
│   │   ├── recorder.py         執行期記錄檔
│   │   └── calibration.py      門口校正的計算
│   └── store/                  店面
│       ├── layout.py           平面圖、貨架代碼、路線、里程計 ↔ 平面圖
│       └── vision.py           ArUco 定位點、貨架消失點
├── drivers/                    硬體存取（只把資料讀進來）
│   ├── uart_receiver.py        MCU 封包（也是 UART 硬體測試工具）
│   ├── barcode_scanner.py      USB 條碼掃描器（evdev 獨佔）
│   ├── ble_beacon_scanner.py   門口 Beacon（bleak）
│   ├── camera_stream.py        相機擷取（picamera2）＋棋盤格校正
│   └── front_camera.py         前方相機：灰階、縮小、去畸變
├── database/db_manager.py      SQLite：商品、會員、偏好、交易、店員、協助請求
├── ui/
│   ├── app_gui.py              主程式：檢查設定、接硬體、開視窗
│   ├── bridge.py               畫面與流程的橋（Bridge）、前端 API（JsApi）
│   ├── workers.py              背景執行緒
│   └── templates/index.html    14 頁單頁應用（產生的，不要手改）
├── design-cart-ui/*.dc.html    畫面設計稿（index.html 的來源）
├── tools/                      終端機工具（校正、驗證、後台、分析）
├── tests/                      自動化測試（都不需要硬體）；run_all.py 一次跑完
├── docs/store_field.DWG/.png   場地 CAD 圖（平面圖的來源）
├── vision/camera_calibration.npz   相機內參（不進版控）
└── data/                       校正錄音、執行期記錄、軌跡圖
```

### 執行緒

app 有 7 條背景執行緒（`ui/workers.py`）：條碼、UART（20 Hz）、BLE、相機、每秒逾時檢查、畫面推送、推車位置推送。
前端按鈕也從 pywebview 的執行緒呼叫進來。**`Bridge` 的公開方法全部在同一把 `RLock` 裡執行**，所以狀態機一次只處理一個事件
（`tests/test_app_gui_bridge.py` 第 34 項用 4 條執行緒同時打，驗證同一時間最多一個事件在狀態機裡）。
硬體事件造成畫面變化時只「要求推送」，由 `StatePusher` 在鎖外呼叫 `evaluate_js`，連續的變化合併成一次。
前端看得到的方法只有 `JsApi` 列出的 21 個（pywebview 會把 js_api 物件的公開屬性遞迴展開，所以不直接交出 `Bridge`）。

---

## 快速上手

### 第一次安裝（每台 Pi 做一次）

```bash
# 1. 關掉序列埠的登入 shell、打開硬體 UART
sudo raspi-config        # Interface Options → Serial Port → login shell: No, hardware: Yes
#    /boot/firmware/config.txt 要有 dtparam=uart0=on

# 2. 套件（venv 要 --system-site-packages，才看得到 apt 裝的 picamera2 與 gi）
cd ~/SmartCartPi5
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install -y python3-picamera2 --no-install-recommends
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1   # 找不到 4.1 就裝 4.0

# 3. 權限
sudo usermod -aG input $USER     # 條碼掃描器（evdev），重新登入生效
sudo rfkill unblock bluetooth    # 藍牙打不開時

# 4. 資料庫（--init 會寫入 5 筆測試商品、2 筆測試會員；正式商品用 product_admin 建）
python3 -m database.db_manager --init
```

### 每天開機

```bash
cd ~/SmartCartPi5 && source venv/bin/activate
python3 -m ui.app_gui --fullscreen
```

啟動時依序檢查並印出 `[1/4]`～`[4/4]`（掃描器、UART、Beacon、相機），任何一個接不上就印出怎麼修、結束。
**開 app 時推車要在門外**（門口判定第一次確認的那一側只當起點；登入時已經在門內的話，登入當下直接算進場）。
退出：右上角 `✕`、鍵盤 `Esc`、終端機 `Ctrl+C`；卡住就另開終端機 `pkill -f ui.app_gui`。

### 指令速查

所有指令都在專案根目錄、venv 裡執行。

**日常**

| 指令 | 用途 |
|---|---|
| `python3 -m ui.app_gui [--fullscreen]` | 開 app（其他參數：`--db`、`--port`、`--baud`、`--scanner-hint`、`--adapter`、`--beacon-timeout`） |
| `python3 -m tools.product_admin` | 資料庫後台互動選單（指令見〈資料庫與後台〉） |
| `python3 -m tools.analyze_gate_log [--events]` | 分析最新一份門口記錄 |

**硬體測試**

| 指令 | 用途 |
|---|---|
| `python3 -m drivers.uart_receiver [--quiet]` | 看 MCU 有沒有正常送封包：印每一筆，每秒一行統計（包數、壞封包） |
| `sudo venv/bin/python3 -m drivers.barcode_scanner --list` | 列出輸入裝置，找掃描器名稱 |
| `sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint USBKey [--debug]` | 刷條碼看解析結果（`--debug` 印原始鍵碼） |
| `python3 -m drivers.ble_beacon_scanner [--list]` | 列出附近 BLE 裝置／印門口兩顆的原始 RSSI |
| `python3 -m drivers.camera_stream --list` | 列出接著的相機（編號、型號） |
| `python3 -m drivers.camera_stream --snapshot` | 每台相機各拍一張原始影像，確認哪台是前方、有沒有裝反 |
| `python3 -m drivers.camera_stream --flip-calibration` | 同一台相機改了翻轉：內參直接換算，不用重拍 |
| `python3 -m drivers.camera_stream --preview` | 相機擷取 |
| `python3 -m drivers.camera_stream --color-test` | 拍一張存 PNG，確認色彩通道 |

**校正**（都會問過才寫回 `config.json`；加 `--dry-run` 只看不寫；`--from-data <目錄>` 用錄音重算）

| 指令 | 定出什麼 |
|---|---|
| `python3 -m tools.calibrate_weight` | 秤重 offset、scale、未掃碼門檻、穩定窗 |
| `python3 -m tools.calibrate_gate_beacons` | 門口的估計量、窗口、確認時間、偏移、遲滯、絕對門檻 |
| `python3 -m tools.calibrate_gate_beacons --monitor` | 即時看原始值、濾波值、判定 |
| `python3 -m tools.calibrate_gate_beacons --survey` | 門口 5 個位置的靜止剖面（改佈署前後比較，不挑參數） |
| `python3 -m tools.calibrate_optical_flow` | 光流 px→mm、安裝角、鏡像、yaw 正負號 |
| `python3 -m drivers.camera_stream --calibrate --square-size-mm 25.8` | 前方相機內參（預設拍 25 張；換相機、改翻轉或改感測器模式要重做） |
| `python3 -m drivers.camera_stream --calibrate --from-data data/camera_calibration/<時間>` | 用存下來的棋盤格照片重算 |
| `python3 -m tools.make_anchor_marker` | 產生地上定位點的 ArUco 標記圖 |

**驗證**（只讀 `config.json`，不寫）

| 指令 | 驗證什麼 |
|---|---|
| `python3 -m tools.verify_weight` | 放已知重量的東西，看換算誤差 |
| `python3 -m tools.track_odometry [--grid-m 0.3]` | 里程計行駛軌跡：即時畫線，推完跟捲尺量的終點比；`--no-window`、`--from-data` |
| `python3 -m tools.verify_store_vision` | 前方相機：定位點距離、相機高度、消失點角度 |

**測試**

| 指令 | 用途 |
|---|---|
| `python3 tests/run_all.py` | 跑完全部測試，列出每一支的結果 |
| `python3 tools/build_app_ui.py` | 從設計稿重新產生 `ui/templates/index.html` |


---

## 購物流程（`core/shopping/state_machine.py`）

狀態機保障整趟流程的正確性：掃碼、秤重比對、防損、進出管制區。純邏輯，除了查資料庫沒有 I/O。

### 狀態

```
UNAUTHENTICATED ─刷會員卡／訪客─▶ LOGGED_IN_OUTSIDE_ZONE ─BLE 判定進場─▶ SHOPPING
SHOPPING ⇄ AWAITING_ITEM_SCAN              重量先變了，等掃碼指認是哪一件
SHOPPING ⇄ AWAITING_WEIGHT_INCREASE / _DECREASE   掃碼了，等重量變化
以上出錯 ─▶ WEIGHT_MISMATCH_ERROR           重試／放棄／店員歸零
SHOPPING ─鎖定結帳─▶ LOCKED_FOR_CHECKOUT ─付款─▶ AWAITING_EXIT ─BLE 判定出場─▶ SESSION_CLOSED ─▶ 自動登出
```

錯誤分兩種：

- **軟體擋得下來的**（條碼查無資料、秤重對不上）：擋下動作、發警告、提供重試／放棄。
- **可能是防損事件的**（沒結帳就出場、未登入就進場）：發 CRITICAL 警告，狀態不自動改，交給畫面與店員處理。狀態機攔不住人，這是軟體的邊界。

被拒絕的事件**不丟例外**，只記一筆警告（`CartAlert`，代碼是 `ALERT_*` 常數）。所以上層一定要比對前後的警告，把結果顯示出來，
不然使用者會看到「按了、畫面沒反應、也沒有錯誤」。`Bridge` 比的是「最後一筆警告物件是不是同一個」，不是筆數——
警告歷史有 200 筆上限，滿了之後筆數不再變多。刷條碼這種使用者明確做的動作，連 INFO 等級的拒絕原因也會顯示。

### 加入與移除：誰先發生

加入／移除不是使用者切換模式，是看「掃碼」與「重量變化」誰先發生——跟真實的動作順序一致：

- **加入**：先掃碼、再放進籃子。掃碼當下重量還沒變 → `AWAITING_WEIGHT_INCREASE`，基準 $b$ = 掃碼當下的穩定重量。
- **移除**：先拿出來、再掃碼。重量先變 → `AWAITING_ITEM_SCAN`，記下變化前的基準 $b$；掃碼時依目前重量比 $b$ 輕或重決定移除或加入，基準用 $b$。
- `AWAITING_ITEM_SCAN` 期間重量自己回到 $b$ 附近（手滑碰到籃子）→ 自動解除；超過 `unscanned_change_timeout_sec`（8 秒）沒掃碼 → 異常（「只做一半」）。

### 秤重比對

令

- $b$：基準重量（g）
- $\tilde w$：目前的穩定讀數（g，定義見下一節）
- $m$：商品的標準重量 `standard_weight_g`（g）
- $\tau$：容許誤差 `weight_tolerance_g`（g）
- $e$：預期變化，加入 $e = +m$、移除 $e = -m$
- $\delta = \tilde w - b$：實測變化

則

| 條件 | 結果 |
|---|---|
| $\lvert \delta - e \rvert \le \tau$ | 比對成功，寫入購物清單 |
| $\operatorname{sign}\delta \ne \operatorname{sign} e$ 且 $\lvert\delta\rvert > \tau$ | 方向相反（可能同時動了別的東西）→ 異常（CRITICAL） |
| $\operatorname{sign}\delta = \operatorname{sign} e$ 且 $\lvert\delta\rvert > \lvert e\rvert + \tau$ | 超出上限（拿了不只一件或拿錯）→ 異常 |
| 其他 | 還在放／拿的過程中，繼續等；超過 `weight_match_timeout_sec`（5 秒）→ 異常 |

購物中 $\lvert \tilde w - \text{穩定重量} \rvert$ 小於未掃碼門檻 $T$（`unscanned_change_threshold_g`，10 g）視為雜訊，並讓穩定重量跟上小幅漂移；
超過 $T$ 就是「重量變了但沒掃碼」。比 $T$ 輕的商品不掃碼放進去偵測不到。

### 等讀數穩定才比

令 $W_s$ = `settle_window_sec`（0.3 秒）、$B_s$ = `settle_band_g`（3 g），最近 $W_s$ 秒內的讀數為 $w_1, \dots, w_n$。若

$$n \ge 3,\qquad t_n - t_1 \ge 0.8\,W_s,\qquad \max_i w_i - \min_i w_i \le B_s$$

則穩定讀數 $\tilde w = \operatorname{median}(w_1, \dots, w_n)$；否則這一刻「還沒穩」，只更新目前重量、不做任何判斷。

為什麼：放入商品時讀數會先衝過頭、晃一下才停（11-16 實測衝擊最多多了 69 g）。每一筆都直接比的話，剛好經過容差範圍的那一筆就被判成加入，
之後讀數繼續往穩定值走，多出來的 10~20 g 又觸發「未掃碼的重量變化」。11-16 的放入錄音：不等時 0/3 趟正常，$W_s \ge 0.2$ 秒起 3/3。

### 逾時與其他事件

| 參數（`config.json`） | 預設 | 逾時後 |
|---|---|---|
| `weight.weight_match_timeout_sec` | 5 秒 | 掃碼後等不到正確的重量變化 → 異常 |
| `weight.unscanned_change_timeout_sec` | 8 秒 | 重量變了沒掃碼 → 異常 |
| `state_machine.zone_entry_timeout_sec` | 120 秒 | 登入後一直沒進場 → 提醒一次 |
| `state_machine.shopping_idle_timeout_sec` | 600 秒 | 購物中沒動作 → 每隔這麼久提醒 |
| `state_machine.exit_timeout_after_checkout_sec` | 180 秒 | 付款後沒出場 → 提醒一次（結帳畫面顯示倒數） |

逾時由 `TimeoutTick` 事件驅動（app 每秒送一次），所以逾時邏輯也能用指定時間的事件測試。

- **感測器斷線**：UART 超過 `stale_data_timeout_sec`（0.5 秒）沒封包 → `SensorDisconnected`；秤重流程進行中的話直接轉異常（斷線期間的比對不可信）。
- **鎖定結帳期間**只更新目前重量、不動穩定重量；「返回繼續選購」解鎖後第一筆穩定讀數就跟鎖定前比，鎖定期間放進或拿出的東西一樣抓得到。
- **沒結帳就出場**：已進場、還沒付款（購物中、等掃碼、等重量、異常、鎖定中）時判定出場 → CRITICAL `exit_without_checkout`。
- **登入時推車已經在門內**（門口判定已確認在門內那一側）→ 登入當下直接算進場，不然要等下一次穿越門口。
- **同一會員在別台推車登入**：單機資料庫看不到，不處理。

### 秤重異常的處理

| 畫面按鈕 | 效果 | 要店員嗎 |
|---|---|---|
| 重新比對 | 用目前重量當新基準，再等一次 | 不用 |
| 放棄此次動作（掃過碼、比對不符的那一筆） | 不加入也不移除；穩定基準沒變，東西還在籃子裡的話下一筆讀數就會抓成未掃碼的重量變化 | 不用 |
| 放棄此次動作（沒掃碼、逾時的那種） | 等於接受目前重量 | **要**：只送出申請，店員刷工作證才生效 |
| 重新校準歸零 | 放棄待比對的那一筆，目前重量當新的穩定基準（接受籃子裡現在的東西） | **要**：同上；哪位店員、前後重量記進警告歷史 |
| 呼叫店員協助 | 寫一筆協助請求 | — |

店員工作證：條碼前綴 `state_machine.staff_barcode_prefix`（`STAFF-`），要在 `staff` 資料表裡。刷了就執行待確認的動作，並把這台車的協助請求標成已處理。
沒有另外的店員通知系統，後台用 `product_admin assist-list` 看。

---

## 觸控畫面（`ui/`）

### 14 頁畫面怎麼決定

| 畫面 | `screen` | 由什麼決定 |
|---|---|---|
| 待機 | `main` | 精靈階段（出場後自動登出也回這裡） |
| 會員登入、載入會員資料 | `login`、`loading_profile` | 精靈階段 |
| 過敏原、葷素、宗教、預算 | `allergens`、`diet`、`religion`、`budget` | 精靈階段 |
| 購物主畫面 | `shopping` | `SHOPPING`、`AWAITING_*`、還沒進場的 `LOGGED_IN_OUTSIDE_ZONE`（顯示等待進入管制區） |
| 找貨、優惠、我的 | `navigation`、`promotions`、`profile` | 底部導覽列分頁 |
| 秤重異常 | `weight_alert` | `WEIGHT_MISMATCH_ERROR`（永遠優先，就算正在走精靈） |
| 結帳 | `checkout` | `LOCKED_FOR_CHECKOUT` |
| 離場確認 | `exit_confirm` | `AWAITING_EXIT` |

精靈（歡迎 → 登入 → 讀取設定 → 過敏原 → 葷素 → 宗教 → 預算）是畫面流程、不是營運狀態，`Bridge` 用 `_ui_stage` 另外記。

### 會員偏好與交易紀錄

- **偏好**：精靈走完時存（訪客不存）。「載入會員資料」選「沿用上次設定」直接跳到預算確認；沒存過就說明並改走精靈。「我的」分頁可以改本次預算或重走精靈（第一步按上一步＝取消，還原原本的設定）。
- **交易**：付款時寫一筆（會員、金額、件數、付款方式、預算、進場時間、明細），出場時補出場時間。寫入失敗會在畫面上說（付款照走，請店員手動記錄）。

### 改畫面

**`ui/templates/index.html` 是產生出來的，不要手改**，下次重跑就被蓋掉：

```bash
python3 tools/build_app_ui.py     # design-cart-ui/*.dc.html → ui/templates/index.html
```

- 各畫面的視覺（字級、間距、顏色）→ 改 `design-cart-ui/<畫面>.dc.html`。
- 前端行為（JS）、共用樣式、診斷面板 → 改 `tools/build_app_ui.py` 的 `TEMPLATE`。
- 哪些假資料要換成動態內容 → 改 `tools/build_app_ui.py` 的 `patch_<畫面>()`。

補丁跟設計稿的 HTML 結構綁在一起（`must_replace()` 找不到會報錯）。改完設計稿一定要重跑、開畫面確認，並跑 `tests/test_ui_layout.py`。
每個畫面的 CSS 會被加上 `#screen-<id>` 前綴，14 份 CSS 放在同一頁不會互相污染。

### 唯讀診斷面板（右上角 `DEV`）

- **最近收到的條碼**：刷一下有變 = 條碼有進到程式（問題在商品資料或秤重）；沒變 = 掃描器沒接好或 evdev 抓錯裝置。這行是「刷了沒反應」的分水嶺。
- **秤重**：目前公克數與連線狀態。沒數字或不會跳 = UART 斷了，這時候掃碼一定會被擋（沒重量就無法驗證）。
- **門口判定次數**與是否在等待進場。

---

## 健康護照、錢包管家、找貨、優惠

全部由商品資料驅動（`products` 表的 `tags`、`shelf`、`discount_pct`、`pairs_with`），用 `product_admin meta` 或 CSV 設定。

### 健康護照（`core/shopping/health_passport.py`）

衝突 = 商品標籤 ∩ 這條規則禁止的標籤。**只提醒、不阻擋**（可能是幫別人買）：掃碼時跳提醒，比對中區塊、購物清單、找貨、優惠都標出來。

| 偏好 | 禁止的標籤 |
|---|---|
| 過敏原（12 種，跟精靈選項一字不差） | 勾的每一項 |
| 全素（純素） | 肉、海鮮、蛋、奶、五辛 |
| 蛋奶素 | 肉、海鮮、五辛 |
| 奶素 | 肉、海鮮、蛋、五辛 |
| 蛋素 | 肉、海鮮、奶、五辛 |
| 五辛素（植物五辛素） | 肉、海鮮、蛋、奶 |
| 鍋邊素（可接受海鮮／肉湯） | 肉（看不出是肉塊還是肉湯，一律提醒） |
| 伊斯蘭教（清真） | 豬肉、酒精 |
| 印度教、台灣民俗不食牛 | 牛肉 |

「肉」= 豬肉、牛肉、其他肉類；「海鮮」= 海鮮、魚類、甲殼類；「奶」= 牛奶／羊奶。

### 錢包管家

比對中顯示「加入後總額、預算剩多少」；總額往上跨過預算的 80%、100% 各提醒一次（降回去再上來會再提醒）；
進度條超過預算變全黑並顯示超出多少；結帳頁列原價、特價折抵、應付、預算剩餘。

### 找貨

點商品名稱 → 平面圖畫出從門口過去的路線、標出那個角落，文字指引例如「進門沿長廊直走過定位點 → 往左走到左架 → 在貨架左側、靠門那段」。
觸控螢幕沒有鍵盤，所以不做打字搜尋。推車位置見〈店面平面圖與推車位置〉。

### 優惠

- **特價**：有折扣的商品（已在購物車的標「已套用」）。
- **搭配**：購物車裡商品的 `pairs_with`，還沒拿的。
- **推薦**：跟偏好沒衝突、在剩餘預算內、有貨架位置、還沒拿的，依折扣與價格排序。

健康護照有衝突的不出現在搭配與推薦。每張卡片可以「導航去貨架」，最多 6 張。

---

## 資料庫與後台（`database/db_manager.py`、`tools/product_admin.py`）

| 資料表 | 內容 |
|---|---|
| `products` | 條碼、名稱、單價、標準重量與容差、成分標籤、貨架、折扣、搭配 |
| `members` | 會員代碼與名稱（登入是刷會員條碼，沒有密碼） |
| `member_preferences` | 過敏原、葷素、宗教飲食、上次預算 |
| `transactions`、`transaction_items` | 交易與明細，出場時間 |
| `staff` | 店員工作證 |
| `assistance_requests` | 協助請求與處理人 |

建表都是 `CREATE TABLE IF NOT EXISTS`，舊資料庫缺的表、欄位在 app 或後台啟動時自動補上，不用重建。時間一律存 epoch 秒。
**沒建檔的條碼刷了就是「查無此商品」**——這是正常的，展示要用的商品要先建好。

```bash
python3 -m tools.product_admin                       # 互動選單
python3 -m tools.product_admin list
python3 -m tools.product_admin add                   # 互動：條碼直接刷、重量放上推車的秤現場量
python3 -m tools.product_admin add --barcode 4710018001234 --name "豆漿" --price 25   # 重量一樣要現場量
python3 -m tools.product_admin edit <條碼>            # Enter 保留原值；標籤、貨架等沿用
python3 -m tools.product_admin meta <條碼>            # 成分標籤、貨架 L1~R4、折扣 %、搭配條碼（不用秤）
python3 -m tools.product_admin delete <條碼>
python3 -m tools.product_admin import products.csv   # CSV 批次匯入／更新
python3 -m tools.product_admin export backup.csv
python3 -m tools.product_admin member-list | member-add | member-delete <代碼>
python3 -m tools.product_admin staff-list  | staff-add  | staff-delete <代碼>
python3 -m tools.product_admin assist-list [--all]
python3 -m tools.product_admin assist-resolve <id|all> --by STAFF-0001
python3 -m tools.product_admin tx-list [--member MEMBER-0001] [--limit 20]
```

所有指令都可以加 `--db <路徑>`；`add`、`edit` 可以加 `--port`、`--baud` 指定秤的 UART。

- **重量只能現場量**：`add`、`edit` 會請你把商品放上推車的秤（同一顆 HX711、同一份校正值），量到的值當標準重量，容差預設 10%。
  秤連不上就報錯結束，沒有手打的退路——手打的數字跟這台秤量出來的差多少沒人知道。
- **會員代碼**要以 `state_machine.login_barcode_prefix`（`MEMBER-`）開頭，否則登入時會被當成「不是會員碼」。
- **CSV**：`barcode,name,unit_price,standard_weight_g,weight_tolerance_g,tags,shelf,discount_pct,pairs_with`；後四欄可省略
  （已設好的標籤等不會被清掉），`tags`、`pairs_with` 用分號隔開；CSV 的容差必填（沒有自動 10%）。

---

## 定位：里程計（`core/positioning/odometry.py`）

光流量推車對地面的位移、IMU 給航向，一筆一筆累加成位置。app 在 UART 執行緒每收到一筆封包算一次。

**座標**：車身座標前方 $f$、左方 $l$；全域座標 $(X, Y)$（mm），原點是 app 啟動那一刻；航向 $\psi$ 逆時針為正（左轉為正）。
BNO080 的 `yaw_deg` 是相對航向（開機為 0），所以全域座標的方向每次開機都不同——要跟店面對齊得靠門口進場與地上定位點（下一章）。

一筆封包 $(dx, dy, \text{squal}, \text{yaw})$：

1. 感測器軸 → 車身（PMW3901 不一定跟車身對齊，從下往上看還會左右顛倒）：

$$s = (dx,\ m\,dy),\qquad \begin{bmatrix} f \\ l \end{bmatrix} = \begin{bmatrix} \cos\varphi & \sin\varphi \\ -\sin\varphi & \cos\varphi \end{bmatrix} s$$

2. 車身 → 全域，用**上一筆**的航向 $\psi$（區間起點的航向），再乘上換算係數 $k$：

$$\Delta X = k\,(f\cos\psi - l\sin\psi),\qquad \Delta Y = k\,(f\sin\psi + l\cos\psi)$$

3. 更新航向 $\psi = \sigma \cdot \text{yaw}$。

| 符號 | 意義 | `config.json`（11-16 校正值） |
|---|---|---|
| $k$ | 光流 count → mm | `odometry.optical_flow_px_to_mm` = 0.74674 |
| $\varphi$ | 安裝角：直推時感測器量到的方向角 | `odometry.flow_rotation_deg` = 94.2° |
| $m$ | 鏡像：側推方向在直推方向 −90° 時為 −1 | `odometry.flow_mirror` = true（$m=-1$） |
| $\sigma$ | yaw 正負號：原地左轉時 yaw 增加為 +1 | `odometry.yaw_sign` = +1 |

squal（PMW3901 追蹤品質 0~255）低於 `min_squal` 的那一筆位置不累加（航向照樣更新）。app 用 `min_squal = 0`（不過濾），
11-16 實測 squal 中位數約 120、最低 5% 約 110。

### 行駛軌跡驗證（`tools/track_odometry.py`）

推車的同時在螢幕上即時畫出里程計算出來的軌跡，推完輸入捲尺量的終點與實際轉角，算誤差、存圖。
位置的算法就是 app 的 `OdometryEngine`（`config.json` 的 `odometry` 參數、`min_squal = 0`），
所以這裡看到的準不準就是找貨頁推車點的準不準。純邏輯在 `core/positioning/track.py`。

```bash
python3 -m tools.track_odometry --grid-m 0.3     # Pi 桌面上跑；格線 0.3 m 對照地上的格子
python3 -m tools.track_odometry --no-window      # SSH：終端機顯示，一樣存圖
python3 -m tools.track_odometry --from-data data/odometry_tracks/<時間>   # 用錄音重算（改過參數後比較）
```

app 開著時序列埠被佔用，要先關 app。每一趟存在 `data/odometry_tracks/<開始時間>/`：原始封包（可以重算）、
`run<k>.png` 軌跡圖、`result.json` 指標。

**座標**：原點是起點，$f$ 軸是起點時的車頭方向（圖上朝上），$l$ 軸是起點時的左方，單位公尺。
里程計全域位置 $(X_i, Y_i)$、航向 $\psi_i$（左轉為正），$i = 0 \dots N$ 是這一趟的第一筆到最後一筆：

$$\begin{bmatrix} f_i \\ l_i \end{bmatrix} = \begin{bmatrix} \cos\psi_0 & \sin\psi_0 \\ -\sin\psi_0 & \cos\psi_0 \end{bmatrix}\begin{bmatrix} X_i - X_0 \\ Y_i - Y_0 \end{bmatrix}$$

**指標**：

$$L = \sum_{i=1}^{N} \left\lVert (X_i, Y_i) - (X_{i-1}, Y_{i-1}) \right\rVert,\qquad D = \left\lVert (f_N, l_N) \right\rVert,\qquad \Delta\psi = \sum_{i=1}^{N} \operatorname{wrap}(\psi_i - \psi_{i-1})$$

$$\mathbf{e} = (f_N - f^*,\ l_N - l^*),\qquad \frac{\lVert \mathbf{e} \rVert}{L},\qquad e_\psi = \Delta\psi - \Delta\psi^*$$

- $L$：路徑長；$D$：起點到終點的直線距離；$\Delta\psi$：總轉角（$\operatorname{wrap}$ 把角度差包到 $(-180^\circ, 180^\circ]$，所以繞一圈是 $360^\circ$）
- $(f^*, l^*)$：捲尺量的終點（起點座標）；繞回起點時是 $(0, 0)$，$\lVert \mathbf{e} \rVert$ 就是閉合誤差
- $\lVert \mathbf{e} \rVert / L$：每走一公尺差多少；$\Delta\psi^*$：實際轉角

**建議的推法**（起點、終點都選地上格子的交點，捲尺好量）：

| 路線 | 量什麼 | 主要看 |
|---|---|---|
| 沿格線直推 3 m | $(f^*, l^*) = (3, 0)$ | $f_N$ 誤差 → `px_to_mm`；$l_N$ 偏多少 → 直線會不會越推越歪 |
| L 形（前 2 m、左轉、前 1 m） | $(2, 1)$、$\Delta\psi^* = 90^\circ$ | 轉彎後方向對不對 |
| 繞一圈回起點（例如 2 m × 1 m 長方形） | $(0, 0)$、$\Delta\psi^* = 360^\circ$ | 閉合誤差 $\lVert \mathbf{e} \rVert / L$ |

**11-16 校正錄音的重播**（`--from-data data/flow_calibration/20261116-201342`；同一份資料校正又重播，偏樂觀）：

| 段落 | 實際 | $D$ | $(f_N, l_N)$ | $\Delta\psi$ |
|---|---|---|---|---|
| 直推 1 | 5 m | 5.051 m | (+5.046, −0.214) m | −5.6° |
| 直推 2 | 5 m | 5.081 m | (+5.079, −0.161) m | −4.0° |
| 直推 3 | 5 m | 4.896 m | (+4.884, −0.342) m | −4.9° |
| 原地左轉 | 90° | — | — | +91.0° |

距離 ±2%、轉角差 1°。但三趟直推的終點都偏右 0.16~0.34 m（路徑長的 3~7%），IMU 也說右轉了 4~6°：
可能是人推的時候真的偏了，也可能是 IMU 航向漂移——那次沒量終點的左右位置，分不出來。
新的推車驗證**記得量 $l^*$**（終點離起點那條格線多遠）。

---

## 店面平面圖與推車位置（`core/store/layout.py`、`core/store/vision.py`）

場地 11-26 架好，CAD 圖在 `docs/store_field.DWG`（截圖 `docs/store_field.png`，單位 mm），程式裡的平面圖
（`core/store/layout.DEFAULT_LAYOUT`）就是照這張圖換算的，**單位是公尺**，原點在門（長廊入口）正中間，$y$ 往店裡、$x$ 往右：

```
            ┌──────────────────────────────────────┐  y = 5.195（後牆）
            │      ┌────┐            ┌────┐        │  y = 4.285
            │      │左架│            │右架│        │
            │      └────┘            └────┘        │  y = 3.065
            └────────────────┐    ┌────────────────┘  y = 2.45（大廳前緣 = 長廊末端）
                             │ ◇  │                   y = 2.20 定位點標記
                             │長廊│
                             └─門─┘                   y = 0
          x = −2.06        x = ±0.31                  x = +2.09
```

| 區域 | $x$（m） | $y$（m） | CAD 圖上的尺寸 |
|---|---|---|---|
| 大廳 | −2.06 ~ +2.09 | 2.45 ~ 5.195 | 4150 × 2745 |
| 長廊 | −0.31 ~ +0.31 | 0 ~ 2.45 | 620 × 2450；左緣離左牆 1750、右緣離右牆 1780 |
| 左架 `L` | −1.13 ~ −0.49 | 3.065 ~ 4.285 | 640 × 1220；離左牆 930 |
| 右架 `R` | +0.53 ~ +1.17 | 3.065 ~ 4.285 | 640 × 1220；兩架間 1020、離右牆 920 |
| 定位點 | 0 | 2.09 | 不在 CAD 圖上：11-26 實際貼的位置（`config.json` 的 `store.anchor`；程式內建建議值 2.20） |

兩座貨架前緣離大廳前緣 0.615 m、後緣離後牆 0.910 m。長廊比大廳中心偏左 15 mm，平面圖照實寫。

**貨架位置代碼**：貨架代號 + 位置編號。位置是貨架的某一面（`face`：左／右／前／後，「前」靠門）上的某一段
（`at`：沿那一面從前或左算起的比例），推車停在那一面往外 `slot_margin_m`（0.3 m）的地方。兩座貨架都是長邊朝左右，預設：

| 編號 | 位置 | 停車點（左架 / 右架） |
|---|---|---|
| 1 | 左側・後段 | (−1.43, 3.98) / (+0.23, 3.98) |
| 2 | 右側・後段 | (−0.19, 3.98) / (+1.47, 3.98) |
| 3 | 左側・前段 | (−1.43, 3.37) / (+0.23, 3.37) |
| 4 | 右側・前段 | (−0.19, 3.37) / (+1.47, 3.37) |

路線：門 → 定位點 → 直走出長廊到貨架前的走道（$y = 2.765$）→ 橫移到那一側 → 停車點（後面的話從近的那側繞過去）。
商品實際擺法不是這樣分的話改 `config.json`，程式不用動，例如每座貨架只分前後兩面：

```json
"store": { "slots": { "F": {"name": "前面", "face": "前"}, "B": {"name": "後面", "face": "後"} } }
```

`hall`、`corridor`、`anchor`、`entry_y`、`slot_margin_m`、`shelves` 也都可以覆蓋。app 啟動時 `validate()` 檢查：長廊接在大廳前緣、
定位點在長廊裡、貨架在大廳裡、每個停車點都在大廳裡，不合理就報錯結束。

**長廊與定位點**：長廊用來**鎖住移動方向**——推車在長廊裡只能朝店內直走，所以航向就是平面圖的 $+y$、
橫向位置就是長廊中線。長廊靠末端的**定位點**地上平貼一張 ArUco 標記（`python3 -m tools.make_anchor_marker` 產生，
印的時候保持正方形）。前方相機（約 50 cm 高、往斜前方打，OpenCV）看到它，就算出標記在推車前方、右方各幾公尺，
反推推車在平面圖上的位置，「大圖座標」（平面圖）與「內部定位座標」（里程計，原點是開機那一刻）在這裡接起來。

**座標換算**：里程計位置 $(X, Y)$（mm）→ 平面圖 $(x, y)$（m）：

$$\begin{bmatrix} x \\ y \end{bmatrix} = \frac{1}{1000}\begin{bmatrix} \cos\alpha & -\sin\alpha \\ \sin\alpha & \cos\alpha \end{bmatrix}\begin{bmatrix} X - X_0 \\ Y - Y_0 \end{bmatrix} + \begin{bmatrix} x_0 \\ y_0 \end{bmatrix}$$

- $(X_0, Y_0)$：對齊那一刻的里程計位置；$(x_0, y_0)$：那一刻推車在平面圖上的位置
- $\alpha$：里程計航向 → 平面圖航向的差，平面圖航向 $\theta = \psi + \alpha$，$\psi$ 是里程計（IMU）航向

| 時間點 | 做什麼 | 地圖上 |
|---|---|---|
| BLE 判定進場 | 推車在長廊、朝店內：$\alpha = 90^\circ - \psi$，$(x_0, y_0) = (0,\ \text{entry\_y})$；之後 $x$ 鎖在長廊中線 | 空心點（約略） |
| 相機看到地上的定位點標記 | 航向固定成正向 $\alpha = 90^\circ - \psi$；$(x_0, y_0) = (x_a - \ell_r,\ y_a - \ell_f)$ | 實心點 |
| 之後一直 | 貨架邊緣的消失點修航向（下面） | |

$(x_a, y_a) = (0, 2.20)$ 是定位點（標記）在平面圖上的位置，$\ell_f, \ell_r$ 是標記在推車前方、右方幾公尺。
標記在視野裡的每一張影像都重新對齊一次，推車越靠近越準。`entry_y` 是 BLE 判定出進場時推車大約在長廊哪裡
（判定比實際穿越晚 1~2 秒，預設 0.8 m）。登入時推車已經在店裡也一樣：推回長廊讓相機看到標記；
標記被擋住的話，找貨頁有「**我在定位點**」按鈕（推車正好停在標記上時按）。走出門口、登出時位置作廢。

**標記 → 推車位置**（`core/store/vision.py`）：標記四個角 → `cv2.solvePnP`（`IPPE_SQUARE`）→ 標記在相機座標
（$x$ 右、$y$ 下、$z$ 沿光軸）裡的旋轉 $R$、位置 $\mathbf{t}$（相機 → 標記中心）。標記貼在地上，所以 $R$ 的第三欄
$\mathbf{n}$ 就是「往上」：

$$h = -\mathbf{n}\cdot\mathbf{t},\qquad \mathbf{g} = \mathbf{t} - (\mathbf{t}\cdot\mathbf{n})\,\mathbf{n},\qquad \mathbf{f} = \frac{\hat{\mathbf{z}} - (\hat{\mathbf{z}}\cdot\mathbf{n})\,\mathbf{n}}{\lVert\hat{\mathbf{z}} - (\hat{\mathbf{z}}\cdot\mathbf{n})\,\mathbf{n}\rVert},\qquad \mathbf{r} = \mathbf{f}\times\mathbf{n}$$

$$\ell_f = \mathbf{g}\cdot\mathbf{f},\qquad \ell_r = \mathbf{g}\cdot\mathbf{r}$$

- $h$：相機離地高度，跟 `vision.camera_height_m`（0.5）差超過 `height_tol_m`（0.15）就當誤判
- $\mathbf{g}$：相機正下方 → 標記中心的水平向量；$\mathbf{f}$：光軸投影到地面 = 車頭方向；$\mathbf{r}$：車身右方
- 不需要知道相機俯角，地面方向由標記本身量出來；水平距離 $\sqrt{\ell_f^2 + \ell_r^2} \le$ `anchor_trigger_m`（1.5 m）才採用

**消失點修航向**：貨架都跟平面圖座標軸平行，層板、上下緣、地磚縫在影像上交於一個消失點 $(u, v)$。相機往下俯 $\beta$，
一個跟光軸水平夾角 $\gamma$（往右為正）的水平方向在相機座標是 $(\sin\gamma,\ -\cos\gamma\sin\beta,\ \cos\gamma\cos\beta)$，投影：

$$u = c_x + f_x\,\frac{\tan\gamma}{\cos\beta},\qquad v = c_y - f_y\tan\beta$$

所以先由 $v$ 求俯角、再由 $u$ 求夾角：

$$\beta = \arctan\frac{c_y - v}{f_y},\qquad \delta = \gamma = \arctan\frac{(u - c_x)\cos\beta}{f_x},\qquad \theta_v = \phi + \delta$$

$f_x, f_y$ 焦距、$c_x, c_y$ 光心（像素），$\phi \in \{0^\circ, 90^\circ, 180^\circ, 270^\circ\}$ 取最接近目前估計的。
誤差 $e = \theta_v - (\psi + \alpha)$，$|e| \le 15^\circ$ 才採用：$\alpha \leftarrow \alpha + w\,e$（$w$ = `vision.vp_yaw_weight`，預設 0.2），
以推車目前位置為中心轉，畫面上的點不跳、之後的位移方向變準。找法：Canny → HoughLinesP → 只留斜線 →
兩兩交點投票 → 最小平方。相機假設跟車身同方向（左右沒裝歪）、沒有側傾。

**設定**（`config.json`，沒寫的用預設值）：

```json
"vision": { "anchor_marker_id": 0, "anchor_marker_size_m": 0.15, "anchor_trigger_m": 1.5,
            "camera_height_m": 0.5, "height_tol_m": 0.15, "vp_yaw_weight": 0.2, "fps": 5, "proc_width": 640 }
```

`vision` 沒寫的欄位用 `core/config.py` 的 `VISION_DEFAULTS`；`config.json` 目前寫了標記編號、邊長與相機高度。

前方相機是 app 的第 4 個必要硬體（`drivers/front_camera.py`），開不起來 app 就不啟動：

- 擷取共用 `drivers/camera_stream.CameraStream`：跟棋盤格校正同一條 picamera2 設定路徑（`preview_configuration` + `align()`），
  全專案只有這裡開相機（一顆 CSI 相機同時只能被一個 `Picamera2` 打開）。
- `CameraStream` 用 `calibration_file=None` 開，拿原始影格；`front_camera.py` 轉灰階、縮到 640 寬，再用預先算好的
  對照表去畸變。**只去一次**，所以 `solvePnP` 的畸變係數傳 0、內參用縮小後的 $K'$。
- 影格超過 1 秒沒更新就丟例外（相機斷線），由 `ui/workers.py` 的 `VisionConsumer` 記錄。

內參換算：校正解析度 $(W_c, H_c)$ = `camera.resolution` = 1280×720，處理解析度 $(W_p, H_p)$ = 640×360，
$s_x = W_p / W_c$、$s_y = H_p / H_c$：

$$f_x' = s_x f_x,\qquad c_x' = s_x c_x,\qquad f_y' = s_y f_y,\qquad c_y' = s_y c_y$$

$f_x, f_y$ 是焦距、$(c_x, c_y)$ 是主點（像素）；畸變係數對正規化座標定義，不隨解析度變。
校正檔（`vision/camera_calibration.npz`）存 `camera_matrix`、`dist_coeffs`（有理模型 8 個係數），以及量的時候的
影像尺寸 `size`、翻轉 `hflip`/`vflip`、感測器模式 `sensor_size`；`FrontCamera` 開相機時逐一核對，對不上直接報錯：
實際影格尺寸跟 `camera.resolution` 不同、翻轉跟 config 不同、或感測器模式（視野）不同，都代表內參不能用。

**誤差**：長廊 0.62 m 寬，對齊時推車中心橫向最多偏約 ±0.1 m（車寬佔掉大半）；位移量隨里程計累積（直線實測 ±2%），航向由消失點拉回。

### 現場驗證

標記與消失點目前只在合成影像上測過，下面是實機要做的，照順序：

**1. 貼定位點標記**：`python3 -m tools.make_anchor_marker`，印的時候保持正方形（縮放 100%、取消「符合框架」），推車會壓過去建議護貝。
量黑色方框邊長 $s$（m，不含白邊），填 `vision.anchor_marker_size_m`（目前 0.15）。
平貼在**長廊中線**、標記中心離門（閘門線）$d_a$ = 2.20 m 的地上，方向不拘。

**為什麼放在長廊裡**：相機在 `anchor_trigger_m`（1.5 m）以內才採用標記，放在 2.20 m 代表推車在 $y$ = 0.7 ~ 2.2 m 之間會看到它——
這一段推車一定在長廊、車頭朝店內，「對齊時航向 = $+y$」的假設才成立。貼的位置跟 2.20 m 差超過 1~2 cm 的話，
量實際的 $d_a$ 改 `config.json`：`"store": {"anchor": {"name": "定位點", "x": 0.0, "y": <d_a>}}`；之後所有推車位置都以這一點為準，**這是整張圖最要量準的一個數**。

**2. 對 CAD 圖**：捲尺抽量幾個尺寸（長廊寬 0.62 m、兩架之間 1.02 m、貨架前緣到大廳前緣 0.615 m），差超過約 3 cm 就照實際改 `config.json` 的 `store` 區塊。

**3. 量相機高** $H$（m）：鏡頭中心離地，填 `vision.camera_height_m`（目前是約略值 0.5）。

**4. 靜態比對**：`python3 -m tools.verify_store_vision`（在 Pi 桌面上跑會開即時預覽視窗：標記外框綠色 = app 會採用、橘色 = 看到但不採用，對準後在視窗按空白鍵收一輪）。車頭朝店內，停在標記前方約 0.6、1.0、1.4 m，各跑一輪；
捲尺量「相機正下方的地面點 → 標記中心」沿車頭方向的距離 $\ell_f$，程式會問。

| 看什麼 | 合理範圍 | 不合理時先查 |
|---|---|---|
| 看到標記的張數 | 接近總張數 | 標記反光、太遠（$d >$ `anchor_trigger_m` 1.5 m 只是不採用，應該還看得到） |
| 前方距離誤差 $\hat{\ell}_f - \ell_f$ | 5 % 以內 | 各距離的**百分比**都差不多 → $s$ 量錯；越遠越差 → 內參 |
| 相機高 $h$ 跟量的 $H$ | ±0.05 m（app 容許 ±0.15 m） | 一直偏同一邊 → 主點 $c_y$（見上面內參換算），重新校正 |
| 車頭正對貨架時的 $\delta$ | ±2° | 相機左右裝歪（README 假設跟車身同方向） |
| 車頭往右轉約 10° 時的 $\delta$ | 約 −10°（往左轉約 +10°） | 正負號相反 → 回報，不要自己改公式 |
| 俯角 $\beta$ | 跟相機實際俯角接近 | 支持線段太少（地磚、層板邊不明顯） |

$\hat{\ell}_f$ 是程式量的中位數，$\delta$ 是消失點方向跟光軸的水平夾角（右為正），$\beta$ 是相機俯角。
標註影像在 `data/vision_check/<時間>/round<k>.png`，可以放報告。

**11-26 靜態比對結果**（`data/vision_check/20261126-062944/`，內參 `data/camera_calibration/20261126-062138`，重投影誤差 0.11 px）：

| 輪 | 程式量的前方距離 $\hat{\ell}_f$ | 相機高 $h$（量的 0.52 m） | 偵測／採用 | 消失點 $\delta$ |
|---|---|---|---|---|
| 1 | 1.47 m | 0.49 m | 15/15 | −6.0°（車頭稍微偏右） |
| 2 | 1.04 m | 0.51 m | 15/15 | −1.4° |
| 3 | 0.61 m | 0.52 m | 15/15 | −1.7° |

每一輪 15 張都找到標記、全部採用；相機高越遠誤差越大（1.47 m 時差 3 cm），仍在容許的 ±0.15 m 內。
內參的棋盤格照片都是平放在地上拍的（角點只在畫面下方 40%），用這 25 張各自反推相機高是 0.516 ~ 0.530 m，地面這個用途是準的；
1 m 以外的標記落在畫面中段、靠模型外推，上表 1.47 m 那輪 $h$ 偏低 3 cm 就是這個來源。要更準：補拍棋盤格拿在手上、在畫面上半部的照片一起算。

**5. 動態**：開 app、登入，從門口推進長廊：

1. 找貨頁出現空心點，沿長廊中線往前。
2. 離標記約 1.5 m 以內，終端機出現「看到定位點標記（前方 … m、右方 … m、相機高 … m），地圖對齊完成」，點變實心。
3. 推到某個貨架的停車點（例如 `L3` 左架左側前段：左架左邊 0.3 m、離貨架前緣 0.3 m），看地圖上的點有沒有落在那附近；再往左右各推一段，看方向對不對。

**6. 商品資料**：`python3 -m tools.product_admin meta <條碼>` 設貨架（`L1`~`R4`）、成分標籤、折扣、搭配（見〈資料庫與後台〉）。

---

## 門口進出場判定（`core/gate/`）

**結論**：門內、門外各一顆 BLE Beacon，比較兩顆平滑後的 RSSI 誰強——強的那側就是推車靠的那側，佔優勢的一側翻轉就是一次穿越。
11-16 裝上推車校正，錄音重播 8 趟全部判對、停門檻線與店內深處沒有來回跳（10-28 那次實測反應約 1~2 秒）。判定規則只有一套（`core/gate/model.py`），
執行期、校正、事後分析都用它，所以「校正時重播通過」跟「實際跑起來」是同一件事。

為什麼用差分不用絕對門檻：車體金屬、人的遮擋造成的衰減會同時影響兩顆，相減可以抵掉。
為什麼不用 RSSI 算連續座標：室內多路徑讓 RSSI 抖 ±10~15 dB，換成距離誤差好幾公尺。
誠實的限制：RSSI 沒有方向性，物理上量不到「有沒有真的穿過那個開口」，跟光電閘門比先天有模糊地帶，要靠佈署（擺法、遮罩）與絕對門檻配合。

### 判定規則（每收到一筆觀測做一次）

令

- $W$：平滑窗口長度 `rssi_smoothing_sec`（秒）
- $s_{in}, s_{out}$：門內、門外 Beacon 窗口內 RSSI 經估計量（`rssi_estimator`）壓成的值，再各加上自己的偏移 `rssi_offset_db`（dBm）
- $d = s_{in} - s_{out}$：差分（dB），$d > 0$ 代表比較靠門內
- $H$：遲滯 `gate_hysteresis_dbm`（dB）
- $R_{\min}$：絕對門檻 `gate_min_crossing_rssi_dbm`（dBm）
- $n_c$、$T_c$：確認筆數 `gate_min_confirm_samples`、確認時間 `gate_min_confirm_sec`（秒）

步驟：

1. 這顆 Beacon 的原始 RSSI 放進它自己的歷史；兩顆都只留 $t_{now} - W < t \le t_{now}$ 的樣本。
2. 任一顆窗內少於 `rssi_min_samples` 筆 → 這一刻不判定。
3. 算 $s_{in}$、$s_{out}$、$d$。
4. $d \ge +H$ 視為偏門內，$d \le -H$ 視為偏門外，中間不改判定；偏的那一側強度要 $\ge R_{\min}$。
5. 連續 $n_c$ 筆、**而且**從第一筆算起持續 $T_c$ 秒都偏同一側才確認；確認的一側跟上次不同 → 一次 `entering`／`exiting`。
6. `gate_exit_yaw_deg` 有填時，再用 IMU 航向交叉驗證（見下）。

| 誰在用 | 怎麼用 |
|---|---|
| 執行期 `core/gate/monitor.py` | 一筆一筆即時跑（app、`--monitor`），每一筆都記到 `data/gate_logs/` |
| 校正 `core/gate/calibration.py` | 候選參數丟進這套規則，重播實測錄音，行為對才採用 |
| 分析 `tools/analyze_gate_log.py` | 用記錄檔重播，核對、換參數試算 |

`tests/test_gate_model.py` 驗證三者算出來的事件、每一筆差分值都一模一樣。

**為什麼要 $T_c$**：判定每收到一筆觀測就做一次，兩顆加起來每秒約 13 筆，$n_c = 3$ 筆只有 0.2 秒；而 max 估計量會被一筆特別強的封包撐住一整個窗口，
這 3 筆用的其實是同一筆封包——等於一筆封包就能確認。11-16 的實際記錄裡「進場 0.5 秒後又出場」就是這樣來的。要求持續 $T_c$ 秒，跟封包率無關。$T_c = 0$ 是不看時間。

**航向交叉驗證不會啟用**：BNO080 給的是相對航向（開機為 0），同一個出場方向每次開機對應的 yaw 都不同，填不出固定角度，所以 `gate_exit_yaw_deg` 維持 `null`。
要用的話得改成相對判斷（出場時跟上一次進場時差約 180°），還沒做。程式保留，有填才生效。

### 濾波器怎麼選

實測 RSSI 的雜訊是**單邊的**：很少突然變強（最多 +1 dB），常常突然變弱 6 dB——多路徑破壞性干涉的典型特徵。所以上包絡線才接近真正的直達路徑。
各估計量在靜止時的殘餘抖動（窗口 5 筆，2026-09-15 實測）：

| 估計量 | INSIDE | OUTSIDE | 可靠需要的 ΔRSSI（3σ） |
|---|---|---|---|
| median | 2.29 dB | 0.60 dB | 7.1 dB（最差） |
| mean | 0.89 dB | 0.80 dB | 3.6 dB |
| p75 | 0.00 dB | 0.53 dB | 1.6 dB |
| p90 | 0.29 dB | 0.46 dB | 1.6 dB |
| max | 0.49 dB | 0.49 dB | 2.1 dB |

中位數對單邊雜訊會被下半部的衰減拉著跑，樣本雙峰分布時還會在兩群之間跳。實際用哪一個由校正從 p75／p90／max 裡挑（11-16 選到 max）。

**代價是延遲**：取上包絡線的估計量在訊號下降時會黏在窗口內最強（最舊）的那筆，延遲約等於一整個窗口，所以

$$W \approx \frac{\text{可接受的判定延遲距離}}{\text{推車速度}}$$

例如 1 m/s 配 1 秒窗口 = 判定晚約 1 m。延遲兩邊對稱（兩顆用同一個估計量），只會變晚、不會判錯方向——安全的失效方向。
窗口用**時間**而不是筆數：兩顆的廣播間隔差了 3 倍，固定筆數會讓慢的那顆涵蓋 3 倍的時間、系統性落後。

濾波的效果（同一組靜止資料）：差分的標準差從原始的 3.19 dB 降到 0.17 dB，遠小於 $H = 4$ dB，靜止時不可能誤翻。
`drivers.ble_beacon_scanner` 印的是原始讀數，系統真正用的數字用 `calibrate_gate_beacons --monitor` 看。

### Beacon 佈署指南

**軟體校正只能修兩顆的基準差，修不了「位置變化造成的差分小於雜訊」——那是佈署問題。**
判定準不準取決於「走幾步讓 $d$ 變多少」相對於「站著不動時 $d$ 自己抖多少」。

| 做法 | 對梯度 | 對「店裡別處不要誤判」 |
|---|---|---|
| 兩顆的間距與擺法 | **決定性的** | 中等 |
| 加指向性（金屬盒、反射板） | **有效** | 有效 |
| 提高廣播頻率、加大窗口 | 不變，但雜訊變小 → 等效變好 | 無 |
| 調低發射功率 | **完全無效**（兩顆一起降，相減抵消） | 有效（縮小範圍） |
| 軟體偏移 `rssi_offset_db` | 不變，只把判定邊界挪到正確位置 | 無 |

**擺法**：兩顆沿**行進方向**前後擺（一顆門內、一顆門外），推車從兩顆中間穿過；左右分兩側擺的話差分不會翻轉。
高度跟推車上 Pi 的天線差不多（裝天花板等於多一段固定距離，梯度被稀釋），天線同方向（極化差 90° 會有 10~20 dB 損耗）。

間距 $D$ 與梯度（推車沿連線通過，中線附近走 25 cm 的差分變化）：

| $D$ | 走 25 cm | 門檻線 ±50 cm 的差分 |
|---|---|---|
| 1.0 m | 9.5 dB | ±14 dB |
| **1.5 m** | **6.0 dB** | **±14 dB** |
| 2.0 m | 4.4 dB | ±9.5 dB |
| 3.0 m | 2.9 dB | ±6.0 dB |

建議 $D$ = 1.0~1.5 m。$D$ = 1.5 m 時橫向偏離連線 50 cm 差分還有 ±7.6 dB、100 cm 剩 ±3.8 dB，門口通道本來就窄，夠用。
差分法只在兩顆的中線附近才陡；遠場（例如離一顆 1.7 m、另一顆 3.6 m）走 25 cm 只變 1.8 dB，跟雜訊同量級。

**指向性**（2.4 GHz 波長 $\lambda$ = 12.5 cm）：

- **金屬盒**（效果最好）：Beacon 放進只有單面開口的金屬盒，開口朝自己那一側。餅乾鐵盒、鋁製盒，或紙盒內貼滿鋁箔都可以。前後比約 15~25 dB。
- **平板反射器**：Beacon 背後 $\lambda/4 \approx$ 3 cm 放一塊邊長 ≥ 12.5 cm 的金屬板，擋住對面。前後比約 10~15 dB。
- 兩顆之間立擋板：訊號會從邊緣繞射、從天花板反射繞過去，板子要 > 1 m 才有用，不建議。

鋁箔就夠（集膚深度只有幾 µm），重點是**連續**：孔洞小於 1.2 cm（$\lambda/10$）就等同實心。中線兩側被擋的量一樣，所以判定邊界不會偏移、只是兩側更好分。
實務上能拿到 10~20 dB 的對比改善，不是完全隔絕——多路徑會繞過遮蔽物，但 20 dB 已經綽綽有餘。
本專案用兩個鍋子背對背黏成一組（10-28）：正面掉了 11~12 dB、背面只掉 2~3 dB，像是極化沒對齊（夾角 $\theta$ 的損失 $-20\log_{10}\lvert\cos\theta\rvert$ dB），安裝時要轉到極化對齊的角度。

**韌體**：

- Arduino UNO R4 WiFi（GATE-INSIDE）預設廣播間隔太長，掃到的筆數只有 ESP32 的 1/3。`BLE.setAdvertisingInterval(32)`（單位 0.625 ms → 20 ms）；R4 的 BLE 經過板載 ESP32-S3，設定不一定生效，改完用 `drivers.ble_beacon_scanner` 數筆數確認。ArduinoBLE 沒有設發射功率的 API，所以軟體偏移非做不可。
- ESP32（GATE-OUTSIDE）：`adv->setMinInterval(0x20)`、`setMaxInterval(0x30)`（20~30 ms）。**發射功率不要調到最低**：10-28 實測 −12 dBm 時在 2.6 m 遮擋側每秒只剩 3.5 包、30 秒斷訊 4 次，改回 +9 dBm 恢復 7 包／秒、沒有斷訊。遮罩加絕對門檻已經把範圍侷限在門口。

**施作順序**：先改擺法（免費）→ 跑 `--survey` 看剖面 → 改韌體廣播間隔 → 還不夠再加金屬盒 → 最後正式校正寫回。
量測資料（10 月遮罩與角度實驗）在 `data/beacon_shield/`。

### 校正（`tools/calibrate_gate_beacons.py`）

```bash
python3 -m tools.calibrate_gate_beacons --dry-run    # 錄音、挑參數、重播驗證，不寫回
python3 -m tools.calibrate_gate_beacons              # 同上，最後問過才寫回
python3 -m tools.calibrate_gate_beacons --trips 6    # 來回幾次（預設 4 次 = 8 趟）
python3 -m tools.calibrate_gate_beacons --from-data data/gate_calibration/<時間>   # 用錄音重算
```

**錄的是推車真的走過門口**（Pi 裝在推車上、平常的速度），整段連續錄：推車停在門外約 2 m → 進場推到門內約 2 m 停好 → 出場推回門外約 2 m 停好，
重複 `--trips` 次 → 推到門檻線停 20 秒 →（選做）推到店內深處停 20 秒。存在 `data/gate_calibration/<時間>/`。

為什麼要真的走過：10-28 之前是 5 個位置各靜止 15 秒、再接起來「模擬走過」——等於推車在每個位置停 15 秒，判定很容易跟上。
同一份資料改成每個位置只停 1 秒重播，那一版建議的參數進場 15 次只判出 5 次。

**挑參數**（`core/gate/calibration.py`）：

| 參數 | 怎麼來 |
|---|---|
| 門內偏移 `rssi_offset_db` | 每一趟結尾停好的最後 1.5 秒取差分中位數：停門內 $\tilde d_{in}$、停門外 $\tilde d_{out}$，偏移 $= -(\tilde d_{in} + \tilde d_{out})/2$，兩邊對稱落在 0 的兩側（門外那顆固定 0） |
| 絕對門檻 $R_{\min}$ | 各趟結尾佔優勢那顆（校正後）強度的最小值 − 5 dB |
| 估計量、$W$、$T_c$ | 網格：p75／p90／max × 1.0／1.5／2.0 秒 × 0／0.3／0.6／1.0 秒（再加上現在的值）；$n_c$ 不調 |
| 遲滯 $H$ | 每一組網格把 $H$ 從 2 到 12 dB、每 0.25 dB 試，整段錄音重播，通過下列全部檢查才算可行；取可行範圍最寬的那一組，$H$ 取範圍中間 |

檢查（錄到的封包依時間順序餵進同一套規則）：

1. 每一趟剛好判出一次、方向正確，而且在結束後 $W + T_c + 1$ 秒內判出。
2. 走過門口的期間沒有多判的事件。
3. 停在門檻線最多 1 次事件（剛停下時判定可能還在跟上），不能來回跳。
4. 停在店內深處最多 1 次事件，而且不能判成出場。
5. 門內偏移 ±2 dB 時以上也要成立（模擬環境漂移）。
6. `--from-data` 時，校正錄音之後的執行期記錄也一起重播：不能**來回跳**（相反事件間隔 < 3 秒）。執行期記錄沒有標準答案，但來回跳不用答案也知道是錯的。
   `--no-logs` 關掉、`--logs-since 20261116-000000` 指定起點。

判讀：可行範圍 ≥ 1 dB「可以」、有但 < 1 dB「勉強」、沒有「不夠」（不給參數，列出每一組最多判對幾趟）。寫回時記下 `gate_calibration_source`。

**目前的值**（11-16 校正）：max 估計量、$W$ = 1.0 秒、$H$ = 4.0 dB、$R_{\min}$ = −68.1 dBm、$n_c$ = 3、$T_c$ 未設（0）、門內偏移 +1.9 dB。
用新版（有 $T_c$ 網格與記錄檢查）重算同一份錄音，建議 $W$ = 2.0 秒、$H$ = 4.5 dB、$R_{\min}$ = −61.9 dBm、門內偏移 +2.1 dB（勉強，範圍 0.75 dB）；
現在的參數在 11-16 之後 9 份記錄裡有 3 份會來回跳，建議參數 0 份。見〈一分鐘現況〉待辦 1。

### 記錄與事後分析

app 每次啟動自動記錄 `data/gate_logs/<日期>/<時間>_app.csv`（`--monitor` 是 `_monitor`）：每一筆 BLE 觀測一行（時間、哪一顆、原始 RSSI、航向、平滑值、差分、判定側、事件），
旁邊的 `.meta.json` 記下當時的全部參數。每小時約 2~3 MB，不進版控。寫入失敗只警告一次、停止記錄，判定照常。

```bash
python3 -m tools.analyze_gate_log                    # 最新一份
python3 -m tools.analyze_gate_log --events           # 每個事件前後的差分值
python3 -m tools.analyze_gate_log <檔案> --set hysteresis_dbm=6 --set smoothing_sec=1.5   # 換參數重播
```

會印出封包率與斷訊、當時的事件、**用當時參數重播是否跟當時一致**（一致才代表事後看到的就是當時發生的）、差分分布、各側時間比例、來回跳次數。

`--survey` 量 5 個位置的靜止剖面（門外 1 m、50 cm、門檻線、門內 50 cm、1 m，各 15 秒），印各位置的強度、差分、σ、封包率，指出哪一側平台不夠寬。
改佈署前後各跑一次比較，存在 `data/gate_survey/<時間>/`。

---

## 校正總覽

三個校正工具（秤重、光流與航向、門口）同一套做法：

1. **錄原始資料**：每一筆感測器讀數都存，標上「這時候在做哪一步」（`data/<種類>_calibration/<時間>/`）。
2. **用執行期同一份程式重播**：秤重丟進 `CartStateMachine`、光流丟進 `OdometryEngine`、門口丟進 `GateDecision`，看實際行為對不對，不用公式推「應該夠」。
3. **印判讀**（可以／勉強／不夠）、建議值與原本的值，問過才寫回，並記下用了哪一份錄音（`*_calibration_source`），之後可以 `--from-data` 重算。

| # | 指令 | 定出的參數 | 準備 |
|---|---|---|---|
| 1 | `python3 -m tools.calibrate_weight` | `weight.hx711_offset`、`hx711_scale`、`unscanned_change_threshold_g`、`settle_window_sec`、`settle_band_g` | 已知重量的砝碼（至少兩種） |
| 2 | `python3 -m tools.product_admin edit <條碼>` | 每個商品的標準重量與容差 | scale 改了就要重秤 |
| 3 | `python3 -m tools.calibrate_gate_beacons` | 門口參數（見〈門口進出場判定〉） | Pi 裝在推車上的最終位置 |
| 4 | `python3 -m tools.calibrate_optical_flow` | `odometry.*`（見〈定位：里程計〉） | 地上一條量好長度的直線（≥ 3 m） |
| — | `python3 -m drivers.camera_stream --calibrate --square-size-mm <格子邊長>` | 相機內參 | 11-26 改成完整視野的感測器模式，**要重做**（舊的 error 0.2026 是裁切模式量的）；之後換相機、改翻轉也要重做 |

### 秤重（`tools/calibrate_weight.py`，計算在 `core/shopping/weight_calibration.py`）

| 步驟 | 段落 | 用途 |
|---|---|---|
| 空秤靜止 8 秒 | `zero_start` | 零點、靜止雜訊 |
| 放砝碼靜止，可放好幾次 | `load_1`… | 最小平方擬合、看線性 |
| 全部拿掉靜止 | `zero_end` | 零點漂移 |
| 空籃推車 20 秒 | `push_empty` | 推車震動 → 未掃碼門檻 |
| 掃碼後放入砝碼 ×3 | `drop_1`… | 重播「掃碼 → 放入」，挑穩定窗 |

令 $r$ = HX711 原始讀數（count）、$m$ = 砝碼質量（g），擬合

$$r = a + b\,m,\qquad w = \frac{r - a}{b}$$

$a$ = `hx711_offset`（空秤讀數）、$b$ = `hx711_scale`（count/g）、$w$ = 換算後的重量（g）。比對只看重量**變化**，所以準不準取決於 $b$；$a$ 只影響顯示的絕對重量。

- **靜止點用中位數**，雜訊用 $\sigma = 1.4826\,\mathrm{MAD}$（MAD = 與中位數差的絕對值的中位數）：11-16 錄的時候手還壓著砝碼，平均值被拉偏 8.7 g、標準差變成 29 g，中位數不受影響。
- **未掃碼門檻**：令 $\sigma_s$ = 靜止時單筆讀數的標準差（g）、$T_{\min}$ = 把推車讀數丟進狀態機重播、不會誤報的最小門檻（g）、$T_{\text{floor}}$ = 人為下限（`--t-floor`，預設 10 g），

$$T = \max\left(\lceil 1.5\,T_{\min}\rceil,\ \lceil 6\,\sigma_s \rceil,\ T_{\text{floor}}\right)$$

  實測推車震動只有 0.3 g，前兩項都只有 2~3 g，所以由 $T_{\text{floor}} = 10$ g 決定（手扶籃子、掛購物袋不該觸發）——這一項是取捨，不是量出來的。
- **穩定窗**：$B_s = \max(2, \lceil 6\,\sigma_s \rceil)$；$W_s$ 從 0、0.2、…、1.0 秒試，用放入錄音重播，取全部正常的最小值。
- `--quick`：只做空秤、砝碼、拿掉（秤台沒動、只想重定 scale），門檻沿用原本的。
- 11-16 實測：198 g 與 581 g 各自的增量相加跟兩個一起放（779 g）只差 0.7 g，秤是線性的；`20261116-193855` 寫回 $a$ = −331970.43、$b$ = 397.392 count/g、$T$ = 10 g、$W_s$ = 0.3 秒、$B_s$ = 3 g。
- 驗證：`python3 -m tools.verify_weight`。

### 光流與航向（`tools/calibrate_optical_flow.py`，計算在 `core/positioning/flow_calibration.py`）

靜止 5 秒 → 沿直線直推 3 趟（距離 $D$）→ 往車子左側橫推（選做）→ 原地左轉 90°。

- $k = D / \lVert S \rVert$，$S = (\sum dx, \sum dy)$ 是一趟的累積位移，各趟平均；**不濾離群值**（執行期也不濾）。
- $\varphi$：直推時感測器量到的方向角（各趟的圓形平均）。
- $m$：側推方向在直推方向 −90° 時為鏡像。
- $\sigma$：原地左轉時 yaw 增加為 +1。
- 算好之後每一趟丟進 `OdometryEngine` 重播，比較距離與方向。11-16：三趟 $k$ 的變異係數 1.8%、方向分散 0.8°、原地左轉量到 91°。

---

## 設定檔 `config.json`

底線開頭的鍵是給人看的註解。讀寫都經過 `core/config.py`；校正工具只改自己的那幾個鍵，其他欄位與註解保留。

| 區塊 | 鍵 | 意義 | 目前 |
|---|---|---|---|
| `serial` | `port`、`baudrate` | UART（一定是 `/dev/ttyAMA0`，不是 `/dev/serial0`） | `/dev/ttyAMA0`、115200 |
| | `timeout_sec`、`reconnect_backoff_sec` | pyserial 讀取逾時、斷線重連間隔 | 1.0、2.0 秒 |
| `uart_protocol` | `stale_data_timeout_sec` | 多久沒封包算斷線 | 0.5 秒 |
| `weight` | `hx711_offset`、`hx711_scale` | 讀數 → 公克 | 11-16 校正 |
| | `weight_match_timeout_sec`、`unscanned_change_timeout_sec` | 比對、未掃碼逾時 | 5、8 秒 |
| | `unscanned_change_threshold_g` | 未掃碼門檻 $T$ | 10 g |
| | `settle_window_sec`、`settle_band_g` | 穩定窗 $W_s$、$B_s$ | 0.3 秒、3 g |
| `odometry` | `optical_flow_px_to_mm`、`flow_rotation_deg`、`flow_mirror`、`yaw_sign` | 里程計 $k$、$\varphi$、$m$、$\sigma$ | 11-16 校正 |
| `landmarks` | `points[]` | 每顆 Beacon 的 `beacon_id`、`address`、`rssi_offset_db` | 門內 +1.9、門外 0 |
| | `gate_beacon_pair` | 哪顆是門內、哪顆是門外 | |
| | `rssi_estimator`、`rssi_smoothing_sec`、`rssi_min_samples` | 估計量、$W$、最少樣本 | max、1.0 秒、3 |
| | `gate_hysteresis_dbm`、`gate_min_crossing_rssi_dbm` | $H$、$R_{\min}$ | 4.0 dB、−68.1 dBm |
| | `gate_min_confirm_samples`、`gate_min_confirm_sec` | $n_c$、$T_c$ | 3、未設（0） |
| | `gate_exit_yaw_deg`、`gate_heading_tolerance_deg` | 航向交叉驗證 | `null`（不啟用） |
| `state_machine` | `login_barcode_prefix`、`staff_barcode_prefix` | 會員碼、工作證前綴 | `MEMBER-`、`STAFF-` |
| | `zone_entry_timeout_sec`、`shopping_idle_timeout_sec`、`exit_timeout_after_checkout_sec` | 提醒用逾時 | 120、600、180 秒 |
| `camera` | `resolution`、`format`、`framerate`、`calibration_file` | 相機（內參在這個解析度下量的） | 1280×720、RGB888、30 |
| | `index`、`hflip`、`vflip` | 前方相機是第幾台、要不要翻轉（改了內參要跟著換） | 0（Camera Module 3）、true、true |
| `store` | `hall`、`corridor`、`anchor`、`shelves`、`slots`… | 覆蓋程式內建的平面圖（m） | 不覆蓋，用 11-26 實際場地 |
| `vision` | `anchor_marker_id`、`anchor_marker_size_m`、`camera_height_m`、`proc_width` | 定位點標記、相機高度、處理寬度；其他（`undistort_alpha` 等）見 `core/config.py` 的 `VISION_DEFAULTS` | 0、0.15 m、0.52 m、1280 |
| `barcode_scanner` | `device_name_hint`、`key_timeout_sec` | 掃描器名稱關鍵字、一次掃碼的按鍵間隔上限 | `USBKey`、0.1 秒 |

---

## UART 封包格式 v2

```
$SDK,<dx>,<dy>,<squal>,<yaw_deg>,<pitch_deg>,<roll_deg>,<hx711_raw>*<CS>\r\n
```

| 欄位 | 型別 | 意義 |
|---|---|---|
| `dx`、`dy` | int | PMW3901 這一輪的相對位移（count） |
| `squal` | int | PMW3901 追蹤品質 0~255 |
| `yaw_deg`、`pitch_deg`、`roll_deg` | float | BNO080 姿態角（度，1 位小數）；yaw 是相對航向 |
| `hx711_raw` | int | HX711 原始 ADC 值 |
| `CS` | hex | `$` 與 `*` 之間所有字元的 XOR（NMEA 算法），2 位十六進位 |

MCU 20 Hz 固定送出（沒有新資料就重送上一次的值）；8N1、115200。2026-09-01 加了 `squal`，從 7 欄變 8 欄，跟舊版不相容：
每包都被丟、記錄一直印「欄位數不對」，就是 Pi 與 MCU 的格式版本對不上。用 `python3 -m drivers.uart_receiver` 看封包與每秒統計。

---

## 硬體環境與踩過的坑

### UART 節點

**不要假設 `/dev/serial0` 就是 pin 8/10 那組 UART。** 這台 Pi 5（RP1）上 `/dev/serial0` 指到 `ttyAMA10`，跟 GPIO14/15 無關，真正對應接腳的是 `/dev/ttyAMA0`。
每台 Pi、每次重燒系統都用 loopback 驗證一次：

```bash
# 杜邦線短接 GPIO14（pin 8, TXD）與 GPIO15（pin 10, RXD）
python3 -c "import serial,time; s=serial.Serial('/dev/ttyAMA0',115200,timeout=1); s.write(b'hello\n'); time.sleep(0.1); print(s.read(10))"
# 應該印出 b'hello\n'
```

接線：MCU TX → Pi GPIO15/RXD（pin 10），MCU RX ← Pi GPIO14/TXD（pin 8），兩板共地。

### venv 與 sudo

`sudo python3 ...` 不繼承 shell 的 PATH，會跑到系統的 `/usr/bin/python3`，明明裝過的套件卻找不到。要用 `sudo venv/bin/python3 -m ...`。
app 本身不需要 sudo，只要使用者在 `input` 群組裡。

### 條碼掃描器（`drivers/barcode_scanner.py`）

1. `device.grab()` 會讓這台掃描器的 Caps Lock LED 交握卡死：它送字元前會先按一次 Caps Lock 詢問大小寫狀態，等作業系統把 LED 狀態回送；grab 之後核心收不到事件、不回送，
   掃描器就一直重試（症狀：一刷就狂閃 Caps Lock，直到 ungrab 才把資料送出）。修法是自己呼叫 `device.set_led(LED_CAPSL, ...)` 回送。
2. 大小寫不能只看 Shift：這台用 Caps Lock 開關切大小寫，判斷要用 Shift XOR Caps Lock。
3. 同一台有時送小鍵盤鍵碼（`KEY_KP0`~`KEY_KP9`）、有時送數字鍵（`KEY_0`~`KEY_9`），對照表兩種都有。目前涵蓋數字、英文字母、`- . /`；條碼有其他符號時用 `--debug` 看原始鍵碼（含翻譯前的 `MSC_SCAN`）再補。
4. 實測裝置名稱 `USBKey Chip USBKey Module`（`/dev/input/event4`）→ `device_name_hint = "USBKey"`。

### 相機（`drivers/camera_stream.py`、`drivers/front_camera.py`）

1. **picamera2 的設定路徑會影響輸出**：用 `create_video_configuration()` 時顏色不對（藍變橘紅），改用 `preview_configuration` + `align()` 正常。全專案統一這條路徑（棋盤格校正也是，內參才對得上）。
2. 一顆 CSI 相機同時只能被一個 `Picamera2` 打開，所以全專案只有 `CameraStream` 開相機。
3. 前方相機：`CameraStream` 給原始影格，`front_camera.py` 轉灰階、縮到 640 寬、用預先算好的對照表去畸變（**只去一次**，所以 `solvePnP` 的畸變係數傳 0）；
   實際影格尺寸跟 `camera.resolution` 不同時直接報錯（改解析度就要重新校正）；影格超過 1 秒沒更新就丟例外（相機斷線）。
4. 校正的預覽視窗要有畫面環境，純文字 SSH 用 `--no-window`。`qt.qpa.plugin ... wayland` 與字型警告無害。
5. 內參 $c_y$ = 406.6 比影像中心 360 偏 47 px；現場驗證時相機高度如果一直系統性偏高或偏低，先懷疑這個，重新校正。
6. **接了不只一台相機（11-26 起兩台）**：`Picamera2()` 不給編號就開第 0 台，第 0 台是誰由 libcamera 的列舉順序決定，不一定是前方那台。
   `config.json` 的 `camera.index` 指定前方相機；`python3 -m drivers.camera_stream --list` 列出編號，`--snapshot` 每台各拍一張原始影像
   （存 `data/camera_check/<時間>/cam<N>.png`）看哪張是前方視角。
7. **相機裝反**（11-26 前方相機裝成轉 180°）：`camera.hflip`、`camera.vflip` 在 ISP 裡翻回來（libcamera `Transform`），上下顛倒兩個都 `true`。
   `--list` 印的 `Rotation=180` 是感測器在模組板上的方向，系統會自己補，跟你怎麼裝無關，要看 `--snapshot` 的照片判斷。
   翻轉會改變影像座標，令 $W \times H$ 為影像寬高（像素），轉 180° 後像素 $(u, v) \to (W-1-u,\ H-1-v)$，所以
   $$c_x' = W - 1 - c_x,\qquad c_y' = H - 1 - c_y,\qquad p_1' = -p_1,\qquad p_2' = -p_2$$
   （$f_x, f_y$ 是焦距、$(c_x, c_y)$ 是主點、$p_1, p_2$ 是切向畸變，徑向畸變 $k_1, k_2, k_3$ 不變；只左右翻是 $c_x$、$p_2$ 變，只上下翻是 $c_y$、$p_1$ 變）。
   **同一台相機只改翻轉**：`python3 -m drivers.camera_stream --flip-calibration` 直接換算（原檔備份成 `.bak-<時間>.npz`），不用重拍棋盤格；
   **換了相機**：`--calibrate` 重拍（會套用 config 的同一組翻轉）。校正檔記著量的時候的翻轉，跟 config 對不上 app 啟動就報錯，不會拿錯的內參算位置。
8. **USB 攝影機（Logitech C270）**：不支援 RGB888，libcamera 給 MJPEG，picamera2 解出來是 R, G, B 順序（直接存檔紅藍對調）。
   `frame_to_bgr()` 依相機的實際格式統一轉成 OpenCV 的 B, G, R；CSI 相機原樣不動。
9. **一堆 libcamera 警告**（`No static properties available for 'imx708_wide'`、`No sensor delays found`、`Zero sequence expected for first frame`）：
   前兩個是 libcamera 的感測器資料庫還沒收錄廣角版 imx708，調校檔照樣找得到；第三個是 USB 攝影機第一張影格的序號，都無害。
   `drivers/camera_stream.py` 預設只讓 libcamera 印錯誤（`LIBCAMERA_LOG_LEVELS=*:ERROR`），除錯要看全部時執行前 `export LIBCAMERA_LOG_LEVELS=*:INFO`。
10. **廣角鏡的視野被裁掉（11-26 發現）**：不指定感測器模式時，libcamera 挑「解析度最接近」的，imx708 對 1280×720 挑到
   1536×864——那是**裁切畫面中間**的模式，視野比 `rpicam-hello` 小很多。現在 `open_camera()` 固定挑**完整視野**的模式
   （`crop_limits` 涵蓋整個感光區、輸出至少 1280×720 的最小那個，imx708 是 2304×1296 的 2×2 合併），ISP 再縮到 1280×720。
   視野變了內參就不同：校正檔記下 `sensor_size`，舊檔（沒記）或模式不同 app 啟動就報錯，要重新 `--calibrate`。
   廣角鏡（水平約 102°）邊緣畸變大，校正改用有理模型（`cv2.CALIB_RATIONAL_MODEL`，$k_1 \sim k_6, p_1, p_2$），預設拍 25 張，
   **棋盤格要拍到畫面四個角落與邊緣**，不然邊緣的畸變係數估不準。
   11-26 實拍重投影誤差一直 > 1：角點精修窗口原本固定 11（23×23 px），廣角鏡下一格只有二十幾個像素，窗口蓋到隔壁角點、精修被拉偏
   （合成影像實測：固定 11 → 1.40 px，改成相鄰角點間距的 0.3 倍 → 0.31 px）。另外改成**棋盤格停住才拍**（連續兩張角點位移 < 0.7 px）、
   每張照片各算誤差並剔除特別大的、照片存 `data/camera_calibration/<時間>/` 可以 `--from-data` 重算。
   視野變寬，同一個標記在畫面上變小：`vision.proc_width` 從 640 提高到 1280（不縮）。去畸變的新內參用
   `cv2.getOptimalNewCameraMatrix`，`vision.undistort_alpha` = 0（預設，不留黑邊、角落裁一點）～ 1（全部留、中央變小）。
11. **相機俯角不用量**：定位點標記的 `solvePnP` 直接量出地面的法向量（推車位置與相機高度都由它算），消失點也是每張影像
   自己由 $v$ 座標算俯角 $\beta$，所以相機往下打幾度都可以，不是參數。真正有假設的是：相機**左右沒裝歪**（光軸投影到地面 = 車頭方向）、
   **沒有側傾**；這兩個裝歪了，`verify_store_vision` 的右方距離 $\ell_r$ 與 $\delta$ 會一直偏同一邊。

### 觸控畫面花屏（WebKitGTK 的 DMA-BUF）

症狀：app 開起來整個畫面花掉，程式正常、沒有錯誤。環境：Pi 5、Debian 13、labwc（Wayland）、DSI-1、Mesa 26.2.2、WebKitGTK 2.54.0（10-29 當時的最新版）。
用 pywebview 直接開設計稿、逐一切換環境變數：

| 組 | 設定 | 結果 |
|---|---|---|
| A | 關合成 + 關 DMA-BUF | 正常 |
| B | 只關合成 | 花屏 |
| C | 只關 DMA-BUF | 正常 |
| D | 全部預設 | 花屏 |
| E | `GDK_BACKEND=x11` | 正常 |
| F | `LIBGL_ALWAYS_SOFTWARE=1` | 正常 |

令 $D$ = DMA-BUF 開、$G$ = 硬體 GPU（V3D）、$L$ = Wayland 原生，六組都符合

$$\text{花屏} \iff D \land G \land L$$

app 採用 C（`ui/app_gui.py` 的 `_RENDER_ENV`）：關 DMA-BUF、合成保持預設（GPU 繪製）。而且是**強制設定**——啟動時清掉終端機殘留的其他 `WEBKIT_*`、`GDK_BACKEND` 等並印出來，
不管在哪個終端機跑都是同一條繪圖路徑。改用 VNC 又花屏時，在 `_RENDER_ENV` 加 `"WEBKIT_DISABLE_COMPOSITING_MODE": "1"`（＝A）。重做實驗：

```bash
T="import webview; webview.create_window('t','file://$PWD/design-cart-ui/Shopping.dc.html',width=720,height=1280); webview.start()"
WEBKIT_DISABLE_DMABUF_RENDERER=1 python3 -c "$T"
```

另一個坑：pywebview GTK 版的 `evaluate_js` 把字元數當位元組數傳給 WebKit，腳本有中文就被截斷成語法錯誤、畫面停在舊狀態（11-16：會員名稱是中文時進場後畫面一直顯示等待進場）。
推給畫面的 JSON 一律 `ensure_ascii=True`。

### 其他

- 裝主動散熱。
- PMW3901 的 SPI 長線不穩時，可以把 MCU 端 SPI 時脈從 1 MHz 降到 500 kHz——那是 MCC 產生的檔案，要在 MCC GUI 改，不要直接改生成檔。
- PMW3901 曾有休眠現象（那段時間 dx/dy 遺失），MCU 端已加看門狗重置。

---

## 測試

```bash
python3 tests/run_all.py
```

| 測試 | 內容 | 項目 |
|---|---|---|
| `test_app_gui_bridge.py` | Bridge 端對端：整趟購物流程、17 種錯誤情境、精靈、偏好、交易、店員、健康護照、錢包、優惠、平面圖、機器視覺（合成影像）、推送、JsApi、多執行緒 | 207 |
| `test_gate_model.py` | 門口判定：即時 = 重播 = 記錄檔重播 | 23 |
| `test_gate_monitor.py` | 門口判定的方向、門檻、確認、航向 | 34 |
| `test_calibrate_gate_beacons.py` | 門口校正的計算與錄音流程 | 37 |
| `test_calibrate_weight.py` | 秤重校正 | 30 |
| `test_calibrate_optical_flow.py` | 光流校正、座標轉換 | 27 |
| `test_odometry_track.py` | 行駛軌跡重播與誤差指標 | 22 |
| `test_ui_layout.py` | 14 頁版面與前端互動（要 playwright，沒裝就略過） | 33 |

測試用的都是跟正式運作**同一個入口**（例如 `Bridge.on_hardware_barcode` / `on_weight_sample` / `on_gate_crossing`），不是另開一條測試旁路。
`test_app_gui_bridge.py` 每次只餵一筆秤重讀數，所以把穩定窗關掉（其他參數照 `config.json`）；穩定窗由 `test_calibrate_weight.py` 用真實錄音重播驗證。

改了校正或判定的程式之後，另外用錄音重算確認結果沒變：`calibrate_* --from-data <目錄> --dry-run`、`analyze_gate_log <檔案>`、`track_odometry --from-data <目錄>`。

---

## 已知限制與待辦

**限制**

- **推車位置只在定位點對齊一次**，之後靠里程計累積；店裡沒有第二個校正點，路線固定是「門 → 定位點 → 貨架」，不會依目前位置重新規劃。
- **相機視覺只在合成影像上測過**（俯角 25°~35°），還沒在實體貨架前驗證。相機假設跟車身同方向、沒有側傾；相機位置當成推車位置。
- **航向交叉驗證不會啟用**（相對航向，見〈門口進出場判定〉）。
- **開 app 時推車要在門外**，否則第一趟進場判不出來（登入時已在門內的情況另有處理）。
- **單機資料庫**：不偵測同一會員在別台推車登入。
- 優惠只有單件折扣（沒有「第 2 件 5 折」）；沒有常購清單。
- `MEMBER-` 會員碼格式是先假設的，正式會員卡設計出來要跟著改。

**還沒做**

- LLM：錢包管家的消費分析、健康護照的替代建議、優惠推薦（目前都是規則式）。
- 異常偵測模型。
- 室內導航的路徑規劃（例如 A*）與店內第二個校正點（例如貨架上的 Beacon）。

---

## 專題沿革與設計取捨

**跟前身提案分道揚鑣**：前身是 YOLOv8 + 雙鏡頭 + Hailo-8L NPU 的視覺辨識路線。重新蒐集與訓練模型的成本、時程、以及 YOLOv8 在 Pi 5 上的效能都難以配合，所以：
定位改用 PMW3901 光流 dead-reckoning（不是視覺 SLAM），防損改用「掃碼 + HX711 秤重比對」（不是視覺辨識）。
前身的四個前端旗艦功能——室內導航、錢包管家、健康護照、即時優惠——保留下來，目前做成規則式（不依賴 LLM）。

| 日期 | 里程碑 |
|---|---|
| 09-01 | UART 封包 v2（加 squal） |
| 09-15 | 單一實體路徑原則：拿掉所有模擬與旁路；第一次實測 BLE RSSI，發現兩顆 Beacon 基準差 12.5 dB（不同廠牌），加入偏移校正 |
| 10-21~10-28 | Beacon 遮罩與角度實驗；門口判定統一成一套規則；改成錄「真的走過門口」來校正 |
| 10-29 | 觸控畫面花屏查到是 WebKitGTK 的 DMA-BUF，固定繪圖設定 |
| 11-16 | 全部硬體裝上推車並重新校正（秤重、門口、光流）；加入「等讀數穩定才比」；BLE 版完整購物流程實機跑通 |
| 11-23 | 健康護照、錢包管家、找貨（平面圖 + 推車位置）、優惠改成資料驅動；地上定位點與貨架消失點；行駛軌跡工具；專案整理（見下） |
| 11-26 | 前方相機改裝成轉 180°（軟體翻回、內參換算）、加一台 USB 攝影機 C270；場地架好：平面圖照 CAD 圖改成實際尺寸（單位公尺、兩座貨架），拿掉「一格幾公尺」`store.unit_m`，啟動時改成檢查平面圖是否合理 |

### 11-23 專案整理

- **結構**：`core/` 依領域分成 `shopping/`、`positioning/`、`gate/`、`store/`；`ui/app_gui.py` 拆成主程式、`bridge.py`、`workers.py`；`config.json` 的讀寫收斂到 `core/config.py`；工具共用的小函式在 `tools/_common.py`。
- **修正的問題**：
  - 重量變了還沒掃碼（`AWAITING_ITEM_SCAN`）時推出門，原本只記 INFO，現在發 CRITICAL「沒結帳出場」。
  - `Bridge` 被 5 條以上執行緒同時呼叫卻沒有上鎖，現在全部在同一把鎖裡；畫面推送改由專門的執行緒在鎖外做。
  - 讀取門口設定時漏抄 `gate_min_confirm_sec`，校正寫回的 $T_c$ 執行時永遠是 0；現在整個區塊直接傳。
  - pywebview 會把 `Bridge` 的 `db`、`sm` 展開給前端呼叫；現在前端只看得到 `JsApi` 的 21 個方法。
  - 各處建立 UART 接收器時沒套用 `config.json` 的逾時與重連參數；現在統一 `UartReceiver.from_config()`。掃描器的 `key_timeout_sec` 也接上了。
- **刪除**：終端機版整合測試、舊的光流驗證工具（被 `track_odometry` 取代）、Beacon 遮罩與角度量測工具（實驗已完成，資料保留在 `data/beacon_shield/`）、
  UART CSV 記錄、里程計即時監看指令、一般地標的峰值偵測與地標校正（從沒被呼叫）、`config.json` 裡沒有程式讀的設定。
- 用 11-16 的校正錄音與執行期記錄重算，整理前後的校正結果、門口事件、軌跡數字完全相同。
