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
    # days_ru/days_en/time/price are the only source of truth for a club's
    # schedule — this used to get silently overridden by whatever the
    # separate Clubs Sheets tab said, backwards from how every other write
    # in this app works (Sheets is a read-only mirror, never an input).
    # No edit UI yet, so these are set directly in the DB for now.
    days_ru = Column(String)
    days_en = Column(String)
    time    = Column(String)
    price   = Column(Integer, nullable=True)
    # Membership itself lives in the Children sheet's "Clubs" column (see sheets_client),
    # not here — that's the single source of truth for who's in which club.

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


class StaffTask(Base):
    """A recurring or one-off task assigned to one staff member (Ольга: 'мисс
    Фика каждую пятницу должна скидывать отчёт') — this row is the template;
    the actual per-occurrence checkboxes the manager taps live in
    StaffTaskInstance below, generated from this."""
    __tablename__ = "staff_tasks"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    staff_name = Column(String)
    title      = Column(String)
    # 'once'   — a single instance due on start_date.
    # 'weekly' — one instance per matching weekday (see weekdays) every week,
    #            from start_date onward (open-ended unless end_date is set).
    # 'count'  — target_count undated instances (Ольга: "провести три
    #            мероприятия" — no specific days, just N times before done).
    recurrence   = Column(String, default="once")
    weekdays     = Column(String, nullable=True)  # comma-separated 0=Mon..6=Sun, 'weekly' only
    target_count = Column(Integer, nullable=True)  # 'count' only
    start_date   = Column(String)            # "YYYY-MM-DD"
    end_date     = Column(String, nullable=True)  # "YYYY-MM-DD", open-ended if null ('weekly' only)
    created_at   = Column(String, default="")
    # Stopping a task (no longer relevant) never deletes its history — same
    # reasoning as active/inactive elsewhere in this app (children, clubs):
    # Ольга's monthly summary for past months must keep showing what was
    # actually asked and done, not quietly lose rows a manager already acted on.
    archived     = Column(Boolean, default=False)
    # Same idempotency-key pattern as payment_log — the frontend durably
    # queues this write (see makeDurableIdempotent), and a blind retry of a
    # create-task call whose first attempt actually landed must be a no-op,
    # not a second identical task generating its own duplicate set of
    # instances alongside the first one's.
    idempotency_key = Column(String, nullable=True, unique=True)


class StaffTaskInstance(Base):
    """One concrete occurrence of a StaffTask — what the manager actually
    taps Done/Postponed/Cancelled on.

    due_date/seq deliberately never use SQL NULL, even though only one of
    them is ever meaningful for a given instance — NULL is never equal to
    NULL even inside a UNIQUE constraint (confirmed directly: two rows both
    holding NULL in the same unique column both insert fine, silently
    defeating the constraint below). So a dated instance (once/weekly)
    always gets seq=0 (sentinel, unused) instead of NULL, and an undated
    'count' instance always gets due_date="" (sentinel, unused) instead of
    NULL — keeping every column in the constraint a real, comparable value
    is what makes ON CONFLICT actually catch a duplicate-generation retry."""
    __tablename__ = "staff_task_instances"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    task_id       = Column(Integer, ForeignKey("staff_tasks.id"))
    due_date      = Column(String, default="")   # "YYYY-MM-DD", or "" for a 'count' task's instances
    seq           = Column(Integer, default=0)   # 1-based for a 'count' task's instances, else 0
    status        = Column(String, default="pending")  # pending/done/postponed/cancelled
    cancel_reason = Column(String, nullable=True)
    updated_at    = Column(String, nullable=True)

    # Stops the monthly-generation sweep from creating the same weekly
    # occurrence twice (e.g. if it runs more than once for the same month —
    # a restart, a slow poll catching up).
    __table_args__ = (UniqueConstraint("task_id", "due_date", "seq", name="staff_task_instance_uq"),)
