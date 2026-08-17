import sqlite3
from contextlib import contextmanager
from pathlib import Path
import model.tools.config_service as config_service
import json

config = config_service.get_config()
DATABASE_PATH = Path(config.get_database_path())
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

def _quote_identifier(name:str) -> str:
    return '"' + name.replace('"', '""') + '"'

def init_db():
    categories = config.get_categories()
    global db_fields
    seen_fields = set()
    db_fields = []
    for category in categories:
        if category.is_relevant:
            for field in category.fields.keys():
                if field not in seen_fields:
                    seen_fields.add(field)
                    db_fields.append(f"{_quote_identifier(field)} TEXT")

    db_fields_string = ",".join(db_fields) # Careful. Fine for reading from local config, but deadly sql injection risk when exposing configuration to outsiders

    with get_connection() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS crawls (
            crawl_id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawl_time TEXT NOT NULL,
            source_url TEXT NOT NULL,
            category TEXT,
            fetch_success BOOLEAN NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_crawl_id INTEGER NOT NULL,
            """ + db_fields_string + (", " if db_fields_string else "") + """
            FOREIGN KEY (source_crawl_id) REFERENCES crawls(crawl_id) ON DELETE CASCADE
        );
        """)

def get_db_fields():
    return db_fields

def save_crawl_instance(url:str, crawl_time:float, fetch_success:bool) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO crawls(crawl_time, source_url, fetch_success) VALUES (?, ?, ?)",
            (crawl_time, url, fetch_success)
        )
        crawl_id = cursor.lastrowid
        connection.commit()
    return crawl_id

def save_site_category(crawl_id:int, category_name:str):
    with get_connection() as connection:
        connection.execute(
            "UPDATE crawls SET category = ? WHERE crawl_id = ?",
            (category_name, crawl_id)
        )
        connection.commit()

def save_extraction(crawl_id:int, extracted_fields):
    with get_connection() as connection:
        fields = list(extracted_fields.keys())
        field_string = ", ".join(_quote_identifier(field) for field in fields)
        field_values = tuple(_to_sql_value(extracted_fields[field]) for field in fields)
        parameters = (crawl_id, ) + field_values
        placeholders = ", ".join(["?"] * len(parameters))
        columns = "source_crawl_id" + (f", {field_string}" if field_string else "")
        connection.execute(
            f"INSERT INTO entries({columns}) VALUES ({placeholders})",
            parameters
        )
        connection.commit()

def _to_sql_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value

def get_successful_crawl_urls():
    with get_connection() as connection:
        return [row["source_url"] for row in connection.execute(
            "SELECT source_url FROM crawls WHERE fetch_success"
        ).fetchall()]

def get_crawl_urls():
    with get_connection() as connection:
        return [row["source_url"] for row in connection.execute(
            "SELECT source_url FROM crawls"
        ).fetchall()]