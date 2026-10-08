import os, re, json, time, threading, calendar, datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import requests
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
YEAR = 2026

REGIONS = ["Central", "North", "South", "West", "East"]
PRODUCTS = {
    "Kettle": r"kettles?", "Blender": r"blenders?", "Juicer": r"juicers?",
    "Mixer": r"mixers?", "Toaster": r"toasters?", "Air Fryer": r"air[\s-]?fryers?",
    "Grinder": r"grinders?", "Rice Cooker": r"rice[\s-]?cookers?",
}

# ---------------------------------------------------------------- data
cache = None
lock = threading.Lock()
load_lock = threading.Lock()


def cents(x):
    return float(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def refresh():
    global cache
    base = os.environ["LEDGER_URL"]
    exp = requests.get(base + "&path=%2Fexport", timeout=30).text
    rates = requests.get(base + "&path=%2Frates", timeout=30).json()["usd_per_unit"]
    latest = {}
    for line in exp.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        r["_u"] = parse_ts(r["updated_at"])
        p = latest.get(r["id"])
        if p is None or r["_u"] >= p["_u"]:
            latest[r["id"]] = r
    rows = list(latest.values())
    for r in rows:
        r["usd"] = Decimal(str(r["amount"])) * Decimal(str(rates[r["currency"]]))
        local = parse_ts(r["created_at"]).astimezone(IST)
        r["date"] = local.strftime("%Y-%m-%d")
        r["month"] = local.strftime("%Y-%m")
    with lock:
        cache = {"rows": rows}
    print("ledger loaded:", len(rows), "orders", flush=True)


def background():
    while True:
        try:
            refresh()
            time.sleep(600)
        except Exception as e:
            print("load failed:", e, flush=True)
            time.sleep(15)


threading.Thread(target=background, daemon=True).start()


def get_data():
    global cache
    if cache is None:
        with load_lock:
            if cache is None:
                refresh()
    return cache


# ---------------------------------------------------------------- dates
MONTHS = {}
for _i, _n in enumerate(calendar.month_name):
    if _n:
        MONTHS[_n.lower()] = _i
        MONTHS[_n[:3].lower()] = _i
MONTHS["sept"] = 9
MON_RE = (r"(?:january|february|march|april|may|june|july|august|september|october|"
          r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")


def span(y, m):
    return (f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}")


def day(y, m, d):
    s = dt.date(y, m, d).isoformat()
    return (s, s)


def parse_dates(text):
    found = []

    def take(pat, fn):
        nonlocal text
        for m in re.finditer(pat, text):
            try:
                iv = fn(m)
            except ValueError:
                iv = None
            if iv:
                found.append((m.start(), iv))
        text = re.sub(pat, lambda m: " " * len(m.group(0)), text)

    yr = lambda s: int(s) if s else YEAR
    QW = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
    quarter = lambda y, n: (span(y, 3 * n - 2)[0], span(y, 3 * n)[1])
    half = lambda y, n: (span(y, 1)[0], span(y, 6)[1]) if n == 1 else (span(y, 7)[0], span(y, 12)[1])

    take(r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: day(int(m[1]), int(m[2]), int(m[3])))
    take(r"\b(\d{4})-(\d{2})\b", lambda m: span(int(m[1]), int(m[2])))
    take(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MON_RE})\b(?:,?\s+(\d{{4}}))?",
         lambda m: day(yr(m[3]), MONTHS[m[2]], int(m[1])))
    take(rf"\b({MON_RE})\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?",
         lambda m: day(yr(m[3]), MONTHS[m[1]], int(m[2])))
    take(rf"\b({MON_RE})\b(?:,?\s+(\d{{4}}))?", lambda m: span(yr(m[2]), MONTHS[m[1]]))
    take(r"\bq([1-4])\b(?:\s+of)?(?:,?\s+(\d{4}))?", lambda m: quarter(yr(m[2]), int(m[1])))
    take(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b(?:\s+of)?(?:,?\s+(\d{4}))?",
         lambda m: quarter(yr(m[2]), QW[m[1]]))
    take(r"\bh([12])\b(?:\s+of)?(?:,?\s+(\d{4}))?", lambda m: half(yr(m[2]), int(m[1])))
    take(r"\b(first|second)\s+half\b(?:\s+of)?(?:,?\s+(\d{4}))?",
         lambda m: half(yr(m[2]), 1 if m[1] == "first" else 2))
    take(r"\b(20\d{2})\b", lambda m: (f"{m[1]}-01-01", f"{m[1]}-12-31"))
    return [iv for _, iv in sorted(found)]


# ---------------------------------------------------------------- parsing
STRONG_UP = "most|highest|top|best|largest|biggest|greatest|leading"
STRONG_DOWN = "least|lowest|fewest|smallest|worst|bottom"
NOUN = r"(product|appliance|item|region|customer|buyer|month)"
NORM = {"product": "product", "appliance": "product", "item": "product",
        "region": "region", "customer": "customer", "buyer": "customer", "month": "month"}
CMP = (r"\b(?:differences?|differ|compar\w*|versus|vs|more than|less than|fewer than|higher than|"
       r"lower than|greater than|larger than|bigger than|smaller than|gap|ratio|times|growth|grew|"
       r"change|increase|decrease|percent|percentage)\b|%")
RANGE = (r"\bfrom\b.+?\b(?:to|through|thru|until|till)\b|\bbetween\b.+?\band\b|"
         r"\b(?:through|thru|until)\b")
NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}
MONEY = r"revenue|sales|earn|spen[dt]|amount|value|usd|\$|dollar|money|income|gross|worth"


def ordered(patterns, text):
    hits = []
    for name, pat in patterns.items():
        m = re.search(rf"\b{pat}\b", text)
        if m:
            hits.append((m.start(), name))
    return [n for _, n in sorted(hits)]


def rank_measure(ql):
    if re.search(MONEY, ql):
        return "usd"
    if re.search(r"\b(orders?|purchases?|transactions?|refund\w*|returns?|voids?)\b", ql):
        return "count"
    if re.search(r"\b(units?|quantity|qty|pieces|sold|selling|sells|popular)\b", ql):
        return "units"
    return "usd"


def plain_measure(ql, p):
    if re.search(r"\b(?:how many|number of|count of)\s+(?:(?:unique|distinct|different|individual)\s+)?"
                 r"(?:customers|buyers|clients)\b", ql):
        p["measure"] = "distinct_customers"
    elif re.search(r"\b(?:how many|number of|count of)\s+(?:(?:unique|distinct|different)\s+)?"
                   r"(?:products|appliances)\b", ql):
        p["measure"] = "distinct_products"
    elif re.search(r"\b(average|avg|mean)\b", ql):
        p["agg"] = "avg"
    elif re.search(r"\b(largest|biggest|highest|greatest|maximum|max)\b.{0,25}\b(order|sale|purchase|transaction)", ql):
        p["agg"] = "max"
    elif re.search(r"\b(smallest|lowest|minimum|min)\b.{0,25}\b(order|sale|purchase|transaction)", ql):
        p["agg"] = "min"
    elif re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", ql):
        if re.search(r"\b(units?|quantity|qty|pieces?|items?|sold)\b", ql) and \
                not re.search(r"\borders?\b|\brefund\w*|\breturn", ql):
            p["measure"] = "units"
        else:
            p["measure"] = "count"
    elif re.search(r"\b(units?|quantity|qty|pieces)\b", ql):
        p["measure"] = "units"


def parse(q):
    ql = " ".join(q.lower().split())
    regions = ordered({r: r.lower() for r in REGIONS}, ql)
    products = ordered(PRODUCTS, ql)
    customers = [c.upper() for c in re.findall(r"\bc\d{3,5}\b", ql)]
    ivs = parse_dates(ql)
    cmp_cue = bool(re.search(CMP, ql))
    if len(ivs) >= 2 and not cmp_cue and re.search(RANGE, ql):
        ivs = [(min(a for a, _ in ivs), max(b for _, b in ivs))]

    p = dict(status="paid", measure="usd", agg="sum", regions=regions, products=products,
             customers=customers, ivs=ivs, group_by=None, order=None, limit=1,
             compare=None, how=None)
    if re.search(r"\b(refund\w*|returned|returns?)\b", ql):
        p["status"] = "refunded"
    elif re.search(r"\b(void\w*|cancel\w*)\b", ql):
        p["status"] = "void"

    strong = re.search(rf"\b({STRONG_UP}|{STRONG_DOWN})\b", ql)
    rank_noun = None
    m = re.search(rf"\b(?:which|what|who)\b(?:\s+[\w'-]+){{0,6}}?\s+{NOUN}s?\b", ql)
    if m and strong:
        rank_noun = NORM[m.group(1)]
    if not rank_noun:
        m = re.search(rf"\b(?:top|bottom)\s+(?:\w+\s+)?{NOUN}s?\b", ql)
        if m:
            rank_noun = NORM[m.group(1)]
    if not rank_noun:
        m = re.search(rf"\b(?:{STRONG_UP}|{STRONG_DOWN})\b(?:[\s-]+[\w']+){{0,2}}?[\s-]+{NOUN}s?\b", ql)
        if m:
            rank_noun = NORM[m.group(1)]

    lm = re.search(r"\b(?:top|bottom|first|lowest|highest|best|worst)\s+(\d+|one|two|three|four|five|ten)\b", ql)
    limit = 1
    if lm:
        g = lm.group(1)
        limit = int(g) if g.isdigit() else NUMW[g]

    if rank_noun:
        p.update(group_by=rank_noun, measure=rank_measure(ql), limit=limit,
                 order="asc" if re.search(rf"\b({STRONG_DOWN})\b", ql) else "desc")
        return p

    dims = (("region", regions), ("product", products), ("month", ivs))
    if re.search(r"\b(which|who)\b", ql):
        for dim, vals in dims:
            if len(vals) >= 2:
                p.update(group_by=dim, measure=rank_measure(ql), limit=1,
                         order="asc" if re.search(r"\b(least|lowest|fewest|smallest|worst|lower|less|fewer|smaller)\b", ql) else "desc")
                return p

    if cmp_cue:
        for dim, vals in dims:
            if len(vals) >= 2:
                plain_measure(ql, p)
                if p["measure"] == "usd":
                    p["measure"] = rank_measure(ql) if p["status"] != "paid" and not re.search(MONEY, ql) and re.search(r"how many|number of", ql) else "usd"
                if re.search(r"percent|%|growth|grew|change|increase|decrease", ql):
                    how = "pct"
                elif re.search(r"\bratio\b|how many times", ql):
                    how = "ratio"
                elif re.search(r"differ|gap", ql):
                    how = "abs"
                elif re.search(r"\b(less|lower|fewer|smaller)\b", ql):
                    how = "b_minus_a"
                else:
                    how = "a_minus_b"
                p.update(compare=dim, how=how)
                return p

    m = re.search(r"\b(?:by|per|for each|each|every|across)\s+(?:the\s+)?(product|region|customer|month)s?\b", ql)
    if m:
        p["group_by"] = m.group(1)
    plain_measure(ql, p)
    return p


# ---------------------------------------------------------------- execute
def value(lst, measure, agg):
    if measure == "count":
        return len(lst)
    if measure == "units":
        return sum(r["qty"] for r in lst)
    if measure == "distinct_customers":
        return len({r["customer"] for r in lst})
    if measure == "distinct_products":
        return len({r["product"] for r in lst})
    if not lst:
        return 0.0
    v = [r["usd"] for r in lst]
    if agg == "avg":
        x = sum(v) / len(v)
    elif agg == "max":
        x = max(v)
    elif agg == "min":
        x = min(v)
    else:
        x = sum(v)
    return cents(x)


def select(d, p):
    out = []
    for r in d["rows"]:
        if p["status"] != "all" and r["status"] != p["status"]:
            continue
        if p["regions"] and r["region"] not in p["regions"]:
            continue
        if p["products"] and r["product"] not in p["products"]:
            continue
        if p["customers"] and r["customer"] not in p["customers"]:
            continue
        if p["ivs"] and not any(a <= r["date"] <= b for a, b in p["ivs"]):
            continue
        out.append(r)
    return out


def execute(p, d):
    if p.get("compare"):
        key = {"region": "regions", "product": "products", "month": "ivs"}[p["compare"]]
        vals = []
        for ent in p[key][:2]:
            q2 = dict(p, compare=None)
            q2[key] = [ent]
            vals.append(execute(q2, d))
        a, b = vals
        how = p["how"]
        if how == "pct":
            return round((b - a) / a * 100, 2) if a else None
        if how == "ratio":
            return round(a / b, 2) if b else None
        r = abs(a - b) if how == "abs" else (b - a if how == "b_minus_a" else a - b)
        return round(r, 2) if isinstance(r, float) else r

    rows = select(d, p)
    if not p["group_by"]:
        return value(rows, p["measure"], p["agg"])
    groups = {}
    for r in rows:
        groups.setdefault(r[p["group_by"]], []).append(r)
    entries = [(k, value(l, p["measure"], p["agg"])) for k, l in groups.items()]
    if p["order"]:
        entries.sort(key=lambda e: (e[1] if p["order"] == "asc" else -e[1], str(e[0])))
        entries = entries[: p["limit"]]
        if p["limit"] == 1 and entries:
            return entries[0][0]
        return [e[0] for e in entries]
    return dict(entries)


# ---------------------------------------------------------------- web
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "OPTIONS"])
def main(path):
    if request.method == "OPTIONS":
        return make_response("", 204)
    if request.method == "GET":
        q = request.args.get("q")
        if q:  # debug helper: /?q=your question
            p = parse(q)
            return jsonify({"plan": p, "answer": execute(p, get_data())})
        err = None
        try:
            get_data()
        except Exception as e:
            err = repr(e)[:300]
        return jsonify({"status": "ok", "loaded": cache is not None, "error": err})
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"answer": None, "error": "bad json"}), 400
    try:
        p = parse(str(body.get("question", "")))
        return jsonify({"answer": execute(p, get_data())})
    except Exception as e:
        return jsonify({"answer": None, "error": str(e)}), 500
