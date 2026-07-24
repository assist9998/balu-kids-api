from sqlalchemy import Column, Integer, String, Boolean, Float, ForeignKey, UniqueConstraint
from database import Base

class Group(Base):
    __tablename__ = "groups"
    id      = Column(String, primary_key=True)
    name_ru = Column(String)
    name_en = Column(String)
    color   = Column(String)
    ink     = Column(String)
    emoji   = Column(String)

class Club(Base):
    __tablename__ = "clubs"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    name_ru = Column(String)
    name_en = Column(String)
    emoji   = Column(String)
    color   = Column(String)
    ink     = Column(String)
    days_ru = Column(String)
    days_en = Column(String)
    time    = Column(String)
    price   = Column(Integer, nullable=True)
    # Membership itself lives in the Children sheet's "Clubs" column (see sheets_client),
    # not here — that's the single source of truth for who's in which club.

class ClubScheduleCache(Base):
    """Local mirror of the (separate) Clubs sheet's price/days/time — same
    idea as ChildCache, just for the other Sheets tab /clubs merges in."""
    __tablename__ = "club_schedule_cache"
    id   = Column(Integer, primary_key=True)  # always 1 — single row holding the whole list
    data = Column(String)  # JSON list, same shape get_clubs_from_sheets() returns

class ChildCache(Base):
    """Local mirror of Sheets' Children data — refreshed from Sheets on a
    background timer and right after every app-side write, so /children and
    /clubs answer instantly instead of hitting the Sheets API per request."""
    __tablename__ = "children_cache"
    id         = Column(String, primary_key=True)  # "First Last"
    data       = Column(String)  # JSON-encoded child dict, same shape get_children() returns
    updated_at = Column(String)

class ChildAvatar(Base):
    __tablename__ = "child_avatars"
    child_id = Column(String, primary_key=True)
    emoji    = Column(String)

class FeedItem(Base):
    __tablename__ = "feed_items"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    type       = Column(String, default="alert")
    emoji      = Column(String, default="📋")
    ru         = Column(String, default="")
    en         = Column(String, default="")
    unread     = Column(Boolean, default=True)
    created_at = Column(String, default="")

class StaffAttendance(Base):
    __tablename__ = "staff_attendance"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    date         = Column(String)   # "YYYY-MM-DD"
    staff_name   = Column(String)   # "First Last" from Sheets Staff tab
    status       = Column(String, default="present")  # present/half-day/absent/late/sick/day-off
    arrival_time = Column(String, nullable=True)  # "09:15" — only set for 'late'
    note         = Column(String, nullable=True)
    # Bus transfer duty — extra-paid, independent of status (someone can be
    # "present" and still have done transfer that same day). 0 = no transfer,
    # 1.0 = full day, 0.5 = half day — matches Ольга's own paper tracking
    # ("+"/"-"/"#" with a monthly total like "18 and half days"), just summed
    # automatically instead of by hand.
    transfer     = Column(Float, default=0)
    # Overtime, same idea as transfer — an "extra" on top of the day's
    # status rather than a status of its own (someone present *and* running
    # overtime that day, both true at once; used to be its own status
    # option, which meant it silently couldn't be shown at the same time as
    # 'late' — same bug class transfer already avoided by being separate).
    extra        = Column(Boolean, default=False)

    # One row per (date, staff) — required for the ON CONFLICT upsert in
    # main.py's _upsert_staff_attendance_row to have a target at all, and
    # what stops two near-simultaneous writes for the same day/person (a
    # double-tap, or the durable queue retrying a call whose first attempt
    # actually landed) from both inserting instead of the second becoming
    # an update. Declared here so a brand-new database gets it for free via
    # create_all(); _migrate() adds it explicitly for a database that
    # already had this table before this constraint existed.
    __table_args__ = (UniqueConstraint("date", "staff_name", name="staff_attendance_date_name_uq"),)
