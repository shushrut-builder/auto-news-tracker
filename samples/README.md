# Sample report

`sample-email.html` and `sample-email.txt` were built from live feeds on 15 November 2026 with `python signals.py --preview`, using the watchlist in `config.yaml` and keyword-rules tagging (no AI key set).

Keyword rules score an item 0.75 only when the signal and a named company are both in the headline, and 0.5 otherwise, so weaker matches fall below the 0.7 confidence gate into Industry development. The rules also can't judge context the way the AI does: an item about fake recall notices is tagged as a recall, for example. With `ANTHROPIC_API_KEY` set, tagging is done by Claude Haiku 5.5 and these errors are filtered out.

Sunday is a quiet news day, so this email is short. Regenerate at any time:

```bash
python signals.py --preview
```
