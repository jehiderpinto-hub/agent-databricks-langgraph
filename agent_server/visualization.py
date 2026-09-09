"""
Visualization tool module for Databricks LangGraph Agent.
Generates comprehensive visual charts and returns them as markdown-embedded images.
"""

import io
import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

import matplotlib
# Use non-interactive backend suitable for server environments
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# In-memory cache of generated chart PNGs, served back to the browser via the
# GET /invocations?chart_id=... route registered in start_server.py.
#
# Charts can't be embedded as data: URIs in the chat markdown: the frontend's
# markdown renderer (Streamdown, via rehype-harden) blocks <img> sources that
# aren't http(s) by default -- and that frontend is a vendored template we
# don't control, re-cloned on every deploy, so we can't just change its
# renderer config. The chat's only backend-reachable path is /invocations
# (the frontend proxies exactly that path, and no other, to this server) --
# see server/src/index.ts in databricks/app-templates' e2e-chatbot-app-next.
# So charts are cached here by id and fetched by the browser as a normal
# same-origin image request instead.
#
# Bounded LRU-ish cache (simple dict with insertion-order eviction) since this
# is ephemeral per-session data, not something that needs to survive restarts.
_CHART_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_CHART_CACHE_MAX_SIZE = 50


def get_cached_chart(chart_id: str) -> Optional[bytes]:
    """Look up a previously generated chart PNG by id. Used by the /invocations
    GET route in start_server.py to serve chart images back to the browser."""
    return _CHART_CACHE.get(chart_id)


def _cache_chart(png_bytes: bytes) -> str:
    chart_id = uuid4().hex
    _CHART_CACHE[chart_id] = png_bytes
    while len(_CHART_CACHE) > _CHART_CACHE_MAX_SIZE:
        _CHART_CACHE.popitem(last=False)
    return chart_id

# Predefined modern color palettes
COLOR_PALETTES = {
    "modern": ["#2563EB", "#3B82F6", "#60A5FA", "#93C5FD", "#1D4ED8", "#1E40AF"],
    "ocean": ["#0284C7", "#0EA5E9", "#38BDF8", "#7DD3FC", "#0369A1", "#075985"],
    "emerald": ["#059669", "#10B981", "#34D399", "#6EE7B7", "#047857", "#065F46"],
    "sunset": ["#EA580C", "#F97316", "#FB923C", "#FDBA74", "#C2410C", "#9A3412"],
    "purple": ["#7C3AED", "#8B5CF6", "#A78BFA", "#C4B5FD", "#6D28D9", "#5B21B6"],
    "corporate": ["#1E293B", "#334155", "#475569", "#64748B", "#94A3B8", "#CBD5E1"],
    "vibrant": ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#06B6D4"],
}


def _apply_style(ax: plt.Axes, title: str, x_label: str = "", y_label: str = ""):
    """Applies a clean, modern aesthetic styling to the matplotlib axes."""
    # Background and spine styling
    ax.set_facecolor("#FAFAFA")
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color("#CBD5E1")
        ax.spines[spine].set_linewidth(1.0)

    # Grid styling
    ax.grid(axis="y", linestyle="--", alpha=0.5, color="#E2E8F0", zorder=0)
    ax.set_axisbelow(True)

    # Labels and title
    if title:
        ax.set_title(
            title,
            fontsize=13,
            fontweight="bold",
            color="#1E293B",
            pad=15,
            loc="center",
        )
    if x_label:
        ax.set_xlabel(x_label, fontsize=10, fontweight="bold", color="#475569", labelpad=8)
    if y_label:
        ax.set_ylabel(y_label, fontsize=10, fontweight="bold", color="#475569", labelpad=8)

    ax.tick_params(axis="both", which="major", labelsize=9, colors="#475569")


def _convert_to_dataframe(data: Union[List[Dict[str, Any]], str, Dict[str, Any]]) -> pd.DataFrame:
    """Converts various data input types (JSON string, list of dicts, dict) to DataFrame."""
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            return pd.DataFrame(parsed)
        except Exception as e:
            raise ValueError(f"Error parsing JSON string data: {e}")
    elif isinstance(data, list):
        return pd.DataFrame(data)
    elif isinstance(data, dict):
        return pd.DataFrame([data])
    else:
        raise ValueError(f"Unsupported data format: {type(data)}")


def _generate_plot_image(
    df: pd.DataFrame,
    chart_type: str,
    x_key: str,
    y_keys: List[str],
    title: str,
    x_label: str,
    y_label: str,
    palette_name: str = "vibrant",
    show_values: bool = True,
) -> bytes:
    """Generates the plot and returns the raw PNG bytes."""
    colors = COLOR_PALETTES.get(palette_name, COLOR_PALETTES["vibrant"])
    
    # Adjust figure size dynamically based on data points
    num_items = len(df)
    fig_width = max(8, min(14, num_items * 0.8))
    fig_height = 5.5

    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=130)
    fig.patch.set_facecolor("#FFFFFF")

    chart_type = chart_type.lower().strip()

    if chart_type in ["bar", "column"]:
        x = np.arange(len(df))
        width = 0.8 / len(y_keys) if len(y_keys) > 1 else 0.55

        for i, y_col in enumerate(y_keys):
            offset = (i - (len(y_keys) - 1) / 2) * width if len(y_keys) > 1 else 0
            color = colors[i % len(colors)]
            bars = ax.bar(
                x + offset,
                df[y_col],
                width,
                label=y_col if len(y_keys) > 1 else None,
                color=color,
                edgecolor="none",
                alpha=0.9,
                zorder=3,
            )
            if show_values:
                for bar in bars:
                    height = bar.get_height()
                    if not np.isnan(height):
                        ax.annotate(
                            f"{height:,.2f}".rstrip("0").rstrip("."),
                            xy=(bar.get_x() + bar.get_width() / 2, height),
                            xytext=(0, 3),
                            textcoords="offset points",
                            ha="center",
                            va="bottom",
                            fontsize=8,
                            color="#334155",
                            fontweight="bold",
                        )

        ax.set_xticks(x)
        ax.set_xticklabels(
            [str(v) for v in df[x_key]],
            rotation=30 if num_items > 5 else 0,
            ha="right" if num_items > 5 else "center",
        )
        _apply_style(ax, title, x_label or x_key, y_label or (y_keys[0] if len(y_keys) == 1 else "Values"))

    elif chart_type in ["horizontal_bar", "barh"]:
        y = np.arange(len(df))
        height = 0.8 / len(y_keys) if len(y_keys) > 1 else 0.55
        
        for i, y_col in enumerate(y_keys):
            offset = (i - (len(y_keys) - 1) / 2) * height if len(y_keys) > 1 else 0
            color = colors[i % len(colors)]
            bars = ax.barh(
                y + offset,
                df[y_col],
                height,
                label=y_col if len(y_keys) > 1 else None,
                color=color,
                alpha=0.9,
                zorder=3,
            )
            if show_values:
                for bar in bars:
                    width = bar.get_width()
                    if not np.isnan(width):
                        ax.annotate(
                            f"{width:,.2f}".rstrip("0").rstrip("."),
                            xy=(width, bar.get_y() + bar.get_height() / 2),
                            xytext=(4, 0),
                            textcoords="offset points",
                            ha="left",
                            va="center",
                            fontsize=8,
                            color="#334155",
                            fontweight="bold",
                        )

        ax.set_yticks(y)
        ax.set_yticklabels([str(v) for v in df[x_key]])
        ax.invert_yaxis()  # top-down order
        _apply_style(ax, title, x_label or (y_keys[0] if len(y_keys) == 1 else "Values"), y_label or x_key)
        ax.grid(axis="x", linestyle="--", alpha=0.5, color="#E2E8F0", zorder=0)
        ax.grid(axis="y", visible=False)

    elif chart_type in ["line", "trend"]:
        x = np.arange(len(df))
        for i, y_col in enumerate(y_keys):
            color = colors[i % len(colors)]
            ax.plot(
                x,
                df[y_col],
                marker="o",
                linewidth=2.2,
                markersize=6,
                label=y_col,
                color=color,
                zorder=3,
            )
            if show_values:
                for xi, yi in zip(x, df[y_col]):
                    if not np.isnan(yi):
                        ax.annotate(
                            f"{yi:,.2f}".rstrip("0").rstrip("."),
                            xy=(xi, yi),
                            xytext=(0, 6),
                            textcoords="offset points",
                            ha="center",
                            fontsize=8,
                            color="#334155",
                        )

        ax.set_xticks(x)
        ax.set_xticklabels(
            [str(v) for v in df[x_key]],
            rotation=30 if num_items > 5 else 0,
            ha="right" if num_items > 5 else "center",
        )
        _apply_style(ax, title, x_label or x_key, y_label or (y_keys[0] if len(y_keys) == 1 else "Values"))

    elif chart_type in ["pie", "donut"]:
        val_col = y_keys[0]
        labels = [str(v) for v in df[x_key]]
        values = df[val_col]
        
        wedgeprops = {"edgecolor": "white", "linewidth": 2}
        if chart_type == "donut":
            wedgeprops["width"] = 0.45  # Donut hole

        wedges, texts, autotexts = ax.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            colors=colors[: len(labels)],
            wedgeprops=wedgeprops,
            pctdistance=0.75 if chart_type == "donut" else 0.6,
        )
        for t in texts:
            t.set_fontsize(9)
            t.set_color("#1E293B")
        for at in autotexts:
            at.set_fontsize(8)
            at.set_fontweight("bold")
            at.set_color("#FFFFFF")

        ax.set_title(title, fontsize=13, fontweight="bold", color="#1E293B", pad=15)

    elif chart_type in ["area"]:
        x = np.arange(len(df))
        for i, y_col in enumerate(y_keys):
            color = colors[i % len(colors)]
            ax.plot(x, df[y_col], color=color, linewidth=1.8, label=y_col)
            ax.fill_between(x, df[y_col], color=color, alpha=0.25)

        ax.set_xticks(x)
        ax.set_xticklabels(
            [str(v) for v in df[x_key]],
            rotation=30 if num_items > 5 else 0,
            ha="right" if num_items > 5 else "center",
        )
        _apply_style(ax, title, x_label or x_key, y_label or (y_keys[0] if len(y_keys) == 1 else "Values"))

    elif chart_type in ["scatter"]:
        x_val = pd.to_numeric(df[x_key], errors="coerce")
        y_val = pd.to_numeric(df[y_keys[0]], errors="coerce")
        ax.scatter(x_val, y_val, color=colors[0], s=60, alpha=0.8, edgecolors="none", zorder=3)
        _apply_style(ax, title, x_label or x_key, y_label or y_keys[0])

    elif chart_type in ["histogram", "hist"]:
        val_col = y_keys[0] if y_keys else x_key
        vals = pd.to_numeric(df[val_col], errors="coerce").dropna()
        n, bins, patches = ax.hist(vals, bins="auto", color=colors[0], edgecolor="white", alpha=0.85, zorder=3)
        _apply_style(ax, title, x_label or val_col, y_label or "Frecuencia")

    else:
        raise ValueError(f"Unsupported chart_type '{chart_type}'. Supported types: bar, horizontal_bar, line, pie, donut, area, scatter, histogram")

    if len(y_keys) > 1 and chart_type not in ["pie", "donut", "histogram"]:
        ax.legend(frameon=False, fontsize=9)

    plt.tight_layout()

    # Save to buffer as PNG bytes
    buffer = io.BytesIO()
    plt.savefig(buffer, format="png", bbox_inches="tight", dpi=130)
    plt.close(fig)
    return buffer.getvalue()


@tool
def generate_chart(
    data: Union[List[Dict[str, Any]], str],
    chart_type: str,
    x_key: str,
    y_keys: Union[List[str], str],
    title: str,
    x_label: Optional[str] = "",
    y_label: Optional[str] = "",
    palette: Optional[str] = "vibrant",
    show_values: Optional[bool] = True,
) -> str:
    """Generates visual charts and graphs (bar, line, pie, donut, area, scatter, histogram)
    from tabular data. Returns a short markdown snippet with an embedded chart image link
    (![title](/invocations?chart_id=...)) -- include this markdown line verbatim, exactly as
    returned, in your reply so the chart renders in the chat.

    Args:
        data: List of dicts or JSON string representing the dataset. Example: [{"category": "A", "sales": 120}, {"category": "B", "sales": 250}]
        chart_type: Type of chart to generate. Options: 'bar', 'horizontal_bar', 'line', 'pie', 'donut', 'area', 'scatter', 'histogram'.
        x_key: Field name for the X-axis (categories, dates, names, or dimension).
        y_keys: Single field name (str) or list of field names for the Y-axis metrics/values.
        title: Descriptive title for the chart.
        x_label: Optional label for the X-axis.
        y_label: Optional label for the Y-axis.
        palette: Color palette name ('vibrant', 'modern', 'ocean', 'emerald', 'sunset', 'purple', 'corporate'). Default is 'vibrant'.
        show_values: Whether to show numeric data labels on chart elements. Default is True.

    Returns:
        A Markdown string with the embedded chart image and an executive data table.
    """
    try:
        df = _convert_to_dataframe(data)

        if df.empty:
            return "⚠️ No hay datos para generar la gráfica (el conjunto de datos está vacío)."

        if isinstance(y_keys, str):
            # Parse comma-separated string if provided
            y_keys = [k.strip() for k in y_keys.split(",") if k.strip()]

        if x_key not in df.columns:
            available = ", ".join(df.columns)
            return f"⚠️ La columna X '{x_key}' no existe en los datos proporcionados. Columnas disponibles: {available}"

        for yk in y_keys:
            if yk not in df.columns:
                available = ", ".join(df.columns)
                return f"⚠️ La columna Y '{yk}' no existe en los datos proporcionados. Columnas disponibles: {available}"

        # Convert numeric columns
        for yk in y_keys:
            df[yk] = pd.to_numeric(df[yk], errors="coerce")

        png_bytes = _generate_plot_image(
            df=df,
            chart_type=chart_type,
            x_key=x_key,
            y_keys=y_keys,
            title=title,
            x_label=x_label or "",
            y_label=y_label or "",
            palette_name=palette or "vibrant",
            show_values=show_values if show_values is not None else True,
        )
        chart_id = _cache_chart(png_bytes)
        image_url = f"/invocations?chart_id={chart_id}"

        # Markdown representation with embedded image. The image URL is short
        # and stable, so the model can safely copy it verbatim into its reply.
        markdown_output = [
            f"### 📊 {title}",
            "",
            f"![{title}]({image_url})",
            "",
        ]

        return "\n".join(markdown_output)

    except Exception as e:
        logger.exception("Error generating chart")
        return f"⚠️ Error al generar la gráfica: {str(e)}"
