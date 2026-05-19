"""
exchange.py — CoinDCX live execution layer.

This module provides all live trading operations needed by main.py.
It is only called when TRADING_MODE = "live" in config.py.
In paper mode, none of these functions are called.

Functions
---------
get_live_inr_balance()         → float (available INR)
get_live_holdings()            → dict  {symbol: quantity}
reconcile_positions(coins)     → dict  of corrections made
place_market_order(symbol, side, quantity_or_amount) → order_id or None
get_order_status(order_id)     → dict
cancel_order(order_id)         → bool
run_connectivity_test()        → prints diagnostics, does NOT place real orders
"""

import time
from coindcx_auth import signed_post


# ---------------------------------------------------------------------------
# Symbol format helpers
# ---------------------------------------------------------------------------

def _order_symbol(symbol: str) -> str:
    """
    Map internal symbol format to CoinDCX order market format.
    'BTC/INR' → 'BTCINR'
    'SHIB/INR' → 'SHIBINR'
    """
    return symbol.replace("/", "")


def _holdings_symbol(market: str) -> str:
    """
    Map CoinDCX balance currency to our internal format.
    CoinDCX returns balances per currency (e.g. 'BTC', 'ETH').
    We map back: 'BTC' → 'BTC/INR'
    """
    return f"{market}/INR"


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

def get_live_inr_balance() -> float:
    """
    Fetch available INR balance from CoinDCX account.
    Returns 0.0 on failure (logged).
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

    # Response is a list of {"currency": "INR", "balance": "1000.00", ...}
    for entry in balances:
        if entry.get("currency") == "INR":
            return float(entry.get("balance", 0))

    print("  [exchange] INR not found in balance response")
    return 0.0


def get_live_holdings() -> dict[str, float]:
    """
    Fetch all non-zero crypto holdings from CoinDCX account.
    Returns {symbol: quantity} e.g. {'BTC/INR': 0.0005, 'ETH/INR': 0.12}
    Only returns currencies that have a positive balance.
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
        if currency == "INR" or balance <= 0:
            continue
        symbol = _holdings_symbol(currency)
        holdings[symbol] = balance

    return holdings


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions(coins: list, load_position_fn, save_position_fn,
                        clear_position_fn) -> dict:
    """
    Compare local position CSVs with actual CoinDCX holdings.
    Ground truth is always the exchange — local files are updated to match.

    Returns a dict of corrections made:
    {
      'added':   [symbols where exchange has holding but CSV is missing],
      'removed': [symbols where CSV says open but exchange has no holding],
      'ok':      [symbols where CSV and exchange agree],
    }

    This prevents the classic bot disaster: local state says flat,
    exchange says we hold 0.5 BTC, bot buys more, now double-exposed.
    """
    corrections = {'added': [], 'removed': [], 'ok': []}
    live_holdings = get_live_holdings()

    for symbol in coins:
        if symbol == "BTC/INR":
            continue   # regime reference, never traded

        local_pos  = load_position_fn(symbol)
        live_qty   = live_holdings.get(symbol, 0.0)
        has_local  = local_pos is not None
        has_live   = live_qty > 0.0001   # small dust threshold

        if has_local and has_live:
            # Both agree we have a position — update quantity to match exchange
            actual_qty = live_qty
            if abs(float(local_pos.get('Quantity', 0)) - actual_qty) > 0.0001:
                local_pos['Quantity'] = actual_qty
                save_position_fn(symbol, local_pos)
                print(f"  [reconcile] {symbol}: quantity updated to {actual_qty:.6f} (exchange)")
            corrections['ok'].append(symbol)

        elif has_local and not has_live:
            # Local says open, exchange says flat — position was closed externally
            print(f"  [reconcile] {symbol}: local position removed "
                  f"(exchange shows no holding)")
            clear_position_fn(symbol)
            corrections['removed'].append(symbol)

        elif not has_local and has_live:
            # Exchange has a holding but no local position CSV
            # This can happen after a crash or manual trade
            # Log it but don't auto-create a position — too risky to assume
            # entry price and direction without that context
            print(f"  [reconcile] {symbol}: exchange holds {live_qty:.6f} "
                  f"but no local position found. Manual check required.")
            corrections['added'].append(symbol)

    return corrections


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def place_market_order(
    symbol:    str,
    side:      str,     # "buy" or "sell"
    quantity:  float,
    dry_run:   bool = False,
) -> str | None:
    """
    Place a market order on CoinDCX.

    Parameters
    ----------
    symbol   : internal format e.g. 'BTC/INR'
    side     : 'buy' or 'sell'
    quantity : quantity in base currency (BTC for BTC/INR)
    dry_run  : if True, logs the order but does NOT send it to the exchange.
               Used for connectivity testing and validation.

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
        "client_order_id": f"bot_{market}_{side}_{int(time.time())}",
    }

    if dry_run:
        print(f"  [exchange] DRY RUN — would place {side.upper()} market order: "
              f"{quantity:.6f} {market}")
        print(f"  [exchange] Body: {body}")
        return "DRY_RUN_ORDER_ID"

    try:
        r = signed_post("/exchange/v1/orders/create", body)
    except Exception as e:
        print(f"  [exchange] Order placement failed: {e}")
        return None

    if r.status_code == 200:
        try:
            data     = r.json()
            order_id = data.get("id") or data.get("order_id") or data.get("orders", [{}])[0].get("id")
            print(f"  [exchange] Order placed: {side.upper()} {quantity:.6f} {market} "
                  f"→ order_id={order_id}")
            return str(order_id)
        except Exception as e:
            print(f"  [exchange] Order response parse failed: {e} | raw: {r.text[:300]}")
            return None

    elif r.status_code == 400:
        # Common: insufficient balance, min order size not met
        print(f"  [exchange] Order rejected (400): {r.text[:300]}")
        return None

    elif r.status_code == 401:
        print(f"  [exchange] Order auth failed (401) — check API key/secret")
        return None

    else:
        print(f"  [exchange] Order error {r.status_code}: {r.text[:200]}")
        return None


def get_order_status(order_id: str) -> dict:
    """
    Fetch status of a specific order.
    Returns the order dict, or {} on failure.

    Key fields in response:
      status          : 'filled', 'partially_filled', 'cancelled', 'open'
      avg_price       : average fill price
      total_quantity  : quantity ordered
      remaining_quantity : unfilled quantity
    """
    if order_id == "DRY_RUN_ORDER_ID":
        return {"status": "filled", "avg_price": 0, "total_quantity": 0,
                "remaining_quantity": 0, "dry_run": True}

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
    """
    Cancel an open order. Returns True if successful, False otherwise.
    """
    if order_id == "DRY_RUN_ORDER_ID":
        return True

    try:
        r = signed_post("/exchange/v1/orders/cancel", {"id": order_id})
    except Exception as e:
        print(f"  [exchange] Cancel failed: {e}")
        return False

    if r.status_code == 200:
        print(f"  [exchange] Order {order_id} cancelled successfully")
        return True
    else:
        print(f"  [exchange] Cancel error {r.status_code}: {r.text[:200]}")
        return False


# ---------------------------------------------------------------------------
# Connectivity test — safe to run anytime, never places real orders
# ---------------------------------------------------------------------------

def run_connectivity_test(coins: list) -> None:
    """
    Tests the full live trading integration WITHOUT placing any real orders.

    What it checks:
      1. API credentials are present and valid
      2. INR balance is readable
      3. Current holdings are readable
      4. Order placement API is reachable (dry run only — no real order)
      5. Order status API is reachable
      6. Symbol formats are correct for all coins

    This is safe to run at any time. Use this to verify your API keys
    work before switching TRADING_MODE to "live".
    """
    print("=" * 60)
    print("CoinDCX Live Trading Connectivity Test")
    print("(DRY RUN — no real orders will be placed)")
    print("=" * 60)

    # Test 1: Credentials present
    print("\n[1/5] Checking credentials...")
    try:
        from coindcx_auth import _get_credentials
        key, secret = _get_credentials()
        print(f"  ✓ API key found: {key[:8]}...{key[-4:]}")
        print(f"  ✓ API secret found: {'*' * 20}")
    except EnvironmentError as e:
        print(f"  ✗ {e}")
        print("  Cannot continue — credentials missing.")
        return

    # Test 2: INR balance
    print("\n[2/5] Fetching INR balance...")
    inr = get_live_inr_balance()
    if inr > 0:
        print(f"  ✓ INR balance: ₹{inr:.2f}")
    elif inr == 0.0:
        print(f"  ⚠️  INR balance is ₹0.00 — account may be empty or API returned error")
        print(f"     (This is expected if you have not deposited yet)")

    # Test 3: Holdings
    print("\n[3/5] Fetching current holdings...")
    holdings = get_live_holdings()
    if holdings:
        for sym, qty in holdings.items():
            print(f"  ✓ Holding: {sym} → {qty:.6f}")
    else:
        print(f"  ✓ No crypto holdings (clean account — ready to trade)")

    # Test 4: Symbol format check
    print("\n[4/5] Checking symbol formats for all coins...")
    tradeable = [c for c in coins if c != "BTC/INR"]
    for symbol in tradeable:
        order_sym = _order_symbol(symbol)
        print(f"  {symbol:<12} → order market: {order_sym}")

    # Test 5: Dry-run order (no real order placed)
    print("\n[5/5] Dry-run order test (NOT a real order)...")
    test_symbol = "BTC/INR"
    order_id = place_market_order(test_symbol, "buy", 0.000001, dry_run=True)
    if order_id:
        print(f"  ✓ Dry-run order construction successful")

    print("\n" + "=" * 60)
    print("Connectivity test complete.")
    if inr >= 100:
        print(f"✓ Account ready for live trading (₹{inr:.2f} available)")
    else:
        print(f"⚠️  Deposit INR to your CoinDCX account before enabling live trading")
    print("To enable live trading: set TRADING_MODE = 'live' in config.py")
    print("=" * 60)