import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

j = json.load(open("backtest/results/exit_variant_results.json"))
summ = j["summary"]
rows = []
for k in ("V0", "V1", "V2", "V3"):
    m = summ[k]["overall"]
    rows.append((k, summ[k]["name"], m["n"], m["wr"], m["profit_factor"], m["avg_pnl_pct"], m["total_pnl_usd"], m["sharpe"]))
rows.sort(key=lambda r: r[0])

fig, ax = plt.subplots(figsize=(9, 6), dpi=110)
colors = {"V0": "#2e86de", "V1": "#27ae60", "V2": "#f39c12", "V3": "#e74c3c"}
for k, name, n, wr, pf, avg, tot, sh in rows:
    ax.scatter(tot, wr, s=900, color=colors[k], edgecolor="black", zorder=3)
    # keep labels inside the frame: high-PnL points annotated to the LEFT
    if tot > 3500:
        ha, dx = "right", -10
    else:
        ha, dx = "left", 10
    ax.annotate(f"{k}  {name}\nWR {wr:.0f}%  PF {pf:.2f}  ${tot:,.0f}",
                (tot, wr), xytext=(dx, 12), textcoords="offset points",
                ha=ha, fontsize=9, fontweight="bold", color=colors[k])

ax.axvline(0, color="grey", lw=0.8, ls="--")
ax.axhline(65, color="#e74c3c", lw=1.0, ls=":")
xmax = max(r[6] for r in rows) * 1.05
ax.set_xlim(-250, xmax)
ax.set_ylim(18, 72)
ax.text(xmax * 0.99, 68.5, "target WR ≥ 65%", ha="right", color="#e74c3c", fontsize=9)
ax.set_xlabel("Total PnL   (US$, $1k per trade,  2025-01 → 2026-08)")
ax.set_ylabel("Win rate  (%)")
ax.set_title("Exit Variants — PnL vs Win-Rate   (40 symbols, 3 signal types)")
ax.grid(alpha=0.3)
ax.set_facecolor("#101418"); fig.patch.set_facecolor("#0b0e12")
for s in ax.spines.values(): s.set_color("#333")
ax.tick_params(colors="white"); ax.xaxis.label.set_color("white"); ax.yaxis.label.set_color("white")
ax.title.set_color("white")
plt.tight_layout()
plt.savefig("backtest/results/exit_variants.png", facecolor=fig.get_facecolor())
print("saved backtest/results/exit_variants.png")
for r in rows:
    print(r[0], r[1], r[3], r[4], r[6])
