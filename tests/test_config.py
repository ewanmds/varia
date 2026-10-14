from pathlib import Path
from arb.config import load_config

ROOT = Path(__file__).resolve().parents[1]

def test_load_default_config():
    g, markets = load_config(ROOT / "config.toml")
    assert g.var_cdp_port == 9223
    assert len(markets) == 6
    oil = [m for m in markets if m.var_asset == "USOILP"][0]
    assert oil.basis_mode == "ema" and oil.roll_block is True
    spx = [m for m in markets if m.var_asset == "US500S"][0]
    assert spx.fill_usd == 500 and spx.min_edge_bps == 1.5 and spx.max_position_usd == 500
    assert spx.max_delta_usd == 750

def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[global]\nfoo = 1\n')
    import pytest
    with pytest.raises(ValueError):
        load_config(p)
