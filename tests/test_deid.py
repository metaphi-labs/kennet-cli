import collections
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import codeval, deid, deid_model

FIXTURES = Path(__file__).parent / "deid"
LABEL = re.compile(r"\{\{(\w+):(.*?)\}\}")
KINDS = (
    "author|domain|org|host|hostname|user|home|repo|remote|tmp|url|email|ipv4|ipv6|mac|name|phone|address"
    "|redacted:[a-z_]+"
)
PLACEHOLDER = re.compile(rf"\[(?:{KINDS})(?::\d+)?\]")
SELLER = {
    "name": "falcon_core",
    "path": "/Users/sseller/work/falcon_core",
    "authors": {"sam.seller@acme-corp.com": 1, "priya@acme-corp.com": 2, "daniel.okafor@acme-corp.com": 3},
    "domains": {"acme-corp.com": 1},
    "people": [["Priya Raman", "priya@acme-corp.com"], ["Sam Seller", "sam.seller@acme-corp.com"]],
    "remotes": ["git@github.com:acme-corp/falcon_core.git"],
}


@pytest.fixture
def seller(monkeypatch):
    """This machine, as the seller's: their home, user name and machine name."""
    import socket

    monkeypatch.setenv("HOME", "/Users/sseller")
    for var in ("USER", "LOGNAME"):
        monkeypatch.setenv(var, "sseller")
    monkeypatch.setattr(socket, "gethostname", lambda: "sams-mbp")
    monkeypatch.setattr(socket, "getfqdn", lambda: "sams-mbp.local")


def labelled(name: str) -> tuple[str, list[list[tuple[int, int, str]]]]:
    """A fixture log without its labels, and per line the (start, end, kind) of each labelled span."""
    lines, gold = [], []
    for raw in (FIXTURES / name).read_text().split("\n"):
        out, spans, at = [], [], 0
        for m in LABEL.finditer(raw):
            out.append(raw[at : m.start()])
            start = sum(map(len, out))
            out.append(m[2])
            spans.append((start, start + len(m[2]), m[1]))
            at = m.end()
        out.append(raw[at:])
        lines.append("".join(out))
        gold.append(spans)
    return "\n".join(lines), gold


def replaced(raw: str, said: str) -> list[tuple[int, int]] | None:
    """The spans of `raw` that became placeholders in `said`, or None when the two do not line up."""
    parts, at = [], 0
    for m in re.finditer(rf"(?:{PLACEHOLDER.pattern})+", said):
        parts += [re.escape(said[at : m.start()]), "(.+?)"]
        at = m.end()
    parts.append(re.escape(said[at:]))
    m = re.fullmatch("".join(parts), raw, re.DOTALL)
    return None if m is None else [m.span(i) for i in range(1, (m.lastindex or 0) + 1)]


def scored(scrub: deid.Scrubber) -> tuple[dict[str, list[int]], list[int]]:
    """Per kind, [caught, labelled]; and [true, all] replacements, over every fixture."""
    recall: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    precision = [0, 0]
    for f in sorted(FIXTURES.glob("*.log")):
        raw, gold = labelled(f.name)
        said = scrub.text(raw)
        for line, out, spans in zip(raw.split("\n"), said.split("\n"), gold, strict=True):
            got = replaced(line, out)
            assert got is not None, (f.name, line, out)
            for start, end, kind in spans:
                recall[kind][1] += 1
                recall[kind][0] += all(any(a <= i < b for a, b in got) for i in range(start, end) if line[i].isalnum())
            for a, b in got:
                precision[1] += 1
                precision[0] += any(a < end and start < b for start, end, _k in spans)
    return dict(recall), precision


def test_fixture_recall_and_precision_without_the_model(seller):
    (k,) = deid.known([SELLER])
    recall, (true, made) = scored(deid.Scrubber(k))
    missed = {kind: f"{c}/{n}" for kind, (c, n) in recall.items() if c < n}
    assert set(missed) <= {"name"}, missed
    assert true / made >= 0.97, (true, made)


def test_known_identifiers_become_the_profiles_author_ids(seller):
    (k,) = deid.known([SELLER])
    scrub = deid.Scrubber(k)
    said = scrub.text(
        "by Sam Seller <sam.seller@acme-corp.com>, Priya Raman; sam.seller pushed; Daniel Okafor reviewed"
    )
    assert said == "by [author:1] <[author:1]>, [author:2]; [author:1] pushed; Daniel Okafor reviewed"
    assert (
        scrub.text("/Users/sseller/work/falcon_core/a.py and /Users/sseller/.cache")
        == "[repo:0]/a.py and [home]/.cache"
    )
    assert scrub.text("sseller@sams-mbp:~$") == "[user]@[hostname]:~$"
    assert scrub.counts == {"author": 4, "repo": 1, "home": 1, "user": 1, "hostname": 1}


def test_identifiers_of_another_repository_carry_no_id(seller):
    other = {
        **SELLER,
        "name": "b",
        "path": "/srv/b",
        "authors": {"lee@b.test": 1},
        "people": [["Lee Park", "lee@b.test"]],
    }
    a, b = deid.known([SELLER, other])
    assert deid.Scrubber(a).text("Lee Park, Sam Seller") == "[author], [author:1]"
    assert deid.Scrubber(b).text("Lee Park, Sam Seller") == "[author:1], [author]"


def test_known_identifiers_are_found_escaped(seller):
    (k,) = deid.known([SELLER])
    said = deid.Scrubber(k).text('{"to": "sam.seller%40acme-corp.com", "home": "\\/Users\\/sseller"}')
    assert "acme-corp" not in said and "sam.seller" not in said


def test_public_hosts_and_checksums_stay_readable():
    scrub = deid.Scrubber(deid.Known())
    kept = [
        "Downloading https://repo.maven.apache.org/maven2/org/junit/junit/4.13.2/junit-4.13.2.pom",
        "go: downloading github.com/stretchr/testify v1.9.0",
        '"integrity": "sha512-Ek2Zy5w1vS4mQ0Tn7pK3jR9xH2bC6dF8gL1aW4eY7uI0oP3sD5fG8hJ2kL4zX6cV9bN1mQ3wE5rT7yU9iO0pA2s"',
        "commit 7f3c2a9e1b4d8f6a0c5e3b7d9a1f2c4e6b8d0a2c version 4.0.0.0 at 127.0.0.1:8080 and [::1]:80",
        "id 3f2a9c1e-7b4d-4e8f-9a6c-1d2e3f4a5b6c in internal_payments.billing.ledger at ledger.py:12",
        "std::vector<int> a::b 12:34:56",
    ]
    for line in kept:
        assert scrub.text(line) == line
    assert not scrub.counts


def test_patterns():
    scrub = deid.Scrubber(deid.Known())
    said = scrub.text(
        "mail ops@corp.example.io from 10.1.2.3 and fe80::1c8a:3bff:fe4d:9a21%en0 mac a4:83:e7:1c:2b:9f "
        "via https://jira.acme.internal/browse/FAL-1, db01.prod.acme.internal and git@git.acme.test:a/b.git "
        "with Qm9wLz2xR8vT4nK7cY1aD5fH3jL6pS9wB0eG and PASSWORD=hunter22"
    )
    assert said == (
        "mail [email] from [ipv4] and [ipv6] mac [mac] via [url], [host] and [url] "
        "with [redacted:high_entropy] and PASSWORD=[redacted:assignment]"
    )


def _packed(tmp_path, text: str) -> tuple[bytes, dict]:
    root = tmp_path / "logs"
    (root / "falcon_core").mkdir(parents=True)
    (root / "falcon_core" / "tip.test-1.log").write_text(text)
    return codeval.logs_packed(root, "", [SELLER])


def test_logs_are_de_identified_and_counted(tmp_path, seller):
    body, cleared = _packed(tmp_path, "Sam Seller at /Users/sseller/x on sams-mbp: 10.0.0.9\n")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        said = tar.extractfile("0/tip.test-1.log").read()
    assert said == b"[author:1] at [home]/x on [hostname]: [ipv4]\n"
    assert cleared == {
        "replaced": {"author": 1, "home": 1, "hostname": 1, "ipv4": 1},
        "model": "not run (KENNET_DEID_MODEL=off)",
    }


def test_nothing_is_sent_when_an_identifier_survives(tmp_path, seller, monkeypatch):
    monkeypatch.setattr(deid.Scrubber, "bytes", lambda self, data: data)
    with pytest.raises(deid.Unsafe) as e:
        _packed(tmp_path, "Sam Seller in /Users/sseller\n")
    assert e.value.code == "deid_incomplete" and "1 author, 1 home" in e.value.message
    assert "Sam" not in e.value.message and "sseller" not in e.value.message


def test_the_checks_report_is_de_identified(seller):
    body, cleared = codeval.checks_packed(
        {"schema": "s", "repos": [{"setup": [{"tries": ["cd /Users/sseller"]}]}]}, [SELLER]
    )
    got = json.loads(gzip.decompress(body))
    assert got == {"schema": "s", "repos": [{"setup": [{"tries": ["cd [home]"]}]}]} and cleared["replaced"] == {
        "home": 1
    }


def test_a_long_log_is_cut_at_line_ends():
    data = b"".join(b"line %07d /Users/sseller\n" % i for i in range(300_000))
    cut = codeval._cut(data)
    head, _, tail = cut.partition(b"[... cut by kennet codeval ...]\n")
    assert len(cut) <= codeval.LOG_FILE_MOST + 64
    assert head.endswith(b"\n") and tail.startswith(b"line ") and b"/Users/sseller\n" in tail[:40]


def test_the_model_is_off_when_asked():
    assert deid_model.load() == (None, "not run (KENNET_DEID_MODEL=off)")


@respx.mock
def test_a_download_that_does_not_match_its_hash_is_refused(tmp_path):
    body = b"x" * 10
    respx.get("https://huggingface.co/m").mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(deid_model.Refused, match="sha256"):
        deid_model.fetch("https://huggingface.co/m", 10, "0" * 64, tmp_path / "m")
    assert not list(tmp_path.iterdir())
    got = deid_model.fetch("https://huggingface.co/m", 10, hashlib.sha256(body).hexdigest(), tmp_path / "m")
    assert got.read_bytes() == body


def test_a_download_goes_only_to_pinned_hosts(tmp_path):
    with pytest.raises(deid_model.Refused, match="evil.test"):
        deid_model.fetch("https://evil.test/m", 10, "0" * 64, tmp_path / "m")


def test_the_model_pass_replaces_what_it_finds(seller):
    def model(lines):
        return [[(m.start(), m.end(), "name") for m in re.finditer(r"Maria Gonzalez", line)] for line in lines]

    (k,) = deid.known([SELLER])
    scrub = deid.Scrubber(k, model)
    assert scrub.text("owner Maria Gonzalez\nby Sam Seller\nMaria Gonzalez again") == (
        "owner [name]\nby [author:1]\n[name] again"
    )
    assert scrub.counts == {"author": 1, "name": 2}


@pytest.mark.parametrize(
    "line, said",
    [
        ("contact +44 20 7946 0958 now", "contact [phone] now"),
        ("owner phone: (208) 555-0143", "owner phone: [phone]"),
        ("fax=208-555-0143;", "fax=[phone];"),
        ("+     020-79460958", "+     [phone]"),
        ("dial 020 7946 0958 today", "dial [phone] today"),
        ("e164 +14155550143", "e164 [phone]"),
        ("tel: 555 0143 1234", "tel: [phone]"),
    ],
)
def test_phone_numbers(line, said):
    scrub = deid.Scrubber(deid.Known())
    assert scrub.text(line) == said and scrub.counts == {"phone": 1}


@pytest.mark.parametrize(
    "line, said",
    [
        ("ship to 1428 Elm Street, Apt 2C, Springfield, IL 62704 ok", "ship to [address] ok"),
        ("  77 Harbour View Rd, Dunedin 9016", "  [address]"),
        ("{'address': '9 rue des Lilas, 69003 Lyon'}", "{'address': '[address]'}"),
        ("at 21 Calle del Sol, Sevilla", "at [address]"),
        ("Hauptstraße 5, 10115 Berlin", "[address]"),
        ("query: 2-4-8 Minami-Aoyama, Minato-ku, Tokyo 107-0062", "query: [address]"),
    ],
)
def test_street_addresses(line, said):
    scrub = deid.Scrubber(deid.Known())
    assert scrub.text(line) == said and scrub.counts == {"address": 1}


def test_numbers_in_build_output_are_not_phones_or_addresses():
    scrub = deid.Scrubber(deid.Known())
    kept = [
        "Tests run: 48, Failures: 2, Errors: 0, Skipped: 1, Time elapsed: 12.503 s",
        "version 1.2.3 took 12.5s at 2026-11-19T09:14:22+05:30 on port 8080",
        "Date: Thu Nov 19 09:14:22 2026 +0000",
        " 12 files changed, 340 insertions(+), 12 deletions(-) +12 -3",
        "Ran 1428 tests in 3.200s; lines 120-130; build 20261119-0914",
        "Account 0042-7781 balance 1180.00 sha 3f2a9c1e +1234",
        "[xUnit.net 00:00:00.74] 3 Passed, 1 Failed, 0 Skipped",
        "=== RUN   TestLedgerTotals",
        "Name    Stmts   Miss  Cover",
    ]
    for line in kept:
        assert scrub.text(line) == line
    assert not scrub.counts


def test_an_authors_name_is_found_however_it_is_written(seller):
    people = [
        ["Kenji Watanabe", "kenji@acme-corp.com"],
        ["Liesl van der Merwe", "liesl@acme-corp.com"],
        ["Siobhan O'Connor", "siobhan@acme-corp.com"],
        ["Björn Ekström", "bjorn@acme-corp.com"],
        ["Nguyễn Thị Lan", "lan@acme-corp.com"],
    ]
    repo = {**SELLER, "authors": {e: n for n, (_name, e) in enumerate(people, 1)}, "people": people}
    (k,) = deid.known([repo])
    scrub = deid.Scrubber(k)
    for line, said in [
        ("WATANABE Kenji", "[author:1]"),
        ("Watanabe, Kenji", "[author:1]"),
        ("Kenji.Watanabe", "[author:1]"),
        ("van der Merwe, Liesl", "[author:2]"),
        ("Merwe, Liesl", "[author:2]"),
        ("Siobhan O&#39;Connor", "[author:3]"),
        ("Siobhan O’Connor", "[author:3]"),
        ("Siobhan OConnor", "[author:3]"),
        ("O'Connor, Siobhan", "[author:3]"),
        ("Bjorn Ekstrom", "[author:4]"),
        ("Nguyen Thi Lan", "[author:5]"),
    ]:
        assert scrub.text(f"by {line}.") == f"by {said}.", line


@pytest.mark.parametrize(
    "line",
    [
        'expected "WATANABE Kenji"',
        "-     Natarajan, Priya",
        "holder Liesl van der Merwe",
        "{'name': 'Nguyễn Thị Lan'}",
        "for D’Arcy McAllister",
        "Grace OMalley-Brennan",
    ],
)
def test_lines_that_could_hold_a_name_reach_the_model(line):
    seen = []
    deid.Scrubber(deid.Known(), lambda lines: seen.extend(lines) or [[] for _ in lines]).text(line)
    assert seen == [line]


def test_the_model_reads_escaped_apostrophes_and_its_spans_map_back():
    def model(lines):
        return [[(m.start(), m.end(), "name") for m in re.finditer(r"Siobhan O'Connor", line)] for line in lines]

    scrub = deid.Scrubber(deid.Known(), model)
    assert scrub.text("for Siobhan O&#39;Connor> but was Ann Lee") == "for [name]> but was Ann Lee"
    assert scrub.text("for Siobhan O&apos;Connor and Siobhan O'Connor") == "for [name] and [name]"
