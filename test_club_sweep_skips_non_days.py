import sys, types, os, json
from unittest.mock import MagicMock
from datetime import datetime, timedelta

gspread_mod = types.ModuleType("gspread")
class WorksheetNotFound(Exception): pass
gspread_mod.WorksheetNotFound = WorksheetNotFound
gspread_mod.authorize = lambda creds: MagicMock()
utils_mod = types.ModuleType("gspread.utils")
utils_mod.rowcol_to_a1 = lambda r, c: f"R{r}C{c}"
gspread_mod.utils = utils_mod
sys.modules["gspread"] = gspread_mod
sys.modules["gspread.utils"] = utils_mod
google_mod = types.ModuleType("google")
oauth2_mod = types.ModuleType("google.oauth2")
sa_mod = types.ModuleType("google.oauth2.service_account")
class Credentials:
    @staticmethod
    def from_service_account_info(*a, **k): return MagicMock()
sa_mod.Credentials = Credentials
sys.modules["google"] = google_mod
sys.modules["google.oauth2"] = oauth2_mod
sys.modules["google.oauth2.service_account"] = sa_mod
psycopg2_mod = types.ModuleType("psycopg2")
extras_mod = types.ModuleType("psycopg2.extras")
extras_mod.RealDictCursor = "marker"
pool_mod = types.ModuleType("psycopg2.pool")
pool_mod.ThreadedConnectionPool = lambda *a, **k: MagicMock()
psycopg2_mod.pool = pool_mod
psycopg2_mod.extras = extras_mod
psycopg2_mod.connect = lambda *a, **k: MagicMock()
sys.modules["psycopg2"] = psycopg2_mod
sys.modules["psycopg2.pool"] = pool_mod
sys.modules["psycopg2.extras"] = extras_mod

os.environ["SPREADSHEET_ID"] = "fake"
os.environ["GOOGLE_CREDENTIALS_JSON"] = "{}"

import tempfile
db_path = tempfile.mktemp(suffix=".db")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
real_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
RealBase = declarative_base()
RealSessionLocal = sessionmaker(bind=real_engine)
fake_db_mod = types.ModuleType("database")
fake_db_mod.engine = real_engine
fake_db_mod.Base = RealBase
fake_db_mod.SessionLocal = RealSessionLocal
def _get_db():
    db = RealSessionLocal()
    try: yield db
    finally: db.close()
fake_db_mod.get_db = _get_db
sys.modules["database"] = fake_db_mod

sys.path.insert(0, os.path.dirname(__file__))
import main
import models
import sheets_client
import pg_dual_write

# A kid who's only a member of Chess — Swimming's loop iteration will see
# zero members for it and "continue" immediately regardless of schedule,
# so it never interferes with what these tests are actually checking.
db = RealSessionLocal()
db.add(models.ChildCache(
    id="ZZZ Sweep Test",
    data=json.dumps({"id": "ZZZ Sweep Test", "active": True, "clubs": "Chess"}),
    updated_at="",
))
db.commit()
db.close()

# garden-side calls _run_attendance_sweep also makes — neutral no-ops so the
# test only exercises the club-loop logic being changed
sheets_client.get_attendance = lambda date: {}
sheets_client.upsert_attendance = lambda *a, **k: None
sheets_client.run_end_of_day_carryover = lambda date: []

def set_chess_schedule(days_en):
    db = RealSessionLocal()
    chess = db.query(models.Club).filter_by(name_en="Chess").first()
    chess.days_ru, chess.days_en = days_en, days_en  # value doesn't need real Russian text for this test
    db.commit()
    club_id = chess.id
    db.close()
    return club_id

# A Thursday with a known weekday() value, used as "today" throughout —
# real calendar date doesn't matter, only its weekday.
thursday = datetime(2026, 7, 30)
assert thursday.weekday() == 3, "sanity check: 2026-07-30 must be a Thursday"
date_str = thursday.strftime("%Y-%m-%d")

print("=== test 1: non-scheduled day with NO existing marks -> sweep writes nothing for the club ===")
set_chess_schedule("Mon, Wed")  # Thursday (weekday 3) is neither
sheets_client.get_club_attendance = lambda club_name, date: {}
upsert_calls = []
sheets_client.upsert_club_attendance = lambda *a, **k: upsert_calls.append((a, k))
carryover_calls = []
sheets_client.run_end_of_day_club_carryover = lambda *a, **k: (carryover_calls.append((a, k)), [])[1]

db = RealSessionLocal()
main._run_attendance_sweep(date_str, db)
db.close()
assert upsert_calls == [], f"FAIL: expected no attendance written on a non-club day, got {upsert_calls}"
assert carryover_calls == [], f"FAIL: expected no carryover check on a non-club day with nothing logged, got {carryover_calls}"
print("OK: a plain non-club day with nothing already marked is left completely untouched")

print()
print("=== test 2: non-scheduled day but SOMETHING already marked (a reschedule) -> fills in the rest ===")
sheets_client.get_club_attendance = lambda club_name, date: {"Some Other Kid": "present"}
upsert_calls.clear()
carryover_calls.clear()

db = RealSessionLocal()
main._run_attendance_sweep(date_str, db)
db.close()
assert len(upsert_calls) == 1, f"FAIL: expected exactly 1 upsert call, got {len(upsert_calls)}"
args, kwargs = upsert_calls[0]
unmarked_arg = args[2] if len(args) > 2 else kwargs.get("statuses")
assert unmarked_arg == {"ZZZ Sweep Test": "absent"}, (
    f"FAIL: expected only the untouched member backfilled as absent, got {unmarked_arg}"
)
assert len(carryover_calls) == 1, "FAIL: expected carryover to still run for a day that has real marks on it"
print("OK: a rescheduled day (already has a mark on it) still fills in the rest of the roster and runs carryover")

print()
print("=== test 3: a real scheduled club day -> unchanged behavior (regression guard) ===")
set_chess_schedule("Thu")  # matches the test date's actual weekday now
sheets_client.get_club_attendance = lambda club_name, date: {}
upsert_calls.clear()
carryover_calls.clear()

db = RealSessionLocal()
main._run_attendance_sweep(date_str, db)
db.close()
assert len(upsert_calls) == 1, f"FAIL: expected the usual absent-backfill on a real club day, got {upsert_calls}"
args, kwargs = upsert_calls[0]
unmarked_arg = args[2] if len(args) > 2 else kwargs.get("statuses")
assert unmarked_arg == {"ZZZ Sweep Test": "absent"}
assert len(carryover_calls) == 1, "FAIL: expected carryover to run as usual on a real club day"
print("OK: an actual scheduled club day behaves exactly as before")

os.remove(db_path)
print()
print("ALL CLUB-SWEEP-SKIPS-NON-DAYS TESTS PASSED")
