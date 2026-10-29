"""Websocket stream workers and Polymarket discovery/polling."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
import websockets

from .common import NO_RAW, as_ms, iso_to_ms, make_record, ts_to_ms

log = logging.getLogger("mdrecorder")

MAX_SIZE = 16 * 1024 * 1024
BINANCE_SPOT_URLS = (
    "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker",
    "wss://stream.binance.com:443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker",
    "wss://data-stream.binance.vision/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker",
)
BINANCE_FUT_URLS = (
    "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker",
)
GAMMA_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
DATA_URL = "https://data-api.polymarket.com/trades?market={cid}&limit=100"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_AGENT = "mdrecorder/1.0 (+https://polymarket.com)"
_WS = " \t\r\n"


def _now():
    return time.time_ns(), time.monotonic_ns()


def _field(text, key, start=0):
    """Value of the first ``"key"`` field at/after ``start``, plus the index
    just past it. A substring scan, so it costs far less than json.loads."""
    marker = '"' + key + '"'
    idx = text.find(marker, start)
    if idx < 0:
        return None, start
    pos = idx + len(marker)
    n = len(text)
    while pos < n and text[pos] in _WS:
        pos += 1
    if pos >= n or text[pos] != ":":
        return None, pos
    pos += 1
    while pos < n and text[pos] in _WS:
        pos += 1
    if pos < n and text[pos] == '"':
        end = text.find('"', pos + 1)
        if end < 0:
            return None, n
        return text[pos + 1:end], end + 1
    end = pos
    while end < n and text[end] not in ",}]":
        end += 1
    return text[pos:end], end


def _array_items(text):
    """Slice a top-level JSON array into the raw text of each element.

    Scans braces/quotes directly so no object is built; the slices are
    stored verbatim and never re-dumped.
    """
    n = len(text)
    start = text.find("[")
    if start < 0:
        return
    depth = 0
    in_str = False
    esc = False
    obj_start = None
    for pos in range(start, n):
        ch = text[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth == 2:
                obj_start = pos
        elif ch in "]}":
            depth -= 1
            if depth == 1 and obj_start is not None:
                yield text[obj_start:pos + 1]
                obj_start = None
            if depth == 0:
                return


class WsStream:
    """One reconnecting websocket stream.

    Subclasses set the connection parameters and implement on_open/handle.
    """

    name = ""
    urls = ()
    app_ping_s = 0.0
    app_ping_text = None
    ws_max_queue = 1024

    def __init__(self, rec):
        self.rec = rec
        self.reconnect_evt = asyncio.Event()
        self.reconnect_reason = "closed"
        self._connected = False

    async def wait_ready(self):
        return True

    async def on_open(self, ws):
        for sub in self.subscriptions():
            await ws.send(json.dumps(sub))

    def subscriptions(self):
        return []

    async def handle(self, ws, text, tw, tm):
        raise NotImplementedError

    def parse(self, text):
        return json.loads(text)

    def ts_exch(self, msg):
        return None

    def emit(self, text, msg, tw, tm, stream=None):
        ts = self.ts_exch(msg)
        self.rec.write(
            stream or self.name,
            make_record(self.name, tw, tm, ts, raw=msg),
        )

    def emit_raw(self, text, tw, tm, stream=None):
        self.rec.write(
            stream or self.name,
            make_record(self.name, tw, tm, None, raw_s=text),
        )

    async def _app_ping(self, ws):
        interval = self.app_ping_s
        if interval <= 0:
            return
        payload = self.app_ping_text
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.reconnect_reason = f"ping:{type(exc).__name__}"
            try:
                await ws.close()
            except Exception:
                pass

    async def _watch_reconnect(self, ws):
        try:
            await self.reconnect_evt.wait()
            self.reconnect_reason = self.reconnect_reason or "resubscribe"
            await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _connect(self):
        last = None
        for url in self.urls:
            try:
                return await websockets.connect(
                    url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                    max_size=MAX_SIZE,
                    max_queue=self.ws_max_queue,
                    compression=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                log.warning(
                    "%s connect %s failed: %s: %s", self.name, url, type(exc).__name__, exc
                )
        if last is not None:
            # Only surface an error once every fallback URL failed; a working
            # fallback is not an outage.
            self.rec.stream_error(self.name, f"{type(last).__name__}: {last}")
            raise last
        raise RuntimeError("no url configured")

    async def run(self):
        backoff = 1.0
        down_since = None
        reason = "never_connected"
        counted = False
        while not self.rec.stopping:
            try:
                await self.wait_ready()
                if self.rec.stopping:
                    break
                ws = await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if down_since is None:
                    down_since = time.monotonic_ns()
                reason = f"connect:{type(exc).__name__}"
                if not counted:
                    self.rec.stream_reconnect(self.name)
                    counted = True
                await self._sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue

            self.reconnect_evt.clear()
            tasks = []
            try:
                await self.on_open(ws)
                if down_since is not None:
                    self.rec.gap(
                        self.name,
                        time.monotonic_ns() - down_since,
                        reason,
                    )
                down_since = None
                counted = False
                backoff = 1.0
                self.rec.stream_up(self.name)
                self._connected = True
                tasks.append(asyncio.create_task(self._app_ping(ws)))
                tasks.append(asyncio.create_task(self._watch_reconnect(ws)))
                async for raw in ws:
                    tw, tm = _now()
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                    try:
                        await self.handle(ws, raw, tw, tm)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.rec.stream_error(
                            self.name, f"handle:{type(exc).__name__}: {exc}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self.rec.stream_error(self.name, reason)
                log.warning("%s disconnected: %s", self.name, exc)
            finally:
                self._connected = False
                self.rec.stream_down(self.name)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await ws.close()
                except Exception:
                    pass

            if self.rec.stopping:
                break
            if down_since is None:
                down_since = time.monotonic_ns()
            reason = self.reconnect_reason or reason
            self.reconnect_reason = "closed"
            if not counted:
                self.rec.stream_reconnect(self.name)
                counted = True
            await self._sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.rec.stop_evt.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class BinanceStream(WsStream):
    def __init__(self, rec, name, urls):
        super().__init__(rec)
        self.name = name
        self.urls = tuple(urls)

    def ts_exch(self, msg):
        data = msg.get("data") if isinstance(msg, dict) else None
        if isinstance(data, dict):
            return as_ms(data.get("E") if data.get("E") is not None else data.get("T"))
        return None

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        self.emit(text, msg, tw, tm)


class CoinbaseStream(WsStream):
    name = "coinbase"
    urls = ("wss://ws-feed.exchange.coinbase.com",)

    def subscriptions(self):
        return [
            {
                "type": "subscribe",
                "product_ids": ["BTC-USD"],
                "channels": ["ticker", "matches"],
            }
        ]

    def ts_exch(self, msg):
        return iso_to_ms(msg.get("time"))

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        if not isinstance(msg, dict):
            return
        if msg.get("type") in ("subscriptions", "heartbeat", "error"):
            return
        self.emit(text, msg, tw, tm)


class BybitStream(WsStream):
    """Bybit v5 public trades plus one-level book (and tickers for perp)."""

    app_ping_s = 20.0
    app_ping_text = '{"op":"ping"}'
    topics = ()

    def subscriptions(self):
        return [{"op": "subscribe", "args": list(self.topics)}]

    def ts_exch(self, msg):
        return as_ms(msg.get("ts"))

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        if not isinstance(msg, dict):
            return
        if "topic" not in msg:
            return
        self.emit(text, msg, tw, tm)


class BybitPerpStream(BybitStream):
    name = "bybit_perp"
    urls = ("wss://stream.bybit.com/v5/public/linear",)
    topics = ("publicTrade.BTCUSDT", "orderbook.1.BTCUSDT", "tickers.BTCUSDT")


class BybitSpotStream(BybitStream):
    name = "bybit_spot"
    urls = ("wss://stream.bybit.com/v5/public/spot",)
    topics = ("publicTrade.BTCUSDT", "orderbook.1.BTCUSDT")


OKX_URLS = (
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://ws.okx.com/ws/v5/public",
)


class OkxStream(WsStream):
    """OKX v5 public trades plus best bid/ask (bbo-tbt)."""

    urls = OKX_URLS
    inst_id = ""
    app_ping_s = 20.0
    app_ping_text = "ping"

    def subscriptions(self):
        return [{
            "op": "subscribe",
            "args": [
                {"channel": "trades", "instId": self.inst_id},
                {"channel": "bbo-tbt", "instId": self.inst_id},
            ],
        }]

    def ts_exch(self, msg):
        data = msg.get("data") if isinstance(msg, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return as_ms(data[0].get("ts"))
        return None

    async def handle(self, ws, text, tw, tm):
        head = text.strip()
        if head in ("ping", '"ping"'):
            await ws.send("pong")
            return
        if head in ("pong", '"pong"'):
            return
        msg = self.parse(text)
        if not isinstance(msg, dict):
            return
        if "data" not in msg:
            if msg.get("event") == "error":
                self.rec.stream_error(self.name, str(msg.get("msg"))[:300])
            return
        self.emit(text, msg, tw, tm)


class OkxSwapStream(OkxStream):
    name = "okx_swap"
    inst_id = "BTC-USDT-SWAP"


class OkxSpotStream(OkxStream):
    name = "okx_spot"
    inst_id = "BTC-USDT"


class KrakenStream(WsStream):
    name = "kraken"
    urls = ("wss://ws.kraken.com/v2",)

    def subscriptions(self):
        return [
            {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}},
            {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}},
            # post_only markets still print book updates even with no trades.
            {"method": "subscribe", "params": {
                "channel": "book", "symbol": ["BTC/USD"], "depth": 10,
            }},
        ]

    def ts_exch(self, msg):
        data = msg.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return iso_to_ms(data[0].get("timestamp"))
        return None

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        if not isinstance(msg, dict):
            return
        if "method" in msg or msg.get("channel") in (None, "status", "heartbeat"):
            return
        self.emit(text, msg, tw, tm)


class DeribitStream(WsStream):
    name = "deribit"
    urls = ("wss://www.deribit.com/ws/api/v2",)
    _raw_rejected = False

    async def on_open(self, ws):
        await ws.send(json.dumps(self._subscribe(
            ["trades.BTC-PERPETUAL.raw", "ticker.BTC-PERPETUAL.100ms"]
        )))
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "public/set_heartbeat",
            "params": {"interval": 30},
        }))

    def _subscribe(self, channels, req_id=1):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "public/subscribe",
            "params": {"channels": channels},
        }

    def ts_exch(self, msg):
        params = msg.get("params") if isinstance(msg, dict) else None
        if isinstance(params, dict):
            data = params.get("data")
            if isinstance(data, dict):
                return as_ms(data.get("timestamp"))
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return as_ms(data[0].get("timestamp"))
        return None

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        if method == "heartbeat":
            params = msg.get("params") or {}
            if params.get("type") == "test_request":
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "public/test",
                }))
            return
        if method == "subscription":
            self.emit(text, msg, tw, tm)
            return
        if "error" in msg and not self._raw_rejected:
            # The raw trade channel can require authentication; keep the
            # ticker channel flowing and try the public 100ms trade channel.
            err = str(msg.get("error"))
            if "raw_subscriptions_not_available" in err:
                self._raw_rejected = True
                log.warning("deribit raw trades unavailable; using 100ms trades")
                try:
                    await ws.send(json.dumps(self._subscribe(
                        ["trades.BTC-PERPETUAL.100ms"], req_id=4
                    )))
                except Exception:
                    pass
            return
        if "result" in msg:
            return


class BitstampStream(WsStream):
    name = "bitstamp"
    urls = ("wss://ws.bitstamp.net",)

    def subscriptions(self):
        return [{"event": "bts:subscribe", "data": {"channel": "live_trades_btcusd"}}]

    def ts_exch(self, msg):
        data = msg.get("data") if isinstance(msg, dict) else None
        if isinstance(data, dict):
            micro = as_ms(data.get("microtimestamp"))
            if micro is not None:
                return micro // 1000
            seconds = as_ms(data.get("timestamp"))
            if seconds is not None:
                return seconds * 1000
        return None

    async def handle(self, ws, text, tw, tm):
        msg = self.parse(text)
        if not isinstance(msg, dict) or msg.get("event") != "trade":
            return
        self.emit(text, msg, tw, tm)


class ChainlinkStream(WsStream):
    name = "chainlink"
    urls = ("wss://ws-live-data.polymarket.com",)
    app_ping_s = 5.0
    app_ping_text = "PING"

    def subscriptions(self):
        # The RTDS server string-matches ``filters``, so it must be exactly
        # the compact form; whitespace in the JSON silently drops live
        # updates and leaves only the subscribe snapshots.
        filters = '{"symbol":"btc/usd"}'
        return [{
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_twap_sixty",
                    "type": "update",
                    "filters": filters,
                },
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": filters,
                },
            ],
        }]

    def ts_exch(self, msg):
        return as_ms(msg.get("timestamp")) if isinstance(msg, dict) else None

    async def handle(self, ws, text, tw, tm):
        if not text or not text.lstrip().startswith(("{", "[")):
            return
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "payload" not in msg:
            return
        self.emit(text, msg, tw, tm)


class PolyStream(WsStream):
    """Polymarket CLOB market websocket; reconnects when assets change."""

    name = "poly_book"
    urls = (POLY_WS,)
    app_ping_s = 10.0
    app_ping_text = "PING"
    ws_max_queue = None  # unbounded; this socket is the one that hits 1013

    def __init__(self, rec, discovery):
        super().__init__(rec)
        self.discovery = discovery
        self._assets = ()

    async def wait_ready(self):
        while not self.rec.stopping:
            assets = self.discovery.assets()
            if assets:
                return True
            await self._sleep(0.5)
        return False

    def subscriptions(self):
        self._assets = tuple(self.discovery.assets())
        return [{"assets_ids": list(self._assets), "type": "market"}]

    async def on_open(self, ws):
        assets = tuple(self.discovery.assets())
        if not assets:
            raise RuntimeError("no poly assets")
        self._assets = assets
        await ws.send(json.dumps({"assets_ids": list(assets), "type": "market"}))

    def ts_exch(self, msg):
        if isinstance(msg, dict):
            ts = msg.get("timestamp")
            if ts is not None:
                return ts_to_ms(ts)
        return None

    async def handle(self, ws, text, tw, tm):
        """Route events without parsing whole frames.

        The book channel can burst thousands of price_change frames a
        second. Each event is stored as the original text under "raw_s";
        event_type and timestamp come from a substring scan, so nothing is
        decoded and re-encoded on the receive path.
        """
        head = text[:64].lstrip()
        if not head:
            return
        if head[0] == "[":
            for event in _array_items(text):
                self._emit_event(event, tw, tm)
        elif head[0] == "{":
            self._emit_event(text, tw, tm)

    def _emit_event(self, event, tw, tm):
        etype, _ = _field(event, "event_type")
        if etype in ("book", "price_change", "tick_size_change"):
            stream = "poly_book"
        elif etype == "last_trade_price":
            stream = "poly_trades"
        elif etype is None and (
            _field(event, "bids")[0] is not None or _field(event, "asks")[0] is not None
        ):
            stream = "poly_book"
        else:
            return
        raw_ts, _ = _field(event, "timestamp")
        ts = ts_to_ms(raw_ts)
        self.rec.write(stream, make_record(stream, tw, tm, ts, raw_s=event))


class PolyDiscovery:
    """Gamma market discovery plus the per-market Data API trade poller."""

    def __init__(self, rec, session):
        self.rec = rec
        self.session = session
        self.markets = {}  # slug -> metadata
        self.seen = {}  # condition_id -> set of trade keys
        self.errors = 0
        self._last_assets = ()
        self.poly = None

    def assets(self):
        assets = []
        for market in self.markets.values():
            for token in market.get("clobTokenIds") or ():
                if token not in assets:
                    assets.append(token)
        self._last_assets = tuple(assets)
        return list(self._last_assets)

    async def _fetch_event(self, slug):
        url = GAMMA_URL.format(slug=slug)
        timeout = aiohttp.ClientTimeout(total=10)
        async with self.session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"gamma {resp.status}")
            return await resp.json(content_type=None)

    async def discover_once(self):
        boundary = int(time.time()) // 300 * 300
        targets = tuple(f"btc-updown-5m-{ts}" for ts in (
            boundary, boundary + 300, boundary + 600,
        ))
        found = {}
        fetched = set()
        for target in targets:
            try:
                events = await self._fetch_event(target)
            except Exception as exc:
                self.errors += 1
                self.rec.discovery_error(str(exc))
                continue
            if not isinstance(events, list) or not events:
                continue
            event = events[0]
            markets = event.get("markets") or []
            if not markets:
                continue
            market = markets[0]
            tokens = market.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except ValueError:
                    tokens = None
            if not isinstance(tokens, list) or not tokens:
                continue
            meta = {
                "slug": market.get("slug") or event.get("slug"),
                "event_slug": event.get("slug"),
                "conditionId": market.get("conditionId"),
                "clobTokenIds": tokens,
                "question": market.get("question"),
                "outcomes": market.get("outcomes"),
                "endDate": market.get("endDate"),
                "startDate": market.get("startDate"),
                "active": market.get("active"),
                "closed": market.get("closed"),
                "acceptingOrders": market.get("acceptingOrders"),
                "bestBid": market.get("bestBid"),
                "bestAsk": market.get("bestAsk"),
                "lastTradePrice": market.get("lastTradePrice"),
                "volume": market.get("volume"),
                "liquidity": market.get("liquidity"),
            }
            found[meta["slug"]] = meta
            fetched.add(meta["slug"])
            tw, tm = _now()
            self.rec.write(
                "poly_markets",
                make_record("gamma", tw, tm, iso_to_ms(meta.get("endDate")), raw=meta),
            )
        if not found:
            return
        # Keep markets whose fetch failed this cycle, but drop those that
        # rolled out of the current/next-two window.
        merged = {slug: meta for slug, meta in self.markets.items() if slug not in fetched}
        merged.update(found)
        assets = [token for meta in merged.values() for token in meta["clobTokenIds"]]
        changed = tuple(assets) != self.assets()
        self.markets = merged
        live = {m.get("conditionId") for m in merged.values()}
        for cid in list(self.seen):
            if cid not in live:
                del self.seen[cid]
        if changed and self.poly is not None:
            self.poly.reconnect_reason = "assets_changed"
            self.poly.reconnect_evt.set()

    async def discovery_loop(self):
        while not self.rec.stopping:
            try:
                await self.discover_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.rec.discovery_error(str(exc))
            await self._sleep(30.0)

    async def poll_once(self):
        markets = [m for m in list(self.markets.values()) if m.get("conditionId")]
        if not markets:
            return
        results = await asyncio.gather(
            *(self._poll_market(m) for m in markets), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                self.errors += 1
                self.rec.discovery_error(f"data_api:{result}")

    async def _poll_market(self, market):
        cid = market["conditionId"]
        url = DATA_URL.format(cid=cid)
        timeout = aiohttp.ClientTimeout(total=8)
        async with self.session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"data_api {resp.status}")
            trades = await resp.json(content_type=None)
        if not isinstance(trades, list):
            return
        seen = self.seen.setdefault(cid, {})
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            key = (
                trade.get("transactionHash"),
                trade.get("asset"),
                trade.get("price"),
                trade.get("size"),
            )
            if key in seen:
                continue
            seen[key] = True
            tw, tm = _now()
            ts = trade.get("timestamp")
            self.rec.write(
                "poly_trades",
                make_record("data_api", tw, tm, ts_to_ms(ts), raw=trade),
            )
        # The API returns at most the last 100 trades; bound the dedupe set.
        while len(seen) > 5000:
            seen.pop(next(iter(seen)))

    async def poll_loop(self):
        while not self.rec.stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.rec.discovery_error(f"data_api:{exc}")
            await self._sleep(1.0)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.rec.stop_evt.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# Every websocket URL the recorder may open, keyed by stream name. Fallback
# URLs are listed too so rtt.py measures each endpoint actually used.
ENDPOINTS = {
    "binance_spot": BINANCE_SPOT_URLS,
    "binance_fut": BINANCE_FUT_URLS,
    "coinbase": CoinbaseStream.urls,
    "bybit_perp": BybitPerpStream.urls,
    "bybit_spot": BybitSpotStream.urls,
    "okx_swap": OkxSwapStream.urls,
    "okx_spot": OkxSpotStream.urls,
    "kraken": KrakenStream.urls,
    "deribit": DeribitStream.urls,
    "bitstamp": BitstampStream.urls,
    "chainlink": ChainlinkStream.urls,
    "poly_book": (POLY_WS,),
}


def endpoints():
    """Unique ``(url, stream names)`` pairs for every configured socket."""
    merged = {}
    for name, urls in ENDPOINTS.items():
        for url in urls:
            merged.setdefault(url, [])
            if name not in merged[url]:
                merged[url].append(name)
    return [(url, tuple(names)) for url, names in merged.items()]
