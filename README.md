# NTUH Breast Cancer Finder MCP

臺大體系**全院區、全科**醫師搜尋與網路掛號 MCP（public id：`ntuh-breastcancer-finder`）。

Repo 名稱仍是 `NTUH-breastcancer-finder-MCP`。**乳癌／乳房照護是其中一種用法，不是預設過濾。** `search_doctors` 預設不會加上 `is_breast_specialist`；`specialty` 會比對專科標籤、科別名稱與備註（例如「放射」對到總院「影像醫學部」，「麻醉」對到「麻醉部」）。

名冊與可掛診次來自公開 [臺大網路掛號 WebReg](https://reg.ntuh.gov.tw/) 各院區「依科別掛號」的 `RegDeptSchedule`（目前開放的掛號窗，約兩週），不是人事名冊。乳專標記若舊資料已有則保留，但不會把非乳專醫師濾掉。

## 院區（2026-10-05 匯入，Asia/Taipei）

| hospital_id | WebReg | 院區 | 醫師 | 診次（可掛） | 科別數 |
| --- | --- | --- | --- | --- | --- |
| `ntuh` | T0 | 總院 | 815 | 2288（1307 可掛號） | 31 |
| `ntuh_children` | CH | 兒童醫院 | 136 | 408（247） | 28 |
| `ntuh_cancer` | C0 | 癌醫中心分院 | 240 | 681（352） | 39 |
| `ntuh_hsinchu` | T4／T7 | 新竹醫院／生醫（竹北，**含竹東**） | 413 | 1700（1171）；T4 871 診、T7 829 診 | 20 |
| `ntuh_yunlin` | Y0 | 雲林（斗六／虎尾） | 206 | 1052（753） | 34 |

診次日期窗：2026-10-05～2026-10-19。同一人在同一院區合成一筆 `doctor_id`（`{hospital_id}:{姓名}`）。新竹 T4 與 T7 共用 `ntuh_hsinchu`，診次備註與 `registration_url` 帶 `vHospCode`。竹東沒有獨立 `hospital_id`。

總院可掛科別含 **影像醫學部（放射科）** 與 **麻醉部（麻醉科）**。癌醫兩者都有診；雲林有影像醫學部門診、麻醉部此窗 0 診。新竹 WebReg 科別清單有麻醉部，**沒有影像／放射門診表**。

舊 id（仍在 DB，掛號時對到上表）：`nhia_0401020013`→癌醫、`nhia_0412040012`→新竹、`nhia_0439010518` 與 `h_2f3e46a806`→雲林。金山、北護不在 WebReg 這組院區碼裡。

重新抓全科（公開頁，不需登入；證書若驗證失敗會改 `verify=False`，與掛號 adapter 相同）：

```bash
PYTHONPATH=. python scripts/import_ntuh_webreg_catalog.py
# 單一院區： --campus T0
```

OpenOnco 乳癌庫仍可用 `scripts/export_ntuh_db.py` 重建骨架，但那不是全科；全科請再跑上面的 WebReg 匯入。

## 半成品限制

- 這是 WebReg **目前開放掛號窗**裡、各科門診表上的醫師，不是全職員工名冊。沒有門診的醫師不會出現。`RegForm?newx=` 時效短，不寫進 DB；`register_start` 會重抓該科班表，用姓名 + 日期 + 午別 + 預約碼（`local_clinic_code`）對到掛號鈕。
- 此窗 0 診的科（仍在科別連結裡）：總院傷口造護小組、預立醫療照護諮商；雲林麻醉部、虎尾影像醫學部。新竹兩院區的 WebReg 沒有影像醫學部／放射科入口。
- 即時燈號 adapter（`get_live_number` 打網路）目前只掛 `ntuh`、`ntuh_cancer`、`ntuh_hsinchu`，而且沿用 OpenOnco 的乳房門診抓取。`from_db=true` 只讀已存列。
- 預設 **dry-run／不自動送出**。真正 POST 仍要 `REG_AUTOSUBMIT=1` 或 `NTUH_REG_AUTOSUBMIT=1`，以及 NTUH reCAPTCHA v3（`mint_ntuh_recaptcha` 或 `NTUH_RECAPTCHA_TOKEN`）。見 [`docs/NTUH_WEBREG_FLOW.md`](docs/NTUH_WEBREG_FLOW.md)。
- 不要把身分證、生日、`patient.env`、`.env`、`schedules/raw/` 提交進 git。

## 執行 MCP

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m mcp_server.server
```

`requirements.txt` 含掛號用的 `ddddocr`、`playwright`（reCAPTCHA）。只測 import 時至少需要 `mcp`。

### Cursor `mcpServers`

路徑改成你的 clone。`PATIENT_*` 用本機 secret 填，**不要 commit**。

```json
{
  "mcpServers": {
    "ntuh-breastcancer-finder": {
      "command": "/absolute/path/NTUH-breastcancer-finder-MCP/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/absolute/path/NTUH-breastcancer-finder-MCP",
      "env": {
        "PYTHONPATH": "/absolute/path/NTUH-breastcancer-finder-MCP",
        "NTUH_DB": "/absolute/path/NTUH-breastcancer-finder-MCP/data/ntuh.db",
        "PATIENT_ID": "",
        "PATIENT_BIRTHDATE": "",
        "NTUH_REG_AUTOSUBMIT": "0",
        "REG_AUTOSUBMIT": "0"
      }
    }
  }
}
```

範本： [`.env.example`](.env.example)。工具說明：[`docs/MCP.md`](docs/MCP.md)。

## 18:00 開放掛號 preset

臺大網路掛號常在門診前一日 **18:00（Asia/Taipei）** 再開放當日名額。Preset 先把意圖存進 `booking_presets`（在 `data/ntuh.db`），到點再跑。

1. MCP `preset_create`（`dry_run` 預設 `true`，`open_at` 預設下一個 18:00）
2. `preset_arm`
3. 執行：

```bash
PYTHONPATH=. python scripts/run_preset_at_open.py --preset-id preset_xxx
# 測試不等 18:00：
PYTHONPATH=. python scripts/run_preset_at_open.py --preset-id preset_xxx --now
```

Runner 預設只呼叫 `register_start`（不 POST）。`--allow-live` 只有在該筆 `dry_run=false` 時才會進 `register_submit`，而且 autosubmit 與 reCAPTCHA 閘門照舊。

`preset_list` / `preset_cancel` 可查與取消（`running` 中不能取消）。

## 工具一覽

搜尋：`search_doctors`、`get_doctor`、`list_leave_or_stops`、`list_cities`、`list_hospitals`  
掛號：`search_bookable`、`get_live_number`、`get_progress_pace`、`register_start`、`register_submit`、`mint_ntuh_recaptcha`  
Preset：`preset_create`、`preset_list`、`preset_arm`、`preset_cancel`
