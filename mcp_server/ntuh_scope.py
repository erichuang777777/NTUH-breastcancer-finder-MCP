"""NTUH system campus ids for this MCP.

WebReg ``vHospCode`` (public BranchIndex):

| hospital_id     | code | campus                                      |
| ntuh            | T0   | 總院                                        |
| ntuh_children   | CH   | 兒童醫院                                    |
| ntuh_cancer     | C0   | 癌醫中心分院                                |
| ntuh_hsinchu    | T4   | 新竹醫院                                    |
| ntuh_hsinchu    | T7   | 生醫醫院（竹北；含原竹東院區，無獨立 id）   |
| ntuh_yunlin     | Y0   | 雲林分院（斗六／虎尾）                      |

金山、北護 are also part of the wider NTUH healthcare system but are not in
this v0 campus set.

OpenOnco ``breast_care.db`` stored some of the same campuses under legacy
ids (``nhia_*``, ``h_*``). Those rows are kept and mapped here for booking.
"""

from __future__ import annotations

CANONICAL_IDS: tuple[str, ...] = (
    "ntuh",
    "ntuh_children",
    "ntuh_cancer",
    "ntuh_hsinchu",
    "ntuh_yunlin",
)

# Legacy OpenOnco hospital_id → canonical booking id.
LEGACY_TO_CANONICAL: dict[str, str] = {
    "nhia_0401020013": "ntuh_cancer",
    "nhia_0412040012": "ntuh_hsinchu",
    "nhia_0439010518": "ntuh_yunlin",
    "h_2f3e46a806": "ntuh_yunlin",
}

WEBREG_CODE: dict[str, str] = {
    "ntuh": "T0",
    "ntuh_children": "CH",
    "ntuh_cancer": "C0",
    "ntuh_hsinchu": "T7",  # default 生醫／竹北；T4 via slot notes or NTUH_REG_HOSP_CODE
    "ntuh_yunlin": "Y0",
}


def canonical_hospital_id(hospital_id: str | None) -> str:
    hid = (hospital_id or "").strip()
    return LEGACY_TO_CANONICAL.get(hid, hid)


def is_ntuh_hospital(hospital_id: str | None) -> bool:
    hid = (hospital_id or "").strip()
    if not hid:
        return False
    if hid.startswith("ntuh"):
        return True
    return hid in LEGACY_TO_CANONICAL
