# BMS Avengers Doomsday Tracker

Polls BookMyShow every 10 minutes for new Bengaluru theatres going live
for **Avengers: Doomsday on December 17, 2026** and fires an email alert.

---

## Setup (5 minutes)

### 1. Prerequisites
- Python 3.8+
- A Gmail account with **App Password** enabled
  - Gmail → Settings → Security → 2-Step Verification → App Passwords
  - Create one for "BMS Tracker" — you get a 16-char password

### 2. Install
```bash
cd bms_tracker
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure email
Edit `tracker.py` lines 35–40, OR set environment variables:

```bash
export BMS_EMAIL_SENDER="your_gmail@gmail.com"
export BMS_EMAIL_PASSWORD="abcd efgh ijkl mnop"   # 16-char App Password
export BMS_EMAIL_TO="your_email@gmail.com"         # where alerts go
```

### 4. Run
```bash
python tracker.py
# or
chmod +x run.sh && ./run.sh
```

---

## How It Works

```
Every 10 minutes:
  1. Fetches in.bookmyshow.com/bengaluru/cinemas
     → extracts full venue list from window.__INITIAL_STATE__

  2. For each venue, checks:
     in.bookmyshow.com/cinemas/bengaluru/x/buytickets/{VENUE_CODE}/20261217
     → looks for Avengers/Doomsday shows in the JSON state

  3. Compares against known_theatres.json (persisted state)

  4. If new theatres found → sends styled HTML email with:
     - Theatre name + address
     - Direct "Book Now" link to BMS
```

---

## Tuning

| Variable | Default | What it does |
|---|---|---|
| `POLL_INTERVAL` | 600s | Check every 10 min. Set to 300 for 5 min |
| `MOVIE_KEYWORD` | `avengers-doomsday` | Keyword to match in showtimes |
| `TARGET_DATE` | `20261217` | Date to track (YYYYMMDD) |

---

## Running 24/7

**Mac/Linux — keep terminal open:**
```bash
nohup python tracker.py > tracker_out.log 2>&1 &
echo $! > tracker.pid    # save PID to kill later
```

**Kill it later:**
```bash
kill $(cat tracker.pid)
```

**Windows — run in background:**
```powershell
Start-Process python -ArgumentList "tracker.py" -WindowStyle Hidden
```

**Cron (Linux/Mac) — if you want OS-level scheduling instead:**
```bash
# Add to crontab -e (runs every 10 min, but tracker.py already loops):
*/10 * * * * cd /path/to/bms_tracker && source venv/bin/activate && python tracker.py --once
```
Add a `--once` flag to `main()` if you want cron-mode instead of the internal loop.

---

## Files
```
bms_tracker/
├── tracker.py           ← main script
├── requirements.txt
├── run.sh               ← launcher
├── known_theatres.json  ← auto-created, persists state between runs
└── tracker.log          ← auto-created, full log
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `0 venues found` | BMS may have changed their HTML structure — check `tracker.log` for the raw response; the fallback HTML parser will kick in |
| Email not sent | Check App Password; Gmail blocks regular passwords for SMTP |
| Getting 403 from BMS | Add a longer delay: change `time.sleep(1.5)` to `time.sleep(3)` |
| State file corrupted | Delete `known_theatres.json` — it'll rebuild |
