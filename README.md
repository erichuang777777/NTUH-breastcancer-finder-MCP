# NTUH Breast Cancer Finder MCP

臺大體系多院區醫師搜尋與網路掛號 MCP（public id：`ntuh-breastcancer-finder`）。

Repo 名稱仍是 `NTUH-breastcancer-finder-MCP`。**乳癌／乳房照護是其中一種用法，不是預設過濾。** 搜尋與可掛診次預設涵蓋本地 DB 裡所有 NTUH 體系列，不會自動加上 `is_breast_specialist`。

從 [OpenOnco Breast Finder](https://github.com/erichuang777777) 的 `breast-cancer-doctors` 削出 NTUH 掛號與搜尋程式，並把名冊／診次合併進本 repo 的 `data/ntuh.db`。

## 院區

| hospital_id | WebReg | 院區 | 本版 DB |
| --- | --- | --- | --- |
| `ntuh` | T0 | 總院 | 有醫師與診次（來源以乳房相關為主，另有影像／復健等） |
| `ntuh_children` | CH | 兒童醫院 | **只有院區目錄**，尚無名冊／診次 |
| `ntuh_cancer` | C0 | 癌醫中心分院 | 有醫師與診次 |
| `ntuh_hsinchu` | T4／T7 | 新竹醫院／生醫（竹北，**含原竹東院區**） | 有醫師與診次。竹東沒有獨立 `hospital_id` |
| `ntuh_yunlin` | Y0 | 雲林分院（斗六／虎尾） | canonical 列是目錄；乳癌名冊片段在舊 id |

舊 id（仍在 DB，掛號時對到上表）：`nhia_0401020013`→癌醫、`nhia_0412040012`→新竹、`nhia_0439010518` 與 `h_2f3e46a806`→雲林。

金山、北護不在這版範圍。

重新匯出：

```bash
SOURCE_DB=/path/to/breast_care.db PYTHONPATH=. python scripts/export_ntuh_db.py
```

## 半成品限制

- `data/ntuh.db` 是 OpenOnco `breast_care.db` 裡**院名屬於臺大體系**的列，不是臺大全院、全科、即時門診表。兒醫沒有列；雲林只有少數舊 id 醫師、沒有 `clinic_slots`。
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
