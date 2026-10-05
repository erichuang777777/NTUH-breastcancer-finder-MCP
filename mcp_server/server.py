#!/usr/bin/env python3
"""NTUH Breast Cancer Finder — NTUH-system doctor finder + booking MCP.

Public id: ntuh-breastcancer-finder
Scope: 臺大醫療體系院區（總院 T0、兒醫 CH、癌醫 C0、新竹 T4／生醫竹北・竹東 T7、雲林 Y0）
的醫師與診次。乳癌／乳房照護是其中一種用法，搜尋預設不過濾乳專。

Run:
  cd /path/to/NTUH-breastcancer-finder-MCP
  PYTHONPATH=. python -m mcp_server.server

DB: data/ntuh.db  (override with NTUH_DB)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP

from mcp_server.booking import register as register_booking_tools
from mcp_server.doctors import register as register_doctor_tools
from mcp_server.presets import register as register_preset_tools

SERVER_NAME = "ntuh-breastcancer-finder"

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "ntuh-breastcancer-finder — 臺大體系（總院／兒醫／癌醫／新竹・竹東／雲林）"
        "全科醫師搜尋與掛號 MCP（stdio）。不是只限乳癌；乳房照護只是其中一種用法。"
        "資料在本地 data/ntuh.db。預設只查 NTUH hospital_id，不過濾 breast specialty。"
        "醫師：search_doctors / get_doctor / list_leave_or_stops / list_cities / list_hospitals。"
        "診次與燈號：search_bookable / get_live_number（全院區全科；可選 breast_only） / get_progress_pace。不提供取消掛號／退掛。"
        "代掛：register_start 為 dry-run（不 POST）。register_submit 需 autosubmit=true "
        "且 REG_AUTOSUBMIT=1 或 NTUH_REG_AUTOSUBMIT=1，另需 reCAPTCHA v3 "
        "（mint_ntuh_recaptcha）。病人身分只讀 server env PATIENT_ID / PATIENT_BIRTHDATE，"
        "工具參數勿傳完整身分證。"
        "18:00 開放掛號：preset_create / preset_list / preset_arm / preset_cancel；"
        "到點由 scripts/run_preset_at_open.py 以 dry-run 執行（預設不送出）。"
    ),
)

register_doctor_tools(mcp)
register_booking_tools(mcp)
register_preset_tools(mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
