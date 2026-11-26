"""Battery and cell health from stored readings (stdlib only, no database code here).

The Daly BMS reports cell voltages (1 mV), pack current (0.1 A), SOC, balancing flags and
temperatures, but no internal resistance and no state of health. Both are calculated here:

Internal resistance (IR) of every cell, at each current step
    Two consecutive readings (<= 15 s apart) with a current jump of >= 4 A, and a steady
    current before the jump: IR = change in cell voltage / change in current (mV/A = mOhm).
    The reading after the jump comes up to one poll interval later, so this is the usual
    "10-second DC IR" (ohmic + fast polarisation). Steps >= 10 A are "good"; smaller ones
    (e.g. the charger switching off at its 5 A tail) only count when there is nothing better.
    Pack voltage from the Daly is only 0.1 V resolution, too coarse for wiring resistance.

Capacity of every cell, from a full charge that started low and rested
    Charge counted in (Ah, trapezoid of current x time). Each cell's start SOC comes from its
    rested open-circuit voltage (LiFePO4 curve below; usable below ~30 % SOC, where the
    curve is steep). A charge that ends with the charger finishing CV fills every cell, so
    cell capacity = Ah in (minus that cell's balancing bleed) / (1 - start SOC).
    Pack capacity = the smallest cell (in a series pack the weakest cell limits).
    SOH = pack capacity / rated capacity (the capacity set in the BMS).

Cell status (each cell is compared with the other cells of the same pack)
    IR    >= 1.25x the other cells: Watch,  >= 1.5x: Replace  (only after 2 good current
          steps, and only when the difference is well above the 1 mV measurement noise)
    Capacity < 95 % of the other cells: Watch,  < 85 %: Replace,  < 80 % of rated: Replace
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from datetime import datetime

# Rested LiFePO4 cell OCV (mV) vs SOC (fraction) at ~25 °C (same curve as the simulator).
OCV_MV = ((0.00, 2500), (0.01, 2800), (0.03, 3000), (0.05, 3100), (0.10, 3200), (0.20, 3250),
          (0.30, 3270), (0.40, 3285), (0.50, 3295), (0.60, 3305), (0.70, 3320), (0.80, 3330),
          (0.90, 3340), (0.95, 3355), (0.98, 3380), (0.99, 3400), (1.00, 3450))
OCV_USABLE_MV = 3270          # above this (SOC > ~30 %) the curve is too flat for SOC

STEP_MIN_A = 4.0              # smallest current step used for IR
STEP_GOOD_A = 10.0            # steps this big give a trustworthy IR
STEADY_A = 1.0                # current must not change more than this before the step
MAX_GAP_S = 15.0              # readings further apart than this are not a step pair
CHG_ON_A = 2.0                # a charge starts above this
IDLE_A = 1.0                  # below this nothing is flowing
PAUSE_MAX_S = 30 * 60         # a charge resumed within this is the same charge
INTEGRATE_GAP_S = 60.0        # a longer gap in the data makes the Ah count unreliable
REST_MIN_S = 5 * 60           # rest needed before the charge for a usable OCV
TOP_MV = 3500                 # a cell above this: the charge reached the top (CV)
KNEE_MV = 3450                # order in which cells pass this = which cells fill first
TAIL_C = 0.08                 # charger finished if it stopped below this x rated current
BALANCE_A = 0.035             # Daly passive balancing bleed current
REF_DCIR_MOHM_AH = 110.0      # new prismatic LiFePO4, 10 s DCIR x capacity (1.1 mOhm @100 Ah)

IR_WATCH, IR_REPLACE = 1.25, 1.5
IR_MIN_STEPS = 2              # cells are flagged on IR only after this many good steps
CELL_MV_NOISE = 1.5           # cell voltage noise of one IR step (two 1 mV readings), mV
CAP_WATCH, CAP_REPLACE, CAP_EOL = 0.95, 0.85, 0.80
RANK = {"Good": 0, "Unknown": 0, "Watch": 1, "Replace": 2}


def soc_from_ocv(mv: float) -> float:
    """Rested cell voltage (mV) -> SOC fraction 0..1."""
    vs = [v for _, v in OCV_MV]
    if mv <= vs[0]:
        return 0.0
    if mv >= vs[-1]:
        return 1.0
    k = bisect_right(vs, mv)
    (s0, v0), (s1, v1) = OCV_MV[k - 1], OCV_MV[k]
    return s0 + (s1 - s0) * (mv - v0) / (v1 - v0)


def _t(ts) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


def reading(m: dict) -> dict:
    """Gateway telemetry message -> the compact reading analyse() works on."""
    p, c, tp = m["pack"], m["cells"], m.get("temps") or {}
    return {"t": _t(m["ts"]), "i": p["current_a"], "v": p["voltage_v"], "soc": p["soc_pct"],
            "rem": p.get("remaining_ah"), "cells": list(c["voltages_mv"]),
            "bal": list(c.get("balancing") or []), "tmax": tp.get("max_c"),
            "tmin": tp.get("min_c")}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def rated_capacity(rs: list[dict]) -> float | None:
    """The capacity set in the BMS: remaining Ah / SOC."""
    est = [r["rem"] / (r["soc"] / 100) for r in rs if r.get("rem") and (r["soc"] or 0) >= 20]
    return round(_median(est), 1) if est else None


def ir_events(rs: list[dict]) -> list[dict]:
    out = []
    for j in range(2, len(rs)):
        a0, a, b = rs[j - 2], rs[j - 1], rs[j]
        n = len(b["cells"])
        if not n or len(a["cells"]) != n or 0 in a["cells"] or 0 in b["cells"]:
            continue
        if b["t"] - a["t"] > MAX_GAP_S or a["t"] - a0["t"] > MAX_GAP_S:
            continue
        di = b["i"] - a["i"]
        if abs(di) < STEP_MIN_A or abs(a["i"] - a0["i"]) > STEADY_A:
            continue
        if abs(a["i"]) < IDLE_A:
            kind = "charger on" if b["i"] > 0 else "load on"
        elif abs(b["i"]) < IDLE_A:
            kind = "charger off" if a["i"] > 0 else "load off"
        else:
            kind = "current step"
        out.append({"t": b["t"], "kind": kind, "from_a": a["i"], "to_a": b["i"],
                    "di": round(di, 1), "soc": b["soc"], "dt_s": round(b["t"] - a["t"], 1),
                    "quality": "good" if abs(di) >= STEP_GOOD_A else "low",
                    "cells_mohm": [round((vb - va) / di, 3)
                                   for va, vb in zip(a["cells"], b["cells"])]})
    return out


def cell_ir(events: list[dict], n: int) -> tuple[list, str, int, float | None]:
    """Median IR per cell, quality, number of steps used, noise of that median (mOhm)."""
    good = [e for e in events if e["quality"] == "good" and len(e["cells_mohm"]) == n]
    use, q = (good, "good") if good else (
        [e for e in events if len(e["cells_mohm"]) == n], "low")
    if not use:
        return [None] * n, "none", 0, None
    noise = CELL_MV_NOISE / _median(abs(e["di"]) for e in use) / len(use) ** 0.5
    return ([round(_median(e["cells_mohm"][k] for e in use), 3) for k in range(n)], q,
            len(use), noise)


def charge_sessions(rs: list[dict], rated: float | None) -> list[dict]:
    sessions, cur, idle_since = [], None, None
    for j, r in enumerate(rs):
        if cur is None:
            if r["i"] >= CHG_ON_A:
                cur = _open_session(rs, j)
            continue
        a = rs[j - 1]
        dt = r["t"] - a["t"]
        if dt > INTEGRATE_GAP_S:
            cur["gaps"] += 1
        else:
            cur["ah_in"] += max(0.0, (a["i"] + r["i"]) / 2) * dt / 3600
            cur["wh_in"] += max(0.0, (a["i"] * a["v"] + r["i"] * r["v"]) / 2) * dt / 3600
            for k in a["bal"]:
                if 1 <= k <= len(cur["balance_s"]):
                    cur["balance_s"][k - 1] += dt
        _track(cur, r)
        if r["i"] >= CHG_ON_A:
            if idle_since is not None:
                cur["pauses"].append({"t": idle_since, "s": round(r["t"] - idle_since)})
            idle_since, cur["last_chg"] = None, r
        elif r["i"] < IDLE_A:
            if idle_since is None:
                idle_since = r["t"]
            if cur["top"] or r["t"] - idle_since > PAUSE_MAX_S or r["i"] < -IDLE_A:
                sessions.append(_close_session(cur, rated))
                cur, idle_since = None, None
    if cur is not None:
        sessions.append(_close_session(cur, rated, open_=True))
    return sessions


def _open_session(rs, j):
    r, n = rs[j], len(rs[j]["cells"])
    k = j - 1
    while k >= 0 and abs(rs[k]["i"]) < 0.5 and (k == j - 1 or rs[k + 1]["t"] - rs[k]["t"] <= INTEGRATE_GAP_S):
        k -= 1
    pre = rs[j - 1] if j else None
    rest = (pre["t"] - rs[k + 1]["t"]) if pre and k + 1 <= j - 1 else 0.0
    return {"start": pre["t"] if pre else r["t"], "pre": pre, "rest_s": rest, "ah_in": 0.0,
            "wh_in": 0.0, "gaps": 0, "balance_s": [0.0] * n, "knee": [], "top": False,
            "pauses": [], "last_chg": r, "end": r["t"], "tmax": r["tmax"],
            "tstart": pre["tmax"] if pre else r["tmax"], "max_delta": 0, "peak": [0] * n,
            "start_soc": pre["soc"] if pre else r["soc"]}


def _track(cur, r):
    cells = r["cells"]
    cur["end"] = r["t"]
    if r["tmax"] is not None:
        cur["tmax"] = max(cur["tmax"] or r["tmax"], r["tmax"])
    good = [c for c in cells if c]
    if good:
        cur["max_delta"] = max(cur["max_delta"], max(good) - min(good))
    for k, mv in enumerate(cells):
        if k < len(cur["peak"]):
            cur["peak"][k] = max(cur["peak"][k], mv)
        if mv >= KNEE_MV and r["i"] > 0 and (k + 1) not in cur["knee"]:
            cur["knee"].append(k + 1)
        if mv >= TOP_MV and r["i"] > 0:
            cur["top"] = True
    cur["end_soc"] = r["soc"]


def _close_session(cur, rated, open_=False):
    tail = cur["last_chg"]["i"]
    full = bool(not open_ and cur["top"] and rated and tail <= TAIL_C * rated)
    s = {"start": cur["start"], "end": cur["end"], "duration_s": round(cur["end"] - cur["start"]),
         "ah_in": round(cur["ah_in"], 2), "wh_in": round(cur["wh_in"], 1),
         "start_soc": cur["start_soc"], "end_soc": cur.get("end_soc"),
         "rest_before_s": round(cur["rest_s"]), "pauses": cur["pauses"],
         "in_progress": open_, "full": full, "tail_a": tail,
         "ended": ("in progress" if open_ else
                   f"charger finished at {tail:g} A" if full else
                   f"stopped at {tail:g} A before full" if cur["top"] else
                   "stopped before the top of charge"),
         "balance_s": [round(x) for x in cur["balance_s"]], "knee_order": cur["knee"],
         "peak_mv": cur["peak"], "max_delta_mv": cur["max_delta"],
         "temp_start_c": cur["tstart"], "temp_max_c": cur["tmax"], "gaps": cur["gaps"],
         "start_cells_mv": cur["pre"]["cells"] if cur["pre"] else None,
         "cell_capacity_ah": None, "capacity_note": None}
    pre = cur["pre"]
    if not full:
        s["capacity_note"] = "needs a charge that runs until the charger finishes"
    elif cur["gaps"]:
        s["capacity_note"] = "gaps in the data during the charge"
    elif cur["rest_s"] < REST_MIN_S or not pre:
        s["capacity_note"] = f"needs {REST_MIN_S // 60} min rest before the charge"
    elif max(pre["cells"]) > OCV_USABLE_MV:
        s["capacity_note"] = "started too full (needs a start below ~30 % SOC)"
    else:
        socs = [soc_from_ocv(mv) for mv in pre["cells"]]
        s["start_cell_soc_pct"] = [round(x * 100, 1) for x in socs]
        s["cell_capacity_ah"] = [round((cur["ah_in"] - BALANCE_A * b / 3600) / (1 - x), 2)
                                 for x, b in zip(socs, cur["balance_s"])]
    return s


def _vs_others(xs: list, k: int):
    others = [x for i, x in enumerate(xs) if i != k and x is not None]
    if xs[k] is None or not others:
        return None
    return xs[k] / statistics.median(others)


def analyse(rs: list[dict], rated: float | None = None) -> dict:
    """Health of one battery from its readings (oldest first)."""
    rs = [r for r in rs if r.get("cells")]
    if not rs:
        return {"readings": 0}
    n = len(rs[-1]["cells"])
    rated = rated or rated_capacity(rs)
    events = ir_events(rs)
    irs, ir_q, ir_n, ir_noise = cell_ir(events, n)
    ir_firm = ir_q == "good" and ir_n >= IR_MIN_STEPS
    sessions = charge_sessions(rs, rated)
    cap_s = next((s for s in reversed(sessions) if s["cell_capacity_ah"]), None)
    caps = cap_s["cell_capacity_ah"] if cap_s else [None] * n
    last_s = sessions[-1] if sessions else None

    cells = []
    for k in range(n):
        st, why = "Good", []
        ir_r, cap_r = _vs_others(irs, k), _vs_others(caps, k)
        ir_flag = ir_firm and ir_r is not None and (
            irs[k] - statistics.median(x for i, x in enumerate(irs) if i != k)) > 3 * ir_noise
        if ir_flag and ir_r >= IR_REPLACE:
            st = "Replace"
            why.append(f"internal resistance {ir_r:.2f}x the other cells")
        elif ir_flag and ir_r >= IR_WATCH:
            st = "Watch"
            why.append(f"internal resistance {ir_r:.2f}x the other cells")
        if cap_r is not None:
            pct_rated = caps[k] / rated if rated else None
            if cap_r < CAP_REPLACE or (pct_rated is not None and pct_rated < CAP_EOL):
                st = "Replace"
                why.append(f"capacity {cap_r * 100:.0f} % of the other cells")
            elif cap_r < CAP_WATCH:
                st = max(st, "Watch", key=RANK.get)
                why.append(f"capacity {cap_r * 100:.0f} % of the other cells")
        if (ir_r is None or not ir_firm) and cap_r is None:
            st = "Unknown"
        cells.append({
            "cell": k + 1, "status": st, "reasons": why,
            "voltage_mv": rs[-1]["cells"][k],
            "ir_mohm": irs[k], "ir_vs_others": None if ir_r is None else round(ir_r, 2),
            "capacity_ah": caps[k], "capacity_vs_others": None if cap_r is None else round(cap_r, 3),
            "soh_pct": round(caps[k] / rated * 100, 1) if caps[k] and rated else None,
            "balance_s": last_s["balance_s"][k] if last_s and k < len(last_s["balance_s"]) else None,
            "knee_rank": (last_s["knee_order"].index(k + 1) + 1
                          if last_s and (k + 1) in last_s["knee_order"] else None),
        })

    known = [c for c in caps if c]
    pack_cap = min(known) if known else None
    med_ir = _median(irs)
    ref_ir = REF_DCIR_MOHM_AH / rated if rated else None
    soh = round(pack_cap / rated * 100, 1) if pack_cap and rated else None
    worst = max((c["status"] for c in cells), key=RANK.get, default="Unknown")
    if soh is not None and soh < 80:
        worst = "Replace"
    elif soh is not None and soh < 90:
        worst = max(worst, "Watch", key=RANK.get)
    weak = [c for c in cells if c["status"] in ("Watch", "Replace")]
    return {
        "readings": len(rs), "from": rs[0]["t"], "to": rs[-1]["t"], "cells": n,
        "rated_ah": rated,
        "soh_pct": soh, "pack_capacity_ah": pack_cap,
        "soh_if_replaced_pct": (round(sorted(known)[len(known) // 2] / rated * 100, 1)
                                if weak and known and rated else None),
        "capacity_from": cap_s and {"start": cap_s["start"], "end": cap_s["end"]},
        "ir": {"cell_mohm": irs, "quality": ir_q, "steps": ir_n,
               "noise_mohm": round(ir_noise, 3) if ir_noise else None,
               "note": (None if ir_firm else
                        "no current step of 10 A or more yet" if ir_q != "good" else
                        f"only {ir_n} current step so far: cells are flagged after {IR_MIN_STEPS}"),
               "median_mohm": med_ir,
               "pack_cells_mohm": round(sum(x for x in irs if x), 3) if any(irs) else None,
               "new_cell_ref_mohm": round(ref_ir, 3) if ref_ir else None,
               "rise_vs_new_pct": (round((med_ir / ref_ir - 1) * 100) if med_ir and ref_ir
                                   else None)},
        "status": worst if any(c["status"] != "Unknown" for c in cells) or soh else "Unknown",
        "abnormal_cells": [c["cell"] for c in weak],
        "cell_health": cells,
        "ir_events": events[-50:],
        "charges": sessions[-10:],
    }
