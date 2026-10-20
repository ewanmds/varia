from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import tomli


# Modes d'entrée : "chase" (défaut), "maker" (historique), "lag_taker" (L4, désactivé par défaut).
ENTRY_MODES = ("chase", "maker", "lag_taker")


@dataclass
class MarketCfg:
    var_asset: str
    var_kind: str
    hl_coin: str
    enabled: bool = True
    fill_usd: float = 1000.0
    min_edge_bps: float = 5.0
    slip_reserve_bps: float = 2.0
    exit_edge_bps: float = 0.0
    reprice_bps: float = 1.0
    # C1a : un ordre au repos devenu trop PASSIF n'est reposé que s'il l'est d'au moins ce nombre
    # de pb (et après min_reprice_interval_s) ; un ordre devenu trop AGRESSIF l'est tout de suite.
    reprice_passive_bps: float = 3.0
    max_position_usd: float = 3000.0
    max_delta_usd: Optional[float] = None  # défaut : 1,5 x fill_usd
    min_hedge_usd: float = 20.0
    sanity_bps: float = 30.0
    basis_mode: str = "raw"  # "raw" | "ema"
    basis_ema_tau_min: float = 30.0
    basis_guard_bps: float = 30.0
    # L3 (levers-2026-09-23) : moyenne mobile RAPIDE du basis (prix websocket Var contre mid bbo
    # HL), constante de temps en secondes. Utilisée pour la décision des marchés en basis_mode
    # "ema" (pétroles) quand le prix websocket Var est frais ; l'EMA lente (basis_ema_tau_min)
    # reste la référence du garde-fou basis_guard_bps.
    basis_fast_tau_s: float = 60.0
    roll_block: bool = False
    # L1 (run réel du 23/09) : à 10, la liquidation Var tombe à environ 5,3 % du prix, sous
    # var_liq_distance_min (8 %) : chaque position déclenchait une réduction forcée. À 3, environ
    # 30 %. Borné au maximum Var de l'actif au setup (5 pour les pétroles).
    var_leverage: int = 5
    hl_maker_fee_bps: Optional[float] = None  # None = lu sur HL au démarrage
    # Mode d'entrée (docs/superpowers/chase-mode-2026-09-23.md). "chase" : aucun ordre sans
    # inefficience, ordre ALO au meilleur prix du carnet HL suivi par modify tant qu'elle dure.
    # "maker" : cotation permanente des deux côtés autour de Var (comportement historique).
    entry_mode: str = "chase"
    chase_ttl_s: float = 30.0
    chase_min_modify_interval_s: float = 1.0
    # Délai minimal, par côté, entre une annulation chase (quelle qu'en soit la raison) et une
    # nouvelle pose : évite d'annuler et de reposer à chaque tick autour du seuil.
    chase_replace_cooldown_s: float = 3.0
    # F1 (docs/superpowers/fresh-quote-2026-09-23.md) : une nouvelle pose chase n'est faite que
    # sur une cotation Var PLUS RÉCENTE que celle qui a détecté l'inefficience (cotation fraîche
    # demandée au VarFeed à la détection), l'écart étant réévalué sur cette cotation.
    chase_fresh_confirm: bool = True
    # F3 : âge maximal (s) de la cotation Var pour poser ou déplacer un ordre chase. None = 1,0
    # pour les pétroles (basis_mode "ema"), 1,5 sinon.
    chase_max_quote_age_s: Optional[float] = None
    # L4 (levers-2026-09-23) : mode "lag_taker" (désactivé par défaut) : prise en IOC sur HL
    # quand le prix websocket Var a bougé et que le meilleur prix HL n'a pas suivi.
    # Âges maximaux (s) du prix websocket Var et du bbo HL au moment de la prise.
    lag_var_max_age_s: float = 1.5
    lag_hl_max_age_s: float = 1.0
    # au plus une prise par marché toutes les lag_min_interval_s secondes.
    lag_min_interval_s: float = 5.0
    # « le prix Var bouge » : variation du prix websocket Var, dans le sens de la prise, mesurée
    # contre le plus ancien prix des lag_move_window_s dernières secondes, d'au moins
    # lag_min_move_bps.
    lag_move_window_s: float = 2.0
    lag_min_move_bps: float = 1.0
    # Revue : fraîcheur du prix websocket côté serveur. Refus si la réception a plus de
    # lag_ws_max_transport_s de retard sur l'horodatage serveur, ou si le prix websocket précédent
    # de ce marché avait plus de lag_ws_max_prev_gap_s (saut de rattrapage après un trou).
    lag_ws_max_transport_s: float = 1.0
    lag_ws_max_prev_gap_s: float = 2.0
    # Revue : demi-spread Var de la prise = max(dernier, médiane récente) x lag_spread_mult, et
    # seulement avec une cotation RFQ de moins de var_ws_spread_max_age_s.
    lag_spread_mult: float = 1.5

    def __post_init__(self) -> None:
        # R2 : retenir si max_delta_usd suit fill_usd (non fixé explicitement), pour le recalculer
        # quand fill_usd change à chaud. Attribut hors dataclass : pas une clé de config.
        self.max_delta_auto = self.max_delta_usd is None
        if self.max_delta_usd is None:
            self.max_delta_usd = 1.5 * self.fill_usd
        if self.basis_mode not in ("raw", "ema"):
            raise ValueError(f"basis_mode invalide: {self.basis_mode}")
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode invalide: {self.entry_mode}")
        if self.chase_max_quote_age_s is None:
            self.chase_max_quote_age_s = 1.0 if self.basis_mode == "ema" else 1.5


@dataclass
class GlobalCfg:
    var_cdp_port: int = 9223
    var_transport: str = "cdp"
    var_quote_interval_s: float = 2.0
    # F2 : un marché « chaud » (ordre chase au repos, ou inefficience vue dans les
    # hot_window_s dernières secondes) est coté au moins toutes les hot_quote_interval_s.
    hot_quote_interval_s: float = 1.0
    hot_window_s: float = 10.0
    var_min_interval_s: float = 0.25
    # L8 (run réel du 23/09) : 3 s était trop proche du cycle réel du VarFeed (âge médian 2 s,
    # jusqu'à 4,3 s) : cotation jugée périmée 9 à 33 % du temps.
    quote_max_age_s: float = 5.0
    # L8 : cotation seulement périmée -> on ne repose rien, mais les ordres au repos ne sont
    # annulés qu'au-delà de cet âge.
    quote_cancel_age_s: float = 8.0
    # L1 (levers-2026-09-23) : silence global HL. Avec le flux bbo (environ 40 messages par
    # seconde sur les 5 marchés, trou global maximal observé 1,5 s un après-midi), 10 s ; 15 s
    # était calibré sur l2Book (une salve toutes les 5 s environ). Pas moins : bbo ne pousse que
    # sur changement, et un silence déclenche une annulation globale sur HL.
    hl_ws_stall_s: float = 10.0
    # amendement t14 (fix) : au-delà de cet âge, le carnet HL d'UN marché précis est considéré
    # périmé (motif "carnet HL périmé", ce marché seul cesse de coter), indépendamment du silence
    # global ci-dessus (qui porte sur l'ensemble des coins/canaux et déclenche une annulation
    # exchange globale).
    # L1 : sur bbo, trou maximal observé par coin 5,9 s (or, 1 écart sur 30 au-delà de 1 s) :
    # 10 s (30 s était calibré sur l2Book).
    hl_book_max_age_s: float = 10.0
    hedge_retry_s: float = 8.0
    max_gross_usd: float = 10000.0
    max_daily_loss_usd: float = 50.0
    hl_margin_ratio_max: float = 0.6
    var_liq_distance_min: float = 0.08  # S1 : spec §6 (8 %)
    close_start_min: int = 20
    close_taker_min: int = 10
    close_flat_min: int = 5
    reopen_delay_min: int = 5
    reopen_wide_min: int = 15
    var_max_slippage: float = 0.001
    reconcile_interval_s: float = 10.0
    strike_window_s: float = 600.0
    strike_max: int = 3
    # I6 : à partir de ce nombre de strikes Var sur 24 h, cotations suspendues jusqu'à minuit UTC.
    var_daily_strike_max: int = 5
    # C1a : délai minimal entre deux placements d'un même côté pour un simple repositionnement
    # d'ordre trop passif.
    min_reprice_interval_s: float = 5.0
    # C1d : sous cette marge de requêtes HL restantes (userRateLimit), mode "annulation seule".
    hl_min_request_headroom: int = 2000
    # N2 : dead-man switch HL, renouvelé toutes les deadman_renew_s avec une échéance à
    # maintenant + deadman_window_s.
    deadman_renew_s: float = 60.0
    deadman_window_s: float = 120.0
    # L2 (run réel du 23/09) : après une réduction forcée, marché en PAUSE pendant ce délai (une
    # 2e réduction forcée sur le même marché en moins de 30 min le passe en BLOQUÉ).
    forced_reduction_cooldown_s: float = 600.0
    # Arrêt normal (bouton « Arrêter », fichier data/STOP) : délai pour que les positions réelles
    # soient à plat sur HL et Var. Au-delà, le bot reste en marche et continue de fermer.
    stop_flatten_timeout_s: float = 120.0
    # Arrêt normal : une jambe dont le notionnel est sous ce seuil (juste au-dessus du minimum
    # d'ordre HL de 11 $) compte comme à plat ; incident « à solder à la main ».
    stop_dust_usd: float = 12.0
    # L2 (levers-2026-09-23) : prix websocket Var (arb/var_ws.py). Référence de prix juste Var
    # tant que son dernier prix a au plus var_ws_max_age_s (1 s de cadence + transport) et que la
    # dernière cotation RFQ du marché (qui donne le demi-spread) a au plus
    # var_ws_spread_max_age_s ; sinon repli sur la cotation RFQ. Alerte si aucun prix depuis
    # var_ws_silence_s.
    var_ws_enabled: bool = True
    var_ws_max_age_s: float = 1.5
    # Revue : 5 s (30 s laissait un demi-spread trop ancien ; même âge que quote_max_age_s).
    var_ws_spread_max_age_s: float = 5.0
    var_ws_silence_s: float = 5.0


@dataclass
class PairCfg:
    """Mode paire pétrole : paire couverte WTI/Brent sur Var, retournée toutes les cycle_min."""
    enabled: bool = False
    wti: str = "USOILP"
    brent: str = "UKOILP"
    kind: str = "commodity"
    leg_usd: float = 5000.0
    tranche_usd: float = 2500.0
    cycle_min: float = 10.0
    volume_cycle_min: float = 0.0
    volume_cycle_required: bool = False
    z_window_min: int = 60
    order_gap_s: float = 1.0
    max_slippage: float = 0.001
    daily_loss_usd: float = 200.0
    var_leverage: int = 5
    close_before_min: int = 10
    no_cycle_before_close_min: int = 15
    reopen_after_min: int = 5
    price_stale_s: float = 5.0
    cost_alert_per_m: float = 80.0
    signal_mode: str = "mean_reversion"
    trend_window_min: int = 30
    trend_confirm_min: int = 5
    trend_context_min: int = 0
    trend_min_move_bps: float = 3.0
    trend_min_r2: float = 0.4
    trend_cost_multiplier: float = 1.5
    trend_projection_min: float = 10.0
    trend_slip_reserve_bps: float = 2.0
    warmup_entry_side: Optional[str] = None
    trend_source: str = "variational"
    tv_wti_symbol: str = "TVC:USOIL"
    tv_brent_symbol: str = "TVC:UKOIL"
    tv_stale_s: float = 90.0

    @property
    def effective_tranche_usd(self) -> float:
        """Each leg opens in two equal slices, including after a size change."""
        return self.leg_usd / 2

    def __post_init__(self):
        import math
        # Keep the legacy config field consistent for paper/replay consumers.
        self.tranche_usd = self.effective_tranche_usd
        if self.signal_mode not in ("mean_reversion", "trend_hold"):
            raise ValueError("signal_mode doit être mean_reversion ou trend_hold")
        if self.warmup_entry_side not in (None, "A", "B"):
            raise ValueError("warmup_entry_side doit être A, B ou absent")
        if self.trend_source not in ("variational", "tradingview"):
            raise ValueError("trend_source doit être variational ou tradingview")
        if self.trend_source == "tradingview" and self.warmup_entry_side is not None:
            raise ValueError("TradingView exige un signal confirmé, sans sens forcé")
        if (not isinstance(self.tv_wti_symbol, str) or not isinstance(self.tv_brent_symbol, str)
                or not self.tv_wti_symbol.startswith("TVC:") or not self.tv_brent_symbol.startswith("TVC:")
                or self.tv_wti_symbol == self.tv_brent_symbol):
            raise ValueError("symboles TVC distincts requis")
        if not math.isfinite(self.tv_stale_s) or not 30 <= self.tv_stale_s <= 120:
            raise ValueError("fraîcheur TradingView invalide")
        if not math.isfinite(self.trend_projection_min) or not 1 <= self.trend_projection_min <= 60:
            raise ValueError("horizon de projection invalide")
        if not math.isfinite(self.volume_cycle_min) or not (self.volume_cycle_min == 0 or 1 <= self.volume_cycle_min <= 60):
            raise ValueError("cycle volume invalide")
        if not isinstance(self.volume_cycle_required, bool):
            raise ValueError("volume_cycle_required doit être booléen")
        if not (5 <= self.trend_window_min <= 120 and 2 <= self.trend_confirm_min < self.trend_window_min):
            raise ValueError("fenêtres de tendance invalides")
        if (not isinstance(self.trend_context_min, int) or isinstance(self.trend_context_min, bool)
                or not (self.trend_context_min == 0 or self.trend_window_min < self.trend_context_min <= 120)):
            raise ValueError("fenêtre de contexte invalide")
        if not all(math.isfinite(v) for v in (self.trend_min_move_bps, self.trend_min_r2,
                                             self.trend_cost_multiplier, self.trend_slip_reserve_bps)):
            raise ValueError("seuil de tendance non fini")
        if not (self.trend_min_move_bps > 0 and 0 <= self.trend_min_r2 <= 1
                and self.trend_cost_multiplier >= 1 and self.trend_slip_reserve_bps >= 0):
            raise ValueError("seuils de tendance invalides")


def _build(cls, data: dict):
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"clés inconnues pour {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_config(path) -> tuple[GlobalCfg, list[MarketCfg]]:
    raw = tomli.loads(Path(path).read_text())
    g = _build(GlobalCfg, raw.get("global", {}))
    markets = [_build(MarketCfg, m) for m in raw.get("market", [])]
    return g, markets


def load_pair_config(path) -> PairCfg:
    raw = tomli.loads(Path(path).read_text())
    return _build(PairCfg, raw.get("pair", {}))
