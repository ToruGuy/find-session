# Security

`find-session` reads your local agent history, keeps extracted text in a private index
(`~/.cache/find-session`, an SQLite file, owner-only) and prints to your terminal. It makes no
network requests.

Secret-looking strings are replaced with `[REDACTED]` before anything is cached or printed. This is
pattern matching, not a guarantee.

## Reporting a problem

If you find a secret shape that is not redacted, a way to make the tool write outside its cache, or
any other security problem, please report it privately through GitHub: **Security → Report a
vulnerability** on this repository. Describe the shape of the value, never the real value.
