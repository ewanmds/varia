"""Graphique du ratio WTI/Brent de Variational (USOILP / UKOILP) à partir des bougies 1 min
téléchargées par tools/var_candles.py.

Usage : python3 tools/ratio_chart.py [--window-min 120] [--zoom-days 3]
Produit data/candles/ratio_usoil_ukoil.png et affiche quelques statistiques.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DIR = Path(__file__).resolve().parents[1] / "data" / "candles"


def load_ratio() -> pd.DataFrame:
    a = pd.read_csv(DIR / "USOILP_1m.csv", index_col="unix_time_ms")
    b = pd.read_csv(DIR / "UKOILP_1m.csv", index_col="unix_time_ms")
    df = pd.DataFrame({"wti": a["close"], "brent": b["close"]}).dropna()
    df.index = pd.to_datetime(df.index, unit="ms", utc=True)
    df["ratio"] = df["wti"] / df["brent"]
    df["logr_bps"] = np.log(df["ratio"]) * 1e4
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=120)
    ap.add_argument("--zoom-days", type=float, default=3)
    args = ap.parse_args()
    df = load_ratio()
    w = args.window_min
    roll = df["logr_bps"].rolling(w, min_periods=w)
    df["mean"], df["std"] = roll.mean(), roll.std()
    df["z"] = (df["logr_bps"] - df["mean"]) / df["std"]
    dev = (df["logr_bps"] - df["mean"]).dropna()

    fig, axes = plt.subplots(3, 1, figsize=(15, 11))
    ax = axes[0]
    ax.plot(df.index, df["ratio"], lw=0.6, color="#1f6feb")
    ax.set_title(f"Ratio WTI / Brent sur Variational (USOILP / UKOILP), bougies 1 min, {df.index[0]:%d/%m} au {df.index[-1]:%d/%m}")
    ax.set_ylabel("WTI / Brent")
    ax.grid(alpha=0.3)

    z = df[df.index >= df.index[-1] - pd.Timedelta(days=args.zoom_days)]
    ax = axes[1]
    ax.plot(z.index, z["logr_bps"], lw=0.7, color="#1f6feb", label="log(ratio) en pb")
    ax.plot(z.index, z["mean"], lw=1, color="#555", label=f"moyenne glissante {w} min")
    for k, c in ((1, "#9be9a8"), (2, "#e3b341")):
        ax.fill_between(z.index, z["mean"] - k * z["std"], z["mean"] + k * z["std"], color=c, alpha=0.25, label=f"± {k} écart-type")
    ax.set_title(f"Zoom sur les {args.zoom_days:g} derniers jours")
    ax.set_ylabel("pb")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[2]
    ax.hist(dev, bins=200, color="#1f6feb", alpha=0.8)
    ax.set_title(f"Écart du ratio à sa moyenne glissante {w} min (pb)")
    ax.set_xlabel("pb")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    out = DIR / "ratio_usoil_ukoil.png"
    fig.savefig(out, dpi=110)

    print(f"{len(df)} minutes communes du {df.index[0]:%Y-%m-%d %H:%M} au {df.index[-1]:%Y-%m-%d %H:%M} UTC")
    print(f"ratio : min {df['ratio'].min():.4f}, max {df['ratio'].max():.4f}, dernier {df['ratio'].iloc[-1]:.4f}")
    print(f"écart à la moyenne {w} min : écart-type {dev.std():.1f} pb, p95 |écart| {dev.abs().quantile(0.95):.1f} pb, max {dev.abs().max():.1f} pb")
    rng = df["logr_bps"].resample("20min").agg(lambda s: s.max() - s.min()).dropna()
    print(f"amplitude du ratio sur 20 min : médiane {rng.median():.1f} pb, p90 {rng.quantile(0.9):.1f} pb")
    print(f"graphique : {out}")


if __name__ == "__main__":
    main()
