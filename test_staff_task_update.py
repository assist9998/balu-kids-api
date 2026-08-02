import sys, types, os
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
from fastapi.testclient import TestClient

client = TestClient(main.app)
token = "test-token"
main._SESSIONS[token] = {"role": "director", "name": "TestAdmin"}
headers = {"Authorization": f"Bearer {token}"}

print("=== test 1: editing a 'once' task's title + date also moves its single instance ===")
r = client.post("/staff-tasks", json={
    "staffName": "ZZZ Update Once", "title": "Заполнить отчет", "recurrence": "once",
    "startDate": "2026-08-05",
}, headers=headers)
task = r.json()
r2 = client.patch(f"/staff-tasks/{task['id']}", json={
    "title": "Заполнить отчёт (исправлено)", "startDate": "2026-08-10",
}, headers=headers)
assert r2.status_code == 200, r2.text
updated = r2.json()
assert updated["title"] == "Заполнить отчёт (исправлено)"
assert updated["startDate"] == "2026-08-10"
r3 = client.get("/staff-tasks/ZZZ Update Once", headers=headers)
inst = r3.json()[0]["instances"][0]
assert inst["dueDate"] == "2026-08-10", f"instance date must move with it, got {inst}"
print("OK: title and date both updated, the single instance's due date moved to match")

print()
print("=== test 2: shortening a 'weekly' task's end date drops future PENDING instances past it ===")
r = client.post("/staff-tasks", json={
    "staffName": "ZZZ Update Weekly", "title": "Отчёт по площадке",
    "recurrence": "weekly", "weekdays": [4], "startDate": "2026-07-01", "endDate": "2026-07-31",
}, headers=headers)
task_w = r.json()
# mark one instance done so we can confirm it survives being pruned
r_get = client.get("/staff-tasks/ZZZ Update Weekly", headers=headers)
instances = sorted(r_get.json()[0]["instances"], key=lambda i: i["dueDate"])
last_instance = instances[-1]  # 2026-07-31 Friday
client.patch(f"/staff-task-instances/{last_instance['id']}", json={"status": "done"}, headers=headers)

r2 = client.patch(f"/staff-tasks/{task_w['id']}", json={
    "title": "Отчёт по площадке", "endDate": "2026-07-17",
}, headers=headers)
assert r2.status_code == 200, r2.text
assert r2.json()["endDate"] == "2026-07-17"

r3 = client.get("/staff-tasks/ZZZ Update Weekly", headers=headers)
remaining = r3.json()[0]["instances"]
remaining_dates = sorted(i["dueDate"] for i in remaining)
# every still-pending instance must now be on/before the new end date
for i in remaining:
    if i["status"] == "pending":
        assert i["dueDate"] <= "2026-07-17", f"pending instance {i} survived past the shortened deadline"
# the done one (2026-07-31, past the new deadline) must still be there — history isn't discarded
assert "2026-07-31" in remaining_dates, "a DONE instance must survive shortening the deadline"
print(f"OK: pending instances past the new deadline pruned, done history kept: {remaining_dates}")

print()
print("=== test 3: clearing a 'weekly' task's end date (set to None) makes it open-ended again ===")
r2 = client.patch(f"/staff-tasks/{task_w['id']}", json={
    "title": "Отчёт по площадке", "endDate": None,
}, headers=headers)
assert r2.status_code == 200, r2.text
assert r2.json()["endDate"] is None
print("OK: endDate cleared back to open-ended")

print()
print("=== test 4: editing a 'count' task only changes the title, no date fields touched ===")
r = client.post("/staff-tasks", json={
    "staffName": "ZZZ Update Count", "title": "Провести три мероприятия",
    "recurrence": "count", "targetCount": 3, "startDate": "2026-08-01",
}, headers=headers)
task_c = r.json()
r2 = client.patch(f"/staff-tasks/{task_c['id']}", json={"title": "Провести пять мероприятий"}, headers=headers)
assert r2.status_code == 200, r2.text
assert r2.json()["title"] == "Провести пять мероприятий"
assert r2.json()["targetCount"] == 3, "editing title must not touch targetCount/instances"
r3 = client.get("/staff-tasks/ZZZ Update Count", headers=headers)
assert len(r3.json()[0]["instances"]) == 3, "instance count must be untouched by a title-only edit"
print("OK: 'count' task title edit leaves targetCount/instances alone")

print()
print("=== test 5: editing a nonexistent task returns 404 ===")
r = client.patch("/staff-tasks/999999", json={"title": "Ghost"}, headers=headers)
assert r.status_code == 404, r.text
print("OK: 404 for a nonexistent task id")

print()
print("=== test 6: titleEn round-trips on create and update (RU->EN translate button) ===")
r = client.post("/staff-tasks", json={
    "staffName": "ZZZ Update Bilingual", "title": "Проверить аптечку", "titleEn": "Check the first aid kit",
    "recurrence": "once", "startDate": "2026-08-05",
}, headers=headers)
assert r.status_code == 200, r.text
task_b = r.json()
assert task_b["titleEn"] == "Check the first aid kit", f"titleEn must round-trip from create, got {task_b}"
r2 = client.patch(f"/staff-tasks/{task_b['id']}", json={
    "title": "Проверить аптечку ещё раз", "titleEn": "Check the first aid kit again",
}, headers=headers)
assert r2.status_code == 200, r2.text
assert r2.json()["titleEn"] == "Check the first aid kit again", f"titleEn must update too, got {r2.json()}"
# a task created without ever translating has titleEn = null, not an empty string or the RU text
r3 = client.get("/staff-tasks/ZZZ Update Count", headers=headers)
assert r3.json()[0]["titleEn"] is None, f"titleEn should be null when never translated, got {r3.json()[0]}"
print("OK: titleEn set on create, updated on edit, stays null when never translated")

os.remove(db_path)
print()
print("ALL STAFF-TASK-UPDATE TESTS PASSED")
