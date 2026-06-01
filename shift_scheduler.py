#!/usr/bin/env python3
"""
CATS — Dartmouth Admissions
--------------------------------------------
Run:  python3 shift_scheduler.py &
Then open http://localhost:8765 in your browser.

Each quarter:
  1. Download the tour template, fill it in, save as CSV.
  2. Drop in the tour CSV + the Google Form availability CSV.
  3. Generate the schedule.
"""

import http.server
import socketserver
import webbrowser
import threading
import json
import csv
import io
import re
from urllib.parse import urlparse
from collections import defaultdict

PORT = 8765

# ─────────────────────────────────────────────────────────────────────────────
# TOUR CSV PARSING
# ─────────────────────────────────────────────────────────────────────────────
#
# TWO-SECTION FORMAT (matches the template generated below):
#
# Section 1 — WEEKLY TOURS grid
#   Row 1: header row starting with "WEEKLY TOURS"
#   Row 2: day names  → Mon, Tue, Wed, Thu, Fri  (up to 7 days)
#   Row 3: times      → 10:15 AM, 11:30 AM, …   (one time per column)
#   Row 4: tour name  → Tour, Tour, Tour, …       (one name per column)
#   Row 5: min guides → 4, 4, 4, …
#   Row 6: max guides → 6, 6, 6, …
#   (repeat rows 3-6 for each additional time block)
#
# Section 2 — ONE-OFF TOURS flat list
#   Starts at a row where col[0] == "ONE-OFF TOURS"
#   Followed by a header row: Tour Name, Date, Day of Week, Time,
#                              Min Guides, Max Guides, Notes
#   Then one row per special tour.

DAYS_FULL = {
    "mon":"Monday","tue":"Tuesday","wed":"Wednesday","thu":"Thursday",
    "fri":"Friday","sat":"Saturday","sun":"Sunday",
    "monday":"Monday","tuesday":"Tuesday","wednesday":"Wednesday",
    "thursday":"Thursday","friday":"Friday","saturday":"Saturday","sunday":"Sunday",
}

def _cell(row, i, default=""):
    return row[i].strip() if i < len(row) else default

def parse_tour_csv(raw_text):
    """
    Parse the two-section tour template CSV.
    Returns (tours list, error string or None).
    """
    reader = csv.reader(io.StringIO(raw_text))
    all_rows = list(reader)

    # Split into weekly section and one-off section
    weekly_rows = []
    oneoff_rows = []
    in_oneoff   = False

    for row in all_rows:
        if not any(c.strip() for c in row):
            continue
        first = row[0].strip().lower()
        if first == "one-off tours":
            in_oneoff = True
            continue
        if first == "weekly tours":
            in_oneoff = False
            continue
        if in_oneoff:
            oneoff_rows.append(row)
        else:
            weekly_rows.append(row)

    tours    = []
    seen_ids = {}

    def add_tour(date_str, name, time, note, min_g, max_g, is_regular, day_name):
        slot     = f"{day_name} {time.strip().lower().replace(' ','')}" if day_name else ""
        slot_key = f"{name} | {slot}" if is_regular else ""
        base_id  = f"{date_str} | {name} | {time}"
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        uid = base_id if seen_ids[base_id] == 1 else f"{base_id} #{seen_ids[base_id]}"
        tours.append({
            "id":         uid,
            "date_str":   date_str,
            "name":       name,
            "time":       time,
            "note":       note,
            "min_guides": min_g,
            "max_guides": max(min_g, max_g),
            "slot":       slot,
            "sort_key":   ("0" if is_regular else "1") + date_str + time,
            "is_oneoff":  not is_regular,
            "slot_key":   slot_key,
        })

    # ── Parse weekly grid ─────────────────────────────────────────────────────
    # Rows are grouped in blocks of 4: Time / Tour Name / Min Guides / Max Guides
    # First row = day names header
    if weekly_rows:
        day_row  = weekly_rows[0]
        day_names = []
        for c in day_row:
            c = c.strip().lower()
            day_names.append(DAYS_FULL.get(c, ""))

        i = 1
        while i < len(weekly_rows):
            # Expect: time row, name row, min row, max row
            if i + 3 >= len(weekly_rows) + 1:
                break
            time_row = weekly_rows[i]     if i     < len(weekly_rows) else []
            name_row = weekly_rows[i + 1] if i + 1 < len(weekly_rows) else []
            min_row  = weekly_rows[i + 2] if i + 2 < len(weekly_rows) else []
            max_row  = weekly_rows[i + 3] if i + 3 < len(weekly_rows) else []

            # Detect if this looks like a time row (first non-empty cell contains AM/PM or colon)
            first_time = _cell(time_row, 0)
            if not first_time or ("am" not in first_time.lower() and
                                  "pm" not in first_time.lower() and
                                  ":" not in first_time):
                i += 1
                continue

            for col_idx, day_name in enumerate(day_names):
                if not day_name:
                    continue
                time_val = _cell(time_row, col_idx)
                name_val = _cell(name_row, col_idx) or "Tour"
                min_val  = int(_cell(min_row,  col_idx) or 1)
                max_val  = int(_cell(max_row,  col_idx) or min_val)
                if not time_val:
                    continue
                add_tour(
                    date_str  = day_name,   # weekly slots use day name as date_str
                    name      = name_val,
                    time      = time_val,
                    note      = "",
                    min_g     = min_val,
                    max_g     = max_val,
                    is_regular= True,
                    day_name  = day_name,
                )
            i += 4

    # ── Parse one-off list ────────────────────────────────────────────────────
    if oneoff_rows:
        # Detect whether first row is a header or data.
        # A header row contains recognisable keyword like "tour name", "date", "time".
        first = [c.strip().lower() for c in oneoff_rows[0]]
        is_header = any(k in first for k in ["tour name","name","date","time"])

        if is_header:
            hdr  = first
            data = oneoff_rows[1:]
            def hcol(keys):
                for k in keys:
                    if k in hdr: return hdr.index(k)
                return None
            c_name = hcol(["tour name","name","tour"])
            c_date = hcol(["date"])
            c_day  = hcol(["day of week","day","weekday"])
            c_time = hcol(["time"])
            c_min  = hcol(["min guides","min","minimum"])
            c_max  = hcol(["max guides","max","maximum"])
            c_note = hcol(["notes","note","special notes"])
        else:
            # No header — assume fixed column order from the template:
            # Tour Name | Date | Day of Week | Time | Min Guides | Max Guides | Notes
            data   = oneoff_rows
            c_name, c_date, c_day, c_time, c_min, c_max, c_note = 0, 1, 2, 3, 4, 5, 6

        for row in data:
            if not any(c.strip() for c in row): continue
            name     = _cell(row, c_name)
            date_str = _cell(row, c_date)
            day      = _cell(row, c_day).strip().capitalize()
            time_val = _cell(row, c_time)
            try:    min_g = int(_cell(row, c_min) or 1)
            except: min_g = 1
            try:    max_g = int(_cell(row, c_max) or min_g)
            except: max_g = min_g
            note     = _cell(row, c_note) if c_note is not None else ""
            day_full = DAYS_FULL.get(day.lower(), day)
            if not name or not date_str or not time_val:
                continue
            add_tour(date_str, name, time_val, note, min_g, max_g,
                     is_regular=False, day_name=day_full)

    if not tours:
        return [], (
            "No tours found. Make sure the CSV has a WEEKLY TOURS section "
            "with day names, then groups of: Time / Tour Name / Min Guides / Max Guides. "
            "One-off tours go after a ONE-OFF TOURS header row."
        )

    # Sort: weekly first (by day order), then one-offs by date
    day_order = list(DAYS_FULL.values())
    def sort_key(t):
        if not t["is_oneoff"]:
            try:    return (0, day_order.index(t["date_str"]), t["time"])
            except: return (0, 99, t["time"])
        return (1, t["sort_key"])
    tours.sort(key=sort_key)
    return tours, None


# ── Template CSV ──────────────────────────────────────────────────────────────
# Two-section format:
#   Section 1: WEEKLY TOURS grid
#   Section 2: ONE-OFF TOURS flat list
#
# In the weekly grid, each time block is 4 rows:
#   Row 1: Time       (one per column, repeated across all days)
#   Row 2: Tour Name  (e.g. "Tour" — change if the tour has a specific name)
#   Row 3: Min Guides
#   Row 4: Max Guides
# Add or remove columns for days; add or remove 4-row blocks for time slots.

TEMPLATE_CSV = \
"""WEEKLY TOURS,,,,,
,Mon,Tue,Wed,Thu,Fri
10:15 AM,10:15 AM,10:15 AM,10:15 AM,10:15 AM,10:15 AM
Tour Name,Tour,Tour,Tour,Tour,Tour
Min Guides,4,4,4,4,4
Max Guides,6,6,6,6,6
11:30 AM,11:30 AM,11:30 AM,11:30 AM,11:30 AM,11:30 AM
Tour Name,Tour,Tour,Tour,Tour,Tour
Min Guides,4,4,4,4,4
Max Guides,6,6,6,6,6
2:15 PM,2:15 PM,2:15 PM,2:15 PM,2:15 PM,2:15 PM
Tour Name,Tour,Tour,Tour,Tour,Tour
Min Guides,4,4,4,4,4
Max Guides,6,6,6,6,6
3:30 PM,3:30 PM,3:30 PM,3:30 PM,3:30 PM,3:30 PM
Tour Name,Tour,Tour,Tour,Tour,Tour
Min Guides,4,4,4,4,4
Max Guides,6,6,6,6,6
,,,,,
ONE-OFF TOURS,,,,,
Tour Name,Date,Day of Week,Time,Min Guides,Max Guides,Notes
Reunions Tour,Fri Jun 20,Friday,10:00 AM,2,4,
Upward Bound (UVM),Fri Jun 27,Friday,11:15 AM,2,4,
Virtual Tour,Wed Jul 9,Wednesday,3:00 PM,2,4,
"""

# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT TOURS FROM GUIDE CSV
# ─────────────────────────────────────────────────────────────────────────────
# Reads the guide availability CSV and generates a pre-filled tour template
# based on the weekly slots and special tours guides have signed up for.

_DAYS_ORDER = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
_MONTHS_MAP = {
    'january':'Jan','february':'Feb','march':'Mar','april':'Apr','may':'May',
    'june':'Jun','july':'Jul','august':'Aug','september':'Sep',
    'october':'Oct','november':'Nov','december':'Dec',
}

def _parse_special_string(s):
    """Parse a free-text special tour string into structured fields."""
    s = re.sub(r'(?i)^interim:\s*', '', s).strip()
    day = next((d for d in _DAYS_ORDER if s.startswith(d)), None)
    if not day:
        return None
    rest = s[len(day):].strip().lstrip(',- ').strip()

    # Month
    month = None
    for m_full, m_abbr in _MONTHS_MAP.items():
        if m_full in rest.lower():
            month = m_abbr
            rest = re.sub(m_full, '', rest, flags=re.IGNORECASE).strip()
            break

    # Day number
    day_num_m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', rest)
    day_num = day_num_m.group(1) if day_num_m else ''
    if day_num_m:
        rest = (rest[:day_num_m.start()] + rest[day_num_m.end():]).strip()

    # Time
    time_m = re.search(r'(\d{1,2}:\d{2}\s*[aApP][mM])', rest)
    time_str = ''
    if time_m:
        raw_t = time_m.group(1).strip()
        t = re.match(r'(\d{1,2}):(\d{2})\s*([aApP][mM])', raw_t)
        if t:
            time_str = f"{int(t.group(1))}:{t.group(2)} {t.group(3).upper()}"
        rest = (rest[:time_m.start()] + rest[time_m.end():]).strip()

    # Tour name — strip leading dashes/spaces
    name = re.sub(r'^[\s\-]+', '', rest).strip()
    if not name:
        name = 'Special Tour'

    date_str = f"{day[:3]} {month} {day_num}" if month and day_num else s[:20].strip()
    return {
        'name':     name,
        'date_str': date_str,
        'day':      day,
        'time':     time_str or '12:00 PM',
    }


def extract_tours_from_guides(raw_text, extra_specials_text=""):
    """
    Parse the guide CSV and return a pre-filled tour template CSV string.
    Weekly slots come from the availability column responses.
    Special tours come from both the sign-up column AND the extra_specials_text
    (pasted directly from the Google Form options), merged and deduplicated.
    """
    reader = csv.reader(io.StringIO(raw_text))
    rows   = list(reader)
    if len(rows) < 2:
        return None, "File appears empty."

    headers = rows[0]
    avail_col   = find_col(headers, ["check *all*", "available to give a tour weekly", "weekly"])
    special_col = find_col(headers, ["special tours", "special tour"])

    if avail_col is None:
        return None, "Could not find the weekly availability column."

    # Collect all unique weekly slots
    weekly_slots = set()
    special_strings = set()

    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        avail_raw = row[avail_col].strip() if avail_col < len(row) else ''
        if avail_raw and 'not available' not in avail_raw.lower() and 'off-campus' not in avail_raw.lower():
            for s in avail_raw.split(','):
                s = s.strip()
                if s:
                    weekly_slots.add(s)

        if special_col is not None:
            spec_raw = row[special_col].strip() if special_col < len(row) else ''
            if spec_raw:
                for s in spec_raw.split(','):
                    s = s.strip()
                    if s and len(s) > 5:
                        special_strings.add(s)

    # Merge in the pasted extra specials (one per line or comma-separated)
    if extra_specials_text:
        for line in re.split(r'[\n,]', extra_specials_text):
            line = line.strip()
            if line and len(line) > 5:
                special_strings.add(line)

    if not weekly_slots and not special_strings:
        return None, "No availability data found in the guide CSV."

    # ── Build weekly grid ─────────────────────────────────────────────────────
    # Group slots by time, collect which days have each time
    # slot format: "Monday 10:15am"
    time_day_map = {}   # normalised_time -> set of day names
    for slot in weekly_slots:
        for day in _DAYS_ORDER:
            if slot.lower().startswith(day.lower()):
                time_part = slot[len(day):].strip().lower().replace(' ', '')
                # Normalise time: "10:15am" -> "10:15 AM"
                tm = re.match(r'(\d{1,2}):(\d{2})([aApP][mM])', time_part)
                if tm:
                    norm = f"{int(tm.group(1))}:{tm.group(2)} {tm.group(3).upper()}"
                    if norm not in time_day_map:
                        time_day_map[norm] = set()
                    time_day_map[norm].add(day)
                break

    # Sort times
    def time_sort(t):
        m = re.match(r'(\d+):(\d+)\s*(AM|PM)', t)
        if not m: return 0
        h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap == 'PM' and h != 12: h += 12
        if ap == 'AM' and h == 12: h = 0
        return h * 60 + mn

    sorted_times = sorted(time_day_map.keys(), key=time_sort)

    # Build header row: find all days present
    days_present = []
    for d in _DAYS_ORDER:
        if any(d in days for days in time_day_map.values()):
            days_present.append(d)

    day_abbrs = [d[:3] for d in days_present]

    lines = ['WEEKLY TOURS' + ',' * len(days_present)]
    lines.append(',' + ','.join(day_abbrs))

    for norm_time in sorted_times:
        days_with_time = time_day_map[norm_time]
        time_row = [norm_time] + [norm_time if d in days_with_time else '' for d in days_present]
        name_row = ['Tour Name'] + ['Tour' if d in days_with_time else '' for d in days_present]
        min_row  = ['Min Guides'] + ['4' if d in days_with_time else '' for d in days_present]
        max_row  = ['Max Guides'] + ['6' if d in days_with_time else '' for d in days_present]
        lines.append(','.join(time_row))
        lines.append(','.join(name_row))
        lines.append(','.join(min_row))
        lines.append(','.join(max_row))

    lines.append('')

    # ── Build one-off section ─────────────────────────────────────────────────
    lines.append('ONE-OFF TOURS' + ',' * len(days_present))
    lines.append('Tour Name,Date,Day of Week,Time,Min Guides,Max Guides,Notes')

    # Parse and deduplicate special tours
    parsed_specials = {}
    for s in special_strings:
        p = _parse_special_string(s)
        if p:
            key = f"{p['date_str']}|{p['name']}|{p['time']}"
            parsed_specials[key] = p

    # Sort by date_str then time
    def special_sort(p):
        mo_map = {v:i for i,v in enumerate(_MONTHS_MAP.values())}
        d = p['date_str']
        day_n = re.search(r'\d+', d.split()[-1] if d.split() else '0')
        mo_s  = d.split()[1] if len(d.split()) > 1 else ''
        return (mo_map.get(mo_s, 99), int(day_n.group()) if day_n else 0, p['time'])

    for p in sorted(parsed_specials.values(), key=special_sort):
        lines.append(f"{p['name']},{p['date_str']},{p['day']},{p['time']},2,4,")

    return '\n'.join(lines) + '\n', None



def find_col(headers, keywords):
    for i, h in enumerate(headers):
        if any(k in h.lower() for k in keywords):
            return i
    return None

def parse_special_tours(raw):
    if not raw or "not available" in raw.lower():
        return []
    skip = {"various","mock","refresher","monday","tuesday","wednesday",
            "thursday","friday","saturday","sunday"}
    results = []
    for item in raw.split(","):
        item = re.sub(r"(?i)^interim:\s*", "", item).strip()
        if not item: continue
        if any(s in item.lower() for s in skip) and len(item) < 15: continue
        results.append(item)
    return results

def parse_guide_csv(raw_text):
    reader = csv.reader(io.StringIO(raw_text))
    rows   = list(reader)
    if len(rows) < 2:
        return [], "File appears empty or has only a header row."

    headers = rows[0]

    seen = {}
    for row in rows[1:]:
        if not any(c.strip() for c in row): continue
        key = row[1].strip().lower() if len(row) > 1 else ""
        seen[key or id(row)] = row

    avail_col   = find_col(headers, ["slot","available to give a tour","check *all*","weekly"])
    special_col = find_col(headers, ["special tours","special tour"])

    def clean(s): return s.strip()

    # Format A: First Name + Last Name columns (e.g. 25X)
    has_fn = any("first name" in h.lower() for h in headers)
    has_ln = any("last name"  in h.lower() for h in headers)
    if has_fn and has_ln:
        fn_col = next(i for i,h in enumerate(headers) if "first name" in h.lower())
        ln_col = next(i for i,h in enumerate(headers) if "last name"  in h.lower())
        if avail_col is None:
            return [], "Could not find the availability column."
        guides = []
        for row in seen.values():
            fn = clean(row[fn_col]) if fn_col < len(row) else ""
            ln = clean(row[ln_col]) if ln_col < len(row) else ""
            if not fn and not ln: continue
            name        = f"{fn} {ln}".strip()
            email       = row[1].strip().lower() if len(row) > 1 else ""
            avail_raw   = row[avail_col].strip()   if avail_col   < len(row) else ""
            special_raw = row[special_col].strip() if special_col is not None and special_col < len(row) else ""
            slots = []
            if avail_raw and "not available" not in avail_raw.lower():
                slots = [s.strip() for s in avail_raw.split(",") if s.strip()]
            guides.append({"name":name,"email":email,"slots":slots,
                           "special_tours":parse_special_tours(special_raw)})
        return guides, None

    # Format B: email-derived names, avail in col[2] (e.g. 26S)
    if len(headers) >= 3 and any(k in headers[2].lower() for k in ["slot","available","check"]):
        def email_to_name(email):
            local = email.split("@")[0]
            parts = local.split(".")
            if parts and len(parts[-1]) == 2 and parts[-1].isdigit():
                parts = parts[:-1]
            return " ".join(p.capitalize() for p in parts)
        sp_col_b = 6
        guides = []
        for row in seen.values():
            email       = row[1].strip().lower() if len(row) > 1 else ""
            name        = email_to_name(email) if email else "Unknown"
            avail_raw   = row[2].strip() if len(row) > 2 else ""
            special_raw = row[sp_col_b].strip() if len(row) > sp_col_b else ""
            slots = []
            if avail_raw and "not available" not in avail_raw.lower():
                slots = [s.strip() for s in avail_raw.split(",") if s.strip()]
            guides.append({"name":name,"email":email,"slots":slots,
                           "special_tours":parse_special_tours(special_raw)})
        return guides, None

    # Format C: generic Name + Yes/No columns
    if headers[0].strip().lower() == "name":
        slot_cols = headers[1:]
        guides = []
        for row in rows[1:]:
            if not row[0].strip(): continue
            name  = row[0].strip()
            slots = []
            for j, col in enumerate(slot_cols):
                val = (row[j+1] if j+1 < len(row) else "").strip().lower()
                if val in ("yes","true","1"): slots.append(col)
            guides.append({"name":name,"email":"","slots":slots,"special_tours":[]})
        return guides, None

    return [], "Could not detect guide CSV format."


# ─────────────────────────────────────────────────────────────────────────────
# SPECIAL TOUR VOLUNTEER MATCHING
# ─────────────────────────────────────────────────────────────────────────────

MONTH_ALIASES = {
    "jan":"jan","feb":"feb","mar":"mar","apr":"apr","may":"may","jun":"jun",
    "jul":"jul","aug":"aug","sep":"sep","oct":"oct","nov":"nov","dec":"dec",
    "june":"jun","july":"jul","august":"aug","september":"sep",
    "january":"jan","february":"feb","march":"mar","april":"apr",
    "october":"oct","november":"nov","december":"dec",
}

def _tokens(s):    return set(re.findall(r"[a-z0-9]+", s.lower()))
def _month_tok(s):
    for w in re.findall(r"[a-z]+", s.lower()):
        if w in MONTH_ALIASES: return MONTH_ALIASES[w]
    return None
def _day_num(s):
    nums = re.findall(r"\b(\d{1,2})\b", s)
    return nums[0] if nums else None

def special_tour_volunteers(guides, tour):
    tour_month     = _month_tok(tour["date_str"])
    tour_day       = _day_num(tour["date_str"])
    tour_time      = re.sub(r"\s+","", tour["time"].lower())
    tour_name_toks = {t for t in _tokens(tour["name"]) if len(t) > 3}
    volunteers     = []
    for g in guides:
        for sp in g.get("special_tours", []):
            sp_month = _month_tok(sp)
            sp_day   = _day_num(sp)
            sp_time  = re.sub(r"\s+","", sp.lower())
            sp_toks  = _tokens(sp)
            score = 0
            if tour_month and sp_month and tour_month == sp_month: score += 2
            if tour_day   and sp_day   and tour_day   == sp_day:   score += 3
            t_c = tour_time.replace("am","").replace("pm","").replace(":","")
            s_c = sp_time.replace("am","").replace("pm","").replace(":","")
            if t_c and t_c in s_c: score += 2
            score += len(tour_name_toks & sp_toks)
            if score >= 4:
                volunteers.append(g["name"]); break
    return sorted(set(volunteers))


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULING
# ─────────────────────────────────────────────────────────────────────────────

def generate_schedule(guides, tours, max_per_guide=None):
    """
    Global fairness scheduling with randomised tie-breaking.
    Guides with equal load scores are shuffled so no name is
    systematically preferred over another across runs.
    """
    import random
    avail      = {g["name"]: set(s.lower() for s in g["slots"]) for g in guides}
    count      = {g["name"]: 0 for g in guides}
    schedule   = {}
    volunteers = {}

    # ── Pre-compute volunteer lists ───────────────────────────────────────────
    for tour in tours:
        if tour.get("is_oneoff"):
            volunteers[tour["id"]] = special_tour_volunteers(guides, tour)
        else:
            volunteers[tour["id"]] = []

    # ── Build weekly slot groups ──────────────────────────────────────────────
    slot_groups = defaultdict(list)
    for tour in tours:
        if not tour.get("is_oneoff") and tour.get("slot_key"):
            slot_groups[tour["slot_key"]].append(tour)

    # ── Build a unified work list ─────────────────────────────────────────────
    work = []

    for key, occurrences in slot_groups.items():
        slot    = occurrences[0]["slot"].lower()
        min_g   = occurrences[0]["min_guides"]
        max_g   = occurrences[0].get("max_guides", min_g)
        n_weeks = len(occurrences)
        eligible = [g["name"] for g in guides
                    if slot and slot in avail.get(g["name"], set())]
        work.append(("weekly", key, eligible, min_g, max_g, n_weeks))

    for tour in [t for t in tours if t.get("is_oneoff")]:
        vols  = volunteers[tour["id"]]
        min_g = tour["min_guides"]
        max_g = tour.get("max_guides", min_g)
        work.append(("special", tour["id"], vols, min_g, max_g, 1))

    # Shuffle first so ties are broken randomly, then sort by constraint
    random.shuffle(work)
    work.sort(key=lambda w: (len(w[2]), -w[5]))

    # ── Assign ────────────────────────────────────────────────────────────────
    slot_assigned = {}

    eligibility_count = defaultdict(int)
    for _, _, eligible, _, _, weight in work:
        for name in eligible:
            eligibility_count[name] += weight

    def pick(eligible, max_g, weight):
        """
        Pick up to max_g guides with the lowest normalised load.
        Shuffle eligible list first so guides with identical scores
        are selected in a different random order each run.
        """
        if max_per_guide:
            eligible = [n for n in eligible if count.get(n, 0) + weight <= max_per_guide]
        shuffled = list(eligible)
        random.shuffle(shuffled)
        def score(n):
            total_elig = max(1, eligibility_count[n])
            return (count.get(n, 0) / total_elig, count.get(n, 0))
        return sorted(shuffled, key=score)[:max_g]

    for kind, key, eligible, min_g, max_g, weight in work:
        assigned = pick(eligible, max_g, weight)
        if kind == "weekly":
            slot_assigned[key] = assigned
            for n in assigned:
                count[n] = count.get(n, 0) + weight
        else:
            schedule[key] = assigned
            for n in assigned:
                count[n] = count.get(n, 0) + 1

    # Write weekly assignments into every occurrence
    for tour in tours:
        if not tour.get("is_oneoff"):
            schedule[tour["id"]] = slot_assigned.get(tour.get("slot_key", ""), [])

    return schedule, count, volunteers


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
.masthead{border-bottom:2px solid #1c1c1a;padding-bottom:14px;margin-bottom:32px;display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;}
.masthead h1{font-size:20px;font-weight:400;font-style:italic;letter-spacing:.01em;}
.masthead p{font-size:12px;color:#888;font-family:system-ui,sans-serif;margin-top:3px;}
.steps{display:flex;margin-bottom:36px;border:1px solid #ccc;border-radius:2px;overflow:hidden;}
.step-tab{flex:1;padding:10px 16px;font-size:11px;font-family:system-ui,sans-serif;background:#faf9f7;border:none;cursor:pointer;color:#aaa;text-align:center;border-right:1px solid #ccc;letter-spacing:.06em;text-transform:uppercase;transition:background .12s,color .12s;}
.step-tab:last-child{border-right:none;}
.step-tab.active{background:#1c1c1a;color:#faf9f7;}
.step-tab:hover:not(.active){background:#ede9e3;color:#1c1c1a;}
.pane{display:none;}.pane.active{display:block;}
.section{margin-bottom:28px;}
.rule{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.1em;color:#aaa;border-bottom:1px solid #ddd;padding-bottom:5px;margin-bottom:14px;}
.two-up{display:grid;grid-template-columns:1fr 1fr;gap:24px;}
.drop-zone{border:1px solid #ccc;background:#fff;border-radius:2px;padding:28px 20px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s;}
.drop-zone:hover{border-color:#888;background:#faf9f7;}
.drop-zone.over{border-color:#1c1c1a;background:#ede9e3;}
.drop-zone.loaded{border-color:#3b6d11;background:#f6faf2;}
.drop-zone i{font-size:20px;display:block;margin-bottom:8px;color:#ccc;}
.drop-zone.loaded i{color:#3b6d11;}
.dz-name{font-size:13px;font-family:system-ui,sans-serif;color:#666;}
.drop-zone.loaded .dz-name{color:#2d5b0e;font-weight:500;}
.dz-hint{font-size:11px;font-family:system-ui,sans-serif;color:#ccc;margin-top:3px;}
.drop-zone.loaded .dz-hint{color:#3b6d11;}
.field-row{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-top:22px;}
.field{display:flex;flex-direction:column;gap:5px;}
.field label{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:#aaa;}
input[type=number],select{border:1px solid #ccc;border-radius:2px;padding:7px 10px;font-size:13px;font-family:system-ui,sans-serif;background:#fff;color:#1c1c1a;outline:none;}
input:focus,select:focus{border-color:#1c1c1a;}
.btn{border:1px solid #1c1c1a;border-radius:2px;padding:8px 16px;font-size:11px;font-family:system-ui,sans-serif;background:#1c1c1a;color:#faf9f7;cursor:pointer;letter-spacing:.06em;text-transform:uppercase;display:inline-flex;align-items:center;gap:6px;transition:background .1s;}
.btn:hover{background:#333;}
.btn-out{background:#faf9f7;color:#1c1c1a;}
.btn-out:hover{background:#ede9e3;}
.btn-sm{padding:5px 11px;}
.msg-err{font-size:12px;font-family:system-ui,sans-serif;color:#8a1e1e;margin-top:6px;min-height:14px;}
.msg-ok{font-size:12px;font-family:system-ui,sans-serif;color:#2d5b0e;margin-top:6px;min-height:14px;}
code{background:#eee;padding:1px 5px;border-radius:2px;font-size:11px;font-family:monospace;color:#555;}
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#ccc;border:1px solid #ccc;border-radius:2px;overflow:hidden;margin-bottom:28px;}
.sbox{background:#faf9f7;padding:16px 20px;}
.sval{font-size:30px;font-weight:400;line-height:1;letter-spacing:-.02em;font-family:Georgia,serif;}
.slbl{font-size:10px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:#aaa;margin-top:6px;}
.sbox.good .sval{color:#2d5b0e;}
.sbox.bad  .sval{color:#8a1e1e;}
.day-block{margin-bottom:20px;}
.day-head{font-size:12px;font-family:system-ui,sans-serif;font-weight:500;color:#1c1c1a;background:#ede9e3;padding:6px 12px;letter-spacing:.02em;}
.trow{display:flex;gap:14px;align-items:baseline;padding:8px 12px;border-bottom:1px solid #ede9e3;font-family:system-ui,sans-serif;font-size:13px;border-left:3px solid transparent;}
.trow:last-child{border-bottom:none;}
.trow.staffed{border-left-color:#3b6d11;}
.trow.partial{border-left-color:#a06a10;}
.trow.empty  {border-left-color:#8a1e1e;}
.trow.special{border-left-color:#534ab7;}
.tname{font-weight:500;min-width:190px;flex-shrink:0;}
.ttime{color:#888;min-width:72px;flex-shrink:0;}
.tguides{flex:1;display:flex;flex-wrap:wrap;gap:4px;align-items:center;}
.tbadge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:2px;white-space:nowrap;}
.bok   {background:#eaf3de;color:#27500a;}
.bwarn {background:#fcebeb;color:#791f1f;}
.bspec {background:#eeedfe;color:#3c3489;}
.bgray {background:#ede9e3;color:#666;}
.chip  {display:inline-block;font-size:11px;background:#dce8f7;color:#0c447c;border-radius:2px;padding:1px 6px;}
.chipv {display:inline-block;font-size:11px;background:#eeedfe;color:#3c3489;border-radius:2px;padding:1px 6px;}
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
.rtab{font-size:11px;font-family:system-ui,sans-serif;text-transform:uppercase;letter-spacing:.07em;color:#aaa;padding:0 20px 8px 0;border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .1s;}
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
      <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-top:2px;font-style:normal;">Carolyn's Assignment and Tour System</p>
      <p>Dartmouth Admissions Office</p>
    </div>
    <button class="btn btn-out btn-sm" onclick="downloadTemplate()">
      <i class="ti ti-download" aria-hidden="true"></i> Tour template
    </button>
  </div>

  <div class="steps">
    <button class="step-tab active" onclick="showPane('upload')"   id="nav-upload">1 &mdash; Upload</button>
    <button class="step-tab"        onclick="showPane('review')"   id="nav-review">2 &mdash; Review</button>
    <button class="step-tab"        onclick="showPane('schedule')" id="nav-schedule">3 &mdash; Schedule</button>
  </div>

  <div class="pane active" id="pane-upload">
    <div class="section">
      <div class="rule">Step 1 &mdash; Tour template</div>
      <p style="font-size:13px;font-family:system-ui,sans-serif;color:#666;margin-bottom:14px;line-height:1.7;">Download the template, fill in your tours for the quarter, and save as CSV. Set <code>Type</code> to <code>regular</code> for weekly recurring slots or <code>special</code> for one-off dates.</p>
      <button class="btn btn-out" onclick="downloadTemplate()"><i class="ti ti-download" aria-hidden="true"></i> Download tour_template.csv</button>
      <p style="margin-top:10px;font-size:11px;font-family:system-ui,sans-serif;color:#bbb;line-height:1.9;">Columns: <code>Tour Name</code> &nbsp;<code>Date</code> &nbsp;<code>Day of Week</code> &nbsp;<code>Time</code> &nbsp;<code>Type</code> &nbsp;<code>Min Guides</code> &nbsp;<code>Max Guides</code> &nbsp;<code>Notes</code></p>
    </div>
    <div class="two-up">
      <div class="section">
        <div class="rule">Step 2 &mdash; Tour schedule CSV</div>
        <div class="drop-zone" id="dz-tours">
          <i class="ti ti-file-spreadsheet" aria-hidden="true"></i>
          <div class="dz-name" id="dz-tours-label">Drop file here or click to browse</div>
          <div class="dz-hint" id="dz-tours-hint">Filled-in tour_template.csv</div>
        </div>
        <input type="file" id="fi-tours" accept=".csv" style="display:none;"/>
        <p class="msg-err" id="err-tours"></p><p class="msg-ok" id="ok-tours"></p>
      </div>
      <div class="section">
        <div class="rule">Step 3 &mdash; Guide availability CSV</div>
        <div class="drop-zone" id="dz-guides">
          <i class="ti ti-users" aria-hidden="true"></i>
          <div class="dz-name" id="dz-guides-label">Drop file here or click to browse</div>
          <div class="dz-hint" id="dz-guides-hint">Google Form responses export</div>
        </div>
        <input type="file" id="fi-guides" accept=".csv" style="display:none;"/>
        <p class="msg-err" id="err-guides"></p>
        <p class="msg-ok" id="ok-guides"></p>
        <div id="extract-btn-row" style="display:none;margin-top:12px;">
          <div style="margin-bottom:10px;">
            <div class="rule" style="margin-bottom:8px;">Special tours from the form (optional but recommended)</div>
            <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-bottom:8px;line-height:1.6;">
              Paste the full list of special tour options from your Google Form — one per line, exactly as they appear on the form. This ensures all tours appear in the template even if no guide has signed up yet.
            </p>
            <textarea id="special-tours-paste" placeholder="Friday June 19th 10:00am - Reunions Tour&#10;Saturday June 20th 3:00pm - Reunions Tour&#10;Tuesday July 7th 2:45pm - Camp Weequahic&#10;..." style="width:100%;min-height:120px;font-size:12px;font-family:monospace;border:1px solid #ccc;border-radius:2px;padding:8px 10px;background:#fff;resize:vertical;outline:none;"></textarea>
          </div>
          <button class="btn btn-out" onclick="downloadExtractedTemplate()">
            <i class="ti ti-table-export" aria-hidden="true"></i> Generate pre-filled tour template from this form
          </button>
          <p style="font-size:11px;font-family:system-ui,sans-serif;color:#aaa;margin-top:6px;line-height:1.6;">Downloads a tour_template_prefilled.csv with all weekly slots and special tours. Open it, adjust Min/Max Guides, then upload as your tour CSV.</p>
        </div>
      </div>
    </div>
    <div class="field-row">
      <div class="field"><label>Max tours per guide (optional)</label><input type="number" id="max-tours" placeholder="No limit" min="1" style="width:140px;"/></div>
      <button class="btn" onclick="proceedToReview()">Continue to review <i class="ti ti-arrow-right" aria-hidden="true"></i></button>
    </div>
  </div>

  <div class="pane" id="pane-review">
    <div class="rtabs">
      <button class="rtab active" onclick="setRTab('regular')" id="rtab-regular">Regular tours</button>
      <button class="rtab"        onclick="setRTab('special')" id="rtab-special">Special tours</button>
      <button class="rtab"        onclick="setRTab('guides')"  id="rtab-guides">Guides</button>
    </div>
    <div id="review-regular"></div>
    <div id="review-special" style="display:none;"></div>
    <div id="review-guides"  style="display:none;"></div>
  </div>

  <div class="pane" id="pane-schedule">
    <div class="actions">
      <button class="btn" onclick="generateSchedule()">Generate schedule</button>
      <button class="btn btn-out" onclick="exportCSV()"><i class="ti ti-download" aria-hidden="true"></i> Export CSV</button>
      <select id="filter-sel" onchange="applyFilter()"><option value="all">All tours</option><option value="regular">Regular only</option><option value="special">Special only</option><option value="understaffed">Understaffed only</option></select>
    </div>
    <p class="msg-err" id="gen-err"></p>
    <div id="stats-area"></div>
    <div class="legend" id="legend" style="display:none;">
      <span><span class="ldot" style="background:#3b6d11;"></span>Fully staffed</span>
      <span><span class="ldot" style="background:#a06a10;"></span>Partially staffed</span>
      <span><span class="ldot" style="background:#534ab7;"></span>Special tour</span>
      <span><span class="ldot" style="background:#8a1e1e;"></span>Understaffed</span>
    </div>
    <div id="schedule-out"></div>
    <div id="fairness-out"></div>
  </div>
</div>
<script>
let tours=[],guides=[],scheduleResult={},countResult={},volunteersResult={},guideFileText=null;
function showPane(p){document.querySelectorAll('.pane').forEach(el=>el.classList.remove('active'));document.querySelectorAll('.step-tab').forEach(el=>el.classList.remove('active'));document.getElementById('pane-'+p).classList.add('active');document.getElementById('nav-'+p).classList.add('active');}
function downloadTemplate(){fetch('/template').then(r=>r.text()).then(csv=>{const a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='tour_template.csv';a.click();});}
function makeDZ(id,inputId,fn){
  const dz=document.getElementById(id);
  const inp=document.getElementById(inputId);
  dz.addEventListener('click',()=>inp.click());
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
  dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files[0])fn(e.dataTransfer.files[0]);});
  inp.addEventListener('change',()=>{if(inp.files[0])fn(inp.files[0]);});
}
makeDZ('dz-tours','fi-tours',f=>handleTourFile(f));
makeDZ('dz-guides','fi-guides',f=>handleGuideFile(f));
function readFile(f,cb){const r=new FileReader();r.onload=e=>cb(e.target.result);r.readAsText(f);}
function handleTourFile(file){readFile(file,text=>{fetch('/parse_tours',{method:'POST',headers:{'Content-Type':'text/plain'},body:text}).then(r=>r.json()).then(data=>{document.getElementById('err-tours').textContent=data.error||'';document.getElementById('ok-tours').textContent=data.error?'':`${data.tours.length} tours &mdash; ${data.tours.filter(t=>!t.is_oneoff).length} regular, ${data.tours.filter(t=>t.is_oneoff).length} special.`;if(!data.error){tours=data.tours;document.getElementById('dz-tours').classList.add('loaded');document.getElementById('dz-tours-label').textContent=file.name;document.getElementById('dz-tours-hint').textContent='Loaded';}});});}
function handleGuideFile(file){
  readFile(file,function(text){
    guideFileText = text;
    fetch('/parse_guides',{method:'POST',headers:{'Content-Type':'text/plain'},body:text})
    .then(function(r){return r.json();})
    .then(function(data){
      document.getElementById('err-guides').textContent=data.error||'';
      document.getElementById('ok-guides').textContent=data.error?'':data.guides.length+' guides loaded — '+data.guides.filter(function(g){return g.slots.length>0;}).length+' with availability.';
      if(!data.error){
        guides=data.guides;
        document.getElementById('dz-guides').classList.add('loaded');
        document.getElementById('dz-guides-label').textContent=file.name;
        document.getElementById('dz-guides-hint').textContent='Loaded';
        document.getElementById('extract-btn-row').style.display='block';
      }
    });
  });
}
function downloadExtractedTemplate(){
  if(!guideFileText){return;}
  var extra=document.getElementById('special-tours-paste').value.trim();
  var payload=JSON.stringify({guide_csv:guideFileText,extra_specials:extra});
  fetch('/extract_tours',{method:'POST',headers:{'Content-Type':'application/json'},body:payload})
  .then(function(r){return r.json();})
  .then(function(data){
    if(data.error){alert('Error: '+data.error);return;}
    var a=document.createElement('a');
    a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(data.csv);
    a.download='tour_template_prefilled.csv';
    a.click();
  });
}
function proceedToReview(){if(!tours.length){alert('Upload a tour CSV first.');return;}if(!guides.length){alert('Upload a guide CSV first.');return;}renderReview();showPane('review');}
function setRTab(v){['regular','special','guides'].forEach(x=>{document.getElementById('review-'+x).style.display=x===v?'block':'none';document.getElementById('rtab-'+x).classList.toggle('active',x===v);});}
function renderReview(){
  const reg=tours.filter(t=>!t.is_oneoff),spec=tours.filter(t=>t.is_oneoff),DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const slots={};reg.forEach(t=>{const k=t.slot_key||t.id;slots[k]=slots[k]||t;});
  let h='<div class="notebox">Same guides are assigned to every occurrence of each weekly slot for the full term.</div>';
  DAYS.forEach(day=>{const items=Object.values(slots).filter(t=>t.slot.toLowerCase().startsWith(day.toLowerCase()));if(!items.length)return;h+=`<div class="day-block"><div class="day-head">${day}</div>`;items.sort((a,b)=>a.time.localeCompare(b.time)).forEach(t=>{const occ=reg.filter(r=>r.slot_key===t.slot_key).length;h+=`<div class="trow staffed"><span class="tname">${t.name}</span><span class="ttime">${t.time}</span><span class="tguides"><span class="tbadge bok">${t.min_guides}&ndash;${t.max_guides} guides</span><span class="tbadge bgray">${occ} week${occ!==1?'s':''}</span></span>${t.note?`<span class="tnote">${t.note}</span>`:''}</div>`;});h+='</div>';});
  document.getElementById('review-regular').innerHTML=h;
  const byD={};spec.forEach(t=>{(byD[t.date_str]=byD[t.date_str]||[]).push(t);});
  let h2='';Object.keys(byD).forEach(d=>{h2+=`<div class="day-block"><div class="day-head">${d}</div>`;byD[d].forEach(t=>{h2+=`<div class="trow special"><span class="tname">${t.name}</span><span class="ttime">${t.time}</span><span class="tguides"><span class="tbadge bspec">${t.min_guides}&ndash;${t.max_guides} guides</span></span>${t.note?`<span class="tnote">${t.note}</span>`:''}</div>`;});h2+='</div>';});
  document.getElementById('review-special').innerHTML=h2||'<p style="font-size:13px;font-family:system-ui,sans-serif;color:#bbb;">No special tours.</p>';
  const active=guides.filter(g=>g.slots.length>0);
  let h3=`<p style="font-size:12px;font-family:system-ui,sans-serif;color:#999;margin-bottom:14px;">${active.length} guides with weekly availability.</p><div class="guide-grid">`;
  active.forEach(g=>{h3+=`<div class="gcard"><div class="gcname">${g.name}</div><div>${g.slots.map(s=>`<span class="spill">${s}</span>`).join('')}</div>${g.special_tours&&g.special_tours.length?`<div style="margin-top:6px;font-size:11px;font-family:system-ui,sans-serif;color:#534ab7;">${g.special_tours.length} special sign-up${g.special_tours.length!==1?'s':''}</div>`:''}</div>`;});
  h3+='</div>';document.getElementById('review-guides').innerHTML=h3;
}
function generateSchedule(){
  const err=document.getElementById('gen-err');
  if(!tours.length){err.textContent='Upload tours first.';return;}if(!guides.length){err.textContent='Upload guides first.';return;}err.textContent='';
  const maxPG=parseInt(document.getElementById('max-tours').value)||null;
  fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tours,guides,max_per_guide:maxPG})}).then(r=>r.json()).then(data=>{if(data.error){err.textContent=data.error;return;}scheduleResult=data.schedule;countResult=data.count;volunteersResult=data.volunteers;renderSchedule();showPane('schedule');});
}
function renderSchedule(){
  var total=tours.length;
  var full=tours.filter(function(t){return (scheduleResult[t.id]||[]).length>=t.min_guides;}).length;
  var empty=tours.filter(function(t){return (scheduleResult[t.id]||[]).length===0;}).length;
  var totalA=Object.values(countResult).reduce(function(a,b){return a+b;},0);
  document.getElementById('stats-area').innerHTML='<div class="stats-row"><div class="sbox"><div class="sval">'+total+'</div><div class="slbl">Total tours</div></div><div class="sbox good"><div class="sval">'+full+'</div><div class="slbl">Fully staffed</div></div><div class="sbox '+(empty>0?'bad':'good')+'"><div class="sval">'+empty+'</div><div class="slbl">Unassigned</div></div><div class="sbox"><div class="sval">'+totalA+'</div><div class="slbl">Assignments</div></div></div>';
  document.getElementById('legend').style.display='flex';
  var byDate={};
  tours.forEach(function(t){ if(!byDate[t.date_str]) byDate[t.date_str]=[]; byDate[t.date_str].push(t); });
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
      var noGuides=!assigned.length?'<span style="font-size:11px;color:#ccc;">No guides assigned</span>':'';
      var badge='<span class="tbadge '+bc+'" style="margin-left:4px;">'+assigned.length+'&thinsp;/&thinsp;'+t.min_guides+'&ndash;'+t.max_guides+'</span>';
      var specBadge=t.is_oneoff?'<span class="tbadge bspec">special</span>':'';
      var noteHtml=t.note?'<span class="tnote">'+t.note+'</span>':'';
      var volHtml='';
      if(vols.length){
        var volChips=vols.map(function(n){return '<span class="chipv">'+n+'</span>';}).join('');
        volHtml='<div class="volline"><span class="vollbl">Also signed up:</span>'+volChips+'</div>';
      } else if(t.is_oneoff){
        volHtml='<div class="volline"><span style="font-size:11px;color:#ccc;font-style:italic;font-family:system-ui,sans-serif;">No guides signed up &mdash; assign manually.</span></div>';
      }
      html+='<div class="trow '+rc+'" data-oneoff="'+t.is_oneoff+'" data-ok="'+ok+'" data-assigned="'+assigned.length+'" data-min="'+t.min_guides+'"><span class="tname">'+t.name+'</span><span class="ttime">'+t.time+'</span><span class="tguides">'+chips+noGuides+badge+specBadge+'</span>'+noteHtml+'</div>'+volHtml;
    });
    html+='</div>';
  });
  document.getElementById('schedule-out').innerHTML=html;
  var withA=Object.entries(countResult).filter(function(e){return e[1]>0;}).sort(function(a,b){return b[1]-a[1];});
  var withNone=guides.filter(function(g){return !countResult[g.name]||countResult[g.name]===0;}).map(function(g){return g.name;}).sort();
  var maxC=withA.length?withA[0][1]:1;
  var fair='<div style="margin-top:32px;"><div class="rule">Distribution per guide</div>';
  withA.forEach(function(e){
    var name=e[0], c=e[1];
    var pct=Math.round((c/maxC)*100);
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
  const val=document.getElementById('filter-sel').value;
  document.querySelectorAll('#schedule-out .trow').forEach(el=>{const io=el.dataset.oneoff==='true',ok=el.dataset.ok==='true',show=val==='all'?true:val==='regular'?!io:val==='special'?io:val==='understaffed'?!ok:true;el.style.display=show?'':'none';});
  document.querySelectorAll('#schedule-out .volline').forEach(el=>{const prev=el.previousElementSibling;el.style.display=prev&&prev.style.display!=='none'?'':'none';});
  document.querySelectorAll('#schedule-out .day-block').forEach(g=>{g.style.display=[...g.querySelectorAll('.trow')].some(el=>el.style.display!=='none')?'':'none';});
}
function exportCSV(){
  if(!Object.keys(scheduleResult).length){document.getElementById('gen-err').textContent='Generate a schedule first.';return;}
  let csv='Date,Tour,Time,Type,Notes,Min Guides,Max Guides,Assigned Guide(s),Volunteers\n';
  tours.forEach(t=>{csv+=`"${t.date_str}","${t.name}","${t.time}","${t.is_oneoff?'Special':'Regular'}","${t.note}","${t.min_guides}","${t.max_guides}","${(scheduleResult[t.id]||[]).join('; ')}","${(volunteersResult[t.id]||[]).join('; ')}"\n`;});
  const a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='tour_guide_schedule.csv';a.click();
}
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/template":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.end_headers()
            self.wfile.write(TEMPLATE_CSV.encode("utf-8"))
        else:
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

        if path == "/parse_tours":
            try:
                tours, err = parse_tour_csv(body.decode("utf-8"))
                respond({"error": err, "tours": tours} if err else {"tours": tours})
            except Exception as e:
                respond({"error": str(e), "tours": []})

        elif path == "/parse_guides":
            try:
                guides, err = parse_guide_csv(body.decode("utf-8"))
                respond({"error": err, "guides": guides} if err else {"guides": guides})
            except Exception as e:
                respond({"error": str(e), "guides": []})

        elif path == "/extract_tours":
            try:
                payload = json.loads(body.decode("utf-8"))
                guide_csv     = payload.get("guide_csv", "")
                extra_specials = payload.get("extra_specials", "")
                template, err = extract_tours_from_guides(guide_csv, extra_specials)
                respond({"error": err} if err else {"csv": template})
            except Exception as e:
                respond({"error": str(e)})

        elif path == "/generate":
            try:
                p = json.loads(body)
                sched, count, vols = generate_schedule(
                    p["guides"], p["tours"], p.get("max_per_guide")
                )
                respond({"schedule": sched, "count": count, "volunteers": vols})
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
