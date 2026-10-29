# mdrecorder

Standalone BTC market-data recorder. It does not import anything from the
rest of this repository.

Records go to hourly gzip files:

```
OUT/<stream>/<YYYY-MM-DD>/<stream>-<YYYYMMDDTH>.jsonl.gz
```

Each line is one JSON record:

```json
{"src":"coinbase","ts_recv_wall":1793260400000000000,"ts_recv_mono":123456789,
 "ts_exch":1793260399123,"raw":{...}}
```

`ts_recv_wall` is `time.time_ns()` and `ts_recv_mono` is `time.monotonic_ns()`
taken when the message is read from the socket. `ts_exch` is the exchange
timestamp in milliseconds, or null. `raw` holds the parsed message; for the
high-rate channels it can be the decoded payload, and records that need no
timestamp keep the original text under `raw_s`.

## Streams

| stream | source |
| --- | --- |
| `binance_spot` | Binance spot `btcusdt@aggTrade` + `btcusdt@bookTicker` |
| `binance_fut` | Binance USD-M futures `btcusdt@aggTrade` + `btcusdt@bookTicker` |
| `coinbase` | Coinbase `ticker` + `matches` for BTC-USD |
| `bybit_perp` | Bybit linear `publicTrade.BTCUSDT` + `orderbook.1.BTCUSDT` + `tickers.BTCUSDT` |
| `bybit_spot` | Bybit spot `publicTrade.BTCUSDT` + `orderbook.1.BTCUSDT` |
| `okx_swap` | OKX `trades` + `bbo-tbt` for BTC-USDT-SWAP |
| `okx_spot` | OKX `trades` + `bbo-tbt` for BTC-USDT |
| `kraken` | Kraken v2 `trade` + `ticker` for BTC/USD |
| `deribit` | Deribit `trades.BTC-PERPETUAL.raw` + `ticker.BTC-PERPETUAL.100ms` |
| `bitstamp` | Bitstamp `live_trades_btcusd` |
| `chainlink` | Polymarket RTDS BTC/USD 60s TWAP and Chainlink prices |
| `poly_book` | Polymarket 5m CLOB book / price_change / tick_size_change |
| `poly_trades` | Polymarket `last_trade_price` events and Data API trades |
| `poly_markets` | Gamma market metadata found during discovery |
| `_health` | gaps, trims, and 30s heartbeats with per-stream counts |

Every websocket task reconnects with exponential backoff from 1s to 30s,
uses protocol pings, and writes a `_gap` record to `_health` on reconnect:

```json
{"src":"_gap","stream":"bybit_perp","down_ns":1234,"reason":"..."}
```

Each exchange source writes to its own directory, so a source can be
recorded or archived independently:

```
OUT/bybit_perp/...   OUT/bybit_spot/...   OUT/okx_swap/...   OUT/okx_spot/...
```

Bybit streams subscribe to `publicTrade` and `orderbook.1`; `bybit_perp`
also keeps `tickers`. Both send `{"op":"ping"}` every 20s. OKX streams
subscribe to `trades` and `bbo-tbt` and send the text `ping` every 20s,
ignore `pong`, and answer a server `ping` with `pong`.

Polymarket discovery runs every 30s against
`https://gamma-api.polymarket.com/events?slug=btc-updown-5m-<ts>` for the
current 5-minute boundary and the next two. When the token set changes, the
market websocket reconnects and resubscribes. Each active market is polled
once per second at
`https://data-api.polymarket.com/trades?market=<conditionId>&limit=100`;
new trades (deduped on transaction hash, asset, price, and size) are written
with `"src":"data_api"`.

## Usage

```sh
python -m mdrecorder.run --out /var/lib/mdrecorder
python -m mdrecorder.status --out /var/lib/mdrecorder
python -m mdrecorder.rtt
```

## Latency probe

`python -m mdrecorder.rtt` opens 5 fresh connections to every websocket
URL the recorder uses, measures TCP connect and TLS handshake time
separately, and prints the median of each:

```
stream             host                          tcp_ms    tls_ms  samples  note
bybit_spot         stream.bybit.com:443             1.1      27.7        5
okx_swap,okx_spot  ws.okx.com:443                   1.0      21.9        5
```

`--streams bybit_perp,okx_swap` limits the probe to those sources and
`--samples N` changes the sample count. Failed samples are counted in the
`note` column; endpoints that never connect show `-` for the timings. The
URL list comes from `mdrecorder.streams.ENDPOINTS`, so it stays in sync
with the recorder.

Dependencies: `pip install -r mdrecorder/requirements.txt` (`websockets`,
`aiohttp`). Python 3.12+.

`MDREC_MAX_GB` (default `12`) caps the output tree. The writer checks the
total size every minute and deletes the oldest day directories until the
tree is back under the cap. The current UTC day is never deleted.

`status.json` in the output directory is rewritten atomically every 30s. The
`status` command prints it as a table together with current disk usage.

## Deployment

`deploy/mdrecorder.service` runs the recorder as `ubuntu` from
`/home/ubuntu/mdrecorder` with `/home/ubuntu/mdrecorder/.venv`. It is at
idle CPU and IO priority with a 40% CPU quota and a 400 MB memory cap.
