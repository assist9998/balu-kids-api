from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

engine = create_engine("sqlite:////data/balu.db", connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def _enable_wal(dbapi_conn, connection_record):
    # WAL lets reads (e.g. /children while the background cache refresh writes) proceed
    # without blocking on the writer — default SQLite journal mode locks the whole file.
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
