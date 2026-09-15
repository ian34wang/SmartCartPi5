# SmartCart_Pi5 — 開發紀錄與交接文件

Raspberry Pi 5 智慧購物車（上位機）。下位機是 SAMD21（MCC 產生的韌體，不在這個 repo 裡），
透過 UART 把 BNO080 姿態、PMW3901 光流、HX711 秤重三合一封包送上來。

**這份文件的定位**：接手的人（或新開的聊天室）只讀這一份，就要能知道
「現在做到哪、什麼是真的驗證過的、什麼還是假的、下一步該做什麼」。
文件裡凡是寫「已在硬體上驗證」的，都是真的在實體 Pi 5（YichaoPi5）+ 真實硬體上
跑過、修過踩到的坑；凡是還沒驗證的，都會明講，不會用模糊的講法帶過。

最後更新：2026-09-15

---

## 一分鐘現況

| Phase | 內容 | 狀態 |
|---|---|---|
| 1 | 環境建置、硬體可靠度驗證 | 完成，實機驗證過 |
| 2 | 資料擷取驅動層（UART / 條碼 / 相機） | 完成，實機驗證過 |
| 3 | 定位積分與視覺融合 | 部分完成：純 UART 里程計（邏輯測過、**未實機推車驗證**）、BLE 地標校正演算法 + 掃描驅動 + 門口判定（演算法測過，**驅動未在真 Beacon 上跑過**）。視覺融合/備援未開始 |
| 4 | 購物流程狀態機 | 完成，邏輯測試涵蓋完整，實機資料流跑過（`tools/run_real_hardware_flow.py`） |
| 5 | 觸控 UI + 系統整合 | **主要工作完成**：13 頁 UI 全部接上狀態機與資料庫；條碼、秤重、閘門三個硬體全部接真的，沒有任何模擬路徑 |
| 6 | AI（錢包管家 / 健康護照 / 即時優惠 / 異常偵測） | **完全未開始**，`ai/` 只有空的 `__init__.py` |
| 7 | 室內導航 | 未開始，且卡在硬體與店面地圖設計 |

**現在最該做的四件事**（詳見最後一章「尚未完成的功能總表」）：

1. **跑門口 Beacon 校正**：`python3 -m tools.calibrate_gate_beacons`。
   Beacon 已經有了，但第一次實測發現兩顆的基準差 12.5 dB 且方向相反——
   **不校正的話出場判定永遠不會觸發**（詳見「Phase 3：定位」章的實測記錄）。
2. 秤重機構定案後跑正式校正（`tools/calibrate_weight.py`），現在 `hx711_scale`
   還是佔位值 1.0。
3. 把真實商品建檔（`tools/product_admin.py`），資料庫目前只有測試資料。
4. 會員偏好（過敏原/飲食/宗教/預算）目前關掉程式就消失，`members` 表沒有對應
   欄位——這是「健康護照」能不能做的前置條件。

---

## 目錄結構

```
SmartCart_Pi5/
├── config.json               # 系統全域參數，含待校正值（標記 CALIBRATE_ME）
├── requirements.txt
├── README.md                 # 本檔
│
├── drivers/                  # 硬體存取層（只管把資料讀進來，不含商業邏輯）
│   ├── uart_receiver.py      # 背景執行緒 + Queue，含 checksum 驗證與可選 CSV logger
│   ├── barcode_scanner.py    # evdev 獨佔 USB 條碼掃描器
│   ├── ble_beacon_scanner.py # bleak 掃描 BLE Beacon RSSI（管制區門口判定的資料來源）
│   └── camera_stream.py      # picamera2 影像擷取 + 棋盤格畸變校正 + --color-test 診斷
│
├── core/                     # 商業邏輯與演算法（不碰硬體，可用假資料完整測試）
│   ├── position_types.py     # 定位系統的輸出契約（PositionEstimate）
│   ├── odometry_engine.py    # 純 UART dead-reckoning 里程計，含 squal 過濾
│   ├── landmark_correction.py# BLE 地標校正的純演算法（峰值偵測、雙 Beacon 差分）
│   ├── gate_monitor.py       # 把 BLE RSSI 觀測變成「進場/出場」判定（平滑 + 航向交叉驗證）
│   ├── weight_convert.py     # HX711 raw -> 公克換算（校正/驗證/狀態機共用）
│   ├── cart_manager.py       # 購物清單資料結構（清單、數量、金額）
│   └── cart_state_machine.py # 購物流程狀態機（登入~登出整趟流程的正確性保障）
│
├── database/
│   ├── db_manager.py         # SQLite 存取層：products + members 兩張表
│   └── inventory.db          # （執行時產生，不進版控）
│
├── ui/                       # Phase 5 觸控應用
│   ├── app_gui.py            # pywebview 主程式 + Bridge（前端與狀態機的橋接）
│   └── templates/index.html  # 13 頁單頁應用（**由腳本產生，不要手改**）
│
├── design-cart-ui/           # UI 視覺設計稿（*.dc.html），index.html 的來源
│
├── tools/
│   ├── build_app_ui.py       # design-cart-ui/*.dc.html -> ui/templates/index.html
│   ├── product_admin.py      # 商品/會員資料庫後台 CLI（建檔、CSV 匯入匯出）
│   ├── run_real_hardware_flow.py  # 純終端機的實機整合測試（三個硬體全接真的）
│   ├── calibrate_weight.py   # HX711 重量校正：算 offset/scale 並寫回 config.json
│   ├── calibrate_optical_flow.py  # PMW3901 光流校正：算 px_to_mm 並寫回 config.json
│   ├── verify_weight.py      # 重量校正「驗證」工具：只讀不寫
│   └── verify_optical_flow.py# 光流校正「驗證」工具：只讀不寫
│
├── tests/
│   ├── test_app_gui_bridge.py# Bridge 端對端（純 Python，必跑）
│   ├── test_gate_monitor.py  # BLE 門口判定邏輯（純 Python，必跑）
│   └── test_ui_layout.py     # 13 頁版面（需要 playwright，選用）
│
├── vision/
│   └── camera_calibration.npz# 相機內參校正結果（已產生，reprojection error 0.2026）
│
└── ai/                       # Phase 6，目前只有空的 __init__.py
```

**執行方式一律用 `python3 -m`，而且要在專案根目錄下**（除了 `product_admin.py`
自己有加 `sys.path` 引導之外，其他模組都沒有，直接 `python3 tools/xxx.py` 會 import 失敗）：

```bash
cd ~/SmartCartPi5
python3 -m ui.app_gui
python3 -m tools.product_admin
```

---

## CLI 指令總覽（速查）

全部都要在專案根目錄下、用 `python3 -m <模組路徑>` 執行。
需要硬體的指令接不到裝置時一律直接報錯結束，不會退回模擬。

### 日常操作

| 指令 | 用途 | 需要的硬體 |
|---|---|---|
| `python3 -m ui.app_gui --fullscreen` | **主程式**：13 頁觸控 UI，完整購物流程 | 掃描器 + UART + BLE 三個都要 |
| `python3 -m tools.product_admin` | 商品/會員資料庫後台（互動選單） | 加/改商品時需要秤；其他指令不用 |
| `python3 -m tools.run_real_hardware_flow` | 純終端機跑完整流程，順便看定位座標 | 掃描器 + UART + BLE 三個都要 |

### 資料庫

| 指令 | 用途 |
|---|---|
| `python3 -m database.db_manager --init` | 建表 + 寫入測試資料（5 筆商品、2 筆會員） |
| `python3 -m database.db_manager --list` | 列出所有商品與會員 |
| `python3 -m tools.product_admin list` | 列出所有商品 |
| `python3 -m tools.product_admin add` | 新增商品（條碼直接刷、重量現場秤、容差自動 10%） |
| `python3 -m tools.product_admin add --barcode X --name Y --price 39` | 同上，但條碼/名稱/單價先用參數給（重量仍要現場秤） |
| `python3 -m tools.product_admin edit <barcode>` | 修改商品（Enter 保留原值） |
| `python3 -m tools.product_admin delete <barcode> [-y]` | 刪除商品 |
| `python3 -m tools.product_admin import <file.csv>` | CSV 批次匯入/更新 |
| `python3 -m tools.product_admin export <file.csv>` | 匯出成 CSV（備份用） |
| `python3 -m tools.product_admin member-list` | 列出所有會員 |
| `python3 -m tools.product_admin member-add --member-id MEMBER-0003 --name "王小明"` | 新增/修改會員 |
| `python3 -m tools.product_admin member-delete <member_id> [-y]` | 刪除會員 |

共用參數：`--db <路徑>`、`--port <序列埠>`、`--baud <鮑率>`

### 硬體單獨測試（診斷用）

| 指令 | 用途 |
|---|---|
| `python3 -m drivers.uart_receiver --port /dev/ttyAMA0` | 看 UART 即時封包（dx/dy/squal/yaw/hx711_raw） |
| `python3 -m drivers.uart_receiver --csv-log` | 同上並寫 CSV（Phase 6 訓練資料） |
| `sudo venv/bin/python3 -m drivers.barcode_scanner --list` | 列出所有輸入裝置，找掃描器的裝置名稱 |
| `sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint USBKey` | 刷條碼看解析結果 |
| `sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint USBKey --debug` | 同上並印原始鍵碼（換掃描器時用） |
| `python3 -m drivers.ble_beacon_scanner --list` | 掃 10 秒，列出附近所有 BLE 裝置與位址 |
| `python3 -m drivers.ble_beacon_scanner` | 持續印門口那兩顆的**原始** RSSI（未濾波，抖動大是正常的） |
| `python3 -m drivers.camera_stream --preview` | 測試相機擷取 |
| `python3 -m drivers.camera_stream --color-test` | 拍一張存檔，肉眼確認色彩通道順序 |

### 校正（都會問過才寫回 `config.json`，加 `--dry-run` 只看不寫）

| 指令 | 校正什麼 | 何時要跑 |
|---|---|---|
| `python3 -m tools.calibrate_gate_beacons --monitor` | 即時看「原始 vs 濾波後」的值與判定結果 | 推車走動、或想確認濾波有沒有在作用時 |
| `python3 -m tools.calibrate_gate_beacons --survey` | 勘測：量梯度/雜訊比，判斷這組佈署夠不夠用 | 改 Beacon 擺法/加反射板的**前後各跑一次**比較 |
| `python3 -m tools.calibrate_gate_beacons` | 門口兩顆 Beacon 的 RSSI 偏移量、遲滯、絕對門檻 | **Beacon 裝好後必跑**，否則出場判定幾乎不會觸發 |
| `python3 -m tools.calibrate_weight` | `hx711_offset` / `hx711_scale` | 秤重機構定案後 |
| `python3 -m tools.calibrate_optical_flow` | `optical_flow_px_to_mm` | PMW3901 安裝高度定案後 |
| `python3 -m drivers.camera_stream --calibrate --num-images 15 --square-size-mm 24` | 相機內參 | **已完成**（error 0.2026） |

### 驗證（只讀 `config.json` 現有值，不會修改）

| 指令 | 驗證什麼 |
|---|---|
| `python3 -m tools.verify_weight` | 放上已知重量的物品，看換算誤差幾 % |
| `python3 -m tools.verify_optical_flow` | 推一段已知距離，看推估誤差幾 % |

### 建置與測試

| 指令 | 用途 |
|---|---|
| `python3 tools/build_app_ui.py` | 從 `design-cart-ui/*.dc.html` 重新產生 `ui/templates/index.html` |
| `python3 tests/test_app_gui_bridge.py` | Bridge 端對端（純 Python，必跑） |
| `python3 tests/test_gate_monitor.py` | BLE 門口判定邏輯（純 Python，必跑） |
| `python3 tests/test_ui_layout.py` | 13 頁版面（需要 playwright，選用） |

### 一次性權限設定（各做一次就好）

```bash
sudo usermod -aG input $USER     # evdev 讀條碼掃描器，重新登入後生效
sudo rfkill unblock bluetooth    # 藍牙打不開時
sudo raspi-config                # Serial Port -> login shell: No, hardware: Yes
```

## 快速上手：在 Pi 上把整套跑起來

```bash
cd ~/SmartCartPi5
source venv/bin/activate

# 1. 建資料庫（第一次才需要；--init 會寫入 5 筆測試商品與 2 筆測試會員）
python3 -m database.db_manager --init

# 2. 把真的商品建檔（測試商品刷不到真的條碼，見「商品資料庫後台」章）
python3 -m tools.product_admin

# 3. 開主程式
python3 -m ui.app_gui --fullscreen
```

開起來之後：畫面右上角有兩顆按鈕，`DEV` 打開唯讀診斷面板，`✕` 退出
（鍵盤 `Esc` 也可以；真的卡住就另開終端機 `pkill -f ui.app_gui`）。

**常用參數**：

```bash
python3 -m ui.app_gui --fullscreen          # 全螢幕（螢幕本身是 720x1280 時建議）
python3 -m ui.app_gui --db /path/to.db      # 指定資料庫
python3 -m ui.app_gui --port /dev/ttyAMA0 --baud 115200   # UART
python3 -m ui.app_gui --scanner-hint USBKey # 條碼掃描器裝置名稱關鍵字
python3 -m ui.app_gui --adapter hci0        # 藍牙介面
python3 -m ui.app_gui --beacon-timeout 30   # 開機時等 Beacon 現身的秒數
```

**第一次跑之前要做的兩件權限設定**（各做一次就好）：

```bash
sudo usermod -aG input $USER     # 讓 evdev 讀得到條碼掃描器，重新登入後生效
sudo rfkill unblock bluetooth    # 藍牙打不開時
```

啟動時會依序檢查三個硬體並印出 `[1/3]`～`[3/3]`，任何一個接不上就會印出
該怎麼修然後結束——不會半殘地開起來。

## 單一實體路徑原則（2026-09-15 重構）

**整個系統只走真實硬體，沒有任何模擬、退回或旁路。** 三個裝置都是啟動時必須連上
的硬性條件，任何一個接不上就直接印出原因並結束（exit 1），不會半殘地開起來：

| 裝置 | 唯一路徑 | 接不上會怎樣 |
|---|---|---|
| 條碼掃描器 | `drivers/barcode_scanner.py`（evdev 獨佔 `/dev/input/event*`） | 印出安裝/權限/裝置名稱三項檢查清單後結束 |
| 秤重 | `drivers/uart_receiver.py` 的 `hx711_raw` → `core/weight_convert.py` | 序列埠開不了、或 5 秒內收不到合法封包就結束 |
| 管制區閘門 | `drivers/ble_beacon_scanner.py` → `core/gate_monitor.py`（BLE 雙 Beacon 差分） | 藍牙打不開、或指定秒數內沒掃到門口那兩顆 Beacon 就結束 |

### 為什麼要這樣訂

之前每個裝置都有兩三種「接不到就退回模擬」的路徑：掃描器有 evdev／鍵盤 wedge
兩種模式、秤重有展示模式（掃碼後自動補一筆假讀數）、閘門有開發面板按鈕、
`product_admin` 的重量可以手打、`run_real_hardware_flow` 有 `--no-scanner`。
結果是兩件事同時發生：

1. **出問題時第一件事不是查硬體，而是要先搞清楚自己現在走在哪一條路上。**
2. **畫面會在硬體其實沒在運作的情況下看起來一切正常**——最糟的一種失敗。

寧可開不起來，也不要假裝正常。所以現在拿掉的東西：

- `ui/app_gui.py`：`--scanner auto/keyboard/evdev/none` 四種模式 → 只剩 evdev；
  `--demo-weight`、`--no-weight` 與整套秤重展示模式 → 全部移除；
  前端的 keyboard wedge（把按鍵拼成條碼）與焦點警告 → 移除；
  所有 `simulate_*` 方法（模擬掃碼、模擬秤重、模擬閘門、模擬感測器斷線）→ 移除。
- 登入畫面的「手動輸入會員代碼」輸入框 → 移除（刷實體會員卡，或走「以訪客身份繼續」）。
- 開發測試面板 → 改成**唯讀診斷面板**，沒有任何能製造事件的按鈕。
- `tools/product_admin.py`：`--no-scale` 與手動輸入公克數的退路 → 移除，
  重量只能從秤現場量（`add --weight` 這個參數也一併拿掉）。
- `tools/run_real_hardware_flow.py`：`--no-scanner`、`b`（手動輸入條碼）、
  `e`/`x`（鍵盤模擬閘門進出）→ 移除，閘門改接真的 BLE。
- `core/cart_state_machine.py --simulate`、`core/landmark_correction.py --simulate`
  → 移除（兩支都改成純邏輯模組，沒有 CLI）。
- 刪除檔案：`tools/mock_uart_generator.py`、`tools/mock_barcode_input.py`、
  `tools/preview_ui.py`、`tools/ui_preview/`。

`run_real_hardware_flow.py` 裡還是用鍵盤輸入的那幾個指令（鎖定結帳、確認付款、
登出、強制登出、重試/放棄比對）**不算旁路**——那些在正式產品裡本來就是觸控螢幕
上的按鈕、由人操作，不是硬體感測器。那支是純終端機工具，鍵盤就是它的介面。

### 這次一併修掉的「無聲失敗」

`CartStateMachine` 設計上「被拒絕的事件」不會拋例外，只會記一筆警告然後 return。
所以任何上層如果不主動去比對 `get_alerts()`，使用者就會看到「操作了、畫面完全
沒反應、也沒有任何錯誤訊息」。這一輪修了三個這類的洞：

- **警告歷史塞滿 200 筆之後錯誤訊息會消失**：`Bridge` 原本用「呼叫前後警告筆數
  有沒有變多」判斷，但清單有上限，滿了之後每新增一筆就砍掉最舊一筆、**總長度
  不變**。一台整天開著的購物車跑滿 200 筆不難，跑滿之後這個 bug 會自己復發。
  改成比對「最後一筆警告物件是不是同一個」，跟清單長度無關。
- **已登入時又刷一張無效會員卡會被誤判成登入成功**：`Bridge.login()` 原本靠
  「狀態有沒有變」判斷，這個情境下狀態本來就不會變。改成靠「有沒有新警告」。
- **還沒進管制區就刷條碼會完全沒反應**：狀態機那種拒絕只記 INFO 等級的警告，
  而 Bridge 預設會過濾 INFO（那多半是背景事件被忽略的雜訊）。現在刷條碼這個
  動作特別設成連 INFO 也要顯示——使用者明確做了一個動作，任何被拒絕的原因都
  必須講出來。

### 順帶修掉的其他問題

- **`tools/product_admin.py` 會把測試資料灌進正式商品庫**：原本是
  `db.init_db(seed=True)`，每次對一個全新資料庫開這支後台工具，就會先被塞進
  5 筆假的測試商品，自己加的第一筆真商品變成第 6 筆。改成 `seed=False`。
- **無法登錄真實會員卡**：`DBManager` 早就有 `upsert_member()`/`delete_member()`，
  但兩支 CLI 都沒有指令呼叫得到——資料庫裡能存在的會員只有寫死的兩筆測試會員。
  補上 `member-add` / `member-list` / `member-delete`。
- **底部導覽列被裁掉**：每個畫面的 CSS 從設計稿的 `body { height:1280px }` 換算
  而來，寫死 1280px；實際視窗可視高度常常小於 1280（標題列吃掉幾十 px），
  固定高度 + `overflow:hidden` 就讓最底下的導覽列看不到。改成填滿實際可視高度。
- **`🛠` 圖示在 Pi 上變成豆腐方框**（沒裝彩色 emoji 字型），找不到面板入口。
  改成純文字 `DEV`。

## Phase 5：觸控 UI（`ui/app_gui.py` + `ui/templates/index.html`）

13 頁畫面全部接上真實的 `CartStateMachine` + `DBManager` + `CartManager`，
是一支可以直接在 Pi 上執行的觸控應用。

### 架構分工

- **`Bridge`（`ui/app_gui.py`）**：pywebview 的 `js_api` 物件。只做
  「前端呼叫 → 轉成 `CartStateMachine` 事件 → 組出前端要畫的完整畫面狀態
  （`_state_payload()`）再丟回去」，不含任何 UI 邏輯。
- **前端（`index.html`）**：畫面切換、選取樣式、鍵盤條碼攔截全部在 JS 裡做。
  每次 `APP.callApi(name, ...)` 拿到 payload 就直接 `render()`，不需要再問一次狀態。
- **背景執行緒推播**：硬體事件（掃描器、UART 秤重、逾時計時器）是背景執行緒
  單方面發生的，沒有對應的前端呼叫可以掛 `.then()`，所以由 Python 主動呼叫
  `window.evaluate_js("window.APP.render(...)")` 把狀態推過去（`Bridge._push_state()`）。
  秤重是 20Hz 連續送值，**只有真的造成狀態變化或新警告才推**，否則畫面每秒重畫
  20 次會閃爍、還會打斷開發面板正在打字的輸入框。
- **精靈流程不在狀態機裡**：歡迎頁 → 登入 → 讀取個人化設定 → 過敏原/飲食/宗教/預算
  四步精靈，這些不是 `CartStateMachine` 的狀態（那邊只有「未登入/購物中/…」這種
  營運層級的狀態），所以用 `Bridge._ui_stage` 另外疊一層。使用者完成預算設定後
  觸發 `GateEntryDetected`，才正式交給狀態機的 `STATE_SHOPPING` 接手。

### 13 頁畫面與狀態機的對應

| 畫面 | `screen` 代號 | 由什麼決定 |
|---|---|---|
| 待機/喚醒 | `main` | `_ui_stage`（或狀態機回到 `SESSION_CLOSED` 後自動登出） |
| 會員登入 | `login` | `_ui_stage` |
| 載入會員資料 | `loading_profile` | `_ui_stage` |
| 過敏原 / 葷素 / 宗教 / 預算 | `allergens` `diet` `religion` `budget` | `_ui_stage` |
| 購物主畫面 | `shopping` | `STATE_SHOPPING` / `AWAITING_ITEM_SCAN` / `AWAITING_WEIGHT_*` |
| 找貨（導航） | `navigation` | 底部導覽列分頁（畫面是靜態 mockup） |
| 優惠 | `promotions` | 底部導覽列分頁（畫面是靜態 mockup） |
| 秤重異常 | `weight_alert` | `STATE_WEIGHT_MISMATCH_ERROR` |
| 結帳 | `checkout` | `STATE_LOCKED_FOR_CHECKOUT` |
| 離場確認 | `exit_confirm` | `STATE_AWAITING_EXIT` |

「秤重比對中」的三個狀態在 UI 上仍然是購物主畫面——掃碼/等待放入的提示框是
購物畫面內的一個區塊（`#f-pending-box-wrap`），不是獨立畫面。

### UI 怎麼改（重要）

**`ui/templates/index.html` 是產生出來的，不要直接手改**，下次重跑腳本就會被蓋掉。

```bash
python3 tools/build_app_ui.py     # design-cart-ui/*.dc.html -> ui/templates/index.html
```

- 要改**各畫面的視覺樣式**（字級、間距、顏色）→ 改 `design-cart-ui/<畫面>.dc.html`，重跑腳本。
- 要改**前端行為（JS）、共用樣式、開發面板** → 改 `tools/build_app_ui.py` 裡的
  `TEMPLATE` 字串，重跑腳本。
- 要改**哪些假資料要被換成動態容器** → 改 `tools/build_app_ui.py` 裡對應的
  `patch_<畫面>()` 函式。

> **腳本的補丁是跟目前 HTML 結構綁定的**：`str.replace()` 找不到目標字串時會安靜地
> 什麼都不做，所以改完設計稿一定要實際跑一次、開畫面確認，不要只看腳本沒報錯就當成功。
> `scope_css()` 會把每個畫面的 CSS 選擇器加上 `#screen-<id>` 前綴，避免 13 份獨立 CSS
> 混在同一頁時互相污染。

### 唯讀診斷面板（右上角 `DEV` 按鈕）

**沒有任何能製造事件的按鈕**——這支程式只走真實硬體，如果面板上按一下就能生出
一筆掃碼或秤重，那就等於又多了一條旁路。面板上只有三行即時狀態加一份完整的
狀態 JSON：

- **最近收到的條碼**：實體掃描器刷一下，這行有變 = 條碼有進到程式（問題在後面的
  商品資料或秤重）；沒變 = 條碼根本沒進來（掃描器沒接好、evdev 抓錯裝置）。
  這一行就是診斷「掃碼沒反應」的分水嶺。
- **秤重**：目前公克數 + 感測器連線狀態。沒有數字或不會跳動 = UART 斷了，
  這時候掃碼一定會被狀態機擋下來（沒有重量就無法驗證掃了什麼）。
- **門口 BLE 判定次數**：以及目前是不是還在「等待進入管制區」。

面板裡也寫了刷條碼沒反應時的四步檢查順序。

### 已知缺口（面板上對應功能會直接跳提示，不會假裝成功）

1. `members` 表沒有欄位存過敏原/飲食習慣/預算/常購清單——LoadingProfile 畫面
   「情境 A：找到已儲存設定」查不到真資料，選 A 一樣會走精靈重問一次
   （並跳提示說明這件事）。精靈的結果只存在這次執行的記憶體裡，程式關掉就不見。
2. Checkout 畫面「✕ 返回繼續選購」——狀態機沒有「解除鎖定」的事件，這顆按鈕
   先跳提示，不會真的解鎖。
3. WeightAlert 的「重新校準歸零」「呼叫店員協助」、ExitConfirm 的
   「取消本次結帳／需要協助」——沒有對應的狀態機事件或店員通知系統，一樣先跳提示。
4. 底部導覽列的「我的」分頁完全沒實作，點下去只跳「尚未實作」提示。
5. 「找貨」「優惠」兩頁是完全靜態的 mockup，沒有任何真實邏輯驅動
   （沒有近效期偵測、沒有多件優惠、沒有搭配推薦）。購物畫面上的
   「健康護照提醒：此商品含麩質穀物」也是寫死的文字，沒有真的比對商品成分。

---

## Phase 4：購物流程狀態機（`core/cart_state_machine.py`、`core/cart_manager.py`）

涵蓋的完整流程：**登入帳號 → 進入管制區 → 購物（掃碼加入/移除 + 秤重比對）
→ 鎖定結帳 → 付款 → 走出管制區 → 登出帳號**。這支是「底層架構的支持」，
只管流程本身正不正確，不含 LLM 推薦、預算分析這類進階功能（那些排 Phase 6）。

### 加入/移除是自動判斷，不是手動切換模式

這是設計上特別要注意的一點。兩個方向的實際操作順序本來就不一樣——加入商品是
「先掃碼、再把商品放進車」；移除商品是「先把商品拿起來（重量先變）、再掃碼」。
所以狀態機分別處理這兩種順序：

- 購物中（`SHOPPING`）掃到碼、當下重量還沒變 → 一律當「加入」，基準值是掃碼當下的重量。
- 購物中重量自己先變了（超過 `weight.unscanned_change_threshold_g` 的雜訊門檻）
  但還沒掃碼 → 進入 `AWAITING_ITEM_SCAN`，記錄「變化前」的重量當基準值，
  並發出 `unscanned_weight_change`（warning）提醒補掃；這時候才掃到碼，就用目前重量
  跟這個基準值的方向（變輕＝移除、變重＝加入）自動推斷模式。
- 在 `AWAITING_ITEM_SCAN` 期間，如果重量自己飄回基準值附近（例如手滑碰到籃子），
  視為虛驚一場，自動解除回 `SHOPPING`（info 等級的 `unscanned_weight_auto_resolved`）。
- 如果超過 `weight.unscanned_change_timeout_sec` 都沒補掃碼——也就是「只做一半」的情境
  ——升級成 `WEIGHT_MISMATCH_ERROR`，發出 critical 等級的 `unscanned_weight_timeout`。
  這是舊版邏輯完全偵測不到的漏洞（舊版對「購物中發生了解釋不了的重量變化」直接忽略，
  等於防損上的一個洞）。

因此 `ItemScanned` 事件**不帶 `mode` 欄位**，模式永遠是狀態機自己推斷的。

### 狀態一覽

`UNAUTHENTICATED`（未登入）→ `LOGGED_IN_OUTSIDE_ZONE`（已登入未進管制區）
→ `SHOPPING`（購物中）↔ `AWAITING_ITEM_SCAN`（重量已變但還沒掃碼）
↔ `AWAITING_WEIGHT_INCREASE`/`AWAITING_WEIGHT_DECREASE`（掃碼後等秤重比對）
↔ `WEIGHT_MISMATCH_ERROR`（秤重異常或補掃逾時，需要重試或放棄該筆）
→ `LOCKED_FOR_CHECKOUT`（鎖定結帳）→ `AWAITING_EXIT`（付款完成，等走出管制區）
→ `SESSION_CLOSED`（已結束，可登出回到 `UNAUTHENTICATED`）。

### 秤重比對邏輯

掃碼當下記錄目前重量當基準值，之後每筆新秤重讀數跟基準值的差，拿去跟這個商品在
資料庫登記的 `standard_weight_g`（加入為正、移除為負）±`weight_tolerance_g` 比對：

- 容差內 → 比對成功，商品進/出購物清單。
- 方向相反且超出容差 → 「方向不符」（可能有其他商品同時被拿動），critical。
- 方向對但超出容差上限 → 「重量不符」（可能拿了不只一件），warning。
- 方向對且還沒超標 → 視為還在放/拿的過程中，繼續等到成功或
  `weight.weight_match_timeout_sec` 逾時。

### 遇到問題怎麼處理

分兩種：

**(a) 軟體層能擋下來/引導重試的**（條碼查無資料、秤重沒對上、逾時、感測器斷線……）
——狀態機會擋下錯誤動作、發出對應的 `CartAlert`（含 severity：info/warning/critical），
並提供 `RetryWeightCheckRequested`（重試）/`VoidPendingItemRequested`（放棄這筆）的路徑。

**(b) 沒辦法只靠軟體解決、代表可能有防損疑慮的**（例如結帳前就走出管制區、
強制登出時清單裡還有商品）——狀態機**不會**假裝沒事發生、自己把狀態轉成正常結束，
只會發出最高等級（critical）的警告，交給上層（UI、警報硬體、店員）處理。
這是軟體邊界，狀態機本身沒辦法真的攔住一個人。

> **設計上的重要特性**：被拒絕的事件**不會拋例外**，只會呼叫 `_alert()` 記一筆警告
> 然後直接 return。所以任何呼叫 `process_event()` 的上層，如果不主動去比對
> `get_alerts()`，使用者就會看到「操作了、畫面完全沒反應、也沒有任何錯誤訊息」
> ——這正是 2026-09-15 那個 bug 的其中一半。寫新的上層時務必記得這件事。

完整的警告代碼列在 `core/cart_state_machine.py` 開頭的 `ALERT_*` 常數，設計成之後
Phase 6 的異常偵測可以直接依代碼分類統計，不用解析訊息文字。

### 跟硬體現況的對應

- **登入**：目前硬體只有 USB 條碼掃描器，所以是「掃會員條碼/QR code」（不是密碼、
  不是 RFID）。條碼格式用 `config.json` 的 `state_machine.login_barcode_prefix`
  （預設 `"MEMBER-"`）跟商品條碼區分，避免誤掃。
- **進入/走出管制區**：確定的方向是 BLE Beacon 雙標籤差分（見 `landmark_correction.py`
  章節），不經過已經滿載的 MCU、直接用 Pi5 內建藍牙。但真正的 BLE 掃描驅動還沒寫，
  **現在這兩個事件只能靠 UI 模擬觸發**（`ui/app_gui.py` 在使用者設定完預算後自動觸發
  `GateEntryDetected`，離場則是開發面板的按鈕）。
- **鎖定結帳**：純軟體狀態鎖，沒有實體鎖車機構，UI 進到這個狀態要自己擋掉繼續增減商品。
- **同一會員是否已在別台購物車登入**：單機 SQLite 看不到其他購物車的狀態，
  需要中央伺服器/共用資料庫才能做，目前架構沒有這塊，暫不處理。

### 這支沒有 CLI

`core/cart_state_machine.py` 是純邏輯模組，沒有互動模擬入口（原本的 `--simulate`
已移除，理由見「單一實體路徑原則」那章）。要跑完整流程用
`tools/run_real_hardware_flow.py`（終端機）或 `python3 -m ui.app_gui`（觸控畫面），
兩邊都是真硬體。純邏輯的回歸測試在 `tests/test_app_gui_bridge.py`。

邏輯測試涵蓋：正常 ADD/REMOVE 全流程、只做一半逾時升級、逾時後重試/放棄、
重量自動飄回解除、雜訊門檻內忽略，以及 17 種錯誤情境（查無會員、未登入闖入、
查無商品、秤重逾時/方向不符/超出容差、移除不存在的商品、鎖定中掃碼、結帳前走出
管制區、登出前未結案、強制登出殘留商品、感測器斷線恢復……），全部通過。

## 商品與會員資料庫後台（`tools/product_admin.py`）

**資料庫裡目前只有出廠寫死的 5 筆測試商品與 2 筆測試會員。刷任何真實商品條碼都會
顯示「查無此商品」**——這是正常行為，不是 bug。要用真的商品，得先建檔。

```bash
python3 -m tools.product_admin                      # 互動選單（最常用）
python3 -m tools.product_admin list
python3 -m tools.product_admin add                  # 互動：條碼可直接刷、重量可直接秤
python3 -m tools.product_admin add --barcode 4710018001234 --name "豆漿" --price 25 --weight 320
python3 -m tools.product_admin edit <barcode>
python3 -m tools.product_admin delete <barcode> [-y]
python3 -m tools.product_admin import products.csv  # CSV 批次匯入/更新
python3 -m tools.product_admin export products.csv

python3 -m tools.product_admin member-list
python3 -m tools.product_admin member-add --member-id MEMBER-0003 --name "王小明"
python3 -m tools.product_admin member-delete MEMBER-0003 [-y]
```

CSV 欄位固定為 `barcode,name,unit_price,standard_weight_g,weight_tolerance_g`。

### 設計上的兩個重點

**條碼欄位就是普通的 `input()`**——USB 掃描器是鍵盤裝置，刷一下就等於把條碼打進去
再按 Enter，不需要任何特殊處理。之前有做過一個 evdev 版的「自動掃描模式」，
那個反而是有害的：`grab()` 獨佔裝置之後，終端機自己就收不到實體按鍵了，
結果是刷完條碼整個操作卡住。已經移除。

**重量是真的秤出來的，容差自動抓 10%**：`add`/`edit` 互動流程走到重量那一步時，
把商品放上秤重感應區、按 Enter 就會即時量測（重用已經硬體驗證過的
`UartReceiver` + `raw_to_grams` + `collect_hx711_samples`）。量到之後自動算
`容差 = 量到的重量 × 10%` 當預設值，直接按 Enter 接受，也可以自己改。
沒有接秤的時候（或加 `--no-scale`）會自動退回手動輸入公克數，不會卡住。

```bash
python3 -m tools.product_admin --no-scale     # 不連秤，重量一律手動輸入
python3 -m tools.product_admin --port /dev/ttyAMA0 --baud 115200
```

### 會員代碼的格式限制

會員代碼必須以 `config.json` 的 `state_machine.login_barcode_prefix`（預設 `MEMBER-`）
開頭，否則登入時會被狀態機判定成「不是會員碼格式」而拒絕。`member-add` 會在代碼
不符時印警告但仍然寫入（有時候只是要先建資料）。要改格式是改 config，不是改程式。

---

## Phase 3：定位

### 輸出契約（`core/position_types.py`）

`PositionEstimate` 這個 dataclass 是「定位系統最終要交給 UI/AI/商業邏輯的東西
長什麼樣子」的契約，欄位不會因為底層是純 UART 算的、還是之後接了視覺融合而改變：

```python
@dataclass
class PositionEstimate:
    x_mm: float                    # 全域座標系位置，公釐
    y_mm: float
    yaw_deg: float                 # 全域座標系航向角，度，逆時針為正
    position_source: str           # "optical_flow" 或（之後）"floor_optical_flow_fallback"
    yaw_source: str                # "imu" 或（之後）"imu+vision"
    sample_count: int
    skipped_low_confidence_count: int
    timestamp: Optional[float]
    last_landmark_id: Optional[str]         # 最近一次地標校正（除錯/UI 用）
    last_landmark_correction_at: Optional[float]
```

兩個確認過的決定：

1. **座標系原點**是程式啟動（或 `reset()`）當下車子的位置與朝向，**不是店面地圖上的
   固定座標**。畫的是「這台車今天這趟的軌跡」。要對齊店面地圖需要另外的初始定位機制。
2. `is_best_effort_estimate()` 現階段會**一直回傳 `True`**（因為視覺融合還沒做），
   這是如實反映現況、不是 bug。但也代表**下游現在拿它當旗標等於毫無鑑別力**，
   `vanishing_point.py` / `floor_optical_flow.py` 補上之前不要用它做 UI 判斷。

### 基礎里程計（`core/odometry_engine.py`）

拿 `UartReceiver` 收到的 PMW3901 `dx`/`dy`（乘上 `optical_flow_px_to_mm` 換算成公釐）
跟 BNO080 的 `yaw_deg`，套 2D 旋轉矩陣把車身局部座標系的位移轉成全域座標系再累加。
`squal` 信心值低於門檻的那一筆位移不計入累積位置（但 yaw 照樣更新，因為 yaw 是
BNO080 給的，跟光流追蹤品質無關）。

```bash
python3 -m core.odometry_engine                  # 用 config.json 設定即時監看
python3 -m core.odometry_engine --min-squal 30   # 過濾信心值低於 30 的光流樣本
python3 -m core.odometry_engine --px-to-mm 1.42  # 手動覆蓋（還沒校正時測試用）
```

**驗證狀態**：核心邏輯（旋轉矩陣正負號、位置累積、squal 過濾、`reset()`、
端對端 mock generator → 真 `UartReceiver` → 真 `OdometryEngine`）都用假資料驗證過。
**但還沒有在真實硬體上實際推車測試過**，而且 `optical_flow_px_to_mm` 還是佔位值 `1.0`，
所以現在算出來的 (X, Y) 只能看趨勢，不是真實公釐數。

**一個已知的小落差**（實機驗證時留意）：`process_packet()` 旋轉位移時用的是
**上一筆封包的 yaw**（`self._state.yaw_deg` 在該行之後才更新成這筆的值），
而 docstring 寫的是「用封包裡的 yaw」。20Hz 下差一個取樣週期影響很小，
而且用區間起點的航向本身也是合理的積分近似，但開機後的第一筆位移會用初始
`yaw_deg=0.0` 而不是真實航向來旋轉。等實機推車驗證時如果發現方向有系統性偏差，
這裡是第一個要看的地方。

### BLE 地標校正（`core/landmark_correction.py`）

用低成本 BLE Beacon 同時解決兩個原本分開卡住的問題——Phase 4 的
`GateEntryDetected`/`GateExitDetected` 沒有硬體來源、Phase 7 室內導航卡在
「浮動座標系怎麼對齊店面地圖」。兩個問題本質上是同一種：都需要「已知絕對座標的
地標點」讓車子經過時把座標釘回去，只是門口多需要一個「方向」。

**關鍵設計原則**：不要用 RSSI 算連續座標——室內多路徑反射環境下 RSSI 抖動可達
±10~15 dBm，換算距離誤差 2~4 公尺，比沒校正的 IMU 漂移還糟。改成把訊號峰值當
「離散觸發點」，車子經過已知座標的 Beacon 附近時觸發一次校正，把 dead-reckoning
累積誤差拉回去。兩次校正之間的位置完全還是靠里程計積分撐著。

**兩種偵測器**：

- `RssiPeakDetector`——給一般地標點用（走道轉角、貨架節點），只需要知道「經過了」。
  偵測訊號「從上升轉為下降」的瞬間（局部極大值），同一個峰值只觸發一次。
- `GateCrossingDetector`——管制區門口專用，需要方向。門內門外各放一顆 Beacon
  （約 1.5~2 公尺），比較兩者訊號的**相對大小**而不是絕對值（可以抵消車體金屬造成的
  固定衰減），哪邊持續佔優勢翻轉到另一邊就是一次穿越，回傳 `entering`/`exiting`。

三層防誤判（這是修正過一次設計漏洞後的結果）：第一版只看「訊號有沒有先升後降」，
抓不住「車子只是在 Beacon 附近晃過、根本沒有真的靠近/穿越」的情況——晃近一點點
一樣會有升降曲線，形狀上跟真的經過沒有差別。所以加了：

1. **絕對訊號強度門檻**（`peak_min_rssi_dbm` / `gate_min_crossing_rssi_dbm`）：
   峰值本身要夠強，代表車子當下真的夠靠近。
2. **連續樣本確認**（`gate_min_confirm_samples`，門口預設 3）：過濾單一雜訊樣本
   造成的瞬間翻轉。
3. **航向交叉驗證**（`is_heading_consistent()`）：BLE 訊號沒有方向性，量不到
   「有沒有真的通過那個實體開口」，所以門口判定理論上還要拿 IMU 航向角交叉驗證。
   **這個函式寫好測過了，但還沒有任何地方呼叫它**——把 BLE 判定跟 IMU 朝向 AND 起來
   才採信的組合邏輯，要等真實資料流接上時才能發揮作用。

**誠實講這個限制**：這三層防護能大幅降低誤判機率，但沒辦法做到零誤判——BLE RSSI
是全向、無方向性的訊號強弱量測，物理上量不到「有沒有真的通過那個實體開口」，
跟光電閘門、地埋迴路線這種「物理上一定要真的通過某個窄通道才會觸發」的機制比，
先天就有模糊地帶。額外能做但程式碼本身做不到的事：**部署時把 Beacon 發射功率調低**，
讓有效偵測範圍實際侷限在門口通道附近。軟體門檻要跟實體佈署互相配合。

**Beacon 選型建議**：因為上述物理侷限，**不要**選發射功率大、涵蓋範圍遠的產品
——功率越小越好，等於用硬體本身幫軟體判斷分擔一部分工作。拿一顆 ESP32
（BLE 廣播模式模擬 iBeacon/Eddystone）、或一支不用的舊手機當 Beacon 都可以，
不需要買訂製的商用 Beacon。

### 資料流：從 BLE 廣播到狀態機事件

```
drivers/ble_beacon_scanner.py   bleak 背景掃描，產生一筆筆 BeaconObservation(beacon_id, rssi)
    -> core/gate_monitor.py     每顆 Beacon 各自維護歷史、移動中位數平滑，湊成一對餵給
                                GateCrossingDetector；再用 IMU 航向角交叉驗證
    -> core/cart_state_machine  GateEntryDetected / GateExitDetected
```

`core/gate_monitor.py` 這一層是後來補的：`GateCrossingDetector` 只負責「兩顆平滑後
的 RSSI 誰佔優勢、什麼時候翻轉」，它不知道 RSSI 從哪來、不做平滑、也不碰 IMU。
中間那些事原本只存在「之後接 main.py 時再做」的口頭約定裡，沒有實作，而
`ui/app_gui.py` 跟 `tools/run_real_hardware_flow.py` 兩邊都需要，所以收斂成一支共用。

**Beacon 怎麼認**：`config.json` 的 `landmarks.points[]` 每個點有 `beacon_id`。
比對方式兩種，優先用前者——
(1) `address`（選填）：BLE MAC 位址，最可靠，用 `--list` 掃出來抄進去；
(2) 廣播名稱等於 `beacon_id`：拿 ESP32 自己刷 Beacon 韌體的話，把廣播名稱設成
`GATE-INSIDE` / `GATE-OUTSIDE` 就不用填 address，換一顆板子也不用改設定。

```bash
python3 -m drivers.ble_beacon_scanner --list   # 掃 10 秒，列出附近所有 BLE 裝置
python3 -m drivers.ble_beacon_scanner          # 持續印出門口那兩顆的即時 RSSI
```

**航向交叉驗證什麼時候生效**：只有 `config.json` 的 `landmarks.gate_exit_yaw_deg`
**有填實測值**時才啟用。還沒量的話那欄是 `null`，這一層自動不啟用——拿一個沒校正
過的角度去擋，只會把真的穿越也一起擋掉。量到之後填數字進去就會自動生效。

### 2026-09-15 第一次實測 RSSI：發現三個問題

第一次接上真的 Beacon 掃到的資料（車子距 GATE-OUTSIDE 1.7 m、距 GATE-INSIDE 3.6 m）：

| | 中位數 | 標準差 | 範圍 | 廣播筆數 |
|---|---|---|---|---|
| GATE-INSIDE (`AC:A7:04:33:C7:69`) | −64 dBm | 2.8 dB | −70 ~ −63 | 14 |
| GATE-OUTSIDE (`F4:2D:C9:A1:8E:FA`) | −70 dBm | 2.4 dB | −76 ~ −69 | 43 |

**問題一：兩顆 Beacon 的基準差 12.5 dB，而且方向相反（最嚴重）。**
車子離 OUTSIDE 近 2.1 倍，照自由空間路徑損耗算，OUTSIDE 應該比 INSIDE 強約
6.5 dB；實測卻是 INSIDE 比 OUTSIDE 強 6.0 dB。兩者相加就是 12.5 dB 的系統性偏差
——兩顆是不同廠牌（MAC 前綴不同）、發射功率差很多。

這直接打破差分判定的核心假設（「訊號比較強＝比較近」）。後果是：**車子站在門外側
附近，系統仍會認為它穩穩地在門內側，「出場」判定永遠不會觸發。** 這不是把門檻值
調一調能解決的，必須量出差多少再補回去。

修法：每顆 Beacon 新增 `rssi_offset_db`，比較訊號強弱之前先加上去，用
`tools/calibrate_gate_beacons.py` 在門檻線上量出來。

**問題二：遲滯 2 dB 蓋不過雜訊。** 車子完全靜止時，單筆 RSSI 的峰對峰抖動就有
7 dB、標準差 2.4~2.8 dB；經 window=5 的中位數平滑後殘餘標準差仍有約 1.6 dB。
原本的 `gate_hysteresis_dbm = 2.0` 比雜訊還小，停在門口不動也會反覆翻轉判定。
先提高到 4.0，正式值由校正工具依實測雜訊算。

**問題三：絕對門檻 −65 dBm 實務上過不了。** 距離 Beacon 1.7 m 時只有 −70 dBm，
等於要求貼著 Beacon 才算數。這個值必須在真正的門檻線上量過才有意義。

**另外發現**：兩顆的廣播間隔差 3 倍（OUTSIDE 的筆數是 INSIDE 的 3.1 倍），而且
任一顆走出範圍後就不再有新讀數。所以 `GateMonitor` 加了新鮮度檢查
（`gate_max_sample_age_sec`，預設 5 秒）——兩顆的最近一筆讀數都要夠新才判定，
否則舊值會一直被當成「現在的訊號強度」拿去比較。

### Beacon 實體佈署指南（決定成敗的一章）

這一段是 2026-09-15 實測之後整理的。**軟體校正只能修正兩顆的基準差，修不了
「位置變化造成的訊號差異小於雜訊」這件事**——那是佈署問題，只能用實體佈署解決。

#### 先搞清楚：什麼會讓梯度變陡，什麼不會

差分判定看的是 `ΔRSSI = RSSI內 − RSSI外`。判定準不準，取決於「走幾步路會讓
ΔRSSI 變多少」相對於「站著不動時 ΔRSSI 自己抖多少」。

| 做法 | 對梯度的影響 | 對「不要在店裡別處誤判」的影響 |
|---|---|---|
| **兩顆的間距與擺法** | **決定性的** | 中等 |
| **加指向性（反射板/金屬盒）** | **有效**，直接疊一層方向性衰減 | 有效 |
| 提高廣播頻率、加大平滑窗口 | 不改變梯度，但把雜訊壓低 → 等效變好 | 無 |
| **調低發射功率** | **完全無效** | 有效（縮小偵測範圍） |
| 軟體 `rssi_offset_db` | 不改變梯度，只把判定邊界挪到正確位置 | 無 |

調低發射功率對梯度沒用這件事違反直覺，但很好理解：路徑損耗的斜率跟發射功率
無關。功率降 10 dB，兩顆的讀數**一起**降 10 dB，相減之後完全抵消。它有用的地方
是讓遠處的絕對訊號掉到門檻以下，那是另一層防護。

#### 擺法：間距與方向比什麼都重要

兩顆要沿著**行進方向**前後擺（一顆在門內、一顆在門外），讓推車從兩顆中間穿過。
不要左右分兩側擺——那樣推車經過時兩顆的距離幾乎同時變化，差分值不會翻轉。

間距 D 對梯度的影響（推車沿連線通過，中線附近走 25 cm 的 ΔRSSI 變化量）：

| 間距 D | 走 25 cm 的變化 | 門檻線 ±50 cm 的差分值 |
|---|---|---|
| 1.0 m | 9.5 dB | ±14 dB（±25cm 處已達 ±9.5） |
| **1.5 m** | **6.0 dB** | **±14 dB** |
| 2.0 m | 4.4 dB | ±9.5 dB |
| 3.0 m | 2.9 dB | ±6.0 dB |

**建議 D = 1.0 ~ 1.5 m。** 太近會讓「門檻線」這個判定邊界太窄、推車還沒真的通過
就翻轉；太遠梯度就塌了。

橫向偏移（推車沒有正好走在兩顆的連線上）的容忍度，D=1.5 m：

| 橫向偏移 | 門檻線 ±50 cm 的差分值 |
|---|---|
| 0 cm | ±14.0 dB |
| 30 cm | ±10.3 dB |
| 50 cm | ±7.6 dB |
| 100 cm | ±3.8 dB |
| 150 cm | ±2.2 dB |

門口通道本來就窄，1 m 以內都還夠用。**兩顆的高度要跟推車上 Pi 的天線差不多**，
不要裝在天花板——裝高了等於增加了一段固定距離，把梯度稀釋掉。

> 回頭看你那組實測資料（離 OUTSIDE 1.7 m、離 INSIDE 3.6 m）：那個位置往內側走
> 25 cm，ΔRSSI 只變 1.8 dB，跟平滑後的雜訊 1.6 dB 同量級——難怪分不出來。
> 那不是門口，是遠場。差分法在遠場本來就沒有鑑別力，它只在兩顆的中線附近才陡。

#### 指向性：怎麼做、能期待多少

2.4 GHz 的波長是 12.5 cm，λ/4 ≈ 3.1 cm。這兩個數字決定了所有尺寸。

**作法 A：平板反射器（最簡單）**
在每顆 Beacon 背後 **3 cm**（λ/4）處放一塊金屬板，板子邊長至少 12.5 cm（1λ），
25 cm（2λ）更好。內側那顆的板子朝門外、外側那顆的板子朝門內——也就是各自把
「對面」擋住。預期前後比 10~15 dB，正面還會多 3~6 dB 增益。

**作法 B：金屬盒（效果最好，成本一樣低）**
把 Beacon 放進一個只有單面開口的金屬盒，開口朝自己那一側。餅乾鐵盒、鋁製
專案盒，或紙盒內側整面貼滿鋁箔都可以。預期前後比 15~25 dB。

**作法 C：兩顆之間立一塊擋板**
效果比 A/B 差，因為訊號會從板子邊緣繞射、也會從天花板和牆面反射繞過去。板子要
很大（> 1 m）才有意義。**不建議優先做這個。**

材料：鋁箔貼在瓦楞紙板/珍珠板上就夠了——2.4 GHz 的集膚深度只有幾微米，厚度完全
不是問題。重點是**要連續**：網目或孔洞只要小於 1.2 cm（λ/10）就等同實心板。

**加上指向性之後的預期效果**（D=1.5 m、F/B=15 dB）：

| 位置 | 距離造成 | 遮蔽造成 | 合計 |
|---|---|---|---|
| 門外 25 cm | −6 dB | −15 dB | **−21 dB** |
| 門檻線 | 0 dB | 0 dB | **0 dB** |
| 門內 25 cm | +6 dB | +15 dB | **+21 dB** |

雜訊 1.6 dB → 訊雜比從 3.8 倍拉到 13 倍，`gate_hysteresis_dbm` 設 6~8 都很安全。
注意中線附近不受遮蔽影響（兩邊被擋的量一樣），所以**判定邊界不會偏移**，只是
兩側變得容易分辨——這正是我們要的。

> **誠實的期待值**：做不到你說的「到內無外」。室內多路徑反射的關係，訊號會從
> 天花板、牆面、地板繞過遮蔽物。實務上能拿到 10~20 dB 的對比度改善，不是隔絕。
> 但 20 dB 對這個判定來說已經綽綽有餘。

#### 韌體端：兩個免費的改善

**Arduino UNO R4 WiFi（你的 INSIDE）廣播間隔太長**，這就是掃描筆數只有 ESP32
三分之一的原因（實測 43 : 14）。筆數少 = 平滑窗口填不滿 = 雜訊壓不下來。

```cpp
#include <ArduinoBLE.h>
void setup() {
  BLE.begin();
  BLE.setLocalName("GATE-INSIDE");
  BLE.setDeviceName("GATE-INSIDE");
  BLE.setAdvertisingInterval(32);   // 單位 0.625 ms -> 20 ms，跟 ESP32 對齊
  BLE.advertise();
}
void loop() { BLE.poll(); }
```

> R4 WiFi 的 BLE 是透過板載 ESP32-S3 協處理器做的，`setAdvertisingInterval()`
> 不一定會完全生效。改完務必用 `python3 -m drivers.ble_beacon_scanner` 數一下
> 兩顆的筆數比有沒有拉近，不要假設有效。
> 另外 ArduinoBLE **沒有**設定發射功率的 API，所以 R4 這側的 TX 功率調不了
> ——這也是為什麼軟體的 `rssi_offset_db` 校正非做不可。

**ESP32（你的 OUTSIDE）可以調發射功率與廣播間隔：**

```cpp
#include <BLEDevice.h>
#include "esp_bt.h"

void setup() {
  BLEDevice::init("GATE-OUTSIDE");
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_N12);  // -12 dBm，縮小範圍
  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->setMinInterval(0x20);   // 0x20 * 0.625 ms = 20 ms
  adv->setMaxInterval(0x30);   // 30 ms
  adv->start();
}
```

再提醒一次：降 TX 功率**不會**改善方向判定，只會縮小偵測範圍。它的價值在於讓
店裡其他地方收不到訊號，配合 `gate_min_crossing_rssi_dbm` 這層絕對門檻。

**天線極化方向**也值得檢查：ESP32 用外接天線、R4 用板載天線，兩者的極化方向如果
差 90 度，會有 10~20 dB 的交叉極化損耗，而且推車轉向時損耗會變——這很可能是你那
12.5 dB 系統性偏差的一部分來源。把兩顆的天線盡量擺成同一個方向（通常是都垂直）。

#### 先搞清楚你看到的是哪一層的數字

`python3 -m drivers.ble_beacon_scanner` 印的是**原始讀數**——那支是驅動層，
故意不做任何處理，用途是確認 Beacon 認得到、位址對不對。原始 RSSI 抖動大是正常的，
實測靜止不動時峰對峰就有 7 dB。

但**判定邏輯看到的不是那些數字**。`core/gate_monitor.py` 會做兩件事：
按時間窗取樣、再用 p75 估計量壓成一個代表值。拿你那組實測資料實際跑一遍：

| | 差分值 Δ 的範圍 | 標準差 |
|---|---|---|
| 原始 | +0 ~ +12 dB | 3.19 dB |
| **濾波後** | **+6 ~ +7 dB** | **0.17 dB** |

抖動降低 19 倍。`gate_hysteresis_dbm = 4.0` 遠大於 0.17 dB，靜止時不可能誤翻轉。

要直接看到這件事：

```bash
python3 -m tools.calibrate_gate_beacons --monitor
```

原始與濾波後的值並排顯示，加上目前判定在哪一側、絕對門檻過不過。用的就是
`GateMonitor` 本身（跟 `ui/app_gui.py` 同一條路徑），不是另外算一套。

#### 濾波器怎麼選的（不是預設的中位數）

一般講「RSSI 要平滑」直覺都是中位數或平均，但實測資料顯示這裡的雜訊**是單邊的**：
訊號很少突然變強（最多 +1 dB），但常常突然變弱 6 dB——多路徑破壞性干涉的典型特徵。
所以**上包絡線才接近真正的直達路徑**，下面那些是被抵消掉的。

各估計量在靜止時的殘餘抖動（window=5，用你的實測資料）：

| 估計量 | INSIDE | OUTSIDE | 需要多大的 ΔRSSI 才可靠（3σ） |
|---|---|---|---|
| median | 2.29 dB | 0.60 dB | 7.1 dB ← 最差 |
| mean | 0.89 dB | 0.80 dB | 3.6 dB |
| **p75** | **0.00 dB** | **0.53 dB** | **1.6 dB** |
| p90 | 0.29 dB | 0.46 dB | 1.6 dB |
| max | 0.49 dB | 0.49 dB | 2.1 dB |

中位數對「對稱雜訊」是好選擇，但對單邊雜訊會被下半部的 fade 拉著跑；樣本呈雙峰
分布（一半正常、一半在 fade 中）時還會在兩群之間跳。所以預設改成 **p75**
（`rssi_estimator`）。選 p75 而非 p90/max 是因為它仍用到 1/4 的樣本，單筆異常高
讀數不會主導結果。

**代價是延遲**：p75 取上包絡線，訊號**下降**時會黏在窗口內最強（最舊）的那筆，
延遲約等於一整個窗口長度。所以窗口長度直接對應判定延遲：

> `rssi_smoothing_sec` ≈ 可接受的判定延遲距離 ÷ 推車速度
>
> 1 m/s 配 0.4 秒 = 判定會晚 40 cm。這個延遲是兩邊對稱的（都用同一個估計量），
> 所以只會讓判定變晚、不會判錯方向——安全的失效方向。

窗口縮短的代價是樣本變少、雜訊壓不住，**所以廣播間隔一定要先調到 20~30 ms**
才撐得住 0.4 秒的窗口。濾波器設定跟韌體的廣播頻率是綁在一起的，不能只調一邊。

另外窗口是用**時間**算不是**筆數**：實測兩顆的廣播間隔差 3 倍，固定筆數會讓慢的
那顆涵蓋 3 倍的時間、系統性落後，而落後最嚴重的時候正好是穿越門口、訊號快速變化
的那幾秒。

#### 怎麼知道改了有沒有用：勘測模式

```bash
python3 -m tools.calibrate_gate_beacons --survey
```

沿行進方向量 5 個位置（門外 1 m / 門外 50 cm / 門檻線 / 門內 50 cm / 門內 1 m），
每個位置收 15 秒，最後印出：

- 每個位置的 ΔRSSI 與雜訊
- 整段的梯度、平滑後的殘餘雜訊、**梯度／雜訊比**
- 判讀：≥ 8 很好、≥ 4 堪用、< 4 不夠（並給出優先改善順序）
- 門檻線 ±50 cm 的**局部梯度**，以及據此建議的 `gate_hysteresis_dbm`

**改佈署之前跑一次、之後再跑一次，比較「梯度／雜訊比」就知道值不值得。**
這是回答「加了反射板到底有沒有變好」最直接的方式，不用憑感覺。

#### 建議的施作順序

1. **先改擺法**（免費）：兩顆沿行進方向前後擺、間距 1~1.5 m、高度對齊推車上的 Pi、
   天線同方向。跑 `--survey` 看比值。這一步很可能就夠了。
2. **改韌體廣播間隔**（免費）：把 R4 拉到 20~30 ms，筆數對齊之後雜訊會明顯下降。
3. **還不夠再加指向性**：優先做金屬盒（作法 B），一顆的材料成本幾十塊。
4. 最後跑正式校正寫回 config：`python3 -m tools.calibrate_gate_beacons`

### 門口校正怎麼做

```bash
python3 -m tools.calibrate_gate_beacons --survey     # 先勘測：這組佈署夠不夠用？
python3 -m tools.calibrate_gate_beacons              # 正式校正，問過才寫回
python3 -m tools.calibrate_gate_beacons --dry-run    # 只算給你看
```

流程：把推車停在「門檻線」上（你希望系統判定為『剛好在門口正中間、內外未定』的
位置，**推車姿態與 Pi 擺放方向都要跟實際使用時一樣**，車體金屬的遮蔽影響很大）
→ 收集 15 秒樣本 → 算出讓兩顆在該位置打平的 `rssi_offset_db`，並從實測雜訊推出
建議的遲滯與絕對門檻 → 再把車推到店內深處驗證「在店裡別處不會誤判成在門口」。

> 校正前 `ui/app_gui.py` 啟動時會印警告（兩顆偏移量都是 0＝沒校正過）。
> **軟體校正不能取代實體佈署**：把兩顆 Beacon 的發射功率都調到最低、讓有效範圍
> 侷限在門口通道，才是最有效的一道防護。BLE RSSI 物理上量不到「有沒有真的通過
> 那個開口」，範圍縮小本身就是在替軟體判斷分擔工作。

**驗證狀態**：演算法層（平滑、峰值偵測、雙 Beacon 差分、遲滯、絕對門檻、連續樣本
確認、航向交叉驗證、歷史長度上限）用合成 RSSI 數列測過，見
`tests/test_gate_monitor.py`。**但 `drivers/ble_beacon_scanner.py` 本身還沒有在
真的 Beacon 上跑過**——這個沙盒沒有 BLE 硬體。第一次在 Pi 上接真 Beacon 時，
`gate_hysteresis_dbm`、`gate_min_crossing_rssi_dbm`、`rssi_smoothing_window`
這幾個參數幾乎一定要照實測到的 RSSI 抖動量級重調。

---

## 硬體環境與踩過的坑

### 1. 關閉 Serial Console，確認硬體 UART 節點

```bash
sudo raspi-config   # Interface Options -> Serial Port -> login shell: No, hardware enabled: Yes
```

確認 `/boot/firmware/config.txt` 有 `dtparam=uart0=on`。

**不要假設 `/dev/serial0` 就是接腳 8/10 那組 UART。** 本專案實測過的 Pi 5
（RP1 架構）上 `/dev/serial0` 指到 `ttyAMA10`，跟 GPIO14/15 無關；真正對應接腳的是
`/dev/ttyAMA0`。用 loopback 驗證：

```bash
# 用杜邦線短接 GPIO14(pin 8, TXD) 和 GPIO15(pin 10, RXD)
python3 -c "
import serial, time
s = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
s.write(b'hello\n'); time.sleep(0.1); print(s.read(10))
"
# 應該印出 b'hello\n'
```

每台 Pi / 每次重新燒錄系統都建議重新驗證一次。

### 2. 安裝依賴

```bash
python3 -m venv --system-site-packages venv   # --system-site-packages 讓 picamera2 可見
source venv/bin/activate
pip install -r requirements.txt
sudo apt update && sudo apt install -y python3-picamera2 --no-install-recommends

# pywebview 在 Pi 上如果說找不到 GTK/WebKit：
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
# （找不到 4.1 這個套件名稱的話改試 gir1.2-webkit2-4.0）
```

**venv + sudo 陷阱**：`sudo python3 ...` 預設不繼承目前 shell 的 `PATH`，會跑到系統的
`/usr/bin/python3` 而不是 venv 裡那個，導致明明裝過的套件卻報錯找不到。正確用法：

```bash
sudo venv/bin/python3 -m drivers.barcode_scanner --list
# 或 sudo -E python3 ...（-E 保留環境變數，前提是已 activate venv）
```

> 注意：`ui/app_gui.py` 本身不需要 sudo——只要使用者在 `input` 群組裡就好
> （`sudo usermod -aG input $USER`，做一次、重新登入生效）。上面這個 venv 陷阱
> 只在你真的要用 `sudo` 單獨測 `drivers.barcode_scanner` 時才會遇到。

### 3. 接線

MCU TX → Pi GPIO15/RXD (pin 10)，MCU RX ← Pi GPIO14/TXD (pin 8)，兩板共地。

### 4. 硬體防護

- 裝主動式散熱模組（Phase 3 的即時積分運算對時序穩定度敏感）。
- 測試廣角相機排線最大穩定長度，評估是否需要 CSI 轉接延長。
- PMW3901 SPI 長距離佈線：若訊號不穩，MCC 端可考慮把 SPI 時脈從 1MHz 降到 500kHz
  ——**這是 MCC 產生的檔案，要先在 MCC GUI 裡改，不要直接動生成檔案。**

### 條碼掃描器踩過的坑

這幾個都是 `drivers/barcode_scanner.py` 的，已經修好了，記錄原因供換掃描器時參考：

1. **`evdev.InputDevice` 沒有 `set_nonblocking()` 這個方法**——`python-evdev` 開啟裝置時
   就已經用 `O_NONBLOCK` 開檔案描述符，不需要（也沒有）這個方法可呼叫。
2. **`device.grab()` 會讓某些掃描器的 Caps Lock LED 交握卡死**：這台掃描器送出真正的
   條碼字元前，會先按一次 Caps Lock「詢問」作業系統目前的大小寫 LED 狀態，
   並期待作業系統把狀態 echo 回裝置才會繼續送資料。這個 echo 正常由 Linux 核心的
   `kbd`/`leds` handler 處理，但 `grab()` 把裝置獨佔之後核心 handler 就收不到事件，
   echo 斷掉，裝置誤以為沒人回應、不斷重試（症狀：一刷條碼就每隔 ~50ms 狂閃 Caps Lock，
   直到程式 `ungrab()` 才瞬間把資料送出）。修法是自己呼叫
   `device.set_led(ecodes.LED_CAPSL, ...)` 模擬核心原本會做的 LED echo。
3. **大小寫判斷不能只看 Shift**：這台掃描器實測是拿 Caps Lock 開關本身當大小寫切換
   （不是每次都送 Shift），所以判斷要同時考慮 Shift 和追蹤到的 Caps Lock 狀態
   （標準鍵盤語意：兩者是 XOR，同時開會互相抵銷）。
4. **鍵碼不固定**：同一台掃描器有時用小鍵盤鍵碼（`KEY_KP0`~`KEY_KP9`）送數字、
   有時用標準數字鍵（`KEY_0`~`KEY_9`），對照表兩種都要涵蓋。

診斷新型號掃描器時用 `--debug` 看原始事件（含 `EV_MSC`/`MSC_SCAN` 這個翻譯前的
原始 HID scancode，比翻譯後的鍵碼更可靠），不要用猜的加鍵碼對照：

```bash
sudo venv/bin/python3 -m drivers.barcode_scanner --list
sudo venv/bin/python3 -m drivers.barcode_scanner --device-hint "USBKey" --debug
```

實測這台 Pi 5 上的裝置名稱是 `USBKey Chip USBKey Module`（`/dev/input/event4`），
已寫入 `config.json` 的 `barcode_scanner.device_name_hint = "USBKey"`。

### 相機踩過的坑

```bash
python3 -m drivers.camera_stream --color-test      # 驗證色彩通道順序（拍照肉眼比對）
python3 -m drivers.camera_stream --calibrate --num-images 15 --square-size-mm 24
python3 -m drivers.camera_stream --preview
```

1. **picamera2 的 stream 設定路徑會影響實際輸出**：一開始用 `create_video_configuration()`，
   畫面顏色不對（藍色顯示成橘紅色）；改用 `preview_configuration`
   （`main.format = "RGB888"` + `align()` + `configure("preview")`）之後顏色正常。
   現在全部統一用這條路徑。如果之後又遇到顏色不對，先用 `--color-test` 拍照肉眼驗證，
   不要用猜的加減轉換。
2. **校正預覽視窗需要能開視窗的畫面環境**：純文字 SSH 開不了視窗會自動退回無預覽模式，
   不會讓校正失敗；也可以主動加 `--no-window`。
3. **`qt.qpa.plugin: Could not find the Qt platform plugin "wayland"` 跟字型警告是無害的**。
4. **相機目前是倒吊測試的，不影響校正**：`camera_matrix`/`dist_coeffs` 是鏡頭本身的
   光學特性，跟安裝角度無關，`findChessboardCorners` 也不在意畫面方向。
   等正式安裝角度確定後如果需要正的畫面，用 `cv2.rotate()` 處理方向即可，不用重新校正。

**已完成校正**：`vision/camera_calibration.npz` 已產生，reprojection error = **0.2026**
（遠低於 1.0 的可接受門檻）。

**一個已知的健壯性缺口**：`CameraStream._run_loop()` 沒有 try/except，
`capture_array()` 一旦拋例外執行緒會靜默死亡，而 `_frame_ready` 已經 set 且不會 clear，
於是 `get_latest_frame()` 會一直成功回傳同一張**過期影格**，上層無從察覺相機已掛。
`UartReceiver` 有 `is_stale()` 可以判斷，`CameraStream` 沒有對應機制。Phase 6 真的
開始用相機之前應該補上。

---

## UART 封包格式 v2（2026-09-01，新增 squal 欄位）

```
$SDK,<dx>,<dy>,<squal>,<yaw_deg>,<pitch_deg>,<roll_deg>,<hx711_raw>*<CS>\r\n
```

`squal` 是 PMW3901 的追蹤信心值（0-255），欄位數從 7 個變成 8 個。

**跟舊版不相容**：新舊版互相收到對方的封包都會被判定成「欄位數量不符」直接整包丟棄，
不是靜默解析錯誤——如果封包每包都被丟、log 一直印「欄位數量或 ID 不符」，
先確認 Pi 端與 MCU 端的封包格式版本是不是對不起來。

測試 UART：

```bash
python3 -m drivers.uart_receiver --port /dev/ttyAMA0      # 看即時封包
python3 -m drivers.uart_receiver --csv-log                # 開 CSV logger 收訓練資料
```

**還沒做但值得考慮**：光流校正工具目前的離群值過濾用的是統計方法（MAD），
這是因為原本封包沒有信心值可以參考。現在有了 `squal`，理論上可以直接用
「這筆信心值太低就丟掉」取代/輔助統計濾波，會比較準。

---

## 校正

`config.json` 裡標記 `CALIBRATE_ME` 的數值都還是佔位值。硬體本身都接好、隨時可以跑，
**只是購物車的機構（秤台安裝方式、PMW3901 離地高度）還在調整中**——現在跑出來的值
只對「當下這個安裝方式」準，機構之後再調整就要重新校正。建議現在先用 `--dry-run`
試跑確認流程與數字合理，等機構定案後再正式跑一次寫回。

| 參數 | 校正工具 | 目前值 | 影響 |
|---|---|---|---|
| `weight.hx711_offset` / `hx711_scale` | `tools/calibrate_weight.py` | 0 / 1.0（佔位） | 秤重比對的公克數不準 |
| `odometry.optical_flow_px_to_mm` | `tools/calibrate_optical_flow.py` | 1.0（佔位） | 距離不是真實公釐數 |
| `landmarks.points[].rssi_offset_db` | `tools/calibrate_gate_beacons.py` | 0.0（**未校正**） | 兩顆 Beacon 基準差 12.5 dB，出場判定幾乎不會觸發 |
| `landmarks.gate_hysteresis_dbm` | 同上 | 4.0（暫定） | 太小會在門口反覆翻轉判定 |
| `landmarks.gate_min_crossing_rssi_dbm` | 同上 | −60（佔位） | 太嚴格會完全過不了；太寬鬆會在店裡別處誤判 |
| `landmarks.points[].x_mm/y_mm` | 實測量測後手填 | 0.0（佔位） | 地標校正會把座標釘到錯的地方 |
| `landmarks.gate_exit_yaw_deg` | 實測量測後手填 | `null`（＝停用該層驗證） | 填了才會啟用航向交叉驗證 |
| 相機內參 | `drivers/camera_stream.py --calibrate` | **已完成**（error 0.2026） | — |

```bash
python3 -m tools.calibrate_weight --dry-run          # 只算數值印出來，不寫回
python3 -m tools.calibrate_weight --samples 50
python3 -m tools.calibrate_optical_flow --dry-run
python3 -m tools.calibrate_optical_flow --countdown 5

python3 -m tools.verify_weight                       # 只讀現有值，實測比對誤差
python3 -m tools.verify_optical_flow
```

**重量校正流程**：等 UART 有資料 → 提示「秤台淨空」→ 收集樣本算平均當 `offset`
→ 輸入已知砝碼重量 → 提示「放上砝碼」→ 再收集樣本算
`scale = (加砝碼平均 - offset) / 已知重量`。算完會問要不要寫回 config.json
（`y` 才寫，只更新 `weight` 區塊，其他欄位與註解不受影響）。

**光流校正流程（v2）**：等 UART 有資料 → 按 Enter 開始大字倒數（純視覺提示）→
倒數結束開始記錄，**沒有固定時間窗**，自己按步調推車，推完再按一次 Enter 停止 →
輸入這段實際推了多遠（公分）→ 算 `px_to_mm = 實際距離(mm) / sqrt(sum_dx² + sum_dy²)`。

> **v1 → v2 改動原因**：v1 是「倒數 + 固定時間窗自動收集」，實測發現人手沒辦法
> 剛好在固定秒數內推完固定距離，不是太快就是太慢。v2 拿掉固定時間窗。
>
> **積分方式**：每筆封包的 `dx`/`dy` 本身就是「這個取樣週期的相對位移」（不是累積值），
> 所以直接把記錄期間所有封包的 `dx`/`dy` 加總就是離散版的積分（黎曼和）。
>
> **離群值過濾的已知偏差**：預設會用 MAD 過濾掉位移量異常大的樣本再加總，
> 但被濾掉的樣本代表的位移也一併從總和中消失了，而使用者輸入的實際距離仍涵蓋
> **整段**推行距離——所以只要有樣本被濾掉，算出的 `px_to_mm` 就會**系統性偏大**。
> 預設 MAD×6 很少觸發，但一旦印出「濾掉 N 筆」就要留意；正式校正時建議比對一次
> `--no-filter` 的結果。

**MCU 端已知修正**：PMW3901 之前發現會有休眠現象，導致那段時間的 `dx`/`dy` 遺失或不準；
下位機已經加上看門狗重置修正。這是韌體端的修正，Pi 端不用改，但代表這個修正之前
如果跑過光流校正，那些結果可能受過休眠影響、不完全準。

---

## 測試

```bash
python3 tests/test_app_gui_bridge.py     # 必跑：Bridge 端對端（純 Python）
python3 tests/test_gate_monitor.py       # 必跑：BLE 門口判定邏輯（純 Python）
python3 tests/test_ui_layout.py          # 選用：13 頁版面（需要 playwright）
```

`test_app_gui_bridge.py` 用的是跟正式運作**完全一樣**的三個硬體入口方法
（`on_hardware_barcode` / `on_weight_sample` / `on_gate_crossing`），不是另外開一條
測試專用的旁路。涵蓋：還沒進管制區時掃碼要被拒絕且看得見錯誤、BLE 判定進場後才
真的開始購物、掃碼+秤重比對成功、沒有秤重讀數時掃碼被拒絕、未建檔條碼的訊息、
會員碼/商品碼分類、先變重量再掃碼的移除流程、秤重不符與放棄、鎖定結帳→付款→
BLE 出場→自動登出、感測器斷線恢復、精靈流程，以及警告歷史塞滿 200 筆之後錯誤
仍要浮得上來的回歸測試。

`test_gate_monitor.py` 涵蓋：方向判定、只有單邊訊號時不判定、訊號太弱不觸發、
單筆突波被連續樣本確認擋掉、航向交叉驗證的啟用/擋下/放行三種情況、非門口
Beacon 被忽略、RSSI 歷史不會無限成長。

`test_ui_layout.py` 需要 `pip install playwright && playwright install chromium`，
沒裝就跳過。改過 `tools/build_app_ui.py` 或設計稿之後，重跑這支最快確認有沒有弄壞。

**這三支是目前僅有的自動化測試。** 專案其他部分（里程計、封包解析、校正工具）的
測試都是開發過程中寫在暫存目錄的一次性腳本，跑完就沒有留下來。`parse_packet()`、
`rotate_local_to_global()` 這些純函式最好測，卻一個單元測試都沒有——**這是值得
優先補的洞**。

### 實機整合測試（`tools/run_real_hardware_flow.py`）

在 Phase 5 的 UI 做出來之前，這是唯一能在真推車上跑完整流程的工具。現在 UI 已經
可以直接接真硬體了，但這支仍然有用：它會同時印出 Phase 3 的定位座標，
而且是純終端機操作，適合站在推車旁邊一邊推一邊看數字。

```bash
python3 -m tools.run_real_hardware_flow
python3 -m tools.run_real_hardware_flow --port /dev/ttyAMA0 --barcode-hint USBKey
```

跟 `ui/app_gui.py` 一樣，三個硬體都是硬性條件，任一接不上就結束。

執行中的單字元指令——只有「本來就該由人操作」的那幾個動作，在正式產品裡這些是
觸控螢幕上的按鈕，不是感測器：

```
l = 鎖定結帳                p = 確認付款完成
o = 登出                    f = 強制登出（工作人員）
r = 秤重異常時重試比對        v = 秤重異常時放棄該筆商品
s = 印出目前完整狀態          q = 結束
```

登入/商品掃碼直接刷條碼；進出管制區直接推車通過門口（BLE 自動判定）；
秤重比對背景自動用真實 HX711 數據跑。

**建議的實機測試順序**：

1. 先個別確認 UART 與掃描器本身沒問題（用「硬體環境」那章的個別驗證方式）。
2. 跑起來，看開機訊息：UART 有沒有連上、掃描器有沒有抓到裝置。
3. 刷會員條碼（`MEMBER-` 開頭），確認狀態從未登入轉成「等待進入管制區」。
4. 推車通過門口，確認 BLE 判定出 `entering`、狀態轉成購物中。
   **這是整套裡最沒把握的一步**——RSSI 門檻是照理論值先給的，第一次實測幾乎
   一定要調。判定不出來就先跑 `python3 -m drivers.ble_beacon_scanner`，看推車
   經過時兩顆 Beacon 的 RSSI 實際上各是多少，再回頭調 `config.json` 的
   `gate_min_crossing_rssi_dbm`（絕對門檻）與 `gate_hysteresis_dbm`（遲滯）。
5. 刷商品條碼，把對應重量的東西放上推車，看是不是正確判定比對成功。
   然後**刻意測失敗情境**：不放東西（等逾時）、放錯重量、先拿起來再掃碼（移除流程）、
   拿起來之後故意不掃碼（等 `unscanned_change_timeout_sec` 逾時，確認會跳
   `unscanned_weight_timeout` 而不是被忽略）。
6. `l` → `p` → 推車出場（BLE 判定 `exiting`）→ `o`，跑完一輪，確認每一步的
   中斷/異常都有對應警告。
7. 全程留意 `[警告]`（佔位校正值）與 `recent_alerts`。

> **這一步是最有價值的實機驗證**：秤重比對的時間/誤差門檻
> （`weight_match_timeout_sec`、`weight_tolerance_g`、`unscanned_change_threshold_g`）
> 都是先給的合理預設值，**第一次吃真實秤重雜訊時很可能需要調整**，
> 訊噪比跟合成測試時的假設不一定一樣。

## 尚未完成的功能總表

分三類：純軟體（現在就能做）、需要額外硬體但不需要組好的車、以及排在後面的 Phase。

### A. 純軟體缺口，現在就能做，不需要任何額外硬體

| 項目 | 說明 | 影響 |
|---|---|---|
| **會員偏好持久化** | `members` 表只有 `member_id` / `name`，沒有欄位存過敏原/飲食/宗教/預算/常購清單 | 精靈設定關掉程式就消失；LoadingProfile 的「情境 A」永遠查不到資料；**健康護照做不了** |
| **商品成分/過敏原資料模型** | `products` 表沒有成分或過敏原欄位 | 購物畫面的「含麩質穀物」提醒是寫死的文字，沒有真的比對 |
| **交易紀錄持久化** | 全 repo 沒有任何地方保存已完成的購物 session；`_on_payment_confirmed()` 只改狀態不寫檔 | 付款完成後買了什麼、多少錢、誰買的全部隨程式結束消失；做不了消費紀錄查詢或營運報表 |
| **結帳解鎖事件** | 狀態機沒有「解除 `LOCKED_FOR_CHECKOUT`」的事件 | 結帳畫面的「返回繼續選購」只能跳提示 |
| **呼叫店員協助** | 沒有店員通知機制 | WeightAlert / ExitConfirm 的協助按鈕只跳提示。這個**完全不需要硬體**，可以做成寫入資料庫一筆待處理紀錄 |
| **「我的」分頁** | 完全沒實作 | 點下去只跳提示 |
| **自動化測試** | 只有 `tests/test_app_gui_bridge.py` | 純函式（封包解析、旋轉矩陣、RSSI 偵測器）一個單元測試都沒有 |
| **`CameraStream` 健康檢查** | 擷取執行緒掛掉不會被上層察覺，會一直回傳過期影格 | Phase 6 用相機之前要補 |

### B. 程式寫好了，等實體 Beacon 到手才能驗證

| 項目 | 狀態 | 要做什麼 |
|---|---|---|
| **BLE 門口偵測** | `drivers/ble_beacon_scanner.py` + `core/gate_monitor.py` 都寫好了，演算法層有測試，**但沒在真 Beacon 上跑過** | 準備兩顆低功率 Beacon 裝在門口內外側；用 `--list` 抄位址或把廣播名稱設成 `GATE-INSIDE`/`GATE-OUTSIDE`；然後照實測 RSSI 重調 `gate_min_crossing_rssi_dbm` / `gate_hysteresis_dbm` |
| **航向交叉驗證** | 接線完成（`GateMonitor` 會自動用 UART 的 yaw），但 `gate_exit_yaw_deg` 是 `null` 所以沒啟用 | Beacon 裝好後量一次「車子正朝門外穿越時的 yaw 是幾度」，填進 `config.json` 就會自動生效 |
| **走道地標點** | `RssiPeakDetector` 寫好了，但沒有任何地方呼叫它 | Phase 7 才需要——要先有店面地圖跟多顆 Beacon 佈點 |

### C. 排在後面的 Phase

**Phase 6（`ai/`，完全未開始，只有空的 `__init__.py`，`config.json` 也沒有 AI 相關區塊）**：

- **錢包管家**：目前 Bridge 只有基本的預算上限追蹤（進度條、百分比），沒有分析與建議。
- **健康護照**：需要先有上面 A 類的「會員偏好持久化」+「商品成分資料模型」兩個前置。
- **即時優惠**：優惠頁目前是完全靜態的 mockup，沒有近效期偵測、多件優惠、搭配推薦。
- **異常偵測**：`uart_receiver.py` 的 CSV logger 就是為了收這個的訓練資料而做的
  （預設關閉，`config.json` 的 `csv_logger.enabled`），但模型本身完全沒開始。

**Phase 7（室內導航）**：BLE 那層現在有了，但還缺店面地圖本身的資料結構
（貨架圖節點）跟尋路演算法（A*）的設計，這些都還沒開始。
真要做還需要實體 Beacon 佈點與實際場地。純演算法部分（A* 本身）理論上可以先抽象地寫，
但那比較像提前準備而不是急迫項目。

---

## 前身專題（大四專題.pdf）的功能取捨與 Phase 7 定位

專案前身是另一份大四專題提案（YOLOv8 + 雙鏡頭 + Hailo-8L NPU 的視覺辨識路線）。
現在的 SmartCart_Pi5 在底層方向上已經跟那份提案分道揚鑣——原因是重新蒐集/訓練
視覺模型的成本、開發時程難以配合、YOLOv8 在 Pi5 上可能跑不動。所以：

- **定位**改用 PMW3901 光流 dead-reckoning（不是視覺 SLAM）。
- **防損驗證**改用「掃碼 + HX711 秤重比對」（不是視覺辨識）。

但前身文件第一頁列的四個前端旗艦功能——**室內導航**、**錢包管家**、**健康護照**、
**即時優惠**——希望盡量保留。這四個在 `SmartCart_Pi5_開發總表_v2.docx`（Phase 1~6）
裡完全沒有出現，是額外要排進來的範圍：

- **錢包管家 / 健康護照 / 即時優惠**：這三個不依賴視覺或定位，可以獨立於 Phase 3 的
  進度往前做。**明確不放進 Phase 4**——Phase 4 只做「底層架構的支持」，也就是
  掃碼、秤重比對這類保障流程正確性的基礎設施。這三個是**建立在 Phase 4 之上、
  要融合 LLM 的進階功能**（例如 LLM 推薦後一鍵加入購物清單、根據目前清單做預算分析），
  天生跟 Phase 6 的 `llm_agent.py`（Context Builder：組裝 (X,Y) + 購物清單餵給 LLM，
  回傳結構化 JSON 給 UI）更接近，所以排進 **Phase 6**。
  範圍/資料表結構/UI 呈現的細節還沒討論定案。
- **室內導航**：正式列為 **Phase 7**。原本卡住的「浮動座標系怎麼跟店面地圖對齊」
  現在有具體的候選機制了（`core/landmark_correction.py` 的 BLE 地標校正），
  但軟體邏輯之外還缺 BLE 驅動、實體 Beacon、以及店面地圖本身的設計。

其他路線差異備查：前身的室內導航用 A* + 貨架格狀障礙物地圖，如果 Phase 7 真的要做
會是全新設計（地圖對齊機制不同了，但 A* 演算法本身之後仍可能重用）；
前身的 mmWave 分層喚醒、太陽能輔助充電、Hailo-8L NPU 等，目前開發總表裡沒有對應項目，
暫不在範圍內。

---

## 已在硬體上驗證過的部分

- `drivers/uart_receiver.py`：實際 UART 封包收發、checksum 驗證、CSV logger 都跑過。
  （注意：被驗證的是**解析邏輯**，沿用自前身已實測的 `pi_uart_receiver.py`；
  背景執行緒 + Queue + 自動重連這層重構本身沒有獨立的實機驗證宣稱。）
- `drivers/barcode_scanner.py`：實際刷載具條碼（含大小寫、`/` 符號）都正確解析。
- `drivers/camera_stream.py`：色彩通道、棋盤格校正都在實體相機上驗證過（error 0.2026）。
- `database/db_manager.py`：建表 + 測試資料 seed 跑過。
- `core/cart_state_machine.py`：透過 `tools/run_real_hardware_flow.py` 接真實 UART 秤重
  與真實掃描器跑過整條流程（**閘門那一段當時是鍵盤模擬的，BLE 版還沒實測**）。

## 已知限制 / 尚未驗證

- **`core/odometry_engine.py` 還沒有在真實硬體上實際推車測試過**。累積誤差量級、
  旋轉方向對不對都要實測才能確認。`optical_flow_px_to_mm` 還是佔位值，算出來的距離
  不是真實公釐數。`--min-squal` 該設多少也沒有實測資料可以參考（目前預設 0＝不過濾）。
  yaw 完全信任 IMU，還沒有視覺校正。
- **BLE 那條路完全沒有在真實 Beacon 上驗證過**：`drivers/ble_beacon_scanner.py`
  是新寫的、沒跑過真硬體；`core/gate_monitor.py` 只用合成 RSSI 數列測過。
  `peak_min_rise_dbm` / `gate_hysteresis_dbm` / `gate_min_crossing_rssi_dbm` /
  `rssi_smoothing_window` 全都是先給的合理預設值，**第一次實測幾乎一定要調**。
  `landmarks.points` 的座標與 `gate_exit_yaw_deg` 也都還沒量。
- **BNO080 的 yaw 正負號慣例還沒實機驗證**（`core/position_types.py` 開頭有註明）。
  座標系是否跟韌體端的約定一致，要實機確認。
- **picamera2 的色彩通道順序假設還沒明確驗證**（`drivers/camera_stream.py` 開頭有註明），
  用 `--color-test` 拍一張肉眼比對即可確認。`vision/` 底下如果要用到顏色資訊
  （不只是灰階角點偵測），先確認過這個假設在當下硬體/picamera2 版本上依然成立。
- **秤重比對用的 `standard_weight_g`/`weight_tolerance_g` 目前是測試資料**，
  跟 `hx711_offset`/`hx711_scale` 一樣，都要等秤重機構定案、真的校正過才準確。
- **`MEMBER-` 這個登入條碼前綴是先假設的格式**，等真正的會員卡/App 設計出來要跟著調整。
- **`barcode_scanner.py` 的鍵盤對照表**目前涵蓋數字（含小鍵盤版）、英文字母、`- . /`，
  如果之後條碼包含其他符號，用 `--debug` 看原始鍵碼再補對照。這是唯一的條碼
  路徑，所以對照表漏字就等於那個條碼刷不進來。

- **`CameraStream` 在非 Pi 的開發機上完全無法實例化**，也沒有任何 mock/fake 可以離線測試
  ——跟 odometry / landmark / state_machine「可用假資料測」的專案哲學不一致。
- **任何 import 鏈經過 `drivers/uart_receiver.py` 的模組**（`odometry_engine`、
  `tools/` 下多支、`ui/app_gui.py`）**在沒裝 pyserial 的機器上連 import 都會失敗**
  （該檔頂層 `import serial` 沒有 try/except）。
