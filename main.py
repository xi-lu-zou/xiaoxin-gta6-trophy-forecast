"""GTA VI trophy rarity forecaster (gta6-trophy-rarity-forecast-01).

Model: for each trophy, the earn rate's logit is fitted as a line in log-time
(hours since release): logit(r) = a + b * ln(1 + hours). The line is fitted per
trophy by least squares over the case's recorded reads; trophies with too
little movement borrow the cross-trophy median slope, anchored at their last
observed rate. The median forecast is the fitted line at the target time.

Spread: pooled one-step-ahead residuals of the same fit, measured on the
case's own history, give the per-root-day sigma on the logit scale; the
predictive sigma grows with the square root of the horizon in days and is
inflated for parameter uncertainty over long horizons. Quantiles are the
normal quantiles at the case's levels around the median, on the logit scale,
mapped back to percent.

Standard library only, deterministic, no network, no model calls.
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

Z = {0.05: -1.645, 0.25: -0.674, 0.5: 0.0, 0.75: 0.674, 0.95: 1.645}


def clamp_pct(x):
    return min(99.99, max(0.01, x))


def logit(pct):
    p = clamp_pct(pct)
    return math.log(p / (100.0 - p))


def inv_logit(y):
    return 100.0 / (1.0 + math.exp(-y))


def round4(x):
    scaled = x * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000
    return int(value) if float(value).is_integer() else value


def parse_ts(s):
    # "2026-11-19T03:00:00Z" or with fractional seconds; -> epoch seconds (UTC)
    import calendar
    import datetime

    dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(dt.timetuple())


def fit_line(xs, ys):
    """Least squares y = a + b*x. Returns (a, b) or None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return (my, 0.0)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return (my - b * mx, b)


def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 == 1 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def forecast_case(case):
    levels = case["levels"]
    trophies = case["trophies"]
    history = case["history"]
    if not history:
        raise ValueError("case has no history")

    t0 = parse_ts(case["game"]["release_date"] + "T00:00:00Z")
    times = [parse_ts(r["slot"]) for r in history]
    hours = [(t - t0) / 3600.0 for t in times]
    xs = [math.log1p(max(0.0, h)) for h in hours]
    target_h = (parse_ts(case["target"]) - t0) / 3600.0
    x_t = math.log1p(max(0.0, target_h))
    horizon_days = case["horizon_minutes"] / 1440.0

    n_tr = len(trophies)
    # Per-trophy series of (x, logit(rate)) over reads that list the trophy.
    series = []
    for i in range(n_tr):
        pts = []
        for j, read in enumerate(history):
            r = read["rates"][i] if i < len(read["rates"]) else None
            if r is not None:
                pts.append((xs[j], logit(r)))
        series.append(pts)

    fits = [fit_line([p[0] for p in pts], [p[1] for p in pts]) if len(pts) >= 3 else None for pts in series]
    slopes = [f[1] for f in fits if f is not None]
    global_slope = median(slopes) if slopes else 0.0

    # Pooled residuals of the per-trophy fits (in-sample), on the logit scale,
    # used as the base noise level; trophies without a fit contribute the
    # deviation of their points from the anchored global-slope line.
    resid = []
    anchors = []
    for i in range(n_tr):
        pts = series[i]
        if not pts:
            anchors.append(None)
            continue
        x_last, y_last = pts[-1]
        anchors.append(y_last)
        if fits[i] is not None:
            a, b = fits[i]
            for (x, y) in pts:
                resid.append(y - (a + b * x))
        elif len(pts) >= 2:
            for (x, y) in pts:
                resid.append(y - (y_last + global_slope * (x - x_last)))
    if resid:
        m = median(resid)
        mad = median([abs(v - m) for v in resid])
        sigma_resid = max(0.05, 1.4826 * mad)
    else:
        sigma_resid = 0.2

    # Predictive sigma: residual noise grown by sqrt(horizon), plus a term for
    # slope uncertainty that grows with the extrapolation distance in log-time.
    dx = max(0.0, x_t - xs[-1])
    sigma = math.sqrt((sigma_resid * math.sqrt(max(horizon_days, 0.02))) ** 2 + (0.35 * dx) ** 2)
    sigma = min(1.6, max(0.06, sigma))

    rows = []
    for i, trophy in enumerate(trophies):
        pts = series[i]
        if not pts:
            med = logit(0.01)
        elif fits[i] is not None:
            a, b = fits[i]
            med = a + b * x_t
        else:
            med = anchors[i] + global_slope * (x_t - pts[-1][0])
        med = min(9.2, max(-11.0, med))
        qs = []
        for lv in levels:
            z = Z.get(lv)
            if z is None:
                # normal quantile fallback (Acklam's approximation via erfcinv)
                z = math.sqrt(2.0) * _erfcinv(2.0 * (1.0 - lv)) * (1 if lv >= 0.5 else -1)
                if lv < 0.5:
                    z = -math.sqrt(2.0) * _erfcinv(2.0 * lv)
            qs.append(round4(inv_logit(med + z * sigma)))
        qs = sorted(qs)
        rows.append({"id": trophy["id"], "q": qs})
    return {"trophies": rows}


def _erfcinv(x):
    # Inverse complementary error function, Winitzki approximation refined once.
    import math as _m

    a = 0.147
    ln = _m.log(max(1e-12, x * (2.0 - x)))
    t = 2.0 / (_m.pi * a) + ln / 2.0
    v = _m.sqrt(_m.sqrt(t * t - ln / a) - t)
    return v if x < 1.0 else -v


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        out = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
