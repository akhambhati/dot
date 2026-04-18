#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import yaml
from pyzotero import zotero


# --------------------------------------------------
# Utility helpers
# --------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sanitize_name(text: str, max_len: int = 120) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    return text[:max_len] if len(text) > max_len else text


def copy_with_retry(src: Path, dst: Path, attempts: int = 5, delay: float = 0.5) -> None:
    last_err = None
    for _ in range(attempts):
        try:
            shutil.copy2(src, dst)
            return
        except Exception as e:
            last_err = e
            time.sleep(delay)
    if last_err is not None:
        raise last_err


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_attach_manifest(doc_out_dir: Path) -> dict:
    return load_json(doc_out_dir / "zotero_attach_manifest.json", {})


def save_attach_manifest(doc_out_dir: Path, data: dict) -> None:
    save_json(doc_out_dir / "zotero_attach_manifest.json", data)


def extract_created_item_keys(resp) -> list[str]:
    keys: list[str] = []

    if isinstance(resp, dict):
        for top_key in ("success", "successful", "unchanged"):
            val = resp.get(top_key)
            if isinstance(val, dict):
                for _, item in val.items():
                    if isinstance(item, str):
                        keys.append(item)
                    elif isinstance(item, dict):
                        if "key" in item and isinstance(item["key"], str):
                            keys.append(item["key"])
                        data = item.get("data")
                        if isinstance(data, dict):
                            key = data.get("key")
                            if isinstance(key, str):
                                keys.append(key)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        keys.append(item)
                    elif isinstance(item, dict):
                        if "key" in item and isinstance(item["key"], str):
                            keys.append(item["key"])
                        data = item.get("data")
                        if isinstance(data, dict):
                            key = data.get("key")
                            if isinstance(key, str):
                                keys.append(key)

    elif isinstance(resp, list):
        for item in resp:
            if isinstance(item, str):
                keys.append(item)
            elif isinstance(item, dict):
                key = item.get("key")
                if isinstance(key, str):
                    keys.append(key)
                data = item.get("data")
                if isinstance(data, dict):
                    key = data.get("key")
                    if isinstance(key, str):
                        keys.append(key)

    # preserve order, remove duplicates
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out

def vlog(args, msg: str) -> None:
    if getattr(args, "verbose", False):
        print(msg)


# --------------------------------------------------
# Zotero attachment representation
# --------------------------------------------------

@dataclass
class ZoteroAttachment:
    attachment_key: str
    parent_key: str | None
    parent_title: str
    filename: str
    local_path: Path


# --------------------------------------------------
# Bridge state database
# --------------------------------------------------

class BridgeState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()
        self._ensure_column("documents", "active", "INTEGER DEFAULT 1")
        self._ensure_column("documents", "last_reconciled_ts", "INTEGER")
        self._ensure_column("documents", "reconcile_note", "TEXT")

    def _init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            zotero_attachment_key TEXT PRIMARY KEY,
            zotero_parent_key TEXT,
            zotero_item_title TEXT,
            attachment_filename TEXT,
            local_pdf_path TEXT NOT NULL,
            local_pdf_hash TEXT,
            rm_uuid TEXT UNIQUE,
            rm_visible_name TEXT,
            rm_parent_uuid TEXT,
            last_push_ts INTEGER,
            last_rm_mtime REAL,
            last_export_ts INTEGER,
            last_export_pdf_path TEXT,
            last_export_hash TEXT,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            zotero_attachment_key TEXT,
            rm_uuid TEXT,
            detail TEXT
        );

        CREATE TABLE IF NOT EXISTS derived_attachments (
            attachment_key TEXT PRIMARY KEY,
            parent_item_key TEXT,
            source_attachment_key TEXT,
            attachment_type TEXT,
            created_ts INTEGER
        );

        CREATE TABLE IF NOT EXISTS derived_hashes (
            sha256 TEXT PRIMARY KEY,
            parent_item_key TEXT,
            source_attachment_key TEXT,
            artifact_type TEXT,
            created_ts INTEGER
        );

        CREATE TABLE IF NOT EXISTS attached_artifacts (
            source_attachment_key TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            zotero_child_key TEXT,
            last_hash TEXT,
            updated_ts INTEGER,
            PRIMARY KEY (source_attachment_key, artifact_type)
        );
        """)
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in cur.fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self.conn.commit()

    def log(
        self,
        event_type: str,
        zotero_attachment_key: str | None = None,
        rm_uuid: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, event_type, zotero_attachment_key, rm_uuid, detail) VALUES (?, ?, ?, ?, ?)",
            (now_ts(), event_type, zotero_attachment_key, rm_uuid, detail),
        )
        self.conn.commit()

    def get_document(self, attachment_key: str):
        cur = self.conn.execute(
            "SELECT * FROM documents WHERE zotero_attachment_key = ?",
            (attachment_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def upsert_document(self, data: dict) -> None:
        cols = sorted(data.keys())
        vals = [data[c] for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        updates = ", ".join(
            [f"{c}=excluded.{c}" for c in cols if c != "zotero_attachment_key"]
        )
        sql = f"""
        INSERT INTO documents ({", ".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(zotero_attachment_key) DO UPDATE SET
        {updates}
        """
        self.conn.execute(sql, vals)
        self.conn.commit()

    def all_documents(self):
        cur = self.conn.execute("SELECT * FROM documents")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row))

    def all_derived_attachments(self):
        cur = self.conn.execute("""
            SELECT attachment_key, parent_item_key, source_attachment_key, attachment_type, created_ts
            FROM derived_attachments
        """)
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row))

    def delete_derived_attachment(self, attachment_key: str) -> None:
        self.conn.execute(
            "DELETE FROM derived_attachments WHERE attachment_key = ?",
            (attachment_key,),
        )
        self.conn.commit()

    def register_derived_attachment(
        self,
        attachment_key: str,
        parent_item_key: str | None,
        source_attachment_key: str | None,
        attachment_type: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO derived_attachments
            (attachment_key, parent_item_key, source_attachment_key, attachment_type, created_ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                attachment_key,
                parent_item_key,
                source_attachment_key,
                attachment_type,
                now_ts(),
            ),
        )
        self.conn.commit()

    def derived_attachment_keys(self) -> set[str]:
        cur = self.conn.execute("SELECT attachment_key FROM derived_attachments")
        return {row[0] for row in cur.fetchall()}

    def register_derived_hash(
        self,
        sha256: str,
        parent_item_key: str | None,
        source_attachment_key: str | None,
        artifact_type: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO derived_hashes
            (sha256, parent_item_key, source_attachment_key, artifact_type, created_ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                sha256,
                parent_item_key,
                source_attachment_key,
                artifact_type,
                now_ts(),
            ),
        )
        self.conn.commit()

    def derived_hashes(self) -> set[str]:
        cur = self.conn.execute("SELECT sha256 FROM derived_hashes")
        return {row[0] for row in cur.fetchall()}

    def get_attached_artifact(self, source_attachment_key: str, artifact_type: str):
        cur = self.conn.execute(
            """
            SELECT source_attachment_key, artifact_type, zotero_child_key, last_hash, updated_ts
            FROM attached_artifacts
            WHERE source_attachment_key = ? AND artifact_type = ?
            """,
            (source_attachment_key, artifact_type),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def upsert_attached_artifact(
        self,
        source_attachment_key: str,
        artifact_type: str,
        zotero_child_key: str | None,
        last_hash: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO attached_artifacts
            (source_attachment_key, artifact_type, zotero_child_key, last_hash, updated_ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_attachment_key,
                artifact_type,
                zotero_child_key,
                last_hash,
                now_ts(),
            ),
        )
        self.conn.commit()

    def delete_attached_artifact(self, source_attachment_key: str, artifact_type: str) -> None:
        self.conn.execute(
            """
            DELETE FROM attached_artifacts
            WHERE source_attachment_key = ? AND artifact_type = ?
            """,
            (source_attachment_key, artifact_type),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------
# Zotero local reader
# --------------------------------------------------

class ZoteroLocalReader:
    def __init__(self, zotero_db: Path, zotero_storage_dir: Path):
        self.zotero_db = zotero_db
        self.zotero_storage_dir = zotero_storage_dir
        self.snapshot_dir: Path | None = None
        self.snapshot_db: Path | None = None

    def prepare_snapshot(self) -> None:
        if self.snapshot_db is not None:
            return

        src_db = self.zotero_db
        src_dir = src_db.parent
        snap_dir = Path(tempfile.mkdtemp(prefix="zotero-snapshot-"))

        for name in [src_db.name, src_db.name + "-wal", src_db.name + "-shm"]:
            src = src_dir / name
            dst = snap_dir / name
            if src.exists():
                copy_with_retry(src, dst)

        self.snapshot_dir = snap_dir
        self.snapshot_db = snap_dir / src_db.name

    def cleanup_snapshot(self) -> None:
        if self.snapshot_dir and self.snapshot_dir.exists():
            shutil.rmtree(self.snapshot_dir, ignore_errors=True)
        self.snapshot_dir = None
        self.snapshot_db = None

    def _connect(self):
        self.prepare_snapshot()
        assert self.snapshot_db is not None
        return sqlite3.connect(f"file:{self.snapshot_db}?mode=ro", uri=True)

    def find_collection_id(self, collection_name: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT collectionID
                FROM collections
                WHERE collectionName = ?
                LIMIT 1
                """,
                (collection_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Collection not found: {collection_name}")
            return int(row[0])

    def attachment_record(self, attachment_key: str) -> ZoteroAttachment | None:
        query = """
        SELECT
            child.key AS attachment_key,
            parent.key AS parent_key,
            MAX(CASE WHEN parentFields.fieldName = 'title' THEN parentValues.value END) AS parent_title,
            ia.path AS attachment_path,
            ia.contentType AS content_type
        FROM items child
        JOIN itemAttachments ia ON ia.itemID = child.itemID
        LEFT JOIN items parent ON parent.itemID = ia.parentItemID

        LEFT JOIN itemData parentData ON parentData.itemID = parent.itemID
        LEFT JOIN fieldsCombined parentFields ON parentFields.fieldID = parentData.fieldID
        LEFT JOIN itemDataValues parentValues ON parentValues.valueID = parentData.valueID

        WHERE child.key = ?
        GROUP BY child.key, parent.key, ia.path, ia.contentType
        """
        with self._connect() as conn:
            cur = conn.execute(query, (attachment_key,))
            row = cur.fetchone()

        if row is None:
            return None

        key, parent_key, parent_title, attachment_path, content_type = row
        if content_type != "application/pdf":
            return None
        if not attachment_path:
            return None

        filename = self._filename_from_attachment_path(attachment_path)
        if not filename:
            return None

        local_path = self._resolve_attachment_file(key, attachment_path, filename)
        if local_path is None:
            return None

        return ZoteroAttachment(
            attachment_key=key,
            parent_key=parent_key,
            parent_title=parent_title or Path(filename).stem,
            filename=filename,
            local_path=local_path,
        )

    def attachment_key_exists(self, attachment_key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM items WHERE key = ? LIMIT 1",
                (attachment_key,),
            )
            return cur.fetchone() is not None

    def _filename_from_attachment_path(self, attachment_path: str) -> str | None:
        if not attachment_path:
            return None
        if attachment_path.startswith("storage:"):
            return Path(attachment_path[len("storage:"):]).name
        return Path(attachment_path).name or None

    def _resolve_attachment_file(
        self,
        attachment_key: str,
        attachment_path: str,
        filename: str,
    ) -> Path | None:
        if attachment_path.startswith("storage:"):
            return self.zotero_storage_dir / attachment_key / filename
        return Path(attachment_path).expanduser()

    def iter_pdf_attachments_in_collection(self, collection_name: str):
        collection_id = self.find_collection_id(collection_name)

        query = """
        SELECT
            child.key AS attachment_key,
            parent.key AS parent_key,
            MAX(CASE WHEN parentFields.fieldName = 'title' THEN parentValues.value END) AS parent_title,
            ia.path AS attachment_path,
            ia.contentType AS content_type,
            ia.linkMode AS link_mode
        FROM collectionItems ci
        JOIN items parent ON parent.itemID = ci.itemID
        JOIN itemAttachments ia ON ia.parentItemID = parent.itemID
        JOIN items child ON child.itemID = ia.itemID

        LEFT JOIN itemData parentData ON parentData.itemID = parent.itemID
        LEFT JOIN fieldsCombined parentFields ON parentFields.fieldID = parentData.fieldID
        LEFT JOIN itemDataValues parentValues ON parentValues.valueID = parentData.valueID

        WHERE ci.collectionID = ?
        GROUP BY child.key, parent.key, ia.path, ia.contentType, ia.linkMode
        ORDER BY parent.key, child.key
        """

        with self._connect() as conn:
            cur = conn.execute(query, (collection_id,))
            rows = cur.fetchall()

        for attachment_key, parent_key, parent_title, attachment_path, content_type, _link_mode in rows:
            if content_type != "application/pdf":
                continue

            if not attachment_path:
                continue

            filename = self._filename_from_attachment_path(attachment_path)
            if not filename:
                continue

            local_path = self._resolve_attachment_file(
                attachment_key=attachment_key,
                attachment_path=attachment_path,
                filename=filename,
            )

            if local_path is None or not local_path.exists():
                continue

            yield ZoteroAttachment(
                attachment_key=attachment_key,
                parent_key=parent_key,
                parent_title=parent_title or Path(filename).stem,
                filename=filename,
                local_path=local_path,
            )

    def inspect(self, collection_name: str) -> list[ZoteroAttachment]:
        return list(self.iter_pdf_attachments_in_collection(collection_name))


# --------------------------------------------------
# RM2 storage handler
# --------------------------------------------------

class RemarkableStore:
    def __init__(self, xochitl_dir: Path):
        self.xochitl_dir = xochitl_dir

    def ensure_folder(self, folder_name: str) -> str:
        for meta_file in self.xochitl_dir.glob("*.metadata"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                meta.get("type") == "CollectionType"
                and meta.get("visibleName") == folder_name
                and meta.get("parent", "") == ""
            ):
                return meta_file.stem

        folder_uuid = str(uuid.uuid4())
        metadata = {
            "deleted": False,
            "lastModified": str(now_ts() * 1000),
            "metadatamodified": False,
            "modified": False,
            "parent": "",
            "pinned": False,
            "synced": False,
            "type": "CollectionType",
            "version": 1,
            "visibleName": folder_name,
        }

        (self.xochitl_dir / f"{folder_uuid}.metadata").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        (self.xochitl_dir / f"{folder_uuid}.content").write_text(
            "{}", encoding="utf-8"
        )
        return folder_uuid

    def create_or_update_pdf_bundle(
        self,
        pdf_path: Path,
        visible_name: str,
        parent_uuid: str,
        existing_uuid: str | None = None,
    ) -> str:
        rm_uuid = existing_uuid or str(uuid.uuid4())

        shutil.copy2(pdf_path, self.xochitl_dir / f"{rm_uuid}.pdf")

        metadata = {
            "deleted": False,
            "lastModified": str(now_ts() * 1000),
            "metadatamodified": False,
            "modified": False,
            "parent": parent_uuid,
            "pinned": False,
            "synced": False,
            "type": "DocumentType",
            "version": 1,
            "visibleName": visible_name,
        }

        content = {
            "dummyDocument": False,
            "extraMetadata": {},
            "fileType": "pdf",
            "fontName": "",
            "lastOpenedPage": 0,
            "lineHeight": -1,
            "margins": 125,
            "orientation": "portrait",
            "pageCount": 0,
            "pages": [],
            "textAlignment": "justify",
            "textScale": 1,
            "transform": {
                "m11": 1,
                "m12": 0,
                "m13": 0,
                "m21": 0,
                "m22": 1,
                "m23": 0,
                "m31": 0,
                "m32": 0,
                "m33": 1,
            },
        }

        (self.xochitl_dir / f"{rm_uuid}.metadata").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        (self.xochitl_dir / f"{rm_uuid}.content").write_text(
            json.dumps(content, indent=2), encoding="utf-8"
        )
        (self.xochitl_dir / f"{rm_uuid}.pagedata").write_text(
            "", encoding="utf-8"
        )
        (self.xochitl_dir / rm_uuid).mkdir(exist_ok=True)

        return rm_uuid

    def bundle_exists(self, rm_uuid: str | None) -> bool:
        if not rm_uuid:
            return False
        needed = [
            self.xochitl_dir / f"{rm_uuid}.metadata",
            self.xochitl_dir / f"{rm_uuid}.content",
            self.xochitl_dir / f"{rm_uuid}.pdf",
            self.xochitl_dir / rm_uuid,
        ]
        return all(p.exists() for p in needed)

    def bundle_mtime(self, rm_uuid: str) -> float:
        paths = [
            self.xochitl_dir / f"{rm_uuid}.metadata",
            self.xochitl_dir / f"{rm_uuid}.content",
            self.xochitl_dir / f"{rm_uuid}.pagedata",
            self.xochitl_dir / f"{rm_uuid}.pdf",
            self.xochitl_dir / rm_uuid,
        ]

        mtimes = []
        for p in paths:
            if not p.exists():
                continue
            try:
                mtimes.append(p.stat().st_mtime)
            except FileNotFoundError:
                continue

            if p.is_dir():
                for child in p.rglob("*"):
                    try:
                        mtimes.append(child.stat().st_mtime)
                    except FileNotFoundError:
                        pass

        return max(mtimes) if mtimes else 0.0


# --------------------------------------------------
# Markdown annotation parsing
# --------------------------------------------------

def parse_obsidian_annotations(md_text: str) -> dict:
    lines = md_text.splitlines()

    meta = {}
    body_start = 0

    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                yaml_block = "\n".join(lines[1:i])
                meta = yaml.safe_load(yaml_block) or {}
                body_start = i + 1
                break

    pages = []
    current_page = None
    in_highlights = False

    for raw in lines[body_start:]:
        line = raw.strip()

        if line.startswith("### [[") and "page" in line.lower():
            if current_page is not None:
                pages.append(current_page)

            page_num = None
            m = re.search(r"page[ =](\d+)", line, flags=re.IGNORECASE)
            if m:
                page_num = int(m.group(1))
            else:
                m = re.search(r", page (\d+)", line, flags=re.IGNORECASE)
                if m:
                    page_num = int(m.group(1))

            current_page = {
                "page": page_num,
                "highlights": [],
            }
            in_highlights = False
            continue

        if line.startswith("#### Highlights"):
            in_highlights = True
            continue

        if line.startswith("#### "):
            in_highlights = False
            continue

        if in_highlights and line.startswith(">"):
            text = line.lstrip(">").strip()
            if text and current_page is not None:
                current_page["highlights"].append(text)

    if current_page is not None:
        pages.append(current_page)

    flat_highlights = []
    for page in pages:
        for h in page["highlights"]:
            flat_highlights.append(
                {
                    "page": page["page"],
                    "text": h,
                }
            )

    return {
        "metadata": meta,
        "pages": pages,
        "flat_highlights": flat_highlights,
    }


def timestamp_to_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return None


def build_zotero_note_markdown(parsed: dict, fallback_title: str) -> str:
    meta = parsed.get("metadata", {})
    title = meta.get("scrybble_filename") or fallback_title
    exported = timestamp_to_iso(meta.get("scrybble_timestamp"))

    out: list[str] = []
    out.append("# RM2 Reading Notes")
    out.append("")
    out.append(f"Source title: {title}")
    if exported:
        out.append(f"Exported: {exported}")
    out.append("")
    out.append("## Highlights by page")
    out.append("")

    for page in parsed.get("pages", []):
        page_no = page.get("page")
        out.append(f"### Page {page_no if page_no is not None else '?'}")
        for h in page.get("highlights", []):
            out.append(f"- {h}")
        out.append("")

    out.append("## Flat highlights")
    out.append("")
    for item in parsed.get("flat_highlights", []):
        page_no = item.get("page")
        text = item.get("text", "")
        if page_no is None:
            out.append(f"- {text}")
        else:
            out.append(f"- [p. {page_no}] {text}")

    out.append("")
    return "\n".join(out)


def zotero_note_markdown_to_html(md_text: str) -> str:
    lines = md_text.splitlines()
    html: list[str] = []
    in_list = False

    for raw in lines:
        line = raw.rstrip()

        if not line.strip():
            if in_list:
                html.append("</ul>")
                in_list = False
            continue

        if line.startswith("# "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h1>{escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h2>{escape(line[3:].strip())}</h2>")
        elif line.startswith("### "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h3>{escape(line[4:].strip())}</h3>")
        elif line.startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{escape(line[2:].strip())}</li>")
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<p>{escape(line.strip())}</p>")

    if in_list:
        html.append("</ul>")

    return "\n".join(html)


# --------------------------------------------------
# Remarks exporter
# --------------------------------------------------

class RemarksExporter:
    def __init__(self, remarks_bin: str):
        self.remarks_bin = remarks_bin

    def _copy_bundle_subset(self, xochitl_dir: Path, uid: str, work_in: Path) -> None:
        candidates = [
            xochitl_dir / f"{uid}.metadata",
            xochitl_dir / f"{uid}.content",
            xochitl_dir / f"{uid}.pagedata",
            xochitl_dir / f"{uid}.pdf",
            xochitl_dir / uid,
        ]

        for src in candidates:
            if not src.exists():
                continue
            dst = work_in / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

    def _inventory_outputs(self, out_dir: Path) -> dict:
        files = []
        for p in sorted(out_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(out_dir)
            files.append(
                {
                    "relative_path": str(rel),
                    "suffix": p.suffix.lower(),
                    "size": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
            )

        by_suffix = {}
        for f in files:
            by_suffix.setdefault(f["suffix"], []).append(f["relative_path"])

        return {
            "file_count": len(files),
            "files": files,
            "by_suffix": by_suffix,
        }

    def _choose_primary_pdf(self, out_dir: Path, uid: str) -> Path | None:
        pdfs = sorted(out_dir.rglob("*.pdf"))
        if not pdfs:
            return None

        for pdf in pdfs:
            if pdf.name != f"{uid}.pdf":
                return pdf

        return pdfs[0]

    def _choose_markdown(self, out_dir: Path) -> Path | None:
        mds = sorted(out_dir.rglob("*.md"))
        if not mds:
            return None
        return mds[0]

    def export_bundle(self, xochitl_dir: Path, uid: str, doc_out_dir: Path) -> dict | None:
        doc_out_dir.mkdir(parents=True, exist_ok=True)

        work_in = doc_out_dir / ".remarks_input"
        work_out = doc_out_dir / ".remarks_output"

        if work_in.exists():
            shutil.rmtree(work_in, ignore_errors=True)
        if work_out.exists():
            shutil.rmtree(work_out, ignore_errors=True)

        work_in.mkdir(parents=True, exist_ok=True)
        work_out.mkdir(parents=True, exist_ok=True)

        self._copy_bundle_subset(xochitl_dir, uid, work_in)

        cmd = [self.remarks_bin, str(work_in), str(work_out)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return None

        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            return None

        inventory = self._inventory_outputs(work_out)
        primary_pdf = self._choose_primary_pdf(work_out, uid)
        primary_md = self._choose_markdown(work_out)

        manifest = {
            "uid": uid,
            "remarks_command": cmd,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "inventory": inventory,
            "primary_pdf": str(primary_pdf.relative_to(work_out)) if primary_pdf else None,
            "primary_md": str(primary_md.relative_to(work_out)) if primary_md else None,
            "exported_at": now_ts(),
        }

        manifest_path = doc_out_dir / "remarks_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return {
            "work_out": work_out,
            "manifest_path": manifest_path,
            "primary_pdf": primary_pdf,
            "primary_md": primary_md,
            "inventory": inventory,
        }


# --------------------------------------------------
# Zotero web writer
# --------------------------------------------------

class ZoteroWebWriter:
    def __init__(self, library_id: str, library_type: str, api_key: str):
        self.z = zotero.Zotero(library_id, library_type, api_key)

    def create_child_note(self, parent_key: str, note_html: str):
        payload = [{
            "itemType": "note",
            "parentItem": parent_key,
            "note": note_html,
        }]
        return self.z.create_items(payload, parentid=parent_key)

    def upload_child_attachment(self, parent_key: str, file_path: Path):
        return self.z.attachment_simple([str(file_path)], parentid=parent_key)

    def item(self, item_key: str):
        return self.z.item(item_key)

    def item_exists(self, item_key: str) -> bool:
        try:
            self.z.item(item_key)
            return True
        except Exception:
            return False

    def update_note(self, item_key: str, note_html: str):
        item = self.z.item(item_key)
        item["data"]["note"] = note_html
        return self.z.update_item(item)

    def delete_item_by_key(self, item_key: str):
        """
        Delete a single Zotero item using the single-item endpoint:
            DELETE /users/<id>/items/<itemKey>
        with If-Unmodified-Since-Version set to the current item version.

        This avoids Pyzotero's multi-item delete path, which uses the library
        version and is much more prone to 412 errors during active write cycles.
        """
        item = self.z.item(item_key)
        if not isinstance(item, dict):
            raise RuntimeError(f"Could not fetch Zotero item for deletion: {item_key}")

        data = item.get("data", {})
        version = data.get("version")
        if version is None:
            raise RuntimeError(f"Zotero item missing version: {item_key}")

        # Determine the correct item endpoint from the library type/id already
        # stored on the Pyzotero client.
        library_type = getattr(self.z, "library_type", None) or getattr(self.z, "libraryType", None)
        library_id = getattr(self.z, "library_id", None) or getattr(self.z, "libraryID", None)

        if library_type == "group":
            prefix = f"/groups/{library_id}"
        else:
            prefix = f"/users/{library_id}"

        url = f"https://api.zotero.org{prefix}/items/{item_key}"

        headers = {
            "Zotero-API-Key": self.z.api_key,
            "If-Unmodified-Since-Version": str(version),
        }

        import httpx

        resp = httpx.delete(url, headers=headers, timeout=60.0)
        if resp.status_code == 204:
            return None

        # Retry once if the item version changed between GET and DELETE
        if resp.status_code == 412:
            item = self.z.item(item_key)
            data = item.get("data", {})
            version = data.get("version")
            headers["If-Unmodified-Since-Version"] = str(version)
            resp = httpx.delete(url, headers=headers, timeout=60.0)
            if resp.status_code == 204:
                return None

        resp.raise_for_status()

    def child_keys(self, parent_key: str) -> set[str]:
        children = self.z.children(parent_key)
        keys = set()

        for item in children:
            if not isinstance(item, dict):
                continue

            key = item.get("key")
            if isinstance(key, str):
                keys.add(key)

            data = item.get("data")
            if isinstance(data, dict):
                key = data.get("key")
                if isinstance(key, str):
                    keys.add(key)

        return keys

# --------------------------------------------------
# Commands
# --------------------------------------------------

def do_inspect_zotero(args):
    reader = ZoteroLocalReader(Path(args.zotero_db), Path(args.zotero_storage_dir))
    reader.prepare_snapshot()
    try:
        collection_id = reader.find_collection_id(args.collection_name)
        print(f"collection={args.collection_name} collection_id={collection_id}")

        found = 0
        for i, att in enumerate(reader.inspect(args.collection_name), start=1):
            found += 1
            print(f"{i:03d} attachment_key={att.attachment_key} parent_key={att.parent_key}")
            print(f"     title={att.parent_title}")
            print(f"     filename={att.filename}")
            print(f"     path={att.local_path}")

        print(f"usable_pdf_attachments={found}")
    finally:
        reader.cleanup_snapshot()


def do_reconcile(args) -> dict:
    stats = {
        "rows_seen": 0,
        "missing_zotero_attachment": 0,
        "resolved_local_path": 0,
        "missing_local_file": 0,
        "missing_rm_bundle": 0,
        "source_hash_refreshed": 0,
        "stale_derived_keys_removed": 0,
    }

    state = BridgeState(Path(args.state_db))
    reader = ZoteroLocalReader(Path(args.zotero_db), Path(args.zotero_storage_dir))
    rm = RemarkableStore(Path(args.xochitl_dir))

    reader.prepare_snapshot()
    try:
        for row in state.all_documents():
            stats["rows_seen"] += 1
            attachment_key = row["zotero_attachment_key"]
            new_row = dict(row)
            new_row["last_reconciled_ts"] = now_ts()
            new_row["reconcile_note"] = None

            att = reader.attachment_record(attachment_key)

            if att is None:
                new_row["active"] = 0
                new_row["status"] = "missing_zotero_attachment"
                new_row["reconcile_note"] = "Attachment key no longer resolves in Zotero as a local PDF"
                state.upsert_document(new_row)
                stats["missing_zotero_attachment"] += 1
                continue

            new_row["zotero_parent_key"] = att.parent_key
            new_row["zotero_item_title"] = att.parent_title
            new_row["attachment_filename"] = att.filename

            if str(att.local_path) != row.get("local_pdf_path"):
                new_row["local_pdf_path"] = str(att.local_path)
                stats["resolved_local_path"] += 1

            if not att.local_path.exists():
                new_row["active"] = 0
                new_row["status"] = "missing_local_file"
                new_row["reconcile_note"] = "Resolved local file path does not exist"
                state.upsert_document(new_row)
                stats["missing_local_file"] += 1
                continue

            current_hash = sha256_file(att.local_path)
            if current_hash != row.get("local_pdf_hash"):
                new_row["local_pdf_hash"] = current_hash
                stats["source_hash_refreshed"] += 1

            if not rm.bundle_exists(row.get("rm_uuid")):
                if row.get("rm_uuid"):
                    stats["missing_rm_bundle"] += 1
                new_row["rm_uuid"] = None
                new_row["status"] = "missing_rm_bundle"
                new_row["reconcile_note"] = "RM2 bundle missing; push can recreate it"
                new_row["active"] = 1
            else:
                new_row["status"] = row.get("status") if row.get("status") not in {
                    "missing_zotero_attachment",
                    "missing_local_file",
                    "missing_rm_bundle",
                } else "tracked"
                new_row["active"] = 1
                new_row["reconcile_note"] = "ok"

            state.upsert_document(new_row)

        for drow in state.all_derived_attachments():
            if not reader.attachment_key_exists(drow["attachment_key"]):
                state.delete_derived_attachment(drow["attachment_key"])
                stats["stale_derived_keys_removed"] += 1

    finally:
        reader.cleanup_snapshot()
        state.close()

    return stats


def do_push(args) -> dict:
    stats = {
        "pushed": 0,
        "skipped_existing": 0,
        "skipped_inactive": 0,
        "skipped_derived_key": 0,
        "skipped_derived_hash": 0,
    }

    state = BridgeState(Path(args.state_db))
    reader = ZoteroLocalReader(Path(args.zotero_db), Path(args.zotero_storage_dir))
    rm = RemarkableStore(Path(args.xochitl_dir))

    reader.prepare_snapshot()
    try:
        rm_parent_uuid = rm.ensure_folder(args.rm_parent_name)
        derived_keys = state.derived_attachment_keys()
        derived_hashes = state.derived_hashes()

        for att in reader.iter_pdf_attachments_in_collection(args.collection_name):
            existing = state.get_document(att.attachment_key)
            if existing and int(existing.get("active") or 1) == 0:
                stats["skipped_inactive"] += 1
                vlog(args, f"skip inactive: {att.attachment_key} {att.filename}")
                continue

            if att.attachment_key in derived_keys:
                stats["skipped_derived_key"] += 1
                vlog(args, f"skip derived key: {att.attachment_key} {att.filename}")
                continue

            local_hash = sha256_file(att.local_path)

            if local_hash in derived_hashes:
                stats["skipped_derived_hash"] += 1
                vlog(args, f"skip derived hash: {att.attachment_key} {att.filename}")
                continue

            row = existing

            if row and row.get("local_pdf_hash") == local_hash and row.get("rm_uuid"):
                stats["skipped_existing"] += 1
                vlog(args, f"skip unchanged: {att.attachment_key} {att.filename}")
                continue

            rm_visible_name = sanitize_name(att.parent_title or Path(att.filename).stem)

            rm_uuid = rm.create_or_update_pdf_bundle(
                pdf_path=att.local_path,
                visible_name=rm_visible_name,
                parent_uuid=rm_parent_uuid,
                existing_uuid=row["rm_uuid"] if row and row.get("rm_uuid") else None,
            )

            state.upsert_document(
                {
                    "zotero_attachment_key": att.attachment_key,
                    "zotero_parent_key": att.parent_key,
                    "zotero_item_title": att.parent_title,
                    "attachment_filename": att.filename,
                    "local_pdf_path": str(att.local_path),
                    "local_pdf_hash": local_hash,
                    "rm_uuid": rm_uuid,
                    "rm_visible_name": rm_visible_name,
                    "rm_parent_uuid": rm_parent_uuid,
                    "last_push_ts": now_ts(),
                    "last_rm_mtime": rm.bundle_mtime(rm_uuid),
                    "status": "pushed",
                    "active": 1,
                    "last_reconciled_ts": row.get("last_reconciled_ts") if row else None,
                    "reconcile_note": row.get("reconcile_note") if row else None,
                }
            )
            state.log("push", att.attachment_key, rm_uuid, str(att.local_path))
            stats["pushed"] += 1
            print(f"Pushed {att.filename} -> {rm_uuid}")
    finally:
        reader.cleanup_snapshot()
        state.close()

    return stats


def do_pull(args) -> dict:
    stats = {
        "exported": 0,
        "export_failed": 0,
        "skipped_inactive": 0,
        "skipped_missing_rm_uuid": 0,
        "skipped_unchanged": 0,
    }

    state = BridgeState(Path(args.state_db))
    rm = RemarkableStore(Path(args.xochitl_dir))
    exporter = RemarksExporter(args.remarks_bin)
    out_root = Path(args.annotated_dir)

    try:
        for row in state.all_documents():
            if int(row.get("active") or 1) == 0:
                stats["skipped_inactive"] += 1
                continue

            uid = row.get("rm_uuid")
            if not uid:
                stats["skipped_missing_rm_uuid"] += 1
                continue

            current_mtime = rm.bundle_mtime(uid)
            previous_mtime = row.get("last_rm_mtime") or 0

            if current_mtime <= previous_mtime:
                stats["skipped_unchanged"] += 1
                continue

            parent_key = row.get("zotero_parent_key") or "unlinked"
            stem = Path(row["attachment_filename"]).stem
            doc_out_dir = out_root / parent_key / stem
            doc_out_dir.mkdir(parents=True, exist_ok=True)

            result = exporter.export_bundle(
                Path(args.xochitl_dir),
                uid,
                doc_out_dir,
            )

            if result is None:
                state.upsert_document(
                    {
                        **row,
                        "last_rm_mtime": current_mtime,
                        "status": "export_failed",
                    }
                )
                stats["export_failed"] += 1
                print(f"export failed {uid}")
                continue

            primary_pdf = result["primary_pdf"]
            primary_md = result["primary_md"]

            canonical_pdf = doc_out_dir / f"{stem}.annotated.pdf"
            canonical_md = doc_out_dir / f"{stem}.annotations.md"
            canonical_json = doc_out_dir / f"{stem}.highlights.json"
            canonical_note = doc_out_dir / f"{stem}.zotero_note.md"

            export_hash = None

            if primary_pdf is not None:
                shutil.copy2(primary_pdf, canonical_pdf)
                export_hash = sha256_file(canonical_pdf)
            else:
                canonical_pdf = None

            if primary_md is not None:
                shutil.copy2(primary_md, canonical_md)

                md_text = canonical_md.read_text(encoding="utf-8")
                parsed = parse_obsidian_annotations(md_text)

                canonical_json.write_text(
                    json.dumps(parsed, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                zotero_note_md = build_zotero_note_markdown(parsed, stem)
                canonical_note.write_text(zotero_note_md, encoding="utf-8")
            else:
                canonical_md = None

            state.upsert_document(
                {
                    **row,
                    "last_rm_mtime": current_mtime,
                    "last_export_ts": now_ts(),
                    "last_export_pdf_path": str(canonical_pdf) if canonical_pdf else None,
                    "last_export_hash": export_hash,
                    "status": "exported",
                }
            )

            stats["exported"] += 1
            print(f"Exported bundle {uid} -> {doc_out_dir}")
            if canonical_pdf:
                print(f"Primary PDF -> {canonical_pdf}")
            if canonical_md:
                print(f"Primary Markdown -> {canonical_md}")
    finally:
        state.close()

    return stats

def do_attach(args) -> dict:
    stats = {
        "note_attached": 0,
        "pdf_attached": 0,
        "note_updated": 0,
        "pdf_replaced": 0,
        "note_skipped_same_hash": 0,
        "pdf_skipped_same_hash": 0,
        "rows_seen": 0,
        "candidates_seen": 0,
    }

    state = BridgeState(Path(args.state_db))
    writer = ZoteroWebWriter(
        library_id=args.zotero_library_id,
        library_type=args.zotero_library_type,
        api_key=args.zotero_api_key,
    )
    out_root = Path(args.annotated_dir)

    try:
        for row in state.all_documents():
            if int(row.get("active") or 1) == 0:
                continue

            stats["rows_seen"] += 1

            parent_key = row.get("zotero_parent_key")
            source_attachment_key = row.get("zotero_attachment_key")
            attachment_filename = row.get("attachment_filename")

            if not parent_key or not attachment_filename or not source_attachment_key:
                continue

            stem = Path(attachment_filename).stem
            doc_out_dir = out_root / parent_key / stem

            annotated_pdf = doc_out_dir / f"{stem}.annotated.pdf"
            zotero_note_md = doc_out_dir / f"{stem}.zotero_note.md"

            if not annotated_pdf.exists() and not zotero_note_md.exists():
                continue

            stats["candidates_seen"] += 1
            manifest = load_attach_manifest(doc_out_dir)

            # --------------------------------------------------
            # PDF SLOT
            # --------------------------------------------------
            if annotated_pdf.exists():
                pdf_hash = sha256_file(annotated_pdf)
                pdf_slot = state.get_attached_artifact(source_attachment_key, "rm2_annotated_pdf")
                existing_pdf_key = pdf_slot.get("zotero_child_key") if pdf_slot else None
                existing_pdf_exists = bool(existing_pdf_key and writer.item_exists(existing_pdf_key))

                if pdf_slot and pdf_slot.get("last_hash") == pdf_hash and existing_pdf_exists:
                    stats["pdf_skipped_same_hash"] += 1
                    vlog(args, f"skip pdf unchanged: {stem}")
                else:
                    if existing_pdf_exists:
                        writer.delete_item_by_key(existing_pdf_key)

                    before_keys = writer.child_keys(parent_key)
                    resp = writer.upload_child_attachment(parent_key, annotated_pdf)
                    created_keys = extract_created_item_keys(resp)
                    after_keys = writer.child_keys(parent_key)

                    if not created_keys:
                        created_keys = sorted(after_keys - before_keys)

                    created_key = created_keys[0] if created_keys else None

                    state.register_derived_hash(
                        sha256=pdf_hash,
                        parent_item_key=parent_key,
                        source_attachment_key=source_attachment_key,
                        artifact_type="rm2_annotated_pdf",
                    )

                    if created_key:
                        state.register_derived_attachment(
                            attachment_key=created_key,
                            parent_item_key=parent_key,
                            source_attachment_key=source_attachment_key,
                            attachment_type="rm2_annotated_pdf",
                        )

                    state.upsert_attached_artifact(
                        source_attachment_key=source_attachment_key,
                        artifact_type="rm2_annotated_pdf",
                        zotero_child_key=created_key,
                        last_hash=pdf_hash,
                    )

                    if existing_pdf_exists:
                        stats["pdf_replaced"] += 1
                        print(f"Replaced annotated PDF for {stem} -> parent {parent_key}")
                    else:
                        stats["pdf_attached"] += 1
                        print(f"Attached annotated PDF for {stem} -> parent {parent_key}")

                manifest["pdf_sha256"] = pdf_hash

            # --------------------------------------------------
            # NOTE SLOT
            # --------------------------------------------------
            if zotero_note_md.exists():
                note_hash = sha256_file(zotero_note_md)
                note_html = zotero_note_markdown_to_html(
                    zotero_note_md.read_text(encoding="utf-8")
                )

                note_slot = state.get_attached_artifact(source_attachment_key, "rm2_note")
                existing_note_key = note_slot.get("zotero_child_key") if note_slot else None
                existing_note_exists = bool(existing_note_key and writer.item_exists(existing_note_key))

                if note_slot and note_slot.get("last_hash") == note_hash and existing_note_exists:
                    stats["note_skipped_same_hash"] += 1
                    vlog(args, f"skip note unchanged: {stem}")
                else:
                    if existing_note_exists:
                        writer.update_note(existing_note_key, note_html)
                        state.upsert_attached_artifact(
                            source_attachment_key=source_attachment_key,
                            artifact_type="rm2_note",
                            zotero_child_key=existing_note_key,
                            last_hash=note_hash,
                        )
                        stats["note_updated"] += 1
                        print(f"Updated Zotero note for {stem} -> parent {parent_key}")
                    else:
                        before_keys = writer.child_keys(parent_key)
                        resp = writer.create_child_note(parent_key, note_html)
                        created_keys = extract_created_item_keys(resp)
                        after_keys = writer.child_keys(parent_key)

                        if not created_keys:
                            created_keys = sorted(after_keys - before_keys)

                        created_key = created_keys[0] if created_keys else None

                        state.upsert_attached_artifact(
                            source_attachment_key=source_attachment_key,
                            artifact_type="rm2_note",
                            zotero_child_key=created_key,
                            last_hash=note_hash,
                        )
                        stats["note_attached"] += 1
                        print(f"Attached Zotero note for {stem} -> parent {parent_key}")

                manifest["note_sha256"] = note_hash

            save_attach_manifest(doc_out_dir, manifest)
    finally:
        state.close()

    return stats


def do_sync(args) -> dict:
    reconcile_stats = do_reconcile(args)
    push_stats = do_push(args)
    pull_stats = do_pull(args)
    attach_stats = do_attach(args)

    summary = {
        "reconcile": reconcile_stats,
        "push": push_stats,
        "pull": pull_stats,
        "attach": attach_stats,
    }

    print("Sync summary:")
    print(
        f"  reconcile rows_seen={reconcile_stats['rows_seen']} "
        f"missing_zotero_attachment={reconcile_stats['missing_zotero_attachment']} "
        f"resolved_local_path={reconcile_stats['resolved_local_path']} "
        f"missing_local_file={reconcile_stats['missing_local_file']} "
        f"missing_rm_bundle={reconcile_stats['missing_rm_bundle']} "
        f"source_hash_refreshed={reconcile_stats['source_hash_refreshed']} "
        f"stale_derived_keys_removed={reconcile_stats['stale_derived_keys_removed']}"
    )
    print(
        f"  push   pushed={push_stats['pushed']} "
        f"skipped_existing={push_stats['skipped_existing']} "
        f"skipped_inactive={push_stats['skipped_inactive']} "
        f"skipped_derived_key={push_stats['skipped_derived_key']} "
        f"skipped_derived_hash={push_stats['skipped_derived_hash']}"
    )
    print(
        f"  pull   exported={pull_stats['exported']} "
        f"export_failed={pull_stats['export_failed']} "
        f"skipped_inactive={pull_stats['skipped_inactive']} "
        f"skipped_missing_rm_uuid={pull_stats['skipped_missing_rm_uuid']} "
        f"skipped_unchanged={pull_stats['skipped_unchanged']}"
    )
    print(
        f"  attach note_attached={attach_stats['note_attached']} "
        f"pdf_attached={attach_stats['pdf_attached']} "
        f"note_updated={attach_stats['note_updated']} "
        f"pdf_replaced={attach_stats['pdf_replaced']} "
        f"note_skipped_same_hash={attach_stats['note_skipped_same_hash']} "
        f"pdf_skipped_same_hash={attach_stats['pdf_skipped_same_hash']}"
    )

    return summary


# --------------------------------------------------
# CLI
# --------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inspect = sub.add_parser("inspect-zotero")
    p_inspect.add_argument("--zotero-db", required=True)
    p_inspect.add_argument("--zotero-storage-dir", required=True)
    p_inspect.add_argument("--collection-name", default="__inbox")

    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument("--zotero-db", required=True)
    p_reconcile.add_argument("--zotero-storage-dir", required=True)
    p_reconcile.add_argument("--xochitl-dir", required=True)
    p_reconcile.add_argument("--state-db", required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("--zotero-db", required=True)
    p_push.add_argument("--zotero-storage-dir", required=True)
    p_push.add_argument("--collection-name", default="__inbox")
    p_push.add_argument("--xochitl-dir", required=True)
    p_push.add_argument("--rm-parent-name", default="zotero")
    p_push.add_argument("--state-db", required=True)

    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--xochitl-dir", required=True)
    p_pull.add_argument("--annotated-dir", required=True)
    p_pull.add_argument("--remarks-bin", default="remarks")
    p_pull.add_argument("--state-db", required=True)

    p_attach = sub.add_parser("attach")
    p_attach.add_argument("--annotated-dir", required=True)
    p_attach.add_argument("--state-db", required=True)
    p_attach.add_argument("--zotero-library-id", required=True)
    p_attach.add_argument("--zotero-library-type", default="user")
    p_attach.add_argument("--zotero-api-key", required=True)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--zotero-db", required=True)
    p_sync.add_argument("--zotero-storage-dir", required=True)
    p_sync.add_argument("--collection-name", default="__inbox")
    p_sync.add_argument("--xochitl-dir", required=True)
    p_sync.add_argument("--rm-parent-name", default="zotero")
    p_sync.add_argument("--annotated-dir", required=True)
    p_sync.add_argument("--remarks-bin", default="remarks")
    p_sync.add_argument("--state-db", required=True)
    p_sync.add_argument("--zotero-library-id", required=True)
    p_sync.add_argument("--zotero-library-type", default="user")
    p_sync.add_argument("--zotero-api-key", required=True)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "inspect-zotero":
        do_inspect_zotero(args)
    elif args.cmd == "reconcile":
        do_reconcile(args)
    elif args.cmd == "push":
        do_push(args)
    elif args.cmd == "pull":
        do_pull(args)
    elif args.cmd == "attach":
        do_attach(args)
    elif args.cmd == "sync":
        do_sync(args)


if __name__ == "__main__":
    main()
