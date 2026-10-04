"""Re-download every seeded medicine's FDA label from openFDA and check LIFELOG's rules against the live text.

    python scripts/verify_labels.py           # report only
    python scripts/verify_labels.py --write   # also update the saved label text in data/labels/

Reports a newer label version, any quote no longer found word-for-word, and any number that no longer matches."""
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import medicines as M  # noqa: E402

FIELDS = ("storage_and_handling", "how_supplied", "how_supplied_storage_and_handling", "information_for_patients",
          "instructions_for_use", "spl_patient_package_insert", "dosage_and_administration")


def main(write: bool) -> int:
    problems = 0
    for key, m in M.PRODUCTS.items():
        saved = M.label(key)
        r = httpx.get("https://api.fda.gov/drug/label.json", params={"search": f'set_id:"{saved["set_id"]}"', "limit": 1}, timeout=30)
        if r.status_code != 200:
            print(f"{key:9} could not fetch (HTTP {r.status_code})"); problems += 1
            continue
        lab = r.json()["results"][0]
        fields = {k: re.sub(r"\s+", " ", " ".join(lab[k])) for k in FIELDS if lab.get(k)}
        res = M.check_model(m, " ".join(fields.values()))
        newer = lab.get("effective_time") != saved["effective_time"]
        status = "OK" if res["all_verified"] and not newer else "CHECK"
        problems += status != "OK"
        print(f"{key:9} {status:5} label {lab.get('effective_time')}{' (NEW: was ' + saved['effective_time'] + ')' if newer else ''} "
              f"· {len(res['verified'])} quotes verified")
        for u in res["unverified"]:
            print(f"          not found ({u['field']}): {u['quote'][:110]}")
        for n in res["number_issues"]:
            print(f"          {n}")
        if write:
            saved.update(effective_time=lab.get("effective_time"), fields=fields)
            (M.LABELS / f"{key}.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(M.PRODUCTS)} medicines, {problems} need attention")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
