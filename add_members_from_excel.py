import argparse
import csv
import os
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import pandas as pd
import phonenumbers
import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    PeerFloodError,
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    InviteHashExpiredError,
    ChatWriteForbiddenError,
)
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.functions.channels import (
    InviteToChannelRequest,
    GetParticipantRequest,
    GetFullChannelRequest,
)
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from urllib.parse import urlparse, parse_qs
from telethon.tl.types import InputPhoneContact, Channel, Chat, User

# Load .env
load_dotenv()

# --- Config pulled from environment (.env) ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "telegram_adder_session")

TELEGRAM_GROUP = os.getenv("TELEGRAM_GROUP", "").strip()
INVITE_LINK = os.getenv("INVITE_LINK", "").strip()

EXCEL_URL = os.getenv("EXCEL_URL", "").strip()
EXCEL_PATH = os.getenv("EXCEL_PATH", "").strip()
PHONE_COLUMN = os.getenv("PHONE_COLUMN", "phone")
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "").strip().upper()

DM_TEMPLATE = os.getenv(
    "DM_TEMPLATE",
    "Hi {first}, I tried to add you to {group} but Telegram privacy or permissions blocked it. "
    "You can join directly using this link: {link}",
)

SLEEP_BETWEEN_ADDS = float(os.getenv("SLEEP_BETWEEN_ADDS", "2"))
SLEEP_BETWEEN_DMS = float(os.getenv("SLEEP_BETWEEN_DMS", "2"))
BATCH_EVERY = int(os.getenv("BATCH_EVERY", "25"))
BATCH_SLEEP = float(os.getenv("BATCH_SLEEP", "30"))

LOG_FILE = "add_members_log.csv"


def download_excel_if_needed(excel_url: str, excel_path: str) -> pd.DataFrame:
    """Download/read the Excel from URL or local path."""
    if excel_url:
        resp = requests.get(excel_url, timeout=60)
        resp.raise_for_status()
        return pd.read_excel(BytesIO(resp.content))
    if excel_path:
        return pd.read_excel(excel_path)
    raise ValueError("You must set EXCEL_URL or EXCEL_PATH.")


def normalize_phone(raw: str, default_region: str) -> Optional[str]:
    """Normalize to E.164 (+15551234567). Returns None if invalid."""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return None

    # Convert "00…" international prefix to "+"
    raw = re.sub(r"^00", "+", raw)

    try:
        if raw.startswith("+") or not default_region:
            num = phonenumbers.parse(raw, None)
        else:
            num = phonenumbers.parse(raw, default_region)
        if not (phonenumbers.is_possible_number(num) and phonenumbers.is_valid_number(num)):
            return None
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return None


def load_phones(df: pd.DataFrame, phone_col: str, default_region: str) -> list[str]:
    """Extract and normalize phones from the dataframe."""
    cols = {c.lower(): c for c in df.columns}
    if phone_col.lower() not in cols:
        raise ValueError(f"Column '{phone_col}' not found. Available: {list(df.columns)}")
    normalized = []
    for raw in df[cols[phone_col.lower()]].astype(str):
        p = normalize_phone(raw, default_region)
        if p:
            normalized.append(p)
    # Deduplicate while preserving order
    out, seen = [], set()
    for p in normalized:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


async def resolve_group(client: TelegramClient, group_str: str):
    """Resolve @username / t.me/xxx / t.me invite / numeric ID to an entity.
    If given an invite link (t.me/+... or t.me/joinchat/...), attempts to join it first.
    """
    s = group_str.strip()

    # 1) Numeric chat ID (including -100... for channels)
    if re.fullmatch(r"-?\d+", s):
        try:
            return await client.get_entity(int(s))
        except Exception:
            pass

    # 2) t.me links (invite or username)
    if s.startswith("http://") or s.startswith("https://"):
        try:
            u = urlparse(s)
            if u.netloc in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
                path = u.path.lstrip("/")
                # Handle tg://join?invite=HASH format in query
                qs = parse_qs(u.query or "")
                if "invite" in qs and qs["invite"]:
                    invite_hash = qs["invite"][0]
                else:
                    # t.me/+HASH or t.me/joinchat/HASH
                    if path.startswith("+"):
                        invite_hash = path[1:]
                    elif path.startswith("joinchat/"):
                        invite_hash = path.split("/", 1)[1]
                    else:
                        invite_hash = ""

                if invite_hash:
                    try:
                        check = await client(CheckChatInviteRequest(invite_hash))
                        # If already a participant, object usually has a 'chat' attribute
                        existing_chat = getattr(check, "chat", None)
                        if existing_chat is not None:
                            return existing_chat
                    except Exception:
                        pass

                    # Try to join and return the resulting chat entity
                    try:
                        updates = await client(ImportChatInviteRequest(invite_hash))
                        chats = getattr(updates, "chats", None)
                        if chats:
                            return chats[0]
                    except Exception:
                        # Fall through and try username resolution
                        pass

                # Not an invite link → likely a public username URL: t.me/username
                if path:
                    try:
                        return await client.get_entity(path)
                    except Exception:
                        pass
        except Exception:
            # Ignore URL parsing issues; fall back to default handling
            pass

    # 3) @username or raw username or other forms that Telethon can resolve
    try:
        return await client.get_entity(s)
    except Exception as e:
        raise ValueError(f"Could not resolve group from '{group_str}': {e}")


async def is_member(client: TelegramClient, group, user: User) -> bool:
    """Return True if user is already a participant."""
    try:
        if isinstance(group, Channel):
            await client(GetParticipantRequest(group, user))
            return True
        elif isinstance(group, Chat):
            # For basic groups, we'll detect membership via add attempt exceptions.
            return False
        else:
            return False
    except Exception:
        return False


async def add_to_group(client: TelegramClient, group, user: User) -> str:
    """Attempt to add user to the group; return status string."""
    try:
        if isinstance(group, Channel):
            await client(InviteToChannelRequest(group, [user]))
        elif isinstance(group, Chat):
            await client(AddChatUserRequest(group.id, user, fwd_limit=0))
        else:
            return "unsupported_group_type"
        return "added"
    except UserAlreadyParticipantError:
        return "already_member"
    except UserPrivacyRestrictedError:
        return "blocked_by_privacy"
    except ChatAdminRequiredError:
        return "not_admin_or_no_add_permission"
    except ChannelPrivateError:
        return "group_private_or_inaccessible"
    except InviteHashExpiredError:
        return "invite_hash_expired"
    except PeerFloodError:
        return "peer_flood_stop_and_wait"
    except FloodWaitError as e:
        return f"rate_limited_wait_{getattr(e, 'seconds', 'unknown')}s"
    except Exception as e:
        return f"error_{type(e).__name__}"


async def import_contact_get_user(client: TelegramClient, phone: str) -> Optional[User]:
    """Import phone as a contact to resolve a Telegram User."""
    imported = await client(
        ImportContactsRequest(
            contacts=[InputPhoneContact(client_id=0, phone=phone, first_name="", last_name="")]
        )
    )
    users = imported.users
    return users[0] if users else None


async def ensure_invite_link(
    client: TelegramClient,
    group,
    provided_link: str,
) -> str:
    """Return a usable invite link.
    If not provided, try exporting one (requires admin privileges).
    Uses ExportChatInviteRequest which works for chats and channels.
    """
    if provided_link:
        return provided_link
    try:
        exported = await client(ExportChatInviteRequest(group))
        link = getattr(exported, "link", None)
        if not link and hasattr(exported, "invite"):
            link = getattr(exported.invite, "link", "")
        return link or ""
    except Exception:
        return ""


def log_rows(rows: list[dict]):
    """Append rows to CSV log."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "phone", "user_id", "username", "status", "dm_status", "note"],
        )
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


async def send_dm_with_invite(
    client: TelegramClient,
    user: User,
    group_label: str,
    invite_link: str,
    template: str,
) -> str:
    """Try to DM the user with the invite link; return dm_status."""
    try:
        first = (user.first_name or "").strip() or "there"
        text = template.format(first=first, group=group_label, link=invite_link)
        await client.send_message(user, text)
        return "dm_sent"
    except ChatWriteForbiddenError:
        return "dm_forbidden"
    except UserPrivacyRestrictedError:
        return "dm_privacy_blocked"
    except PeerFloodError:
        return "dm_peer_flood_stop_and_wait"
    except FloodWaitError as e:
        return f"dm_rate_limited_wait_{getattr(e, 'seconds', 'unknown')}s"
    except Exception as e:
        return f"dm_error_{type(e).__name__}"


def sleep_progress(seconds: float):
    """Simple wrapper to allow clean Ctrl+C during sleeps."""
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add members from Excel to a Telegram group; DM invite on failure."
    )
    parser.add_argument("--group", default=TELEGRAM_GROUP, help="Group @username, t.me link, or ID")
    parser.add_argument("--excel-url", default=EXCEL_URL, help="Direct URL to Excel")
    parser.add_argument("--excel-path", default=EXCEL_PATH, help="Local path to Excel")
    parser.add_argument("--phone-col", default=PHONE_COLUMN, help="Excel column name for phone numbers")
    parser.add_argument("--region", default=DEFAULT_REGION, help="Default region code (e.g., US, IN)")
    parser.add_argument("--invite-link", default=INVITE_LINK, help="Invite link to DM on failures (optional; will try to export if missing)")
    # Scheduling options
    parser.add_argument("--daily", action="store_true", help="Run once a day (keeps process running)")
    parser.add_argument("--at", default="03:00", help="Daily run local time in HH:MM (24h) if --daily")
    return parser


async def run_once(args: argparse.Namespace):
    if not API_ID or not API_HASH:
        raise SystemExit("Set API_ID and API_HASH in .env")
    if not args.group:
        raise SystemExit("Set TELEGRAM_GROUP in .env or pass --group")

    df = download_excel_if_needed(args.excel_url, args.excel_path)
    phones = load_phones(df, args.phone_col, args.region)
    if not phones:
        raise SystemExit("No valid phone numbers found after normalization.")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # First run will ask for phone/SMS/2FA and create a .session file

    group = await resolve_group(client, args.group)

    # Validate access/rights for Channels (supergroups)
    if isinstance(group, Channel):
        try:
            await client(GetFullChannelRequest(group))
        except Exception as e:
            raise SystemExit(f"Cannot access the group (are you an admin?): {e}")

    # Try to get an invite link if not provided
    invite_link = await ensure_invite_link(client, group, args.invite_link)
    group_label = args.group
    results = []
    op_count = 0

    for phone in phones:
        dm_status = ""
        try:
            user = await import_contact_get_user(client, phone)
            if not user:
                results.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "phone": phone,
                    "user_id": "",
                    "username": "",
                    "status": "not_on_telegram_or_privacy_hidden",
                    "dm_status": "",
                    "note": "",
                })
                continue

            already = await is_member(client, group, user)
            if already:
                status = "already_member"
            else:
                status = await add_to_group(client, group, user)

            # If we couldn't add, try to DM the invite link
            if status not in ("added", "already_member"):
                sleep_progress(SLEEP_BETWEEN_DMS)
                op_count += 1
                dm_status = await send_dm_with_invite(
                    client=client,
                    user=user,
                    group_label=group_label,
                    invite_link=invite_link,
                    template=DM_TEMPLATE,
                )

            results.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "phone": phone,
                "user_id": getattr(user, "id", ""),
                "username": getattr(user, "username", ""),
                "status": status,
                "dm_status": dm_status,
                "note": "",
            })

        except Exception as e:
            results.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "phone": phone,
                "user_id": "",
                "username": "",
                "status": f"error_{type(e).__name__}",
                "dm_status": dm_status,
                "note": str(e),
            })

        # Pace add attempts to reduce rate limiting
        sleep_progress(SLEEP_BETWEEN_ADDS)
        op_count += 1

        # Batch sleep every N operations
        if BATCH_EVERY > 0 and op_count % BATCH_EVERY == 0:
            sleep_progress(BATCH_SLEEP)

    log_rows(results)
    added_count = sum(1 for r in results if r.get("status") == "added")
    print(f"Done. Added {added_count} member(s). Logged {len(results)} rows to {LOG_FILE}.")


if __name__ == "__main__":
    import asyncio
    import schedule
    import time as _time

    parser = build_arg_parser()
    args = parser.parse_args()

    if args.daily:
        # Schedule daily run at specified time. We wrap the async fn.
        def _job():
            asyncio.run(run_once(args))

        schedule.every().day.at(args.at).do(_job)
        print(f"Scheduled daily run at {args.at}. Press Ctrl+C to exit.")
        try:
            while True:
                schedule.run_pending()
                _time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        asyncio.run(run_once(args))
