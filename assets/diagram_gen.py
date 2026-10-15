"""Generate the discord-hub architecture diagram PNG for the forum post."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

BG = "#1e1f22"  # discord dark
BOX = "#2b2d31"
EDGE = "#5865f2"  # discord blurple
ACCENT = "#57f287"  # green
TEXT = "#e3e5e8"
MUTED = "#949ba4"

fig, ax = plt.subplots(figsize=(12, 6.75), dpi=150)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_xlim(0, 12)
ax.set_ylim(0, 6.75)
ax.axis("off")


def box(x, y, w, h, label, sub=None, ec=EDGE, lw=2.2, fs=13, bold=True):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.08,rounding_size=0.15",
            facecolor=BOX,
            edgecolor=ec,
            linewidth=lw,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2 + (0.14 if sub else 0),
        label,
        ha="center",
        va="center",
        color=TEXT,
        fontsize=fs,
        fontweight="bold" if bold else "normal",
        family="Segoe UI",
    )
    if sub:
        ax.text(
            x + w / 2,
            y + h / 2 - 0.28,
            sub,
            ha="center",
            va="center",
            color=MUTED,
            fontsize=9.5,
            family="Segoe UI",
        )


def arrow(x1, y1, x2, y2, label=None, lx=0, ly=0.18):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle="<->", mutation_scale=16, color=EDGE, linewidth=2
        )
    )
    if label:
        ax.text(
            (x1 + x2) / 2 + lx,
            (y1 + y2) / 2 + ly,
            label,
            ha="center",
            va="center",
            color=ACCENT,
            fontsize=9,
            style="italic",
            family="Segoe UI",
        )


# Discord (left)
box(0.4, 2.6, 2.6, 1.5, "Discord", "the server · channels · voice")

# Hub (center) — bigger, highlighted
box(4.6, 2.45, 2.8, 1.8, "discord-hub", "one bot · one connection", ec=ACCENT, lw=3, fs=16)

# Projects (right)
projects = ["socratic-partner", "teaching-agent", "trading engine", "future projects…"]
ys = [5.3, 3.75, 2.2, 0.65]
for name, y in zip(projects, ys, strict=True):
    box(9.0, y, 2.6, 1.0, name, fs=11)
    arrow(7.4, 3.35, 9.0, y + 0.5)

arrow(3.0, 3.35, 4.6, 3.35, "the only connection")

# API label under hub
ax.text(
    6.0,
    2.15,
    "simple HTTP API — no Discord code in projects",
    ha="center",
    va="center",
    color=ACCENT,
    fontsize=10,
    style="italic",
    family="Segoe UI",
)

ax.text(
    6.0,
    6.35,
    "One bot. One connection. Every project talks to Discord through this.",
    ha="center",
    va="center",
    color=TEXT,
    fontsize=13,
    fontweight="bold",
    family="Segoe UI",
)

plt.tight_layout(pad=0.4)
out = r"C:\Users\cedgo\Pictures\discord-hub-diagram.png"
plt.savefig(out, facecolor=BG, bbox_inches="tight")
print("saved:", out)
