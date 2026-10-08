# Bundled Codeforces submit

Adapted from [ehnryx/cf_submit](https://github.com/ehnryx/cf_submit). The pinned upstream revision, original file hashes and compatibility changes are recorded in `UPSTREAM.json` and `compatibility.patch`. The upstream GPL-3.0 license is preserved in `LICENSE`.

After `vp cf ID` or `vp gym ID` configures the contest directory, run:

```bash
submit A.cpp
```

The installed entry reads `contest.json` in the current directory and uses the existing Codeforces login session. Its compiler selection uses the options offered by the submission form. The upstream local problem-testing helper is omitted from this distribution.
