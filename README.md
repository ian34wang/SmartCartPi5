# SmartCart_Pi5 — 開發紀錄

Phase 1（環境建置與硬體可靠度驗證）、Phase 2（資料擷取驅動層）已完成。
Phase 3（定位積分與視覺融合）進行中，目前完成第一步「純 UART 版本的基礎
里程計」（`core/odometry_engine.py`），視覺校正（`vanishing_point.py`）、
數據融合、視覺光流備援（`floor_optical_flow.py`）留待下次。Phase 4 起
（商業邏輯、UI、AI）也留待之後。

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
│   └── db_manager.py        # SQLite CRUD，含 5 筆測試商品資料 seed
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
│   └── odometry_engine.py   # Phase 3 基礎里程計：純 UART dead-reckoning，含 squal 過濾
├── ai/, ui/                 # 目前只有 __init__.py，Phase 4 起才會實作
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
python3 -m database.db_manager --init    # 建表 + 寫入 5 筆測試商品
python3 -m database.db_manager --list
```

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
