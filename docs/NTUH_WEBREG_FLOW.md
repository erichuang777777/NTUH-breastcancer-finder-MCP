<!-- Used by ntuh-breastcancer-finder. Campus set for this MCP: T0 總院、CH 兒醫、C0 癌醫、T4 新竹、T7 生醫（竹北／竹東）、Y0 雲林。 -->

# 臺大醫院 WebReg 代掛號流程對照（NTUH family）

**Date:** 2026-10-02 Asia/Taipei  
**Scope:** `ntuh` (T0 總院)、`ntuh_cancer` (C0 癌醫)、`ntuh_hsinchu` (T4 新竹／T7 竹北生醫)  
**Code:** `schema/registration/`（與 schedule `HospitalAdapter` 分離）

---

## 1. 入口 URL

| 步驟 | URL | 說明 |
| --- | --- | --- |
| Hub | `https://reg.ntuh.gov.tw/` | 全家族入口 |
| 院區 | `…/WebReg/WebReg/BranchIndex?vHospCode={T0\|C0\|T4\|T7\|…}` | 院區首頁 |
| 科別 | `…/RegShowBlock?vHospCode=` → `RegDeptInfo?…&showBlock=` | 選科 |
| 診次表 | `…/RegDeptSchedule?vhospCode=&showBlock=&vDeptCode=` | **ClinicSlot deep link 來源** |
| 依醫師 | `…/RegByDrName?vHospCode=` | 勾選院區 + 醫師姓名搜尋 |
| 掛號表單 | `…/RegForm?newx=<opaque token>` | 可掛診次按鈕 `onclick` 深鏈 |
| 進度 | `…/ClinicCurrentLightNo?vHospCode=` | 全院；需科別過濾 |
| 進度明細 | `…/ClinicCurrentLightNoDetail?ServiceIDSE=&vHospitalCode=` | 目前燈號／已叫最大號／預計叫號 |

### 乳房相關 RegDeptSchedule（公開）

| 院區 | 範例 |
| --- | --- |
| C0 癌醫 | `RegDeptSchedule?vhospCode=C0&showBlock=H&vDeptCode=KBRC&realSubDeptCode=` |
| T7 竹北 | `RegDeptSchedule?vhospCode=T7&showBlock=E&vDeptCode=KBRV&realSubDeptCode=` |
| T4 新竹 | `RegDeptSchedule?vhospCode=T4&showBlock=B&vDeptCode=SURG&realSubDeptCode=`（診別含「乳房」） |

網掛開放約 **兩週內** 診次；不可發明格子。

---

## 2. End-to-end 步驟（從 ClinicSlot）

1. **取得可掛深鏈**  
   GET `RegDeptSchedule`（或 slot 的 `source_url` / `registration_url`）。  
   可掛號按鈕：`button.doctor-tag` 且 `onclick="window.location.href='RegForm?newx=…'"`（class 常含 `avaliable`）。  
   額滿／停診通常只有 modal、無 `RegForm` onclick。

2. **建立 session cookies**（同 host）  
   - `ASP.NET_SessionId`  
   - `__RequestVerificationToken_L1dlYlJlZw2`（cookie）  
   - `Localization.CurrentUICulture=zh-TW`  
   - `TBMCookie_*` / `___utmvm`（WAF／分析；跟 session 走即可）  
   表單內另有 hidden `__RequestVerificationToken`（anti-forgery，**POST 必帶**）。

3. **GET RegForm?newx=…**  
   顯示診次摘要（`clinicHint`）、醫師照片、身分表單、圖形驗證碼、reCAPTCHA v3。

4. **填寫身分**（見下節欄位）→ 圖形驗證碼 →（瀏覽器）`grecaptcha.execute` → POST。

5. **送出後**  
   成功／確認頁（文案含掛號成功、看診號碼等）或錯誤（驗證碼／額滿）。  
   **初診**可能另需補姓名／電話（`PATIENT_NAME` / `PATIENT_PHONE`）；現況主路徑以複診身分證＋生日為主，初診 follow-up 為下一階段。

6. **無 SMS／OTP**（2026-10-02 探測）：身分頁未見簡訊驗證；僅有圖形碼 + Google reCAPTCHA v3。

---

## 3. RegForm 欄位

| name | 說明 |
| --- | --- |
| `__RequestVerificationToken` | CSRF |
| `clinicHint` | 診次摘要 HTML（hidden） |
| `urlNewx` | newx token（hidden） |
| `radInputType` | `personID` \| `chartNo` \| `other` \| `newBorn` |
| `usrNationValue` | 國籍，預設 `TWN` |
| `txtInputID` | 證件／病歷號 |
| `vHospCode` | 如 `C0` |
| `txtBirthday_Year` / `_Month` / `_Day` | 國曆年月日（月日可無前導零） |
| `txtVerifyCode` | 圖形驗證碼（不分大小寫） |
| `gRecaptchaToken` | reCAPTCHA v3 token |
| `btnAction` | `formSubmit`（確定送出）或 `reBtn`（換圖） |

**證件類型 UI：** 身分證 10 碼／病歷 7 碼／其他／新生兒初診。

---

## 4. Captcha

| 種類 | 端點／金鑰 | 解法 |
| --- | --- | --- |
| 圖形 | `ValidNumerImage?inputText=…` → `image/png` | **ddddocr**（`schema/registration/captcha_ddddocr.py`） |
| reCAPTCHA v3 | site key `6LfudEshAAAAAMe_Ny_0xh2rjwsIZCr3Blzr-J4T`，action=`submit` | **無法用 ddddocr**。需瀏覽器執行 `grecaptcha.execute`，或事先注入 env `NTUH_RECAPTCHA_TOKEN` |

**Blocker（live submit）：** 無有效 `gRecaptchaToken` 時伺服器會拒絕。Dry-run 預設不 POST。

---

## 5. 環境變數（PII 只走 env／secrets）

**必填**

| Env | 說明 |
| --- | --- |
| `PATIENT_ID` | 完整身分證／病歷／其他證件號（**禁止寫死在程式**） |
| `PATIENT_BIRTHDATE` | `YYYY-MM-DD`（國曆） |

**選填**

| Env | 說明 |
| --- | --- |
| `PATIENT_ID_TYPE` | `personID`（預設）\|`chartNo`\|`other`\|`newBorn` |
| `PATIENT_ID_LAST4` | 與 `PATIENT_ID` 後四碼核對 |
| `PATIENT_NAME` | 初診 follow-up |
| `PATIENT_PHONE` | 初診 follow-up |
| `PATIENT_NATION` | 預設 `TWN` |
| `NTUH_REG_AUTOSUBMIT` | 設為 `1` 才允許真正 POST |
| `NTUH_RECAPTCHA_TOKEN` | 預先取得的 v3 token |
| `NTUH_REG_HOSP_CODE` | 強制院區碼 `T0`/`CH`/`C0`/`T4`/`T7`/`Y0` |

---

## 6. Dry-run vs Submit

```bash
cd /path/to/NTUH-breastcancer-finder-MCP
# Dry-run（預設）：導航 → 解析表單 → ddddocr → 組 payload → 存 artifacts，不 POST
# export PATIENT_ID and PATIENT_BIRTHDATE from your local secret file — do not commit values
.venv/bin/python scripts/dry_run_ntuh_reg.py --hospital ntuh_cancer --prefer-open

# 真送出（需另備 reCAPTCHA token）
export NTUH_REG_AUTOSUBMIT=1
export NTUH_RECAPTCHA_TOKEN='...'
.venv/bin/python scripts/dry_run_ntuh_reg.py --hospital ntuh_cancer --prefer-open --submit
```

Artifacts：`schedules/raw/ntuh_reg_dryrun/`  
（schedule HTML、RegForm HTML、captcha PNG、redacted payload JSON、submit 結果 HTML）

---

## 7. 看診進度（live_progress）

- 列表：`POST DeptLightTable`（`vHospitalCode`, `DeptCode`, `AmpmCode` 1/2/3）  
- 過濾：C0/T0 → `KBRC`；T7 → `KBRV`；T4 → `SURG` + 乳房關鍵字／名冊 allowlist  
- 明細：`ClinicCurrentLightNoDetail` → `.now-number` / `.biggest-number` / `.next-number`  
- 實作：`schema/adapters/ntuh_progress.py`；三院 adapter 的 `fetch_live_progress()` 已接通  
- SQLite：`live_progress`（見 `scripts/lib/schema_sql.py`、`SCHEMA.md`）

夜間無門診時燈號常為 `------`（正規化為 `null`）。

---

## 8. 已知限制 / 下一步

1. **reCAPTCHA v3**：`scripts/mint_ntuh_recaptcha.py` / MCP `mint_ntuh_recaptcha`（Playwright）；或 headed console `grecaptcha.execute`；autosubmit 且無 env token 時 adapter 會嘗試自動 mint。  
2. 初診第二頁（姓名／電話）尚未自動化。  
3. `newx` token 有時效，須從當次 schedule 頁現抓。  
4. MCP：單一 `openonco-breast-finder`（`mcp_server/server.py`；見 `mcp_server/README.md`、`docs/MCP.md`）。  
5. 僅公開頁；代掛須病人授權；勿把 PII 寫進 git／log（僅 redacted）。
