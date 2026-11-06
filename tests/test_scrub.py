import pytest

import scrub as S


@pytest.mark.parametrize("name", sorted(S.FAKE_SECRETS))
def test_fake_secret_is_redacted(name):
    out, counts = S.scrub(S.FAKE_SECRETS[name])
    assert sum(counts.values()) > 0
    assert "[REDACTED:" in out


@pytest.mark.parametrize("name", sorted(S.BENIGN_SAMPLES))
def test_benign_text_is_untouched(name):
    text = S.BENIGN_SAMPLES[name]
    out, counts = S.scrub(text)
    assert out == text
    assert sum(counts.values()) == 0


def test_returns_counts_never_values():
    secret = "sk-proj-" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2Jh1Gf0Ed"
    out, counts = S.scrub(f"key: {secret}")
    assert secret not in out
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in counts.items())
    assert secret not in repr(counts)


def test_scrub_is_idempotent():
    once, _ = S.scrub(" ".join(S.FAKE_SECRETS.values()))
    twice, counts = S.scrub(once)
    assert twice == once
    assert sum(counts.values()) == 0


def test_card_requires_valid_luhn():
    assert S.scrub("card 4111 1111 1111 1111")[1]["card-number"] == 1
    assert S.scrub("card 4111 1111 1111 1112")[1]["card-number"] == 0


def test_iban_requires_valid_checksum():
    assert S.scrub("DE89 3704 0044 0532 0130 00")[1]["iban"] == 1
    assert S.scrub("DE00 3704 0044 0532 0130 00")[1]["iban"] == 0


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----"
    out, counts = S.scrub(f"before\n{pem}\nafter")
    assert counts["private-key"] == 1
    assert "MIIfake" not in out and "before" in out and "after" in out


def test_url_credentials_keep_host():
    out, _ = S.scrub("postgres://admin:" + "Hunter2Pass99" + "@db.local:5432/app")
    assert "db.local:5432/app" in out and "Hunter2Pass99" not in out


def test_placeholders_are_not_secrets():
    for text in ("api_key = <your key>", "password: changeme", "token=${TOKEN}", "secret = os.environ['X']"):
        assert S.scrub(text)[0] == text


def test_selftest_passes():
    assert S._selftest()
