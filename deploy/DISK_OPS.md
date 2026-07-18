# GCP disk ops (polybot + polyshadow)

## Why the disk filled (2026-07 incident)

| What we thought | What actually happened |
|---|---|
| RAM / CPU pressure from shadow | **Disk** full (`ENOSPC`) |
| `sim_data/` tick files | `sim_data` was only **~5MB** |
| — | **`/var/log` ≈ 5GB** on a **9.7GB** root disk |

Small e2 boot disks fill from **host logs** (journal, syslog, ops agent) long before paper-trade data matters. Shadow also used to write dense tick files; that is now off by default, but it was **not** the 5GB culprit this time.

## Prevention (do all of these on the VM)

1. **Cap journal permanently**
   ```bash
   sudo mkdir -p /etc/systemd/journald.conf.d
   sudo cp deploy/journald-size.conf /etc/systemd/journald.conf.d/size.conf
   sudo systemctl restart systemd-journald
   sudo journalctl --vacuum-size=50M
   ```

2. **Keep shadow disk-light** (already in `sim/strategy.sim.json`)
   - `record_ticks: false`
   - prune ticks 6h, cap `sim_data` ~150MB, require ~200MB free
   - ENOSPC disables optional writes; unresolved markets do not poison PnL

3. **Monitor free space**
   ```bash
   df -h /
   # alert if Avail < 1G
   ```

4. **Prefer a larger boot disk** (20–30GB) if running bot + shadow + journal long-term.

## Recovery if 100% full

```bash
sudo systemctl stop polyshadow
sudo du -xh / --max-depth=1 2>/dev/null | sort -h
sudo du -xh /var/log --max-depth=2 2>/dev/null | sort -h | tail -20
sudo journalctl --vacuum-size=20M
sudo find /var/log -type f \( -name '*.gz' -o -name '*.1' -o -name '*.old' \) -delete
sudo find /var/log -type f -size +50M -exec truncate -s 0 {} \;
sudo apt-get clean
df -h /
cd ~/poly-money-maker && git pull
sudo systemctl start polyshadow
```
