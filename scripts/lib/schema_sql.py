"""SQLite DDL aligned to schema/SCHEMA.md (+ LeaveNotice, DoctorProfile, scrape_runs)."""

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hospitals (
  hospital_id TEXT PRIMARY KEY,
  name_zh TEXT NOT NULL,
  name_en TEXT,
  aliases_json TEXT,              -- JSON string[]
  campus TEXT,
  city TEXT,                      -- 縣市 e.g. 臺北市
  address TEXT,                   -- 街道門牌
  system_family TEXT,
  nhia_code TEXT,
  registration_hub_url TEXT,
  progress_hub_url TEXT,
  roster_url TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
  adapter_id TEXT,
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS hospital_aliases (
  alias TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL REFERENCES hospitals(hospital_id),
  canonical_name_zh TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doctors (
  doctor_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL REFERENCES hospitals(hospital_id),
  name_zh TEXT NOT NULL,
  name_en TEXT,
  local_doctor_code TEXT,
  department_zh TEXT,
  specialty_tags_json TEXT NOT NULL,  -- JSON string[]
  is_breast_specialist INTEGER,       -- 1=BCST/TOPBS, 0=intro-only, NULL=unknown
  source_url TEXT,
  source_type TEXT,                   -- society | hospital_page | schedule | manual
  registration_url TEXT,
  progress_url TEXT,
  notes TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_doctors_hospital ON doctors(hospital_id);
CREATE INDEX IF NOT EXISTS idx_doctors_name ON doctors(name_zh);
CREATE INDEX IF NOT EXISTS idx_doctors_specialist ON doctors(is_breast_specialist);

CREATE TABLE IF NOT EXISTS clinic_slots (
  slot_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT NOT NULL,
  name_zh TEXT NOT NULL,
  department_zh TEXT,
  clinic_date TEXT NOT NULL,          -- YYYY-MM-DD
  weekday TEXT,
  session TEXT NOT NULL,              -- 上午|下午|夜間
  local_clinic_code TEXT,
  status TEXT NOT NULL,               -- 可掛號|額滿|停診|未知
  specialty_tag TEXT NOT NULL,
  registration_url TEXT,
  progress_url TEXT,
  source_url TEXT NOT NULL,
  scraped_at TEXT NOT NULL,
  notes TEXT,
  booked_count INTEGER,               -- 已掛號人數 if published
  max_quota INTEGER,                  -- 上限／總號源 if published
  remaining_count INTEGER             -- 剩餘可掛 if published
);

CREATE INDEX IF NOT EXISTS idx_slots_date ON clinic_slots(clinic_date);
CREATE INDEX IF NOT EXISTS idx_slots_doctor ON clinic_slots(doctor_id);
CREATE INDEX IF NOT EXISTS idx_slots_hospital ON clinic_slots(hospital_id);

CREATE TABLE IF NOT EXISTS registration_links (
  link_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  kind TEXT NOT NULL,                 -- hub|dept|doctor|slot|progress
  url TEXT NOT NULL,
  label_zh TEXT,
  timetable_how TEXT,
  valid_from TEXT,
  valid_to TEXT,
  source_url TEXT,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_reg_hospital ON registration_links(hospital_id);

CREATE TABLE IF NOT EXISTS live_progress (
  progress_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  name_zh TEXT,
  department_zh TEXT,
  clinic_date TEXT NOT NULL,          -- YYYY-MM-DD
  session TEXT,                      -- 上午|下午|夜間
  local_clinic_code TEXT,
  current_number TEXT,               -- int or string as published
  next_number TEXT,
  max_number TEXT,
  waiting_count INTEGER,
  status_text TEXT,
  specialty_tag TEXT,
  source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  raw_json TEXT                      -- optional debug JSON
);

CREATE INDEX IF NOT EXISTS idx_progress_date ON live_progress(clinic_date);
CREATE INDEX IF NOT EXISTS idx_progress_hospital ON live_progress(hospital_id);
CREATE INDEX IF NOT EXISTS idx_progress_doctor ON live_progress(doctor_id);

-- Stub: future leave / 停診 notices from hospital boards
CREATE TABLE IF NOT EXISTS leave_notices (
  notice_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  name_zh TEXT,
  leave_date TEXT,                    -- YYYY-MM-DD
  session TEXT,
  reason_zh TEXT,
  source_url TEXT,
  scraped_at TEXT,
  notes TEXT
);

-- Stub: future rich bio pages
CREATE TABLE IF NOT EXISTS doctor_profiles (
  doctor_id TEXT PRIMARY KEY REFERENCES doctors(doctor_id),
  bio_zh TEXT,
  specialties_raw TEXT,
  education_zh TEXT,
  experience_zh TEXT,
  photo_url TEXT,
  profile_url TEXT,
  scraped_at TEXT,
  raw_json TEXT
);

-- Multi-site logical person (same physician across campuses)
CREATE TABLE IF NOT EXISTS doctor_persons (
  person_id TEXT PRIMARY KEY,
  name_zh TEXT NOT NULL,
  name_en TEXT,
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS doctor_person_links (
  doctor_id TEXT PRIMARY KEY REFERENCES doctors(doctor_id),
  person_id TEXT NOT NULL REFERENCES doctor_persons(person_id),
  hospital_id TEXT NOT NULL,
  is_primary INTEGER DEFAULT 0,
  notes TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_person_links_person ON doctor_person_links(person_id);
CREATE INDEX IF NOT EXISTS idx_person_links_hospital ON doctor_person_links(hospital_id);


CREATE TABLE IF NOT EXISTS live_progress_history (
  hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
  progress_id TEXT NOT NULL,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  name_zh TEXT,
  department_zh TEXT,
  clinic_date TEXT NOT NULL,
  session TEXT,
  local_clinic_code TEXT,
  current_number TEXT,
  next_number TEXT,
  max_number TEXT,
  waiting_count INTEGER,
  status_text TEXT,
  specialty_tag TEXT,
  source_url TEXT,
  fetched_at TEXT NOT NULL,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_lph_progress ON live_progress_history(progress_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_lph_hospital_date ON live_progress_history(hospital_id, clinic_date);
CREATE INDEX IF NOT EXISTS idx_lph_doctor ON live_progress_history(doctor_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_lph_name ON live_progress_history(hospital_id, name_zh, clinic_date);


-- Public visit-experience / review snippets (not clinic-slot core)
CREATE TABLE IF NOT EXISTS doctor_reviews (
  review_id TEXT PRIMARY KEY,
  doctor_id TEXT,                     -- nullable when only a name match is safe
  hospital_id TEXT NOT NULL,
  name_zh TEXT NOT NULL,
  person_id TEXT,                     -- from doctor_person_links when linked
  source TEXT NOT NULL,               -- google_maps|article|forum|hospital_site|other
  source_url TEXT NOT NULL,
  title TEXT,
  excerpt TEXT NOT NULL,              -- short public snippet, not a full article
  rating REAL,                        -- null unless the page prints a number
  published_at TEXT,
  scraped_at TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'zh-Hant',
  raw_json TEXT,
  disambiguation_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_doctor ON doctor_reviews(doctor_id);
CREATE INDEX IF NOT EXISTS idx_reviews_hospital ON doctor_reviews(hospital_id);
CREATE INDEX IF NOT EXISTS idx_reviews_source ON doctor_reviews(source);
CREATE INDEX IF NOT EXISTS idx_reviews_name ON doctor_reviews(name_zh);
CREATE INDEX IF NOT EXISTS idx_reviews_url ON doctor_reviews(source_url);

CREATE TABLE IF NOT EXISTS scrape_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  kind TEXT NOT NULL,                 -- society|roster|schedule|registration|import|leave|profile|review
  hospital_id TEXT,
  status TEXT NOT NULL,               -- ok|partial|error
  rows_upserted INTEGER DEFAULT 0,
  message TEXT,
  meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_kind ON scrape_runs(kind, started_at);

CREATE TABLE IF NOT EXISTS booking_presets (
  preset_id TEXT PRIMARY KEY,
  doctor_name TEXT,
  doctor_id TEXT,
  hospital_id TEXT NOT NULL,
  dept TEXT,
  clinic TEXT,
  preferred_dates TEXT,
  sessions TEXT,
  open_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  dry_run INTEGER NOT NULL DEFAULT 1,
  slot_id TEXT,
  registration_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT,
  last_message TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_presets_status ON booking_presets(status, open_at);
CREATE INDEX IF NOT EXISTS idx_presets_hospital ON booking_presets(hospital_id);
"""


# Incremental migrations for existing DBs (CREATE IF NOT EXISTS won't alter columns).
MIGRATIONS_SQL = """
-- Hospital geo
ALTER TABLE hospitals ADD COLUMN city TEXT;
ALTER TABLE hospitals ADD COLUMN address TEXT;

-- Multi-site doctor identity (logical person ↔ per-hospital doctor rows)
CREATE TABLE IF NOT EXISTS doctor_persons (
  person_id TEXT PRIMARY KEY,
  name_zh TEXT NOT NULL,
  name_en TEXT,
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS doctor_person_links (
  doctor_id TEXT PRIMARY KEY REFERENCES doctors(doctor_id),
  person_id TEXT NOT NULL REFERENCES doctor_persons(person_id),
  hospital_id TEXT NOT NULL,
  is_primary INTEGER DEFAULT 0,
  notes TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_person_links_person ON doctor_person_links(person_id);
CREATE INDEX IF NOT EXISTS idx_person_links_hospital ON doctor_person_links(hospital_id);

-- Live progress snapshots (breast-filtered)
CREATE TABLE IF NOT EXISTS live_progress (
  progress_id TEXT PRIMARY KEY,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  name_zh TEXT,
  department_zh TEXT,
  clinic_date TEXT NOT NULL,
  session TEXT,
  local_clinic_code TEXT,
  current_number TEXT,
  next_number TEXT,
  max_number TEXT,
  waiting_count INTEGER,
  status_text TEXT,
  specialty_tag TEXT,
  source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_progress_date ON live_progress(clinic_date);
CREATE INDEX IF NOT EXISTS idx_progress_hospital ON live_progress(hospital_id);
CREATE INDEX IF NOT EXISTS idx_progress_doctor ON live_progress(doctor_id);

CREATE INDEX IF NOT EXISTS idx_hospitals_city ON hospitals(city);


CREATE TABLE IF NOT EXISTS live_progress_history (
  hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
  progress_id TEXT NOT NULL,
  hospital_id TEXT NOT NULL,
  doctor_id TEXT,
  name_zh TEXT,
  department_zh TEXT,
  clinic_date TEXT NOT NULL,
  session TEXT,
  local_clinic_code TEXT,
  current_number TEXT,
  next_number TEXT,
  max_number TEXT,
  waiting_count INTEGER,
  status_text TEXT,
  specialty_tag TEXT,
  source_url TEXT,
  fetched_at TEXT NOT NULL,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_lph_progress ON live_progress_history(progress_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_lph_hospital_date ON live_progress_history(hospital_id, clinic_date);
CREATE INDEX IF NOT EXISTS idx_lph_doctor ON live_progress_history(doctor_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_lph_name ON live_progress_history(hospital_id, name_zh, clinic_date);

-- ClinicSlot published registration counts (NULL when source is status-only)
ALTER TABLE clinic_slots ADD COLUMN booked_count INTEGER;
ALTER TABLE clinic_slots ADD COLUMN max_quota INTEGER;
ALTER TABLE clinic_slots ADD COLUMN remaining_count INTEGER;

-- Public visit-experience / review snippets (not clinic-slot core)
CREATE TABLE IF NOT EXISTS doctor_reviews (
  review_id TEXT PRIMARY KEY,
  doctor_id TEXT,                     -- nullable when only a name match is safe
  hospital_id TEXT NOT NULL,
  name_zh TEXT NOT NULL,
  person_id TEXT,                     -- from doctor_person_links when linked
  source TEXT NOT NULL,               -- google_maps|article|forum|hospital_site|other
  source_url TEXT NOT NULL,
  title TEXT,
  excerpt TEXT NOT NULL,              -- short public snippet, not a full article
  rating REAL,                        -- null unless the page prints a number
  published_at TEXT,
  scraped_at TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'zh-Hant',
  raw_json TEXT,
  disambiguation_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_doctor ON doctor_reviews(doctor_id);
CREATE INDEX IF NOT EXISTS idx_reviews_hospital ON doctor_reviews(hospital_id);
CREATE INDEX IF NOT EXISTS idx_reviews_source ON doctor_reviews(source);
CREATE INDEX IF NOT EXISTS idx_reviews_name ON doctor_reviews(name_zh);
CREATE INDEX IF NOT EXISTS idx_reviews_url ON doctor_reviews(source_url);

"""


def ensure_schema(conn) -> list[str]:
    """Apply DDL + best-effort column/table migrations. Returns applied notes."""
    notes: list[str] = []
    conn.executescript(DDL)
    # Column adds: SQLite errors if column already exists — ignore those.
    for stmt in MIGRATIONS_SQL.strip().split(";"):
        lines = []
        for line in stmt.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(line)
        s = "\n".join(lines).strip()
        if not s:
            continue
        try:
            conn.execute(s)
            notes.append(s.splitlines()[0][:80])
        except Exception as e:  # noqa: BLE001 — duplicate column / already exists
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            if "exists" in msg:
                continue
            raise
    return notes
