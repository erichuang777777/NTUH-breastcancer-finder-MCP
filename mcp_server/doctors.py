#!/usr/bin/env python3
"""Physician-discovery tools for the ntuh-breastcancer-finder MCP server (READ-ONLY).

Registered onto the shared FastMCP instance by ``mcp_server.server``.
Deprecated entry (warns, then starts the unified server):

  PYTHONPATH=. .venv/bin/python -m mcp_server.doctors
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.db import (
    list_cities as db_list_cities,
    list_hospitals as db_list_hospitals,
    list_leave_or_stops as db_list_leave_or_stops,
    resolve_person_or_doctor,
    search_doctors as db_search_doctors,
    upcoming_slots_summary,
)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _parse_bool(val: bool | str | int | None) -> bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("", "null", "none", "any", "全部"):
        return None
    if s in ("1", "true", "yes", "y", "是"):
        return True
    if s in ("0", "false", "no", "n", "否"):
        return False
    return None


def search_doctors(
    name: str | None = None,
    city: str | None = None,
    hospital: str | None = None,
    specialty: str | None = None,
    is_specialist: bool | str | None = None,
    limit: int = 50,
) -> str:
    """搜尋臺大體系院區醫師（唯讀；全科，預設不過濾乳專；不發明資料）。

    Args:
        name: 醫師中文／英文姓名（模糊）
        city: 縣市或地點關鍵字（比對 hospitals.city／campus／address／name；如「竹北」）
        hospital: 醫院 id 或中文名／院區關鍵字
        specialty: 專科標籤片段，如 breast_surgery / radiation_oncology
        is_specialist: 是否乳專標記（true/false；省略＝不限，這是預設）
        limit: 最多筆數（預設 50，上限 200）
    """
    spec = _parse_bool(is_specialist)

    rows = db_search_doctors(
        name=name or None,
        city=city or None,
        hospital=hospital or None,
        specialty=specialty or None,
        is_specialist=spec,
        limit=limit,
    )

    by_person: dict[str, list[str]] = {}
    for r in rows:
        pid = r.get("person_id")
        if pid:
            by_person.setdefault(pid, []).append(r["doctor_id"])

    return _json(
        {
            "count": len(rows),
            "filter": {
                "name": name,
                "city": city,
                "hospital": hospital,
                "specialty": specialty,
                "is_specialist": spec,
                "limit": limit,
            },
            "person_groups": {
                pid: ids for pid, ids in by_person.items() if len(ids) > 1
            },
            "doctors": rows,
            "hint": (
                "多院區同一人請用 get_doctor(person_id=…) 看全部 sites；"
                "掛號請用本 server 的 search_bookable。"
            ),
        }
    )


def get_doctor(
    person_id: str | None = None,
    doctor_id: str | None = None,
    slots_limit: int = 40,
) -> str:
    """取得醫師詳情：跨院區 sites、專科標籤、簡介（若有）、近期診次摘要、停診。

    Args:
        person_id: 邏輯人 id，如 person:huang_hsiang_wei（優先）
        doctor_id: 單院 doctor_id；若有 person 連結會自動展開全部院區
        slots_limit: 近期診次最多筆數
    """
    if not person_id and not doctor_id:
        return _json(
            {
                "status": "error",
                "message": "請提供 person_id 或 doctor_id",
            }
        )
    try:
        resolved = resolve_person_or_doctor(
            person_id=person_id or None,
            doctor_id=doctor_id or None,
        )
    except ValueError as e:
        return _json({"status": "error", "message": str(e)})

    if not resolved:
        return _json(
            {
                "status": "not_found",
                "person_id": person_id,
                "doctor_id": doctor_id,
            }
        )

    slots = upcoming_slots_summary(
        doctor_ids=resolved["doctor_ids"],
        name_zh=resolved.get("name_zh"),
        limit=slots_limit,
    )
    leaves = db_list_leave_or_stops(
        person_id=resolved.get("person_id"),
        doctor_id=doctor_id if not resolved.get("person_id") else None,
        name_zh=resolved.get("name_zh"),
        limit=30,
    )

    tags: list[str] = []
    seen: set[str] = set()
    for s in resolved["sites"]:
        for t in s.get("specialty_tags") or []:
            if t not in seen:
                seen.add(t)
                tags.append(t)

    return _json(
        {
            "status": "ok",
            "person_id": resolved.get("person_id"),
            "name_zh": resolved.get("name_zh"),
            "name_en": resolved.get("name_en"),
            "notes": resolved.get("notes"),
            "specialty_tags": tags,
            "sites": resolved["sites"],
            "profiles": resolved["profiles"],
            "upcoming_slots": {
                "count": len(slots),
                "slots": slots,
            },
            "leave_or_stops": leaves,
            "hint": "代掛號請用本 server 的 register_start / register_submit（勿傳身分證）。",
        }
    )


def list_leave_or_stops(
    doctor_id: str | None = None,
    person_id: str | None = None,
    name_zh: str | None = None,
    hospital_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> str:
    """查停診／請假。優先 leave_notices；若無資料則改查 clinic_slots status=停診並註明。

    Args:
        doctor_id: 單院醫師 id
        person_id: 邏輯人 id（展開全部連結院區）
        name_zh: 姓名模糊（可與 hospital_id 併用）
        hospital_id: 醫院 slug
        date_from / date_to: YYYY-MM-DD（預設今天～+30d Asia/Taipei）
        limit: 最多筆數
    """
    if not any([doctor_id, person_id, name_zh, hospital_id]):
        return _json(
            {
                "status": "error",
                "message": "請至少提供 doctor_id、person_id、name_zh 或 hospital_id 之一",
            }
        )
    result = db_list_leave_or_stops(
        doctor_id=doctor_id or None,
        person_id=person_id or None,
        name_zh=name_zh or None,
        hospital_id=hospital_id or None,
        date_from=date_from or None,
        date_to=date_to or None,
        limit=limit,
    )
    result["status"] = "ok"
    return _json(result)


def list_cities() -> str:
    """列出資料庫中有地址縣市的醫院城市清單（含醫院數）。"""
    rows = db_list_cities()
    return _json({"count": len(rows), "cities": rows})


def list_hospitals(
    city: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> str:
    """列出／搜尋醫院（可依縣市或關鍵字）。

    Args:
        city: 縣市或地點關鍵字（含 campus／地址，如「竹北」「癌醫」）
        q: 醫院 id／名稱／院區模糊搜尋
        limit: 最多筆數（預設 100，上限 500）
    """
    rows = db_list_hospitals(city=city or None, q=q or None, limit=limit)
    return _json(
        {
            "count": len(rows),
            "filter": {"city": city, "q": q, "limit": limit},
            "hospitals": rows,
        }
    )


_DOCTOR_TOOLS = (
    search_doctors,
    get_doctor,
    list_leave_or_stops,
    list_cities,
    list_hospitals,
)


def register(mcp) -> None:
    """Attach discovery tools to the shared ntuh-breastcancer-finder server."""
    for fn in _DOCTOR_TOOLS:
        mcp.tool()(fn)


def main() -> None:
    """Deprecated dual entrypoint: warn and start ntuh-breastcancer-finder."""
    import warnings

    warnings.warn(
        "mcp_server.doctors is deprecated; tools live on ntuh-breastcancer-finder "
        "(python -m mcp_server.server).",
        DeprecationWarning,
        stacklevel=2,
    )
    print(
        "DEPRECATED: mcp_server.doctors merged into ntuh-breastcancer-finder. "
        "Use: python -m mcp_server.server",
        file=sys.stderr,
    )
    from mcp_server.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
