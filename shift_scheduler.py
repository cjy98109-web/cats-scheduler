#!/usr/bin/env python3
"""
CATS — Carolyn's Assignment and Tour System
--------------------------------------------
Run:  python3 shift_scheduler.py &
Then open http://localhost:8765

Upload 1: Availability survey CSV (Google Form export)
Upload 2: One-off tours CSV  (Date / Time / Event / # TGs / Notes)
Then generate the schedule.
"""

import http.server, socketserver, webbrowser, threading
import json, csv, io, re
from collections import defaultdict
from urllib.parse import urlparse


# ─────────────────────────────────────────────────────────────────────────────
# GUIDE CSV PARSING
# ─────────────────────────────────────────────────────────────────────────────

def find_col(headers, keywords):
    for i, h in enumerate(headers):
        if any(k in h.lower() for k in keywords):
            return i
    return None

def parse_guide_csv(raw_text):
    """
    Auto-detects Dartmouth form format.
    Returns (guides list, error or None).
    Each guide: {name, email, slots: [str], special_tours: [str]}
    """
    reader = csv.reader(io.StringIO(raw_text))
    rows   = list(reader)
    if len(rows) < 2:
        return [], "File appears empty."

    headers = rows[0]

    avail_col   = find_col(headers, ["check *all*", "available to give a tour weekly", "weekly"])
    special_col = find_col(headers, ["special tours", "special tour"])

    # Deduplicate by email — prefer the row that has availability data,
    # and among those keep the latest submission (last row wins).
    seen = {}
    for row in rows[1:]:
        if not any(c.strip() for c in row): continue
        key = row[1].strip().lower() if len(row) > 1 else str(id(row))
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
        else:
            # Prefer the row that has availability; if both do, take the later one
            existing_av = existing[avail_col].strip() if avail_col is not None and avail_col < len(existing) else ""
            new_av      = row[avail_col].strip()      if avail_col is not None and avail_col < len(row)      else ""
            existing_has = bool(existing_av) and "not available" not in existing_av.lower() and "off-campus" not in existing_av.lower()
            new_has      = bool(new_av)      and "not available" not in new_av.lower()      and "off-campus" not in new_av.lower()
            if new_has and not existing_has:
                seen[key] = row   # upgrade to the row that actually has availability
            elif new_has and existing_has:
                seen[key] = row   # both have it — keep latest



    def clean_slots(raw):
        if not raw or "not available" in raw.lower() or "off-campus" in raw.lower():
            return []
        return [s.strip() for s in raw.split(",") if s.strip()]

    def clean_specials(raw):
        results = []
        if not raw: return results
        for item in raw.split(","):
            item = re.sub(r"(?i)^interim:\s*", "", item).strip()
            if item and len(item) > 5:
                results.append(item)
        return results

    guides = []

    # Format A: First Name + Last Name columns (e.g. 25X, 26X)
    has_fn = any("first name" in h.lower() for h in headers)
    has_ln = any("last name"  in h.lower() for h in headers)
    if has_fn and has_ln:
        fn_col = next(i for i,h in enumerate(headers) if "first name" in h.lower())
        ln_col = next(i for i,h in enumerate(headers) if "last name"  in h.lower())
        if avail_col is None:
            return [], "Could not find the weekly availability column."
        for row in seen.values():
            fn = row[fn_col].strip() if fn_col < len(row) else ""
            ln = row[ln_col].strip() if ln_col < len(row) else ""
            if not fn and not ln: continue
            guides.append({
                "name":          f"{fn} {ln}".strip(),
                "email":         row[1].strip().lower() if len(row) > 1 else "",
                "slots":         clean_slots(row[avail_col] if avail_col < len(row) else ""),
                "special_tours": clean_specials(row[special_col] if special_col is not None and special_col < len(row) else ""),
            })
        return guides, None

    # Format B: email-derived names, avail in col[2] (26S)
    if len(headers) >= 3 and any(k in headers[2].lower() for k in ["slot","available","check"]):
        def email_to_name(email):
            local = email.split("@")[0]
            parts = local.split(".")
            if parts and len(parts[-1]) == 2 and parts[-1].isdigit():
                parts = parts[:-1]
            return " ".join(p.capitalize() for p in parts)
        for row in seen.values():
            email = row[1].strip().lower() if len(row) > 1 else ""
            guides.append({
                "name":          email_to_name(email) if email else "Unknown",
                "email":         email,
                "slots":         clean_slots(row[2] if len(row) > 2 else ""),
                "special_tours": clean_specials(row[6] if len(row) > 6 else ""),
            })
        return guides, None

    # Format C: generic Name + Yes/No
    if headers[0].strip().lower() == "name":
        slot_cols = headers[1:]
        for row in rows[1:]:
            if not row[0].strip(): continue
            slots = [col for j, col in enumerate(slot_cols)
                     if j+1 < len(row) and row[j+1].strip().lower() in ("yes","true","1")]
            guides.append({"name": row[0].strip(), "email": "", "slots": slots, "special_tours": []})
        return guides, None

    return [], "Could not detect guide CSV format."


# ─────────────────────────────────────────────────────────────────────────────
# ONE-OFF TOUR CSV PARSING
# ─────────────────────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"oct":10,"nov":11,"dec":12,
}
_MON_ABBR = ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_DAY_ABBR = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
_DAY_FULL = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

def _parse_date(raw):
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", raw)
    if m:
        mo, day = int(m.group(1)), int(m.group(2))
        try:
            from datetime import date as _d
            d = _d(2026, mo, day)
            return f"{_DAY_ABBR[d.weekday()]} {_MON_ABBR[mo]} {day}", _DAY_FULL[d.weekday()]
        except: pass
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", raw)
    if m:
        mo_n = _MONTH_MAP.get(m.group(1).lower())
        day  = int(m.group(2))
        if mo_n:
            try:
                from datetime import date as _d
                d = _d(2026, mo_n, day)
                return f"{_DAY_ABBR[d.weekday()]} {_MON_ABBR[mo_n]} {day}", _DAY_FULL[d.weekday()]
            except: pass
    return raw, ""

def _parse_time(raw):
    raw = raw.strip()
    if raw.upper() == "AM": return "9:00 AM"
    if raw.upper() == "PM": return "12:00 PM"
    raw = re.split(r"-(?=\d)", raw)[0].strip()
    m = re.match(r"^(\d{1,2})\s*([aApP][mM])$", raw.replace(" ",""))
    if m: return f"{int(m.group(1))}:00 {m.group(2).upper()}"
    m = re.match(r"(\d{1,2}):(\d{2})\s*([aApP][mM])", raw)
    if m: return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}"
    return raw or "12:00 PM"

def _parse_guides(raw):
    raw = str(raw).strip()
    if not raw: return 1, 2
    m = re.match(r"(\d+)\s*[-\u2013]\s*(\d+)", raw)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d+)", raw)
    if m: n = int(m.group(1)); return n, n
    return 1, 2

def parse_oneoff_csv(raw_text):
    """
    Parse one-off tours spreadsheet: Date / Time / Event / # TGs / Notes
    Accepts tab-separated or CSV with or without header row.
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return [], "Empty file."

    delim = "\t" if "\t" in raw_text.split("\n")[0] else ","
    reader = csv.reader(io.StringIO(raw_text), delimiter=delim)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], "No rows found."

    start = 1 if rows[0][0].strip().lower() in ("date","dates") else 0

    tours, seen_ids = [], {}
    for row in rows[start:]:
        date_raw = row[0].strip() if len(row) > 0 else ""
        time_raw = row[1].strip() if len(row) > 1 else ""
        name_raw = row[2].strip() if len(row) > 2 else ""
        tg_raw   = row[3].strip() if len(row) > 3 else ""
        note_raw = row[4].strip() if len(row) > 4 else ""
        if not date_raw or not name_raw: continue

        # Strip URLs from name
        name = re.sub(r"https?://\S+", "", name_raw).strip().strip(",").strip()

        date_str, day_name = _parse_date(date_raw)
        time_str           = _parse_time(time_raw)
        min_g, max_g       = _parse_guides(tg_raw)
        slot = f"{day_name} {time_str.lower().replace(' ','')}" if day_name else ""

        base_id = f"{date_str} | {name} | {time_str}"
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        uid = base_id if seen_ids[base_id] == 1 else f"{base_id} #{seen_ids[base_id]}"

        tours.append({
            "id":         uid,
            "date_str":   date_str,
            "name":       name,
            "time":       time_str,
            "note":       note_raw,
            "min_guides": min_g,
            "max_guides": max(min_g, max_g),
            "slot":       slot,
            "sort_key":   f"1{date_str}{time_str}",
            "is_oneoff":  True,
            "slot_key":   "",
        })

    if not tours:
        return [], "No valid rows found. Expected columns: Date, Time, Event, # TGs, Notes."
    return tours, None


# ─────────────────────────────────────────────────────────────────────────────
# SPECIAL TOUR VOLUNTEER MATCHING
# ─────────────────────────────────────────────────────────────────────────────

_MONTH_ALIASES = {
    "jan":"jan","feb":"feb","mar":"mar","apr":"apr","may":"may","jun":"jun",
    "jul":"jul","aug":"aug","sep":"sep","oct":"oct","nov":"nov","dec":"dec",
    "june":"jun","july":"jul","august":"aug","september":"sep",
    "january":"jan","february":"feb","march":"mar","april":"apr",
    "october":"oct","november":"nov","december":"dec",
}

def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))

def _month_tok(s):
    for w in re.findall(r"[a-z]+", s.lower()):
        if w in _MONTH_ALIASES: return _MONTH_ALIASES[w]
    return None

def _day_num(s):
    nums = re.findall(r"\b(\d{1,2})\b", s)
    return nums[0] if nums else None

def special_tour_volunteers(guides, tour):
    """Return guide names who signed up for this specific special tour."""
    tour_month     = _month_tok(tour["date_str"])
    tour_day       = _day_num(tour["date_str"])
    tour_time      = re.sub(r"\s+", "", tour["time"].lower())
    tour_name_toks = {t for t in _tokens(tour["name"]) if len(t) > 3}
    volunteers     = []
    for g in guides:
        for sp in g.get("special_tours", []):
            score = 0
            if tour_month and _month_tok(sp) == tour_month: score += 2
            if tour_day   and _day_num(sp)   == tour_day:   score += 3
            t_c = tour_time.replace("am","").replace("pm","").replace(":","")
            s_c = re.sub(r"\s+","",sp.lower()).replace("am","").replace("pm","").replace(":","")
            if t_c and t_c in s_c: score += 2
            score += len(tour_name_toks & _tokens(sp))
            if score >= 4:
                volunteers.append(g["name"]); break
    return sorted(set(volunteers))


# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY TOUR GENERATION FROM AVAILABILITY DATA
# ─────────────────────────────────────────────────────────────────────────────

def parse_weekly_csv(raw_text):
    """
    Parse the weekly tour grid CSV.
    Format:
      Row 1: blank, Mon, Tue, Wed, Thu, Fri (day headers)
      Row 2+: Time, [x or blank per day]  — any non-blank cell = active slot

    Returns (set of 'DayName HH:MM XM' strings, error or None).
    """
    reader = csv.reader(io.StringIO(raw_text))
    rows   = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 2:
        return set(), "Weekly tours CSV appears empty."

    # First row: day headers (skip first cell)
    day_headers = [c.strip() for c in rows[0][1:]]

    # Expand abbreviations to full day names
    _DAY_EXPAND = {
        "mon":"Monday","tue":"Tuesday","wed":"Wednesday","thu":"Thursday",
        "fri":"Friday","sat":"Saturday","sun":"Sunday",
        "monday":"Monday","tuesday":"Tuesday","wednesday":"Wednesday",
        "thursday":"Thursday","friday":"Friday","saturday":"Saturday","sunday":"Sunday",
    }
    day_names = [_DAY_EXPAND.get(d.lower(), d) for d in day_headers]

    # Normalise time string: "10:15 AM", "10:15am" -> "10:15 AM"
    def norm_time(t):
        t = t.strip()
        m = re.match(r"(\d{1,2}):(\d{2})\s*([aApP][mM])", t)
        if m: return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}"
        m = re.match(r"(\d{1,2})\s*([aApP][mM])", t)
        if m: return f"{int(m.group(1))}:00 {m.group(2).upper()}"
        return t

    active_slots = set()
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        time_str = norm_time(row[0])
        for col_idx, day_name in enumerate(day_names):
            cell = row[col_idx + 1].strip() if col_idx + 1 < len(row) else ""
            if cell:  # any non-blank value = active
                active_slots.add(f"{day_name} {time_str}")

    if not active_slots:
        return set(), "No active tour slots found. Make sure cells are non-blank for active slots."

    return active_slots, None


def build_weekly_tours(active_slots):
    """
    Build weekly tour dicts from a set of 'DayName HH:MM XM' strings.
    Every slot gets min=5, max=5 guides and name='Tour'.
    """
    _DAYS_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    def time_minutes(t):
        m = re.match(r"(\d+):(\d+)\s*(AM|PM)", t)
        if not m: return 0
        h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == "PM" and h != 12: h += 12
        if ap == "AM" and h == 12: h = 0
        return h * 60 + mn

    tours = []
    seen_ids = {}
    for slot in sorted(active_slots, key=lambda s: (
        next((i for i,d in enumerate(_DAYS_ORDER) if s.startswith(d)), 99),
        time_minutes(s.split(None, 1)[1] if " " in s else "")
    )):
        parts = slot.split(None, 1)
        if len(parts) < 2: continue
        day_name = next((d for d in _DAYS_ORDER if slot.startswith(d)), None)
        if not day_name: continue
        time_str = slot[len(day_name):].strip()
        slot_norm = f"{day_name} {time_str.lower().replace(' ','')}"
        slot_key  = f"Tour | {slot_norm}"

        base_id = f"{day_name} | Tour | {time_str}"
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        uid = base_id if seen_ids[base_id] == 1 else f"{base_id} #{seen_ids[base_id]}"

        tours.append({
            "id":         uid,
            "date_str":   day_name,
            "name":       "Tour",
            "time":       time_str,
            "note":       "",
            "min_guides": 5,
            "max_guides": 5,
            "slot":       slot_norm,
            "sort_key":   f"0{day_name}{time_str}",
            "is_oneoff":  False,
            "slot_key":   slot_key,
        })
    return tours



    slot_set = set()
    for g in guides:
        for s in g["slots"]:
            s = s.strip()
            if s and "not available" not in s.lower():
                slot_set.add(s)

    _DAYS_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    def time_minutes(t):
        m = re.match(r"(\d+):(\d+)\s*(am|pm)", t.lower().replace(" ",""))
        if not m: return 0
        h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        return h * 60 + mn

    def normalise_time(s):
        """'10:15am' -> '10:15 AM'"""
        m = re.match(r"(\d{1,2}):(\d{2})\s*([aApP][mM])", s.strip())
        if m: return f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}"
        m = re.match(r"(\d{1,2})\s*([aApP][mM])", s.strip())
        if m: return f"{int(m.group(1))}:00 {m.group(2).upper()}"
        return s.strip()

    tours = []
    seen_ids = {}
    for slot in sorted(slot_set, key=lambda s: (
        _DAYS_ORDER.index(next((d for d in _DAYS_ORDER if s.lower().startswith(d.lower())), "Sunday")),
        time_minutes(s.split(None,1)[1] if " " in s else s)
    )):
        # Parse "Monday 10:15am"
        day_name = next((d for d in _DAYS_ORDER if slot.lower().startswith(d.lower())), None)
        if not day_name: continue
        time_part = slot[len(day_name):].strip()
        time_str  = normalise_time(time_part)
        slot_norm = f"{day_name} {time_part.lower().replace(' ','')}"
        slot_key  = f"Tour | {slot_norm}"

        base_id = f"{day_name} | Tour | {time_str}"
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        uid = base_id if seen_ids[base_id] == 1 else f"{base_id} #{seen_ids[base_id]}"

        tours.append({
            "id":         uid,
            "date_str":   day_name,
            "name":       "Tour",
            "time":       time_str,
            "note":       "",
            "min_guides": 4,
            "max_guides": 6,
            "slot":       slot_norm,
            "sort_key":   f"0{day_name}{time_str}",
            "is_oneoff":  False,
            "slot_key":   slot_key,
        })
    return tours


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULING
# ─────────────────────────────────────────────────────────────────────────────

def generate_schedule(guides, tours, max_per_guide=None):
    import random
    avail        = {g["name"]: set(s.lower() for s in g["slots"]) for g in guides}
    count        = {g["name"]: 0 for g in guides}
    schedule     = {}
    volunteers   = {}
    eligible_all = {}   # tour_id -> full sorted eligible pool

    for tour in tours:
        volunteers[tour["id"]] = special_tour_volunteers(guides, tour) if tour.get("is_oneoff") else []

    slot_groups = defaultdict(list)
    for tour in tours:
        if not tour.get("is_oneoff") and tour.get("slot_key"):
            slot_groups[tour["slot_key"]].append(tour)

    work = []
    for key, occs in slot_groups.items():
        slot    = occs[0]["slot"].lower()
        min_g   = occs[0]["min_guides"]
        max_g   = occs[0].get("max_guides", min_g)
        n_weeks = len(occs)
        eligible = [g["name"] for g in guides if slot and slot in avail.get(g["name"], set())]
        work.append(("weekly", key, eligible, min_g, max_g, n_weeks))

    for tour in [t for t in tours if t.get("is_oneoff")]:
        vols  = volunteers[tour["id"]]
        min_g = tour["min_guides"]
        max_g = tour.get("max_guides", min_g)
        work.append(("special", tour["id"], vols, min_g, max_g, 1))

    random.shuffle(work)
    work.sort(key=lambda w: (len(w[2]), -w[5]))

    eligibility_count = defaultdict(int)
    for _, _, eligible, _, _, weight in work:
        for name in eligible:
            eligibility_count[name] += weight

    def pick(eligible, max_g, weight):
        if max_per_guide:
            eligible = [n for n in eligible if count.get(n, 0) + weight <= max_per_guide]
        shuffled = list(eligible)
        random.shuffle(shuffled)
        def score(n):
            return (count.get(n, 0) / max(1, eligibility_count[n]), count.get(n, 0))
        return sorted(shuffled, key=score)[:max_g]

    slot_assigned = {}
    slot_eligible = {}
    for kind, key, eligible, min_g, max_g, weight in work:
        assigned = pick(eligible, max_g, weight)
        if kind == "weekly":
            slot_assigned[key] = assigned
            slot_eligible[key] = sorted(eligible)
            for n in assigned: count[n] = count.get(n, 0) + weight
        else:
            schedule[key] = assigned
            for n in assigned: count[n] = count.get(n, 0) + 1

    for tour in tours:
        if not tour.get("is_oneoff"):
            schedule[tour["id"]]     = slot_assigned.get(tour.get("slot_key", ""), [])
            eligible_all[tour["id"]] = slot_eligible.get(tour.get("slot_key", ""), [])

    return schedule, count, volunteers, eligible_all


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CATS</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:Georgia,serif;background:#faf9f7;color:#1c1c1a;font-size:15px;line-height:1.6;}
.page{max-width:860px;margin:0 auto;padding:40px 32px 80px;}
.masthead{border-bottom:2px solid #1c1c1a;padding-bottom:14px;margin-bottom:32px;display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.masthead h1{font-size:20px;font-weight:400;font-style:italic;}
.masthead p{font-size:12px;color:#888;font-family:system-ui,sans-serif;margin-top:3px;}
.steps{display:flex;margin-bottom:36px;border:1px solid #ccc;border-radius:2px;overflow:hidden;}
.step-tab{flex:1;padding:10px 16px;font-size:11px;font-family:system-ui,sans-serif;background:#faf9f7;border:none;cursor:pointer;color:#aaa;text-align:center;border-right:1px solid #ccc;letter-spacing:.06em;text-transform:uppercase;transition:background .12s,color .12s;}
.step-tab:last-child{border-right:none;}
.step-tab.active{background:#1c1c1a;color:#faf9f7;}
.step-tab:hover:not(.active){background:#ede9e3;color:#1c1c1a;}
.pane{display:none;}.pane.active{display:block;}
.rule{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.1em;color:#aaa;border-bottom:1px solid #ddd;padding-bottom:5px;margin-bottom:14px;}
.two-up{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:20px;}
.drop-zone{border:1px solid #ccc;background:#fff;border-radius:2px;padding:28px 20px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s;}
.drop-zone:hover{border-color:#888;background:#faf9f7;}
.drop-zone.over{border-color:#1c1c1a;background:#ede9e3;}
.drop-zone.loaded{border-style:solid;border-color:#3b6d11;background:#f6faf2;}
.drop-zone i{font-size:20px;display:block;margin-bottom:8px;color:#ccc;}
.drop-zone.loaded i{color:#3b6d11;}
.dz-name{font-size:13px;font-family:system-ui,sans-serif;color:#888;}
.drop-zone.loaded .dz-name{color:#2d5b0e;font-weight:500;}
.dz-hint{font-size:11px;font-family:system-ui,sans-serif;color:#ccc;margin-top:3px;}
.drop-zone.loaded .dz-hint{color:#3b6d11;}
.field-row{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-top:4px;}
.field{display:flex;flex-direction:column;gap:4px;}
.field label{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:#aaa;}
input[type=number],select{border:1px solid #ccc;border-radius:2px;padding:7px 10px;font-size:13px;font-family:system-ui,sans-serif;background:#fff;color:#1c1c1a;outline:none;}
input:focus,select:focus{border-color:#1c1c1a;}
.btn{border:1px solid #1c1c1a;border-radius:2px;padding:8px 16px;font-size:11px;font-family:system-ui,sans-serif;background:#1c1c1a;color:#faf9f7;cursor:pointer;letter-spacing:.06em;text-transform:uppercase;display:inline-flex;align-items:center;gap:6px;}
.btn:hover{background:#333;}
.btn-out{background:#faf9f7;color:#1c1c1a;}
.btn-out:hover{background:#ede9e3;}
.btn-sm{padding:5px 11px;}
.msg-err{font-size:12px;font-family:system-ui,sans-serif;color:#8a1e1e;margin-top:6px;min-height:14px;line-height:1.5;white-space:pre-wrap;}
.msg-ok{font-size:12px;font-family:system-ui,sans-serif;color:#2d5b0e;margin-top:6px;min-height:14px;}
code{background:#eee;padding:1px 5px;border-radius:2px;font-size:11px;font-family:monospace;color:#555;}
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#ccc;border:1px solid #ccc;border-radius:2px;overflow:hidden;margin-bottom:28px;}
.sbox{background:#faf9f7;padding:16px 20px;}
.sval{font-size:28px;font-weight:400;line-height:1;letter-spacing:-.02em;font-family:Georgia,serif;}
.slbl{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:#aaa;margin-top:6px;}
.sbox.good .sval{color:#2d5b0e;}
.sbox.bad  .sval{color:#8a1e1e;}
.day-block{margin-bottom:20px;}
.day-head{font-size:12px;font-family:system-ui,sans-serif;font-weight:500;color:#1c1c1a;background:#ede9e3;padding:6px 12px;}
.trow{display:flex;gap:14px;align-items:baseline;padding:8px 12px;border-bottom:1px solid #ede9e3;font-family:system-ui,sans-serif;font-size:13px;border-left:3px solid transparent;}
.trow:last-child{border-bottom:none;}
.trow.staffed{border-left-color:#3b6d11;}
.trow.partial{border-left-color:#a06a10;}
.trow.empty  {border-left-color:#8a1e1e;}
.trow.special{border-left-color:#534ab7;}
.tname{font-weight:500;min-width:180px;flex-shrink:0;}
.ttime{color:#888;min-width:72px;flex-shrink:0;}
.tguides{flex:1;display:flex;flex-wrap:wrap;gap:4px;align-items:center;}
.tbadge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:2px;white-space:nowrap;}
.bok{background:#eaf3de;color:#27500a;}
.bwarn{background:#fcebeb;color:#791f1f;}
.bspec{background:#eeedfe;color:#3c3489;}
.bgray{background:#ede9e3;color:#666;}
.chip{display:inline-block;font-size:11px;background:#dce8f7;color:#0c447c;border-radius:2px;padding:1px 6px;}
.chipv{display:inline-block;font-size:11px;background:#eeedfe;color:#3c3489;border-radius:2px;padding:1px 6px;}
.tnote{font-size:11px;color:#bbb;font-style:italic;flex-shrink:0;}
.volline{display:flex;flex-wrap:wrap;gap:4px;align-items:center;padding:5px 12px 6px 15px;background:#fdf9ff;border-bottom:1px solid #ede9e3;font-family:system-ui,sans-serif;}
.vollbl{font-size:11px;color:#aaa;margin-right:4px;}
.guide-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));gap:10px;margin-top:12px;}
.gcard{background:#fff;border:1px solid #ccc;border-radius:2px;padding:10px 12px;}
.gcname{font-size:13px;font-weight:500;font-family:system-ui,sans-serif;margin-bottom:5px;}
.spill{display:inline-block;background:#ede9e3;color:#666;border-radius:2px;padding:1px 5px;margin:1px 2px 1px 0;font-size:10px;font-family:system-ui,sans-serif;}
.distrow{display:flex;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid #ede9e3;font-family:system-ui,sans-serif;font-size:12px;}
.distrow:last-child{border-bottom:none;}
.dname{min-width:180px;color:#444;}
.dbg{flex:1;background:#ddd;border-radius:2px;height:3px;}
.dbf{background:#1c1c1a;border-radius:2px;height:3px;}
.dcnt{min-width:58px;text-align:right;color:#999;}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:11px;font-family:system-ui,sans-serif;color:#888;margin-bottom:14px;}
.ldot{width:8px;height:8px;border-radius:1px;display:inline-block;margin-right:4px;}
.rtabs{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid #ccc;}
.rtab{font-size:11px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.07em;color:#aaa;padding:0 20px 8px 0;border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;}
.rtab.active{color:#1c1c1a;border-bottom-color:#1c1c1a;}
.rtab:hover:not(.active){color:#1c1c1a;}
.notebox{font-size:12px;font-family:system-ui,sans-serif;color:#888;background:#f5f2ec;border-left:2px solid #ccc;padding:8px 12px;margin-bottom:16px;line-height:1.6;}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:20px;}
@media(max-width:600px){.stats-row{grid-template-columns:1fr 1fr;}.two-up{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="page">

  <div class="masthead">
    <div>
      <h1>CATS</h1>
      <p>Carolyn's Assignment and Tour System &mdash; Dartmouth Admissions Office</p>
    </div>
  </div>

  <div class="steps">
    <button class="step-tab active" onclick="showPane('upload')"   id="nav-upload">1 &mdash; Upload</button>
    <button class="step-tab"        onclick="showPane('review')"   id="nav-review">2 &mdash; Review</button>
    <button class="step-tab"        onclick="showPane('schedule')" id="nav-schedule">3 &mdash; Schedule</button>
  </div>

  <!-- UPLOAD -->
  <div class="pane active" id="pane-upload">
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-bottom:20px;">

      <div>
        <div class="rule">1 &mdash; Availability survey</div>
        <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-bottom:12px;line-height:1.6;">Google Form responses CSV. Contains weekly availability and special tour sign-ups.</p>
        <div class="drop-zone" id="dz-guides">
          <i class="ti ti-users" aria-hidden="true"></i>
          <div class="dz-name" id="dz-guides-label">Drop file here or click</div>
          <div class="dz-hint" id="dz-guides-hint">Google Form responses CSV</div>
        </div>
        <input type="file" id="fi-guides" accept=".csv" style="display:none;"/>
        <p class="msg-err" id="err-guides"></p>
        <p class="msg-ok"  id="ok-guides"></p>
      </div>

      <div>
        <div class="rule">2 &mdash; Weekly tours</div>
        <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-bottom:12px;line-height:1.6;">Grid with days as columns and times as rows. Any non-blank cell = active slot (5 guides each).</p>
        <div class="drop-zone" id="dz-weekly">
          <i class="ti ti-calendar-week" aria-hidden="true"></i>
          <div class="dz-name" id="dz-weekly-label">Drop file here or click</div>
          <div class="dz-hint" id="dz-weekly-hint">Day/time grid CSV</div>
        </div>
        <input type="file" id="fi-weekly" accept=".csv" style="display:none;"/>
        <p class="msg-err" id="err-weekly"></p>
        <p class="msg-ok"  id="ok-weekly"></p>
      </div>

      <div>
        <div class="rule">3 &mdash; One-off tours</div>
        <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-bottom:12px;line-height:1.6;">Spreadsheet with columns: <code>Date</code> <code>Time</code> <code>Event</code> <code># TGs</code> <code>Notes</code>.</p>
        <div class="drop-zone" id="dz-oneoff">
          <i class="ti ti-clipboard-list" aria-hidden="true"></i>
          <div class="dz-name" id="dz-oneoff-label">Drop file here or click</div>
          <div class="dz-hint" id="dz-oneoff-hint">Date / Time / Event / # TGs / Notes</div>
        </div>
        <input type="file" id="fi-oneoff" accept=".csv,.tsv,.txt" style="display:none;"/>
        <p class="msg-err" id="err-oneoff"></p>
        <p class="msg-ok"  id="ok-oneoff"></p>
      </div>

    </div>

    <div class="field-row">
      <div class="field">
        <label>Max tours per guide (optional)</label>
        <input type="number" id="max-tours" placeholder="No limit" min="1" style="width:140px;"/>
      </div>
      <button class="btn" onclick="proceedToReview()">
        Continue to review <i class="ti ti-arrow-right" aria-hidden="true"></i>
      </button>
    </div>
  </div>

  <!-- REVIEW -->
  <div class="pane" id="pane-review">
    <div class="rtabs">
      <button class="rtab active" onclick="setRTab('weekly')"  id="rtab-weekly">Weekly tours</button>
      <button class="rtab"        onclick="setRTab('special')" id="rtab-special">One-off tours</button>
      <button class="rtab"        onclick="setRTab('guides')"  id="rtab-guides">Guides</button>
    </div>
    <div id="review-weekly"></div>
    <div id="review-special" style="display:none;"></div>
    <div id="review-guides"  style="display:none;"></div>
  </div>

  <!-- SCHEDULE -->
  <div class="pane" id="pane-schedule">
    <div class="actions">
      <button class="btn" onclick="generateSchedule()">Generate schedule</button>
      <button class="btn btn-out" onclick="exportCSV()">
        <i class="ti ti-download" aria-hidden="true"></i> Export CSV
      </button>
      <select id="filter-sel" onchange="applyFilter()">
        <option value="all">All tours</option>
        <option value="weekly">Weekly only</option>
        <option value="special">One-off only</option>
        <option value="understaffed">Understaffed only</option>
      </select>
    </div>
    <p class="msg-err" id="gen-err"></p>
    <div id="stats-area"></div>
    <div class="legend" id="legend" style="display:none;">
      <span><span class="ldot" style="background:#3b6d11;"></span>Fully staffed</span>
      <span><span class="ldot" style="background:#a06a10;"></span>Partially staffed</span>
      <span><span class="ldot" style="background:#534ab7;"></span>One-off tour</span>
      <span><span class="ldot" style="background:#8a1e1e;"></span>Understaffed</span>
    </div>
    <div id="schedule-out"></div>
    <div id="fairness-out"></div>
  </div>

</div>
<script>
document.addEventListener("DOMContentLoaded",function(){
var guides=[], weeklyTours=[], oneoffTours=[], allTours=[];
var scheduleResult={}, countResult={}, volunteersResult={}, eligibleResult={};

function showPane(p){
  document.querySelectorAll('.pane').forEach(function(el){el.classList.remove('active');});
  document.querySelectorAll('.step-tab').forEach(function(el){el.classList.remove('active');});
  document.getElementById('pane-'+p).classList.add('active');
  document.getElementById('nav-'+p).classList.add('active');
}

function makeDZ(dzId, inpId, onText){
  var dz=document.getElementById(dzId);
  var inp=document.getElementById(inpId);
  dz.addEventListener('click',function(){inp.click();});
  dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('over');});
  dz.addEventListener('dragleave',function(){dz.classList.remove('over');});
  dz.addEventListener('drop',function(e){
    e.preventDefault();dz.classList.remove('over');
    if(e.dataTransfer.files[0])readFile(e.dataTransfer.files[0],onText);
  });
  inp.addEventListener('change',function(){
    if(inp.files[0])readFile(inp.files[0],onText);
  });
}

function readFile(file,cb){
  var r=new FileReader();
  r.onload=function(e){cb(e.target.result,file.name);};
  r.readAsText(file);
}

makeDZ('dz-guides','fi-guides',function(text,name){
  fetch('/parse_guides',{method:'POST',headers:{'Content-Type':'text/plain'},body:text})
  .then(function(r){return r.json();})
  .then(function(data){
    document.getElementById('err-guides').textContent=data.error||'';
    if(data.error) return;
    guides=data.guides;
    document.getElementById('dz-guides').classList.add('loaded');
    document.getElementById('dz-guides-label').textContent=name;
    document.getElementById('dz-guides-hint').textContent='Loaded';
    document.getElementById('ok-guides').textContent=
      guides.length+' guides loaded. '+
      guides.filter(function(g){return g.slots.length>0;}).length+' with weekly availability.';
  });
});

makeDZ('dz-oneoff','fi-oneoff',function(text,name){
  fetch('/parse_oneoff',{method:'POST',headers:{'Content-Type':'text/plain'},body:text})
  .then(function(r){return r.json();})
  .then(function(data){
    document.getElementById('err-oneoff').textContent=data.error||'';
    if(data.error) return;
    oneoffTours=data.tours;
    document.getElementById('dz-oneoff').classList.add('loaded');
    document.getElementById('dz-oneoff-label').textContent=name;
    document.getElementById('dz-oneoff-hint').textContent='Loaded';
    document.getElementById('ok-oneoff').textContent=oneoffTours.length+' one-off tours loaded.';
  });

makeDZ('dz-weekly','fi-weekly',function(text,name){
  fetch('/parse_weekly',{method:'POST',headers:{'Content-Type':'text/plain'},body:text})
  .then(function(r){return r.json();})
  .then(function(data){
    document.getElementById('err-weekly').textContent=data.error||'';
    if(data.error) return;
    weeklyTours=data.tours;
    document.getElementById('dz-weekly').classList.add('loaded');
    document.getElementById('dz-weekly-label').textContent=name;
    document.getElementById('dz-weekly-hint').textContent='Loaded';
    document.getElementById('ok-weekly').textContent=data.tours.length+' weekly tour slots loaded.';
  });
});
});

function proceedToReview(){
  if(!guides.length){alert('Upload the availability survey first (file 1).');return;}
  if(!weeklyTours.length && !oneoffTours.length){alert('Upload at least one tour file (weekly tours or one-off tours).');return;}
  allTours=weeklyTours.concat(oneoffTours);
  renderReview();
  showPane('review');
}

function setRTab(v){
  ['weekly','special','guides'].forEach(function(x){
    document.getElementById('review-'+x).style.display=x===v?'block':'none';
    document.getElementById('rtab-'+x).classList.toggle('active',x===v);
  });
}

function renderReview(){
  // Weekly tours
  var DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  var h='<div class="notebox">Same guides assigned to every occurrence of each weekly slot for the term.</div>';
  DAYS.forEach(function(day){
    var items=weeklyTours.filter(function(t){return t.date_str===day;});
    if(!items.length) return;
    h+='<div class="day-block"><div class="day-head">'+day+'</div>';
    items.forEach(function(t){
      h+='<div class="trow staffed"><span class="tname">'+t.name+'</span><span class="ttime">'+t.time+'</span><span class="tguides"><span class="tbadge bok">'+t.min_guides+'&ndash;'+t.max_guides+' guides</span></span></div>';
    });
    h+='</div>';
  });
  document.getElementById('review-weekly').innerHTML=h||'<p style="font-size:13px;color:#bbb;">No weekly tours detected.</p>';

  // One-off tours
  var byD={};
  oneoffTours.forEach(function(t){if(!byD[t.date_str])byD[t.date_str]=[];byD[t.date_str].push(t);});
  var h2='';
  Object.keys(byD).forEach(function(d){
    h2+='<div class="day-block"><div class="day-head">'+d+'</div>';
    byD[d].forEach(function(t){
      h2+='<div class="trow special"><span class="tname">'+t.name+'</span><span class="ttime">'+t.time+'</span><span class="tguides"><span class="tbadge bspec">'+t.min_guides+'&ndash;'+t.max_guides+' guides</span></span>'+(t.note?'<span class="tnote">'+t.note+'</span>':'')+'</div>';
    });
    h2+='</div>';
  });
  document.getElementById('review-special').innerHTML=h2||'<p style="font-size:13px;color:#bbb;">No one-off tours uploaded.</p>';

  // Guides
  var active=guides.filter(function(g){return g.slots.length>0;});
  var h3='<p style="font-size:12px;font-family:system-ui,sans-serif;color:#999;margin-bottom:14px;">'+active.length+' guides with weekly availability.</p><div class="guide-grid">';
  active.forEach(function(g){
    h3+='<div class="gcard"><div class="gcname">'+g.name+'</div><div>'+g.slots.map(function(s){return '<span class="spill">'+s+'</span>';}).join('')+'</div>'+(g.special_tours&&g.special_tours.length?'<div style="margin-top:6px;font-size:10px;font-family:system-ui,sans-serif;color:#534ab7;">'+g.special_tours.length+' special sign-up'+(g.special_tours.length!==1?'s':'')+'</div>':'')+'</div>';
  });
  h3+='</div>';
  document.getElementById('review-guides').innerHTML=h3;
}

function generateSchedule(){
  var err=document.getElementById('gen-err');
  if(!guides.length){err.textContent='Upload the availability survey first.';return;}
  allTours=weeklyTours.concat(oneoffTours);
  if(!allTours.length){err.textContent='No tours to schedule.';return;}
  err.textContent='';
  var maxPG=parseInt(document.getElementById('max-tours').value)||null;
  fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tours:allTours,guides:guides,max_per_guide:maxPG})})
  .then(function(r){return r.json();})
  .then(function(data){
    if(data.error){err.textContent=data.error;return;}
    scheduleResult=data.schedule;countResult=data.count;volunteersResult=data.volunteers;eligibleResult=data.eligible||{};
    renderSchedule();
    showPane('schedule');
  });
}

function renderSchedule(){
  var total=allTours.length;
  var full=allTours.filter(function(t){return (scheduleResult[t.id]||[]).length>=t.min_guides;}).length;
  var empty=allTours.filter(function(t){return (scheduleResult[t.id]||[]).length===0;}).length;
  var totalA=Object.values(countResult).reduce(function(a,b){return a+b;},0);
  document.getElementById('stats-area').innerHTML=
    '<div class="stats-row">'+
    '<div class="sbox"><div class="sval">'+total+'</div><div class="slbl">Total tours</div></div>'+
    '<div class="sbox good"><div class="sval">'+full+'</div><div class="slbl">Fully staffed</div></div>'+
    '<div class="sbox '+(empty>0?'bad':'good')+'"><div class="sval">'+empty+'</div><div class="slbl">Unassigned</div></div>'+
    '<div class="sbox"><div class="sval">'+totalA+'</div><div class="slbl">Assignments</div></div>'+
    '</div>';
  document.getElementById('legend').style.display='flex';

  var byDate={};
  allTours.forEach(function(t){if(!byDate[t.date_str])byDate[t.date_str]=[];byDate[t.date_str].push(t);});
  var html='';
  Object.keys(byDate).forEach(function(ds){
    html+='<div class="day-block"><div class="day-head">'+ds+'</div>';
    byDate[ds].forEach(function(t){
      var assigned=scheduleResult[t.id]||[];
      var vols=volunteersResult[t.id]||[];
      var ok=assigned.length>=t.min_guides;
      var isfull=assigned.length>=t.max_guides;
      var rc=t.is_oneoff?'special':(!ok?'empty':(!isfull?'partial':'staffed'));
      var bc=ok?'bok':'bwarn';
      var chips=assigned.map(function(n){return '<span class="chip">'+n+'</span>';}).join('');
      var badge='<span class="tbadge '+bc+'" style="margin-left:4px;">'+assigned.length+'/'+t.min_guides+'&ndash;'+t.max_guides+'</span>';
      var specBadge=t.is_oneoff?'<span class="tbadge bspec">one-off</span>':'';
      var noteHtml=t.note?'<span class="tnote">'+t.note+'</span>':'';
      var noGuides=!assigned.length?'<span style="font-size:11px;color:#ccc;">No guides assigned</span>':'';
      var volHtml='';
      if(vols.length){
        volHtml='<div class="volline"><span class="vollbl">Signed up:</span>'+vols.map(function(n){return '<span class="chipv">'+n+'</span>';}).join('')+'</div>';
      } else if(t.is_oneoff){
        volHtml='<div class="volline"><span style="font-size:11px;color:#ccc;font-style:italic;font-family:system-ui,sans-serif;">No sign-ups recorded &mdash; assign manually.</span></div>';
      }
      if(!t.is_oneoff){
        var elig=eligibleResult[t.id]||[];
        var notAssigned=elig.filter(function(n){return assigned.indexOf(n)===-1;});
        var eligHtml='';
        if(notAssigned.length){
          eligHtml='<div class="volline"><span class="vollbl">Also available:</span>'+notAssigned.map(function(n){return '<span class="chipv">'+n+'</span>';}).join('')+'</div>';
        } else if(elig.length===0){
          eligHtml='<div class="volline"><span style="font-size:11px;color:#ccc;font-style:italic;font-family:system-ui,sans-serif;">No guides available for this slot.</span></div>';
        }
        volHtml+=eligHtml;
      }
      html+='<div class="trow '+rc+'" data-oneoff="'+t.is_oneoff+'" data-ok="'+ok+'" data-assigned="'+assigned.length+'" data-min="'+t.min_guides+'">'+
        '<span class="tname">'+t.name+'</span>'+
        '<span class="ttime">'+t.time+'</span>'+
        '<span class="tguides">'+chips+noGuides+badge+specBadge+'</span>'+
        noteHtml+'</div>'+volHtml;
    });
    html+='</div>';
  });
  document.getElementById('schedule-out').innerHTML=html;

  var withA=Object.entries(countResult).filter(function(e){return e[1]>0;}).sort(function(a,b){return b[1]-a[1];});
  var withNone=guides.filter(function(g){return !countResult[g.name]||countResult[g.name]===0;}).map(function(g){return g.name;}).sort();
  var maxC=withA.length?withA[0][1]:1;
  var fair='<div style="margin-top:32px;"><div class="rule">Distribution per guide</div>';
  withA.forEach(function(e){
    var name=e[0],c=e[1],pct=Math.round((c/maxC)*100);
    fair+='<div class="distrow"><span class="dname">'+name+'</span><div class="dbg"><div class="dbf" style="width:'+pct+'%"></div></div><span class="dcnt">'+c+' tour'+(c!==1?'s':'')+'</span></div>';
  });
  if(withNone.length){
    fair+='<div style="font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:#ccc;padding:12px 0 5px;">Not assigned</div>';
    withNone.forEach(function(name){
      fair+='<div class="distrow"><span class="dname" style="color:#ccc;">'+name+'</span><div class="dbg"></div><span class="dcnt" style="color:#ccc;">0 tours</span></div>';
    });
  }
  fair+='</div>';
  document.getElementById('fairness-out').innerHTML=fair;
  applyFilter();
}

function applyFilter(){
  var val=document.getElementById('filter-sel').value;
  document.querySelectorAll('#schedule-out .trow').forEach(function(el){
    var io=el.dataset.oneoff==='true',ok=el.dataset.ok==='true';
    var show=val==='all'?true:val==='weekly'?!io:val==='special'?io:val==='understaffed'?!ok:true;
    el.style.display=show?'':'none';
  });
  document.querySelectorAll('#schedule-out .volline').forEach(function(el){
    var prev=el.previousElementSibling;
    el.style.display=prev&&prev.style.display!=='none'?'':'none';
  });
  document.querySelectorAll('#schedule-out .day-block').forEach(function(g){
    g.style.display=[].slice.call(g.querySelectorAll('.trow')).some(function(el){return el.style.display!=='none';})?'':'none';
  });
}

function exportCSV(){
  if(!Object.keys(scheduleResult).length){document.getElementById('gen-err').textContent='Generate a schedule first.';return;}
  var csv='Date,Tour,Time,Type,Notes,Min Guides,Max Guides,Assigned Guide(s),Volunteers\n';
  allTours.forEach(function(t){
    csv+='"'+t.date_str+'","'+t.name+'","'+t.time+'","'+(t.is_oneoff?'One-off':'Weekly')+'","'+t.note+'","'+t.min_guides+'","'+t.max_guides+'","'+(scheduleResult[t.id]||[]).join('; ')+'","'+(volunteersResult[t.id]||[]).join('; ')+'"\n';
  });
  var a=document.createElement('a');
  a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download='tour_guide_schedule.csv';
  a.click();
}
}); // DOMContentLoaded
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        path   = urlparse(self.path).path

        def respond(obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        if path == "/parse_guides":
            try:
                text = body.decode("utf-8")
                guides, err = parse_guide_csv(text)
                if err:
                    respond({"error": err, "guides": []})
                else:
                    respond({"guides": guides})
            except Exception as e:
                respond({"error": str(e), "guides": []})

        elif path == "/parse_weekly":
            try:
                slots, err = parse_weekly_csv(body.decode("utf-8"))
                if err:
                    respond({"error": err, "tours": []})
                else:
                    tours = build_weekly_tours(slots)
                    respond({"tours": tours})
            except Exception as e:
                respond({"error": str(e), "tours": []})

        elif path == "/parse_oneoff":
            try:
                tours, err = parse_oneoff_csv(body.decode("utf-8"))
                respond({"error": err, "tours": tours} if err else {"tours": tours})
            except Exception as e:
                respond({"error": str(e), "tours": []})

        elif path == "/generate":
            try:
                p = json.loads(body)
                sched, count, vols, elig = generate_schedule(
                    p["guides"], p["tours"], p.get("max_per_guide")
                )
                respond({"schedule": sched, "count": count, "volunteers": vols, "eligible": elig})
            except Exception as e:
                respond({"error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args): pass


def open_browser():
    import time; time.sleep(0.6)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    import os
    PORT = int(os.environ.get("PORT", 8765))
    is_local = not os.environ.get("RAILWAY_ENVIRONMENT")
    if is_local:
        threading.Thread(target=open_browser, daemon=True).start()
        print(f"CATS  ->  http://localhost:{PORT}")
        print("Press Ctrl+C to stop.  Run with & to keep terminal free.\n")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
