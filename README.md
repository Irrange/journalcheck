# journalcheck

Track review status from 3 journal systems:

- `aha`: AHA / eJournalPress
- `em`: Editorial Manager
- `bmc`: Springer Nature / BMC submission details

## GUI

Start the desktop window in one of these ways:

```powershell
python main_gui.py
```

Or double-click:

- `dist/JournalCheck.exe`

Main GUI features:

- refresh now
- auto refresh on a timer
- change summary at the top
- active submissions list with full manuscript summary
- click one submission to show detail below
- account management for AHA / EM / BMC
- notification settings and test email
- explicit HTTP / HTTPS proxy settings for refresh requests

## CLI

The CLI is still available:

```powershell
python main.py
python main.py --once
python main.py --interval-minutes 30
python main.py --site aha --once
python main.py --test-email
```

## Multi-config format

Copy `.env.example` to `.env` and put real journal accounts, email settings,
webhook URLs, and proxy values only in `.env`. The `.env` file is local-only and
must not be committed.

All 3 site families support numbered configs:

- `AHA_1_*`, `AHA_2_*`, `AHA_3_*`
- `EM_1_*`, `EM_2_*`, `EM_3_*`
- `BMC_1_*`, `BMC_2_*`, `BMC_3_*`

Example:

```env
AHA_1_BASE_URL=https://example-1
AHA_1_USERNAME=...
AHA_1_PASSWORD=...

EM_1_BASE_URL=https://example-2
EM_1_JOURNAL_CODE=jaaa
EM_1_USERNAME=...
EM_1_PASSWORD=...
```

Legacy single-config names still work:

- `AHA_BASE_URL`, `AHA_USERNAME`, `AHA_PASSWORD`
- `EM_BASE_URL`, `EM_JOURNAL_CODE`, `EM_USERNAME`, `EM_PASSWORD`
- `BMC_SUBMISSION_URL`, `BMC_USERNAME`, `BMC_PASSWORD`

If any numbered config exists for a site family, the program uses the numbered configs for that family.

## Output

The program updates the same files instead of creating a new file every time:

- `outputs/statuses.json`: current active submissions + incremental history
- `outputs/statuses.csv`: current active snapshot
- `outputs/refresh_log.csv`: append-only refresh history; every refresh writes a summary row and the current snapshot rows

## Proxy

If a journal site or Gmail SMTP is only stable through a proxy, set these values in `.env` or in the GUI:

```env
HTTP_PROXY=http://proxy-host:proxy-port
HTTPS_PROXY=http://proxy-host:proxy-port
NO_PROXY=example.com,example.org
```

Notes:

- `HTTP_PROXY` / `HTTPS_PROXY` should be full proxy URLs
- `NO_PROXY` is optional and bypasses the proxy for selected domains
- SMTP email uses the same proxy values through an HTTP CONNECT tunnel

## Build EXE

Build the Windows launcher executable:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Output:

- `dist/JournalCheck.exe`

Note:

- this launcher exe uses your local Python installation or `.venv`
- it is a native Windows launcher, not a fully self-contained frozen bundle

## Notification on change

If status changes are detected compared with the previous refresh, the program can notify you.

Supported channels:

- Email via SMTP
- WeCom group robot webhook (`WECOM_WEBHOOK_URL`)

## Notes

- AHA only checks `Live Manuscripts`
- EM only keeps active submissions
- BMC also records invited / accepted / returned reviewer counts
- AHA past review comments URL is captured and the program tries to extract readable comment blocks when available
