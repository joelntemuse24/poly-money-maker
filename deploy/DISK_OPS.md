# GCP disk ops (polybuybot VMs)

## Why the disk filled (2026-07 incident)

| What we thought | What actually happened |
|---|---|
| App data files | App data was only **~5MB** |
| — | **`/var/log` ≈ 5GB** on a **9.7GB** root disk |

Small e2 boot disks fill from **host logs** (journal, syslog, ops agent) long before
bot data matters.

## Prevention (do all of these on the VM)

1. **Cap journal permanently**
   ```bash
   sudo mkdir -p /etc/systemd/journald.conf.d
   sudo cp deploy/journald-size.conf /etc/systemd/journald.conf.d/size.conf
   sudo systemctl restart systemd-journald
   sudo journalctl --vacuum-size=50M
   ```

2. **Monitor free space**
   ```bash
   df -h /
   # alert if Avail < 1G
   ```

3. **Prefer a larger boot disk** (20–30GB) if running the bots long-term.

4. **Pathlog ticks are capped in-app** (`pathlog.py`: 14 days / **400 MB**, oldest
   JSONL first). That is sized for this ~10GB VM (~15 MB/day of ticks ≈ 210 MB
   in 14 days). Journal cap ≠ pathlog cap. **Export before prune:**

   ```bash
   cd ~/poly-money-maker
   .venv/bin/python check_path_backtest.py --grid --budget 2.5 --series 5m --csv /tmp/hits.csv
   .venv/bin/python check_path_backtest.py --export-market <slug> --csv /tmp/m.csv
   # scp /tmp/hits.csv off the VM, or: scp -r pathlog/ticks ./pathlog-export-$(date -u +%Y%m%d)
   ```

   Do not `rm` `pathlog/ticks` by hand. Look for `pathlog_prune` in `pathlog.log`.

## Recovery if 100% full

```bash
sudo systemctl stop polybuybot polybuybot5m polybuybothourly
sudo du -xh / --max-depth=1 2>/dev/null | sort -h
sudo du -xh /var/log --max-depth=2 2>/dev/null | sort -h | tail -20
sudo journalctl --vacuum-size=20M
sudo find /var/log -type f \( -name '*.gz' -o -name '*.1' -o -name '*.old' \) -delete
sudo find /var/log -type f -size +50M -exec truncate -s 0 {} \;
sudo apt-get clean
df -h /
# Export pathlog ticks BEFORE any manual delete — prune already caps them at 400 MB
# scp -r ~/poly-money-maker/pathlog/ticks ./pathlog-export-$(date -u +%Y%m%d)
cd ~/poly-money-maker && git pull
sudo systemctl start polybuybot polybuybot5m polybuybothourly polypathlog
```
