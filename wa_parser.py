"""Turn WhatsApp property posts into structured listings (rules only, no AI).

Handles typical owner / agent posts in English and Malay, e.g.
  "For Rent: The Conlay 2R2B 1,066sf Fully furnished RM8.5k/mo  Call 012-345 6789"
  "Dijual! Pavilion DH 1.2mil 1200 kps partly furnished"
"""
import re

from phone_filter import extract_from_text

RENT = re.compile(r"\b(for\s*rent|rent(?:al|ing)?|to\s*let|tenant|sewa|disewakan|per\s*month|monthly|/\s*mo(?:nth)?|p\.?m)\b", re.I)
SALE = re.compile(r"\b(for\s*sale|sale|selling|sell|subsale|jual|dijual|asking|nego(?:tiable)?|freehold|leasehold)\b", re.I)
SQFT = re.compile(r"(?<![\d,.])(\d{1,2},\d{3}|\d{3,5}(?:\.\d+)?)[ \t]*(?:sq\.?\s*f(?:ee)?t|sqft|sf|s\.f\.|kps|square\s*f(?:ee|oo)?t)\b", re.I)
MONEY = re.compile(r"(?:\b(rm|myr)\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k|mil(?:lion)?|m|juta)?\b", re.I)
ROOMS = re.compile(r"\b(\d{1,2})\s*(?:\+\s*\d\s*)?(?:r|rooms?|bed(?:room)?s?|br|bilik(?!\s*air))\b", re.I)
BATHS = re.compile(r"\b(\d{1,2})\s*(?:b|baths?|bathrooms?|bilik\s*air)\b", re.I)
COMBO = re.compile(r"\b(\d{1,2})\s*(?:\+\s*\d\s*)?(?:r|b)\s*(\d{1,2})\s*(?:b|br|ba|bath)\b", re.I)  # 3R2B / 3+1R2B / 3B2BR
# Unfurnished / partly are checked before "fully" so "not fully furnished" is not read as fully furnished.
# Up to two words may sit between "fully" and "furnished" ("fully ID furnished", "fully reno & furnished").
FURNISH = [("Unfurnished", re.compile(r"\b(un\s*-?furnish\w*|non\s*-?furnish\w*|not\s+furnish\w*|bare\s*unit|uf|"
                                      r"kosong|tanpa\s*perabot|没家私|无家私)\b", re.I)),
           ("Partially Furnished", re.compile(r"\b(not\s+full(?:y)?\s+furnish\w*|partial\w*\s+(?:\S+\s+){0,2}?furnish\w*|partly\s+(?:\S+\s+){0,2}?"
                                              r"furnish\w*|semi\s*-?\s*furnish\w*|pf|separa\s*perabot|半家私)\b", re.I)),
           ("Fully Furnished", re.compile(r"\b(full(y)?\s+(?:\S+\s+){0,2}?furnish\w*|ff|lengkap\s*perabot|"
                                          r"fully\s+(?:id|i\.d\.?)(?:\s|$)|全家私|家私齐全)", re.I))]


def furnishing_in(text):
    """Furnishing stated in a text ("Fully ID furnished" -> Fully Furnished), or None."""
    return next((label for label, rx in FURNISH if rx.search(text or "")), None)
# Where a property name stops and the details begin ("The Conlay 2R2B 1,066sf ..." -> "The Conlay")
PLACE_END = re.compile(r"(\s|^)(for\s*(rent|sale)|rent(al)?\b|sale\b|dijual|disewakan|sewa\b|jual\b|rm\b|myr\b|"
                       r"\d+(?:\.\d+)?\s*(?:\+\s*\d\s*)?(?:r\b|r\d|b\b|bed|bath|room|bilik|sf|sq|kps|k\b|mil|m\b|juta|%)|"
                       r"\d{1,3},\d{3}|"
                       r"fully|partial|partly|semi|unfurnish|furnish|call\b|wa\b|whatsapp|asking|nego|freehold|"
                       r"leasehold|owner|available|urgent|high\s*floor|low\s*floor)|[,|;:!@]|\.(?=\s|$)", re.I)
NOT_PLACE = re.compile(r"^(for\s*(rent|sale)|rent|sale|dijual|disewakan|sewa|jual|price|rm|call|wa|whatsapp|contact|"
                       r"hp|tel|pm|available|urgent|hot|new|listing|owner|direct|agent|no\.?)\b", re.I)


def _amount(num, unit):
    v = float(num.replace(",", ""))
    unit = (unit or "").lower()
    if unit == "k":
        v *= 1_000
    elif unit in ("m", "mil", "million", "juta"):
        v *= 1_000_000
    return v


def _price(text, phones_digits):
    """Pick the most likely price: prefer amounts written with RM, ignore sqft numbers and phone digits."""
    sqft_spans = [m.span() for m in SQFT.finditer(text)]
    best = None
    for m in MONEY.finditer(text):
        if any(a <= m.start() < b for a, b in sqft_spans):
            continue
        raw_digits = re.sub(r"\D", "", m.group(2))
        if len(raw_digits) >= 9 or any(raw_digits and raw_digits in p for p in phones_digits):
            continue  # part of a phone number
        has_rm, unit = bool(m.group(1)), m.group(3)
        if not has_rm and not unit:
            continue  # a bare number (floor, year, unit no.) is too ambiguous
        v = _amount(m.group(2), unit)
        if not 300 <= v <= 500_000_000:
            continue
        score = (2 if has_rm else 0) + (1 if unit else 0)
        if best is None or score > best[0]:
            best = (score, v)
    return int(best[1]) if best else None


PERSON = re.compile(r"^\W*(mr|mrs|ms|mdm|madam|miss|dato'?|datuk|datin|tan\s*sri|dr|encik|en\.|puan|pn\.|cik|tuan)\b",
                    re.I)
# Words that make a line look like a property / area name (preferred over the first line, e.g. a chat header)
PLACE_HINT = re.compile(r"\b(residen\w*|suites?|condo\w*|tower|towers|park|heights|villa\w*|court|place|square|"
                        r"city|garden\w*|terrace|bungalow|mansion|loft|soho|sovo|klcc|trx|mont\s*kiara|kiara|"
                        r"bangsar|damansara|ampang|cheras|setapak|kepong|sentul|pavilion|bukit|jalan|jln|taman|"
                        r"desa|sri|seri|kl|kuala\s*lumpur|petaling|subang|puchong|cyberjaya|putrajaya|shah\s*alam)\b",
                        re.I)


def _place(text):
    """A line that reads like a property name / area - preferring lines with property words - rather than a
    keyword, price, greeting or person's name (e.g. the contact name at the top of a chat screenshot)."""
    candidates = []
    for line in text.splitlines():
        intro = INTRO.search(line[:40])  # "Hi this is May ..." is an introduction, not a place
        if intro and intro.group(1).split()[0].lower() in NOT_A_NAME_WORD | {"video", "photo", "photos", "unit"}:
            intro = None  # "This is the video for ..." is not an introduction
        if PERSON.match(line) or re.search(r"\b(owner|agent|tenant)\s*$", line.strip(), re.I) or intro:
            continue
        s = _place_line(line)
        if s:
            candidates.append((bool(PLACE_HINT.search(line)), s))
    hinted = [s for hint, s in candidates if hint]
    return (hinted or [s for _, s in candidates] or [None])[0]


def _place_line(line):
    """The property-name part of one line, or None."""
    s = re.sub(r"[^\w\s@&'().,/+|-]", " ", line)        # drop emojis / decoration
    s = re.sub(r"\s+", " ", s).strip(" -:|*,.")
    # Chatty openings in private chats: "Hi bro, my unit The Conlay ..." -> "The Conlay ..."
    s = re.sub(r"^((hi|hello|hey|hai|dear|good\s*(morning|afternoon|evening)|selamat\s*\w+|bro|sis|boss|sir|"
               r"madam|mr\.?|ms\.?|tuan|puan|encik|cik|thanks?|tq|ok(ay)?|yes|yeah|ya|yup|actually|"
               r"(do\s+)?(u|you)\s+know)\b[\s,!.]*)+", "", s, flags=re.I)
    s = re.sub(r"^((i|we)\s+(have|got|want\s+to\s+(rent|sell))\s+|(my|our|this|the)\s+)?"
               r"(unit|condo|house|property|place|apartment|rumah)\s*(is\s+|at\s+|in\s+|@\s*)?", "", s, flags=re.I)
    # Captions on media: "Above photos for sky suite B15-06", "This is the video for ..."
    s = re.sub(r"^((above|below|here|these|this|the)\s+)*((is|are)\s+)?(the\s+)?(photos?|pictures?|pics?|videos?|"
               r"images?|details?)\s+(of|for)\s+", "", s, flags=re.I)
    s = re.sub(r"^((urgent|hot|new|cheap|best\s*buy|below\s*market|limited|rare|exclusive|owner\s*direct)\b\s*)+",
               "", s, flags=re.I)
    s = re.sub(r"^(unit\s+)?(for\s*(rent|sale)|dijual|disewakan|sewa|jual)\s*[:\-]?\s*", "", s, flags=re.I)
    if m := re.search(r"\b(?:at|@|di)\s+([A-Z].*)", s):     # "unit for sale at Eaton Residences"
        s = m.group(1)
    if m := PLACE_END.search(s):
        s = s[:m.start()]
    s = s.strip(" -:|*,.@")
    if len(s) < 3 or NOT_PLACE.match(s) or not re.search(r"[A-Za-z]{3}", s):
        return None
    return s[:80]


def parse(text, need_price=True):
    """Return a dict of listing fields, or None if the message doesn't look like a property listing.
    need_price=False: the fields even without a price (an owner's part that has no price yet)."""
    if not text or len(text) < 10:
        return None
    phones = list(dict.fromkeys(n for n, _, _ in extract_from_text(text) if n))
    price = _price(text, [p[1:] for p in phones])
    sqft = None
    if m := SQFT.search(text):
        sqft = int(float(m.group(1).replace(",", "")))
    beds = baths = None
    # "One room plus one bath" -> 1 room 1 bath
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "satu": 1, "dua": 2, "tiga": 3,
             "empat": 4}
    numeric = re.sub(r"\b(" + "|".join(words) + r")\b", lambda w: str(words[w.group(1).lower()]), text, flags=re.I)
    if m := COMBO.search(numeric):
        beds, baths = int(m.group(1)), int(m.group(2))
    else:
        if m := ROOMS.search(numeric):
            beds = int(m.group(1))
        if m := BATHS.search(numeric):
            baths = int(m.group(1))
    furnishing = furnishing_in(text)
    is_rent, is_sale = bool(RENT.search(text)), bool(SALE.search(text))
    if is_rent and is_sale and price:
        kind = "Sale" if price >= 100_000 else "Rent"
    elif is_rent or is_sale:
        kind = "Rent" if is_rent else "Sale"
    elif price:
        kind = "Sale" if price >= 100_000 else "Rent" if price <= 50_000 else None
    else:
        kind = None
    # "Sale RM950k ... currently rented out for RM4.5k": the sale amount is the price, the other is the rental
    sale_v, rent_v = _keyword_amount(SALE_AMT, text, 50_000, 500_000_000), _keyword_amount(RENT_AMT, text, 300, 300_000)
    rental = None
    if sale_v:
        price, rental = sale_v, rent_v
        kind = "Sale / Rent" if rent_v and ASKING_RENT.search(text) else "Sale"
    elif rent_v and (not price or price < 50_000):
        price, kind = rent_v, "Rent"
    # Needs a price plus at least one other property clue to count as a listing
    if need_price and (not price or not (kind or sqft or beds or furnishing)):
        return None
    return {"type": kind, "price_rm": price, "sqft": sqft, "bedrooms": beds, "bathrooms": baths,
            "furnishing": furnishing, "place": _place(text), "phones": phones, "rental_rm": rental}


AMT = r"(?:rm|myr)?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k|mil(?:lion)?|m|juta)?\b"
SALE_AMT = re.compile(r"\b(?:for\s*sale|sale|selling(?:\s*price)?|sell(?:ing)?\s*(?:at|for)|asking(?:\s*(?:selling|sale))?"
                      r"(?:\s*price)?|jual|dijual|harga)\b(?:\s*(?:is|at|:|-|=|price|of))*\s*" + AMT, re.I)
RENT_AMT = re.compile(r"\b(?:rented\s*(?:out\s*)?(?:for|at)|tenanted\s*(?:for|at)|asking\s*rental|monthly\s*rental|"
                      r"rental|rent|sewa)\b(?:\s*(?:is|at|:|-|=|of|for))*\s*" + AMT, re.I)
ASKING_RENT = re.compile(r"\basking\s*rental|\bfor\s*rent\b|\bto\s*let\b|\bdisewakan\b", re.I)


def _keyword_amount(rx, text, lo, hi):
    """First amount right after a sale / rent keyword, within a sensible range (else None)."""
    for m in rx.finditer(text):
        v = _amount(m.group(1), m.group(2))
        if lo <= v <= hi:
            return int(v)
    return None


def group_listings(messages, window_s=600):
    """messages: bridge records. Returns listings with their photos attached.

    A photo belongs to the closest listing text from the same sender in the same chat within `window_s`
    (owners usually send photos just before or after the description). Photo captions can be listings too.
    """
    listings, photos = [], []
    for m in sorted(messages, key=lambda r: (r["chatId"], r["timestamp"])):
        text = full_text(m)
        fields = parse(text)
        ref = _ref(m)
        if fields:
            listings.append(_listing(fields, m, text, [ref] if ref else []))
        elif ref:
            photos.append((m, ref))
    for p, ref in photos:
        # Match on the sender's WhatsApp id (works even when their phone number is hidden)
        near = [l for l in listings if l["chat"] == p["chatName"] and l["sender"] == p.get("author")
                and abs(l["posted_at"] - p["timestamp"]) <= window_s]
        if near:
            min(near, key=lambda l: abs(l["posted_at"] - p["timestamp"]))["photos"].append(ref)
    return listings


# ---------- documents that list MANY properties (owner PDFs, WhatsApp catalogue screenshots) ----------
DATE_TAG = re.compile(r"\s*[（(]\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{0,2}\s*[)）]", re.I)
UNIT = re.compile(r"(?<![\w/])((?:[A-Z]{1,3}\d{0,3}[A-Z]?|\d{1,3}[A-Z])(?:-[A-Z0-9]{1,4}){1,3})(?![\w-])")
UNIT_WORD = re.compile(r"\b(?:unit|no\.?|lot)\s*[:.]?\s*([A-Z]{0,2}\d{1,4}[A-Z]?)\b", re.I)
PDF_LABELS = ["Location", "Address", "Type", "Building position", r"Rental \(per month\)", "and tenure", "Price",
              "Yield", "Note", "Total built area", "Total land area", "Developer", "Freehold/Leasehold"]
PDF_LABEL = re.compile(r"^\s*(" + "|".join(PDF_LABELS) + r")(?=[\s:]|$)\s*:?\s*(.*)$", re.I)
PROPERTY_HEAD = re.compile(r"^\s*Property\s+(\d+(?:\s*&\s*(?:Property\s+)?\d+)*)\s*$", re.I | re.M)
CATALOGUE_PRICE = re.compile(r"^\s*R[MN]\s?(\d[\d.,\s]*\d)\s*(?:\d{1,2}:\d{2}\s*[ap]m)?\s*$", re.I)
SUMMARY_TITLE = re.compile(r"\b(total|shops? and|shops? &|commission|strong\s*points|price low|roi high|ranking|"
                           r"ets\b|in operation|catalogue)\b", re.I)


def unit_no(*texts):
    """Unit number such as G-19, A-G-29, E-0-8, B15-06, 33A-8, N-51-03 (needs a letter, so phones/dates don't match)."""
    for t in texts:
        if not t:
            continue
        for m in UNIT.finditer(t):
            u = m.group(1)
            if re.search(r"[A-Z]", u) and not re.fullmatch(r"[A-Z]+-[A-Z]+", u):
                return u
        if m := UNIT_WORD.search(t):
            return m.group(1).upper()
    return None


def _money(s):
    """RM amount in a field value: 'RM5,000', '3,800,000', 'RM 4,700'. None if absent."""
    if not s:
        return None
    m = re.search(r"R[MN]\s*([\d,]+(?:\.\d+)?)", s, re.I) or re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)", s)
    if m:
        return int(float(m.group(1).replace(",", "")))
    m = MONEY.search(s)
    return int(_amount(m.group(2), m.group(3))) if m and (m.group(1) or m.group(3)) else None


def _clean_place(s):
    s = DATE_TAG.sub("", s or "")
    s = re.sub(r"\s*[（(]\s*valuation[^)）]*[)）]", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,.-:") or None


def split_pdf_properties(text):
    """'Property 1 / Location ... / Address ... / Price ...' blocks (owner PDFs) -> one dict per property."""
    heads = list(PROPERTY_HEAD.finditer(text))
    if len(heads) < 2:
        return None
    blocks = {}
    for i, h in enumerate(heads):
        key = "&".join(re.findall(r"\d+", h.group(1)))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        blocks[key] = blocks.get(key, "") + "\n" + text[h.end():end]
    props = []
    for key, body in blocks.items():
        f, cur = {}, None
        for line in body.splitlines():
            if m := PDF_LABEL.match(line):
                cur = m.group(1).lower()
                if cur not in f:
                    f[cur] = m.group(2).strip()
                elif cur != "location":
                    f[cur] += " " + m.group(2).strip()
                if cur == "location":
                    cur = None  # lines after 'Location' are photo captions, not part of it
            elif cur and line.strip():
                f[cur] = (f[cur] + " " + line.strip()).strip()
        price = _money(f.get("price"))
        psf = None
        if price and re.match(r"\s*R[MN]?\s*[\d,.]+\s*(?:per\s*sq|psf|p\.s\.f)", f.get("price", ""), re.I):
            psf, price = price, None  # land priced per square foot
        rental = _money(f.get("rental (per month)")) or _money(f.get("and tenure"))
        if not (price or rental or psf):
            continue
        area = f.get("total built area", "") or f.get("total land area", "")
        size = SQFT.search(area) or re.search(r"([\d,]{3,})\s*sq", area)
        acres = re.search(r"([\d.]+)\s*acres?", area, re.I)
        sqft = int(size.group(1).replace(",", "")) if size else int(float(acres.group(1)) * 43_560) if acres else None
        tenure = re.match(r"\s*(freehold|leasehold)", f.get("freehold/leasehold", ""), re.I)
        props.append({
            "type": "Sale" if (price or psf) else "Rent", "price_rm": price or (None if psf else rental),
            "price_psf": psf, "rental_rm": rental if (price or psf) else None,
            "place": _clean_place(f.get("location")), "address": _clean_place(f.get("address")),
            "unit_no": unit_no(f.get("address"), f.get("location")), "property_type": f.get("type") or None,
            "tenure": tenure.group(1).title() if tenure else None,
            "sqft": sqft, "bedrooms": None, "bathrooms": None,
            "furnishing": furnishing_in(body), "phones": [], "text": f"Property {key}\n{body.strip()}"})
    return props


UI_LINE = re.compile(r"^\s*(\d{1,2}[:.]\d{2}\b.*|.*\bbusiness account\b.*|[+\d][\d\s+()-]{8,}|view|message.*|"
                     r"\W*\d{1,3}\W*)$", re.I)
INLINE_PRICE = re.compile(r"\s+R[MN]\s?(\d{1,3}(?:[.,]\d{3})+[.,]\d{2})\s*(?:\d{1,2}:\d{2}\s*[ap]m)?\s*$", re.I)


def _catalogue_price(s):
    digits = re.sub(r"[.,\s]", "", re.sub(r"[.,]\d{2}\s*$", "", s))
    return int(digits) if digits.isdigit() else None


def split_catalogue(text):
    """WhatsApp catalogue screenshots: 'title / description / RM 1,200,000.00' repeated -> one dict per item."""
    lines = [l for l in text.splitlines() if l.strip()]
    if sum(1 for l in lines if CATALOGUE_PRICE.match(l) or INLINE_PRICE.search(l)) < 2:
        return None
    props, buf = [], []
    items = []  # (title/description lines, price)
    for line in lines:
        if m := CATALOGUE_PRICE.match(line):
            items.append((buf, _catalogue_price(m.group(1))))
            buf = []
        elif m := INLINE_PRICE.search(line):  # "Plaza Damas E-10 ... RM 3,100,000.00" on one line
            items.append((buf + [line[:m.start()]], _catalogue_price(m.group(1))))
            buf = []
        elif not line.lstrip().startswith("←") and not UI_LINE.match(line):
            buf.append(line.strip())
    for chunk, price in items:
        title, desc = (chunk[0] if chunk else ""), " ".join(chunk[1:])
        if (not price or not title or price > 30_000_000 or price < 500 or SUMMARY_TITLE.search(f"{title} {desc}")
                or not re.search(r"[A-Za-z]{4}", title)):
            continue
        rent = re.search(r"\brental\s*(?:rm\s*)?([\d.,]+\s*k?)", desc, re.I)
        size = SQFT.search(desc) or SQFT.search(title)
        props.append({
            "type": "Sale" if price >= 100_000 else "Rent", "price_rm": price,
            "rental_rm": _money(rent.group(1) if rent and "k" in rent.group(1).lower() else
                                ("RM" + rent.group(1)) if rent else None) if price >= 100_000 else None,
            "place": _clean_place(re.sub(r"^[\d&\sA-Za-z]{0,12}\([^)]*\)\s*:\s*", "", title)),
            "address": None, "unit_no": unit_no(title, desc), "property_type": None, "tenure":
                (re.search(r"\b(freehold|leasehold)\b", desc, re.I) or [None, None])[1],
            "sqft": int(float(size.group(1).replace(",", ""))) if size else None, "bedrooms": None,
            "bathrooms": None, "furnishing": furnishing_in(desc), "phones": [], "text": f"{title}\n{desc}".strip()})
    return props


def is_summary_table(text):
    """Price/ROI ranking tables: many lines with an RM amount and a % - totals, not a listing."""
    rows = [l for l in text.splitlines() if re.search(r"R[MN]\s?[\d,]", l, re.I) and "%" in l]
    return len(rows) >= 3 and re.search(r"\btotal\b", text, re.I) is not None


def properties_in(text):
    """All properties described in one message / screenshot / PDF (list; empty if none)."""
    if not text:
        return []
    for splitter in (split_pdf_properties, split_catalogue):
        found = splitter(text)
        if found is not None:
            return found
    if is_summary_table(text):
        return []
    if len([m for m in MONEY.finditer(text) if m.group(1) or m.group(3)]) >= 3 and not SQFT.search(text):
        return []  # a general message mentioning several prices ("my shops: A 3.5k, B Rm5k, C Rm3k...")
    f = parse(text)
    if not f:
        return []
    f.update(address=None, unit_no=unit_no(text), property_type=None, tenure=None, text=text)
    f["place"] = _clean_place(f["place"])
    return [f]


# ---------- owner name ----------
NAME = r"([A-Z][a-zA-Z'.]+(?:\s+[A-Z][a-zA-Z'.]+){0,3})"
NAME_PATTERNS = [re.compile(p, re.M) for p in (
    r"(?:regards|thanks|thank you|cheers)[,!.\s]+" + NAME,
    r"🙏\s*" + NAME,
    r"\bby\s+(?:Mr\.?\s|Ms\.?\s|Mdm\.?\s|Madam\s)?" + NAME + r"(?:\.pdf)?\s*$",
    r"contact\s+(?:Mr|Ms|Mrs|Mdm|Madam)\.?\s+" + NAME,
    r"^\s*(?:owner|name|contact person)\s*[:\-]\s*" + NAME,
)]
# Self-introductions ("Hi this is May", "I am Ali", "saya Siti") and greetings back ("Thanks May") - names in
# chats are often typed in lower case, so these match any case and are capitalised afterwards
NAME_CI = r"([A-Za-z][A-Za-z']{1,15}(?:\s+[A-Z][a-z']{1,15})?)"
INTRO = re.compile(r"\b(?:this is|i am|i'm|im|my name is|my name's|nama saya|saya)\s+" + NAME_CI, re.I)
THANKS = re.compile(r"^\s*(?:thanks|thank you|tq|thx|ok|okay|noted|hi|hello|dear)[,!.\s]+" + NAME_CI + r"\s*[.!]*\s*$",
                    re.I | re.M)
NOT_A_NAME_WORD = {"a", "an", "the", "my", "our", "for", "from", "about", "regarding", "unit", "units", "property",
                   "owner", "agent", "interested", "looking", "selling", "renting", "here", "not", "only", "you",
                   "so", "very", "much", "bro", "sis", "boss", "dear", "all", "again", "sure", "ya", "yes", "no",
                   "ok", "sorry", "available", "also", "still", "the", "in", "at", "on", "with", "and", "too",
                   "willing", "keen", "busy", "free", "back", "now", "going", "selling", "having", "noted",
                   "received", "done", "good", "great", "nice", "sir", "madam", "then", "lah", "la", "see",
                   "will", "can", "pm", "dm", "thanks", "thank", "tq", "welcome", "morning", "evening", "night",
                   "afternoon", "everyone", "guys", "there", "again"}
NOT_NAME = re.compile(r"\b(business|account|view|message|catalogue|whatsapp|property|properties|shop|condo|"
                      r"managing|director|sdn|bhd|developments?|segamat|kuala|lumpur|address|suite)\b", re.I)


def owner_name(texts):
    """Most likely owner name across one owner's messages / screenshots / PDFs (or None)."""
    votes = {}
    for t in texts:
        if not t:
            continue
        found = [re.sub(r"\.pdf$", "", m.group(1), flags=re.I) for rx in NAME_PATTERNS for m in rx.finditer(t)]
        for rx in (INTRO, THANKS):
            for m in rx.finditer(t):
                words = m.group(1).split()
                while words and words[-1].lower() in NOT_A_NAME_WORD:
                    words.pop()
                if words and words[0].lower() not in NOT_A_NAME_WORD:
                    found += [" ".join(w[:1].upper() + w[1:].lower() for w in words)] * (2 if rx is INTRO else 1)
        # Business cards: a 2-4 word ALL-CAPS line right next to a phone number line
        lines = [l.strip() for l in t.splitlines()]
        for i, l in enumerate(lines):
            if re.fullmatch(r"[A-Z][A-Z.' ]{3,40}", l) and 2 <= len(l.split()) <= 4 and any(
                    re.search(r"\+?6?0\d[\d\s-]{7,}", x) for x in lines[max(0, i - 4):i + 5]):
                found.append(l.title().rstrip(". "))
        for name in found:
            name = re.sub(r"\s+(Y\.?T\.?|Owner|Agent)\.?$", "", name.strip(" .,"), flags=re.I)
            if len(name) >= 3 and not NOT_NAME.search(name) and len(name.split()) <= 4:
                key = name.lower()
                votes[key] = (votes.get(key, (0, name))[0] + 1, name)
    if not votes:
        return None
    return max(votes.values(), key=lambda v: (v[0], len(v[1])))[1]


def _same_property(a, b):
    """Same property seen twice (e.g. in a PDF and in a catalogue screenshot)?"""
    if a["price_rm"] != b["price_rm"]:
        return False
    if a.get("unit_no") and b.get("unit_no"):  # E-8 == E-0-8, AG-29 == A-G-29
        norm = lambda u: re.sub(r"\W", "", re.sub(r"-0+(?=-)", "", u)).lower()
        return norm(a["unit_no"]) == norm(b["unit_no"])

    def tokens(p):
        s = re.sub(r"\d+'\s*\*\s*\d+'", " ", " ".join(x for x in (p.get("place"), p.get("address")) if x))
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 2}
    ta, tb = tokens(a), tokens(b)
    nums_a, nums_b = {w for w in ta if w.isdigit()}, {w for w in tb if w.isdigit()}
    # A shared 5-digit number is the postcode (a whole town shares 85000 Segamat) - it proves nothing. Other
    # numbers (house / lot / unit, or two different postcodes) must have something in common.
    postcode = {n for n in nums_a & nums_b if len(n) == 5}
    house_a, house_b = nums_a - postcode, nums_b - postcode
    if house_a and house_b and not house_a & house_b:
        return False
    words = (ta - nums_a - GENERIC_WORDS) & (tb - nums_b - GENERIC_WORDS)
    return len(words) >= 1


# Words that appear in many addresses - sharing only these doesn't make two listings the same property
GENERIC_WORDS = {"jalan", "jln", "taman", "tmn", "lorong", "persiaran", "lebuh", "lebuhraya", "kampung", "kg", "bandar",
                 "lot", "no", "unit", "the", "of", "and", "for", "at", "sale", "rent", "land", "acre", "acres", "gross",
                 "nett", "net", "kuala", "lumpur", "kl", "selangor", "johor", "darul", "ehsan", "takzim", "wilayah",
                 "persekutuan", "malaysia", "mukim", "daerah", "sungai", "segamat", "petaling", "subang", "jaya", "shah",
                 "alam", "seksyen", "section", "utama", "baru", "indah", "jaya", "city", "tower", "block", "blok",
                 "level", "floor", "ground"}


# ---------- simple property type for the dashboard (Condo, Shop, Land, ...) ----------
PROPERTY_TYPES = [  # first match wins - order matters ("Bungalow Land" is land, "ShopOffice" is a shop)
    ("Factory / Warehouse", r"factor(y|ies)|warehouse|industrial|kilang|gudang|工厂|厂房|仓库"),
    ("Land", r"\bland\b|\bacres?\b|\btanah\b|vacant lot|agricultur|土地|地皮"),
    ("Shop / Shop office", r"shop ?office|shop ?lot|shop ?house|storey shop|retail|\bshops?\b|kedai|店屋|店铺|"
                           r"\b\d(\.5)? ?(shc?|sso|ssh)\b"),  # agents' shorthand: 2Shc = 2-storey shop (corner)
    ("Office / Commercial", r"office|commercial|\bsofo\b|\bsoho\b|\bsovo\b|pejabat|办公"),
    ("Bungalow", r"bungalow|banglo|独立式"),
    ("Semi-D", r"semi[- ]?d\b|semi[- ]?detached|半独立"),
    ("Townhouse", r"town ?house"),
    ("Terrace", r"terrace|link ?house|\bteres\b|排屋"),
    ("Condo / Apartment", r"condo|condominium|apartment|serviced? (residence|apartment|suite)|residen|\bsuites?\b|"
                          r"penthouse|duplex|\bflat\b|pangsapuri|公寓"),
]


def property_category(stated_type, place, message="", condo_names=()):
    """'Condo / Apartment', 'Shop / Shop office', 'Land', ... or 'Unknown'. Looks at the stated type, then the
    place, then known condo names (e.g. from Condo prices), then the message text."""
    def by_words(text):
        t = (text or "").lower()
        return next((label for label, pattern in PROPERTY_TYPES if t and re.search(pattern, t)), None)

    found = by_words(stated_type) or by_words(place)
    if found:
        return found
    p = re.sub(r"\W+", " ", (place or "").lower()).strip()
    for name in condo_names:
        n = re.sub(r"\W+", " ", (name or "").lower()).strip()
        if len(n) >= 4 and len(p) >= 4 and (n in p or p in n):
            return "Condo / Apartment"
    return by_words(message) or "Unknown"


# ---------- chats where you separate owners with a "." message ----------
SEPARATOR = re.compile(r"^\s*[.。·•]+\s*$")


def full_text(m):
    """Message text + text read from its screenshot / PDF."""
    return "\n".join(x for x in (m.get("body"), m.get("ocr_text"), m.get("pdf_text")) if x).strip()


def _ref(m):
    """The message's file: name if downloaded, else (chat, message id) still to download; None if no file."""
    if m.get("photo"):
        return m["photo"]
    return (m["chatId"], m["id"]) if (m.get("has_image") or m.get("is_pdf") or m.get("is_video")) else None


def _listing(fields, m, text, photos):
    one_to_one = not m["chatId"].endswith("@g.us") and re.search(r"[A-Za-z]{2}", m.get("chatName") or "")
    fields = {"owner_name": m["chatName"] if one_to_one else None} | fields
    return {**fields, "id": m["id"], "chat": m["chatName"], "posted_at": m["timestamp"], "sender": m.get("author"),
            "sender_name": m.get("authorName"), "sender_phone": m.get("authorNumber"), "text": text,
            "photos": photos, "from_picture": bool(m.get("ocr_text") or m.get("pdf_text"))}


def uses_separators(messages):
    return any(not (m.get("has_image") or m.get("is_pdf") or m.get("is_video"))
               and SEPARATOR.match(m.get("body") or "") for m in messages)


def group_by_owner(messages, mine=(), overrides=None):
    """Everything between two '.' messages belongs to ONE owner (e.g. screenshots of one person's chat).
    Each property found in that part becomes a listing carrying the owner's number; the part's other screenshots,
    PDFs and photos are attached to it. A part with only a number or a price is still kept as one record."""
    out = []
    by_chat = {}
    for m in sorted(messages, key=lambda r: (r["chatId"], r["timestamp"])):
        by_chat.setdefault(m["chatId"], []).append(m)
    for msgs in by_chat.values():
        parts, cur = [], []
        for m in msgs:
            if not (m.get("has_image") or m.get("is_pdf") or m.get("is_video")) and SEPARATOR.match(m.get("body") or ""):
                if cur:
                    parts.append(cur)
                cur = []
            else:
                cur.append(m)
        if cur:
            parts.append(cur)
        _move_late_items(parts, set(mine), overrides or {})
        for n, part in enumerate(parts, 1):
            out += _owner_part(part, n, set(mine))
    return out


def _phones_in(m, mine):
    return {x for x, _, _ in extract_from_text(full_text(m)) if x and x not in mine}


def _move_late_items(parts, mine, overrides):
    """Things sent later (e.g. a PDF forwarded afterwards) land in the last owner's part. Move them to the right
    owner: 0) if you assigned it ('overrides': message id -> a message id in the owner's part), 1) if sent as a
    WhatsApp reply to a message in another part, or 2) if they show the phone number of an owner in another part
    (and not one of their current part's numbers)."""
    where = {m["id"]: i for i, part in enumerate(parts) for m in part}
    moves = []
    for i, part in enumerate(parts):
        for m in part:
            target = where.get(overrides.get(m["id"])) if m["id"] in overrides else where.get(m.get("reply_to"))
            if target is None:
                mine_here = set().union(*(_phones_in(x, mine) for x in part if x is not m)) if len(part) > 1 else set()
                own = _phones_in(m, mine)
                if own and not own & mine_here:
                    hits = [j for j, other in enumerate(parts) if j != i
                            and own & set().union(*(_phones_in(x, mine) for x in other))]
                    target = hits[0] if len(hits) == 1 else None
            if target is not None and target != i:
                moves.append((m, i, target))
    for m, src, dst in moves:
        parts[src].remove(m)
        parts[dst].append(m)
    for p in parts:
        p.sort(key=lambda m: m["timestamp"])
    parts[:] = [p for p in parts if p]


EXTRA_FIELDS = ("address", "unit_no", "property_type", "tenure", "rental_rm", "price_psf", "sqft", "bedrooms",
                "bathrooms", "furnishing", "place", "type")


def _owner_part(part, n, mine):
    texts = [(m, full_text(m)) for m in part]
    everything = "\n".join(t for _, t in texts if t)
    owner_phones = [p for p in dict.fromkeys(x for x, _, _ in extract_from_text(everything) if x) if p not in mine]
    # Owner name: from the owner's messages and the screenshots / PDFs you forward - but from YOUR typed messages
    # only greetings to the owner ("Thanks May"), never your own sign-off
    name_texts = []
    for m, text in texts:
        if m.get("fromMe"):
            name_texts += [m.get("ocr_text") or "", m.get("pdf_text") or "",
                           "\n".join(x.group(0) for x in THANKS.finditer(m.get("body") or ""))]
        else:
            name_texts.append(text)
    name = owner_name(name_texts)
    chat_name = part[0].get("chatName") or ""
    if not name and not part[0]["chatId"].endswith("@g.us") and re.search(r"[A-Za-z]{2}", chat_name):
        name = chat_name  # one-to-one chat: the contact's name
    props, extras = [], []
    # PDFs first (most detail), then screenshots / messages fill gaps and add their files
    for m, text in sorted(texts, key=lambda mt: not mt[0].get("is_pdf")):
        ref = _ref(m)
        found = properties_in(text)
        if not found:
            if ref:
                extras.append((m, ref))
            continue
        for fields in found:
            fields = {k: v for k, v in fields.items() if k != "text"} | {"text": fields.get("text") or text}
            same = next((p for p in props if _same_property(p, fields)), None)
            if same:  # seen already (e.g. in the PDF and again in a catalogue screenshot)
                for k in EXTRA_FIELDS:
                    if not same.get(k) and fields.get(k):
                        same[k] = fields[k]
                if ref and ref not in same["photos"]:
                    same["photos"].append(ref)
                continue
            p = _listing(fields, m, fields["text"], [ref] if ref else [])
            key = re.sub(r"[^a-z0-9]+", "", (fields.get("unit_no") or fields.get("place") or "").lower())[:30]
            p["id"] = f"{m['id']}#{key or len(props)}" if len(found) > 1 else m["id"]
            props.append(p)
    if not props:  # only a number / a price / photos for this owner - still keep one record
        price = _price(everything, [p[1:] for p in owner_phones])
        if not (owner_phones or price):
            return []
        first = next((m for m, _ in texts), part[0])
        fields = parse(everything) or parse(everything, need_price=False) or {
            "type": None, "price_rm": price, "sqft": None, "bedrooms": None,
                                       "bathrooms": None, "furnishing": furnishing_in(everything), "place": _place(everything),
                                       "phones": owner_phones}
        props.append(_listing(fields, first, everything, []))
    sources = {p["posted_at"] for p in props}
    for m, ref in extras:
        cap = (m.get("body") or "").lower()
        # 1) captioned with a property's unit -> that property only
        target = [p for p in props if p.get("unit_no") and p["unit_no"].lower() in cap]
        # 2) properties described in separate messages (e.g. a chat) -> the one sent closest in time
        if not target and len(sources) > 1:
            nearest = min(sources, key=lambda t: abs(t - m["timestamp"]))
            target = [p for p in props if p["posted_at"] == nearest]
        # 3) otherwise (one PDF / catalogue listing several properties) -> all of this owner's properties
        for p in target or props:
            if ref not in p["photos"]:
                p["photos"].append(ref)
    for p in props:
        p["phones"] = [x for x in p.get("phones") or [] if x not in mine] or owner_phones
        p["owner_no"], p["owner_phones"], p["owner_name"] = n, owner_phones, name
        for k in EXTRA_FIELDS:
            p.setdefault(k, None)
    return props
