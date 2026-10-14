from arb.keystore import KeyStore


def test_roundtrip(tmp_path):
    ks = KeyStore(tmp_path / "s.enc", "motdepasse")
    key = "0x" + "11" * 32
    from eth_account import Account
    addr = Account.from_key(key).address.lower()
    assert ks.save_agent("0x" + "ab" * 20, key, addr)
    ks2 = KeyStore(tmp_path / "s.enc", "motdepasse")
    assert ks2.load_agents()["0x" + "ab" * 20]["key"] == key
    ks3 = KeyStore(tmp_path / "s.enc", "mauvais")
    # vague finale R3 : un mauvais mot de passe lève (le bot refuse de démarrer) au lieu de {}.
    import pytest
    from arb.keystore import KeyStoreError
    with pytest.raises(KeyStoreError):
        ks3.load_agents()
