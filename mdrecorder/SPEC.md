# mdrecorder spec (standalone market-data recorder)
Standalone package in mdrecorder/ ONLY. Never import anything from the repo (no buy/, mintbot, recorder/). Python 3.12, deps: websockets, aiohttp (put in mdrecorder/requirements.txt).
Entry: `python -m mdrecorder.run --out DIR` and `python -m mdrecorder.status --out DIR`. Latency probe: `python -m mdrecorder.rtt` measures TCP connect and TLS handshake time (median of 5 samples) for every websocket URL used and prints a table.

Every record = one JSON line: {"src":..., "ts_recv_wall": time.time_ns(), "ts_recv_mono": time.monotonic_ns(), "ts_exch": <exchange ts ms or null>, "raw": <original message parsed json>}.
Writer: asyncio queue -> single writer task; per-stream files OUT/<stream>/<YYYY-MM-DD>/<stream>-<YYYYMMDDTHH>.jsonl.gz (hourly gzip rotation, compresslevel 6, flush every 5s). Disk cap: if OUT total > MDREC_MAX_GB (default 12) delete oldest day dirs.
Streams (one asyncio task each, auto-reconnect with exponential backoff 1..30s, ping, log gaps: on reconnect write a {"src":"_gap","stream":..,"down_ns":..,"reason":..} record to stream "_health"):
- binance_spot: wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker
- binance_fut: wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker
- coinbase: wss://ws-feed.exchange.coinbase.com subscribe ticker+matches BTC-USD
- bybit_perp: wss://stream.bybit.com/v5/public/linear subscribe publicTrade.BTCUSDT, orderbook.1.BTCUSDT, tickers.BTCUSDT ; send {"op":"ping"} every 20s
- bybit_spot: wss://stream.bybit.com/v5/public/spot subscribe publicTrade.BTCUSDT, orderbook.1.BTCUSDT ; send {"op":"ping"} every 20s
- okx_swap: wss://ws.okx.com:8443/ws/v5/public subscribe trades + bbo-tbt BTC-USDT-SWAP; send text "ping" every 20s, ignore "pong", answer server "ping" with "pong"; ts_exch from data[0].ts
- okx_spot: same for BTC-USDT
- (every okx/bybit source is its own stream and its own output dir; okx falls back to wss://ws.okx.com/ws/v5/public when 8443 is unreachable)
- kraken: wss://ws.kraken.com/v2 subscribe trade + ticker BTC/USD
- deribit: wss://www.deribit.com/ws/api/v2 public/subscribe trades.BTC-PERPETUAL.raw, ticker.BTC-PERPETUAL.100ms; public/set_heartbeat interval 30 and reply to test_request with public/test
- bitstamp: wss://ws.bitstamp.net bts:subscribe live_trades_btcusd
- chainlink: wss://ws-live-data.polymarket.com subscribe {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_twap_sixty","type":"update","filters":"{\"symbol\":\"btc/usd\"}"},{"topic":"crypto_prices_chainlink","type":"*","filters":"{\"symbol\":\"btc/usd\"}"}]} ; send text "PING" every 5s; ignore non-JSON.
- poly_book + poly_trades: market discovery every 30s via https://gamma-api.polymarket.com/events?slug=btc-updown-5m-<ts> for ts = current 5-min boundary (floor(now/300)*300) and next two boundaries; take clobTokenIds (JSON string) from markets[0]; also record discovered market metadata to stream "poly_markets". Connect wss://ws-subscriptions-clob.polymarket.com/ws/market, send {"assets_ids":[...],"type":"market"}; when the token set changes, resubscribe (reconnect). Send "PING" every 10s. Route event_type book/price_change/tick_size_change -> poly_book, last_trade_price -> poly_trades. Also every 1s per active market poll https://data-api.polymarket.com/trades?market=<conditionId>&limit=100 and write new trades (dedupe by transactionHash+asset+price+size) to poly_trades with src "data_api".
- status: _health stream gets a heartbeat every 30s with per-stream msg counts, last msg age, reconnect counts; also write OUT/status.json atomically. status command prints status.json as a table + disk usage.
Keep it simple, robust, low CPU (no per-msg heavy work; json.loads only where needed for ts_exch; otherwise store raw string under "raw_s"). Add mdrecorder/README.md and deploy/mdrecorder.service (Nice=19, IOSchedulingClass=idle, CPUQuota=40%, MemoryMax=400M, Restart=always, user ubuntu, WorkingDirectory=/home/ubuntu/mdrecorder, venv at /home/ubuntu/mdrecorder/.venv).
Test: run `timeout 60 python -m mdrecorder.run --out /tmp/mdtest` and verify every stream has data, then fix bugs.
