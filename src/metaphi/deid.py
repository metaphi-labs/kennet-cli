"""De-identification of build and test output, run on this machine before anything is sent.

Three passes, each replacing what it finds with a typed placeholder, then a check that fails closed:

1. Known identifiers, replaced exactly: git authors and committers (as the profile's own author ids, so a log line
   joins to the profile), their email domains, the user, home folder, machine name, repository paths, remotes and
   their organizations.
2. Patterns: secrets (provider token shapes, keyword assignments, high-entropy strings), emails, IP and MAC
   addresses, hosts and URLs off a list of public registries and documentation sites, phone numbers and street
   addresses.
3. A local model for names and other personal data the first two miss (`metaphi.deid_model`), when it is installed.

Before anything is sent, the packed bytes are searched for every known identifier; if one is left, nothing is sent.
"""

from __future__ import annotations

import collections
import functools
import getpass
import json
import math
import os
import re
import socket
import tempfile
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metaphi import errors

SECRETS = (
    ("private_key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("metaphi_key", re.compile(rb"mai_sk_[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab_token", re.compile(rb"\bgl(?:pat|dt|rt|ptt|cbt|oas)-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(rb"\bxox[abposre]-[A-Za-z0-9-]{10,}")),
    ("slack_webhook", re.compile(rb"https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9+/_-]{20,}")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe_key", re.compile(rb"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("npm_token", re.compile(rb"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi_token", re.compile(rb"\bpypi-AgE[A-Za-z0-9_-]{50,}")),
    ("api_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("url_credentials", re.compile(rb"(?<=://)[^/\s:@\[]+:[^/\s@\[]+(?=@)")),
    ("bearer", re.compile(rb"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{16,}")),
    (
        "assignment",
        re.compile(
            rb"(?i)((?:TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|AUTH|SESSION(?:[_-]?ID)?|"
            rb"(?:API|ACCESS|SECRET|PRIVATE|SIGNING)[_-]?KEY)"
            rb"[\"']?\s*[=:]\s*[\"']?)(?!\[[a-z_]+(?::[A-Za-z0-9_]+)?\])[^\s\"',;]{4,}"
        ),
    ),
    (
        "keyword_token",
        re.compile(
            rb"(?i)(\b(?:token|bearer|auth|authorization|api[_-]?key|secret|password)\b[\s\"']+)[a-z0-9]{32,}\b"
        ),
    ),
    (
        "prefixed_token",
        re.compile(
            rb"\b[a-z][a-z0-9]{1,9}_(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])[A-Za-z0-9]{20,}\b"
        ),
    ),
    ("ssn", re.compile(rb"(?<![\w-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\w-])")),
)
"""Secret shapes, after gitleaks' rules and the keyword detector of detect-secrets. Each match becomes
`[redacted:<kind>]`; a keyword assignment keeps its name."""

NOT_A_VALUE = re.compile(
    rb"(?i:true|false|null|none|nil|undefined|required|optional|redacted|changeme|placeholder|example|xxx+|\.\.\.)"
    rb"|\$\{?\w+\}?|<[^>]*>?|\*+|%s|\{\{.*|\(.*|\[.*"
)
"""What follows `password=` or `token:` and is no secret: an obvious placeholder, a variable or a mask. Any other
value is taken for a secret, plain words included (`password: correcthorse`)."""

CARD = re.compile(rb"(?<![\w.-])(?:[3-6]\d{3}(?:[ -]?\d{4}){3}|3[47]\d{2}[ -]?\d{6}[ -]?\d{5})(?![\w-]|\.\d)")
"""A payment card number, 15 or 16 digits, kept only when its Luhn check holds."""


def _luhn(digits: bytes) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = c - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


ENTROPY_MIN_CHARS = 32
ENTROPY_BITS = 4.5
"""detect-secrets' Base64HighEntropyString limit. A token is long, mixes upper and lower case with digits, and is
not a hex digest or a package checksum: git ids and lockfile integrity hashes stay readable."""

TOKEN = re.compile(rb"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{%d,}={0,2}(?![A-Za-z0-9+/=_-])" % ENTROPY_MIN_CHARS)
CHECKSUM_BEFORE = re.compile(rb"(?i)(?:sha(?:1|256|384|512)[-:=]|h1:|integrity\W{0,4}|sum\W{0,4})$")

EMAIL = re.compile(rb"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
IPV4 = re.compile(
    rb"(?<![A-Za-z0-9@._-])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    rb"(?![A-Za-z0-9_-]|\.\d)"
)
VERSION_BEFORE = re.compile(rb"(?i)(?:version|ver|v|release)[\s:=]*$")
IPV6 = re.compile(
    rb"(?<![A-Za-z0-9:.])(?:"
    rb"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    rb"|(?:[0-9A-Fa-f]{1,4}:){1,6}(?::[0-9A-Fa-f]{1,4}){1,6}"
    rb"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?![0-9A-Fa-f])"
    rb"|::(?:[0-9A-Fa-f]{1,4}:){0,5}[0-9A-Fa-f]{1,4}"
    rb")(?:%[A-Za-z0-9]+)?(?![A-Za-z0-9:])"
)
MAC = re.compile(rb"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
URL = re.compile(rb"(?i)\b[a-z][a-z0-9+.-]{1,15}://[^\s\"'<>()\[\]{}`]+")
SCP_REMOTE = re.compile(rb"(?<![A-Za-z0-9._-])[A-Za-z0-9._-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):[A-Za-z0-9._~/-]+")
INTERNAL_TLDS = ("local", "internal", "corp", "lan", "intranet", "localdomain", "home", "priv", "private")
PUBLIC_TLD_SET = frozenset(
    [
        "com",
        "net",
        "org",
        "io",
        "dev",
        "ai",
        "co",
        "app",
        "cloud",
        "biz",
        "info",
        "me",
        "us",
        "uk",
        "de",
        "fr",
        "nl",
        "jp",
        "cn",
        "ca",
        "au",
        "eu",
        "ch",
        "se",
        "no",
        "fi",
        "dk",
        "es",
        "it",
        "ru",
        "kr",
        "tech",
        "site",
        "xyz",
        "systems",
        "services",
        "tools",
        "team",
        "company",
        "works",
        "online",
        "example",
        "test",
    ]
)
HOST = re.compile(rb"(?<![A-Za-z0-9_.@-])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9_-]|\.[A-Za-z0-9])")
"""A dotted name; it is a host when it ends in an internal top-level domain, or has three or more labels and ends in
a public one. File names (`ledger.py`) and Java packages (`com.acme.Ledger`) are not."""


def _is_host(name: bytes) -> bool:
    labels = name.lower().split(b".")
    tld = labels[-1].decode()
    return (tld in INTERNAL_TLDS and len(labels) >= 2) or (tld in PUBLIC_TLD_SET and len(labels) >= 3)


PUBLIC_HOSTS = frozenset(
    [
        "github.com",
        "githubusercontent.com",
        "github.io",
        "ghcr.io",
        "gitlab.com",
        "bitbucket.org",
        "sourceforge.net",
        "codeberg.org",
        "pypi.org",
        "pythonhosted.org",
        "python.org",
        "readthedocs.io",
        "readthedocs.org",
        "conda.anaconda.org",
        "anaconda.com",
        "anaconda.org",
        "npmjs.org",
        "npmjs.com",
        "yarnpkg.com",
        "nodejs.org",
        "unpkg.com",
        "jsdelivr.net",
        "maven.org",
        "apache.org",
        "sonatype.com",
        "gradle.org",
        "gradle.com",
        "jitpack.io",
        "spring.io",
        "jetbrains.com",
        "crates.io",
        "rust-lang.org",
        "rustup.rs",
        "docs.rs",
        "golang.org",
        "go.dev",
        "google.com",
        "googleapis.com",
        "gstatic.com",
        "googlesource.com",
        "gopkg.in",
        "rubygems.org",
        "ruby-lang.org",
        "nuget.org",
        "microsoft.com",
        "visualstudio.com",
        "azureedge.net",
        "packagist.org",
        "getcomposer.org",
        "hex.pm",
        "erlang.org",
        "hackage.haskell.org",
        "stackage.org",
        "cran.r-project.org",
        "r-project.org",
        "cocoapods.org",
        "swift.org",
        "docker.io",
        "docker.com",
        "quay.io",
        "gcr.io",
        "k8s.io",
        "kubernetes.io",
        "debian.org",
        "ubuntu.com",
        "canonical.com",
        "alpinelinux.org",
        "fedoraproject.org",
        "centos.org",
        "redhat.com",
        "gnu.org",
        "kernel.org",
        "llvm.org",
        "gcc.gnu.org",
        "cmake.org",
        "sqlite.org",
        "w3.org",
        "json-schema.org",
        "xmlsoap.org",
        "schemas.android.com",
        "android.com",
        "mozilla.org",
        "whatwg.org",
        "ietf.org",
        "eclipse.org",
        "junit.org",
        "pytest.org",
        "tox.wiki",
        "nixos.org",
        "brew.sh",
        "amazonaws.com",
        "cloudflare.com",
        "example.com",
        "example.org",
        "example.net",
        "localhost",
    ]
)
"""Hosts that stay readable: public registries, mirrors and documentation. A host matches itself and its subdomains."""


def _entropy(token: bytes) -> float:
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in collections.Counter(token).values())


def _public(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in PUBLIC_HOSTS) or host.startswith("[")


PLACEHOLDER_BYTES = rb"\[(?:redacted:)?[a-z_]+(?::[A-Za-z0-9_]+)?\]"
"""What de-identification writes: a known value equal to a placeholder's kind (`author@acme.io` gives the word
`author`) is never found inside one."""


@dataclass
class Known:
    """Identifiers known before the logs are read, each with the placeholder it becomes.

    People, paths and the machine are replaced first, so an author's address becomes their author id and not a bare
    `[email]`. Domains, organizations and remote hosts are replaced last, after the patterns, so a URL or host on a
    known domain is still found whole."""

    values: dict[str, tuple[str, bool, bool]] = field(default_factory=dict)
    segments: dict[str, str] = field(default_factory=dict)  # a name replaced as a whole path or URL segment
    _compiled: dict[bool, tuple[re.Pattern[bytes], dict[bytes, bytes]]] = field(default_factory=dict)

    def add(self, value: str | None, placeholder: str, word: bool = False, late: bool = False) -> None:
        """A value to replace. `word` marks a bare word (a user name, a machine name): it is replaced only where it
        stands alone, never inside a longer word. `late` replaces it after the patterns."""
        value = (value or "").strip()
        if len(value) < 3 or value.lower() in COMMON_WORDS:
            return
        self.values.setdefault(value, (placeholder, word, late))
        self._compiled.clear()

    def _pattern(self, late: bool | None) -> tuple[re.Pattern[bytes], dict[bytes, bytes]]:
        if late not in self._compiled:
            to: dict[bytes, bytes] = {}
            loose, bounded = set(), set()
            for value, (placeholder, word, is_late) in self.values.items():
                if late is not None and is_late != late:
                    continue
                for form in _forms(value):
                    to.setdefault(form.lower(), placeholder.encode())
                    (loose if form.startswith(b"/") and not word else bounded).add(form.lower())

            def alts(forms: set[bytes]) -> bytes:
                return b"|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) or b"(?!)"

            self._compiled[late] = (
                re.compile(
                    rb"(?i)((?-i:" + PLACEHOLDER_BYTES + rb"))"
                    rb"|(?:(?:" + alts(loose) + rb")|(?<![A-Za-z0-9_])(?:" + alts(bounded) + rb"))(?![A-Za-z0-9_])"
                ),
                to,
            )
        return self._compiled[late]

    def replace(self, data: bytes, counts: collections.Counter, late: bool = False) -> bytes:
        pattern, to = self._pattern(late)

        def sub(m: re.Match[bytes]) -> bytes:
            if m.group(1):
                return m.group(0)
            placeholder = to[m.group(0).lower()]
            counts[_kind_of(placeholder)] += 1
            return placeholder

        return pattern.sub(sub, data)

    def segment(self, name: str, placeholder: str) -> None:
        """A name replaced wherever it is a whole segment after a `/` (`/dev/ledgerline`, `host/fin/ledgerline.git`),
        not where it begins a longer one (`/Ledgerline.Core/`), and anywhere inside a URL or host name, joined or not
        (`ledgerline.halcyon-grid.com`, `.../ledgerline-protos`)."""
        if len(name) >= 3:
            self.segments.setdefault(name.lower(), placeholder)
            self._compiled.clear()

    def _segment_pattern(self) -> re.Pattern[bytes] | None:
        if not self.segments:
            return None
        names = b"|".join(re.escape(n.encode()) for n in sorted(self.segments, key=len, reverse=True))
        return re.compile(rb"(?i)(?<=/)(" + names + rb")(?:\.git)?(?![A-Za-z0-9_.-])")

    def _joined_pattern(self) -> re.Pattern[bytes]:
        names = b"|".join(re.escape(n.encode()) for n in sorted(self.segments, key=len, reverse=True))
        return re.compile(rb"(?i)(?<![A-Za-z0-9])(" + names + rb")(?![A-Za-z0-9])")

    def _addresses(self, data: bytes) -> list[tuple[int, int]]:
        """Where URLs and host names are in `data`."""
        spans = [m.span() for m in URL.finditer(data)]
        spans += [
            m.span()
            for m in HOST.finditer(data)
            if _is_host(m.group(0)) and not any(a <= m.start() < b for a, b in spans)
        ]
        return spans

    def replace_segments(self, data: bytes, counts: collections.Counter) -> bytes:
        pattern = self._segment_pattern()
        if pattern is None:
            return data

        def sub(m: re.Match[bytes]) -> bytes:
            placeholder = self.segments[m.group(1).decode().lower()]
            counts[_kind_of(placeholder.encode())] += 1
            return placeholder.encode()

        joined = self._joined_pattern()
        for a, b in sorted(self._addresses(data), reverse=True):
            data = data[:a] + joined.sub(sub, data[a:b]) + data[b:]
        return pattern.sub(sub, data)

    def left(self, data: bytes) -> collections.Counter:
        """Known identifiers still in `data`, counted by kind."""
        pattern, to = self._pattern(None)
        got = collections.Counter(_kind_of(to[m.group(0).lower()]) for m in pattern.finditer(data) if not m.group(1))
        if (segments := self._segment_pattern()) is not None:
            got.update(_kind_of(self.segments[m.group(1).decode().lower()].encode()) for m in segments.finditer(data))
            joined = self._joined_pattern()
            for a, b in self._addresses(data):
                got.update(
                    _kind_of(self.segments[m.group(1).decode().lower()].encode()) for m in joined.finditer(data[a:b])
                )
        return got


COMMON_WORDS = frozenset(
    [
        "admin",
        "administrator",
        "root",
        "user",
        "users",
        "runner",
        "build",
        "builder",
        "builds",
        "ci",
        "dev",
        "developer",
        "test",
        "tester",
        "tests",
        "ubuntu",
        "debian",
        "git",
        "github",
        "gitlab",
        "noreply",
        "no-reply",
        "bot",
        "bots",
        "info",
        "mail",
        "email",
        "team",
        "support",
        "admin1",
        "home",
        "local",
        "localhost",
        "main",
        "master",
        "default",
        "docker",
        "node",
        "app",
        "service",
        "server",
        "worker",
        "jenkins",
        "travis",
        "circleci",
        "vagrant",
        "ec2-user",
        "centos",
        "fedora",
        "codespace",
        "codespaces",
        "vscode",
        "code",
        "work",
        "project",
        "projects",
        "src",
        "source",
        "null",
        "none",
        "nobody",
        "unknown",
        "example",
    ]
)
"""Values too common to replace as identifiers: replacing them would rewrite ordinary log text."""


def _forms(value: str) -> set[bytes]:
    raw = value.encode()
    forms = {raw, json.dumps(value)[1:-1].encode(), urllib.parse.quote(value, safe="/").encode()}
    forms.add(urllib.parse.quote(value, safe="").encode())
    return {f for f in forms if f}


def _kind_of(placeholder: bytes) -> str:
    return placeholder[1:-1].split(b":")[0].decode()


def _remote_parts(url: str) -> tuple[str | None, str | None]:
    """The host and owning organization of a git remote URL, or None for what it lacks."""
    m = re.match(r"^[\w.+-]+://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$", url) or re.match(
        r"^(?:[^@/]+@)?([^/:]+):(.*)$", url
    )
    if not m:
        return None, None
    parts = [p for p in m[2].removesuffix(".git").split("/") if p]
    return m[1], (parts[-2] if len(parts) >= 2 else None)


PARTICLES = frozenset(
    ["van", "von", "der", "den", "de", "del", "della", "dei", "di", "da", "das", "dos", "du", "des", "la", "le"]
    + ["ter", "ten", "bin", "ibn", "al", "el", "y", "zu", "af", "av"]
)
"""Lower-case words that open a surname (`van der Merwe`, `de la Cruz`, `bin Salem`): they go with it."""

APOSTROPHES = ("'", "’", "&#39;", "&apos;", "&#x27;", "")
FOLDED = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "æ": "ae", "œ": "oe"})


def _folded(text: str) -> str:
    """Without diacritics: `Nguyễn Thị` as `Nguyen Thi`, `Ekström` as `Ekstrom`."""
    decomposed = unicodedata.normalize("NFKD", text.translate(FOLDED))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def name_forms(name: str) -> set[str]:
    """The ways a person's name is written in output: as given, surname first, `Surname, Given`, dotted, with
    diacritics folded, and with its apostrophes typed, curled, HTML-escaped or dropped. Matching ignores case, so
    `WATANABE Kenji` is `Watanabe Kenji`."""
    words = name.split()
    forms = {name}
    if len(words) >= 2:
        at = next((n for n, w in enumerate(words[1:], 1) if w.lower() in PARTICLES), len(words) - 1)
        given, surname = " ".join(words[:at]), " ".join(words[at:])
        forms |= {f"{surname} {given}", f"{surname}, {given}", ".".join(words)}
        if words[at].lower() in PARTICLES:
            bare = " ".join(w for w in words[at:] if w.lower() not in PARTICLES)
            forms |= {f"{bare}, {given}"}
    forms |= {_folded(f) for f in forms}
    for f in list(forms):
        for mark in ("'", "’"):
            if mark in f:
                forms |= {f.replace(mark, other) for other in APOSTROPHES}
    return {" ".join(f.split()) for f in forms if f.strip()}


NAME_PART_MIN = 4
WORDS_FILE = Path(__file__).parent / "data" / "common_words.txt"


@functools.cache
def _dictionary() -> frozenset[str]:
    """Common English words, from the list this package carries (the same on every machine): a given name or surname
    that is also one (`Mark`, `Grace`, `Hunter`) is not replaced alone, or ordinary text would be rewritten."""
    return frozenset(w.strip().lower() for w in WORDS_FILE.read_text().splitlines() if w.strip())


def name_parts(name: str) -> set[str]:
    """The given names and surnames of a person's name, each standing alone (`Reviewed by Natarajan`, `ping priya`):
    4 letters or more, and neither a common word nor a dictionary one."""
    out = set()
    for word in re.split(r"[\s,]+", name):
        word = word.strip(".")
        if (
            len(word) >= NAME_PART_MIN
            and word.lower() not in PARTICLES
            and word.lower() not in COMMON_WORDS
            and word.lower() not in _dictionary()
            and not re.search(r"\d", word)
        ):
            out |= {word, _folded(word)}
            for mark in ("'", "’"):
                if mark in word:
                    out |= {word.replace(mark, other) for other in APOSTROPHES}
    words = [_folded(w.strip(".")) for w in re.split(r"[\s,]+", name) if w and w.lower() not in PARTICLES]
    if len(words) >= 2 and all(w.isalpha() for w in (words[0], words[-1])):
        first, last = words[0], words[-1]
        for joined in (first[0] + last, first + last, first + last[0], last + first[0]):
            if len(joined) >= NAME_PART_MIN + 1:
                out.add(joined)
        for initial in (f"{first[0]}. {last}", f"{first[0]}.{last}", f"{first[0]} {last}"):
            out.add(initial)
    return out


def _url_names(repo: dict[str, Any]) -> set[str]:
    """The repository's name as its remotes give it (`git@host:fin/ledgerline.git`: `ledgerline`)."""
    out = set()
    for url in [*(repo.get("remotes") or []), repo.get("given") or ""]:
        if re.match(r"^(?:[\w.+-]+://|[\w.-]+@[\w.-]+:)", url):
            tail = re.split(r"[/:]", url.rstrip("/"))[-1]
            out.add(re.sub(r"\.git$", "", tail))
    return out


def _repo_name(k: Known, name: str, n: int, declared: list[str]) -> None:
    """The repository's name is an identifier: always replaced in a path or URL (`/ledgerline`), and as a bare word
    too unless the repository declares a package or module of that name or under it (`ledgerline-core`), or it is a
    common word (`api`, `server`).
    Replaced after the patterns, so a URL that holds it is still found whole."""
    if len(name) < 4:
        return
    k.segment(name, f"[repo:{n}]")
    folded = re.sub(r"[-_.\s]+", "-", name.lower())
    namespace = any(d == folded or d.startswith(folded + "-") for d in declared)
    if not namespace and name.lower() not in COMMON_WORDS and name.lower() not in _dictionary():
        k.add(name, f"[repo:{n}]", word=True, late=True)


def known(repos: Iterable[dict[str, Any]]) -> list[Known]:
    """The known identifiers for each repository read by `codeval.profiled`, in its order. Each repository's
    authors become `[author:<id>]` with the profile's ids for that repository; what belongs to the machine is shared."""
    machine = Known()
    home = str(Path.home())
    for value, placeholder in ((tempfile.gettempdir(), "[tmp]"), (os.path.realpath(tempfile.gettempdir()), "[tmp]")):
        if value not in ("/tmp", "/var/tmp"):
            machine.add(value, placeholder)
    if home not in ("/", ""):
        machine.add(home, "[home]")
        machine.add(os.path.realpath(home), "[home]")
    for user in {getpass.getuser(), os.environ.get("USER", ""), os.environ.get("LOGNAME", ""), Path(home).name}:
        machine.add(user, "[user]", word=True)
    for host in {socket.gethostname(), socket.getfqdn()}:
        machine.add(host, "[hostname]", word=True)
        machine.add(host.split(".")[0], "[hostname]", word=True)
    out = []
    orgs: dict[str, int] = {}
    hosts: dict[str, int] = {}
    for n, repo in enumerate(repos):
        k = Known(dict(machine.values))
        path = repo.get("path")
        if path:
            k.add(str(path), f"[repo:{n}]")
            k.add(os.path.realpath(path), f"[repo:{n}]")
        for name in {repo.get("name") or "", *_url_names(repo)}:
            _repo_name(k, name, n, repo.get("declared") or [])
        authors: dict[str, int] = repo.get("authors") or {}
        domains: dict[str, int] = repo.get("domains") or {}
        for email, i in authors.items():
            k.add(email, f"[author:{i}]")
            local, _, domain = email.partition("@")
            handle = local.split("+", 1)[1] if domain.endswith("users.noreply.github.com") and "+" in local else local
            if len(handle) >= 4 and not re.fullmatch(r"\d+", handle):
                k.add(handle, f"[author:{i}]", word=True)
            if domain and not _public(domain) and domain not in PUBLIC_EMAIL and domain in domains:
                k.add(domain, f"[domain:{domains[domain]}]", late=True)
        for name, email in repo.get("people") or []:
            i = authors.get(email.lower())
            if i is not None and "@" not in name:
                for form in name_forms(name):
                    k.add(form, f"[author:{i}]", word=" " not in form)
                for part in name_parts(name):
                    k.add(part, f"[author:{i}]", word=True)
        urls = set(repo.get("remotes") or [])
        given = repo.get("given") or ""
        if re.match(r"^(?:[\w.+-]+://|[\w.-]+@[\w.-]+:)", given):
            urls.add(given)
        for url in sorted(urls):
            host, org = _remote_parts(url)
            k.add(url, f"[remote:{n}]")
            if host and not _public(host):
                k.add(host, f"[host:{hosts.setdefault(host.lower(), len(hosts) + 1)}]", word=True, late=True)
            if org:
                k.add(org, f"[org:{orgs.setdefault(org.lower(), len(orgs) + 1)}]", word=True, late=True)
        out.append(k)
    for k in out:
        for other in out:
            for name in other.segments if other is not k else ():
                k.segment(name, "[repo]")
            for value, (placeholder, word, late) in other.values.items():
                k.add(value, re.sub(r"^\[(author|domain):\d+\]$", r"[\1]", placeholder), word, late)
    return out


PUBLIC_EMAIL = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "aol.com",
        "gmx.com",
        "gmx.de",
        "mail.ru",
        "yandex.ru",
        "qq.com",
        "163.com",
        "users.noreply.github.com",
    ]
)


def _secrets(data: bytes, counts: collections.Counter) -> bytes:
    for kind, pattern in SECRETS:
        tag = b"[redacted:" + kind.encode() + b"]"

        def sub(m: re.Match[bytes], tag: bytes = tag, kind: str = kind) -> bytes:
            if m.re.groups and NOT_A_VALUE.fullmatch(m.group(0)[len(m.group(1)) :]):
                return m.group(0)
            counts[kind] += 1
            return (m.group(1) if m.re.groups else b"") + tag

        data = pattern.sub(sub, data)

    def card(m: re.Match[bytes]) -> bytes:
        digits = re.sub(rb"\D", b"", m.group(0))
        if len(set(digits)) < 2 or not _luhn(digits):
            return m.group(0)
        counts["card"] += 1
        return b"[redacted:card]"

    data = CARD.sub(card, data)

    def high(m: re.Match[bytes]) -> bytes:
        token = m.group(0)
        body = token.rstrip(b"=")
        if (
            re.fullmatch(rb"[0-9a-fA-F]+", body)
            or not (re.search(rb"[a-z]", body) and re.search(rb"[A-Z]", body) and re.search(rb"\d", body))
            or body.count(b"/") > 2
            or CHECKSUM_BEFORE.search(data[max(0, m.start() - 16) : m.start()])
            or _entropy(body) < ENTROPY_BITS
        ):
            return token
        counts["high_entropy"] += 1
        return b"[redacted:high_entropy]"

    return TOKEN.sub(high, data)


def _patterns(data: bytes, counts: collections.Counter) -> bytes:
    def counted(kind: str, placeholder: bytes) -> Callable[[re.Match[bytes]], bytes]:
        def sub(m: re.Match[bytes]) -> bytes:
            counts[kind] += 1
            return placeholder

        return sub

    def url(m: re.Match[bytes]) -> bytes:
        text = m.group(0)
        trail = re.search(rb"[.,;:!?]+$", text)
        cut = trail.start() if trail else len(text)
        host = urllib.parse.urlsplit(text[:cut].decode("utf-8", "replace")).hostname or ""
        if _public(host) or not host or host.startswith("[") or _is_ip_literal(host):
            return text
        counts["url"] += 1
        return b"[url]" + text[cut:]

    def scp(m: re.Match[bytes]) -> bytes:
        if _public(m.group(1).decode()):
            return m.group(0)
        counts["url"] += 1
        return b"[url]"

    def host(m: re.Match[bytes]) -> bytes:
        if not _is_host(m.group(0)) or _public(m.group(0).decode()):
            return m.group(0)
        counts["host"] += 1
        return b"[host]"

    def ipv4(m: re.Match[bytes]) -> bytes:
        text = m.group(0)
        if (
            text.startswith((b"127.", b"0."))
            or text in (b"255.255.255.255", b"255.255.255.0")
            or VERSION_BEFORE.search(m.string[max(0, m.start() - 12) : m.start()])
            or re.search(rb"[A-Za-z]:$", m.string[max(0, m.start() - 2) : m.start()])
        ):
            return text
        counts["ipv4"] += 1
        return b"[ipv4]"

    def ipv6(m: re.Match[bytes]) -> bytes:
        text = m.group(0)
        if text in (b"::1", b"::") or not re.search(rb"\d", text) or text.count(b":") < 2:
            return text
        counts["ipv6"] += 1
        return b"[ipv6]"

    data = URL.sub(url, data)
    data = SCP_REMOTE.sub(scp, data)
    data = EMAIL.sub(counted("email", b"[email]"), data)
    data = MAC.sub(counted("mac", b"[mac]"), data)
    data = IPV6.sub(ipv6, data)
    data = IPV4.sub(ipv4, data)
    data = HOST.sub(host, data)
    data = _addresses(data, counts)
    return _phones(data, counts)


_UP = r"[^\W\d_a-z]"
"""An upper-case letter, or any letter outside ASCII: `re` has no Unicode case classes, and a lower-case accented
letter opening a word is rare enough in output."""
_CAP = rf"{_UP}[^\W\d_]*(?:['\u2019.-][^\W\d_]+)*\.?"
_JOINS = r"(?:de|del|della|di|da|das|dos|do|du|des|la|le|les|el|al|ibn|bin|van|von|der|den|ter|of|y)"
_TYPES = (
    r"Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace|Ter|Parkway|Pkwy"
    r"|Highway|Hwy|Square|Sq|Crescent|Cres|Close|Circle|Cir|Trail|Row|Alley|Grove|Gardens|Mews"
)
_LEADS = (
    r"(?i:rue|avenue|av\.|boulevard|bd|chemin|impasse|all[ée]e|quai|place|via|viale|piazza|corso|calle|carrer"
    r"|avenida|rua|travessa|plaza|paseo|ulica|ul\.)"
)
_UNIT = (
    r"(?:Apt|Apartment|Suite|Ste|Unit|Flat|Floor|Fl|Bldg|Building|Room|Rm|Office|Villa)\b\.?[ \t]*#?[\w-]+|#[\w-]+"
    r"|\d{1,3}(?:st|nd|rd|th)[ \t]+(?:Floor|Fl)\.?"
)
_UK_POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]?[ \t]+\d[A-Z]{2}"
_BUILDINGS = r"Tower|Towers|Building|House|Centre|Center|Plaza|Complex|Court|Mall|Park"
_PART = (
    rf"{_UNIT}"
    rf"|\d{{4,6}}(?:-\d{{3,4}})?(?:[ \t]+{_CAP}){{1,3}}"
    rf"|{_CAP}(?:[ \t]+{_CAP}){{0,2}}[ \t]+{_UK_POSTCODE}"
    rf"|{_UK_POSTCODE}"
    rf"|{_CAP}(?:[ \t]+{_CAP}){{0,2}}(?:[ \t]+[A-Z]{{2}})?(?:[ \t]+\d{{3,6}}(?:-\d{{3,4}})?)?"
    rf"|[A-Z]{{2}}[ \t]+\d{{5}}(?:-\d{{4}})?"
)
ADDRESS = re.compile(
    r"(?<![\w.:/#-])(?:"
    rf"\d{{1,6}}[A-Za-z]?(?:[ \t]+{_CAP}){{1,4}}[ \t]+(?:{_TYPES})\b\.?"
    rf"|\d{{1,5}}[A-Za-z]?,?[ \t]+{_LEADS}(?:[ \t]+(?:{_JOINS}[ \t]+)*{_CAP}){{1,4}}"
    rf"|(?:Office|Suite|Unit|Flat|Villa|Apt)\.?[ \t]*\d{{1,5}}[A-Za-z]?,[ \t]*(?:{_CAP}[ \t]+){{1,3}}(?:{_BUILDINGS})\b"
    rf"|{_UP}[^\W\d_]*(?:stra(?:ss|ß)e|str\.|weg|gasse|platz|allee|straat|gracht|laan|kade|vej|gade|gatan|gata|veien"
    r"|v[äa]gen|torget|katu|tie|utca|ulica)"
    r"[ \t]+\d{1,4}[a-z]?\b"
    rf"|\d{{1,2}}-\d{{1,2}}(?:-\d{{1,3}})?[ \t]+{_CAP}(?=,)"
    rf")(?:,?[ \t]+(?:{_UNIT}))?(?:,[ \t]*(?:{_PART})){{0,3}}"
)
"""A street address: a number and a street with its type (`1428 Willow Creek Rd`), a number and a leading type
(`17 rue de la Paix`), a street with its type joined and a number (`Hauptstraße 5`), or a block number
(`3-7-1 Nishi-Shinjuku,`), then its unit, city, region and postal code. Build output almost never has one."""

PHONES = (
    re.compile(rb"(?<![\w+/.=-])\+[1-9]\d{0,2}(?:[ .-]?\(?\d{1,5}\)?){1,6}(?![\w.:/-]|\.\d)"),
    re.compile(rb"(?<![\w./:-])\(\d{3}\)[ ]?\d{3}[-.]\d{4}(?![\w.:/-])"),
    re.compile(rb"(?<![\w./:-])(?:\d{3}-\d{3}-\d{4}|0\d{1,4}-\d{6,8}|0\d{1,4} \d{3,4} \d{3,4})(?![\w.:/-])"),
    re.compile(rb"(?i)(\b(?:phone|tel|telephone|mobile|cell|fax)[ \t]?[:=#][ \t]*[\"']?)(\(?\d[\d ().-]{5,}\d)"),
)
"""Phone numbers: international (`+44 20 7946 0958`, 8 to 15 digits), North American (`(208) 555-0143`,
`208-555-0143`), trunk-prefixed national (`020-79460958`, `020 7946 0958`), and digits after `phone:` or `tel=`."""


PHONE_WORD = re.compile(rb"(?i)phone|\btel\b|mobile|contact|\bcell\b|\bfax\b")
BARE_PHONE = re.compile(rb"(?<![\w.:/+-])\+?\d{10,15}(?![\w.:/-])")
"""A bare run of 10 to 15 digits, a phone number on a line that names one (`normalisesPhone ... was: <4155550172>`);
elsewhere a run like it is an id or a time."""


def _bare_phones(data: bytes, counts: collections.Counter) -> bytes:
    if not PHONE_WORD.search(data):
        return data
    lines = data.split(b"\n")
    for i, line in enumerate(lines):
        if PHONE_WORD.search(line):

            def sub(m: re.Match[bytes]) -> bytes:
                counts["phone"] += 1
                return b"[phone]"

            lines[i] = BARE_PHONE.sub(sub, line)
    return b"\n".join(lines)


def _phones(data: bytes, counts: collections.Counter) -> bytes:
    def sub(m: re.Match[bytes]) -> bytes:
        head = m.group(1) if m.re.groups else b""
        number = m.group(m.re.groups)
        digits = sum(c in b"0123456789" for c in number)
        plus = m.re is PHONES[0]
        if not 7 <= digits <= 15 or (plus and digits < (8 if re.search(rb"[ .()-]", number) else 10)):
            return m.group(0)
        if VERSION_BEFORE.search(m.string[max(0, m.start() - 12) : m.start()]):
            return m.group(0)
        counts["phone"] += 1
        return head + b"[phone]"

    for pattern in PHONES:
        data = pattern.sub(sub, data)
    return _bare_phones(data, counts)


def _addresses(data: bytes, counts: collections.Counter) -> bytes:
    if not re.search(rb"\d[ \t,]+[^\W\d_]|[a-z\xc3\x9f][ \t]+\d", data):
        return data
    text = data.decode("utf-8", "surrogateescape")

    def sub(m: re.Match[str]) -> str:
        counts["address"] += 1
        return "[address]"

    return ADDRESS.sub(sub, text).encode("utf-8", "surrogateescape")


def _is_ip_literal(host: str) -> bool:
    return bool(re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", host))


Model = Callable[[list[str]], list[list[tuple[int, int, str]]]]
"""A model takes lines of text and gives, for each, the (start, end, kind) of the personal data it holds."""

_NAME_WORD = r"(?:Ma?c|[DO]['’]?)?[^\W\d_a-z](?:[^\W\d_A-Z]*|[^\W\d_a-z]*)(?:['’-][^\W\d_a-z]?[^\W\d_A-Z]+)*\.?"
"""A word that can be part of a name: capitalized (`Kenji`, `McAllister`, `O'Malley-Brennan`, `Ekström`) or all
upper-case (`WATANABE`); never CamelCase (`TestLedgerTotals`)."""
CANDIDATE = re.compile(
    rf"(?<![\w./\\-]){_NAME_WORD}(?:,? (?:(?:{'|'.join(sorted(PARTICLES))}) )*{_NAME_WORD})+(?![\w/\\])"
)
"""Lines the model reads: those with two capitalized words in a row, where a person's name could stand, also with a
comma between (`Natarajan, Priya`), particles (`van der Merwe`), an upper-case surname (`WATANABE Kenji`) or letters
outside ASCII (`Nguyễn Thị Lan`). Build output is mostly paths, numbers and lowercase identifiers; reading only these
lines keeps the model's time small."""

ESCAPED_APOSTROPHE = re.compile(r"&#0*39;|&#[xX]0*27;|&apos;|&#8217;|&rsquo;")


def _unescaped(line: str) -> tuple[str, list[int]]:
    """`line` with HTML-escaped apostrophes as `'`, which the model reads as part of a name, and for each of its
    characters (and its end) where it starts in `line`."""
    out, where, at = [], [], 0
    for m in ESCAPED_APOSTROPHE.finditer(line):
        out.append(line[at : m.start()])
        where += range(at, m.start())
        out.append("'")
        where.append(m.start())
        at = m.end()
    out.append(line[at:])
    where += range(at, len(line) + 1)
    return "".join(out), where


PLACEHOLDER = re.compile(r"\[[a-z_]+(?::[A-Za-z0-9_]+)?\]")


MODEL_WORDS = 256
MODEL_OVERLAP = 32
WORDS = re.compile(r"\w+(?:[-_]\w+)*|\S")
MODEL_EVERY_LINE = False
"""Whether the model reads every line with a letter in it, or only those `CANDIDATE` finds. Measured on the
labelled logs: every line costs six times the model time (about a minute a MiB on a laptop) and catches no name the
candidate lines miss, at lower precision; the known authors' names and their parts are found without the model."""


def _windows(line: str) -> list[tuple[int, int]]:
    """A line as the model reads it: whole when short, else windows of MODEL_WORDS words that overlap by
    MODEL_OVERLAP, so a name across a window's edge is still read whole in one of them."""
    spans = [m.span() for m in WORDS.finditer(line)]
    if len(spans) <= MODEL_WORDS:
        return [(0, len(line))]
    out, step = [], MODEL_WORDS - MODEL_OVERLAP
    for i in range(0, len(spans), step):
        part = spans[i : i + MODEL_WORDS]
        out.append((part[0][0], part[-1][1]))
        if i + MODEL_WORDS >= len(spans):
            break
    return out


def _read(line: str) -> bool:
    bare = PLACEHOLDER.sub(" ", line)
    return bool(re.search(r"[^\W\d_]{2}", bare)) if MODEL_EVERY_LINE else bool(CANDIDATE.search(bare))


def _modelled(data: bytes, model: Model, counts: collections.Counter) -> bytes:
    text = data.decode("utf-8", "surrogateescape")
    lines = text.split("\n")
    wanted: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        if _read(line):
            wanted.setdefault(line, []).append(i)
    if not wanted:
        return data
    unique = list(wanted)
    pieces: list[tuple[int, int, int]] = []  # (line, start, end) of each window read
    for n, line in enumerate(unique):
        pieces += [(n, a, b) for a, b in _windows(line)]
    shown = [_unescaped(unique[n][a:b]) for n, a, b in pieces]
    found: dict[int, list[tuple[int, int, str]]] = collections.defaultdict(list)
    for (n, a, _b), (_read_, where), spans in zip(pieces, shown, model([r for r, _w in shown]), strict=True):
        found[n] += [(a + where[s], a + where[e], k) for s, e, k in spans]
    for n, line in enumerate(unique):
        out, at = [], 0
        for start, end, kind in sorted(set(found[n])):
            if start < at or PLACEHOLDER.fullmatch(line[start:end]) or PLACEHOLDER.search(line[start:end]):
                continue
            out += [line[at:start], f"[{kind}]"]
            at = end
            counts[kind] += len(wanted[line])
        if at:
            new = "".join([*out, line[at:]])
            for i in wanted[line]:
                lines[i] = new
    return "\n".join(lines).encode("utf-8", "surrogateescape")


@dataclass
class Scrubber:
    """De-identifies text for one repository and counts what it replaced, by kind, never by value."""

    known: Known
    model: Model | None = None
    counts: collections.Counter = field(default_factory=collections.Counter)

    def bytes(self, data: bytes) -> bytes:
        data = _secrets(data, self.counts)
        data = self.known.replace(data, self.counts)
        data = _patterns(data, self.counts)
        data = self.known.replace(data, self.counts, late=True)
        data = self.known.replace_segments(data, self.counts)
        if self.model is not None:
            data = _modelled(data, self.model, self.counts)
        return data

    def text(self, value: str) -> str:
        return self.bytes(value.encode("utf-8", "surrogateescape")).decode("utf-8", "surrogateescape")

    def doc(self, value: Any) -> Any:
        """A JSON document with every string in it de-identified; keys and numbers kept."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.doc(v) for v in value]
        if isinstance(value, dict):
            return {k: self.doc(v) for k, v in value.items()}
        return value


class Unsafe(errors.MetaphiError):
    type = "deid_error"


def check(found: collections.Counter, what: str) -> None:
    """Raise, so that nothing is sent, when a known identifier is still in a payload."""
    if not found:
        return
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(found.items()))
    raise Unsafe(
        f"{what} still holds known identifiers after de-identification ({kinds}); nothing was sent",
        code="deid_incomplete",
        next_step="nothing has left this machine; report the kinds above (never the values) at "
        "https://github.com/metaphi-labs/kennet-cli/issues",
    )


def summary(counts: collections.Counter | dict[str, int], model: str) -> dict[str, Any]:
    """What de-identification replaced, by kind, and whether the model pass ran: for sent.json and the preview."""
    return {"replaced": dict(sorted(counts.items())), "model": model}


__all__ = ["SECRETS", "Known", "Model", "Scrubber", "Unsafe", "check", "known", "summary"]
