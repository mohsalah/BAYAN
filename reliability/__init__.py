"""Reliability bench for BAYAN: a field-by-field gold set of real papers, test agents that use
the app the way researchers do, scorecards, and models that learn where the app fails.

Commands (run from the repository root):
    python -m reliability taxonomy        # OpenAlex fields and subfields -> data/taxonomy.json
    python -m reliability gold            # 50 verified papers per subfield -> data/gold/
    python -m reliability run             # test agents drive a live BAYAN -> results/<run_id>/
    python -m reliability train           # models learn from the results -> results/<run_id>/ml/
    python -m reliability report          # REPORT.md from the latest run

Nothing here invents a paper: every gold record comes from OpenAlex and its DOI is checked
against Crossref (or the DOI registry for non-Crossref DOIs); a record that fails is dropped
and counted.
"""
