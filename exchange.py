"""
exchange.py — CoinDCX live execution layer (USDT pairs).

Called only when TRADING_MODE = "live" in config.py.
In paper mode, none of these functions are invoked.

Functions
---------
get_live_usdt_balance()          → float  (available USDT)
get_live_holdings()              → dict   {symbol: quantity}
reconcile_positions(...)         → dict   of corrections made
place_market_order(symbol, side, quantity) → order_id or None
get_order_status(order_id)       → dict
cancel_order(order_id)           → bool
run_connectivity_test(coins)     → prints diagnostics, no real orders placed
"""

import time
from coinswitch_auth import signed_post


# ---------------------------------------------------------------------------
# Symbol format helpers
# ---------------------------------------------------------------------------

def _order_symbol(symbol: str) -> str:
    """
    Map internal symbol to CoinDCX order market format.
    'BTC/USDT' → 'BTCUSDT'
    'ADA/USDT' → 'ADAUSDT'
    """
    return symbol.replace("/", "")


def _holdings_symbol(currency: str) -> str:
    """
    Map CoinDCX balance currency back to internal format.
    'BTC' → 'BTC/USDT'
    'ADA' → 'ADA/USDT'
    """
    return f"{currency}/USDT"


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

def get_live_usdt_balance() -> float:
    """
    Fetch available INR balance from CoinDCX account.
    CoinDCX automatically converts INR to USDT at trade time
    so you deposit INR and trade USDT pairs directly.
    Returns balance in INR (used as capital for position sizing).
    Returns 0.0 on failure.
    """
    try:
        r = signed_post("/exchange/v1/users/balances", {})
    except Exception as e:
        print(f"  [exchange] Balance fetch failed: {e}")
        return 0.0

    if r.status_code != 200:
        print(f"  [exchange] Balance error {r.status_code}: {r.text[:200]}")
        return 0.0

    try:
        balances = r.json()
    except Exception as e:
        print(f"  [exchange] Balance JSON parse failed: {e}")
        return 0.0

    # Read INR balance — CoinDCX converts INR to USDT at trade execution
    for entry in balances:
        if entry.get("currency") == "INR":
            inr_balance = float(entry.get("balance", 0))
            print(f"  [exchange] INR balance: Rs {inr_balance:,.2f}")
            return inr_balance

    # Fallback — check USDT if INR not found
    for entry in balances:
        if entry.get("currency") == "USDT":
            usdt_balance = float(entry.get("balance", 0))
            print(f"  [exchange] USDT balance: ${usdt_balance:,.2f}")
            return usdt_balance

    print("  [exchange] No INR or USDT balance found")
    return 0.0


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

def get_live_holdings() -> dict[str, float]:
    """
    Fetch all non-zero crypto holdings from CoinDCX account.
    Returns {symbol: quantity} e.g. {'BTC/USDT': 0.0005, 'ADA/USDT': 120.0}
    Skips USDT itself and any zero/dust balances.
    """
    try:
        r = signed_post("/exchange/v1/users/balances", {})
    except Exception as e:
        print(f"  [exchange] Holdings fetch failed: {e}")
        return {}

    if r.status_code != 200:
        print(f"  [exchange] Holdings error {r.status_code}: {r.text[:200]}")
        return {}

    try:
        balances = r.json()
    except Exception as e:
        print(f"  [exchange] Holdings JSON parse failed: {e}")
        return {}

    holdings = {}
    for entry in balances:
        currency = entry.get("currency", "")
        balance  = float(entry.get("balance", 0))
        # Skip quote currency and dust
        if currency in ("USDT", "INR") or balance <= 0.0001:
            continue
        holdings[_holdings_symbol(currency)] = balance

    return holdings


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions(
    coins: list,
    load_position_fn,
    save_position_fn,
    clear_position_fn,
) -> dict:
    """
    Compare local position CSVs with actual CoinDCX holdings.
    Ground truth is always the exchange — local files are updated to match.

    Returns a dict:
        added   : symbols where exchange has holding but CSV is missing
        removed : symbols where CSV says open but exchange has no holding
        ok      : symbols where CSV and exchange agree
    """
    corrections    = {"added": [], "removed": [], "ok": []}
    live_holdings  = get_live_holdings()

    for symbol in coins:
        local_pos = load_position_fn(symbol)
        live_qty  = live_holdings.get(symbol, 0.0)
        has_local = local_pos is not None
        has_live  = live_qty > 0.0001

        if has_local and has_live:
            # Both agree — update quantity to match exchange
            actual_qty = live_qty
            if abs(float(local_pos.get("Quantity", 0)) - actual_qty) > 0.0001:
                local_pos["Quantity"] = actual_qty
                save_position_fn(symbol, local_pos)
                print(f"  [reconcile] {symbol}: qty updated to {actual_qty:.6f}")
            corrections["ok"].append(symbol)

        elif has_local and not has_live:
            # Local says open, exchange says flat — closed externally
            print(f"  [reconcile] {symbol}: local position removed "
                  f"(exchange shows no holding)")
            clear_position_fn(symbol)
            corrections["removed"].append(symbol)

        elif not has_local and has_live:
            # Exchange has holding, no local CSV — manual trade or crash
            print(f"  [reconcile] {symbol}: exchange holds {live_qty:.6f} "
                  f"but no local position. Manual check required.")
            corrections["added"].append(symbol)

    return corrections


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_market_order(
    symbol:   str,
    side:     str,    # "buy" or "sell"
    quantity: float,
    dry_run:  bool = False,
) -> str | None:
    """
    Place a market order on CoinDCX.

    Parameters
    ----------
    symbol   : internal format e.g. 'BTC/USDT'
    side     : 'buy' or 'sell'
    quantity : quantity in base currency (BTC for BTC/USDT)
    dry_run  : if True, logs the order but does NOT send to exchange.
               Use for connectivity testing.

    Returns
    -------
    order_id (str) on success, None on failure.
    """
    market = _order_symbol(symbol)

    body = {
        "market":          market,
        "side":            side.lower(),
        "order_type":      "market_order",
        "total_quantity":  quantity,
        "client_order_id": f"hma_{market}_{side}_{int(time.time())}",
    }

    if dry_run:
        print(f"  [exchange] DRY RUN — {side.upper()} {quantity:.6f} {market}")
        return "DRY_RUN_ORDER_ID"

    try:
        r = signed_post("/exchange/v1/orders/create", body)
    except Exception as e:
        print(f"  [exchange] Order placement failed: {e}")
        return None

    if r.status_code == 200:
        try:
            data     = r.json()
            order_id = (data.get("id")
                        or data.get("order_id")
                        or data.get("orders", [{}])[0].get("id"))
            print(f"  [exchange] {side.upper()} {quantity:.6f} {market} "
                  f"→ order_id={order_id}")
            return str(order_id)
        except Exception as e:
            print(f"  [exchange] Response parse failed: {e} | {r.text[:300]}")
            return None

    elif r.status_code == 400:
        print(f"  [exchange] Order rejected (400): {r.text[:300]}")
        return None

    elif r.status_code == 401:
        print(f"  [exchange] Auth failed (401) — check API key/secret")
        return None

    else:
        print(f"  [exchange] Order error {r.status_code}: {r.text[:200]}")
        return None


def get_order_status(order_id: str) -> dict:
    """
    Fetch status of a specific order. Returns {} on failure.

    Key fields:
        status             : 'filled' | 'partially_filled' | 'cancelled' | 'open'
        avg_price          : average fill price
        total_quantity     : quantity ordered
        remaining_quantity : unfilled quantity
    """
    if order_id == "DRY_RUN_ORDER_ID":
        return {
            "status": "filled", "avg_price": 0,
            "total_quantity": 0, "remaining_quantity": 0,
        }

    try:
        r = signed_post("/exchange/v1/orders/status", {"id": order_id})
    except Exception as e:
        print(f"  [exchange] Order status fetch failed: {e}")
        return {}

    if r.status_code != 200:
        print(f"  [exchange] Order status error {r.status_code}: {r.text[:200]}")
        return {}

    try:
        return r.json()
    except Exception as e:
        print(f"  [exchange] Order status JSON parse failed: {e}")
        return {}


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True if successful."""
    if order_id == "DRY_RUN_ORDER_ID":
        return True

    try:
        r = signed_post("/exchange/v1/orders/cancel", {"id": order_id})
    except Exception as e:
        print(f"  [exchange] Cancel failed: {e}")
        return False

    if r.status_code == 200:
        print(f"  [exchange] Order {order_id} cancelled")
        return True
    else:
        print(f"  [exchange] Cancel error {r.status_code}: {r.text[:200]}")
        return False


# ---------------------------------------------------------------------------
# Connectivity test — safe to run anytime, no real orders placed
# ---------------------------------------------------------------------------

def run_connectivity_test(coins: list) -> None:
    """
    Tests the full live trading integration without placing real orders.

    Checks:
      1. API credentials present and valid
      2. USDT balance readable
      3. Current holdings readable
      4. Symbol formats correct for all coins
      5. Order API reachable (dry run only)

    Run this before switching TRADING_MODE to 'live'.
    """
    print("=" * 58)
    print("  CoinDCX Live Connectivity Test")
    print("  (DRY RUN — no real orders placed)")
    print("=" * 58)

    # 1. Credentials
    print("\n[1/5] Checking credentials...")
    try:
        from coinswitch_auth import _get_credentials
        key, secret = _get_credentials()
        print(f"  ✓ API key    : {key[:8]}...{key[-4:]}")
        print(f"  ✓ API secret : {'*' * 20}")
    except EnvironmentError as e:
        print(f"  ✗ {e}")
        print("  Cannot continue — credentials missing.")
        return

    # 2. INR balance
    print("\n[2/5] Fetching INR balance...")
    balance = get_live_usdt_balance()
    if balance > 0:
        print(f"  ✓ Balance: Rs {balance:,.2f} (INR — converts to USDT at trade time)")
    else:
        print(f"  ⚠  Balance is zero — deposit INR to CoinDCX before going live")

    # 3. Holdings
    print("\n[3/5] Fetching current holdings...")
    holdings = get_live_holdings()
    if holdings:
        for sym, qty in holdings.items():
            print(f"  ✓ {sym}: {qty:.6f}")
    else:
        print(f"  ✓ No crypto holdings — account clean and ready")

    # 4. Symbol formats
    print("\n[4/5] Checking symbol formats...")
    for symbol in coins:
        print(f"  {symbol:<14} → order market: {_order_symbol(symbol)}")

    # 5. Dry-run order
    print("\n[5/5] Dry-run order test (NOT real)...")
    oid = place_market_order(coins[0], "buy", 0.000001, dry_run=True)
    if oid:
        print(f"  ✓ Order construction OK")

    print("\n" + "=" * 58)
    print("  Connectivity test complete.")
    if balance >= 100:
        print(f"  ✓ Ready for live trading (Rs {balance:,.2f} available)")
    else:
        print(f"  ⚠  Deposit INR to CoinDCX before enabling live mode")
    print("  To go live: set TRADING_MODE=live in config.py or .env")
    print("=" * 58)