"""Read a manufacturer's label straight from DailyMed (NLM) when openFDA doesn't have it, e.g. Ozempic and Wegovy
injections, for which openFDA only returns tablet or repackager labels. DailyMed serves the same FDA Structured
Product Label (SPL) XML that openFDA is built from."""
import re
import xml.etree.ElementTree as ET

import httpx

BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
NS = {"v3": "urn:hl7-org:v3"}
SECTIONS = {   # LOINC section codes in the SPL
    "34069-5": "how_supplied", "44425-7": "storage_and_handling", "34076-0": "information_for_patients",
    "59845-8": "instructions_for_use", "42230-3": "spl_patient_package_insert", "34068-7": "dosage_and_administration",
}


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def search(name: str, manufacturer: str | None = None) -> list[dict]:
    r = httpx.get(f"{BASE}/spls.json", params={"drug_name": name, "pagesize": 50}, timeout=30)
    r.raise_for_status()
    rows = [{"set_id": d["setid"], "title": d["title"], "published": d.get("published_date")} for d in r.json().get("data", [])]
    if manufacturer:
        rows = [x for x in rows if manufacturer.lower() in x["title"].lower()]
    return rows


def label(set_id: str) -> dict:
    """The label's storage-related sections as plain text, keyed like openFDA's fields."""
    r = httpx.get(f"{BASE}/spls/{set_id}.xml", timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    eff = root.find("v3:effectiveTime", NS)
    fields: dict[str, str] = {}
    for sec in root.iter("{urn:hl7-org:v3}section"):
        code = sec.find("v3:code", NS)
        key = SECTIONS.get(code.get("code") if code is not None else "")
        if key and key not in fields:
            fields[key] = _text(sec)
    title = root.find("v3:title", NS)
    return {"set_id": set_id, "effective_time": eff.get("value")[:8] if eff is not None else "",
            "title": _text(title) if title is not None else "", "fields": fields, "source": "dailymed"}
