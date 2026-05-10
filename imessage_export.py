#!/usr/bin/env python3
"""
imessage_export.py — Chatbook iMessage Export Tool
====================================================
Exports an iMessage group chat to a zip file you can upload at ourchatbook.com.

Requirements
------------
  • macOS (Monterey 12+ recommended)
  • Python 3.8 or later  (built into macOS — no install needed)
  • Terminal Full Disk Access  (System Settings → Privacy & Security → Full Disk Access)
  • No third-party packages required

Usage
-----
  python3 imessage_export.py

The script will:
  1. Show your group chats — pick one by number
  2. Ask a few questions about the book (title, season, names)
  3. Copy all your media and create a self-contained zip file on your Desktop
  4. Tell you exactly what to enter when you upload on ourchatbook.com
"""

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
# iMessage sometimes stores message text only in the attributedBody binary blob.
# These functions extract the human-readable text from it.

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
    Format a datetime as a WhatsApp iOS chat.txt timestamp.
    Example: [3/15/23, 9:04:32 AM]
    Uses %-m / %-d / %-I for non-zero-padded values (macOS strftime supports this).
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


# ── Attachment staging ─────────────────────────────────────────────────────────

def _resolve_path(raw_path):
    """Expand ~ and env vars; return Path if file exists, else None."""
    if not raw_path:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(raw_path))))
    return p if p.exists() else None


def _heic_to_jpeg(src, dest):
    """
    Convert a HEIC/HEIF file to JPEG using macOS's built-in sips tool.
    No third-party libraries required.  Returns True on success.
    """
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
    Copy/convert an iMessage attachment into stage_dir with a clean filename
    that is safe to reference in a WhatsApp-format _chat.txt.

    Returns the destination filename (no path prefix) for use as
    `<attached: filename>`, or None if the file is unavailable.

    Behaviour by type:
      • HEIC/HEIF  → converted to JPEG with sips; falls back to copy-as-is
      • Images     → copied directly (WhatsApp parser will base64-embed them)
      • Videos     → copied directly (server renders as a video pill)
      • Other      → copied directly (server renders as a document pill)
      • Missing    → returns None gracefully; no crash
    """
    src = _resolve_path(raw_path)
    name = (
        transfer_name
        or (Path(raw_path).name if raw_path else None)
        or ""
    )
    if not name:
        return None

    # Skip iMessage internal payload blobs (not real user media)
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
        # File no longer exists on disk (deleted or iCloud-only)
        return None

    if ext in (".heic", ".heif"):
        dest_name = f"{prefix}_{ts}_{index:05d}.jpg"
        dest_path = stage_dir / dest_name
        if _heic_to_jpeg(src, dest_path):
            return dest_name
        # sips failed — copy the original HEIC; the server can still
        # embed it as base64 (WeasyPrint may not render it, but it won't crash)
        dest_name = f"{prefix}_{ts}_{index:05d}{ext}"
        dest_path = stage_dir / dest_name
        shutil.copy2(src, dest_path)
        return dest_name

    dest_name = f"{prefix}_{ts}_{index:05d}{ext}"
    dest_path = stage_dir / dest_name
    shutil.copy2(src, dest_path)
    return dest_name


# ── _chat.txt builder ──────────────────────────────────────────────────────────

def build_chat_txt(messages, handle_map, nickname_map, my_name, cur, stage_dir):
    """
    Convert iMessage DB rows to WhatsApp iOS _chat.txt format.

    Format for regular messages:
        [M/D/YY, H:MM:SS AM] Name: message text
        [M/D/YY, H:MM:SS AM] Name: <attached: IMG_20230115_093012_00001.jpg>

    Format for system events (no sender):
        [M/D/YY, H:MM:SS AM] Sarah added Alex to the group

    Rules applied:
      • Reactions (associated_message_type in REACTION_TYPES) are skipped
      • Messages with item_type != 0 are treated as system events
      • Each attachment in a multi-attachment iMessage gets its own line
      • Inline text and attachments are split onto separate lines
      • No live network calls; no qlmanage; no third-party libs
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

        # ── Skip reactions ─────────────────────────────────────────────────────
        if assoc_type and assoc_type in REACTION_TYPES:
            skipped_reactions += 1
            continue

        # ── System events (item_type != 0) ─────────────────────────────────────
        # These are group membership changes, name changes, etc.
        # Written as timestamp-only lines (no sender field) so the WhatsApp
        # parser treats them as system events, not attributed messages.
        if item_type != 0:
            label = (
                group_title
                or get_message_text(text_col, attributed_body)
                or "Group updated"
            )
            label = label.strip().replace("\n", " ")
            # Guard: any colon in system event text would cause _MSG_RE to
            # misparse it as "Name: message" (the name capture is [^:]+?).
            # Replace all colons with an em dash to keep the line safe.
            label = label.replace(":", "\u2014")
            if label:
                lines.append(f"{ts} {label}")
            continue

        # ── Regular message ────────────────────────────────────────────────────

        # Sender display name
        if is_from_me:
            sender = my_name
        else:
            phone  = handle_map.get(handle_id, f"unknown_{handle_id}")
            sender = nickname_map.get(phone, phone)

        # Message text
        text = (get_message_text(text_col, attributed_body) or "").strip()

        # ── Attachments ────────────────────────────────────────────────────────
        att_fnames = []
        if cache_has_attachments:
            att_rows = get_attachments(cur, msg_id)
            for raw_path, mime_type, transfer_name in att_rows:
                att_index[0] += 1
                ext = Path(transfer_name or raw_path or "").suffix.lower()
                fname = stage_attachment(
                    raw_path, mime_type, transfer_name,
                    stage_dir, dt, att_index[0],
                )
                if fname:
                    att_fnames.append(fname)
                    if ext in IMAGE_EXTS:
                        images_staged += 1
                    elif ext in VIDEO_EXTS:
                        videos_staged += 1
                    else:
                        other_staged += 1
                else:
                    missing_files += 1

        # ── Build _chat.txt lines ──────────────────────────────────────────────
        #
        # The WhatsApp parser's _ATTACHED_SEARCH captures only the FIRST
        # <attached: ...> per message line.  For multi-attachment iMessages
        # we therefore write each attachment as its own timestamped line.
        #
        # Layout:
        #   • Text (if present)  → one line: "[ts] Name: text"
        #   • Each attachment    → one line: "[ts] Name: <attached: fname>"
        #
        # If only one attachment and no text, just one line.

        if text:
            lines.append(f"{ts} {sender}: {text}")

        for fname in att_fnames:
            lines.append(f"{ts} {sender}: <attached: {fname}>")

        # If message had nothing (empty text, all attachments missing) skip it.
        if not text and not att_fnames:
            continue

    print(f"    Skipped  {skipped_reactions:>5}  reactions")
    print(f"    Missing  {missing_files:>5}  attachment(s) not on disk (iCloud-only or deleted)")
    print(f"    Staged   {images_staged:>5}  image(s)")
    print(f"    Staged   {videos_staged:>5}  video(s)")
    print(f"    Staged   {other_staged:>5}  other file(s)")

    return "\n".join(lines) + "\n"


# ── Interactive prompts ────────────────────────────────────────────────────────

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


def prompt_book_details(default_title):
    print(_hr())
    print("Book details")
    print(_hr())
    print(f"\nBook title  (will be embossed on the cover)")
    title = input(f"  [{default_title}]: ").strip()
    if not title:
        title = default_title

    print("\nSeason number  (most people start at 1)")
    season_raw = input("  [1]: ").strip()
    try:
        season = int(season_raw) if season_raw else 1
        if season < 1:
            season = 1
    except ValueError:
        season = 1

    return title, season


def prompt_my_name():
    print()
    print(_hr())
    print("Your name")
    print(_hr())
    print("\nYour iMessages will appear under this name in the book.")
    print("Enter it exactly as you'd like it printed.\n")
    my_name = input("  Your name: ").strip()
    if not my_name:
        my_name = "Me"
    return my_name


def prompt_nicknames(handle_map):
    """
    Prompt for a display name for each phone number / Apple ID in the chat.
    Phone numbers are what iMessage stores; we ask the customer to map them
    to real names for the cast page and message attribution.
    """
    if not handle_map:
        return {}
    print()
    print(_hr())
    print("Participant names")
    print(_hr())
    print(
        f"\nFound {len(handle_map)} other participant(s).\n"
        "Press Enter to keep the raw identifier, or type a display name.\n"
    )
    nickname_map = {}
    for phone in sorted(handle_map.values()):
        nick = input(f'  "{phone}":  ').strip()
        nickname_map[phone] = nick if nick else phone
    return nickname_map


def prompt_extras():
    print()
    print(_hr())
    print("Optional: Dedication & Closing Note")
    print(_hr())
    print(
        "\nDedication  (appears on the dedication page at the front of the book)\n"
        "Max 200 characters. Press Enter to skip.\n"
    )
    dedication = input("  Dedication: ").strip()[:200]

    print(
        "\nClosing note  (appears on the final page — a note from you to everyone)\n"
        "Max 400 characters. Press Enter to skip.\n"
    )
    closing_note = input("  Closing note: ").strip()[:400]

    return dedication, closing_note


# ── Zip builder ────────────────────────────────────────────────────────────────

def build_zip(chat_txt_content, stage_dir, output_path):
    """
    Bundle _chat.txt and all staged media files into a flat zip.

    Flat structure (all files at the root of the zip) matches how
    WhatsApp iOS exports a chat, so the server parser finds _chat.txt
    immediately with os.walk and resolves attachment filenames correctly.
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # _chat.txt first (cosmetic — parsers don't care about order)
        zf.writestr("_chat.txt", chat_txt_content.encode("utf-8"))
        media_files = sorted(f for f in stage_dir.iterdir() if f.is_file())
        for f in media_files:
            zf.write(f, f.name)  # arcname = bare filename, no path prefix
    return len(media_files)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # ── Open database ──────────────────────────────────────────────────────────
    conn = open_db()
    cur  = conn.cursor()

    # ── Step 1: pick a chat ────────────────────────────────────────────────────
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

    # ── Step 2: book details ───────────────────────────────────────────────────
    title, season = prompt_book_details(default_title=display_name)

    # ── Step 3: participants ───────────────────────────────────────────────────
    handle_map   = get_handle_map(cur, chat_id)
    my_name      = prompt_my_name()
    nickname_map = prompt_nicknames(handle_map)

    # ── Step 4: dedication + closing note ──────────────────────────────────────
    dedication, closing_note = prompt_extras()

    # ── Confirm before processing ──────────────────────────────────────────────
    print()
    print(_hr("═"))
    print("Summary — please confirm before exporting")
    print(_hr("═"))
    print(f"\n  Chat      :  {display_name}")
    print(f"  Title     :  {title}  (Season {season})")
    print(f"  Your name :  {my_name}")
    if handle_map:
        for phone, nick in sorted(nickname_map.items(), key=lambda x: x[1]):
            print(f"  Participant: {nick}  [{phone}]")
    if dedication:
        print(f"  Dedication:  {dedication[:70]}{'…' if len(dedication) > 70 else ''}")
    if closing_note:
        print(f"  Closing:     {closing_note[:70]}{'…' if len(closing_note) > 70 else ''}")

    print()
    try:
        input("Press Enter to start the export, or Ctrl+C to cancel: ")
    except KeyboardInterrupt:
        print("\n\n  Export cancelled.")
        sys.exit(0)

    # ── Step 5: fetch messages ─────────────────────────────────────────────────
    print()
    print(_hr())
    print("Exporting…")
    print(_hr())
    print("\nReading messages from iMessage database…")
    messages = get_messages(cur, chat_id)
    total_rows = len(messages)
    real_msgs  = sum(
        1 for row in messages
        if row[8] == 0  # item_type == 0
        and not (row[7] and row[7] in REACTION_TYPES)  # not a reaction
    )
    print(f"  {total_rows} database rows ({real_msgs} messages, rest are reactions/system)")

    # ── Step 6: stage attachments + write _chat.txt ────────────────────────────
    with tempfile.TemporaryDirectory(prefix="chatbook_") as tmp:
        stage_dir = Path(tmp)

        print("\nConverting messages and collecting media files…")
        chat_txt = build_chat_txt(
            messages, handle_map, nickname_map, my_name, cur, stage_dir
        )
        conn.close()

        # ── Step 7: build zip ──────────────────────────────────────────────────
        safe_title  = re.sub(r"[^\w\s\-]", "", title).strip()[:40]
        zip_name    = f"Chatbook - {safe_title} - Season {season}.zip"
        output_path = HOME / "Desktop" / zip_name

        print(f"\nBuilding zip file…")
        media_count = build_zip(chat_txt, stage_dir, output_path)
        size_mb     = output_path.stat().st_size / 1_048_576
        print(f"  {media_count} media file(s) + _chat.txt  →  {size_mb:.1f} MB")

    # ── Done ───────────────────────────────────────────────────────────────────
    print()
    print(_hr("═"))
    print("  ✓  Export complete!")
    print(_hr("═"))
    print(f"\n  File saved to your Desktop:\n  {zip_name}")

    print()
    print(_hr())
    print("Next steps — what to do on ourchatbook.com")
    print(_hr())
    print(
        "\n  1.  Go to ourchatbook.com and click 'Build your Chatbook'\n"
        "  2.  On the platform screen, select WhatsApp\n"
        "  3.  Upload the zip file from your Desktop\n"
        f"  4.  Book title:  {title}\n"
        f"  5.  Season:      {season}\n"
    )

    # Print participants for the wizard's nickname screen
    if nickname_map:
        print("  6.  Participant nicknames to enter in the wizard:")
        for phone, nick in sorted(nickname_map.items(), key=lambda x: x[1]):
            print(f"        {nick}")

    if dedication or closing_note:
        print()
        print("  Copy-paste these into the wizard when prompted:\n")
        if dedication:
            print(f"  Dedication:\n  {dedication}\n")
        if closing_note:
            print(f"  Closing note:\n  {closing_note}\n")

    print()
    print(
        "  Questions? Email hello@ourchatbook.com\n"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Export cancelled.")
        sys.exit(0)
