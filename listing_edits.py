"""Your own corrections to saved listings (Saved listings tab -> Edit): unit number, type, rent / sale price, furnishing.

Each edit goes into the listing row (owner_listings) straight away, and is also kept in Supabase storage
owner-photos/data/listing_edits.json so a later WhatsApp scan of the same chat doesn't put the old values back
(wa_scan.scan applies them again before saving).
"""
import json
import time
from datetime import datetime, timezone

BUCKET, PATH = "owner-photos", "data/listing_edits.json"
TYPES = ["Condo / Apartment", "Shop / Shop office", "Office / Commercial", "Factory / Warehouse", "Land", "Bungalow",
         "Semi-D", "Terrace", "Townhouse"]
FURNISH = {"Fully furnished": "Fully Furnished", "Partial": "Partially Furnished", "Bare": "Unfurnished"}


def furnish_choice(stored):
    """'Partially Furnished (from photos)' -> 'Partial' (the 3 choices on the dashboard), None if not known."""
    s = (stored or "").lower()
    return "Bare" if "unfurnish" in s or "bare" in s else "Partial" if "partial" in s or "semi" in s \
        else "Fully furnished" if "furnish" in s else None


def load(db):
    try:
        return json.loads(db.storage.from_(BUCKET).download(PATH))
    except Exception:
        return {}


def row_changes(row, unit=None, ptype=None, rent=None, sale=None, furnish=None):
    """What you typed -> the listing's fields. A sale price makes it a sale (the rent = what a tenant pays); only a
    rent makes it a rental."""
    out = {"unit_no": unit or None, "property_type": ptype or row.get("property_type"),
           "furnishing": FURNISH.get(furnish, row.get("furnishing")) if furnish else row.get("furnishing")}
    if sale:
        kind = row.get("listing_type") if (row.get("listing_type") or "").startswith("Sale") else "Sale"
        out.update(listing_type=kind, price_rm=int(sale), rental_rm=int(rent) if rent else None)
    elif rent:
        out.update(listing_type="Rent", price_rm=int(rent), rental_rm=None)
    else:
        out.update(price_rm=None, rental_rm=None)
    return out


def save(db, row, changes):
    """Into the listing now (scanned_at = now, so the PropertyGuru comparison runs again) + remembered for scans -
    only what you changed, so an owner's later price still comes through for fields you didn't touch.
    -> the fields changed ({} = nothing)."""
    changes = {k: v for k, v in changes.items() if row.get(k) != v}
    if not changes:
        return {}
    message_id = row["message_id"]
    db.table("owner_listings").update({**changes, "scanned_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("message_id", message_id).execute()
    edits = load(db)
    edits[message_id] = {**edits.get(message_id, {}), **changes, "_at": time.time()}
    db.storage.from_(BUCKET).upload(PATH, json.dumps(edits, ensure_ascii=False).encode(),
                                    {"content-type": "application/json", "upsert": "true"})
    return changes


def apply(db, rows):
    """wa_scan: your edits over what a scan read (same listing = same message id)."""
    edits = load(db)
    for r in rows:
        for k, v in (edits.get(r["message_id"]) or {}).items():
            if not k.startswith("_"):
                r[k] = v
    return rows
