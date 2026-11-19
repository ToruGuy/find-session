"""Replace secret-looking strings before anything is cached or printed."""

from __future__ import annotations

import re

# --- Secrets -----------------------------------------------------------------
# Everything that enters a Session (conversation text and tool-call commands)
# passes through redact() first, so the cache, every output format and inspect
# only ever see the redacted text. Pattern-based: it catches the usual shapes
# (assignments, headers, URLs with passwords, known token formats, long random
# strings), it cannot promise to catch every secret.
REDACTED = "[REDACTED]"

_SECRET_NAME = (
    r"[A-Za-z0-9_.-]*?(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey"
    r"|access[_-]?key|private[_-]?key|auth|credentials?|[_-]key|has[łl][oa])s?"
)

# An AWS secret access key: exactly 40 base64 characters, standing alone.
_AWS_SECRET = r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"

# A value that is only a number (a limit, a port, a count) is not a secret.
_NUMBER_ONLY = r"['\"]?\d+['\"]?(?=[\s,;&|)}\]]|$)"

SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # PEM private keys (possibly cut off at the end of a command).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), REDACTED),
    # user:password@ in URLs.
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:)(?!\$)[^\s@/'\"]+@", re.I), rf"\1{REDACTED}@"),
    # Authorization headers and bearer tokens.
    (
        re.compile(
            r"(\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token|bot)\s+)?)"
            r"(?![$|]|(?:bearer|basic|token|bot)\b)[^\s'\",;]+",
            re.I,
        ),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"(\bbearer\s+)(?!\$)[A-Za-z0-9._~+/=-]{8,}", re.I), rf"\1{REDACTED}"),
    # Password options of common commands.
    (re.compile(r"(\bsshpass\s+-p\s*)(?:'[^']*'|\"[^\"]*\"|\S+)"), rf"\1{REDACTED}"),
    (re.compile(r"(\bmysql(?:dump|admin)?\b[^\n|;]*?\s-p)(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)"), rf"\1{REDACTED}"),
    (
        re.compile(
            r"(--(?:password|passwd|pass|token|secret|api-key|apikey|auth-token|access-key)(?:=|\s+))"
            r"(?!\$)(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)",
            re.I,
        ),
        rf"\1{REDACTED}",
    ),
    # "password": "…" in JSON.
    (re.compile(rf"(\"{_SECRET_NAME}\"\s*:\s*\")(?!\$|\d+\.\d)[^\"]{{4,}}(\")", re.I), rf"\1{REDACTED}\2"),
    # NAME=value (environment, CLI, query strings) and NAME: value of 8+ chars.
    # Values that are references ($VAR, $(command), paths, plain numbers) stay.
    (
        re.compile(
            rf"(?<![\w.-])({_SECRET_NAME})(=)(?!['\"]?[$\s~/]|{_NUMBER_ONLY})"
            rf"(?:'[^']*'|\"[^\"]*\"|[^\s&;|'\"`]+)",
            re.I,
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(
            rf"(?<![\w.-])({_SECRET_NAME})(\s*:\s*['\"]?)(?![-$\[~/]|{_NUMBER_ONLY}|process\.env|os\.environ)"
            rf"[^\s'\",]{{8,}}",
            re.I,
        ),
        rf"\1\2{REDACTED}",
    ),
    # A password written in prose: "password Abc123!", "hasło to Xy7…", and with up
    # to three words in between when a colon or an equals sign then introduces
    # the value ("hasło do serwera: Xy7…"). Words alone are not enough there:
    # "send the password reset link to user42@…" is an ordinary sentence. The
    # value must hold a digit or a special character, and a date or a number
    # alone is not one.
    (
        re.compile(
            r"(\b(?:has[łl][oa]|password|passphrase)"
            r"(?:\s*[:=]\s*|\s+(?:to\s+|is\s+|jest\s+)?|(?:\s+[^\s:=.,;!?()]{1,20}){1,3}\s*[:=]\s*))"
            r"(?=[^\s'\",]*[\d!@#$%^&*])(?![$\[]|\d[\d.:/-]*(?=[\s'\",]|$))[^\s'\",]{5,}",
            re.I,
        ),
        rf"\1{REDACTED}",
    ),
    # Well-known token formats.
    (
        re.compile(
            r"\b(?:sk-(?=[\w-]*[A-Z])(?=[\w-]*\d)[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{30,}"
            r"|glpat-[\w-]{20,}|xox[abprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}|AIza[\w-]{35}"
            r"|(?:sk|rk|pk)_(?:live|test)_\w{16,}|xkeysib-[\w-]{20,}|npm_[A-Za-z0-9]{36}|hf_[A-Za-z0-9]{30,}"
            r"|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}|\d{8,10}:AA[\w-]{30,})"
        ),
        REDACTED,
    ),
    (re.compile(r"(hooks\.slack\.com/services/|discord(?:app)?\.com/api/webhooks/)[\w/-]+"), rf"\1{REDACTED}"),
)

# A long run of token characters is treated as a random secret when one of its
# parts (between - _ + =) mixes upper case, lower case and digits often; ids
# made of words, dates, hex hashes and UUIDs stay searchable.
RANDOM_RUN = re.compile(r"(?<![A-Za-z0-9+=_-])[A-Za-z0-9+=_-]{24,}(?![A-Za-z0-9+=_-])")


def _looks_random(token: str) -> bool:
    for part in re.split(r"[-_+=]", token):
        if len(part) < 16 or part.islower() or part.isupper() or part.isalpha() or part.isdigit():
            continue
        if not re.search(r"\d", part):
            continue
        classes = ["d" if c.isdigit() else "u" if c.isupper() else "l" for c in part]
        if CAMEL_NAME.fullmatch(part):
            continue
        if sum(a != b for a, b in zip(classes, classes[1:])) >= len(part) * 0.3:
            return True
    return False


# Names built from words and numbers (InvoiceExport2026Final, parseV2Response)
# are identifiers, not random secrets.
CAMEL_NAME = re.compile(r"(?:[A-Z][a-z]{2,}|[a-z]{2,}|\d+|[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]\d+)+")


# AWS secret access keys: 40 characters of base64, which may hold slashes, so
# the pattern for random runs never sees one whole. Recognised by what stands
# before it: a name ("aws_secret_access_key = …", "secret access key: …",
# "secret …") or the key id ("AKIA… / …"). A key pasted with nothing around it
# is not recognised: a relative path can have exactly that shape. Each pattern
# is tried only on text that has its keyword, which most text has not.
AWS_NAMED_SECRET = re.compile(
    rf"(\b(?:aws[_ ]?)?secret(?:[_ ]?access)?(?:[_ ]?key)?\s*(?:[:=]|is|to)?\s*['\"]?){_AWS_SECRET}", re.I
)
AWS_SECRET_AFTER_ID = re.compile(rf"(\b(?:AKIA|ASIA)[0-9A-Z]{{16}}\b[^\n]{{0,80}}?){_AWS_SECRET}")


# What may stand between a command and the option that carries its password:
# the command's own arguments and nothing else. Text is stored on one line, so
# "docker login ghcr.io, then docker run -p 3000:3000" must not be read as one
# command: an argument is an option with or without a value, a host or a URL,
# never a plain word. `stop` is the option that is looked for itself.
def _arguments(stop: str) -> str:
    value = r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"-]\S*)"
    option = rf"--?[A-Za-z][\w-]*(?:=\S+|\s+{value})?"
    host = r"['\"]?(?:[a-z][a-z0-9+.-]*://\S+|localhost(?::\d+)?|[\w-]+(?:\.[\w-]+)+(?::\d+)?(?:/\S*)?)"
    return rf"(?:\s+(?!{stop})(?:{option}|{host}))*?"


# -p of a command that logs in: docker login -u me -p …, helm registry login
# -p …, mongosh -u me -p …. A reference ($VAR, "$VAR") stays, so does a number
# or a port (the -p of another command), and --password-stdin has no value.
# Tried only on text that has " -p".
LOGIN_PASSWORD = re.compile(
    r"(\b(?:(?:docker|podman|nerdctl|buildah|skopeo|oc|az|cf|helm(?:\s+registry)?)"
    + _arguments(r"login\b")
    + r"\s+login|mongo(?:sh|dump|restore|export|import)?)"
    + _arguments(r"-p(?:\s|=)")
    + r"\s+-p(?:\s+|=))"
    r"(?!['\"]?\$|\d+(?::\d+)*(?:\s|\Z))(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)"
)
# curl -u user:password. Tried only on text that has "curl".
_CURL_USER = r"(?:-[A-Za-z]*u|--user)\s"
CURL_PASSWORD = re.compile(
    r"(\bcurl" + _arguments(_CURL_USER) + rf"\s+{_CURL_USER}\s*['\"]?[^\s:'\"$]+:)(?!\$)[^\s'\"]+"
)

# A secret told in a sentence: "the password for staging is …", "the secret is
# …", "hasło do serwera to …", "token to …". The name, then at most a short
# "for/do <what>", then is / to / jest / brzmi, then the value. (With a colon
# or an equals sign it is one of the patterns above.) Whether the value is
# redacted is decided by its shape (`_told_secret`), because the same words
# introduce ordinary sentences: "the secret is in src/…", "the token is
# expired", "hasło to podstawa".
_TOLD_PASSWORD = r"has[łl][oa]|passwords?|passwd|passphrase"
_TOLD_OTHER = r"secret|sekret|token|api[ _-]?key|klucz\s+api"
_WORD = r"[^\s:=.,;!?()'\"`]{1,30}"
TOLD_SECRET = re.compile(
    rf"(\b(?:(?P<password>{_TOLD_PASSWORD})|{_TOLD_OTHER})"
    rf"(?:\s+(?:for|of|to|do|dla|na|od)\s+(?:the\s+)?{_WORD}(?:\s+{_WORD}){{0,2}})?"
    r"\s+(?:is|to|jest|brzmi)\s+['\"`]?)"
    r"(?P<value>[^\s'\"`,;]{5,})",
    re.I,
)

# The pattern is tried only on text that has one of the names (looked for in
# lower case), which most text has not.
_TOLD_NAMES = ("token", "secret", "passw", "has\u0142", "hasl", "sekret", "passphrase", "api key", "api-key", "api_key")
_TOLD_NAMES += ("apikey", "klucz")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+[.)!?]*\Z")
_NUMBER_OR_DATE = re.compile(r"\d[\d.:/-]*[.)!?]*\Z")
# What passwords are made of besides letters and digits. Characters that join
# words in names (base64-encoded, utf8mb4_unicode_ci, v1.2) are not among them.
_SPECIAL = re.compile(r"[!@#$%^&*+~]")
# Code, not a value somebody was told: call(), a[0], a|b, <placeholder>, {template}.
_CODE = re.compile(r"[()\[\]{}|\\<>]")
# A file name: config2.yml, utils2.py.
_FILE_NAME = re.compile(r"[\w.-]+\.[A-Za-z]{1,5}[.)!?]*\Z")


def _told_secret(match: re.Match) -> str:
    """The match with its value redacted when the value looks like a secret, else unchanged."""
    value = match.group("value")
    if (
        value.startswith("$")
        or "/" in value
        or _CODE.search(value)
        or _EMAIL.match(value)
        or _FILE_NAME.match(value)
        or _NUMBER_OR_DATE.match(value)
    ):
        return match.group(0)
    digit = any(char.isdigit() for char in value)
    special = bool(_SPECIAL.search(value))
    if match.group("password"):
        # After "password is" little else than the password has a digit or a special character.
        secret = digit or special
    else:
        # "token", "secret" and "API key" start many more sentences: the value
        # must mix three kinds of characters (aB3, a3#), or be one unbroken
        # run of letters with digits in two places (abc123def456).
        lower = any(char.islower() for char in value)
        upper = any(char.isupper() for char in value)
        kinds = lower + upper + digit + special
        unbroken = value.isalnum() and len(value) >= 10 and len(re.findall(r"\d+", value)) >= 2
        secret = len(value) >= 6 and (kinds >= 3 or unbroken)
    return match.group(1) + REDACTED if secret else match.group(0)


def redact(text: str) -> str:
    if not text:
        return text
    # First, while the key id is still there to recognise the secret by.
    if "AKIA" in text or "ASIA" in text:
        text = AWS_SECRET_AFTER_ID.sub(rf"\1{REDACTED}", text)
    if "ecret" in text or "ECRET" in text:
        text = AWS_NAMED_SECRET.sub(rf"\1{REDACTED}", text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if " -p" in text:
        text = LOGIN_PASSWORD.sub(rf"\1{REDACTED}", text)
    if "curl" in text:
        text = CURL_PASSWORD.sub(rf"\1{REDACTED}", text)
    lowered = text.lower()
    for name in _TOLD_NAMES:
        if name in lowered:
            text = TOLD_SECRET.sub(_told_secret, text)
            break
    return RANDOM_RUN.sub(lambda m: REDACTED if _looks_random(m.group(0)) else m.group(0), text)
