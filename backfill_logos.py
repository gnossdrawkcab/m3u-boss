"""One-shot logo backfill for M3U Boss.

For each enabled channel with an empty logo, try in order:
  1. <icon> in the exported EPG XML, matched by tvg_id
  2. Another channel row in the DB with the same non-empty tvg_id and a logo
  3. Another channel row in the DB with the same name (case-insensitive) and a logo
     — skipped for names <6 chars (too generic, e.g. "News")
     — skipped when sibling logos disagree (ambiguous)

Dry-run by default. Pass --apply to write.
"""
import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "sources.db"
EPG_XML = ROOT / "exports" / "organized.xml"


def _pick(values):
    """Pick the most common value; return None if ambiguous & no clear majority."""
    if not values:
        return None
    ctr = Counter(values)
    top, n = ctr.most_common(1)[0]
    return top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write to DB")
    args = ap.parse_args()

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    no_logo = list(con.execute(
        "SELECT id, name, source_group, tvg_id FROM channels "
        "WHERE enabled=1 AND (logo IS NULL OR logo='')"
    ))
    print(f"Channels missing logo: {len(no_logo)}")

    # EPG icon lookup
    epg_icons: dict[str, str] = {}
    if EPG_XML.exists():
        root = ET.parse(str(EPG_XML)).getroot()
        for ch in root.findall("channel"):
            icon = ch.find("icon")
            if icon is not None and icon.attrib.get("src"):
                epg_icons[ch.attrib.get("id", "")] = icon.attrib["src"]
    print(f"EPG channels with icon: {len(epg_icons)}")

    # Internal: tvg_id -> [logos]
    tvg_logos: dict[str, list[str]] = {}
    for tvg_id, logo in con.execute(
        "SELECT tvg_id, logo FROM channels "
        "WHERE tvg_id != '' AND logo != '' AND logo IS NOT NULL"
    ):
        tvg_logos.setdefault(tvg_id, []).append(logo)

    # Internal: name (lower) -> [logos]
    name_logos: dict[str, list[str]] = {}
    for name, logo in con.execute(
        "SELECT name, logo FROM channels WHERE logo != '' AND logo IS NOT NULL"
    ):
        if name:
            name_logos.setdefault(name.strip().lower(), []).append(logo)

    updates: list[tuple[int, str, str]] = []  # (id, logo, source)
    skipped_ambiguous = 0
    for row in no_logo:
        cid, name, _grp, tvg = row["id"], row["name"], row["source_group"], (row["tvg_id"] or "").strip()
        chosen = None
        src = None

        if tvg and tvg in epg_icons:
            chosen = epg_icons[tvg]
            src = "epg"
        elif tvg and tvg in tvg_logos:
            chosen = _pick(tvg_logos[tvg])
            src = "tvg_id"
        elif name and len(name.strip()) >= 6:
            key = name.strip().lower()
            if key in name_logos:
                candidates = name_logos[key]
                # require consensus when matching by name
                if len(set(candidates)) == 1:
                    chosen = candidates[0]
                    src = "name"
                else:
                    skipped_ambiguous += 1

        if chosen:
            updates.append((cid, chosen, src))

    src_ctr = Counter(u[2] for u in updates)
    print(f"\nWill update: {len(updates)}")
    for s, n in src_ctr.most_common():
        print(f"  {n:5}  via {s}")
    print(f"Skipped (ambiguous name match): {skipped_ambiguous}")
    print(f"Remaining without logo: {len(no_logo) - len(updates)}")

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        # show a sample
        print("\nSample updates:")
        for cid, logo, src in updates[:10]:
            r = con.execute("SELECT name, source_group FROM channels WHERE id=?", (cid,)).fetchone()
            print(f"  [{src}] id={cid} {r['name']!r} ({r['source_group']}) -> {logo}")
        return

    cur = con.cursor()
    cur.executemany("UPDATE channels SET logo=? WHERE id=?", [(u[1], u[0]) for u in updates])
    con.commit()
    print(f"\nApplied {cur.rowcount} updates.")


if __name__ == "__main__":
    sys.exit(main())
