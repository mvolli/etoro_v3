import json, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Path of the results JSON to chart (default = DIP-unlocked main run)
path = sys.argv[1] if len(sys.argv) > 1 else \
    "backtest/results/exit_variant_results_main_ka4.0_c0.3.json"
j = json.load(open(path))
summ = j["summary"]
cost = j.get("cost_pct", 0)
sim_start = j.get("sim_start") or j.get("data_start")
sim_end = j.get("sim_end") or "today"

rows = []
for k in ("V0", "V1", "V2", "V3"):
    m = summ[k]["overall"]
    rows.append((k, summ[k]["name"], m["n"], m["wr"], m["profit_factor"],
                 m["avg_pnl_pct"], m["total_pnl_usd"], m["sharpe"]))
rows.sort(key=lambda r: r[0])

fig, ax = plt.subplots(figsize=(9.5, 6.2), dpi=110)
colors = {"V0": "#2e86de", "V1": "#27ae60", "V2": "#f39c12", "V3": "#e74c3c"}
for k, name, n, wr, pf, avg, tot, sh in rows:
    ax.scatter(tot, wr, s=950, color=colors[k], edgecolor="black", zorder=3)
    if tot > max(r[6] for r in rows) * 0.55:
        ha, dx = "right", -10
    else:
        ha, dx = "left", 10
    ax.annotate(f"{k}  {name}\nWR {wr:.0f}%  PF {pf:.2f}  ${tot:,.0f} (net)",
                (tot, wr), xytext=(dx, 13), textcoords="offset points",
                ha=ha, fontsize=9, fontweight="bold", color=colors[k])

ax.axvline(0, color="grey", lw=0.8, ls="--")
ax.axhline(65, color="#e74c3c", lw=1.0, ls=":")
xmax = max(max(r[6] for r in rows), 1) * 1.08
xmin = min(-250, min(r[6] for r in rows) * 1.25 - 150)
ax.set_xlim(xmin, xmax)
ax.set_ylim(15, 72)
ax.text(xmax * 0.99, 68.5, "target WR ≥ 65%", ha="right", color="#e74c3c", fontsize=9)
ax.set_xlabel(f"Net PnL  (US$, $1k/trade, {cost:g}%/rt cost,  {sim_start} → {sim_end})")
ax.set_ylabel("Win rate  (%)")
ax.set_title(f"Exit Variants — Net PnL vs Win-Rate   (40 symbols, cost {cost:g}%/rt,  {sim_start} → {sim_end})")
ax.grid(alpha=0.3)
ax.set_facecolor("#101418"); fig.patch.set_facecolor("#0b0e12")
for s in ax.spines.values(): s.set_color("#333")
ax.tick_params(colors="white"); ax.xaxis.label.set_color("white"); ax.yaxis.label.set_color("white")
ax.title.set_color("white")
plt.tight_layout()
out = sys.argv[2] if len(sys.argv) > 2 else "backtest/results/exit_variants.png"
plt.savefig(out, facecolor=fig.get_facecolor())
print("saved", out)
for r in rows:
    print(r[0], r[1], "net$", round(r[6]), "wr", round(r[3],1), "pf", r[5])
