# Full-depth tape and log archive

`recorder.py` is a separate process from `mintbot.py`. It does not import
mintbot, does not read `.env`, and does not place orders. Leave
`polyrecorder` stopped until the operator asks for it. The sell loop is
unchanged.

## Why `logs/books.jsonl` stopped (2026-10-05)

That file was lockbot's optional book tape, added in PR #239
(`buy/lock_book_log.py`, default `book_log_enabled` false, path
`logs/books.jsonl`, top 5 levels, 100ms throttle). On the VM the live
`lockbot.json` had `book_log_enabled` true. The file is 77MB. The first
row is 2026-10-05T12:05:17Z and the last row is 2026-10-05T14:12:43Z
(`btc-updown-5m-1791209400`). Lockbot was deleted the same day (PR #243,
commit 14:58 UTC) and `polylockbot.service` is gone. `mintbot.py` never
writes `logs/books.jsonl`. Do not put book logging back on the sell
loop. This recorder replaces it, off to the side.

## What the recorder writes

For the current BTC 15m window, the next one, and a window that ended
in the last 30s:

- Polymarket CLOB market websocket
  `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Both outcome tokens
- Every `book` snapshot (full depth), every `price_change`, and every
  `last_trade_price`
- `recv_ts` (local arrival) and `exch_ts` (exchange stamp, milliseconds
  scaled to seconds)

The BTC reference is a second socket to the same RTDS feed mintbot
parses (`buy.oracle_log.parse_rtds_message` / `parse_rtds_live` /
`SUBSCRIBE_FRAME`). Each update is written once, to
`data/tape/YYYY-MM-DD/oracle.jsonl.gz` for that UTC day. It is not
copied into every open bag. Replay loads that sibling next to the bag
file. `OracleLogService` is not started, so `logs/oracle_twap.jsonl`
is not touched.

Files:

```
data/tape/YYYY-MM-DD/<slug>.jsonl.gz
data/tape/YYYY-MM-DD/oracle.jsonl.gz
```

The date is the window start in UTC, so one bag stays one file. The
oracle file uses the receive time, so one day has one reference file.
Gzip members are appended and fsync'd about once a second (or at 32KB).
A crash loses at most that buffer. There is no in-memory history of the
book. When a bag's window is at least 30s past its end, or the file has
had no rows for 60s, the writer flushes, closes the handle, and drops
that bag's buffer. The next row for the same path appends a new gzip
member. That keeps the open-file count on the current and next windows
instead of one handle per bag for the life of the process.

Reconnects back off from 1s to 30s. The process stays up on socket
errors. `STOP_RECORDER` or SIGTERM exits 0. A second process exits 0
while `.recorder.lock` is held, so systemd does not crash-loop it.
`Restart=on-failure` with `RestartSec=30` and a start limit of 5 per
5 minutes covers a hard crash.

The unit is `Nice=10`, `IOSchedulingClass=idle`, `CPUQuota=25%`,
`MemoryMax=150M`. It has no `EnvironmentFile`.

## Replay

```bash
python tools/replay.py depth data/tape/YYYY-MM-DD/<slug>.jsonl.gz \
  --ts 1790000000.0 --leg up --side bid --price 0.92
python tools/replay.py fill data/tape/YYYY-MM-DD/<slug>.jsonl.gz \
  --ts 1790000000.0 --leg up --side sell --shares 100
python tools/example_up_bid.py data/tape/YYYY-MM-DD/<slug>.jsonl.gz \
  --ts 1790000000.0
```

`depth_at(ts, "bid", 0.92)` is shares bid at or above 0.92.
`size_at` is the single rung. `fill_estimate(ts, "sell", 100)` walks
bids from high to low and returns the share-weighted average. That is
the displayed book only: no queue and no latency. `--clock exch` uses
the exchange stamp instead of local arrival.

Library: `recorder.replay.Tape.load(path)`. A bag whose 15m window
reaches the next UTC midnight also loads that next day's
`oracle.jsonl.gz`. Other bags in the same directory do not.

## How far logs go (read-only VM check, 2026-10-15)

| Source | Oldest still on disk | Why the older part is gone |
|---|---|---|
| `mintbot.log` plus `logs/archive/mintbot.log.*.gz` | archive from 2026-09-26; `mintbot.log.3` mtime 2026-09-22 | Before the archive handler, rotation used `backupCount=3` and deleted older numbered files. Those deletes already happened. The current handler does not prune `logs/archive/`. |
| `logs/oracle_twap.jsonl` and its archives | archive from 2026-09-30 | Same archive-and-keep policy, 20MB rolls. |
| `pathlog/ticks` | 2026-09-08 through 2026-09-22 (4330 files) | Pathlog stopped 2026-09-22, so its 14-day prune stopped too. |
| `logs/books.jsonl` | 2026-10-05 12:05Z–14:12Z only | Lockbot deleted. See above. |
| `logs/shadow_early_scrap.jsonl` | still appended (mtime 2026-10-15) | Live file, not rotated away. |
| systemd journal `/var/log/journal` | oldest closed **system** file mtime 2026-10-12 09:32 UTC; directory 99M | Live `SystemMaxUse=100M` is full. `99-size.conf` also sets `MaxFileSec=3day`. Vacuum deleted everything older. `poly-auditor` cannot read the entries (not in `systemd-journal`). |
| syslog (`/var/log/syslog` … `syslog.4.gz`) | file mtimes back to 2026-09-20 | `logrotate` `rotate 4` weekly. Content was not readable as `poly-auditor`. |

There is no rclone remote, no `~/.config/gcloud`, and no S3 config on
the box. `gsutil` is installed and unused. The durable copy stays local
until `POLY_ARCHIVE_REMOTE` is set.

The "~Oct 7" cliff matches this vacuum, not a mintbot outage. Mintbot's
own files continue through 2026-10-15. A 7-day `MaxRetentionSec` (the
old text in `deploy/journald-size.conf`, not what the VM was running)
would also drop a week of journal. The 100M cap is tighter: on
2026-10-15 only files from about 2026-10-12 remained. Someone looking a
few days earlier would have seen the cliff nearer Oct 7.

## Archive job

`tools/archive_logs.py` (timer `polyarchive.timer`, 00:20 UTC) writes
`logs/durable/YYYY-MM-DD/` for yesterday:

- mintbot, oracle, shadow, price tape (`logs/price_tape.jsonl`, the
  other open PR), books, pathlog, and scrapbid lines whose `ts` /
  `recv_ts` fall on that day, gzipped
- `journalctl -u polymintbot` for that UTC day, if the user can read
  the journal
- `data/tape/<day>/` moved into `logs/durable/<day>/tape/` after the
  copy matches in size and sha256. A same-size file with different
  bytes stays in the spool and the manifest records `hash_mismatch`

It does not delete mintbot logs or `logs/archive/`.
`MANIFEST.json` records `deleted_logs: []`. A journal permission error
is written into the manifest and the job still finishes. Shedding tape
for the 5GB cap is the upload job below, not this one.

Point it at remote storage when rclone is already configured:

```bash
# one line, gitignored
echo 'gdrive:poly-logs' > deploy/archive_remote.path
# or, for the service environment:
# sudo systemctl edit polyarchive.service
# [Service]
# Environment=POLY_ARCHIVE_REMOTE=gdrive:poly-logs
```

The archive job still leaves the local durable directory in place.
`s3:bucket/prefix` works the same way if that remote exists in
rclone. There is no built-in Google Drive client here. Log extracts
under `logs/durable/<day>/` stay. Tape bytes in `logs/durable/<day>/tape/`
can be uploaded, or removed if free disk is under 4GB, as below.

Journal export needs the group:

```bash
sudo usermod -aG systemd-journal ntemusejoel
```

The next timer run picks the group up. Until then the manifest says
the export failed and the files are unchanged.

## Install (operator)

Do not start these from a cloud agent. After merge and the usual pull
on the VM:

```bash
cd /home/ntemusejoel/poly-money-maker
sudo cp deploy/polyrecorder.service /etc/systemd/system/
sudo cp deploy/polyarchive.service /etc/systemd/system/
sudo cp deploy/polyarchive.timer /etc/systemd/system/
sudo cp deploy/polyupload.service /etc/systemd/system/
sudo cp deploy/polyupload.timer /etc/systemd/system/
sudo rm -f /etc/systemd/journald.conf.d/99-size.conf
sudo cp deploy/journald-size.conf /etc/systemd/journald.conf.d/size.conf
sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable --now polyarchive.timer
sudo systemctl enable --now polyupload.timer
# recorder stays stopped until you want the tape:
# sudo systemctl enable --now polyrecorder.service
```

`polymintbot` does not need a restart for this change. Do not restart
it unless you were already going to. Confirm the recorder is idle:

```bash
systemctl status polyrecorder
ps -o pid,nice,rss,pcpu,cmd -C python
```

`CPUQuota=25%` is a quarter of one CPU. `MemoryMax=150M` is the cap.

## Disk

A 20-second local run on 2026-10-15, while the live 15m book was
active, used about **10% of one core** and **53MB RSS** (peak 53.5MB).
The unit cap is 25% of one core and 150MB. In that window the current
bag wrote 1.0MB gzip (~21k `price_change` rows, 144 full books, 69
trades, and the Chainlink feed from the first second). The next bag
was 15KB. At the hot bag's rate (~50KB/s gzip) a full day would be
several GB. The recorder checks at startup and about every minute.
Durable paths are always `<repo>/logs/durable`, including when
`--tape-dir` is not `data/tape`.

**Over 5GB.** `data/tape` plus `logs/durable/*/tape` over 5GB does not
delete by itself. `polyupload.timer` (every 15 minutes) runs
`tools/upload_tape.py` outside the recorder: Nice=19, idle IO,
`CPUQuota=10%`, `MemoryMax=100M`, and `rclone copy --bwlimit 2M`.
It uploads the oldest day to `gdrive:poly-tape/<YYYY-MM-DD>/` (or
`POLY_TAPE_REMOTE`), then `rclone check --one-way` (size and hash).
If the remote has no hashes, it compares MD5 and `rclone ls` sizes.
The local day is removed only after that match. A failed copy or check
keeps the files and the next timer run retries the same oldest day.
Today's directory is left alone, and so is any day touched in the last
two minutes. Mintbot logs in that durable directory are not part of
the upload or the delete.

**Under 4GB free.** The recorder may delete the oldest tape day
(durable tape first, then that day's spool) so mintbot's state writes
still fit. It logs `recorder_disk_emergency` at level error, including
that the upload was not verified. An open bag's day is left in place.
If free space is still under 4GB, the recorder stops appending.

**Age.** Durable tape is removed locally once the UTC day is 7 days
old or older. A day younger than 7 days is kept. That age check is
separate from the 5GB upload.

The boot disk had about 14GB free on 2026-10-15. RAM is the tight
resource (~1GB, ~540MB available).

## Google Drive auth (once, headless)

Do this on the VM as `ntemusejoel`. The token stays in rclone's config
(`~/.config/rclone/rclone.conf`), not in git.

```bash
rclone config
```

Choose `n` for a new remote, name it `gdrive`, storage `drive`.
Leave `client_id` and `client_secret` blank unless you have your own
Google client. Scope `1` (full drive). Leave the root folder and
service account blank. Skip advanced config. When asked to use auto
config, answer `n`. rclone then prints a command of the form:

```bash
rclone authorize "drive" "<blob>"
```

Run that exact command on a machine that has a browser and the same
rclone major version. Approve the drive access. Paste the token it
prints back into the VM's `config_token>` prompt. Quit config.

Point the upload job at that remote (the file is gitignored):

```bash
echo 'gdrive:poly-tape' > deploy/tape_remote.path
# or, instead of the file:
# sudo systemctl edit polyupload.service
# [Service]
# Environment=POLY_TAPE_REMOTE=gdrive:poly-tape
```

`gdrive:poly-logs` (the log archive) and `gdrive:poly-tape` are
different prefixes. The first copy creates `poly-tape`. Confirm with
`rclone lsd gdrive:` before enabling the timer if the remote is new.

Pathlog's tick prune (14 days / 400MB) is unchanged. Pathlog stays
stopped, so it is not deleting anything now. `pathlog.log` rotates at
2MB with `backupCount=3`. `scrapbid.log` rotates at 1MB with
`backupCount=2`. Mintbot's log still rolls into `logs/archive/` and
that archive is not pruned.
