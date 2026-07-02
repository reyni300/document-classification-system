import sqlite3
import json
from datetime import datetime

class DatabaseModule:
    def __init__(self, db_file="documents.db"):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                ocr_text TEXT,
                data TEXT,
                created_at TEXT,
                ocr_version TEXT,
                llm_version TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER,
                event TEXT,
                details TEXT,
                timestamp TEXT,
                FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE SET NULL
            )
        """)
        self.conn.commit()

    def save(self, filename, ocr_text, parsed_data, ocr_version, llm_version):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO documents (filename, ocr_text, data, created_at, ocr_version, llm_version)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (filename, ocr_text, json.dumps(parsed_data, ensure_ascii=False, indent=2),
              datetime.now().isoformat(), ocr_version, llm_version))
        self.conn.commit()
        return cur.lastrowid

    def log_event(self, doc_id, event, details=""):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO logs (doc_id, event, details, timestamp)
            VALUES (?, ?, ?, ?)
        """, (doc_id, event, details, datetime.now().isoformat()))
        self.conn.commit()

    def get_all(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, filename, created_at FROM documents ORDER BY id DESC")
        return cur.fetchall()

    def get_one(self, doc_id):
        cur = self.conn.cursor()
        cur.execute("SELECT ocr_text, data FROM documents WHERE id=?", (doc_id,))
        row = cur.fetchone()
        if row:
            return row[0], json.loads(row[1])
        return None

    def delete(self, doc_id):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        cur.execute("DELETE FROM logs WHERE doc_id=?", (doc_id,))
        self.conn.commit()

    def get_logs(self, doc_id):
        cur = self.conn.cursor()
        cur.execute("SELECT event, details, timestamp FROM logs WHERE doc_id=? ORDER BY timestamp", (doc_id,))
        return cur.fetchall()

    def update_document(self, doc_id, updated_data):
        cur = self.conn.cursor()
        cur.execute("SELECT data FROM documents WHERE id=?", (doc_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Document {doc_id} not found")
        current_data = json.loads(row[0])
        current_data.update(updated_data)
        cur.execute("UPDATE documents SET data=? WHERE id=?",
                    (json.dumps(current_data, ensure_ascii=False, indent=2), doc_id))
        self.conn.commit()
