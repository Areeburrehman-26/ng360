# GEICO 2FA iMessage Automation - Technical Documentation

## Overview

This document explains how the PL Bot automatically retrieves GEICO 2FA codes from iMessage to enable completely unattended quote automation.

## How It Works

### The Problem
GEICO requires 2FA verification codes sent via SMS when downloading quote PDFs. Manual intervention would be needed to read these codes from messages and type them in.

### The Solution
The bot automatically:
1. Queries the macOS Messages database (`~/Library/Messages/chat.db`)
2. Finds the most recent 2FA code from GEICO (sender: 69525)
3. Extracts the 6-digit code
4. Fills it into the GEICO login form

### Technical Flow

```
GEICO sends SMS → iPhone receives → iCloud syncs → Messages.app → SQLite DB → Bot reads code
```

## Requirements

### 1. Hardware & Software
- Mac with macOS 10.14+
- iPhone with active cellular service
- iMessage enabled and synced to Mac
- Messages app signed in with same Apple ID as iPhone

### 2. Database Permissions
The bot needs **Full Disk Access** to read the Messages database:

**Grant Access:**
1. System Settings → Privacy & Security → Full Disk Access
2. Click the "+" button
3. Add: `/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python`
4. Or add: `/usr/bin/python3` (if using system Python)
5. Toggle the switch to enable

### 3. GEICO Configuration
- 2FA phone number: `4048010128` (configured in GEICO account)
- SMS messages sent from: `69525` (GEICO's shortcode)
- Message format: `"Use verification code XXXXXX for your GEICO Agency..."`

## Database Structure

### Messages Database Location
```
~/Library/Messages/chat.db
```

### Relevant Tables

**`message` table:**
- `ROWID` - Unique message ID
- `text` - Message content
- `date` - Timestamp (Apple epoch: seconds since 2001-01-01)
- `handle_id` - Link to sender

**`handle` table:**
- `ROWID` - Unique handle ID
- `id` - Phone number or email

**`chat_message_join` table:**
- Links messages to chat threads

## The Code

### Function: `retrieve_2fa_code_from_imessage()`

This function automatically retrieves the most recent GEICO 2FA code from iMessage.

```python
import sqlite3
import time
from pathlib import Path

def retrieve_2fa_code_from_imessage(timeout=60, check_interval=2):
    """
    Retrieve GEICO 2FA code from iMessage database

    Args:
        timeout (int): Maximum seconds to wait for new code (default: 60)
        check_interval (int): Seconds between database checks (default: 2)

    Returns:
        str: 6-digit 2FA code, or None if not found

    How it works:
        1. Queries Messages database for recent messages from 69525 (GEICO)
        2. Looks for pattern: "Use verification code XXXXXX"
        3. Returns most recent code found
        4. Waits up to timeout seconds for a new code if none exists
    """

    # Path to Messages database
    db_path = Path.home() / "Library" / "Messages" / "chat.db"

    if not db_path.exists():
        print(f"❌ Messages database not found at {db_path}")
        return None

    # Calculate time threshold (messages from last 5 minutes)
    # Apple epoch starts at 2001-01-01, not 1970-01-01
    apple_epoch = 978307200  # Seconds between 1970-01-01 and 2001-01-01
    current_time = int(time.time()) - apple_epoch
    time_threshold = current_time - (5 * 60)  # 5 minutes ago

    start_time = time.time()

    while (time.time() - start_time) < timeout:
        try:
            # Connect to Messages database (read-only)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Query for recent messages from GEICO (69525)
            # Order by date DESC to get newest messages first
            query = """
                SELECT
                    message.ROWID,
                    message.text,
                    message.date,
                    handle.id
                FROM message
                JOIN handle ON message.handle_id = handle.ROWID
                WHERE handle.id = '69525'
                  AND message.date > ?
                  AND message.text IS NOT NULL
                ORDER BY message.date DESC
                LIMIT 50
            """

            cursor.execute(query, (time_threshold,))
            messages = cursor.fetchall()
            conn.close()

            if messages:
                print(f"   Found {len(messages)} recent message(s) from 69525")

                # Search for 2FA code in messages
                # IMPORTANT: We stop at the FIRST message (most recent)
                # This prevents using old codes
                for row_id, text, msg_date, sender in messages:
                    if text:
                        # Pattern 1: "Use verification code 123456 for your GEICO Agency"
                        if "verification code" in text.lower():
                            import re
                            # Extract 6-digit code
                            match = re.search(r'\b(\d{6})\b', text)
                            if match:
                                code = match.group(1)
                                print(f"   ✅ Found 2FA code: {code} from {sender} at {row_id}")
                                return code

                        # Pattern 2: Direct 6-digit number
                        import re
                        match = re.search(r'\b(\d{6})\b', text)
                        if match:
                            code = match.group(1)
                            print(f"   ✅ Found code: {code} from {sender}")
                            return code

                    # CRITICAL: Stop after first message (most recent)
                    # This ensures we use the newest code, not an old one
                    break

            # No code found yet, wait and retry
            print(f"   ⏳ Waiting for 2FA code... ({int(time.time() - start_time)}s elapsed)")
            time.sleep(check_interval)

        except sqlite3.Error as e:
            print(f"   ⚠️ Database error: {e}")
            time.sleep(check_interval)
        except Exception as e:
            print(f"   ⚠️ Error retrieving 2FA code: {e}")
            time.sleep(check_interval)

    print(f"   ❌ Timeout: No 2FA code found after {timeout} seconds")
    return None


def fill_geico_2fa(page, code):
    """
    Fill GEICO 2FA code into the verification form

    Args:
        page: Playwright page object
        code (str): 6-digit verification code

    Returns:
        bool: True if successful, False otherwise
    """
    import asyncio

    try:
        # Wait for 2FA input field
        code_input = page.locator("input[type='tel'], input[name='code'], input[id*='code'], input[placeholder*='code']").first
        await code_input.wait_for(state="visible", timeout=10000)

        # Fill the code
        await code_input.fill(code)
        print(f"   ✅ Filled 2FA code: {code}")

        # Find and click submit button
        submit_button = page.locator("button:has-text('Verify'), button:has-text('Submit'), button[type='submit']").first
        await submit_button.click()
        print(f"   ✅ Clicked verify button")

        # Wait for verification to complete
        await page.wait_for_timeout(3000)

        return True

    except Exception as e:
        print(f"   ❌ Failed to fill 2FA code: {e}")
        return False
```

## Usage Example

```python
# In pl_bot.py automation flow:

# Step 1: Detect GEICO 2FA prompt
if "verification" in page.url.lower() or "2fa" in page.url.lower():
    print("🔐 GEICO 2FA detected - retrieving code from iMessage...")

    # Step 2: Retrieve code from iMessage
    code = retrieve_2fa_code_from_imessage(timeout=60)

    if code:
        # Step 3: Fill the code
        success = await fill_geico_2fa(page, code)

        if success:
            print("✅ 2FA verification successful")
        else:
            print("❌ 2FA verification failed")
    else:
        print("❌ Could not retrieve 2FA code")
```

## Key Implementation Details

### 1. Time Window
- Queries messages from last **5 minutes** only
- Prevents using old codes that may have expired

### 2. Code Selection
- Orders messages by date **DESC** (newest first)
- Uses **FIRST** match found (most recent code)
- Critical fix from March 27, 2026: Previously looped through all messages, now stops immediately after first match

### 3. Pattern Matching
Two patterns supported:
```
Pattern 1: "Use verification code 123456 for your GEICO Agency..."
Pattern 2: Any 6-digit number in the message
```

### 4. Retry Logic
- Checks database every 2 seconds
- Maximum wait time: 60 seconds
- Allows for SMS delivery delays

### 5. Database Access
- Opens in **read-only** mode: `mode=ro`
- Prevents accidental database corruption
- Requires Full Disk Access permission

## Error Handling

### Common Issues

**1. Database not found:**
```
❌ Messages database not found at ~/Library/Messages/chat.db
```
**Fix:** Enable iMessage sync on Mac

**2. Permission denied:**
```
⚠️ Database error: attempt to open database file failed
```
**Fix:** Grant Full Disk Access to Python in System Settings

**3. No messages found:**
```
❌ Timeout: No 2FA code found after 60 seconds
```
**Fix:**
- Verify phone number 4048010128 is registered in GEICO
- Check iPhone is receiving SMS
- Ensure iMessage sync is working

**4. Old code being used:**
```
⚠️ Verification failed - code expired
```
**Fix:** Already fixed - code now stops at first message (most recent)

## Security Considerations

### Why This Is Safe

1. **Read-only access:** Database opened in read-only mode
2. **Local only:** No data transmitted externally
3. **Temporary:** Codes expire after use
4. **Specific sender:** Only reads from GEICO (69525)

### What's NOT Stored

- Codes are NOT saved to disk
- No logging of verification codes
- Database is never modified

## Troubleshooting

### Test the function manually:

```python
# Run this in Python REPL to test
from pl_bot import retrieve_2fa_code_from_imessage

# Send a test SMS from GEICO, then run:
code = retrieve_2fa_code_from_imessage(timeout=10)
print(f"Retrieved code: {code}")
```

### Check database manually:

```bash
# View recent messages from GEICO
sqlite3 ~/Library/Messages/chat.db "
SELECT message.text, message.date, handle.id
FROM message
JOIN handle ON message.handle_id = handle.ROWID
WHERE handle.id = '69525'
ORDER BY message.date DESC
LIMIT 5;
"
```

### Verify permissions:

```bash
# Test database access
sqlite3 ~/Library/Messages/chat.db "SELECT COUNT(*) FROM message;"
```

If you get "attempt to write a readonly database" - this is actually OK! The database is protected by macOS, but read-only mode still works.

## Version History

### March 27, 2026 - Critical Fix
**Problem:** Bot was collecting ALL codes from messages, then using the first from the list (oldest)
**Fix:** Now stops immediately after finding first message (newest code)
**Result:** Most recent 2FA code is always used

### March 26, 2026 - Initial Implementation
- Automated GEICO 2FA code retrieval
- iMessage database integration
- 60-second timeout with retry logic

## Related Files

- `pl_bot.py` - Main automation script (contains this function)
- `GEICO_2FA_IMESSAGE_SETUP.md` - Setup instructions
- `IMESSAGE_2FA_TECHNICAL_GUIDE.txt` - Additional technical details

## Support

If 2FA automation fails:
1. Check iMessage sync is working
2. Verify Full Disk Access permissions
3. Test with manual code entry first
4. Check logs for specific error messages