import pytest

from arb.auth import AGENT_NAME, AuthState, _build_agent_approve_action, approve_submit
from arb.keystore import KeyStore


def test_build_agent_approve_action_uses_botarb_name():
    action = _build_agent_approve_action(
        agent_address="0x" + "ab" * 20, agent_name=AGENT_NAME, nonce=1,
    )
    assert action["agentName"] == "BOTARB"
    assert action["type"] == "approveAgent"


def test_approve_submit_unknown_nonce_raises_runtime_error(tmp_path):
    keystore = KeyStore(tmp_path / "s.enc", "motdepasse")
    state = AuthState(keystore)
    with pytest.raises(RuntimeError):
        approve_submit(state, "0x" + "ab" * 20, 999999, "0x" + "11" * 65)
