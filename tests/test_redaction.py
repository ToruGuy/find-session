"""Secret-looking strings never reach the cache or any output."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    FAKE,
    KEPT,
    SECRET_TEXT,
    claude_rows,
    codex_meta,
    codex_user,
    fake,
    omp_header,
    omp_msg,
    run_raw,
    write_jsonl,
    write_omp,
)

from find_session import (
    model,
    redact,
    tools,
)


class RedactionTests(unittest.TestCase):
    def test_secret_shapes_are_replaced(self):
        cleaned = redact.redact(SECRET_TEXT)
        for name, value in FAKE.items():
            self.assertNotIn(value, cleaned, name)
        self.assertIn("Authorization: Bearer [REDACTED]", cleaned)
        self.assertIn("postgres://app:[REDACTED]@db.internal/x", cleaned)
        self.assertIn("DB_PASSWORD=[REDACTED]", cleaned)
        self.assertIn("GITHUB_TOKEN=[REDACTED]", cleaned)

    def test_references_ids_and_paths_are_kept(self):
        for text in (
            *KEPT,
            "Authorization: Bearer $API_TOKEN",
            'TOKEN="$(security find-generic-password -w)"',
            "postgres://app:${DB_PASS}@db/x",
            "max_tokens=4000",
            '"version": "1.2.3"',
            "sk-hello-world-skill-name",
            "AcmeAuditReport2026Final",
        ):
            self.assertEqual(redact.redact(text), text)

    def test_numbers_only_values_stay_but_number_prefixed_secrets_go(self):
        self.assertEqual(redact.redact("max_tokens=4000 port: 5432"), "max_tokens=4000 port: 5432")
        cleaned = redact.redact(fake("PASSWORD=", "2024-", "abc!x") + " " + fake("hasło: ", "tajne", "12345"))
        self.assertEqual(cleaned, "PASSWORD=[REDACTED] hasło: [REDACTED]")
        self.assertEqual(redact.redact(fake("login admin, hasło ", "Abc123xyz!")), "login admin, hasło [REDACTED]")
        self.assertEqual(redact.redact("hasło do panelu jest w menedżerze"), "hasło do panelu jest w menedżerze")

    def test_aws_secret_access_keys_are_removed_in_every_form(self):
        # 40 base64 characters with slashes: no pattern for random runs sees it whole.
        secret = fake("wJalrXUtnFEMI", "/K7MDENG/", "bPxRfiCY", "EXAMPLEKEY")
        self.assertEqual(len(secret), 40)
        for text in (
            f"aws key {FAKE['aws']} secret {secret}",
            f"{FAKE['aws']} / {secret}",
            f"key id {FAKE['aws']}\nsecret access key {secret}",
            f"aws_secret_access_key = {secret}",
            f"AWS_SECRET_ACCESS_KEY={secret}",
            f'"SecretAccessKey": "{secret}"',
            f"the secret access key is {secret}, keep it safe",
            f"SECRET: '{secret}'",
        ):
            cleaned = redact.redact(text)
            self.assertNotIn(secret[:12], cleaned, text)
            self.assertNotIn(secret[-12:], cleaned, text)
            self.assertNotIn(FAKE["aws"], cleaned, text)
            self.assertIn("[REDACTED]", cleaned)

    def test_paths_and_hashes_shaped_like_a_key_are_kept(self):
        for text in (
            # 40 characters of letters, digits and slashes each, like a key.
            "see Users/alex/Projects/Shop2026/src/AppMain1 for it",
            "skills/findSession/scripts/SessionsParser2 is here",
            "the secret is in src/components/InvoiceExportDialog2Componentx/index.ts",
            "commit 4f1c9e0b7a2d6e8f3b5c1a9d7e2f4b6c8a0d3e1f",
            "the secret santa list is in notes/santa.md",
            "secret: none",
        ):
            self.assertEqual(redact.redact(text), text)

    def test_password_in_prose_with_words_before_the_value(self):
        value = fake("Kot", "123", "!@#")
        for text, kept in (
            (f"hasło do serwera: {value}", "hasło do serwera: "),
            (f"hasło do bazy produkcyjnej: {value}", "hasło do bazy produkcyjnej: "),
            (f"password for the panel: {value}", "password for the panel: "),
            (f"haslo roota = {value}", "haslo roota = "),
            (f"the password is {value}", "the password is "),
        ):
            self.assertEqual(redact.redact(text), kept + "[REDACTED]")

    def test_sentences_about_passwords_are_kept(self):
        for text in (
            "hasło do serwera zmienimy jutro, bo wygasa 2026-11-19",
            "hasło do serwera: zmienimy jutro",
            "hasło do bazy przenieś to config2.yml",
            "the password for staging: ask Anna",
            "password rotation date: 2026-11-19 and then",
            "password policy: minimum 12 characters",
            "We reset the password. Then step2 of the migration ran",
            "password reset flow is described in docs/auth2.md",
            "send the password reset link to user42@example.com",
            "set the password and push to origin/feature-123",
            "move password validation to utils2.py",
            "the password field is input2 in the form",
        ):
            self.assertEqual(redact.redact(text), text)

    def test_a_secret_told_in_a_sentence_is_removed(self):
        password = fake("Stag", "ing", "9x", "$")
        mixed = fake("Xk9", "mP2", "qLw")
        plain = fake("abc", "123", "def", "456")
        for text, kept in (
            (f"the password for staging is {password}", "the password for staging is "),
            (f"The password for the admin account is {mixed}", "The password for the admin account is "),
            (f"password of the vault is '{password}'", "password of the vault is '"),
            (f"change the password to {password}", "change the password to "),
            (f"hasło to {password}", "hasło to "),
            (f"hasło do serwera to {password}", "hasło do serwera to "),
            (f"hasło do bazy danych jest {mixed}", "hasło do bazy danych jest "),
            (f"Hasło brzmi {password}", "Hasło brzmi "),
            (f"haslo dla klienta to {mixed}", "haslo dla klienta to "),
            (f"the secret is {mixed}", "the secret is "),
            (f"the secret for prod is {password}", "the secret for prod is "),
            (f"sekret to {mixed}", "sekret to "),
            (f"the token is {mixed}", "the token is "),
            (f"token to {mixed}", "token to "),
            (f"the token for staging is {plain}", "the token for staging is "),
            (f"The API key is {mixed}", "The API key is "),
            (f"klucz API to {mixed}", "klucz API to "),
        ):
            self.assertEqual(redact.redact(text), kept + "[REDACTED]" + ("'" if kept.endswith("'") else ""), text)
        # The sentence goes on after the value.
        cleaned = redact.redact(f"the password for staging is {password} and the user is deploy")
        self.assertEqual(cleaned, "the password for staging is [REDACTED] and the user is deploy")

    def test_ordinary_sentences_with_the_same_words_are_kept(self):
        for text in (
            # English.
            "send the password reset link to user42@example.com",
            "the password for staging is the same as before",
            "the password for staging is user42@example.com",
            "the password is wrong",
            "the password is stored in 1password",
            "the password for the demo is in docs/demo2.md",
            "password of the day is 2026-11-19",
            "the secret is in src/components/Login2.tsx",
            "the secret is to start early",
            "the secret is $SECRET_VALUE",
            "the secret is sha256sum of the file",
            "the secret for staging is rotated every 90 days",
            "the token is expired",
            "the token is base64-encoded",
            "the token is utf8mb4_unicode_ci",
            "the token is ed25519",
            "the token is https://example.com/a1B2",
            "the token for the API is v1.2.3",
            "the token limit is 200000",
            "the token is generated by getToken(user1)",
            "the token is <TOKEN_FROM_VAULT1>",
            "the api key is stored in 1password",
            "the API key is config2.yml material",
            "tokenizer is GPT4o-mini",
            "the monkey is B4nana#1",
            # Polish.
            "hasło to podstawa",
            "hasło jest błędne",
            "hasło do serwera to tajemnica",
            "hasło do bazy jest w 1password",
            "hasło do bazy przenieś to config2.yml",
            "hasło do panelu jest w menedżerze",
            "hasło do poczty to nie wszystko, potrzebny jest też kod",
            "sekret to nie wszystko",
            "sekret do produkcji jest w sejfie",
            "token to 1024 znaki",
            "token to JWT z 3 częściami",
            "token to nie jest hasło",
            "klucz do sukcesu to 5minutowe przerwy",
            "klucz API jest w pliku ustawienia2.json",
        ):
            self.assertEqual(redact.redact(text), text)

    def test_password_of_a_login_command_is_removed(self):
        value = fake("Zx9", "q#", "11a")
        # In parts, like the values: a scanner for secrets must not take this test for one.
        client = fake("cu", "rl")
        for text, cleaned in (
            (
                f"docker login -u me -p {value} registry.example.com",
                "docker login -u me -p [REDACTED] registry.example.com",
            ),
            (f"podman login -p={value} quay.example.com", "podman login -p=[REDACTED] quay.example.com"),
            (
                f"helm registry login -u me -p '{value}' r.example.com",
                "helm registry login -u me -p [REDACTED] r.example.com",
            ),
            (
                f"echo x && docker login reg.example.com -p {value}",
                "echo x && docker login reg.example.com -p [REDACTED]",
            ),
            (f"mongosh -u app -p {value} db.example.com", "mongosh -u app -p [REDACTED] db.example.com"),
            (
                f'mongosh "mongodb://db.example.com/x" -u app -p {value}',
                'mongosh "mongodb://db.example.com/x" -u app -p [REDACTED]',
            ),
            (f"docker --config /tmp/c2 login -u me -p {value}", "docker --config /tmp/c2 login -u me -p [REDACTED]"),
            (
                f"docker login registry.example.com -u me -p {value}",
                "docker login registry.example.com -u me -p [REDACTED]",
            ),
            (
                f'{client} -H "Accept: application/json" -X POST -u admin:{value} https://x.example.com',
                f'{client} -H "Accept: application/json" -X POST -u admin:[REDACTED] https://x.example.com',
            ),
            (f"{client} -u admin:{value} https://x.example.com", f"{client} -u admin:[REDACTED] https://x.example.com"),
            (
                f"{client} -sSu admin:{value} https://x.example.com",
                f"{client} -sSu admin:[REDACTED] https://x.example.com",
            ),
            (
                f"{client} --user admin:{value} https://x.example.com",
                f"{client} --user admin:[REDACTED] https://x.example.com",
            ),
        ):
            self.assertEqual(redact.redact(text), cleaned)
        for text in (
            "docker run -p 8080:80 nginx",
            # Text is stored on one line: the -p or -u of a later command is not this command's.
            "docker run -d --name mongo -p 27017:27017 mongo:7",
            "we use mongo here. Run mkdir -p data/cache2 first",
            "docker login ghcr.io then docker run -p 3000:3000 app",
            "docker login -u me then cp -p file1 file2",
            "docker login registry.example.com && mkdir -p out/v2",
            "helm registry login r.example.com then helm upgrade -p plugin2",
            "cf login -a api.example.com then cf push -p build/app2.zip",
            "az login then az storage blob upload -p premium2 file",
            "mongodump --uri mongodb://localhost/db2 then tar -p archive2.tgz",
            "docker compose run web login -p pw12ab",
            f"{client} -fsSL https://get.example.com -o get.sh docker run -u 1000:1000 img",
            f"{client} https://example.com/a then ssh -u deploy:staff2 host",
            "docker login --password-stdin registry.example.com",
            "docker login -u me -p $REGISTRY_PASSWORD registry.example.com",
            'docker login -u me -p "$REGISTRY_PASSWORD" registry.example.com',
            "docker logs -p web1",
            "ssh -p 2222 deploy@host1",
            "mkdir -p build/out2 && docker login registry.example.com",
            f"{client} -u $USER:$PASS https://x.example.com",
            "curl -u admin https://x.example.com",
            "curl -o out2.json https://user1.example.com/a:b",
        ):
            self.assertEqual(redact.redact(text), text)

    def test_secret_across_the_command_length_limit_is_still_removed(self):
        session = model.Session("Claude", "x", "~", Path("x"), 0.0)
        token = FAKE["github"]
        # The token starts a few characters before the cut.
        command = "x" * (tools.MAX_TOOL_TEXT - 10) + " " + token
        session.add_tool(tools.tool_text("Bash", {"command": command}))
        [(_, stored)] = session.tools
        self.assertNotIn(token[:8], stored)
        self.assertEqual(len(stored), tools.MAX_TOOL_TEXT)
        self.assertEqual(session.tool_cuts, {0: len(redact.redact("$ " + command)) - tools.MAX_TOOL_TEXT})

    def test_no_secret_reaches_cache_output_or_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "history"
            write_jsonl(home / ".claude/projects/-Users-alex-projects-shop/sec-1.jsonl", claude_rows())
            write_jsonl(
                home / ".codex/sessions/2026/09/22/rollout-codex-sec.jsonl",
                [
                    codex_meta("codex-sec"),
                    codex_user(f"Codex keys {SECRET_TEXT}"),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "s1",
                            "arguments": json.dumps({"cmd": f"deploy {SECRET_TEXT}"}),
                        },
                    },
                ],
            )
            write_omp(
                home / ".omp/agent/sessions/-Users-alex-x/2026-09-22T10-00-00-000Z_omp-sec.jsonl",
                [
                    omp_header("omp-sec"),
                    omp_msg("u1", None, "user", f"omp keys {SECRET_TEXT}"),
                    omp_msg(
                        "a1",
                        "u1",
                        "assistant",
                        [
                            {
                                "type": "toolCall",
                                "id": "t1",
                                "name": "bash",
                                "arguments": {"command": f"run {SECRET_TEXT}"},
                            },
                        ],
                    ),
                ],
            )
            cache_dir = Path(tmp) / "cache"
            outputs = []
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(cache_dir)}):
                for argv in (
                    ("search", "keys"),
                    ("search", "keys", "--full"),
                    ("search", "keys", "--json", "--children"),
                    ("search", "deploy"),
                    ("inspect", "claude:sec-1", "--tools"),
                    ("inspect", "codex:codex-sec", "--tools", "--json"),
                    ("inspect", "omp:omp-sec", "--tools"),
                ):
                    code, output = run_raw("--home", str(home), *argv)
                    self.assertEqual(code, 0, argv)
                    outputs.append(output)
            joined = "\n".join(outputs)
            self.assertIn("[REDACTED]", joined)
            # Everything the cache wrote: the index and whatever SQLite keeps next to it.
            stored = b"".join(path.read_bytes() for path in sorted(cache_dir.iterdir()))
            self.assertIn(b"[redacted]", stored)
            for name, value in FAKE.items():
                self.assertNotIn(value, joined, name)
                self.assertNotIn(value.encode(), stored, name)
                # The search text is stored in lower case.
                self.assertNotIn(value.lower().encode(), stored, name)
            # Searching for a secret value itself finds nothing.
            code, output = run_raw("--home", str(home), "--no-cache", "search", FAKE["github"])
            self.assertIn("no matches", output)


if __name__ == "__main__":
    unittest.main()
