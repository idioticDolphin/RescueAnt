import sqlite3
from contextlib import contextmanager
from pathlib import Path
import model.tools.config_service as config_service

DATABASE_PATH = Path(__file__).parent.parent.parent.parent / "db.sqlite3"
config = config_service.get_config()
db_fields = []

@contextmanager
def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON') # Enforce foreign keys on inserts
    try:
        yield connection
    except Exception:
        connection.rollback() # undo changes on failure
        raise
    finally:
        connection.close() # always close connection afterwards

def init_db():
    categories = config.get_categories()
    global db_fields
    db_fields.append("category TEXT NOT NULL")
    for category in categories:
        if category.is_relevant:
            for field in category.fields.keys():
                db_fields.append(f"{field} TEXT NOT NULL")

    db_fields_string = ",".join(db_fields)

    with get_connection() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_crawl INTEGER NOT NULL,
            FOREIGN KEY (source_crawl) REFERENCES crawls(crawl_id) ON DELETE CASCADE,
            """ + db_fields_string + """
        );
        CREATE TABLE IF NOT EXISTS crawls (
            crawl_id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawl_time TEXT NOT NULL,
            source_url TEXT NOT NULL
        );
        """)

def get_db_fields():
    return db_fields