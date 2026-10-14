from arb.hl_client import parse_xyz_meta, effective_maker_fee_bps, parse_order_status

def test_parse_meta():
    meta = {"universe": [{"name": "xyz:SILVER", "szDecimals": 2, "maxLeverage": 25, "growthMode": "enabled"},
                         {"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25}]}
    m = parse_xyz_meta(meta)
    assert m["xyz:SILVER"].sz_decimals == 2 and m["xyz:SILVER"].growth is True
    assert m["xyz:GOLD"].growth is False

def test_maker_fee():
    assert effective_maker_fee_bps(0.00015, True) == 0.3
    assert effective_maker_fee_bps(0.00015, False) == 3.0

def test_parse_order_status():
    ok = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}}}
    assert parse_order_status(ok) == {"oid": 7, "filled_sz": 0.0, "avg_px": None, "error": None}
    fill = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 8, "totalSz": "1.5", "avgPx": "67.1"}}]}}}
    assert parse_order_status(fill)["filled_sz"] == 1.5
    err = {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only order would have immediately matched"}]}}}
    assert parse_order_status(err)["error"].startswith("Post only")
    bad = {"status": "err", "response": "boom"}
    assert parse_order_status(bad)["error"] == "boom"
