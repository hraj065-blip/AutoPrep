# Five-minute demo

1. Start PrepPilot and upload `data/sample/messy_customers.csv`.
2. Inspect the observed profile: leading/trailing whitespace, a repeated business record, possible sentinel, mixed date strings, and type suggestions.
3. Show the operation ledger. Whitespace trimming is proposed based on measured examples and waits for review. Duplicate rows create a review item with a separate row-deletion consent checkbox; no row is deleted automatically.
4. Reject or skip a proposed change and note the source remains unchanged. Alternatively, approve a trim or enable the row-deletion checkbox and approve duplicate removal.
5. Inspect the before/after JSON report and query the lineage endpoint for a changed cell using a source row ID.
6. Finalize, download CSV/XLSX and the audit ZIP, and invoke replay to compare the replay hash with the candidate hash.
7. Run the 100-case scripted benchmark and inspect the saved Evaluation Lab result.

The current graph demonstrates bounded routing and human review. Its default evidence planner proposes whitespace trimming and duplicate review; changes wait for approval. It does not currently demo an adaptive recovery after a post-operation validation failure or a hosted LLM. Do not claim those demonstrations have been implemented.
