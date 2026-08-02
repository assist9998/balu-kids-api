import sys, types, os, json
from unittest.mock import MagicMock
from datetime import datetime

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

db = RealSessionLocal()
db.add(models.ChildCache(
    id="ZZZ Garden Sweep",
    data=json.dumps({"id": "ZZZ Garden Sweep", "active": True, "clubs": ""}),
    updated_at="",
))
db.commit()
db.close()

# No club members anywhere -> the club-loop part of the sweep is a no-op
# for every seeded club, isolating these tests to the garden logic only.
sheets_client.run_end_of_day_club_carryover = lambda *a, **k: []

saturday = datetime(2026, 8, 1)
assert saturday.weekday() == 5, "sanity check: 2026-08-01 must be a Saturday"
sat_str = saturday.strftime("%Y-%m-%d")
monday = datetime(2026, 8, 3)
assert monday.weekday() == 0, "sanity check: 2026-08-03 must be a Monday"
mon_str = monday.strftime("%Y-%m-%d")

print("=== test 1: Saturday with NO existing marks -> sweep writes nothing for the garden ===")
sheets_client.get_attendance = lambda date: {}
upsert_calls = []
sheets_client.upsert_attendance = lambda *a, **k: upsert_calls.append((a, k))
carryover_calls = []
sheets_client.run_end_of_day_carryover = lambda *a, **k: (carryover_calls.append((a, k)), [])[1]

db = RealSessionLocal()
main._run_attendance_sweep(sat_str, db)
db.close()
assert upsert_calls == [], f"FAIL: expected no attendance written on a weekend, got {upsert_calls}"
assert carryover_calls == [], f"FAIL: expected no carryover check on a weekend with nothing logged, got {carryover_calls}"
print("OK: a plain Saturday with nothing already marked is left completely untouched")

print()
print("=== test 2: Saturday but SOMETHING already marked (a real exception) -> fills in the rest ===")
sheets_client.get_attendance = lambda date: {"Some Other Kid": "present"}
upsert_calls.clear()
carryover_calls.clear()

db = RealSessionLocal()
main._run_attendance_sweep(sat_str, db)
db.close()
assert len(upsert_calls) == 1, f"FAIL: expected exactly 1 upsert call, got {len(upsert_calls)}"
args, kwargs = upsert_calls[0]
unmarked_arg = args[1] if len(args) > 1 else kwargs.get("statuses")
assert unmarked_arg == {"ZZZ Garden Sweep": "absent"}, (
    f"FAIL: expected only the untouched kid backfilled as absent, got {unmarked_arg}"
)
assert len(carryover_calls) == 1, "FAIL: expected carryover to still run for a day that has real marks on it"
print("OK: a Saturday with an existing mark on it (real exception) still fills in the rest and runs carryover")

print()
print("=== test 3: an ordinary weekday -> unchanged behavior (regression guard) ===")
sheets_client.get_attendance = lambda date: {}
upsert_calls.clear()
carryover_calls.clear()

db = RealSessionLocal()
main._run_attendance_sweep(mon_str, db)
db.close()
assert len(upsert_calls) == 1, f"FAIL: expected the usual absent-backfill on a weekday, got {upsert_calls}"
args, kwargs = upsert_calls[0]
unmarked_arg = args[1] if len(args) > 1 else kwargs.get("statuses")
assert unmarked_arg == {"ZZZ Garden Sweep": "absent"}
assert len(carryover_calls) == 1, "FAIL: expected carryover to run as usual on a weekday"
print("OK: an ordinary weekday behaves exactly as before")

os.remove(db_path)
print()
print("ALL GARDEN-SWEEP-SKIPS-WEEKENDS TESTS PASSED")
