# KDC Groupchat Automation

Automate adding members to a Telegram group/channel from an Excel list, skip existing members, and DM users who can’t be added with an invite link. Supports private groups (t.me invite links), groups without usernames, and daily scheduled runs.

## Objectives
- Read phone numbers from a public Excel URL or local file.
- Normalize and deduplicate numbers; validate with region defaults.
- Add users to a Telegram group/channel, skipping existing members.
- If adding fails (privacy/admin), DM each user with an invite link.
- Works with private groups: resolves `t.me/+...` or `t.me/joinchat/...` links (auto-join) and numeric IDs (`-100...`).
- Optional daily scheduling, logging to `add_members_log.csv`.

## How it works (high level)
1. Load config from `.env` and CLI flags.
2. Load Excel via `requests + pandas` (URL) or local path.
3. Normalize numbers with `phonenumbers` to E.164.
4. Authenticate with Telethon; resolve or join the target group from:
   - `@username`, `t.me/username`, `t.me/+HASH`, `t.me/joinchat/HASH`, or numeric ID.
5. For each phone:
   - Import as contact to resolve the Telegram user.
   - If not a member, attempt to add.
   - On failure, DM invite link (auto-exported if you’re admin or provided).
6. Log every outcome to `add_members_log.csv`.
7. Print how many were added and the total rows logged.

## Key libraries
- **telethon**: Telegram API client (auth, resolve/join group, add members, DM).
- **pandas, openpyxl**: Read Excel.
- **python-dotenv**: Load `.env`.
- **phonenumbers**: Validate/normalize phone numbers.
- **requests**: Download Excel from a URL.
- **schedule**: Simple daily scheduler.

## File structure
- `add_members_from_excel.py`: main script.
- `.gitignore`: ignores secrets (`.env`), session files, logs, and `venv/`.
- `requirements.txt`: Python dependencies.
- `add_members_log.csv`: run log (ignored by git).

## Configuration (.env)
Create `.env` at the project root:

```
API_ID=123456
API_HASH=your_api_hash
SESSION_NAME=telegram_adder_session

# Group identifier: @username, https://t.me/username, https://t.me/+HASH, https://t.me/joinchat/HASH, or -100xxxxxxxxxx
TELEGRAM_GROUP=https://t.me/+YourInviteHash

# Optional: used when adding fails (DM invite). If you’re not admin, set this.
INVITE_LINK=https://t.me/+YourInviteHash

# Excel source (use one):
EXCEL_URL=https://example.com/members.xlsx
# or
EXCEL_PATH=/absolute/path/to/members.xlsx

PHONE_COLUMN=phone
DEFAULT_REGION=US

# Optional: DM text
DM_TEMPLATE=Hi {first}, I tried to add you to {group} but Telegram privacy or permissions blocked it. You can join directly using this link: {link}

# Rate limiting
SLEEP_BETWEEN_ADDS=2
SLEEP_BETWEEN_DMS=2
BATCH_EVERY=25
BATCH_SLEEP=30
```

Notes:
- `PHONE_COLUMN` is case-insensitive but must match spaces: e.g., `"Emergency Contact Phone Number"`.
- `DEFAULT_REGION` is a 2-letter ISO country code (e.g., US, IN, GB).
- If your SharePoint/OneDrive link requires login, use a public “Anyone with the link can view” URL plus `download=1`, or download the file and use `EXCEL_PATH`.

## First run (creates session)
```
source venv/bin/activate
python add_members_from_excel.py --group "https://t.me/+YourInviteHash" --excel-url "https://.../members.xlsx" --phone-col "Emergency Contact Phone Number" --region US
```
- Enter your phone, code, and 2FA if prompted. A `.session` file is created.

## Examples
- Local file:
```
python add_members_from_excel.py --group -1001234567890 --excel-path "/Users/me/Downloads/members.xlsx" --phone-col "Emergency Contact Phone Number" --region US
```
- URL with explicit invite link:
```
python add_members_from_excel.py --group "https://t.me/+YourInviteHash" --invite-link "https://t.me/+YourInviteHash" --excel-url "https://.../members.xlsx" --phone-col "Emergency Contact Phone Number" --region US
```
- Daily at 03:00:
```
python add_members_from_excel.py --group "https://t.me/+YourInviteHash" --excel-url "https://.../members.xlsx" --phone-col "Emergency Contact Phone Number" --region US --daily --at 03:00
```

## Permissions and private groups
- You must be in the group with the same account used by the script.
- To add members directly, you typically need admin rights with “Add Members”.
- If you aren’t admin or adding is blocked by user privacy, the script DMs the invite link.
- Private groups without usernames are supported via t.me invite links or numeric IDs.

## Logs and output
- `add_members_log.csv` with columns:
  - `timestamp, phone, user_id, username, status, dm_status, note`
- Terminal output ends with:
  - `Done. Added X member(s). Logged Y rows to add_members_log.csv.`

Common statuses:
- `added`: user added
- `already_member`: skipped
- `not_on_telegram_or_privacy_hidden`: no resolvable user
- `blocked_by_privacy`, `not_admin_or_no_add_permission`: add failed; user likely DMed
- `dm_sent`, `dm_forbidden`, `dm_privacy_blocked`
- `peer_flood_stop_and_wait`, `rate_limited_wait_XXs`: slow down or increase sleeps

## Rate limiting tips
- Increase pauses if you see Flood/peer_flood:
  - `SLEEP_BETWEEN_ADDS`: 3–6
  - `BATCH_EVERY`: 10–15
  - `BATCH_SLEEP`: 60–180

## Security
- `.env`, Telethon `.session` files, logs are ignored by git via `.gitignore`.
- Never commit API secrets.

## Troubleshooting
- 403 on Excel URL: make it publicly downloadable or use `--excel-path`.
- “Cannot find entity”: join the group first or use the correct link/ID; the script auto-handles `t.me/+...` joins.
- No DM: user blocked unknown DMs or bot-like activity; status will show `dm_forbidden` or `dm_privacy_blocked`.

## License
Private/internal use. Add a license if you plan to distribute.
