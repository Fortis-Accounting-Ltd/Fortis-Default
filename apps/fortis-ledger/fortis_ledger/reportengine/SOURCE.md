# Report engine (the app's own copy)

`scripts/` (chart.py, fr_engine.py, render_pack.py, build_pack.py) and `assets/` (the firm's four
charts of accounts, report-map.json, boilerplate.json, fonts) were copied unchanged from the
**financial-report-builder** skill at commit `23c4b38` on 16 October 2026.

The app keeps its own copy so that work on the app never changes the skill, and changes to the skill
never change the app. The two copies are separate from here on: something changed in one is not in
the other unless it is copied across deliberately.

The fonts are Source Sans 3 under the SIL Open Font License (see `assets/fonts/OFL-NOTICE.txt`).
