#!/usr/bin/env python3
"""
make_report.py - one command from workpaper to Financial Reports PDF.

Runs build_pack.py (post the workpaper to the firm chart -> pack.json, mapping.md,
ledger.csv) and then render_pack.py (pack.json -> PDF) into one output folder.
While any mapping line still needs review the PDF is stamped DRAFT; pass --final
once every REVIEW line has been resolved (override or chart change).

  python3 scripts/make_report.py --out-dir out/ --workpaper "2026 Workpapers - Client.xlsm" \\
      --name "Client Pty Ltd" --abn "12 345 678 901" --fy-end 2026-06-30 --type company \\
      --directors "Jane Citizen;John Citizen"

  # after the preparer asks for changes - re-run with the saved overrides plus new ones
  python3 scripts/make_report.py --out-dir out/ --workpaper ... --overrides out/pack.json \\
      --set-code "650.1=ASS.NCA.REC" --final

Every option other than --out-dir / --final / --allow-errors / --order is passed to
build_pack.py unchanged (see its --help).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--final", action="store_true", help="no DRAFT stamp (only once nothing needs review)")
    ap.add_argument("--allow-errors", action="store_true", help="render even with validation errors (always DRAFT)")
    ap.add_argument("--order", help="report order for render_pack.py, comma separated")
    ap.add_argument("--pdf-name", help="file name for the PDF (default: 'YYYY Financial Report - <Entity>.pdf')")
    args, rest = ap.parse_known_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pack_path, mapping_path, ledger_path = out / "pack.json", out / "mapping.md", out / "ledger.csv"
    if "--overrides" in rest:
        # a previous pack.json in the same folder is about to be overwritten - read it first
        i = rest.index("--overrides")
        src = Path(rest[i + 1])
        if src.resolve() == pack_path.resolve():
            keep = out / "overrides.json"
            data = json.loads(src.read_text(encoding="utf-8"))
            keep.write_text(json.dumps(data.get("overrides", {}), indent=2), encoding="utf-8")
            rest[i + 1] = str(keep)

    b = subprocess.run([sys.executable, str(HERE / "build_pack.py"), *rest, "--out", str(pack_path),
                        "--mapping-out", str(mapping_path), "--ledger-out", str(ledger_path)],
                       capture_output=True, text=True)
    sys.stdout.write(b.stdout)
    if b.returncode != 0:
        sys.stderr.write(b.stderr)
        sys.exit(b.returncode)

    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    review = [a for a in pack["accounts"] if a.get("mapping", {}).get("review")]
    ent = pack["entity"]
    year = (ent.get("fy_end") or "")[:4]
    pdf = out / (args.pdf_name or f"{year} Financial Report - {ent.get('name', 'Entity')}.pdf")
    draft = (not args.final) or bool(review) or args.allow_errors
    cmd = [sys.executable, str(HERE / "render_pack.py"), str(pack_path), str(pdf)]
    if draft:
        cmd.append("--draft")
    if args.allow_errors:
        cmd.append("--allow-errors")
    if args.order:
        cmd += ["--order", args.order]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    print(f"\n== Output ({out})")
    for p in (pdf, pack_path, mapping_path, ledger_path):
        print(f"  {p.name}")
    if review:
        print(f"\n{len(review)} line(s) still need review - the PDF is stamped DRAFT:")
        for a in review:
            print(f"  - {a['name']} -> {a['report_code']} ({a['mapping']['reason']})")
    elif args.final:
        print("\nFinal PDF (no DRAFT stamp).")
    else:
        print("\nNothing needs review. Re-run with --final for the version without the DRAFT stamp.")


if __name__ == "__main__":
    main()
