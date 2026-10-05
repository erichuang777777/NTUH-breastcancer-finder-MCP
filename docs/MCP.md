# NTUH finder MCP（ntuh-breastcancer-finder）

**Public id:** `ntuh-breastcancer-finder`  
**Entry:** `python -m mcp_server.server`  
**DB:** `data/ntuh.db`（env `NTUH_DB`，相容舊名 `BREAST_CARE_DB`）

臺大體系多院區、**全科**醫師／診次／掛號。乳癌只是其中一種查詢（把 `specialty=breast_surgery` 或 `is_specialist=true` 加上去），預設不過濾乳房專科。

範圍（WebReg `vHospCode`）：

| hospital_id | code | 院區 |
| --- | --- | --- |
| `ntuh` | T0 | 總院 |
| `ntuh_children` | CH | 兒童醫院 |
| `ntuh_cancer` | C0 | 癌醫中心分院 |
| `ntuh_beihu` | T2 | 北護分院 |
| `ntuh_jinshan` | T3 | 金山分院 |
| `ntuh_hsinchu` | T4／T7 | 新竹醫院／生醫（竹北，含原竹東院區） |
| `ntuh_yunlin` | Y0 | 雲林分院（斗六／虎尾） |

舊 OpenOnco id（`nhia_0401020013` 癌醫、`nhia_0412040012` 新竹、`nhia_0439010518` 與 `h_2f3e46a806` 雲林）若在 DB 裡會一併被搜尋，掛號時對到上表的 canonical id。

流程對照：[`NTUH_WEBREG_FLOW.md`](NTUH_WEBREG_FLOW.md)。掛號程式從 OpenOnco Breast Finder 削出。全院區全科名冊／診次以 `scripts/import_ntuh_webreg_catalog.py` 從公開 WebReg 匯入 `data/ntuh.db`（`export_ntuh_db.py` 只重建乳癌骨架）。

PII：工具參數禁止傳完整身分證。Server 讀 `PATIENT_ID`、`PATIENT_BIRTHDATE`。

---

## Discovery

`search_doctors`、`get_doctor`、`list_leave_or_stops`、`list_cities`、`list_hospitals`。

`search_doctors` 參數：`name`、`city`、`hospital`、`specialty`（可選）、`is_specialist`（可選；省略＝不限）、`limit`。結果一律限 NTUH 體系列。

## Booking

| Tool | 說明 |
| --- | --- |
| `search_bookable` | `clinic_slots`。省略 `hospital_id`＝所有 NTUH 列。不過濾乳專。 |
| `get_live_number` | 只接受 NTUH id。排程 adapter 目前只有 `ntuh`／`ntuh_cancer`／`ntuh_hsinchu`（乳房門診來源）。`from_db=true` 讀已存燈號。 |
| `get_progress_pace` | `live_progress_history` |
| `register_start` | dry-run，不 POST，回 `confirm_token` |
| `register_submit` | 須 `autosubmit=true` **且** `REG_AUTOSUBMIT=1` 或 `NTUH_REG_AUTOSUBMIT=1`，NTUH 另需 reCAPTCHA |
| `mint_ntuh_recaptcha` | Playwright 取 v3 token |

## 18:00 preset

| Tool | 說明 |
| --- | --- |
| `preset_create` | 儲存掛號意圖。`open_at` 預設下一個 18:00 Asia/Taipei。`dry_run` 預設 true |
| `preset_list` | 依 status／hospital 列出 |
| `preset_arm` | `pending` 或 `failed` → `armed` |
| `preset_cancel` | 非 `running` 可取消 |

狀態：`pending` | `armed` | `running` | `done` | `failed` | `cancelled`。

到點執行（預設不送出）：

```bash
PYTHONPATH=. python scripts/run_preset_at_open.py --preset-id preset_xxx
PYTHONPATH=. python scripts/run_preset_at_open.py --preset-id preset_xxx --now
```

`--allow-live` 只有在該筆 `dry_run=false` 時才會呼叫 `register_submit`，而且既有的 autosubmit／reCAPTCHA 閘門仍然有效。
