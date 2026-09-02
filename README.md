# SmartCart_Pi5 — 開發紀錄

Phase 1（環境建置與硬體可靠度驗證）、Phase 2（資料擷取驅動層）已完成。
Phase 3（定位積分與視覺融合）進行中，目前完成第一步「純 UART 版本的基礎
里程計」（`core/odometry_engine.py`），也完成了 BLE 地標校正的軟體邏輯
（`core/landmark_correction.py`，同時解決 Phase 4 的門口偵測跟 Phase 7 的
地圖對齊，見對應章節）；視覺校正（`vanishing_point.py`）、視覺光流備援
（`floor_optical_flow.py`）、真正的 BLE 掃描硬體整合留待下次（等硬體組裝、
延長線到貨、有真的 Beacon 可以測）。Phase 4（商業邏輯與狀態機）已開始：
整套「登入->進管制區->購物->鎖定結帳->付款->出管制區->登出」流程的狀態機
（`core/cart_state_machine.py`）、購物清單資料結構（`core/cart_manager.py`）
都已完成並邏輯測試過。Phase 5 起（UI、系統整合、AI）留待之後。

以下內容已經在實體 Pi 5（YichaoPi5）+ 真實硬體上跑過、修過踩到的坑，不是紙上規劃。

## 目錄結構

```
SmartCart_Pi5/
├── config.json              # 系統全域參數，含待校正值（標記 CALIBRATE_ME）
├── requirements.txt
├── drivers/
│   ├── uart_receiver.py     # 背景執行緒 + Queue，含 checksum 驗證與可選 CSV logger
│   ├── barcode_scanner.py   # evdev 攔截 USB 條碼掃描器，含 --debug 診斷模式
│   └── camera_stream.py     # picamera2 影像擷取 + 棋盤格畸變校正 + --color-test 診斷工具
├── database/
│   └── db_manager.py        # SQLite CRUD，商品(products)+會員(members)兩張表，含測試資料 seed
├── vision/
│   └── camera_calibration.npz  # 相機內參校正結果（已產生，reprojection error 0.2026）
├── tools/
│   ├── mock_uart_generator.py     # CI / 無硬體測試輔助工具（非正式流程必要步驟）
│   ├── mock_barcode_input.py      # 同上
│   ├── calibrate_weight.py        # HX711 重量校正互動工具：算 offset/scale 並寫回 config.json
│   ├── calibrate_optical_flow.py  # PMW3901 光流位移校正互動工具：算 px_to_mm 並寫回 config.json
│   ├── verify_weight.py           # 重量校正「驗證」工具：只讀 config.json 現有值，實測比對，不寫回
│   └── verify_optical_flow.py     # 光流校正「驗證」工具：同上
├── core/
│   ├── position_types.py    # Phase 3 定位系統最終輸出格式（PositionEstimate），UI/AI 可直接針對這個開發
│   ├── odometry_engine.py   # Phase 3 基礎里程計：純 UART dead-reckoning，含 squal 過濾
│   ├── weight_convert.py    # HX711 raw -> 公克換算公式（校正/驗證/狀態機共用）
│   ├── cart_manager.py      # Phase 4 購物清單資料結構（清單、數量、金額）
│   ├── cart_state_machine.py # Phase 4 購物流程狀態機（登入~登出整趟流程的正確性保障）
│   └── landmark_correction.py # Phase 3/7 BLE 地標校正：門口偵測 + 地圖對齊，同一套邏輯
├── ai/, ui/                 # 目前只有 __init__.py，Phase 5/6 起才會實作
```

## Pi 5 上要做的事（依順序）

### 1. 關閉 Serial Console，確認硬體 UART 節點

```bash
sudo raspi-config   # Interface Options -> Serial Port -> login shell: No, hardware enabled: Yes
```

確認 `/boot/firmware/config.txt` 有 `dtparam=uart0=on`。

**不要假設 `/dev/serial0` 就是接腳 8/10 那組 UART。** 本專案實測過的 Pi 5
（RP1 架構）上，`/dev/serial0` 指到 `ttyAMA10`，跟 GPIO14/15 無關；真正對應
接腳的是 `/dev/ttyAMA0`（已實測確認）。用 loopback 驗證：

```bash
# 用杜邦線短接 GPIO14(pin 8, TXD) 和 GPIO15(pin 10, RXD)
python3 -c "
import serial, time
s = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
s.write(b'hello\n')
time.sleep(0.1)
print(s.read(10))
"
# 應該印出 b'hello\n'，表示這個裝置節點就是實體接腳
```

確認後把 `config.json` 的 `serial.port` 填成驗證出來的裝置節點（目前是
`/dev/ttyAMA0`，但每台 Pi / 每次重新燒錄系統都建議重新驗證一次）。

### 2. 安裝依賴套件

```bash
python3 -m venv --system-site-packages venv   # --system-site-packages 讓 picamera2 可見
source venv/bin/activate
pip install -r requirements.txt

# picamera2 建議用系統套件（見 requirements.txt 註解）：
sudo apt update && sudo apt install -y python3-picamera2 --no-install-recommends
```

**用 `sudo` 執行需要存取 `/dev/input/*` 的指令（例如條碼掃描器）時要注意 venv 陷阱**：
`sudo python3 ...` 預設不會繼承目前 shell 的 `PATH`，會跑到系統的 `/usr/bin/python3`
而不是 venv 裡裝了套件的那個 Python，導致明明裝過的套件卻報錯找不到。正確用法：

```bash
sudo venv/bin/python3 -m drivers.barcode_scanner --list
# 或者： sudo -E python3 -m drivers.barcode_scanner --list（-E 保留環境變數，前提是已 activate venv）
```

### 3. 接線確認

MCU TX → Pi GPIO15/RXD (pin 10)，MCU RX ← Pi GPIO14/TXD (pin 8)，兩板共地。

### 4. 硬體防護

- 裝主動式散熱模組（後面 Phase 3 的即時積分運算對時序穩定度敏感）。
- 測試廣角相機排線最大穩定長度，評估是否需要 CSI 轉接延長。
- PMW3901 SPI 長距離佈線：若訊號不穩，MCC 端可考慮把 SPI 時脈從 1MHz 降到
  500kHz——**這是 MCC 產生的檔案，要先請你在 MCC GUI 裡改，不要直接動生成檔案。**

## 各驅動模組怎麼單獨測試

### UART 接收器

用真實硬體：

```bash
python3 -m drivers.uart_receiver --port /dev/ttyAMA0
```

沒有硬體時，用 mock 產生器（不需要 sudo，純軟體 PTY）：

```bash
python3 -m tools.mock_uart_generator --inject-errors &
# 印出的虛擬序列埠路徑，例如 /dev/pts/3，拿去給 uart_receiver 測試
python3 -m drivers.uart_receiver --port /dev/pts/3
```

要收集訓練資料時打開 CSV logger：

```bash
python3 -m drivers.uart_receiver --csv-log
# 或在 config.json 把 csv_logger.enabled 設成 true
```

### 條碼掃描器

```bash
sudo venv/bin/python3 -m drivers.barcode_scanner --list      # 先找出實際裝置名稱
sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint <上面看到的名稱關鍵字>
```

實測這台 Pi 5 上的裝置名稱是 `USBKey Chip USBKey Module`（`/dev/input/event4`），
已寫入 `config.json` 的 `barcode_scanner.device_name_hint = "USBKey"`。換一台掃描器
或換一台 Pi 部署時要重新用 `--list` 確認一次，名稱不一定叫這個。

沒有實體掃描器時（需要 root 或 uinput 權限）：

```bash
sudo venv/bin/python3 -m tools.mock_barcode_input 4710018001234
```

**踩過的坑（都已經修好，這裡記錄原因供之後參考）：**

1. **`evdev.InputDevice` 沒有 `set_nonblocking()` 這個方法**——`python-evdev`
   開啟裝置時就已經用 `O_NONBLOCK` 開檔案描述符，不需要（也沒有）這個方法可呼叫。
   原本的程式碼多寫了一行不存在的方法呼叫，已拿掉。
2. **`device.grab()` 會讓某些掃描器的 Caps Lock LED 交握卡死**：這台掃描器送出
   真正的條碼字元前，會先按一次 Caps Lock，用意是「詢問」目前作業系統回報的
   大小寫 LED 狀態，並期待作業系統把 LED 狀態回應（echo）回裝置才會繼續送資料。
   這個 echo 正常由 Linux 核心自己的 `kbd`/`leds` handler 處理，但我們
   `grab()` 把裝置獨佔給自己之後，核心那個 handler 就再也收不到事件，echo
   斷掉，裝置會誤以為沒人回應、不斷重試（症狀：一刷條碼就開始每隔 ~50ms
   狂閃 Caps Lock，直到程式 `stop()`／`ungrab()` 才瞬間把資料送出）。修法是
   在 `_process_key()` 裡偵測到 `KEY_CAPSLOCK` 按下時，自己呼叫
   `device.set_led(ecodes.LED_CAPSL, ...)` 模擬核心原本會做的 LED echo。
3. **大小寫判斷不能只看 Shift**：這台掃描器實測是拿 Caps Lock 開關本身當作
   大小寫切換（不是每次都送 Shift），所以字母大小寫判斷要同時考慮 Shift 和
   目前追蹤到的 Caps Lock 狀態（標準鍵盤語意：兩者是 XOR 關係，同時開會互相
   抵銷）。
4. **鍵碼不固定**：同一台掃描器有時候用小鍵盤鍵碼（`KEY_KP0`~`KEY_KP9`）送
   數字，有時候又用標準數字鍵（`KEY_0`~`KEY_9`），對照表兩種都要涵蓋，不能
   只認一種。

如果之後換了不同型號的掃描器又遇到「掃了沒反應」，用 `--debug` 旗標看原始
事件（含 `EV_MSC`/`MSC_SCAN` 這個翻譯前的原始 HID scancode，比 `EV_KEY` 翻譯
後的鍵碼更可靠），不要用猜的加鍵碼對照：

```bash
sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint "USBKey" --debug
```

### 相機

```bash
# 驗證色彩通道順序對不對（拍一張存成 color_test.png，肉眼比對，不用猜的）
python3 -m drivers.camera_stream --color-test

# 用棋盤格校正（9x6 內角點、方格邊長實測約 24mm）
python3 -m drivers.camera_stream --calibrate --num-images 15 --square-size-mm 24

# 測試擷取
python3 -m drivers.camera_stream --preview
```

**已完成校正**：`vision/camera_calibration.npz` 已產生，reprojection error =
**0.2026**（遠低於 1.0 的可接受門檻）。`CameraStream` 建構子帶
`calibration_file="vision/camera_calibration.npz"` 就會自動載入、對每一影格
做即時 `cv2.undistort()`。

**踩過的坑：**

1. **picamera2 的 stream 設定路徑會影響實際輸出**：一開始用
   `create_video_configuration()`，畫面顏色不對（藍色的東西顯示成橘紅色）；
   改用 `preview_configuration`（`picam2.preview_configuration.main.format = "RGB888"`
   + `picam2.preview_configuration.align()` + `picam2.configure("preview")`）之後，
   用 `--color-test` 拍照肉眼比對，顏色是正常的。目前 `camera_stream.py` 全部
   統一用 `preview_configuration` 這條路徑。如果之後又遇到顏色不對，先用
   `--color-test` 拍照肉眼驗證，不要用猜的加減轉換——`cv2.imwrite()`/`cv2.imshow()`
   都預期輸入是 BGR，存出來的圖顏色不對就代表資料其實是 RGB，需要在用到影格的
   地方統一加一次 `cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)`。
2. **校正預覽視窗需要能開視窗的畫面環境**：`--calibrate` 預設會開一個顯示
   即時角點偵測結果的預覽視窗（`cv2.imshow`），本機接螢幕操作可以直接用；
   純文字 SSH（沒有 `-X`/`-Y`）開不了視窗，會自動印警告並退回無預覽模式繼續
   跑，不會讓整個校正流程失敗；也可以主動加 `--no-window` 跳過視窗。
3. **終端機印出 `qt.qpa.plugin: Could not find the Qt platform plugin "wayland"`
   跟一堆字型警告是無害的**——Qt 自動退回其他可用的顯示後端，不影響校正流程
   （已實測 15/15 張全部成功擷取）。
4. **相機目前是倒吊測試的，不影響這次校正**：因為零件還沒定位、目前測試環境
   相機是倒掛安裝。`camera_matrix`/`dist_coeffs` 這些內參是鏡頭本身的光學特性，
   跟安裝角度無關，`findChessboardCorners` 也不在意畫面方向，所以現在校正的
   結果之後零件定位好可以直接沿用。等正式安裝角度確定後，如果應用需要正的
   畫面，再用 `cv2.rotate()` 處理方向，不需要重新校正。

### 資料庫

```bash
python3 -m database.db_manager --init    # 建表 + 寫入測試商品(products)與測試會員(members)
python3 -m database.db_manager --list
```

### Phase 4：購物流程狀態機（`core/cart_state_machine.py`、`core/cart_manager.py`）

涵蓋的完整流程：**登入帳號 -> 進入管制區 -> 購物（掃碼加入/移除+秤重比對）
-> 鎖定結帳 -> 付款 -> 走出管制區 -> 登出帳號**，這條路上每一步可能遇到的
問題都設計了對應的處理方式——見下面「狀態與警告一覽」。這支是「底層架構的
支持」（Phase 4 的定位，見「前身專題功能取捨」章節），只管流程本身正不正
確，不含 LLM 推薦、預算分析這類進階功能（那些排 Phase 6）。

跟目前硬體現況的對應（決定了這支怎麼設計）：

- **登入**：目前硬體只有 USB 條碼掃描器，所以是「掃會員條碼/QR code」
  （不是密碼、不是 RFID），會員代碼查 `database/db_manager.py` 新增的
  `members` 表。條碼格式用 `config.json` 的
  `state_machine.login_barcode_prefix`（預設 `"MEMBER-"`）跟商品條碼區分，
  避免誤掃。
- **進入/走出管制區**：確定的方向是 BLE Beacon 雙標籤差分（見下面
  `core/landmark_correction.py` 章節），不經過已經滿載的 MCU、直接用 Pi5
  內建藍牙，成本接近零。狀態機這邊先假設事件會以 `GateEntryDetected`/
  `GateExitDetected` 的形式餵進來；真正把 `GateCrossingDetector` 判定出的
  `entering`/`exiting` 組成這兩個事件、串接真實 BLE 掃描（`bleak`）的
  `drivers/ble_beacon_scanner.py`，還沒寫，狀態機邏輯本身不用因此改。
- **鎖定結帳**：純軟體狀態鎖（`LOCKED_FOR_CHECKOUT`），沒有實體鎖車機構，
  UI 進到這個狀態要自己擋掉繼續增減商品的操作。
- **同一會員是否已經在別台購物車登入**：單機沒辦法知道其他購物車狀態，
  這需要中央伺服器/共用資料庫才能做，目前架構沒有這塊，暫不處理跨購物車
  重複登入偵測（見 `database/db_manager.py` 開頭的說明）。

**狀態一覽**：`UNAUTHENTICATED`（未登入）→ `LOGGED_IN_OUTSIDE_ZONE`（已登入
未進管制區）→ `SHOPPING`（購物中）↔ `AWAITING_WEIGHT_INCREASE`/
`AWAITING_WEIGHT_DECREASE`（掃碼後等秤重比對）↔ `WEIGHT_MISMATCH_ERROR`
（秤重異常，需要重試或放棄該筆）→ `LOCKED_FOR_CHECKOUT`（鎖定結帳）→
`AWAITING_EXIT`（付款完成，等走出管制區）→ `SESSION_CLOSED`（已結束，可登
出回到 `UNAUTHENTICATED`）。

**秤重比對邏輯**：掃碼當下記錄目前重量當基準值，之後每筆新秤重讀數跟基準
值的差，拿去跟這個商品在 `database` 登記的 `standard_weight_g`（加入為
正、移除為負）±`weight_tolerance_g` 比對——在容差內視為成功；方向相反且
超出容差直接判定「方向不符」（可能有其他商品同時被拿動）；方向對但超出容
差上限判定「重量不符」（可能拿了不只一件）；方向對且還沒超標則視為還在放
/拿的過程中，繼續等到成功或等到 `weight.weight_match_timeout_sec` 逾時。

**遇到問題的處理方式**：分兩種——(a) 軟體層能擋下來/引導重試的（條碼查無
資料、秤重沒對上、逾時、感測器斷線……），狀態機會擋下錯誤動作、發出對應
的 `CartAlert`（含 `severity`：`info`/`warning`/`critical`），並提供
`RetryWeightCheckRequested`（重試）/`VoidPendingItemRequested`（放棄這筆）
的路徑；(b) 沒辦法只靠軟體解決、代表可能有防損疑慮的（例如結帳前就走出管
制區、強制登出時清單裡還有商品），狀態機**不會**假裝沒事發生、自己把狀態
轉成正常結束，只會發出最高等級（`critical`）的警告，交給上層（UI、警報硬
體、店員）處理——這是軟體邊界，狀態機本身沒辦法真的攔住一個人。

完整的警告代碼列在 `core/cart_state_machine.py` 開頭的 `ALERT_*` 常數，每個
代碼對應流程裡一個具體的問題點（例如 `zone_entry_without_login`、
`exit_without_checkout`、`weight_direction_mismatch`……），設計成之後
Phase 6 的 `anomaly_detector.py` 可以直接依代碼分類統計，不用解析訊息文字。

**互動模擬（不需要真實硬體）**：

```bash
python3 -m core.cart_state_machine --simulate
```

用數字選單手動觸發每一種事件（登入、進管制區、掃碼、回報秤重、快轉時間
檢查逾時、鎖定、付款、出管制區、登出、強制登出、感測器斷線/恢復），每次
操作後印出目前狀態、購物清單、總價、最近警告——測試資料用
`--db` 預設的 `database/inventory.db`（含測試商品與測試會員 `MEMBER-0001`/
`MEMBER-0002`）。已經照這個流程寫過完整的邏輯測試（正常全流程 + 17 種錯
誤情境：查無會員、未登入闖入、查無商品、秤重逾時/方向不符/超出容差、移除
不存在的商品、鎖定中掃碼、結帳前走出管制區、登出前未結案、強制登出殘留商
品、感測器斷線恢復、鎖定中秤重比對未完成……），全部通過。

**尚未接上真實硬體整合**：目前只有 `--simulate` 互動模擬，真正把
`barcode_scanner.py`、`uart_receiver.py` 換算出的重量、之後的
`gate_sensor.py` 產生的事件即時餵進 `CartStateMachine.process_event()`（背
景執行緒 + 主迴圈整合），這件事留給 Phase 5 的 `main.py` 做，因為那牽涉到
多個 driver 執行緒協調，跟這支狀態機本身的邏輯正確性是分開的兩件事。

### Phase 3：定位系統的最終輸出格式（`core/position_types.py`）

因為裝置暫時沒電、購物車也還沒組裝完成（延伸線材下週才會到），沒辦法繼續做
需要實機測試的中間步驟（`vanishing_point.py` 視覺校正、`floor_optical_flow.py`
視覺備援），所以先把 Phase 3「最終要交給 UI/AI/商業邏輯的東西長什麼樣子」這件
不需要硬體的事確定下來，讓 Phase 4/5/6 可以先針對這個穩定格式開發，不用等中間
那兩個模組做完。

`PositionEstimate` 這個 dataclass 就是這個「輸出契約」，欄位不會因為底層是
純 UART 算的、還是之後接了視覺融合/備援而改變：

```python
@dataclass
class PositionEstimate:
    x_mm: float                    # 全域座標系位置，公釐
    y_mm: float
    yaw_deg: float                 # 全域座標系航向角，度，逆時針為正
    position_source: str           # "optical_flow" 或（之後）"floor_optical_flow_fallback"
    yaw_source: str                # "imu" 或（之後）"imu+vision"
    sample_count: int              # 累計處理過的樣本數（除錯用）
    skipped_low_confidence_count: int  # 累計因信心值太低而跳過的樣本數（除錯用）
    timestamp: Optional[float]     # 最後一次更新的時間戳記
```

設計上確認過的兩個決定：

1. **座標系原點**：程式啟動（或呼叫 `reset()`）當下車子的位置與朝向，不是
   店面地圖上的固定座標。畫的是「這台車今天這趟的軌跡」，不是對應賣場實體
   地圖座標——如果之後要對齊店面地圖，需要另外做開機時的初始定位機制（例如
   掃描入口的固定標記），目前沒有這個機制，先不做。
2. **信心值/來源欄位**：`position_source`、`yaw_source` 這兩個字串欄位，加上
   `is_best_effort_estimate()` 方法，讓下游（UI 可以顯示「定位精準度較低」的
   提示、AI 的 prompt 可以附註目前定位可信度）不用自己去猜測底層是哪個模組
   算出來的。現階段因為視覺融合還沒做，`is_best_effort_estimate()` 會一直
   回傳 `True`，這是如實反映現況，不是 bug。

`core/odometry_engine.py` 是目前唯一一個會產生 `PositionEstimate` 的來源，
往後 `floor_optical_flow.py`、視覺融合邏輯接上時，改的是這兩個來源欄位的
「值」，欄位「形狀」不會變——這樣 Phase 4/5/6 現在就可以直接針對
`core/position_types.py` 開發，等真正的視覺模組做出來直接接上，UI/AI 端的
程式碼完全不用改。

### Phase 3：基礎里程計（`core/odometry_engine.py`）

按開發總表的順序要求，先做「純 UART 版本」並實際走一段固定距離驗證誤差量級，
確認邏輯沒問題之後才進行下一步的視覺融合（`vanishing_point.py`，還沒開始寫）。

作法：拿 `UartReceiver` 收到的 PMW3901 `dx`/`dy`（乘上 `optical_flow_px_to_mm`
換算成公釐）跟 BNO080 的 `yaw_deg`，套標準 2D 旋轉矩陣把「車身局部座標系」的
位移轉成「全域座標系」的位移再累加，得到全域 `(X, Y)`。新增的 `squal` 欄位
也用上了——信心值低於門檻的那一筆位移不計入累積位置（但 yaw 還是照樣更新，
因為 yaw 是 BNO080 給的，跟光流追蹤品質無關）。這支引擎目前回報的
`position_source`/`yaw_source` 永遠是 `"optical_flow"`/`"imu"`（見上一節），
因為還沒有備援/視覺融合可以切換。

```bash
python3 -m core.odometry_engine                    # 用 config.json 的 serial/odometry 設定即時監看
python3 -m core.odometry_engine --min-squal 30      # 過濾掉信心值低於 30 的光流樣本
python3 -m core.odometry_engine --px-to-mm 1.42     # 手動覆蓋 px_to_mm（還沒校正時暫時測試用）
```

執行時會每收到約 20 筆樣本（預設，`--print-every` 可調）印一次目前的
`X`/`Y`/距原點距離/`yaw`/樣本數/已跳過的低信心筆數/`position_source`與
`yaw_source`，方便你「歸零 → 推一段量好的距離 → 比對印出的距離跟捲尺量到的
差多少」這種驗證方式。因為 `optical_flow_px_to_mm` 目前還是 `config.json`
裡的 `CALIBRATE_ME` 佔位值（`1.0`），程式啟動時會印警告——這代表現在算出來
的 (X, Y) 只能看趨勢（有沒有往對的方向走、旋轉有沒有轉對），還不是真實的
公釐數，等 `calibrate_optical_flow.py` 正式校正過（機構定案後）數字才會準。

核心邏輯（`rotate_local_to_global()` 的旋轉矩陣、`process_packet()` 的位置
累積、squal 過濾、`reset()`、`PositionEstimate` 的 `is_best_effort_estimate()`）
都用假資料驗證過，包含：朝不同方向移動時全域座標的正負號對不對、轉向後再
移動的方向有沒有跟著轉對、低信心封包確實不影響位置但仍更新 yaw、`reset()`
正確歸零位置並保留 yaw 與來源欄位、`distance_from_origin_mm()` 的畢氏定理
結果、背景執行緒版本（`start()`/`stop()`）搭配真的 `queue.Queue` 生產者的
整合測試，以及模擬「之後接上視覺融合/備援」時 `position_source`/`yaw_source`
換成不同值，確認 `is_best_effort_estimate()` 會正確跟著反應（提前驗證這個
輸出契約設計本身合理，不用等真的做出視覺模組才知道好不好用）。另外也做了
完整鏈路的端對端測試：`mock_uart_generator.py` 送假封包 → 真的
`UartReceiver` 解析 → 真的 `OdometryEngine` 累積位置，全部串起來能正常運作。

還沒做、還沒驗證的部分：這支還沒有在真實硬體上實際推車測試過（邏輯用假資料
驗證，硬體本身接著、隨時可以跑，只是我還沒有實機數據可以核對），也還沒有
視覺校正（yaw 目前完全信任 IMU，`config.json` 的 `vision_yaw_fusion_weight`
還是 0，等 `vanishing_point.py` 做出來才會啟用）。

### Phase 3/7：BLE 地標校正（`core/landmark_correction.py`）——同時解決門口偵測與地圖對齊

背景：跟你討論另一份 Gemini 對話紀錄（賣場防盜門與標籤技術解析）後定案的
方向，用低成本 BLE Beacon 同時解決兩個原本分開卡住的問題——Phase 4
`GateEntryDetected`/`GateExitDetected` 沒有硬體來源、Phase 7 室內導航卡在
「浮動座標系怎麼對齊店面地圖」沒有機制。兩個問題本質上是同一種問題：都需
要「已知絕對座標的地標點」讓車子經過時把座標釘回去，只是門口多需要一個
「方向」。

**關鍵設計原則**（跟討論紀錄的結論一致）：不要用 RSSI 算連續座標——室內多
路徑反射環境下 RSSI 抖動可達 ±10~15 dBm，換算距離誤差 2~4 公尺，比沒校正
的 IMU 漂移還糟。改成把訊號峰值當「離散觸發點」，車子經過已知座標的
Beacon 附近時，觸發一次校正，把 dead-reckoning 累積誤差拉回去——兩次校正
之間的位置完全還是靠 `core/odometry_engine.py` 的積分撐著，這不是取代主要
定位來源，是跟 squal 過濾、（還沒寫的）`vanishing_point.py` 同一種「離散/
局部修正」的設計哲學。

**兩種偵測器**：

- `RssiPeakDetector`——給一般地標點用（走道轉角、貨架節點……Phase 7 之後
  要擴充），只需要知道「經過了」，不需要方向。持續餵平滑後的 RSSI，偵測
  訊號「從上升轉為下降」的瞬間（局部極大值），觸發一次校正，同一個峰值只
  觸發一次（`min_rise_dbm` 門檻避免雜訊誤判）。
- `GateCrossingDetector`——管制區門口專用，需要方向（決定要不要觸發防損
  警報，信心要求比一般地標點高）。門內門外各放一顆 Beacon（約 1.5~2 公
  尺），比較兩者訊號的「相對大小」而不是絕對值（可以抵消車體金屬造成的
  固定衰減），哪邊持續佔優勢翻轉到另一邊就是一次穿越，回傳 `entering`／
  `exiting`。`hysteresis_dbm` 避免兩者訊號接近時（例如車子剛好停在門口正
  中間）反覆誤判方向。

兩者都靠 `smooth_rssi()`（移動中位數平滑，消除單筆突波）打底。

**修正過一個設計漏洞**（你發現的，記在這裡避免以後又犯）：第一版只看「訊
號有沒有先升後降」，這抓不住「車子只是在 Beacon 附近晃過、根本沒有真的靠
近/穿越」的情況——晃近一點點一樣會有升降曲線，形狀上跟真的經過沒有差別。
修正方式是加一道**絕對訊號強度門檻**，不只看「有沒有相對上升」，還要求峰
值本身（或門口判定當下佔優勢那邊的訊號）夠強，代表車子當下真的夠靠近：

- `RssiPeakDetector` 加了 `min_peak_rssi_dbm`：峰值沒有強到這個門檻，就當
  作沒發生，不觸發校正。
- `GateCrossingDetector` 加了 `min_crossing_rssi_dbm`（同樣邏輯，門口這裡
  代價更高所以更重要）跟 `min_confirm_samples`（要求新的優勢方連續出現
  N 筆才算數，過濾單一雜訊樣本造成的瞬間翻轉，門口預設調到 3）。
- 新增 `is_heading_consistent()` 工具函式——這是討論紀錄裡 Gemini 自己也
  強調的「關鍵」步驟，第一版漏掉了：BLE 訊號本身沒有方向性，量不到「有沒
  有真的通過那個實體開口」，所以門口的穿越判定除了 BLE 差分之外，理論上
  還需要拿 IMU 的航向角做交叉驗證（車子朝向要跟「真的在穿越門口」該有的
  朝向大致一致）。這個函式本身寫好測過了，但「把 BLE 判定跟 IMU 朝向兩個
  獨立訊號 AND 起來才採信」這個組合邏輯，要等接上 `main.py`（Phase 5）整
  合真實資料流時才能真正發揮作用——`GateCrossingDetector` 本身只管 BLE 這
  一半，這是刻意的分工，不是漏做。

**誠實講這個限制到底能不能完全解決**：這三層防護（絕對門檻、連續樣本、朝
向交叉驗證）能大幅降低「晃過但沒真的穿越」的誤判機率，但沒辦法做到理論上
的零誤判——BLE RSSI 本身是全向、無方向性的訊號強弱量測，物理上量不到「有
沒有真的通過那個實體開口」，這跟光電閘門、地埋迴路線這種「物理上一定要真
的通過某個窄通道才會觸發」的機制比起來，先天就有模糊地帶。額外能做、但這
支程式碼本身做不到的事：**部署時把 Beacon 發射功率調低**，讓兩顆門口
Beacon 的有效偵測範圍實際侷限在門口通道附近，店裡其他地方訊號弱到連
`hysteresis_dbm` 都過不了——軟體門檻要跟這個實體佈署互相配合，單靠其中
一邊都不夠穩。

**跟 `core/odometry_engine.py` 的整合**：新增 `apply_landmark_correction(x_mm, y_mm, timestamp, landmark_id, blend_weight=1.0)` 方法，收到校正事件時把累積座標拉向地標的已知座標——`blend_weight=1.0`（預設）是直接強制重置，對應討論紀錄的建議；小於 1.0 是加權融合，留給以後需要更平滑校正時調整，目前不需要。`core/position_types.py` 的 `PositionEstimate` 也加了 `last_landmark_id`/`last_landmark_correction_at` 兩個欄位記錄最近一次校正，純除錯/UI 顯示用，不影響定位邏輯本身。

**地標座標設定**：`config.json` 新增 `landmarks` 區塊——`points`（每個 Beacon
的 `beacon_id`/絕對座標/`label`，目前是 `CALIBRATE_ME` 佔位資料，等安裝位
置定案實測填入）、`gate_beacon_pair`（門口那組的內外側 Beacon ID）、平滑/
偵測參數（`rssi_smoothing_window`、`peak_min_rise_dbm`、`peak_min_rssi_dbm`、
`gate_hysteresis_dbm`、`gate_min_crossing_rssi_dbm`、`gate_min_confirm_samples`、
`gate_exit_yaw_deg`、`gate_heading_tolerance_deg`）。之後 Phase 7 要擴充走
道地標點，就是往 `points` 陣列繼續加。

**互動模擬（不需要真實硬體）**：

```bash
python3 -m core.landmark_correction --simulate peak   # 模擬推車經過一般地標點
python3 -m core.landmark_correction --simulate gate    # 模擬推車通過門口（輸入 A、B 兩顆 Beacon 的 RSSI）
```

邏輯測試涵蓋：平滑函式的中位數計算、單一峰值正確偵測且只觸發一次、小幅雜
訊不誤判成峰值、兩次獨立經過偵測成兩個峰值、門口進/出兩種方向都正確判
定、遲滯門檻確實避免訊號接近時反覆誤判、`apply_landmark_correction()` 的
強制重置與加權融合都正確、`blend_weight` 超出範圍正確擋下——以及這次補強
的：**只是晃近（峰值不夠強）不會誤觸發地標校正、晃近但沒真的到門口附近不
會誤觸發門口穿越、單一雜訊樣本的瞬間翻轉不會被 `min_confirm_samples` 誤
判成穿越、加了絕對門檻後真正靠近的情況仍然正常觸發（沒有把真的訊號也一起
擋掉）、`is_heading_consistent()` 的角度容許範圍與 ±180 度邊界換算都正
確**，全部通過；也用 CLI 手動模擬過完整流程。

**還沒做的部分**：這裡全部是純邏輯，真正掃描 BLE 廣播、把原始 RSSI 讀數餵
進來的 `drivers/ble_beacon_scanner.py`（背景執行緒 + `bleak` 套件）還沒
寫——這個環境沒有 BLE 硬體/Beacon 可以測，等邏輯定案（現在）、真的有
Beacon 可以測之後再做。`GateCrossingDetector` 判定出 `entering`/`exiting`
之後，要怎麼組成 `GateEntryDetected`/`GateExitDetected` 事件餵進
`core/cart_state_machine.py`，這個串接也留給 Phase 5 的 `main.py`（原因跟
`gate_sensor.py` 一樣：牽涉多執行緒整合，跟這支邏輯本身的正確性是分開的
兩件事）。Beacon 的絕對座標也還是佔位值，等實際安裝位置量測後才準確。

### 校正工具（`odometry.optical_flow_px_to_mm`、`weight.hx711_offset`/`hx711_scale`）

這兩個是 `config.json` 裡剩下標記 `CALIBRATE_ME` 的數值，跟秤重機構、光流感測器
安裝方式有關，沒辦法用理論公式算出來，需要在硬體上實測。硬體（HX711、PMW3901、
UART）本身都是接好、可以跑的，**只是購物車的機構（秤台安裝方式、PMW3901 離地
高度）還在調整中**——現在跑出來的 offset/scale/px_to_mm 只對「當下這個安裝方式」
準，機構之後再調整，這些值很可能就要重新校正一次，不算是最終定案的數字。所以
現在可以照下面指令實際跑跑看（建議先用 `--dry-run` 只看數字、不寫回
`config.json`），等機構真正定案後，再正式跑一次把結果寫回 `config.json`。

**重量校正 `tools/calibrate_weight.py`**：

```bash
python3 -m tools.calibrate_weight              # 用 config.json 的 serial 設定
python3 -m tools.calibrate_weight --dry-run     # 只算數值印出來，不寫回 config.json
python3 -m tools.calibrate_weight --samples 50 --timeout 10
```

流程：等 UART 有資料流進來 → 提示「秤台淨空」→ 收集一批 `hx711_raw` 樣本算平均
當 `offset` → 輸入已知砝碼重量（公克）→ 提示「放上砝碼」→ 再收集一批樣本算
`scale = (加砝碼平均值 - offset) / 已知砝碼重量`。算完會問要不要寫回
`config.json`（`y` 才寫，其餘任何輸入都不動檔案），寫回時只會更新 `weight`
區塊，其他欄位與註解不受影響。

**光流校正 `tools/calibrate_optical_flow.py`**（v2，流程改過一輪，見下方說明）：

```bash
python3 -m tools.calibrate_optical_flow
python3 -m tools.calibrate_optical_flow --dry-run
python3 -m tools.calibrate_optical_flow --countdown 5      # 開始記錄前的大倒數秒數
python3 -m tools.calibrate_optical_flow --no-filter        # 關掉離群值過濾
```

流程（v2）：等 UART 有資料流進來 → 按 Enter 開始一個全螢幕大字體倒數（預設
5 秒，純視覺提示，讓你有時間準備）→ 倒數結束就開始記錄，**沒有固定時間窗**，
你按自己的步調把車推過去，推完再按一次 Enter 停止記錄 → 這時候才輸入這段
實際推了多遠（公分，先量好或推完才量都可以）→ 算
`px_to_mm = 實際距離(mm) / sqrt(sum_dx² + sum_dy²)`。同樣算完會問要不要寫回
`config.json`（只更新 `odometry` 區塊）。

> **v1 → v2 的改動原因**：v1 是「倒數 + 固定時間窗自動收集」，實測發現人手
> 沒辦法剛好在固定秒數內推完固定距離，不是太快就是太慢，體驗很差。v2 拿掉
> 了固定時間窗，只留一個純視覺的大倒數當「準備」緩衝，記錄的起訖完全由你
> 自己按 Enter 控制，多久都行，推完再回頭量/報距離就好。
>
> **積分方式**：每一筆封包的 `dx`/`dy` 本身就是「這個取樣週期的相對位移」
> （不是累積值），所以直接把記錄期間收到的所有封包的 `dx`/`dy` 加總，就是
> 離散版的積分（黎曼和），不需要再乘取樣間隔或做其他處理。
>
> **離群值過濾**：預設會用中位數絕對偏差（MAD）過濾掉位移量明顯異常大的
> 樣本再加總——PMW3901 追蹤不穩、光線不足、離地高度不對時偶爾會吐出離譜的
> 單筆數值，這種尖峰不會像雜訊一樣互相抵銷，會直接偏移整段加總，所以有濾掉
> 的必要；樣本數太少（<5）或彼此差異太小（沒有明顯離群值可判斷）時就不濾，
> 避免正常推車動作被誤判。不想濾可以加 `--no-filter`，想調嚴格程度可以用
> `--mad-multiplier`（預設 6.0，越小濾得越兇）。

兩支工具的核心邏輯（樣本收集、平均值、offset/scale/px_to_mm 計算、離群值
過濾、config.json 讀寫）都已經用假資料（合成的 `UartPacket`、預先塞好的
`queue.Queue`、模擬按鍵觸發的執行緒）驗證過，包含正常情況、逾時只收到部分
樣本、除以零、離群值濾除等邊界情況。硬體是接著的，隨時都能實際跑一次看
流程與計算對不對；只是機構還沒定案前跑出來的值只能當「流程試跑」用，不
建議直接寫回 `config.json` 當正式值——等機構定案後再跑一次正式的。

**驗證工具 `tools/verify_weight.py` / `tools/verify_optical_flow.py`**：

校正跟驗證是兩件事——`calibrate_*.py` 是「算出新值寫回 config.json」，
`verify_*.py` 是「config.json 裡現有的值到底準不準」，只會讀取、不會修改
`config.json`，適合校正完之後，或懷疑機構鬆動、感測器飄移時拿來抽查。

```bash
python3 -m tools.verify_weight              # 用 config.json 現有的 hx711_offset/scale
python3 -m tools.verify_weight --samples 30 --timeout 10

python3 -m tools.verify_optical_flow         # 用 config.json 現有的 optical_flow_px_to_mm
python3 -m tools.verify_optical_flow --countdown 5
```

`verify_weight.py`：秤台放上一個你知道實際重量的物品 → 收集樣本、用現有
offset/scale 換算成公克 → 輸入實際重量（可留空跳過）→ 印出誤差百分比。
可以連續測多個物品，`q` 結束。

`verify_optical_flow.py`：跟校正工具一樣的「大倒數 → 自己步調推車 → Enter
停止」流程（沒有固定時間窗），差別是拿現有的 px_to_mm 把累積像素位移換算回
推測距離 → 輸入實際距離（可留空跳過）→ 印出誤差百分比。可以連續測多段距離，
`q` 結束。

兩支驗證工具的換算與誤差計算邏輯也都用假資料驗證過。

## 這次開發過程中做的決定（依你的回覆）

1. **Mock 開發路線**：保留，放在 `tools/`，當 CI/無硬體測試輔助工具，不擋在正式流程前面。
2. **CSV logger**：加進 `uart_receiver.py`，預設關閉（`config.json` 的
   `csv_logger.enabled`），要收集 Phase 6 訓練資料時再開。
3. **本次範圍**：只做 Phase 1 + Phase 2，Phase 3（定位融合）起下次再繼續。

## UART 封包格式 v2（2026-09-01，新增 squal 欄位）

MCU 端封包格式改版，新增 `squal`（PMW3901 SQUAL，0-255，這筆 `dx`/`dy` 的
追蹤信心值），欄位數從 7 個變成 8 個：

```
$SDK,<dx>,<dy>,<squal>,<yaw_deg>,<pitch_deg>,<roll_deg>,<hx711_raw>*<CS>\r\n
```

**跟舊版不相容**：舊版（7 欄位、沒有 squal）接收端收到新版封包，或新版接收端
收到舊版封包，都會被判定成「欄位數量不符」直接整包丟棄，不是靜默解析錯誤——
如果封包每包都被丟、log 一直印「欄位數量或 ID 不符」，先確認 Pi 端與 MCU 端
的封包格式版本是不是對不起來。

已經跟著改版的地方（都已用假資料/mock 驗證過，含端對端 mock generator ->
真的 UartReceiver 的整合測試）：

- `drivers/uart_receiver.py`：`UartPacket` 加了 `squal` 欄位、`parse_packet()`
  改成驗證 8 欄位、CSV logger 表頭與 `to_csv_row()` 都加了 `squal`、獨立測試
  模式（`python3 -m drivers.uart_receiver`）印出的內容也加了 `squal`。
- `tools/mock_uart_generator.py`：`build_packet()` 與 `_run_loop()` 都改成送
  8 欄位封包，`squal` 用隨機值模擬正常追蹤信心（60~255）。
- `config.json`：`uart_protocol.field_order` 加了 `squal`。
- `tools/calibrate_weight.py`、`tools/calibrate_optical_flow.py`、
  `tools/verify_weight.py`、`tools/verify_optical_flow.py`：這幾支都是透過
  `packet.dx`／`packet.hx711_raw` 等屬性存取，不是照欄位順序取值，所以
  `UartPacket` 多一個欄位不會讓它們壞掉，邏輯測試也重新跑過確認沒問題。

**還沒做，但值得考慮的後續**：光流校正工具目前的離群值過濾用的是統計方法
（MAD，見上面「校正工具」小節），這是因為原本封包沒有信心值可以參考，只能
事後用統計猜。現在有了 `squal`，理論上可以直接用「這筆信心值太低就丟掉」
取代/輔助統計濾波，會比較準——不過這牽涉到要不要改校正工具的過濾邏輯，
我還沒動，等你想清楚要不要採用再說。

## 前身專題（大四專題.pdf）功能取捨與 Phase 7 定位

專案前身是另一份大四專題提案（YOLOv8 + 雙鏡頭 + Hailo-8L NPU 的視覺辨識路線）。
現在的 SmartCart_Pi5 已經在底層方向上跟那份提案分道揚鑣了——原因是：需要重新
蒐集/訓練視覺模型的成本、開發時程難以配合、YOLOv8 在 Pi5 上可能跑不動。所以
定位（改用 PMW3901 光流 dead-reckoning，見 `core/odometry_engine.py`）和防損
驗證（改用「掃碼 + 秤重比對」而非視覺辨識）的底層結構都已經是全新設計，不是
沿用前身文件的做法。但前身文件第一頁列的四個前端旗艦功能——**室內導航**、
**錢包管家**、**健康護照**、**即時優惠**——使用者希望盡量保留，這幾個功能在
現在的 `SmartCart_Pi5_開發總表_v2.docx`（Phase 1~6）裡完全沒有出現，是額外要
排進來的範圍，目前討論結果：

- **錢包管家 / 健康護照 / 即時優惠**：這三個不依賴視覺或定位，可以獨立於
  Phase 3（定位）的進度往前做，但範圍/資料表結構/UI 呈現方式的細節還沒討論定
  案，先不動工。**這三個明確不放進 Phase 4**——使用者的定位是：Phase 4 只做
  「底層架構的支持」，也就是開發總表原定的 `state_machine.py`（狀態機：
  IDLE/SCANNED_WAIT_WEIGHT/ERROR_WEIGHT_MISMATCH 等）跟 `cart_manager.py`
  （購物清單/總價這種基礎資料結構與存取，不含進階邏輯），是掃碼、秤重比對這
  類「保障購物流程正確性」的基礎設施。錢包管家/健康護照/即時優惠則是**建立在
  Phase 4 這層基礎設施之上、要融合 LLM 的進階功能**——例如 LLM 推薦後一鍵把
  商品加入購物清單、根據目前購物清單做預算分析與建議，這類「推薦 + 決策輔助」
  的邏輯天生就跟 Phase 6 的 `llm_agent.py`（Context Builder：組裝 (X,Y) +
  購物清單餵給 LLM，回傳結構化 JSON 給 UI 用）更接近，所以目前傾向排進
  **Phase 6**（或 Phase 6 之後另開一個小 Phase），而不是 Phase 4。等這三個功
  能的範圍/資料表/UI 細節聊清楚後再正式排定。
- **室內導航（含大地圖設計建置）**：正式列為 **Phase 7**，開發本身仍然擱置
  （硬體還沒組裝），但原本卡住的「浮動座標系怎麼跟店面地圖對齊」這個問題，
  現在有具體的候選機制了——`core/landmark_correction.py`（BLE Beacon 地標
  校正，見上面對應章節）：每個 Beacon 有寫死的絕對座標，車子經過時把
  dead-reckoning 的累積座標校正/融合回那個座標，等於是分散在店裡多個點的
  「初始定位」機制，而不只是開機時一次性對齊。這個機制的軟體邏輯已經做完
  並測試過，但還沒有真的 BLE 硬體/Beacon 可以實測，也還沒有店面地圖本身
  （貨架圖節點、A* 尋路）的設計——要真正啟動 Phase 7，除了地標校正機制本
  身要實機驗證過，還需要另外設計店面地圖的資料結構跟尋路演算法，這些都還
  沒開始。

跟前身文件另外幾個路線差異，記錄在這裡備查：前身用 YOLOv8 雙鏡頭視覺辨識做
「先視覺確認、後重量驗證」的防損邏輯，現在用「掃碼 + HX711 秤重比對」取代
（不需要訓練/佈署視覺辨識模型）；前身的室內導航用 A* 路徑規劃 + 貨架格狀障礙
物地圖，這部分如果 Phase 7 真的要做，會是全新設計，不會直接沿用前身的路徑規
劃演算法本身（因為地圖對齊機制不同了，但 A* 這類演算法本身之後仍可能重用）；
前身的 mmWave 分層喚醒、太陽能輔助充電、YOLOv8+Hailo-8L NPU 硬體等，目前的開
發總表裡沒有對應項目，暫不在範圍內，之後如果要考慮省電/續航再另外討論。

## 待確認事項（尚未決定，需要你確認）

- `config.json` 裡標記 `CALIBRATE_ME` 的數值（`odometry.optical_flow_px_to_mm`、
  `weight.hx711_offset` / `hx711_scale`）都還是佔位值。校正工具
  （`tools/calibrate_weight.py`、`tools/calibrate_optical_flow.py`）已經做好、
  邏輯也驗證過，硬體也是接著的，隨時可以跑；只是購物車秤重機構與光流感測器
  安裝位置還在調整中，現在跑出來的值只對「當下的安裝方式」準，機構定案前先跑
  只能當流程試跑，不建議直接當正式值寫回——等機構真正定案後，照上面「校正
  工具」小節的指令正式跑一次填入正確值（相機的內參校正已經完成，跟這幾個是
  不同的校正項目）。
- PMW3901 SPI 若需要降時脈，記得先在 MCC GUI 端改。
- **MCU 端已知修正**：PMW3901 之前發現會有休眠現象，導致那段時間的
  `dx`/`dy` 遺失或不準；下位機已經加上看門狗重置修正，目前光流資料不會
  再遺失。這是韌體端的修正，Pi 端不用改，但代表這個修正之前如果有跑過
  `calibrate_optical_flow.py` / `verify_optical_flow.py`，那些結果可能
  受過休眠影響、不完全準，建議修正後找時間重新跑一次流程試跑
  （`--dry-run`）確認資料收集正常、數字合理。

## 已在硬體上驗證過的部分

- `drivers/uart_receiver.py`：實際 UART 封包收發、checksum 驗證、CSV logger 都跑過。
- `drivers/barcode_scanner.py`：實際刷你的載具條碼（含大小寫、`/` 符號）都正確解析。
- `drivers/camera_stream.py`：色彩通道、棋盤格校正都在實體相機上驗證過。
- `database/db_manager.py`：建表 + 5 筆測試資料 seed 跑過。

## 已知限制 / 尚未驗證

- `barcode_scanner.py` 的鍵盤對照表目前涵蓋數字（含小鍵盤版）、英文字母、
  `- . /`，如果之後條碼包含其他符號，用 `--debug` 看原始鍵碼再補對照。
- Phase 3 起 `vision/` 底下的模組如果要用到顏色資訊（不只是灰階角點偵測），
  記得先確認過 `camera_stream.py` 的色彩通道假設在當下硬體/picamera2 版本上
  依然成立（用 `--color-test` 驗證），不要直接沿用假設。
- `core/odometry_engine.py` 目前只用假資料驗證過核心邏輯，還沒有在真實硬體上
  實際推車測試過（累積誤差量級、旋轉方向對不對這些都要實測才能確認）；而且
  `optical_flow_px_to_mm` 還是 `CALIBRATE_ME` 佔位值，算出來的距離現在還不
  是真實公釐數。`--min-squal` 的門檻值也還沒有實測資料可以參考該設多少，
  目前預設 0（不過濾），有實機 squal 數據之後再回頭調。yaw 目前完全信任
  IMU，還沒有視覺校正（`vanishing_point.py` 待寫）。
- `core/cart_state_machine.py`／`core/cart_manager.py` 目前只用假資料/假事件
  邏輯測試過（含互動模擬 `--simulate`），還沒有接上真實的
  `barcode_scanner.py`／`uart_receiver.py` 秤重資料流即時驗證過。
  `GateEntryDetected`/`GateExitDetected` 的來源方向已經確定是
  `core/landmark_correction.py` 的 BLE 雙 Beacon 差分（見對應章節），但真正
  掃描 BLE 硬體的 `drivers/ble_beacon_scanner.py` 還沒寫、也沒有真的 Beacon
  可以測，現在只能靠這兩個事件手動/程式模擬觸發。秤重比對用的
  `standard_weight_g`/`weight_tolerance_g` 是 `database/db_manager.py` 裡的
  測試資料，跟 `weight.hx711_offset`/`hx711_scale` 一樣，都要等秤重機構定
  案、真的校正過才是準確值。members 表的登入條碼前綴（`MEMBER-`）也是先
  假設的格式，等真正的會員卡/App 設計出來要跟著調整。
- `core/landmark_correction.py` 的 `RssiPeakDetector`/`GateCrossingDetector`
  只用合成的 RSSI 數列驗證過峰值偵測與雙 Beacon 方向判定的邏輯本身（包含
  CLI 互動模擬），還沒有真實 BLE 環境的訊號特性可以核對——`min_rise_dbm`／
  `gate_hysteresis_dbm`／`rssi_smoothing_window` 這幾個參數都是先給合理預設
  值，等真的有 Beacon 跟 Pi5 藍牙可以測，量到真實 RSSI 抖動的量級後要回頭
  調整。`config.json` 的 `landmarks.points` 座標也是佔位值。
