#!/usr/bin/env python3
"""Secret scrubber for Jarvis private knowledge. stdlib only.

scrub(text) -> (clean_text, Counter{type: n}). Values are never logged or returned;
callers only ever see counts. Replacement marker: [REDACTED:<type>].

Run `python3 scrub.py --selftest` to check the patterns against synthetic samples.
"""
import math
import re
import sys
from collections import Counter

R = lambda p, f=0: re.compile(p, f)  # noqa: E731

# Order matters: specific provider formats first, generic heuristics last.
PATTERNS = [
    ("private-key", R(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("nvidia-api-key", R(r"\bnvapi-[A-Za-z0-9_\-]{20,}")),
    ("telegram-bot-token", R(r"(?<!\d)\d{8,10}:AA[A-Za-z0-9_\-]{30,}")),  # also inside /bot<token>/ URLs
    ("anthropic-api-key", R(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-api-key", R(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("openrouter-api-key", R(r"\bsk-or-v1-[A-Za-z0-9]{20,}")),
    ("github-token", R(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab-token", R(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("aws-access-key", R(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", R(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", R(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("stripe-key", R(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("huggingface-token", R(r"\bhf_[A-Za-z0-9]{30,}")),
    ("groq-api-key", R(r"\bgsk_[A-Za-z0-9]{30,}")),
    ("sendgrid-key", R(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("twilio-key", R(r"\b(?:AC|SK)[0-9a-f]{32}\b")),
    ("discord-token", R(r"\b[MN][A-Za-z0-9_\-]{23,25}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")),
    ("webhook-url", R(r"https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)/[A-Za-z0-9_/\-]{20,}")),
    # app tokens with a role/env prefix, e.g. ds_admin_<...>, sk_live_<...>, app_prod_<...>
    ("prefixed-token", R(r"\b[a-z]{1,8}_(?:admin|root|live|prod|secret|token|key|api|service|master)_"
                          r"(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Z])(?=[A-Za-z0-9_]*[a-z])[A-Za-z0-9_]{8,}")),
    ("jwt", R(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url-credentials", R(r"(?<=://)[^\s/:@'\"]+:[^\s/@'\"]+(?=@)")),
    ("bearer-token", R(r"(?i)(?<=\bbearer )[A-Za-z0-9_\-\.=+/]{16,}")),
    # key = value / key: value assignments (env files, configs, prose like "password is X")
    ("credential-assignment", R(
        r"(?i)(?<![A-Za-z])(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|bot[_-]?token|"
        r"client[_-]?secret|private[_-]?key|passwd|password|passphrase|pwd|"
        r"(?:admin|api|secret|access|refresh|session|service|reseed|webhook|bot)[ _-]token)"
        r"\s*(?:=|:|\bis\b|\bwas\b)\s*(?P<q>[\"'`]?)(?P<v>[^\s\"'`,;]{4,})")),
    ("verification-code", R(
        r"(?i)\b(?:(?:verification|security|confirmation|2fa|mfa|one[- ]time|login|sign[- ]in|auth)\s*(?:code|pin|passcode)|otp|verification)\b"
        r"[^\n\d]{0,20}?(?P<v>\b(?!(?:19|20)\d{2}\b)\d{4,8}\b)")),
    ("recovery-code", R(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{5}-(?=[0-9a-f]*[a-f])[0-9a-f]{5}\b")),
    ("bank-account", R(
        r"(?i)(?:account|acct|routing|transit|institution|aba|sort code|bsb)\s*(?:number|no\.?|#)?\s*[:#]?\s*(?P<v>\d[\d \-]{4,20}\d)")),
    ("ssn", R(r"\b\d{3}-\d{2}-\d{4}\b")),
]
# Card-shaped only: 4-4-4-4(-3) / Amex 4-6-5 groups, or 15-16 contiguous digits with a card prefix.
CARD = R(r"(?<![\w.\-/:])(?:\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{3})?|3[47]\d{2}([ -])\d{6}\2\d{5}|"
         r"(?:4\d{15}|5[1-5]\d{14}|2[2-7]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12}))(?![\w.\-/:])")
IBAN = R(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
SENSITIVE_PARAM = R(r"(?i)(key|token|secret|sig|signature|auth|pass|pwd|session|code)")
TOKENISH = R(r"(?<![A-Za-z0-9_\-/.])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")
UUID = R(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
# Placeholders/examples that look like secrets but aren't
BENIGN = R(r"(?i)^(?:x+|\*+|your[_-]?\w*|<[^>]*>|\$\{?\w+\}?|example\w*|placeholder|changeme|redacted.*|none|null|true|false|\.\.\.|os\.environ.*|process\.env.*|getenv.*|env\[.*)$")


def _luhn(digits):
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        s, alt = s + d, not alt
    return s % 10 == 0


def _entropy(s):
    c = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in c.values())


def scrub(text):
    counts = Counter()

    def sub_group(kind, rx):
        nonlocal text

        def rep(m):
            if "v" in rx.groupindex:
                v = m.group("v")
                quoted = bool(m.groupdict().get("q"))
                if (BENIGN.match(v) or v.startswith(("[REDACTED", "<", "{", "$"))
                        or (kind == "credential-assignment" and not quoted
                            and (not re.search(r"[\d!@#$%^&*_+=.\-]", v.rstrip(".,;:!?)"))
                                 or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+[.,;:)]*", v)))):
                    return m.group(0)
                counts[kind] += 1
                s, e = m.span("v")
                return m.group(0)[: s - m.start()] + f"[REDACTED:{kind}]" + m.group(0)[e - m.start():]
            if m.group(0).startswith("[REDACTED:"):  # our own marker (re-scrub): don't count it twice
                return m.group(0)
            counts[kind] += 1
            return f"[REDACTED:{kind}]"
        text = rx.sub(rep, text)

    for kind, rx in PATTERNS:
        sub_group(kind, rx)

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1:
            counts["card-number"] += 1
            return "[REDACTED:card-number]"
        return m.group(0)
    text = CARD.sub(card, text)

    def iban(m):
        raw = m.group(0).replace(" ", "")
        if 15 <= len(raw) <= 34:
            num = "".join(str(int(ch, 36)) for ch in raw[4:] + raw[:4])
            if int(num) % 97 == 1:
                counts["iban"] += 1
                return "[REDACTED:iban]"
        return m.group(0)
    text = IBAN.sub(iban, text)

    def tok(m):
        t = m.group(0)
        seg = re.split(r"\s", m.string[max(0, m.start() - 3000):m.start()])[-1]  # back to last whitespace
        if ("://" in seg or seg.startswith("www.")) and re.search(r"(?:[?&;][\w.\-]+=[^&\s]*|%[0-9A-Fa-f]{2})$", seg):
            q = re.findall(r"[?&;]([\w.\-]+)=", seg)  # token is a query value or URL-encoded id, not a path segment
            if not (q and SENSITIVE_PARAM.search(q[-1])):  # tracking ids, URNs, citations...
                return t
        before = m.string[max(0, m.start() - 40):m.start()]
        if (re.search(r"(?i)(?:id|idorurl|integrity|sha\d*|hash|etag|digest|checksum|project(?: id)?:?\**)[\"'`]?\s*[:=]?\s*[\"'`]?$", before)
                or re.search(r"base64,$|Buffer\.from\(['\"]$", before) or re.match(r"(?:prj|dpl|team|icfg|oac)_", t)):
            return t  # identifiers (Vercel project/deployment ids, npm integrity, base64 blobs), not secrets
        if (UUID.match(t) or re.fullmatch(r"[0-9a-f]+", t)  # uuids, git shas, hashes
                or not (re.search(r"[A-Z]", t) and re.search(r"[a-z]", t) and re.search(r"\d", t))
                or _entropy(t) < 4.0 or "_" in t.strip("_") and t.count("_") > 3):  # snake_case identifiers
            return t
        counts["high-entropy-token"] += 1
        return "[REDACTED:high-entropy-token]"
    text = TOKENISH.sub(tok, text)
    return text, counts


# Synthetic, obviously-fake samples only (split literals so repo secret scanners don't flag them).
FAKE_SECRETS = {
    "nvidia": "key nvapi-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z" + "Q7",
    "telegram": "token 7012345678:AA" + "Fq3kZ9xLmP2vB8nR4tY6uW1eC5gH7jK0dS",
    "openai": "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6",
    "card": "card 4111 1111 1111 1111 exp 12/29",
    "card2": "pay with 4242424242424242 today",
    "iban": "wire to DE89 3704 0044 0532 0130 00",
    "tgurl": "curl https://api.telegram.org/bot8123456789:AA" + "GjFakeFakeFakeFake0123456789abcdEF/getUpdates",
    "pathtok": "https://hooks.slack.com/" + "services/T0FAKE/B0FAKE/" + "Xq7Lm2Pz9Rt4Vw8Kb3Nc6Jd1Fh5Gs0Ay",
    "admintok": "Admin token `zz_admin_2026_Qw3Er5Ty7Ui9` is compromised, rotate it",
    "admintok2": "the admin token: " + "Hj7Kl9Zx" + "2Cv4Bn6M",
    "urlkey": "https://api.x.com/v1?api_" + "key=" + "Zx9QpL2mV7nR4tY8" + "wK3bH6jD1fS5gA0c",
    "pw": "my password is Hunter2!x",
    "env": "OPENAI_API_KEY=sk-live-should-be-caught and DB_PASSWORD: s3cr3tPass",
    "otp": "your verification code is 482913",
    "otp4": "sign-in code: 7391",
    "ghrec": "recovery codes: 3f9a2-b71c4 8e0d1-c55af",
    "acct": "account number: 1234-5678-9012",
    "url": "postgres://" + "admin:" + "SuperSecret99" + "@db.example.com:5432/app",
}
BENIGN_SAMPLES = {
    "sha": "commit 5e5e0ee and 0327766 merged; uuid 8cfd01fa-56e6-43f3-9520-64e1626a1578",
    "prose": "The password field must be at least 8 characters. Set api_key = <your key>. PR #210 shipped, 458 tests passed.",
    "phone": "call 555 010 0199 tomorrow; price $99/mo + $50 setup; GPA 3.5",
    "ident": "use process_vault_notes_before_upload and DEALERSCOPE_DEMO_ACCOUNT_ID",
    "codeids": "approval_key_legacy, test_admin_account_deletion, ds_admin_secret_change_me, nvidia_key_length",
    "table": "0 500 500 500 500 500 500 500 500 260 500 500 260 4111 500 389 574",
    "msfiles": "1781224785647_image.png, 1781224796879_image.png",
    "trackurl": "https://x.com/?a=1&gbraid=0AAAAAqF3mXyZbQ9vT2kLpR8nWc4eJdHgS1u&gclid=CjwKCAiAzZNBhAEEiwAMtqKyWq7Lp3Rf9XzT",
    "ids": 'game_id "4485b8f1d2c3e4a5-a4e9-0dcd89cf0b5a" and order 4532015112830366X',
    "design": "UI/UX patterns defining SaaS design in 2026, sign in 2025 flows",
    "pwprose": "Sudo without password was disabled. Use form.old_password: payload.old_password, here.",
    "linkedin": "https://www.linkedin.com/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAAAhX0QBIlqZ7xK9mPw3RtY&recipient=x",
    "hsts": 'headers["Strict-Transport-Security"] = "max-age=31536000"; AuthModal at: 24311',
    "ranges": "Average recovery 12000-18000/month; this pin is ==1.7.4 + bcrypt==4.0.1",
    "vercel": '{"deploymentId": "dpl_7Hq2LmZx9KpR4tW8vB3nY6cF1jD5", "projectId": "prj_Ab3dEf6hIj9kLm2nOp5qRs8tUv1w"}',
    "caps": "ID GB82WEST12345698765431 is a sample IBAN-looking string? no: AB12CDEFGHIJKLMNOP",
}


def _selftest():
    fake, benign = FAKE_SECRETS, BENIGN_SAMPLES
    ok = True
    for k, v in fake.items():
        out, c = scrub(v)
        hit = sum(c.values()) > 0 and "REDACTED" in out
        ok &= hit
        print(f"  secret {k:9} -> {'REDACTED' if hit else 'MISSED'} {dict(c)}")
    for k, v in benign.items():
        out, c = scrub(v)
        clean = sum(c.values()) == 0
        ok &= clean
        print(f"  benign {k:9} -> {'untouched' if clean else 'FALSE POSITIVE ' + str(dict(c)) + ' ' + out}")
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    data = sys.stdin.read()
    out, c = scrub(data)
    sys.stdout.write(out)
    print(dict(c), file=sys.stderr)
