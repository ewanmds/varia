"""Wallet auth and Hyperliquid agent-wallet approval flow.

Ported from the bot HIP-4 (`/home/manao/bot hip-4/server.py`), stripped down
to wallet auth + agent approval only (no BotManager, no sessions, no
strategy). Nothing is imported from the other project; FastAPI stays out of
this module entirely — callers (the arb-varia server) translate the
`RuntimeError`s raised here into HTTP errors.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

from arb.keystore import KeyStore, normalize_address

BASE_URL = "https://api.hyperliquid.xyz"
AGENT_NAME = "BOTARB"
SIGNATURE_CHAIN_ID = "0x66eee"

AUTH_CHALLENGE_TTL_SEC = 300
AUTH_SESSION_TTL_SEC = 604800
AGENT_APPROVE_CHALLENGE_TTL_SEC = 300


# ---------------------------------------------------------------------------
# EIP-712 / signing helpers (copied, standalone)
# ---------------------------------------------------------------------------

def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _address_from_private_key(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    try:
        from eth_account import Account
        return normalize_address(Account.from_key(raw).address)
    except Exception:
        return ""


def _build_agent_approve_action(*, agent_address: str, agent_name: str, nonce: int) -> Dict[str, Any]:
    return {
        "type": "approveAgent",
        "agentAddress": normalize_address(agent_address),
        "agentName": str(agent_name or AGENT_NAME).strip() or AGENT_NAME,
        "nonce": int(nonce),
        "signatureChainId": SIGNATURE_CHAIN_ID,
        "hyperliquidChain": "Mainnet",
    }


def _build_agent_approve_typed_data(action: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int(str(action.get("signatureChainId", SIGNATURE_CHAIN_ID)), 16),
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            "HyperliquidTransaction:ApproveAgent": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "agentAddress", "type": "address"},
                {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "HyperliquidTransaction:ApproveAgent",
        "message": action,
    }


def _parse_signature_hex(signature_hex: str) -> Dict[str, Any]:
    raw = str(signature_hex or "").strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 130:
        raise RuntimeError("Invalid signature format.")
    try:
        sig = bytes.fromhex(raw)
    except ValueError:
        raise RuntimeError("Invalid signature hex.")
    v = sig[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        raise RuntimeError("Invalid signature recovery id.")
    return {"r": "0x" + sig[:32].hex(), "s": "0x" + sig[32:64].hex(), "v": int(v)}


def _recover_typed_data_signer(typed_data: Dict[str, Any], signature_hex: str) -> str:
    from eth_account import Account
    try:
        from eth_account.messages import encode_structured_data
        encoded = encode_structured_data(typed_data)
    except ImportError:
        from eth_account.messages import encode_typed_data
        encoded = encode_typed_data(full_message=typed_data)
    recovered = Account.recover_message(encoded, signature=signature_hex)
    return normalize_address(str(recovered))


def _hl_info_post_raw(payload: Dict[str, Any], timeout: float = 5.0) -> Any:
    target = BASE_URL.rstrip("/") + "/info"
    req = urllib.request.Request(
        target, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_exchange_action(payload: Dict[str, Any], max_retries: int = 4) -> Any:
    target = BASE_URL.rstrip("/") + "/exchange"
    body = json.dumps(payload).encode("utf-8")
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            target, data=body, method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries - 1:
                time.sleep(2.0 * (attempt + 1))
                last_exc = exc
                continue
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
            raise RuntimeError(f"exchange HTTP {exc.code}: {detail}")
        except Exception as exc:
            raise RuntimeError(f"exchange request failed: {exc}")
    raise RuntimeError(f"exchange HTTP 429: {last_exc}")


def _live_query_extra_agents(account: str) -> Optional[List[Dict[str, Any]]]:
    if not account:
        return None
    try:
        raw = _hl_info_post_raw({"type": "extraAgents", "user": account})
        if isinstance(raw, list):
            return [a for a in raw if isinstance(a, dict)]
        return []
    except Exception as exc:
        print(f"[agent-status] live query failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class AuthState:
    def __init__(self, keystore: KeyStore) -> None:
        self._lock = Lock()
        self.keystore = keystore
        self._auth_challenges: Dict[str, Dict[str, Any]] = {}
        self._auth_sessions: Dict[str, Dict[str, Any]] = {}
        self._approve_challenges: Dict[str, Dict[str, Any]] = {}
        self._last_nonce: Dict[str, int] = {}
        # agent keys in memory: account -> {key, agent_address, agent_name}
        self._agents: Dict[str, Dict[str, str]] = self.keystore.load_agents()

    # -- wallet auth -----------------------------------------------------

    def create_wallet_auth_challenge(self, account_address: str) -> Dict[str, Any]:
        norm = normalize_address(account_address)
        if not norm:
            raise RuntimeError("Invalid account address.")
        now = time.time()
        nonce = secrets.token_hex(16)
        message = (
            "Bot Arb Varia Wallet Authentication\n"
            f"Account: {norm}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {_utc_iso(now)}\n"
            "Sign this message to prove wallet ownership. No transaction will be sent."
        )
        with self._lock:
            self._cleanup()
            self._auth_challenges[nonce] = {
                "account_address": norm, "message": message,
                "expires_at": now + AUTH_CHALLENGE_TTL_SEC,
            }
        return {"account_address": norm, "nonce": nonce, "message": message}

    def verify_wallet_auth(self, account_address: str, nonce: str, signature: str) -> Dict[str, Any]:
        norm = normalize_address(account_address)
        if not norm:
            raise RuntimeError("Invalid account address.")
        with self._lock:
            challenge = self._auth_challenges.pop(nonce, None)
        if challenge is None or float(challenge.get("expires_at", 0)) <= time.time():
            raise RuntimeError("Challenge expired or invalid.")
        if normalize_address(str(challenge.get("account_address", ""))) != norm:
            raise RuntimeError("Challenge/account mismatch.")
        from eth_account import Account
        from eth_account.messages import encode_defunct
        recovered = Account.recover_message(
            encode_defunct(text=str(challenge.get("message", ""))), signature=signature,
        )
        if normalize_address(str(recovered)) != norm:
            raise RuntimeError("Signature does not match account address.")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._auth_sessions[token] = {
                "account_address": norm, "expires_at": time.time() + AUTH_SESSION_TTL_SEC,
            }
        return {"authenticated": True, "auth_token": token, "account_address": norm}

    def require_auth(self, token: str, account_address: str) -> None:
        norm = normalize_address(account_address)
        with self._lock:
            session = self._auth_sessions.get(token) if token else None
        if not session or float(session.get("expires_at", 0)) <= time.time():
            raise RuntimeError("Wallet signature missing or expired. Please sign again.")
        if norm and normalize_address(str(session.get("account_address", ""))) != norm:
            raise RuntimeError("Auth token / account mismatch.")

    def _next_nonce(self, account: str) -> int:
        key = normalize_address(account) or "__global__"
        now_ms = int(time.time() * 1000)
        prev = int(self._last_nonce.get(key, 0))
        nonce = now_ms if now_ms > prev else prev + 1
        self._last_nonce[key] = nonce
        return nonce

    # -- agent approval --------------------------------------------------

    def create_agent_approve_challenge(self, account_address: str) -> Dict[str, Any]:
        norm = normalize_address(account_address)
        if not norm:
            raise RuntimeError("Invalid account address.")
        existing = self._agents.get(norm, {})
        agent_key = ""
        agent_address = ""
        if existing.get("key"):
            candidate = normalize_address(_address_from_private_key(existing["key"]))
            live = _live_query_extra_agents(norm)
            if live is None:
                agent_key, agent_address = existing["key"], candidate
            elif live:
                now_ms = int(time.time() * 1000)
                for ag in live:
                    if normalize_address(str(ag.get("address", ""))) != candidate:
                        continue
                    try:
                        vu = int(ag.get("validUntil") or 0)
                    except (TypeError, ValueError):
                        vu = 0
                    if vu and vu < now_ms:
                        continue
                    agent_key, agent_address = existing["key"], candidate
                    break
        if not agent_key:
            from eth_account import Account
            generated = Account.create()
            _hex = generated.key.hex()
            agent_key = _hex if _hex.startswith("0x") else "0x" + _hex
            agent_address = normalize_address(generated.address)
        if not agent_address.startswith("0x"):
            raise RuntimeError("Failed to derive agent address.")

        now = time.time()
        with self._lock:
            self._cleanup()
            nonce = self._next_nonce(norm)
            action = _build_agent_approve_action(
                agent_address=agent_address, agent_name=AGENT_NAME, nonce=nonce,
            )
            typed_data = _build_agent_approve_typed_data(action)
            self._approve_challenges[str(nonce)] = {
                "account_address": norm, "agent_address": agent_address,
                "agent_name": AGENT_NAME, "agent_private_key": agent_key,
                "action": action, "typed_data": typed_data,
                "expires_at": now + AGENT_APPROVE_CHALLENGE_TTL_SEC,
            }
        return {
            "nonce": nonce, "account_address": norm, "agent_address": agent_address,
            "agent_name": AGENT_NAME, "typed_data": typed_data,
        }

    def consume_agent_approve_challenge(self, account_address: str, nonce: int) -> Dict[str, Any]:
        norm = normalize_address(account_address)
        with self._lock:
            self._cleanup()
            challenge = self._approve_challenges.pop(str(int(nonce)), None)
        if challenge is None:
            raise RuntimeError("Agent approval challenge expired. Please try again.")
        if normalize_address(str(challenge.get("account_address", ""))) != norm:
            raise RuntimeError("Challenge/account mismatch.")
        return challenge

    def set_agent_key(self, account: str, key: str, agent_address: str, agent_name: str = AGENT_NAME) -> None:
        norm = normalize_address(account)
        addr = normalize_address(agent_address)
        if not norm or not key.startswith("0x") or not addr.startswith("0x"):
            raise RuntimeError("invalid account, agent, or key")
        if _address_from_private_key(key) != addr:
            raise RuntimeError("agent address does not match private key")
        with self._lock:
            self._agents[norm] = {"key": key, "agent_address": addr, "agent_name": agent_name}
        self.keystore.save_agent(norm, key, addr, agent_name)

    def agent_identity(self, account: str) -> Dict[str, Any]:
        norm = normalize_address(account)
        rec = self._agents.get(norm, {})
        key = str(rec.get("key", "")).strip()
        return {
            "account_address": norm,
            "agent_key_present": bool(key),
            "agent_address": normalize_address(str(rec.get("agent_address", ""))),
            "agent_name": str(rec.get("agent_name", AGENT_NAME)) or AGENT_NAME,
        }

    def agent_key_for(self, account: str) -> str:
        return str(self._agents.get(normalize_address(account), {}).get("key", "")).strip()

    def _cleanup(self) -> None:
        now = time.time()
        for store in (self._auth_challenges, self._approve_challenges):
            for k in [k for k, v in store.items() if float(v.get("expires_at", 0)) <= now]:
                store.pop(k, None)
        for tok in [t for t, s in self._auth_sessions.items() if float(s.get("expires_at", 0)) <= now]:
            self._auth_sessions.pop(tok, None)


# ---------------------------------------------------------------------------
# Module-level API (FastAPI-free; callers translate RuntimeError to HTTP)
# ---------------------------------------------------------------------------

def agent_status(state: AuthState, account: str) -> Dict[str, Any]:
    account = normalize_address(account)
    identity = state.agent_identity(account)
    if not identity.get("agent_key_present"):
        identity["approved"] = False
        identity["message"] = "Agent not created yet. Sign the approval."
        return identity
    live = _live_query_extra_agents(account)
    approved = False
    if live:
        agent_addr = identity.get("agent_address", "")
        now_ms = int(time.time() * 1000)
        for ag in live:
            if normalize_address(str(ag.get("address", ""))) == agent_addr:
                try:
                    vu = int(ag.get("validUntil") or 0)
                except (TypeError, ValueError):
                    vu = 0
                if not vu or vu >= now_ms:
                    approved = True
                    break
    identity["approved"] = approved
    identity["message"] = "Agent approved." if approved else "Agent key present, not approved on-chain."
    return identity


def approve_submit(state: AuthState, account: str, nonce: int, signature: str) -> Dict[str, Any]:
    account = str(account or "").strip()
    signature_hex = str(signature or "").strip()
    try:
        nonce = int(nonce)
    except Exception:
        raise RuntimeError("Invalid nonce.")
    if not signature_hex:
        raise RuntimeError("Missing signature.")
    challenge = state.consume_agent_approve_challenge(account, nonce)
    typed_data = challenge.get("typed_data", {})
    recovered = _recover_typed_data_signer(typed_data, signature_hex)
    if recovered != normalize_address(account):
        raise RuntimeError("Signature does not match account address.")
    sig = _parse_signature_hex(signature_hex)
    action = challenge.get("action", {})
    exchange_payload = {
        "action": action, "nonce": int(action.get("nonce", nonce)),
        "signature": sig, "vaultAddress": None, "expiresAfter": None,
    }
    resp = _post_exchange_action(exchange_payload)
    if isinstance(resp, dict) and resp.get("status") == "err":
        if "Extra agent already used" in str(resp.get("response", "")):
            raise RuntimeError(
                "Révoquez l'agent BOTARB dans les réglages API wallet d'Hyperliquid, "
                "puis signez à nouveau."
            )
        raise RuntimeError(f"Hyperliquid rejected agent approval: {resp}")
    if not (isinstance(resp, dict) and resp.get("status") == "ok"):
        raise RuntimeError(f"Hyperliquid did not confirm agent approval: {resp}")
    agent_addr = normalize_address(str(challenge.get("agent_address", "")))
    state.set_agent_key(account, str(challenge.get("agent_private_key", "")), agent_addr, AGENT_NAME)
    return {
        "status": "approved", "account_address": normalize_address(account),
        "agent_address": agent_addr, "agent_name": AGENT_NAME,
    }
