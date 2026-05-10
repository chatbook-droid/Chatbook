#!/usr/bin/env python3
"""
chatbook-imessage.py — Chatbook iMessage Export Tool
=====================================================
Exports an iMessage group chat to a zip file you can upload at ourchatbook.com.

Requirements
------------
  • macOS (Monterey 12+ recommended)
  • Python 3.8 or later  (built into macOS — no install needed)
  • Terminal Full Disk Access  (System Settings → Privacy & Security → Full Disk Access)
  • No third-party packages required

Usage
-----
  python3 chatbook-imessage.py

The script will:
  1. Show your group chats — pick one by number
  2. Export all messages and media to a zip file on your Desktop
  3. Upload the zip at ourchatbook.com to continue
"""

import glob
import json
import os
import re
import sys
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

HOME    = Path.home()
DB_PATH = HOME / "Library" / "Messages" / "chat.db"

# ── iMessage constants ─────────────────────────────────────────────────────────

MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# associated_message_type values that are reactions, not messages
REACTION_TYPES = {
    2000, 2001, 2002, 2003, 2004, 2005,
    3000, 3001, 3002, 3003, 3004, 3005,
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff",
              ".heic", ".heif", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}

# Binary artifacts that appear after real text in attributedBody blobs
_BINARY_ARTIFACTS = [
    "NSDictionary", "__kIMMessagePartAttributeName",
    "NSKeyedArchiver", "bplist", "NS.rangeval",
]

# ── Binary text extraction ─────────────────────────────────────────────────────

def _clean_text(text):
    if not text:
        return text
    cut = len(text)
    for marker in _BINARY_ARTIFACTS:
        idx = text.find(marker)
        if idx != -1 and idx < cut:
            cut = idx
    text = text[:cut]
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\ufffd]+$", "", text)
    return text.strip()


def _extract_typedstream_text(data):
    """Pull the UTF-8 string out of an NSAttributedString typedstream blob."""
    if not data:
        return None
    data = bytes(data)
    idx = data.find(b"\x84\x01\x2b")
    if idx == -1:
        return None
    pos = idx + 3
    if pos >= len(data):
        return None
    b = data[pos]
    if b == 0x81:
        if pos + 3 > len(data):
            return None
        length = int.from_bytes(data[pos + 1 : pos + 3], "big")
        pos += 3
    elif b == 0x82:
        if pos + 5 > len(data):
            return None
        length = int.from_bytes(data[pos + 1 : pos + 5], "big")
        pos += 5
    else:
        length = b
        pos += 1
    text_bytes = data[pos : pos + length]
    x86 = text_bytes.find(b"\x86")
    if x86 != -1:
        text_bytes = text_bytes[:x86]
    raw = text_bytes.decode("utf-8", errors="replace").replace("\ufffc", "")
    return _clean_text(raw) or None


def get_message_text(text_col, attributed_body):
    """Return the best available plain text for a message row."""
    if text_col:
        clean = _clean_text(str(text_col).replace("\ufffc", ""))
        if clean:
            return clean
    return _extract_typedstream_text(attributed_body)


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def mac_ns_to_dt(ns):
    """Convert Mac absolute time (nanoseconds since 2001-01-01) to local datetime."""
    if not ns:
        return None
    return (MAC_EPOCH + timedelta(seconds=ns / 1_000_000_000)).astimezone()


def dt_to_wa_timestamp(dt):
    """
    Format a datetime as a WhatsApp iOS _chat.txt timestamp.
    Example: [3/15/23, 9:04:32 AM]
    """
    if not dt:
        return "[1/1/00, 12:00:00 AM]"
    return dt.strftime("[%-m/%-d/%y, %-I:%M:%S %p]")


# ── Database helpers ───────────────────────────────────────────────────────────

def open_db():
    """Open chat.db read-only; falls back to read-write if URI mode fails."""
    if not DB_PATH.exists():
        print(
            f"\n  ✗  iMessage database not found at:\n     {DB_PATH}\n"
            "\n  Make sure you're running this on a Mac where iMessage is set up."
        )
        sys.exit(1)
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        try:
            return sqlite3.connect(str(DB_PATH))
        except sqlite3.OperationalError as e:
            print(
                f"\n  ✗  Could not open iMessage database: {e}\n"
                "\n  You may need to grant Terminal Full Disk Access:\n"
                "     System Settings → Privacy & Security → Full Disk Access\n"
                "  Add Terminal (or iTerm2) to the list, then try again."
            )
            sys.exit(1)


def list_group_chats(cur):
    """Return [(rowid, display_name, last_date_ns)] newest-active first."""
    cur.execute(
        """
        SELECT c.ROWID, c.display_name, MAX(m.date) AS last_date
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON cmj.message_id = m.ROWID
        WHERE c.display_name IS NOT NULL AND c.display_name != ''
        GROUP BY c.ROWID
        ORDER BY last_date DESC
        """
    )
    return cur.fetchall()


def get_handle_map(cur, chat_id):
    """Return {handle_rowid: phone_or_email} for all participants in a chat."""
    cur.execute(
        "SELECT h.ROWID, h.id "
        "FROM handle h "
        "JOIN chat_handle_join chj ON h.ROWID = chj.handle_id "
        "WHERE chj.chat_id = ?",
        (chat_id,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_messages(cur, chat_id):
    cur.execute(
        """
        SELECT m.ROWID, m.text, m.attributedBody, m.date,
               m.is_from_me, m.handle_id,
               m.cache_has_attachments, m.associated_message_type,
               m.item_type, m.group_title
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC
        """,
        (chat_id,),
    )
    return cur.fetchall()


def get_attachments(cur, msg_id):
    cur.execute(
        "SELECT a.filename, a.mime_type, a.transfer_name "
        "FROM attachment a "
        "JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id "
        "WHERE maj.message_id = ?",
        (msg_id,),
    )
    return cur.fetchall()


def get_my_sender(cur, chat_id):
    """
    Find the local account identifier for outgoing messages.

    1. Check chat.account_login — stored as 'P:+15555551234' (phone) or
       'E:user@example.com' (Apple ID / email).  Strip the prefix.
    2. Fall back to message.account on outgoing messages in this chat,
       which uses the same P:/E: encoding.
    3. Return None on any failure — caller falls back to "Me".
    """
    # Strategy 1: chat.account_login
    try:
        cur.execute("SELECT account_login FROM chat WHERE ROWID = ?", (chat_id,))
        row = cur.fetchone()
        if row and row[0]:
            val = row[0].strip()
            if val.startswith(("P:", "E:")):
                val = val[2:]
            if val:
                return val
    except Exception:
        pass

    # Strategy 2: message.account on any outgoing message in this chat
    try:
        cur.execute(
            """
            SELECT m.account
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id = ? AND m.is_from_me = 1 AND m.account IS NOT NULL
            LIMIT 1
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            val = row[0].strip()
            if val.startswith(("P:", "E:")):
                val = val[2:]
            if val:
                return val
    except Exception:
        pass

    return None


def resolve_apple_id_to_phone(email):
    """
    Given an Apple ID email, look it up in the macOS AddressBook SQLite database
    and return the first associated phone number, or None if not found.

    Searches all AddressBook sources; returns None silently on any failure.
    """
    ab_dir = HOME / "Library" / "Application Support" / "AddressBook" / "Sources"
    try:
        ab_paths = list(ab_dir.glob("*/AddressBook-v22.abcddb"))
    except Exception:
        return None

    for ab_path in ab_paths:
        try:
            conn = sqlite3.connect(f"file:{ab_path}?mode=ro", uri=True)
            c    = conn.cursor()

            # Find the record that owns this email address
            c.execute(
                "SELECT ZOWNER FROM ZABCDEMAILADDRESS WHERE LOWER(ZADDRESS) = LOWER(?)",
                (email.strip(),),
            )
            row = c.fetchone()
            if not row:
                conn.close()
                continue
            owner_pk = row[0]

            # Grab the first phone number for that record
            c.execute(
                "SELECT ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZOWNER = ? LIMIT 1",
                (owner_pk,),
            )
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                digits = re.sub(r"\D", "", row[0])
                if len(digits) >= 7:
                    return row[0].strip()
        except Exception:
            pass

    return None


# ── Attachment staging ─────────────────────────────────────────────────────────

def _resolve_path(raw_path):
    """Expand ~ and env vars; return Path if file exists, else None."""
    if not raw_path:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(raw_path))))
    return p if p.exists() else None


def _heic_to_jpeg(src, dest):
    """Convert a HEIC/HEIF file to JPEG using macOS's built-in sips tool."""
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(src), "--out", str(dest)],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def stage_attachment(raw_path, mime_type, transfer_name, stage_dir, dt, index):
    """
    Copy/convert an attachment into stage_dir with a clean filename safe for _chat.txt.
    Returns the destination filename (bare, no path prefix), or None if unavailable.
    """
    src = _resolve_path(raw_path)
    name = (
        transfer_name
        or (Path(raw_path).name if raw_path else None)
        or ""
    )
    if not name:
        return None

    if name.endswith(".pluginPayloadAttachment"):
        return None

    ext = Path(name).suffix.lower()
    ts = dt.strftime("%Y%m%d_%H%M%S") if dt else "00000000_000000"

    if ext in IMAGE_EXTS:
        prefix = "IMG"
    elif ext in VIDEO_EXTS:
        prefix = "VID"
    else:
        prefix = "ATT"

    if not src:
        return None

    if ext in (".heic", ".heif"):
        dest_name = f"{prefix}_{ts}_{index:05d}.jpg"
        dest_path = stage_dir / dest_name
        if _heic_to_jpeg(src, dest_path):
            return dest_name
        # sips failed — copy original; server can still embed as base64
        dest_name = f"{prefix}_{ts}_{index:05d}{ext}"
        shutil.copy2(src, stage_dir / dest_name)
        return dest_name

    dest_name = f"{prefix}_{ts}_{index:05d}{ext}"
    shutil.copy2(src, stage_dir / dest_name)
    return dest_name


# ── _chat.txt builder ──────────────────────────────────────────────────────────

def build_chat_txt(messages, handle_map, my_sender, cur, stage_dir):
    """
    Convert iMessage DB rows to WhatsApp iOS _chat.txt format.

    Outgoing messages (is_from_me=1) use my_sender (the account's phone number
    or Apple ID extracted from the database).  Other senders use their raw phone
    number or Apple ID — the wizard collects nicknames just like WhatsApp exports.

    Each attachment gets its own timestamped line because the server
    parser's _ATTACHED_SEARCH captures only the first <attached:> per line.
    """
    lines = []
    att_index = [0]
    skipped_reactions = 0
    missing_files     = 0
    images_staged     = 0
    videos_staged     = 0
    other_staged      = 0

    for row in messages:
        (
            msg_id, text_col, attributed_body, date_ns,
            is_from_me, handle_id,
            cache_has_attachments, assoc_type,
            item_type, group_title,
        ) = row

        dt = mac_ns_to_dt(date_ns)
        ts = dt_to_wa_timestamp(dt)

        # Skip reactions
        if assoc_type and assoc_type in REACTION_TYPES:
            skipped_reactions += 1
            continue

        # System events (group membership changes, name changes, etc.)
        if item_type != 0:
            label = (
                group_title
                or get_message_text(text_col, attributed_body)
                or "Group updated"
            )
            label = label.strip().replace("\n", " ").replace(":", "\u2014")
            if label:
                lines.append(f"{ts} {label}")
            continue

        # Regular message
        sender = my_sender if is_from_me else handle_map.get(handle_id, f"unknown_{handle_id}")
        text   = (get_message_text(text_col, attributed_body) or "").strip()

        att_fnames = []
        if cache_has_attachments:
            for raw_path, mime_type, transfer_name in get_attachments(cur, msg_id):
                att_index[0] += 1
                ext   = Path(transfer_name or raw_path or "").suffix.lower()
                fname = stage_attachment(
                    raw_path, mime_type, transfer_name,
                    stage_dir, dt, att_index[0],
                )
                if fname:
                    att_fnames.append(fname)
                    if ext in IMAGE_EXTS:   images_staged += 1
                    elif ext in VIDEO_EXTS: videos_staged += 1
                    else:                   other_staged  += 1
                else:
                    missing_files += 1

        if text:
            lines.append(f"{ts} {sender}: {text}")
        for fname in att_fnames:
            lines.append(f"{ts} {sender}: <attached: {fname}>")

    print(f"    Skipped  {skipped_reactions:>5}  reactions")
    print(f"    iCloud   {missing_files:>5}  file(s) stored in iCloud only — skipped")
    print(f"    Staged   {images_staged:>5}  image(s)")
    print(f"    Staged   {videos_staged:>5}  video(s)")
    print(f"    Staged   {other_staged:>5}  other file(s)")

    return "\n".join(lines) + "\n"


# ── Contacts lookup ────────────────────────────────────────────────────────────

def lookup_contacts(handle_ids):
    """
    Attempt to read the macOS AddressBook SQLite database and return a dict
    mapping {phone_or_email → display_name} for participants in the chat.

    Tries every AddressBook source found under:
      ~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb

    Phone numbers are matched by comparing digit strings (handles country-code
    variations such as +1 (212) 555-1234 vs +12125551234).

    Returns {} silently on any failure — permission denied, missing file,
    schema mismatch, or any other exception.
    """
    identifiers = set(handle_ids)
    if not identifiers:
        return {}

    ab_dir = HOME / "Library" / "Application Support" / "AddressBook" / "Sources"
    try:
        ab_paths = list(ab_dir.glob("*/AddressBook-v22.abcddb"))
    except Exception:
        return {}
    if not ab_paths:
        return {}

    contact_map = {}

    for ab_path in ab_paths:
        try:
            conn = sqlite3.connect(f"file:{ab_path}?mode=ro", uri=True)
            cur  = conn.cursor()

            # Build pk → display name from ZABCDRECORD
            cur.execute(
                """
                SELECT Z_PK,
                       TRIM(COALESCE(ZFIRSTNAME,'') || ' ' || COALESCE(ZLASTNAME,'')),
                       COALESCE(ZORGANIZATION, '')
                FROM ZABCDRECORD
                """
            )
            name_map = {}
            for pk, full_name, org in cur.fetchall():
                n = full_name.strip() or org.strip()
                if n:
                    name_map[pk] = n

            # Match phone numbers
            cur.execute(
                "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
            )
            for owner, raw_num in cur.fetchall():
                display = name_map.get(owner)
                if not display:
                    continue
                ab_digits = re.sub(r"\D", "", raw_num)
                if len(ab_digits) < 7:
                    continue
                for ident in identifiers:
                    if "@" in ident or ident in contact_map:
                        continue
                    id_digits = re.sub(r"\D", "", ident)
                    if len(id_digits) < 7:
                        continue
                    # One digit string is a suffix of the other — handles
                    # country-code prefix variations in either direction.
                    if ab_digits.endswith(id_digits) or id_digits.endswith(ab_digits):
                        contact_map[ident] = display

            # Match email / Apple ID
            cur.execute(
                "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
            )
            for owner, email in cur.fetchall():
                display = name_map.get(owner)
                if not display:
                    continue
                for ident in identifiers:
                    if "@" in ident and ident.lower() == email.lower().strip():
                        contact_map[ident] = display

            conn.close()
        except Exception:
            pass  # silently skip any source that errors

    return contact_map


# ── Zip builder ────────────────────────────────────────────────────────────────

def build_zip(chat_txt_content, stage_dir, output_path, contacts=None):
    """
    Bundle _chat.txt and all staged media into a flat zip.
    Flat structure matches WhatsApp iOS exports so the server parser
    finds _chat.txt immediately and resolves attachment filenames correctly.
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_chat.txt", chat_txt_content.encode("utf-8"))
        if contacts:
            zf.writestr("contacts.json", json.dumps(contacts, ensure_ascii=False))
        media_files = sorted(f for f in stage_dir.iterdir() if f.is_file())
        for f in media_files:
            zf.write(f, f.name)
    return len(media_files)


# ── Chat picker ────────────────────────────────────────────────────────────────

def _hr(char="─", width=56):
    return char * width


def pick_chat(chats):
    print()
    print("┌" + _hr() + "┐")
    print("│  Chatbook — iMessage Export Tool" + " " * 22 + "│")
    print("│  ourchatbook.com" + " " * 38 + "│")
    print("└" + _hr() + "┘")
    print(f"\nFound {len(chats)} named group chat(s), most recent first:\n")
    for i, (rowid, name, last_ns) in enumerate(chats, 1):
        dt = mac_ns_to_dt(last_ns)
        date_str = dt.strftime("%-m/%-d/%y") if dt else "unknown"
        print(f"  {i:3}.  {name}  [{date_str}]")
    print()
    while True:
        raw = input(f"Which chat? Enter a number (1–{len(chats)}): ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(chats):
                chat_id, display_name, _ = chats[idx]
                print(f"\n  ✓  \"{display_name}\" selected.\n")
                return chat_id, display_name
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(chats)}.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    conn = open_db()
    cur  = conn.cursor()

    chats = list_group_chats(cur)
    if not chats:
        print(
            "\n  ✗  No named group chats found in your iMessage history.\n"
            "\n  Only group chats with a custom name are listed.\n"
            "  To name a chat: open it in Messages → tap/click the group name at the top\n"
            "  → set a group name → try this script again."
        )
        sys.exit(1)

    chat_id, display_name = pick_chat(chats)
    handle_map = get_handle_map(cur, chat_id)

    # ── Resolve outgoing sender identifier ────────────────────────────────────
    my_sender_raw = get_my_sender(cur, chat_id)

    # If the account identifier is an Apple ID email, try to swap it for the
    # associated phone number so it appears in the wizard like other participants.
    if my_sender_raw and "@" in my_sender_raw:
        phone = resolve_apple_id_to_phone(my_sender_raw)
        if phone:
            my_sender_raw = phone

    my_sender = my_sender_raw or "Me"

    # ── Contacts lookup ────────────────────────────────────────────────────────
    print(_hr())
    print(
        "Looking up participant names in your Contacts…\n"
        "  (Reading a local database on your Mac — nothing leaves your computer.)"
    )
    all_identifiers = list(handle_map.values())
    if my_sender_raw:
        all_identifiers.append(my_sender_raw)
        print(f"  ✓  Your account: {my_sender_raw}")
    else:
        print("  –  Could not find your phone number — outgoing messages labeled: Me")
    contacts = lookup_contacts(all_identifiers)
    if contacts:
        print(f"  ✓  Matched {len(contacts)} contact name(s).")
    else:
        print("  –  No contacts matched — you can add names in the wizard.")
    print()

    print("Reading messages…")
    messages   = get_messages(cur, chat_id)
    total_rows = len(messages)
    real_msgs  = sum(
        1 for row in messages
        if row[8] == 0 and not (row[7] and row[7] in REACTION_TYPES)
    )
    print(f"  {total_rows} database rows ({real_msgs} messages, rest are reactions/system)\n")

    with tempfile.TemporaryDirectory(prefix="chatbook_") as tmp:
        stage_dir = Path(tmp)

        print("Collecting media files…")
        chat_txt = build_chat_txt(messages, handle_map, my_sender, cur, stage_dir)
        conn.close()

        safe_name   = re.sub(r"[^\w\s\-]", "", display_name).strip()[:40]
        zip_name    = f"Chatbook - {safe_name}.zip"
        output_path = HOME / "Desktop" / zip_name

        print(f"\nBuilding zip…")
        media_count = build_zip(chat_txt, stage_dir, output_path, contacts=contacts)
        size_mb     = output_path.stat().st_size / 1_048_576
        print(f"  {media_count} media file(s) + _chat.txt  →  {size_mb:.1f} MB")

    print()
    print(_hr("═"))
    print("  ✓  Done!")
    print(_hr("═"))
    print(
        f"\n  File saved to your Desktop:\n  {zip_name}\n"
        "\n  Drop it on ourchatbook.com/imessage to continue.\n"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Export cancelled.")
        sys.exit(0)
