# Poly Money Maker: a guided tour of the live hourly system

This tour explains the system captured on 9 September 2026. It follows `buybothourly.py`, the primary hourly trader, from a quoted opportunity to a confirmed acquisition, a sale or redemption, and the local accounting that survives a restart. Python examples are taken from the source snapshot named in the footer. Hypothetical trades illustrate arithmetic; they are not performance claims.

Three documents have different jobs:

| Document | Question it answers |
|---|---|
| `CURRENT.md` | What is running today, with which settings? |
| `AGENTS.md` | What must a coding agent know before changing anything? |
| `TECHNICAL_DESIGN.md` | How is the system built, and why do its money paths work this way? |

Read Parts I and II straight through. Part III follows execution. Part IV lets you return to a helper without reopening the entire bot. Part V covers deployment, verification and the distinctions most likely to cause an expensive mistake.

## Contents

- [Part I - Picture](#part-i)
  - [The opportunity and its limits](#section-1)
  - [Processes, wallet identities and files](#section-2)
  - [Repository map and reading order](#section-3)
- [Part II - Ideas the code assumes](#part-ii)
  - [Books, FAK and three prices](#section-4)
  - [Python shape: values, state and side effects](#section-5)
  - [The external systems and Python packages](#section-6)
  - [JSON as durable memory](#section-7)
- [Part III - Walking buybothourly.py](#part-iii)
  - [Startup is execution because the file has no main guard](#section-8)
  - [Strategy load, validation, and the dry-run boundary](#section-9)
  - [Authentication and the working process](#section-10)
  - [Discovery, positions, and local uncertainty](#section-11)
  - [The clock, bands, and the actual hourly values](#section-12)
  - [WS, REST, and the GUI price](#section-13)
  - [Oracle and side selection](#section-14)
  - [From budget to signed share amount](#section-15)
  - [The pre-submit sequence](#section-16)
  - [FAK response, confirmation, and fees](#section-17)
  - [Hypothetical lifecycle: B15 at 90 cents](#section-18)
  - [Hypothetical lifecycle: A22 at a 94.9 cents ask](#section-19)
  - [Ambiguity is a state, not a guess](#section-20)
  - [What this code does not prove](#section-21)
  - [Exits: follow the shares, then follow the cash](#section-22)
  - [HEDGE: an oracle veto is not an entry signal](#section-23)
  - [HEDGE: qualifying, waiting, recovering, dumping](#section-24)
  - [Quotes: what “fresh” actually measures](#section-25)
  - [TP: half once, then a full lock](#section-26)
  - [SELL: persist intent, validate, post, reconcile](#section-27)
  - [Accounting: remaining basis is not lifetime acquisition debit](#section-28)
  - [A checked hypothetical lifecycle](#section-29)
  - [REDEEM and GC: submission is not receipt](#section-30)
  - [LOOP: futures reduce waiting, but do not remove ordering](#section-31)
- [Part IV - The buy/ helpers](#part-iv)
  - [The ownership map](#section-32)
  - [Market identity before market prices](#section-33)
  - [Hourly ET labels and epoch clocks](#section-34)
  - [Entry policy returns meaning, not an order](#section-35)
  - [BTC evidence: last print against an opening reference](#section-36)
  - [CLOB freshness includes quote shape](#section-37)
  - [Hedge helpers as state-transition interfaces](#section-38)
  - [SDK objects and precise money boundaries](#section-39)
  - [Depth telemetry measures hypothetical capacity](#section-40)
  - [Sibling complement and mint boundaries](#section-41)
- [Part V - Operations, verification and sharp edges](#part-v)
  - [Operations are another state machine](#section-42)
  - [CI updates files; it does not reload Python](#section-43)
  - [Testing without constructing a trader](#section-44)
  - [The recorder and the limits of replay](#section-45)
  - [Landmines worth remembering](#section-46)
  - [Glossary](#section-47)
  - [Source snapshot](#section-48)

<a id="part-i"></a>
# Part I - Picture

<a id="section-1"></a>
## The opportunity and its limits

An hourly Bitcoin Up or Down market pays one unit of collateral per winning outcome share and zero per losing share. The two outcome tokens represent opposite answers to the same question. A token identifier is the exchange's identifier for one answer; a condition identifier groups the answers into their market. Buying the Up token is not buying Bitcoin. Bitcoin supplies the reference price used to decide which answer wins.

The **Price To Beat (PTB)** is the reference value at the window's start. The hourly bot compares the latest Binance BTC/USDT price with its stored window-open value. If the latest price is higher, Up is favored by that comparison. If lower, Down is favored. This is an entry and risk-management signal. Binance is not proof that Polymarket has finalized a payout: settlement and redemption are separate observations.

The strategy waits until the final twenty minutes. It looks for a book that prices one outcome highly, checks that both sides support the same interpretation, and requires the underlying price to agree. It then buys the favored outcome with a marketable limit. It is trying to collect the remaining premium between its acquisition cost and a later sale or payout near one dollar.

The apparent premium is also the risk compensation. Paying 97 cents leaves only three cents per share before fees if the share wins and is held to payout. A single loss can consume many small wins. High observed win rate is therefore not enough: realized entry costs, partial exits, losing tails and transaction fees determine the return. The architecture can enforce limits and preserve evidence. It cannot create an edge merely by confirming that a price is high.

The configured entry slices are:

| Slice | Time remaining | Qualifying ask | Reference budget | Reference price | Book persistence |
|---|---|---|---:|---:|---:|
| b15 | Final 20 minutes | 0.90-0.94 | $6 | 0.90 | 20 seconds |
| a22 | Final 20 minutes | At least 0.949, within its upper band | $40 | 0.95 | 8 seconds |
| c5 | Disabled | No active window | - | - | - |

The names are historical identifiers, not a promise that the windows start fifteen or twenty-two minutes before expiry. The JSON values set the actual windows. The a22 marketable limit can be 0.99 even when the observed ask is lower. Eligibility price, signed limit and average fill price are three different numbers.

Both enabled slices can buy the same market on the same leg. "One entry per market" is implemented through per-slice stamps. It does not mean one buy total across both slices. After an exit closes the market's tracked position, the closed marker prevents ordinary re-entry. A missed b15 opportunity does not become extra a22 budget.

**Share-target sizing** starts with a desired share quantity derived from a reference price. The a22 reference calculation is $40 / 0.95, approximately 42.105 shares, before the executor's amount constraints. A richer ask can spend more for similar shares. This differs from constant-dollar sizing and from increasing the stake until every win makes a fixed dollar profit. Caps still bound the order intent and market spend; the detailed rounding path appears in Part III.

A **hedge** in this repository usually means selling the held outcome token to reduce exposure. It does not necessarily mean buying the opposite leg. **Take-profit (TP)** is another reason to sell that same inventory: part of the bag when the bid exceeds its remaining cost by four cents, and a full-lock path when the bid reaches 0.999. A sale and a redemption both reduce exposure, but they use different services and produce different evidence.

### Three possible endings for one acquisition

Assume, just for arithmetic, a confirmed acquisition of 40 shares for $38, with no fees. The remaining cost is 95 cents per share.

1. The bid reaches 99 cents and satisfies the half-TP persistence rule. Selling 20 shares at that price returns $19.80. Economically, the unsold 20 shares retain $19 of cost and the original acquisition debit is still $38. The current implementation writes the reduced basis into `pnl_entry_cost`; Part III explains the resulting distinction between correct economic arithmetic and the field used by final accounting. If the rest sells for $19.98, total proceeds are $39.78 and total profit is $1.78.
2. The book reverses, the oracle policy permits a hedge, and the sell actually executes at 50 cents. Forty shares return $20. The loss is $18. A threshold is a decision rule, not a guaranteed execution price: the available bid can move while an order is built.
3. No sale occurs and the held leg wins. Forty shares have a potential payout of $40, but a displayed winner is not cash already received. The redemption path must submit the correct contract call and preserve uncertainty until settlement evidence is sufficient.

These endings explain why cost, quantity, proceeds and order state cannot be compressed into a single "bought" Boolean.

<a id="section-2"></a>
## Processes, wallet identities and files

The live tree is `/home/ntemusejoel/poly-money-maker` on a Linux VM. The trader runs as `ntemusejoel`, using the project's Python virtual environment. A virtual environment supplies a particular set of Python packages; it is not isolation from the filesystem, network or wallet. Running the wrong script inside the correct environment can still trade.

**systemd** is Linux's service manager: it launches a configured process, tracks its state and applies the unit's restart policy. A repository file named `buybot5m.py` says nothing about whether it is running. A unit being enabled describes startup policy, not necessarily its current activity. The service state was read during this tour:

| Unit | Program | Observed state |
|---|---|---|
| `polybuybothourly.service` | `buybothourly.py` | Active, running; process start 2026-09-09 03:08:38 UTC |
| `polypathlog.service` | `pathlog.py` | Active, running; records without placing orders |
| `polybuybot5m.service` | `buybot5m.py` | Inactive |
| `polybuybot.service` | `buybot.py` | Inactive |
| `polycomplement.service` | `complementbot.py` | Inactive |
| `polymintbot.service` | `mintbot.py` | Inactive |

A stray `polybuy5m.service` also appeared as not-found/failed. It is not the active trader and is not the correctly named `polybuybot5m` unit. Operational inspection should keep those names distinct.

The public primary funder/proxy address supplied for this desk is `0x822279ae008c54b7ab4dd733994c76a711258b4e`. The secondary complement account has public funder address `0xCfF52577f80222e4b36f03B5d58443781b9D2433`. They identify accounts to observe, not credentials to use. This documentation task did not read private configuration to re-derive either identity.

The signer, funder and API account serve different roles. An externally owned account signs an instruction with a private key. The funder/proxy holds the collateral and outcome tokens used by the trading account. CLOB credentials authenticate exchange requests. Relayer or Builder credentials authenticate the transaction-submission service. One successful authentication does not prove that all those identities describe the same wallet.

The code checks these boundaries. For example, `submit_proxy_tx` refuses a derived proxy wallet different from `FUNDER_ADDRESS` at `buybothourly.py:4503`. That is a money-path check, not a cosmetic address comparison: otherwise a redemption could be signed for an account different from the one whose position the bot is tracking.

| Local file | What it represents | How to read it |
|---|---|---|
| `strategy_buyhourly.json` | Operator settings | Current parameters, not executable code |
| `positions_buyhourly.json` | Durable market metadata and unresolved intents | The bot's memory of acquisitions, sales, quantities and recovery |
| `pnl_buyhourly.json` | Finalized profit-and-loss records | A finalized subset, not necessarily complete wallet accounting |
| `ptb_binance_buyhourly.json` | Cached reference prices | Reproducible window-open observations |
| `buybothourly.log` and rotations | Structured event tape | Decisions and execution evidence, with finite retention |
| `.heartbeat_buyhourly` | Most recent loop heartbeat | Process progress, not proof that every upstream feed is fresh |
| `underlying_research_buyhourly.jsonl` | Research observations | A separate record for later analysis |
| `buy_data_hourly/depth_ladder.jsonl` | Depth-based estimates | Hypothetical capacity at sampled prices |
| `buy_data_hourly/depth_topup.jsonl` | Additional depth-path diagnostics | Simulated follow-on sizing, not orders |
| `pathlog/ticks/*.jsonl` | Recorder observations and resolution labels | Check timestamp gaps before replaying |
| `late_edge_bleed.jsonl` | Post-hour |live−PTB| late-vs-early bleed (VM-local) | Written by `check_late_edge_bleed.py`; not a trading input |

JSON Lines, or JSONL, stores one JSON object per line. An event log can append a line without rewriting a whole collection. The position file is instead one structured snapshot: replacing that file safely matters because a partially written snapshot could erase the only record of a submitted order.

`strategy_buyhourly.json` is deliberately tracked in the Sep 9 sync for byte verification. That exception does not make other state or secret files source code. The example strategy keeps `dry_run=true` and entries disabled while mirroring the captured strategy parameters. The actual strategy has `dry_run=false`; displaying it in Git is not a safe way to launch a second process.

<a id="section-3"></a>
## Repository map and reading order

Start with the entry point, but do not read all its formatting code before reaching the first order. The large script includes setup, helpers, exchange adapters and a loop in one file. The importable modules remove selected decisions and parsers from that script. Tests then exercise those pieces without starting the process.

| Area | Responsibility | Read when |
|---|---|---|
| `buybothourly.py` | Hourly orchestration, execution, recovery and accounting | Following actual money movement |
| `buy/entry_skip.py` | Band selection, per-slice admission, spend checks | Explaining why an entry is allowed |
| `buy/hedge_gate.py` | Exit decisions, persistence, oracle and TP helpers | Explaining why a held position may sell |
| `buy/btc_price.py` | Underlying feed and window-open reference | Explaining the BTC agreement gate |
| `buy/clob_book_ws.py` | Streaming book cache | Explaining quote availability and age |
| `buy/market.py` | Market discovery and normalized market records | Explaining condition/token/time identity |
| `buy/book.py` | Book-level parsing | Translating an exchange payload into prices and sizes |
| `buy/depth_ladder.py` | Depth telemetry and simulations | Assessing sampled liquidity without confusing it with execution |
| `pathlog.py` | Independent no-order recorder | Building research inputs |
| `tests/` | Regression tests and isolated source extraction | Checking contracts and failure cases |
| `deploy/`, `.github/workflows/` | Service templates and automation | Understanding deployment boundaries |
| `check_*.py` | Diagnostics and research tools | Answering a specific question after reviewing assumptions |

The sibling bots are not aliases for the hourly loop. They have related code and some shared helpers, but retain different time units, defaults and behavior. A fix in a shared helper can affect an inactive sibling's next deployment. A fix made only inside the hourly file does not automatically repair another copy.

<a id="part-ii"></a>
# Part II - Ideas the code assumes

<a id="section-4"></a>
## Books, FAK and three prices

A **central limit order book (CLOB)** matches buyers and sellers by price; its highest bid is the best visible sale price and its lowest ask is the best visible purchase price. The spread is ask minus bid. The size at a price describes the displayed quantity at that level, not an assurance that the quantity will remain until this process reaches the exchange.

**Fill-And-Kill (FAK)** executes whatever quantity can immediately match within the signed limit and cancels the remainder. It is not an instruction to keep retrying forever, and it is not all-or-nothing. The Python retry loop may submit another FAK, but that is another execution attempt that needs its own identity and checks.

Suppose the visible ask is 0.95 and the permitted buy limit is 0.99. The entry gate can be satisfied at 0.95, while the signed order permits fills up to 0.99. Some fills might arrive at 0.95 and others higher. The final average is total acquisition value divided by actual acquired shares, with fees interpreted consistently. The gate price cannot replace the signed limit, and neither replaces actual financial evidence.

On the sell side, a 0.999 bid can trigger full-lock while the executor submits a 0.99 limit because of tick handling. A **tick** is the allowed price increment. Rounding a sell price down makes the instruction more marketable but weakens the minimum price. The exchange may still fill at the better bid. The code must report trigger, limit and execution separately if a reader is to know what happened.

A displayed probability is also not an independent oracle. The bot derives website-style prices from a sufficiently tight book midpoint or eligible last-trade data. It does not automate the Polymarket webpage. Asking for book consensus and a derived display comparison filters inconsistent quotes; it does not create a second statistically independent forecast.

<a id="section-5"></a>
## Python shape: values, state and side effects

`buybothourly.py` executes substantial work at module scope. Python runs top-level statements on import. Here that includes environment loading, a process lock, logger setup, clients and eventually the main loop. `import buybothourly` is therefore not a harmless way for a test to obtain a helper. The absence of an ordinary application boundary is why many tests parse the file and extract selected function definitions instead.

The safe reading rule is to distinguish a definition from its execution. Reading a `def` creates a function object; calling it performs the body. Reading an import may execute another module's top-level statements. Constructing a feed object may start a background thread, whereas importing a small decision module may only define types and pure functions. "It is under buy/" is useful organization, not a substitute for inspecting side effects.

A **keyword argument** names the value at the call site: `load_json(path, required=True)` is clearer than a pair of positional values when the second value changes failure policy. In the actual signature at `buybothourly.py:787`:

```python
def load_json(path, *, required=False):
```

The standalone `*` makes `required` keyword-only. The caller can supply `path` positionally but must name `required`. `**kwargs`, when present in other helpers, collects named arguments into a dictionary; `**mapping` at a call expands a dictionary into named arguments. Those are different uses of the same syntax. Execution callbacks use named arguments so a price cannot silently be mistaken for a deadline.

An annotation such as `Optional[float]` means a value can be a floating-point number or `None`. `None` is missing knowledge, not zero. A missing bid means the bot cannot establish a sale quote. A zero balance returned by a successful query means something different from a failed query. Turning both into `0.0` makes an unavailable wallet look empty and can erase inventory incorrectly.

Use strings for token and condition identifiers even when they contain only digits. They are names, not measurements. A float is convenient for quote comparisons and elapsed time, but it represents binary approximations. `Decimal` provides deliberate decimal arithmetic for order amounts and rounding. Even Decimal does not choose the policy: the code must specify which side to round, how many units the exchange accepts, and whether a cost includes fees.

A dataclass groups named fields into an object with generated construction and comparison behavior. A NamedTuple groups named fields into an immutable tuple-like record. Both let a decision return several related values without forcing every caller to remember that the third tuple position means a particular timestamp. They do not validate exchange data automatically unless the implementation adds validation. Part IV follows the actual records in the helper modules.

The bot also uses futures. A future is a handle to work running elsewhere; the loop can ask whether it is done without waiting for the network operation. This helps keep an existing position's exit work ahead of an unrelated market lookup. It introduces stale snapshots, however. A fast loop reading the same old future result is not a fresh market observation on every cycle.

<a id="section-6"></a>
## The external systems and Python packages

| Service | Question answered | Python integration |
|---|---|---|
| Gamma | Which markets exist, what are their outcomes, when do they end? | HTTP requests and `MarketGateway` |
| CLOB HTTP | What is the book/order/trade state, and can this signed order be submitted? | `requests` plus `py_clob_client_v2` |
| CLOB WebSocket | What changed in the streamed market book? | `websocket-client` through the book feed |
| Data API | What positions does this account appear to hold? | Paginated HTTP requests |
| Binance | What is the latest BTC/USDT print and the hourly window-open reference? | Underlying feed and reference cache |
| Relayer | Will the service submit this signed wallet transaction? | Relayer SDK, signing SDK and HTTP |
| Polygon contracts | Did the outcome-token transaction actually execute? | Encoded contract calls and settlement evidence |

A **Relayer** accepts a signed wallet transaction request and submits it to the chain; accepting that request is not the same as completing it. Gamma identifies a market, CLOB matches its trades, and the chain holds and redeems the tokens. Those services can disagree temporarily without any one response being fabricated.

An **SDK**, or software development kit, packages protocol details into Python types and methods. It still has behavior that matters to latency and money. `ImmediateResponseClobClient` at `buybothourly.py:106` overrides the SDK's transaction-hash resolution hook to return the POST response promptly. The bot then owns confirmation instead of waiting inside the SDK while other position work is blocked. The override does not turn POST acknowledgement into finality.

`OrderArgs` and `PartialCreateOrderOptions` describe the order and its tick options. `ExchangeOrderBuilderV2` helps derive the deterministic signed order identity before posting. `ROUNDING_CONFIG` is SDK amount-format policy used alongside the bot's sizing logic. The implementation must match the installed SDK contract; inspecting only the public method name misses rounding and response-decoding behavior.

The redemption path uses `py_builder_relayer_client` to encode and sign a proxy transaction and `py_builder_signing_sdk` for an alternative authentication-header route. `eth_abi` turns typed arguments into contract-call bytes; `eth_utils` supplies address and hash utilities. These libraries solve encoding and signing tasks. They do not decide whether the position is economically ready to redeem.

The dependency list in `requirements.txt` is not version-pinned. That makes the deployed environment another part of the reproducibility story: a source hash identifies the Python file, not every SDK implementation it will import. The CI environment uses Python 3.12; the VM audit environment reported Python 3.11.2. The deployed runtime and compiled source must agree on supported syntax and interfaces.

<a id="section-7"></a>
## JSON as durable memory

The bot needs to remember an order before it sends it. Otherwise a crash after acceptance but before the response reaches Python leaves the restarted process thinking it never tried. Repeating the full buy budget can then double the intended exposure. This is the purpose of **write-ahead intent**: persist the order's identity and baseline state before the POST can happen.

Durable does not mean infallible. It means the process makes deliberate writes that survive normal restart boundaries rather than relying on local variables. `atomic_save` at `buybothourly.py:748` begins:

```python
tmp = path + ".tmp"
backup = path + ".bak"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, allow_nan=False)
    f.flush()
    os.fsync(f.fileno())
```

The temporary filename keeps a reader from seeing half of the new JSON. `allow_nan=False` rejects non-finite floating values that would make financial state ambiguous. `flush` moves Python's buffered bytes to the operating system. `fsync` requests that the file data reach durable storage. These steps have different jobs; calling only `flush` does not provide the same persistence boundary.

The next block validates the existing primary file before replacing the recovery copy. If the primary was corrupt and the loader recovered from `.bak`, blindly copying that corrupt primary over `.bak` would destroy the remaining good state. After preparing a valid backup, `os.replace(tmp, path)` at line 778 switches the primary path to the completed file. The directory is then synchronized at lines 779-784 so the filename replacement itself is included in the durability work.

`load_json` tries the primary and then the backup. It requires a dictionary root, rejects invalid numeric constants and raises when recovery has failed. A required live-state file that is missing is not treated as a brand-new account. That refusal prevents "empty local JSON" from becoming permission to re-buy a wallet position whose history was lost.

An uncertain order is a durable question: did this specific signed instruction execute, and for how much? It is not a new order type on the exchange. The metadata preserves the exact order identifier, token, pre-order size, observed fills and timing needed to answer that question later. While unresolved, the loop can block additional actions for that position rather than guess.

Atomic files do not form a database transaction across several independent JSON files and a remote exchange. The program still has to make finalization idempotent: repeating an already-accounted observation should not add the same proceeds again. It also has to distinguish intent written before POST, a definite rejection before acceptance, a matched response, confirmed trade evidence and final cash accounting. The next part follows those boundaries in source order.

<a id="part-iii"></a>
# Part III - Walking buybothourly.py

<a id="section-8"></a>
## Startup is execution because the file has no main guard

The hourly module begins with imports and immediately loads environment variables at `buybothourly.py:118-119`. It then acquires the process lock at `buybothourly.py:122-137`. The important shape is:

```python
def acquire_process_lock(path):
    """Fail closed when another copy of this bot is already running."""
    lock_fh = open(path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[bold red]Another hourly buy bot process already holds the runtime lock.[/]")
        raise SystemExit(1)
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()
    return lock_fh


_PROCESS_LOCK_FH = acquire_process_lock("/tmp/poly-money-maker-buybothourly.lock")
```

Line 122 defines a function whose `path` argument is the lock-file location. Line 126 asks the operating system for an exclusive, nonblocking lock. Exclusive means two hourly processes cannot both own the same runtime lock. Nonblocking means a second process fails promptly rather than silently waiting and later duplicating orders. Line 137 calls the function during import. `lock_fh` remains open, which keeps the advisory lock held for the process lifetime.

`buybothourly.py` has no `if __name__ == "__main__"` guard. Importing it executes startup, authentication, and the loop. Tests must therefore avoid importing the bot. The repository instructions explicitly call this out. A safe reader imports `buy/` helpers or uses AST extraction for analysis.

The imports at `buybothourly.py:1-103` also reveal the dependency boundary. `requests` serves HTTP work. `py_clob_client_v2` builds and posts CLOB orders. `dotenv` reads environment variables. The `buy.market`, `buy.btc_price`, `buy.clob_book_ws`, `buy.entry_skip`, and `buy.hedge_gate` modules provide discovery, the Binance last-print oracle, the WebSocket book, hourly band rules, and exit rules. Startup does not place an order merely because these modules import. The first order still passes the later strategy, book, oracle, balance, and persistence gates.

<a id="section-9"></a>
## Strategy load, validation, and the dry-run boundary

The strategy loader starts at `buybothourly.py:331`. It checks the file modification time, parses JSON, rejects unknown keys, casts each value to the type of its default, and validates relationships. The startup call is at `buybothourly.py:572`.

```python
if not exists:
    if _strat_cache is None:
        raise RuntimeError(
            f"strategy file {STRATEGY_FILE} is required at startup"
        )
    # Retain hedge parameters, but fail closed for new entries. Store the
    # safe snapshot too, otherwise an unchanged missing/bad file can return
    # the previously armed cache on the following cycle.
    cfg = _entries_disabled(_strat_cache or _STRATEGY_DEFAULTS)
    _strat_cache = cfg
    _strat_mtime = mtime
    return cfg
if _strat_cache is not None and mtime == _strat_mtime:
    return _strat_cache
cfg = dict(_STRATEGY_DEFAULTS)
try:
    with open(STRATEGY_FILE, "r") as f:
        overrides = json.load(f)
```

Lines 347-351 make a missing file fatal on the first load. A live bot cannot invent a budget. Lines 359-360 avoid reparsing unchanged JSON. Line 361 starts with code defaults. Lines 363-364 apply the file's overrides. This distinction is useful: defaults document intended behavior, while the live file chooses the current behavior.

A malformed file is handled differently after startup. Lines 340-345 create a safe copy with `entry_enabled` false and `hedge_enabled` true. Lines 559-569 return that safe copy when an already-running process sees a bad update. Existing inventory can still follow its hedge path, but new money is disarmed. A missing or malformed hot reload therefore stops new entries without turning a held bag into an unmanaged bag.

The validator at `buybothourly.py:383-557` checks price ordering, windows, caps, shares, reference prices, tick size, and live-mode safety. For example, `buy_threshold` must be no greater than `buy_max_price`; `buy_max_spend` must cover `market_spend_cap`; and `buy_max_shares` must cover the cap at the threshold. With reference sizing enabled, the validator also checks the largest slice budget divided by its reference price and the possible spend at the FAK limit. These are static rails. The live loop still checks the remaining cap for the particular market.

`dry_run` is special. The loader reads it at startup, and `STARTUP_DRY_RUN` and `DRY_RUN` are assigned at `buybothourly.py:640-641`. If true, state and P&L paths are changed to `.dryrun` names at `buybothourly.py:649-655`. The buy function exits at `buybothourly.py:3371-3377` after logging a `dry_buy`. It does not read a real token baseline, sign an order, or POST.

Hot reload can change entry thresholds, windows, budgets, and hedge knobs. It cannot safely turn an already-running process from dry to live. Treat `dry_run` as startup-only because the state paths and order behavior are selected before the main loop. A file edit is not a consent prompt. In live mode, the bot has no human confirmation question before a POST.

<a id="section-10"></a>
## Authentication and the working process

After configuration, startup chooses existing API credentials or derives them. The current branch is `buybothourly.py:4573-4589`.

```python
if API_KEY and API_SECRET and API_PASSPHRASE:
    api_creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]pre-generated API credentials loaded[/]")
else:
    temp_client = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    api_creds = temp_client.create_or_derive_api_key()
    console.print("[bold bright_cyan]▶ AUTH[/] [dim]API credentials derived from private key[/]")

client = ImmediateResponseClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=api_creds,
    signature_type=1,
    funder=FUNDER_ADDRESS,
    retry_on_error=False,
)
```

Lines 4573-4579 select credentials. Line 4581 constructs a subclass that returns the POST response instead of waiting in the SDK's long transaction poll. Lines 4582-4588 bind the private key, credential object, chain, signature mode, and funder.

The client setup then synchronizes collateral allowance at `buybothourly.py:4591-4596`, creates `MarketGateway` at 4598, starts the Binance feed at 4599, and gets the shared WebSocket feed at 4600-4601. The loop state is initialized at `buybothourly.py:4640-4677`. Live state is loaded with `required=not DRY_RUN`. That means missing live positions state is a startup refusal, while a dry-run process may start with an empty dry-run state file. The executor pools separate market discovery, book work, refreshes, entries, and redemption status. The CLOB client calls themselves are serialized by `safe_api_call` at `buybothourly.py:696-703`, which holds `_clob_lock` while calling the SDK. Threads improve polling responsiveness; they do not make one client call magically safe to interleave.

<a id="section-11"></a>
## Discovery, positions, and local uncertainty

Market discovery comes through `MarketGateway` with the hourly series slug. The loop keeps a cached market snapshot and refreshes positions from the Data API. `fetch_all_position_rows` at `buybothourly.py:991-1020` requests pages of 500 rows, includes archived rows, uses `sizeThreshold: 0`, and refuses to accept an overlong pagination result. A successful missing token is a zero at `check_token_balance`, lines 1031-1045. A request or parse failure is `None`.

That `None` versus zero distinction is essential. Zero says, "the successful response did not contain this token." `None` says, "we do not know."

`build_held_positions` at `buybothourly.py:1070-1103` filters slugs and outcomes, maps Up/Yes to `up`, Down/No to `dn`, and preserves asset, size, redeemable status, and average price. `merge_tracked_positions` at 1106-1145 then keeps locally tracked confirmed inventory in the hedge view when the Data API is temporarily behind. Its comment states that the execution ledger is authoritative for bot-confirmed fills and sells.

The local record is therefore more than a display cache. It contains the condition ID, selected token, leg, size, entry cost, slice flags, and temporary uncertainty fields. It can preserve a just-confirmed bag during API lag. It cannot safely guess a leg from an unrecognized token: lines 1139-1145 deliberately stop if the token cannot be mapped to the Up or Down asset.

The atomic state writer at `buybothourly.py:748-784` writes a temporary file, flushes and fsyncs it, copies a valid primary to a backup, replaces the primary, and fsyncs the parent directory. `load_json` at 787-810 tries the primary and then the backup, rejecting non-finite JSON. This is durable bookkeeping before it is durable accounting. It reduces crash loss; it cannot make a remote POST reversible.

<a id="section-12"></a>
## The clock, bands, and the actual hourly values

Hourly time is measured in minutes remaining. The helper in `buy/entry_skip.py:535-545` calculates the widest look-ahead. Its comment warns not to feed five-minute seconds into hourly logic. At `buy/entry_skip.py:548-603`, `applicable_hourly_entry_bands` opens a band only when `0 < minutes_left <= window`. A zero window disables a slice.

The helper `hourly_entry_final_gate` is at `buy/entry_skip.py:718-763`. Its signature begins:

```python
def hourly_entry_final_gate(
    minutes_left,
    *,
    selected_slice,
    buy_ask,
    bands,
```

This is the beginning of the signature; later keyword-only parameters are omitted. The bare `*` at line 720 requires named arguments for the remaining gates. As in Part II, an unavailable `buy_ask` is `None`, not a zero-priced opportunity.

The final gate rejects expired time, a closed hedge, a changed band, a failed book winner check, and a CLOB side flip. Its later lines require a valid leg, the oracle check, and GUI agreement. This is evaluated immediately before each POST, after the order has been built and write-ahead state has been saved.

<a id="section-13"></a>
## WS, REST, and the GUI price

The WebSocket feed is a fast look path. `buy/clob_book_ws.py:28-35` defines the endpoint, a two second staleness limit, and a quote tuple containing bid, bid size, ask, ask size, and mid. `quote` at lines 100-111 returns `None` when the cached quote is absent or too old. A price-only update cannot carry trustworthy size after a price move; the feed zeros that size so the caller must refresh REST before sizing.

The hourly look helper at `buybothourly.py:2004-2019` uses WS or a prefetched quote and avoids HTTP on the 0.01 second loop. REST is used when the bot decides to buy. In the retry function, lines 3448-3453 force a fresh REST quote before each attempt. That is the right division of labor: WS finds a candidate quickly, REST confirms the tradable book immediately before signing and again on retries.

A displayed GUI price is not always the midpoint. `polymarket_display_price` at `buybothourly.py:2101-2111` uses the midpoint when the spread is at most 10 cents. Otherwise it uses the last trade. For example, a 94 cents ask and 89 cents bid has a 5 cents spread, so the display is 91.5 cents. A 98 cents ask and 1 cents bid has a 97 cents spread, so the display falls back to the last trade. `entry_book_ok` at 2114-2133 protects against that illusion by requiring a noncrossed book, a spread no wider than the configured five cents, and a bid at least as strong as `min_winner_bid`.

The live JSON's winner bid is 90 cents. Thus a nominal 90 cents winning ask over an 84 cents bid fails the five cent spread check and also fails the bid floor. A 90 cents ask and 90 cents bid passes the basic book shape. The displayed loser GUI price and the underlying Binance/PTB check still matter.

The entry book must persist. `entry_book_persist_ready` at `buybothourly.py:2145-2167` records the monotonic time when the book first becomes acceptable, clears that arm when the book fails, and reports ready only after the configured wait. The live B15 wait is 20 seconds; A22 uses 8 seconds. A one-tick favorable quote does not satisfy the configured patience.

<a id="section-14"></a>
## Oracle and side selection

The hourly source sets `UNDERLYING_SOURCE` to Binance through `require_last_print_source(SOURCE_BINANCE)` at lines 154-155. The entry gate compares the live last BTC print with the market's window-open Price To Beat. The live JSON allows a zero minimum edge, but freshness and side direction still apply. "Zero dollars" here means no magnitude threshold beyond a nonzero, usable comparison; missing or stale data still fails closed.

The selected leg must agree across signals. The CLOB book must identify a winner. The GUI values must meet the configured winner and loser limits and minimum edge. The Binance last print must favor the selected Up or Down side. Positions add another constraint. `can_arm_hourly_slice` in `buy/entry_skip.py:680-715` refuses a closed market, a `buy_uncertain` record, a filled slice, an exhausted spend cap, and a purchase of the other leg while a bag is held. This is the no-straddle rule. A local bag may be small but still meaningful: the helper treats more than 0.01 shares as held. A missing position snapshot must not be converted into "no bag" by the caller.

<a id="section-15"></a>
## From budget to signed share amount

The buyer is a limit FAK. It does not submit a USDC market order. The core function starts at `buybothourly.py:3277`, and its docstring at 3297-3313 states the contract: the limit is the open band maximum, the maker amount is exact cents, the taker share amount is constrained, and the result is `(shares_bought, usdc_spent, status)`.

The share calculation is deliberately Decimal-based. `quoted_buy_shares_up_to_limit` at `buybothourly.py:2340-2397` uses a reference price when enabled. The live B15 reference is 90 cents. A $6 slice therefore targets about `6 / 0.90 = 6.666...` target shares. With the live 94 cents FAK limit, the exact-cent example is 6.50 shares and `6.50 * 0.94 = $6.11` of limit notional. The 2-decimal share tick and exact cent maker constraint can reduce the final amount slightly.

A22 is more instructive. Its $40 budget and 95 cents reference target about `40 / 0.95 = 42.105263` shares. Its FAK limit is 99 cents, so the desired spend is about `42.105263 * 0.99 = $41.6842`. That is a controlled overspend relative to the nominal slice budget, bounded by the market and global caps. The validator checks this worst case, and the call passes `market_spend_remaining` from `hourly_remaining_to_cap`.

The lower-level `quoted_buy_shares` helper at `buybothourly.py:2265-2304` explains the cents rule. It quantizes spend to `Decimal("0.01")`, starts shares at `spend / ask` quantized to `Decimal("0.01")`, applies the share cap, and decrements one share cent at a time until `shares * ask` is an exact cent amount. This avoids a CLOB 400 caused by an apparently harmless binary float.

The code's `Decimal` use does not make every value Decimal. The public function receives floats, converts with `Decimal(str(value))`, and returns floats for SDK arguments. That is a pragmatic boundary: exact arithmetic for the order amount, ordinary floats for API models and telemetry. Fees and reported financials are estimated or exact according to available exchange data.

<a id="section-16"></a>
## The pre-submit sequence

The retry loop at `buybothourly.py:3439-3525` is a sequence of money gates. It checks the deadline, remaining budget, fresh REST ask, price cap, minimum price, entry book shape, and share amount. If the ask rises over the band maximum, it waits briefly and retries with a new quote. If the ask falls below the band floor, it stops rather than buying a cheaper but differently classified signal.

It then creates a signed order at lines 3543-3564:

```python
signed_order = safe_api_call(
    client.create_order,
    OrderArgs(
        token_id=token_id,
        price=limit_price,
        size=shares,
        side=BUY,
    ),
    options=PartialCreateOrderOptions(
        tick_size=tick_size, neg_risk=False,
    ),
)
```

Line 3543 serializes SDK access. Lines 3546-3549 identify the exact token, price, size, and signed side. The `BUY` constant is a signed order direction; it is not a user-interface label. Lines 3551-3553 pass the 0.01 tick and risk mode. The deterministic signed ID is computed next and placed in `intent` with maker amount, taker amount, timestamp, and quoted shares.

The order is not posted immediately. `on_submit` is called at `buybothourly.py:3574-3590`. In the main loop its callback records the baseline balance, attempt, spend, price, order ID, and order size in local state. This is write-ahead intent. If the process dies after POST but before a normal fill callback, the next process has an order identity and a wallet baseline to investigate.

Immediately after write-ahead, `pre_submit` runs at 3591-3600. The hourly callback calls `hourly_entry_final_gate` with the fresh bid and ask, current minutes, current bands, oracle result, CLOB winner, and GUI values. If the gate rejects, `on_abort` clears the write-ahead because no POST occurred. The deadline is checked once more at 3601-3602. Only then does line 3605 call `client.post_order`, with `OrderType.FAK` at 3608.

This ordering has a useful crash invariant. A signed intent exists before the network POST. A failed final gate removes it synchronously. An accepted but unclear POST keeps the market quarantined. The source does not assume that an HTTP exception means no order was accepted.

<a id="section-17"></a>
## FAK response, confirmation, and fees

An FAK can match immediately, match partially, find no opposing liquidity, or produce an unclear response. The code distinguishes these cases. `unmatched_fak_rejection` at `buybothourly.py:3109-3117` retries only an HTTP 400 containing "no orders found to match." Other 400s, including invalid amounts or balance problems, do not receive the same retry treatment.

For an explicit unmatched rejection, the code checks the authenticated CLOB token balance before treating it as empty, at lines 3619-3625. This protects against a race in which the response says unmatched while inventory appeared. It then may refresh the quote and try again, up to the retry count. A fully empty trigger waits the configured `empty_fak_cooldown_s` before another cycle.

`confirm_fill_size` at `buybothourly.py:2895-3021` is intentionally conservative. A delayed response may contain the signed order amounts before matching, so those amounts are not automatically treated as fills. A terminal matched response must have settlement evidence. Nonterminal responses rely on GET-order `size_matched`. A transient order 404 is polled because the order index can lag a matched FAK.

The settlement path loads exact trade rows at 2707-2746. `_trade_settlement_state` at 2788-2810 returns confirmed only when every relevant trade is confirmed with a transaction hash. It returns partial when some trades confirm and others fail. A partial result preserves the confirmed subset rather than erasing it. This is a critical current limitation and strength: confirmation can remain pending while the chain evidence catches up.

Financial values follow the evidence available. `fill_cost_usdc` at `buybothourly.py:2556-2607` prefers an average price, then a reported making amount, then limit price times filled shares, and finally applies a known fee schedule if one exists. A missing fee schedule does not become a fabricated zero fee in every path; the function marks the fee unavailable and uses the gross amount. Exact confirmed trades are aggregated by `_confirmed_trade_financials` at 2748-2786. The amount decoder at 2663-2689 handles both fixed-six strings and human-unit decimal responses by comparing each interpretation with the expected signed amount.

<a id="section-18"></a>
## Hypothetical lifecycle: B15 at 90 cents

The following numbers are hypothetical. They illustrate code flow and are not a report of a live fill.

Assume an hourly market has 18 minutes left. The Up token has a REST bid of 0.90 and ask of 0.90. The Down token has a bid of 0.08 and ask of 0.10. The Binance last BTC print is above the window-open PTB, and the CLOB and GUI both identify Up. The B15 band is open because the ask is inside 0.90 through 0.94 and the configured window is 20 minutes.

The bot first sees a qualifying book and arms the B15 persist timer. It must remain qualifying for 20 seconds. At second 12, the bid falls to 0.84 and the five cent spread rule fails. The arm clears. At second 20, a new qualifying book begins a new timer. This protects the $6 slice from one favorable quote that disappears during the order build.

After the persist completes, the retry path obtains fresh REST data. The $6 budget and 90 cents reference target about 6.666 shares. The exact-cent and 0.01-share constraints yield a nearby legal amount. Suppose it signs 6.50 shares at a 0.94 limit. The limit notional is `6.50 * 0.94 = $6.11`, an exact cent amount. This is the clean legal example produced by the Decimal sizing path. The FAK may still fill at cheaper levels, so `$6.11` is the signed limit notional, not a promise about the final average price.

The signed order is written ahead with its order ID and baseline conditional-token balance. The final gate rechecks the band, book, oracle, CLOB winner, and GUI. The POST is FAK with a 0.94 limit. Suppose confirmed trade financials show 6.50 shares acquired at 0.90. Gross acquisition cost is `6.50 * 0.90 = $5.85`; gross VWAP is `5.85 / 6.50 = 0.90`. Fees must be added consistently when calculating fee-inclusive cost. The order's $6.11 limit notional and its $5.85 realized gross cost answer different questions. The bot persists confirmed size and cost through its fill callback.

If the POST response is accepted but times out, the bot does not spend the $6 again. It waits and checks the conditional-token balance against the baseline. If the balance delta is positive, it records a ghost fill and reconciles it. If the exchange order and trades remain unresolved, the market is marked `buy_uncertain` and blocked from further entry until exact order and trade evidence resolves it.

<a id="section-19"></a>
## Hypothetical lifecycle: A22 at a 94.9 cents ask

This example is also hypothetical. Assume 10 minutes remain and the Up ask is 94.9 cents with a 94.5 cents bid. The spread is 0.4 cents, so the entry book shape is strong. The A22 band is open because its live window is 20 minutes and its configured floor is 94.9 cents. C5 is disabled, so there is no last-five-minute competition for the band.

A22 uses the $40 budget and a 95 cents reference. The target is about 42.105 shares. The 99 cents FAK limit permits taking asks behind the 94.9 cents touch, but the spend ceiling is checked against the $48.50 market cap and $49 POST cap. If the order fills at 94.9 cents, the actual cost can be near $40. If it walks to 99 cents, the share-target calculation permits a higher spend, but the cap stops it from becoming an unbounded cheap-share order.

A fill below the configured band floor is classified against the average fill price. Extra shares caused by cheaper depth are not automatically toxic. A cheaper fill can simply mean more shares for the same money. The source's `classify_buy_fill` logic distinguishes a benign walk from a junk walk below `toxic_force_exit_below`, which is 65 cents in the live file.

<a id="section-20"></a>
## Ambiguity is a state, not a guess

The most dangerous operational mistake is to convert an unclear POST into either "empty" or "fully filled" without evidence. The buy function handles falsy responses, exceptions, nonterminal statuses, and order ID mismatches as ambiguity. It reconciles once and stops reposting the full budget.

The uncertainty fields are visible in the source around `buybothourly.py:3224-3246` and the callback at 7335-7367. They include the order ID, token, leg, baseline, attempted spend, price, quoted shares, known size and cost, trade IDs, slice, and prior slice values. These fields let a later loop compare a wallet delta with the exact attempt rather than with the market's entire history.

`inspect_uncertain_order` at `buybothourly.py:3120-3220` checks order identity, side, asset, market, trade settlement, matched size, and spend limits. An identity mismatch remains unresolved. A confirmed BUY can allow a walk above the requested shares when the exchange reports true trade financials; a SELL cannot silently accept an oversized result. The asymmetry reflects the different risk of crediting extra purchased inventory versus selling more than intended.

The main loop later attempts reconciliation. It can promote a confirmed order, record an empty terminal order, or keep observing a balance delta until it has repeated evidence. A single stale Data API zero is not enough to erase a just-bought bag. That is why local tracked inventory and remote wallet views are merged carefully.

<a id="section-21"></a>
## What this code does not prove

A confirmed CLOB trade proves matched trade evidence under the code's settlement rules. It does not prove that the market will resolve Up or Down, that the GUI was correct about probability, or that the final P&L will be positive. The Binance last print is the configured underlying signal; it is not a guarantee of the market's final resolution.

A displayed GUI midpoint is a presentation rule. It is not executable liquidity. A WS quote is a speed-path observation. It can be stale, incomplete, or replaced before POST. REST confirmation improves the decision but still has network and matching latency.

The source records fees only when the fee schedule or confirmed trade rows provide enough data. When the exchange response omits amounts, the code may estimate cost from the limit and filled shares, capped by the spend limit. That estimate is useful for risk rails and local bookkeeping, but it is not the same as an exchange statement.

The service state and source hashes for this tour are recorded in Parts I and V. Historical documents do not override those observations.


<a id="section-22"></a>
## Exits: follow the shares, then follow the cash

A position has two kinds of truth: confirmed local execution state and external inventory observations. `merge_tracked_positions` preserves locally confirmed shares when the Data API briefly omits them (`buybothourly.py:1106`). Otherwise a successful buy could disappear from the hedge loop during API lag. Uncertain orders add a third state: an action may have happened, but the bot cannot safely treat it as filled or empty yet.

The current strategy file matters more than historical comments. At this snapshot, ordinary hedge qualification is bid at most 0.60, ask at most 0.62, spread at most 0.20, persisting five seconds. Recovery is 0.63. Toxic dump is 0.35 with eight seconds of persistence and the tight-book option enabled. TP is enabled with a 0.04 edge, five-second persistence, half-size ordinary exits, and a 0.999 full-exit trigger (`strategy_buyhourly.json:11`, `:52`, `:62`). Comments mentioning 50/52 or “hedge exits only” remain in places where the current behavior is broader.

<a id="section-23"></a>
## HEDGE: an oracle veto is not an entry signal

Start with `hold_while_oracle_agrees` (`buybothourly.py:860`). Its boolean reads backwards if you expect a trading signal: true means hold. It obtains the last-print underlying check using the hedge-specific minimum edge, currently $10, then asks `hedge_oracle_allows_sell` whether the book path may continue. Entry's minimum underlying edge is separately zero.

The helper's complete signature is short enough to retain:

Source: [buy/hedge_gate.py:560](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L560) (excerpt).

```python
def hedge_oracle_allows_sell(held_leg, check, *, enabled=True):
```

The bare `*` makes `enabled` keyword-only. The executor signatures differ: their optional arguments remain positional-or-keyword, although callers usually name them.

The underlying edge is signed: last live BTC minus the price to beat, or PTB (`buy/btc_price.py:153`). A positive value favors Up; a negative value favors Down. `side_from_live_vs_ptb` leaves `favored=None` when the absolute edge is below the requested minimum. That does not erase the sign stored in `edge_usd`.

The current patch uses that retained sign:

Source: [buy/hedge_gate.py:592](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L592) (excerpt).

```python
if edge == 0:
    return True, "oracle_flat"
favored_small = "up" if edge > 0 else "down"
if favored_small == leg:
    return False, "oracle_still_winning_small"
return True, "oracle_against_small"
```

Read each branch as permission, not an order. Exactly flat permits book evaluation. Otherwise the sign reconstructs the favored side. Agreement with the held leg blocks an ordinary hedge even inside the $10 band; opposition permits evaluation. For an Up bag, +$3 blocks and -$3 permits. For a Down bag those answers reverse. Treating every `edge_too_small` as “flat enough to sell” would discard the patch's central distinction.

An explicitly favored held leg also blocks; an explicitly favored opposing leg permits. `edge_zero` permits. Missing checks, invalid legs, missing or non-finite small edges, and unrecognized reasons fail closed (`buy/hedge_gate.py:570`). Missing PTB, missing live data, stale live data, and non-last-print input return before a favored side is assigned (`buy/btc_price.py:379`). They therefore hold ordinary hedges. A missing feed is not evidence of a reversal.

When the wrapper holds, it clears ordinary persistence, completed persistence, and dump-arm state (`buybothourly.py:868`). Persistence cannot quietly mature behind an oracle veto. The final ordinary-sell callback checks the oracle again after write-ahead and before POST (`:6460`), so permission at the start of a slow cycle is insufficient.

<a id="section-24"></a>
## HEDGE: qualifying, waiting, recovering, dumping

`evaluate_held_bag` is a pure decision helper: it returns a `HedgeIntent` rather than contacting the exchange (`buy/hedge_gate.py:267`). The tuple carries action, reason, sell price, persistence state, GUI requirements, an abort boundary, and dump state. The caller owns the timers and the order side effects.

Ordinary qualification requires both sides, no crossed book, bid below the configured threshold, ask below its ceiling, and acceptable spread (`buy/hedge_gate.py:244`). The caller then checks display-price consensus. It reconstructs the website's midpoint-or-last-trade convention; it does not scrape the GUI. With current settings, held display must be at most 0.62, other display at least 0.38, and the held last trade at most 0.62 (`buybothourly.py:2213`, `:2226`). The passed `min_edge` is deliberately unused here. The other display need not exceed the held display.

Persistence is elapsed monotonic time across qualifying observations. `hedge_persist_ready` resets on false, arms on the first true observation, waits, then returns ready (`buy/hedge_gate.py:19`). Its signature also has a keyword-only `*` before timing arguments. Crucially, elapsed time alone does not complete ordinary persistence: the endpoint must still pass book and GUI qualification. Five seconds after arming, a now-wide book cannot turn the old arm into a sell.

Once a qualifying endpoint has completed persistence, the policy changes. A bid below 0.63 may sell, including a fade below 0.60 because `hedge_sell_fade` is true. At or above 0.63, recovery cancels the exit and clears persistence (`buy/hedge_gate.py:397`). The completed state is therefore permission to follow a failing bid, not permanent permission to sell a recovered winner.

Toxic dump has its own timer and runs before ordinary qualification. Any bag can take this route; the position need not have `toxic_fill=True`. At bid at most 0.35, the current tight-book mode requires an ask. Ordinarily spread must be at most 0.20, but ask at most 0.60 bypasses that spread test (`buy/hedge_gate.py:340`). Thus 0.34/0.99 is rejected, while 0.20/0.50 passes the helper despite its 0.30 spread. The low ask says the weakness is present on both sides.

There is an important caller-level restriction. Before reaching this helper, the hourly oracle override checks bid at most 0.35 and, with tight mode enabled, spread at most 0.20 (`buybothourly.py:6051`). It does not apply the helper's low-ask exception. Consequently 0.20/0.50 can satisfy the helper yet remain blocked when the oracle agrees or is missing. A tight 0.33/0.50 can bypass that veto, then must persist eight seconds. The exception is real, but its reach depends on which gate executes first.

Dump bypasses GUI consensus, and a selected dump bypasses the final oracle callback. Its executor still checks expiry and the configured bounce boundary. The eight-second qualification is not rerun from scratch on every retry; execution receives the already-selected dump state. Ordinary and dump timer dictionaries are in memory (`buybothourly.py:826`), whereas uncertain execution records are durable. A restart does not restore eight seconds of book observation merely because it restores an order intent.

<a id="section-25"></a>
## Quotes: what “fresh” actually measures

The WS cache records local monotonic receipt times. `quote` accepts a quote only within the caller's age budget; held checks use 0.25 seconds here (`buy/clob_book_ws.py:100`). Stream event timestamps reject grossly old or future messages, and older server timestamps cannot overwrite newer ones. Price-only updates preserve size only when price is unchanged and the previous receipt is recent (`:153`). A fresh price is not automatically fresh executable depth.

The usual selector prefers WS, then a rate-limited REST cache. `force_rest=True` bypasses both, issuing a new book request (`buybothourly.py:1964`). REST validates token identity, condition identity when supplied, numerical levels, and timestamp plausibility. Its timestamp rule deliberately allows an unchanged book timestamp up to thirty days old (`:1457`). A newly requested snapshot may describe a quiet book whose last mutation was old. This is not a 250-millisecond server-age guarantee.

The live caller has weaker fallback behavior than a quick reading of helper comments suggests. `last_good_held_quote` stores only bid and ask, with no age (`buybothourly.py:2022`). TP tries fresh WS, then this last-good quote, and requests REST only if no bid remains (`:5616`). A stale last-good bid can therefore arm or sustain TP persistence. Before selling, the executor requests REST, but an incomplete response can retain the prior bid or ask (`:3990`). The final TP validator sees that retained bid; it does not independently validate its timestamp.

Similarly, hourly hedge skips initial REST for either a fresh dump peek, a last-good dump, or completed persistence (`buybothourly.py:6154`). The shared `held_hedge_decision` helper describes stricter last-good handling, but this loop directly calls `evaluate_held_bag`. Follow the call site. Claiming that every dump begins with fresh two-sided REST would be incorrect.

These choices explain both speed and limitations. Keeping a failing bag sellable through incomplete books avoids a frozen exit. It also means repeated loop observations can reuse the same underlying quote. Persistence proves repeated qualification of the values inspected, not a continuous exchange-side measurement. Network latency remains between the final check and matching.

<a id="section-26"></a>
## TP: half once, then a full lock

TP runs after uncertainty recovery and expiry exclusion, before the ordinary hedge veto (`buybothourly.py:5567`). A profitable bag normally has an agreeing oracle, so applying the ordinary veto first would defeat profit-taking. Eligibility requires held shares above dust, no closed hedge, TP enabled, and a market start at or above the configured grandfather floor (`:5603`). The floor is inclusive; zero disables that restriction.

The code divides `pnl_entry_cost` by current `held_size`, with `fill_price` as fallback, to obtain TP VWAP (`:5641`). Ordinary qualification is bid at least VWAP plus 0.04. Full-lock qualification independently compares bid with 0.999. Either may qualify; full lock chooses fraction 1.0, otherwise fraction 0.5. After `take_profit_done`, only full lock can qualify again. A later add does not clear that flag.

Both routes share the same five-second TP timer. It measures their combined qualifying predicate rather than separate uninterrupted timers for half and full exits (`buybothourly.py:5666`). If ordinary edge already kept the predicate true, a full-lock observation can inherit that arm. If half TP has already happened, ordinary edge no longer keeps it alive.

Sizing uses decimal arithmetic and rounds down to two decimal places (`buy/hedge_gate.py:726`). Half of 10.01 shares becomes 5.00, not 5.005. “Half TP” is the requested size: a FAK may fill less. Any meaningful confirmed partial TP sets `take_profit_done`, so the next ordinary TP does not repeatedly halve whatever remains. Full lock stays available.

Separate the trigger from the exchange limit. The current full trigger is **bid 0.999**, not ask 0.999 and not a rounded GUI display. A 0.99 bid under a 0.999 ask does not qualify (`buy/hedge_gate.py:705`). Once qualified, a 0.01 execution tick rounds a 0.999 bid down to a 0.99 sell limit:

Source: [buybothourly.py:2049](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L2049) (excerpt).

```python
tick = hedge_exec_tick(tick_size)
undercut = max(0, int(undercut_ticks)) * tick
raw = float(bid or 0) - undercut
aligned = (int(raw / tick + 1e-12)) * tick
return max(tick, aligned)
```

First select an accepted tick. Then subtract any undercut, floor to that grid, and enforce one tick as the minimum. The `min_price` argument is intentionally ignored. The TP callback still requires the observed bid to meet its unrounded target (`buybothourly.py:5883`). That guards the decision, while the signed order permits execution at 0.99. It does not guarantee a 0.999 realized price.

<a id="section-27"></a>
## SELL: persist intent, validate, post, reconcile

`sell_market_with_retry` accepts shares, not a dollar budget (`buybothourly.py:3855`). Despite the name `create_market_order`, it supplies a price and posts a FAK. Fill-And-Kill means immediately executable shares may fill and the rest do not rest on the book. There is no promise that the whole requested amount trades.

Trace the order boundary line by line. At `:4096`, the chosen bid becomes a tick-aligned price. At `:4101`, `MarketOrderArgs` receives token, remaining shares, SELL, and that price. At `:4111`, the locally signed order yields an expected identity. The intent records that identity and signed amounts so a timeout can be investigated later.

At `:4149`, `on_submit` saves uncertain state before POST. Failure returns `persist_fail`; the executor does not knowingly post an unrecorded intent. At `:4164`, the caller-specific `pre_submit` callback runs after persistence. At `:4175`, the deadline is checked again. An expiry or veto invokes `on_abort` to clear the no-POST intent durably. Only then does the exchange call occur:

Source: [buybothourly.py:4179](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L4179) (excerpt).

```python
result = safe_api_call(
    client.post_order,
    signed_order,
    order_type=OrderType.FAK,
)
```

Response identity must match the expected order. Confirmed fill size updates cumulative sold shares, proceeds, and remaining shares before `on_fill` persists them (`:4197`, `:4214`). Proceeds prefer execution details and confirmed trade financials, with price/amount fallbacks (`:3044`). When fees cannot be determined, the function can return gross proceeds; its “net” docstring is not an unconditional fee guarantee.

A definitely unmatched FAK can re-quote and retry. Invalid tick errors can increase the tick and rebuild. An unknown POST outcome stops retries and leaves quarantine because another sale could duplicate an accepted order. Hedge calls allow twelve attempts for dump or completed persistence; TP uses the default three (`:6493`, `:5905`). Those are bounded attempts within one invocation, not a global guarantee of closure.

`reconcile_hedge_sold` keeps confirmed fills authoritative even if the Data API lags (`:2446`). A single external zero does not fabricate the unconfirmed tail. One executor exception is worth knowing: after an unmatched 400, a refreshed CLOB balance drop can be credited as a ghost fill at the last limit (`:4299`). That is inferred quantity and price, rather than ordinary exact-fill evidence.

Unknown sells are inspected by exact order on later cycles (`:5438`). Confirmed results combine previously sold quantity and proceeds with the inspected order; empty or failed results clear quarantine. Pending or identity-mismatched outcomes prevent further action. Recovery happens even after expiry, but new orders do not. Finishing an old accounting obligation and placing a new trade are different operations.

<a id="section-28"></a>
## Accounting: remaining basis is not lifetime acquisition debit

An acquisition debit is cash paid to obtain shares. For lifetime P&L, already-spent cash must remain counted after shares are sold. Remaining basis is the portion of acquisition cost attributed to shares still held. It should shrink after a partial sale and grow when new shares are acquired. These are distinct quantities even when they initially have the same value.

The current source does **not** maintain two independent durable fields for them. `pnl_entry_cost` starts as accumulated purchase cost, then TP mutates it into remaining basis. GC, spend-cap logic, and other notional calculations still read that same field. This is an implementation limitation, not an immutable-debit guarantee supplied by the patch.

The TP fill callback computes remaining shares from the original held-size snapshot, then shrinks cost proportionally:

Source: [buybothourly.py:5862](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L5862) (excerpt).

```python
if prior_size > 0.01 and float(sold_total or 0) > 0.01:
    meta["pnl_entry_cost"] = round(
        prior_cost * (rem_now / prior_size), 4
    )
```

Using a pre-sell cost and size prevents the final reconciliation from shrinking the same successful single-attempt fill twice. The final TP branch repeats this calculation before clearing uncertainty (`:5959`). Exact-order recovery also applies proportional cost reduction using `hedge_uncertain_entry_cost`, falling back to current cost when absent (`:5500`). This repairs the common half-bag case where full purchase cost divided by half the shares produced an impossible VWAP near two dollars.

There are limits. Every TP retry's submit callback overwrites `hedge_uncertain_entry_cost` from current metadata while retaining the original invocation's held size (`:5829`). If an earlier attempt already shrank cost, a later retry can snapshot that reduced cost and shrink it again. Ordinary hedge fill callbacks update size and proceeds without proportional basis reduction (`:6439`), while the shared exact-order recovery branch does reduce it. The accounting behavior is therefore not uniform across paths.

Adds accumulate current size and current cost with new fill size and spend (`buy/entry_skip.py:385`; `buybothourly.py:7296`). This produces a useful remaining-inventory VWAP after a clean half TP. But `hourly_spent_so_far` simply returns `pnl_entry_cost` (`buy/entry_skip.py:629`), so reduced basis can also reopen apparent spend-cap headroom. Slice flags and other entry gates still apply; TP proceeds themselves do not authorize another purchase.

A useful reading discipline is to annotate each assignment with units. `bought_size` is shares; `pnl_entry_cost` is dollars, although its intended accounting role changes; their quotient is dollars per share. `pnl_hedge_proceeds` includes TP sales despite its name. A dimensionally valid quotient can still be economically wrong if its numerator describes all historical purchases while its denominator describes only remaining inventory. Conversely, a valid remaining basis becomes the wrong debit when reused in lifetime P&L. Python will happily perform both calculations. The protection has to come from consistent state semantics, including callbacks, retries, restart recovery, and finalization, rather than from the arithmetic operator itself.

<a id="section-29"></a>
## A checked hypothetical lifecycle

Assume an eligible B slice buys 6 Up shares at 0.90, spending $5.40. This could be a partial FAK within the current $6 B budget. Assume no fees and exact one-attempt confirmations throughout. Initial size is 6, `pnl_entry_cost` is $5.40, and TP VWAP is $0.90. Lifetime acquisition debit is also $5.40, but retain that number separately in this example because the bot does not.

A 0.94 bid qualifies for ordinary TP: 0.90 + 0.04 = 0.94. After persistence, all 3 requested shares sell at 0.94. Proceeds are $2.82. Remaining shares are 3; remaining basis becomes $5.40 × 3/6 = $2.70. `take_profit_done=True`; `hedge_closed` remains false. The remaining VWAP is correctly $2.70/3 = $0.90.

Later, assume an unused eligible A slice passes its independent gates and buys 40 shares at 0.95, spending $38.00. This is hypothetical partial execution within the A budget, not a claim that TP automatically schedules an add. Inventory becomes 43 shares and remaining basis becomes $2.70 + $38.00 = $40.70. VWAP is $40.70/43 = $0.9465116279. Lifetime acquisition debit is $5.40 + $38.00 = $43.40. Both totals fit the current $48.50 market cap, although its helper now sees the smaller total.

Ordinary TP cannot fire again because the flag survives the add. Suppose bid later meets 0.999 and full-lock persistence completes. All 43 remaining shares sell at the permitted 0.99 limit. Proceeds are $42.57. Total sale proceeds are $2.82 + $42.57 = $45.39. Correct lifetime P&L is **$45.39 - $43.40 = $1.99**. As a cross-check, the first partial sale realized $0.12 and the final sale realized $42.57 - $40.70 = $1.87; together they are $1.99.

In this clean TP path, remaining basis becomes zero and `hedge_closed=True`. GC later reads that zero as entry cost and all $45.39 as hedge proceeds. Its recorded net would therefore be $45.39, not $1.99 (`buybothourly.py:4948`). This is why immutable acquisition debit and remaining basis cannot be treated as synonyms when explaining the current implementation.

<a id="section-30"></a>
## REDEEM and GC: submission is not receipt

Redemption submits CTF `redeemPositions` calldata with the collateral address, zero parent collection, condition, and outcome index sets `[1, 2]` (`buybothourly.py:4530`). The relayer builder validates signer/proxy relationships and returns a transaction ID after an accepted submission (`:4439`). That ID identifies work to inspect. It is not cash received.

Before submission, the loop saves an intent and expected value, calculated as the larger outcome balance (`:5112`). Successful submission records `redeem_pending` and its transaction ID. The intent is useful evidence, but by itself does not prevent all duplicates: an accepted response lost before the ID is saved leaves a retryable intent rather than a known pending transaction. Throttling limits retries; it cannot prove whether an unknown request executed.

Status checks run in a single-worker background executor. `STATE_CONFIRMED` or `STATE_MINED` sets the confirmation flag; `STATE_FAILED` clears pending fields, and `STATE_INVALID` is retryable (`:7604`). This path trusts relayer status. It does not independently decode a collateral-transfer receipt into realized proceeds.

Credit additionally requires a newly completed positions fetch and remaining inventory at most 0.01 (`:7667`). Fetching is all-pages-or-failure (`:991`), avoiding a truncated response masquerading as an empty wallet. However the actual balance lookup uses filtered `held`, rather than directly searching the raw returned rows. The code's comment says complete fresh snapshot; the implementation also depends on merge/filter behavior. Expected value supplies the credited amount, not a measured cash-balance delta.

GC only considers finalization after a successful positions refresh and terminal evidence. Uncertain buys, uncertain sells, and pending redemptions block deletion (`:2511`). `gc_par_redeem` returns only explicitly recorded redemption value; disappearance alone never invents a dollar per share. P&L is written before metadata deletion, and failure retains state for retry (`:4944`). `record_pnl` deduplicates by condition among retained trades and computes proceeds minus entry cost (`:957`). Its correctness still depends on callers supplying lifetime cost, which TP currently does not preserve.

<a id="section-31"></a>
## LOOP: futures reduce waiting, but do not remove ordering

Each cycle reloads strategy, consumes only completed position/balance/discovery futures, then schedules due replacements (`buybothourly.py:4690`, `:4780`). Confirmed local inventory is merged, dust is filtered, tracked markets are stubbed when needed, and held markets sort ahead of unheld entry work (`:4867`). Uncertain recovery also receives priority.

GC and optional UI work precede trading. Redemption submission is textually before the hedge phase, but its loop breaks whenever active hedge inventory or a qualifying entry window exists (`:5063`). Completed book futures are collected without waiting; pending ones carry forward. Within each market, recovery precedes expiry exclusion, TP precedes HEDGE, and BUY follows. A TP attempt ends that market's pass with `continue` (`:6032`); ordinary hedge processing can reach later buy checks, whose flags and closure rules decide eligibility.

Futures are not uniformly nonblocking. Entry confirmation launches four futures, then calls their `.result()` methods directly (`:7093`). Shared SDK calls also serialize through `_clob_lock` (`:696`). A book worker may therefore contend with execution despite separate thread pools. Sell retries, durable saves, and selected HTTP calls still occupy the main cycle.

After market work, the loop consumes and schedules redemption-status futures. It then chooses sleep duration, schedules missing book prefetches for the next cycle, updates WS subscriptions, and sleeps (`:7604`, `:7731`). The configured 0.01-second sleep is a pause after work, not a guaranteed hundred complete decisions per second. Reading the loop in this order explains why a timer, a returned future, a persisted intent, and confirmed money movement are four different milestones.
<a id="part-iv"></a>
# Part IV - The buy/ helpers

<a id="section-32"></a>
## The ownership map

The hourly script composes the system. Its imports show the boundaries: market metadata and underlying BTC evidence arrive through gateways; the CLOB feed supplies cached quotes; entry and hedge helpers evaluate supplied facts; depth helpers describe observed liquidity. The script owns SDK order construction, submission, and durable execution state ([buybothourly.py:51](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L51), [buybothourly.py:3543](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L3543)).

| Boundary | Owner | What crosses it |
| --- | --- | --- |
| Gamma discovery | `buy/market.py` | Market identity and window timestamps |
| Underlying evidence | `buy/btc_price.py` | Last BTC print, opening reference, freshness, favored leg |
| CLOB observations | `buy/clob_book_ws.py`, using `buy/book.py` | Token-keyed quotes and displayed sizes |
| Policy | `buy/entry_skip.py`, `buy/hedge_gate.py` | Bands, eligibility, reasons, proposed transitions |
| Research | `buy/depth_ladder.py` | Hypothetical capacity and sampled path summaries |
| Execution | Hourly script and CLOB SDK | Signed orders, responses, reconciliation evidence |

“No I/O” describes particular policy functions, not every module under `buy/`. Feeds own threads and caches; Gamma owns HTTP; depth logging appends JSONL. Several policy helpers mutate supplied metadata, but the caller owns saving it. A function named “stamp” can change an in-memory flag without making a durable transaction ([buy/entry_skip.py:665](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L665)).

<a id="section-33"></a>
## Market identity before market prices

`MintMarket` retains a historical name but is the shared discovery record consumed by hourly. A dataclass generates initialization, representation, and equality machinery from annotated fields. `frozen=True` prevents ordinary field reassignment, making metadata a stable value passed between components ([buy/market.py:14](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L14)).

Source: [buy/market.py:14](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L14) (excerpt).

```python
@dataclass(frozen=True)
class MintMarket:
    condition_id: str
    slug: str
    question: str
    end_ts: float
    series_slug: str
    up_token: str
    dn_token: str
```

Token identifiers are strings even when they resemble enormous integers. They name outcome assets; they are not quantities to round. Condition IDs identify markets, while Up and Down token IDs identify their tradable outcomes. `_parse_event` accepts arrays or JSON-encoded arrays, matches outcomes by name, requires two distinct tokens, and normalizes identifiers to strings ([buy/market.py:132](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L132)). Never infer Up from array position alone.

`MarketGateway` owns a reusable `requests.Session`, timeout, discovery cache, and `discovery_fresh` indicator. Session injection allows an alternate HTTP implementation without changing parsing. Discovery queries Gamma events by series and deduplicates by condition ID. Fresh-cache and bounded stale-cache returns are separate cases; freshness is exposed on the gateway ([buy/market.py:236](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L236)).

Metadata freshness differs from quote freshness. `discovery_allows_buy_look` permits an already-known market in its live window despite stale Gamma discovery, provided valid tokens and a close time exist. That permits a CLOB look; it does not establish entry eligibility ([buy/market.py:205](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L205)).

The same gateway also exposes Data API positions as a dictionary from string token IDs to float share balances. Unlike discovery's best-effort loop, this method raises on a failed HTTP request or a response that is not a list. That difference is part of the interface: an unavailable inventory snapshot must not silently become an empty wallet ([buy/market.py:308](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L308)). Type annotations describe expected shapes; they do not validate remote JSON automatically. The explicit parsing and validation implement that boundary.

<a id="section-34"></a>
## Hourly ET labels and epoch clocks

Hourly selects series `btc-up-or-down-hourly` and prefix `bitcoin-up-or-down` ([buybothourly.py:154](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L154)). Human-readable hourly slugs label Eastern Time hours. ET has daylight-saving rules, not a permanent UTC offset. Readable hour labels should not be interpreted in the machine's local timezone. The live gateway obtains dates from Gamma and normalizes them into epoch seconds; an epoch value identifies one instant regardless of the operator's Europe/Dublin display timezone.

Numeric 5m/15m slugs follow a different convention. `slug_start_ts` extracts a trailing Unix timestamp; `_parse_event` pins those windows to start plus series duration. For readable hourly slugs without that suffix, it uses Gamma dates and corrects inconsistent start/end durations against the requested series ([buy/market.py:75](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L75), [buy/market.py:157](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L157)). Do not interpret an ET label as an epoch suffix. Keep epoch seconds, feed milliseconds, and minutes-to-close distinct: `ttm_minutes` divides timestamp subtraction by 60 ([buy/market.py:29](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/market.py#L29)).

<a id="section-35"></a>
## Entry policy returns meaning, not an order

`entry_skip` owns band selection and reasons a candidate cannot arm. `EntryBand` is a `NamedTuple`: an immutable tuple with named fields and tuple unpacking. It carries a matching interval, exclusivity flag, slice name, and optional explicit FAK limit ([buy/entry_skip.py:8](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L8)). Matching ceiling and execution limit can differ: hourly A can match only through 0.95 while retaining a 0.99 limit when C is also open. Read `fak_limit`, not merely `max_price`.

Hourly windows use minutes; sibling 5m helpers use seconds. Nonpositive hourly windows disable slices. Selection gives B, then C, then A priority among matching bands. A union preserves gaps rather than treating separated intervals as one continuous range ([buy/entry_skip.py:494](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L494), [buy/entry_skip.py:548](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L548)). Defaults and slice names describe reusable machinery, not evidence of currently configured budgets.

`Optional[EntryBand]` means “an EntryBand or None.” `None` means no selected band, not a zero-priced band. The convention also appears in missing quotes and unset timestamps. Callers must branch before accessing attributes. Skip labels explain rejected candidates; they are not independent authorization to buy.

`hourly_slice_budget` clips the named allowance to remaining credited market cost. `can_arm_hourly_slice` checks closure, uncertain execution, previous fills, remaining budget, and incompatible held tokens. Passing `buy_token=None` deliberately skips the other-leg comparison because the candidate is unknown ([buy/entry_skip.py:638](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L638), [buy/entry_skip.py:672](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L672)). Preliminary eligibility is therefore not the final decision.

The current `hourly_entry_final_gate` rechecks the selected slice against the live ask, expiry, closure, book evidence, favored underlying leg, and GUI consensus. Disabling the underlying gate fails this final check. It consumes supplied evidence rather than fetching fresh quotes ([buy/entry_skip.py:718](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/entry_skip.py#L718)). The execution wrapper calls its supplied `pre_submit` validator before posting ([buybothourly.py:3591](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L3591)).

<a id="section-36"></a>
## BTC evidence: last print against an opening reference

`BtcUnderlyingFeed` owns a locked tick ring, last observation, PTB cache, persistence path, and background thread. Hourly chooses `SOURCE_BINANCE` through `require_last_print_source`; TWAP feeds remain available for research but are refused as trading sources ([buy/btc_price.py:120](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L120), [buy/btc_price.py:193](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L193)).

PTB means price to beat: a reference near market opening. Capture selects the nearest buffered tick within 2,000 milliseconds. If Binance missed opening, it can backfill the open of a one-second BTCUSDT kline. Successful capture records provenance and skew, then caches the reference ([buy/btc_price.py:267](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L267)). PTB is not whatever price exists when the process first notices a market.

The comparison computes signed `live - ptb`: positive favors Up, negative Down. Exactly flat yields no favored side even with zero minimum edge. Missing PTB, missing live data, excessive age, and TWAP input have distinct failure reasons ([buy/btc_price.py:130](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L130), [buy/btc_price.py:379](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L379)). Despite the latter method's “memory-only” docstring, `ptb_record` can reach capture and REST backfill on a cache miss. Follow implementation dependencies when judging side effects.

Source: [buy/btc_price.py:245](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L245) (excerpt).

```python
def live_price(self, *, allow_stale: bool = False) -> Optional[float]:
    px, _src, age = self.live_quote()
    if px is None:
        return None
    if age is not None and age > LIVE_STALE_S and not allow_stale:
        return None
```

The bare `*` makes subsequent parameters keyword-only: callers must name `allow_stale` instead of supplying a positional boolean. `Optional[float]` permits an absent price. Freshness uses monotonic receive age, with a five-second threshold; incoming ticks also undergo source-time validation ([buy/btc_price.py:53](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L53), [buy/btc_price.py:480](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/btc_price.py#L480)).

The code calls this evidence an “oracle,” but last Binance print versus locally captured PTB is a decision signal. It is not confirmation of official resolution, nor an interchangeable substitute for the settlement source specified in market rules. A `resolution_url` field is metadata, not settlement evidence. Keep discovery, trading evidence, and official resolution separate.

<a id="section-37"></a>
## CLOB freshness includes quote shape

`ClobMarketBookFeed` owns token subscriptions, a daemon socket thread, locks, and quote caches. Its tuple layout is explicit ([buy/clob_book_ws.py:34](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L34)):

Source: [buy/clob_book_ws.py:34](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L34) (excerpt).

```python
# (bid, bid_size, ask, ask_size, mid)
Quote = Tuple[Optional[float], float, Optional[float], float, Optional[float]]
```

This is a typed tuple alias, not a `NamedTuple`: positions still matter. Prices are floating-point outcome prices, conventionally fractions of a dollar per share; sizes are shares. Missing ask is `None`; unavailable displayed size is zero. Neither says the asset costs zero dollars.

`quote` returns `None` after its allowed receive age, two seconds by default. Server timestamps reject implausibly old/future events; per-token ordering rejects older updates; socket generations prevent replaced connections from repopulating caches. Reconnection clears previous quotes ([buy/clob_book_ws.py:38](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L38), [buy/clob_book_ws.py:100](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L100), [buy/clob_book_ws.py:373](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L373)). Monotonic time measures elapsed age; wall time validates exchange timestamps.

Price-only events require care. Omitted sides can retain their previous price, but size survives only when that price is unchanged and the previous receipt is recent. Otherwise size becomes zero. Full snapshots replace sizes. Fresh price evidence is therefore not necessarily fresh executable depth ([buy/clob_book_ws.py:126](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L126), [buy/clob_book_ws.py:251](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/clob_book_ws.py#L251)). REST remains the fallback and confirmation path; neither transport reserves liquidity.

`book.best_from_levels` is the smaller parsing boundary. It accepts dictionary levels, rejects nonfinite values, requires `0 < price < 1` and positive size, then chooses highest bid or lowest ask. Empty usable input yields `(None, 0.0)`. It does not aggregate a ladder or validate a complete trading opportunity ([buy/book.py:14](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/book.py#L14)). The file notes that buy-script quote paths remain separately owned.

<a id="section-38"></a>
## Hedge helpers as state-transition interfaces

`hedge_gate` packages reusable policy without submitting anything. `hedge_persist_ready` consumes current qualification and a caller-owned arm timestamp, returning readiness, the next timestamp, and a reason. Failed qualification clears the arm; elapsed time alone is insufficient ([buy/hedge_gate.py:19](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L19)).

`HedgeIntent`, another `NamedTuple`, carries action, reason, optional proposed price, persistence state, and flags. `HedgeLadder` carries thresholds. These values separate evaluating a tick from saving state and executing an action ([buy/hedge_gate.py:117](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L117)). They are not signed orders. `hedge_market_tick` and its error parser translate exchange constraints into usable increments; a finer desired tick cannot override a coarser market minimum ([buy/hedge_gate.py:56](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L56)).

The current module also contains cost-basis and take-profit helpers imported by hourly. Older descriptions limited to historical hedge behavior are incomplete ([buy/hedge_gate.py:653](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/hedge_gate.py#L653), [buybothourly.py:86](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L86)). Execution details belong with exits; the interface distinction is between policy output and completed execution.

<a id="section-39"></a>
## SDK objects and precise money boundaries

The buyer imports `ClobClient`, `OrderArgs`, `MarketOrderArgs`, `OrderType`, and `PartialCreateOrderOptions` from `py_clob_client_v2`. These SDK representations are separate from market metadata, quotes, and policy records ([buybothourly.py:24](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L24)). Construction supplies token, price, share size, and side; options supply tick size and risk mode. Submission passes the signed object with FAK semantics: fill available eligible liquidity and cancel the remainder ([buybothourly.py:3543](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L3543), [buybothourly.py:3604](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L3604)).

Floats suit observed prices and comparisons. Money and order-grid constraints require deliberate decimal arithmetic. Hourly sizing uses `Decimal(str(value))`, hundredth-share quantization, and exact-cent maker checks. Converting through a string avoids importing a binary float's full approximation into Decimal. A reference-price share target can change the spending ceiling, still subject to caps; sizing is not universally budget divided by current ask ([buybothourly.py:2340](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L2340)). Decimal implements permitted increments; it does not choose policy.

The SDK owns signing and transport; the script retains execution bookkeeping. `ImmediateResponseClobClient` bypasses SDK transaction-hash polling so the script can perform its own finality checks ([buybothourly.py:106](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L106)). Builder signing and relayer packages form separate authentication/transaction boundaries; they are not discovery or quote engines ([buybothourly.py:39](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L39)).

<a id="section-40"></a>
## Depth telemetry measures hypothetical capacity

`depth_ladder` normalizes asks, sorts ascending, and walks visible size at or below a supplied limit for multiple dollar budgets. It reports hypothetical shares, notional, VWAP, and full/partial/zero capacity. Those labels describe a model, not an exchange response ([buy/depth_ladder.py:50](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/depth_ladder.py#L50), [buy/depth_ladder.py:130](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/depth_ladder.py#L130)). Price times shares consumes budget, potentially partway through a level.

Hourly's `emit_buy_depth_ladder` uses supplied asks or cached levels, attempts REST refresh if needed, and emits observations around attempts and fills. Its payload is telemetry, not durable order intent; failures do not block buying ([buybothourly.py:1585](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L1585)). Signed-order intent is assembled separately ([buybothourly.py:3555](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buybothourly.py#L3555)).

`DepthPathBuffer` bounds samples; `simulate_topup_path` accumulates available notional toward hypothetical targets, normally counting only samples marked `gates_ok`. That flag is supplied evidence, not a rerun of every entry rule ([buy/depth_ladder.py:285](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/depth_ladder.py#L285), [buy/depth_ladder.py:309](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/depth_ladder.py#L309)). Queue priority, competing takers, and replenishment within a sample are explicitly excluded. The implementation cannot identify the same resting order across successive samples, so repeated snapshots can overstate independent liquidity. It does not model latency, acceptance, fees, or exact-cent sizing. “Completed” means sampled arithmetic reached a target, not that topping up would succeed.

A second limitation is the difference between full depth and top-of-book fallback. `available_at_limit` sums normalized levels when available; otherwise it can estimate capacity from only the best ask and its displayed size. It returns a source label so analysis can distinguish those observations ([buy/depth_ladder.py:203](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/depth_ladder.py#L203)). Ten displayed shares at 0.90 describe nine dollars of visible notional, not ten dollars and not guaranteed execution. A missing deeper ladder cannot establish that no deeper liquidity exists. Likewise, a hypothetical VWAP says how the supplied levels would combine; it says nothing about whether they remain available when an order arrives.

<a id="section-41"></a>
## Sibling complement and mint boundaries

`complement_gate` represents a primary holding with frozen `ArmedMarket`, selects the other token, evaluates its book and underlying agreement, and computes capped share multiples using Decimal grid checks. Outcome helpers distinguish fills, cooldowns, and ambiguous-submission quarantine; dictionary updates require caller persistence ([buy/complement_gate.py:18](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/complement_gate.py#L18), [buy/complement_gate.py:166](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/complement_gate.py#L166), [buy/complement_gate.py:425](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/complement_gate.py#L425)).

`complement_clob` adapts `polymarket-client` to a ClobClient-like interface for a separate type-3 deposit wallet. This is duck typing: compatible methods and arguments rather than shared inheritance. It translates construction and normalizes responses while enforcing its wallet boundary ([buy/complement_clob.py:324](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/complement_clob.py#L324)). That sibling adapter does not redefine hourly's SDK contract.

- Mint-only `buy/chain.py`: `ChainReader` performs Polygon RPC prechecks for balances, outcome slots, and contract presence ([buy/chain.py:11](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/chain.py#L11)).
- Mint-only `buy/contracts.py`: calldata builders create approval/split calls and validate six-decimal amounts; they do not submit CLOB orders ([buy/contracts.py:61](https://github.com/joelntemuse24/poly-money-maker/blob/f5d7a0e5c80b8826e264f910f9f697ad0b628c67/buy/contracts.py#L61)).
<a id="part-v"></a>
# Part V - Operations, verification and sharp edges

<a id="section-42"></a>
## Operations are another state machine

There are three versions of "current" to distinguish: the files on disk, the configuration most recently accepted by the loop, and the code already loaded by the running Python process. A Git pull changes the first. Hot reload can change the second. A running interpreter does not replace its function definitions just because a source file changed underneath it.

The service manager is outside all three. `systemctl show` supplies process state and start time, while `systemctl is-enabled` supplies startup policy. An active service can have stale data, unresolved orders or a broken optional subsystem. A moving heartbeat demonstrates loop progress, not profitable trading or fresh exchange data. Diagnose the specific boundary that failed before choosing an intervention.

Read-only inspection can establish the current process and configuration without importing the bot:

```sh
systemctl show polybuybothourly.service   --property=ActiveState,SubState,ExecMainStartTimestamp,User
sha256sum buybothourly.py buy/hedge_gate.py strategy_buyhourly.json
```

These commands do not start or stop anything. Inspect settings as data, and keep private environment files out of terminal transcripts. A configuration key named `dry_run` does not make module import safe: the startup path still has side effects, and the selected file paths and process lock belong to startup rather than to an arbitrary test fixture.

The observed live/stopped table is in Part I. Siblings matter mainly as shared-code consumers:

| Program | Relationship to hourly | What not to transfer blindly |
|---|---|---|
| `buybot5m.py` | Related execution and recovery logic, shared entry/hedge helpers | Seconds-based windows and 5m-specific rails |
| `buybot.py` | Older 15m sibling with separate orchestration | Its entry and hedge defaults |
| `complementbot.py` | Separate account/other-leg workflow | Wallet identity, sizing and activation assumptions |
| `mintbot.py` | Split/merge or mint-oriented path | Its transaction model; hourly buys outcomes on CLOB |
| `pathlog.py` | Independent market recorder | Recording a market does not mean the trader bought it |

An inactive program can still be imported by a test or be affected by a shared helper change. That is why a narrow production change can require a wider regression run without authorizing the other services to run.

<a id="section-43"></a>
## CI updates files; it does not reload Python

**Continuous integration (CI)** is automated validation of a proposed change. The workflow at `.github/workflows/test.yml:3` runs on pull requests and main-branch pushes. It installs dependencies, compiles the listed scripts, and runs unittest discovery. Its Python version is explicitly 3.12 at line 15.

Compilation checks whether source is syntactically valid. It does not execute the module's top-level trading loop. Tests then exercise selected behaviors. A green job establishes that those checks passed in that dependency/runtime environment; it does not establish exchange availability, wallet reconciliation or expected profitability.

The deployment workflow is separate. `.github/workflows/deploy.yml:4` listens for selected main-branch path changes. Its remote script includes:

```sh
cd ~/poly-money-maker
git pull origin main
.venv/bin/python -m pip install -r requirements.txt --quiet
```

Those are lines 27-29. The following lines explicitly avoid restarting services. A merge that touches the listed production paths can therefore change VM files and installed packages while the existing process continues running older loaded code. Operators must understand that mixed state when comparing a traceback, source line and service start timestamp.

`TECHNICAL_DESIGN.md` is outside the deployment path filter. A change restricted to this document does not meet the workflow's listed deployment triggers. That is a property of this workflow snapshot, not a universal guarantee about future automation. Read the filter when changing deployment behavior.

The Sep 9 sync intentionally committed a non-secret live strategy snapshot for byte verification. The bot reads its runtime path, not a GitHub webpage or `.example` file. Keep the deployment manifest and the three-document split current rather than treating a historical design narrative as an instruction to paste old settings.

<a id="section-44"></a>
## Testing without constructing a trader

The test suite is built around small importable helpers and **abstract syntax tree (AST)** extraction. An AST is Python's parsed representation of the source. A test can select a `FunctionDef`, compile just that definition, and supply mocked dependencies. This avoids executing the file's environment loading, process lock, wallet clients and loop.

The trade-off is explicit. An extracted function does not automatically bring every global name it uses. A new call to depth telemetry may require a no-op telemetry stub in a test whose purpose is retry ordering. A missing stub is not proof that the production module lacks the function. Conversely, adding a stub must not bypass the behavior under test: a test about confirmation cannot replace confirmation with unconditional success and still claim to verify finality.

`tests/test_buy_fill_shapes.py` covers response normalization, fixed-point amounts, ambiguous results, sizing and accounting helpers. `tests/test_hourly_safety.py` checks pre-submit hooks, deadlines, persistence continuity and recovery barriers. `tests/test_hedge_persist.py` exercises decisions separately from network execution, including oracle and TP rules. `tests/test_btc_price.py` checks source selection and live-versus-reference behavior. Backtest tests verify parsers and arithmetic, not the truth of an assumed market outcome.

One useful test shape is to make the first attempt pass a gate, change the synthetic clock or book, and require the retry to stop before POST. That checks the reason for a final validator: an earlier approval must not remain valid forever. Another test simulates acceptance with a lost response, then confirms the same order on the next cycle and ensures its quantity and proceeds are not counted twice.

For accounting, test a cumulative sequence rather than only a single sale. Start with a confirmed acquisition. Feed a partial fill, a larger cumulative fill, the same fill again, and delayed exact-order recovery. Remaining quantity, remaining basis and lifetime debit should follow their intended meanings at each step. A test that only asserts the final displayed VWAP can miss a spend-cap error caused by mutating acquisition debit.

The ordinary command is:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

Run it in an isolated checkout. Direct temporary files and caches into that sandbox when permissions require it. Do not use the live working directory as a testing convenience. In particular, do not import an executable bot to inspect its globals, start a second dry-run process beside it, or install a new package into the shared production virtual environment.

PR #156's final CI passed after test-only fixtures were aligned to the synchronized VM code. Those fixture updates covered the hourly configuration, a new telemetry dependency and the presence of both TP and hedge sell calls. This tour changes documentation only. A future source change should rerun the relevant behavior tests and then the shared suite, not change the documented hashes until the source snapshot actually changes.

<a id="section-45"></a>
## The recorder and the limits of replay

`pathlog.py` records public books without placing orders. Its configured series include 5m, 15m and hourly at `pathlog.py:49`. The intended windows are the full 5m market, the final eight minutes of 15m, and the final twenty minutes of hourly. `POLL_S = 1.0` at line 62 is an intended loop delay, not a promise that every market has one observation each second.

`run_cycle` at `pathlog.py:400` discovers markets, selects those within their recording horizon, samples due markets using a worker pool, and appends successful ticks. The timestamp supplied to a sample and the completion time of its network requests need not be identical. Failed or delayed requests leave gaps. Resolution lookup is bounded separately so searching old unresolved markets does not consume the entire sampling cycle.

After the end time plus a grace period, the recorder may append a `resolved` event with `src="gamma"` at `pathlog.py:456`. That is a useful source label. It is not an on-chain redemption receipt for the trading wallet. A hypothetical hold-to-payout return can use a resolved market label, while realized cash P&L needs the wallet's actual acquisition and exit history.

The recorder prunes its files. `RETAIN_S` is fourteen days and `MAX_TICK_BYTES` is 400 MiB at `pathlog.py:74`. Pruning protects the machine but limits later research. Export a specific immutable sample before making a claim that depends on a period staying available. Keep each file's first/last timestamp, observation count, missing intervals and resolution source with the result.

Hourly pathlog is not an |live−PTB| tape: it stores CLOB top-of-book for the last twenty minutes, often a handful of ticks, with no Binance print. `underlying_research_buyhourly.jsonl` has `ptb_capture` plus sparse skip/fill rows — useful PTB alignment, not a 58m+2m edge path. `check_late_edge_bleed.py` reconstructs signed `live − ptb` from Binance 1s klines (same fetch as `check_reversal_features.py`) versus captured PTB, then writes `late_edge_bleed_hour` / `late_edge_bleed_summary` for a post-hour read. That script does not arm, skip, or hedge.

A sparse path cannot establish continuous eight-second agreement. If two qualifying quotes are six minutes apart, neither proves what happened between them. Carrying the earlier state across the gap can manufacture persistence, fills and stop-outs. Resetting at gaps is more honest about observation coverage, but does not mean the real bot had no opportunity. It means the dataset cannot answer that question.

The same distinction applies to depth telemetry. A sampled ladder can answer how much displayed quantity was available within a limit under a chosen walk model. It cannot know whether the quote would survive network delay, whether another taker would remove it first, or which fee-adjusted amount the exchange would settle. Label simulated fills as simulated even when the arithmetic is exact.

<a id="section-46"></a>
## Landmines worth remembering

### A successful request is not a completed financial event

HTTP success, order acceptance, matching, settled trade details, token balance movement and cash proceeds are successive kinds of evidence. They are not interchangeable status words. An order can be accepted before the matching or settlement view becomes readable. A balance endpoint can lag. Recovery should answer the exact order question before using broad account movement as an explanation.

### A zero baseline must remain distinguishable from missing data

Python's `a or b` selects `b` when `a` is zero. That is convenient for defaults and dangerous when zero is a valid prior proceeds value. Review recovery expressions for the semantic difference between `None` and `0`. An immutable pre-order baseline prevents a retry from treating already-accounted proceeds as the original starting point.

### The oracle threshold and the signed edge are different decisions

A $10 confidence band can say that a direction is not strong enough to name a favorite. The sign of a $5 difference still says which side of the reference the last print lies on. The current hourly hedge helper uses signed-edge behavior for the near-reference case rather than blindly interpreting "too small" as permission to sell either side. The toxic-book override remains a separate policy branch, and the retry must still validate the current quote and deadline.

### Persistence measures observations, not elapsed wall time alone

A timer needs a continuing qualifying condition. A stale cache plus a clock does not prove a market remained below a threshold. Entry persistence also shares state by condition and leg, while the required duration depends on the currently selected ask band. That is not identical to continuously occupying one named slice's band. Inspect the key and reset conditions before interpreting a log's elapsed seconds.

### Reference sizing is not a fixed debit or a hard aggregate share ceiling

The configured reference budget produces a target share quantity. Signed amount constraints and price improvement affect actual acquired shares and actual spend. A lifetime spend cap should use acquisition debit. The current helper instead reads `pnl_entry_cost`, which TP reduces; this can reopen apparent headroom after a sale. A per-order share rail is not automatically a cap on every token balance the account could hold after multiple fills.

### A held bag is pooled inventory

The two entry stamps explain which slices fired. They are not a lot ledger that reserves b15 shares for settlement. Half-TP can reduce the blended position, including inventory attributed economically to the lower-priced sleeve. "Hold blend" is an operator description; the code's actual sell allocation is what determines exposure.

### A trigger is not a fill guarantee

A 0.60 hedge threshold is not a guaranteed 0.60 exit. A 0.999 full-lock trigger is not a signed 0.999 floor when the chosen tick rounds the limit to 0.99. FAK can fill partially or not at all. Logs need to preserve both the decision quote and actual confirmed financials to explain a result.

### Acquisition debit and remaining basis answer different questions

Lifetime acquisition debit answers how much was spent entering the market. Remaining basis answers how much of that cost is assigned to unsold shares. Proceeds answer what sales actually returned. Reducing the first to repair the second can make both spend limits and final P&L wrong. The current patch snapshots pre-sell cost to prevent repeated reduction across a clean single-attempt callback and final reconciliation, but still writes remaining cost into `pnl_entry_cost`. A later retry can overwrite that snapshot with already-reduced cost; the exit chapter follows that limitation. Spend-cap and final-P&L readers also use that field. The snapshot repair is therefore not a complete separation of lifetime debit from remaining basis. Read the actual writers and readers before interpreting reported profit.

### A source hash is narrower than a deployment fingerprint

The footer proves which bytes were read for this tour. It does not identify every package version, environment choice, external service response or historical JSON record. Record the source hash, effective configuration and process start together when reconstructing behavior. A line reference is reliable against this snapshot; later edits can move the same function.

<a id="section-47"></a>
## Glossary

| Term | Meaning in this system |
|---|---|
| Acquisition debit | Total cost attributed to buys in a market; used for spend and final accounting |
| Ask / bid | Lowest visible offer to sell / highest visible offer to buy |
| Basis | Acquisition cost allocated to the shares still held |
| Book consensus | Required relationships between both outcome books and derived display prices |
| CLOB | Central limit order book where outcome-token orders match |
| Condition ID | Identifier for the market condition shared by its outcome tokens |
| Decimal | Python decimal arithmetic used where amount precision and rounding matter |
| FAK | Fill-And-Kill: match immediately within a limit, cancel the unfilled remainder |
| Finality | Evidence that a financial operation is settled rather than merely accepted or matched |
| Funder / proxy | Account holding the collateral and outcome tokens used by the strategy |
| Future | Handle for asynchronous work whose result may not yet be available |
| Gamma | Market catalog and metadata service |
| Hedge | Here, usually sale of held shares to reduce exposure |
| Hot reload | Re-reading selected configuration values while the process keeps running |
| Idempotent | Repeating the same accounted observation does not apply it twice |
| Keyword-only | A Python argument that must be named at the call site, after `*` in a signature |
| Leg | One outcome token, Up or Down |
| Limit | Worst price the signed order permits; distinct from its triggering quote |
| Maker/taker amounts | Encoded assets offered and received in a signed exchange order |
| NamedTuple / dataclass | Python constructs for records with named fields |
| Oracle gate | Comparison used by the strategy; not itself a settlement receipt |
| PTB | Price To Beat, the window-open reference |
| Relayer | Service that submits a signed wallet transaction to the chain |
| REST | Request/response HTTP interface, contrasted here with a persistent WebSocket stream |
| SDK | Library implementing part of an external protocol's client behavior |
| Share target | Desired share quantity derived from reference budget and reference price |
| Slice | Named entry opportunity with its own eligibility and spent/fill stamp |
| Tick | Permitted price increment |
| TP | Take-profit sale, partial or full-lock |
| TTM | Time to maturity: remaining time before the market window ends |
| Uncertain intent | Persisted unresolved execution question about a specific submitted order |
| VWAP | Volume-weighted average price: cost or proceeds divided by the corresponding shares |
| WebSocket | Persistent connection carrying streamed market updates |
| Write-ahead | Persist intent and baseline before the external action can occur |

<a id="section-48"></a>
## Source snapshot

GitHub base: `f5d7a0e5c80b8826e264f910f9f697ad0b628c67`, the merge of PR #156. The three files below matched the live VM byte-for-byte when read on 2026-09-09. Service status was inspected read-only. No live file, environment file, order or service was changed while writing this tour.

```text
99266a0f1222095af9f4ea9f4c4470ceb3b5452b8ae078e0573c83b770d0cfff  buybothourly.py
36db2ed1e5923e0eb3534e6e07a48c1609d9bd0773ee360b08eac392626f8283  buy/hedge_gate.py
bebb505e1140fced394a1cccb90bbc91f80d0ccf880bc489837cec476d6897b9  strategy_buyhourly.json
```
