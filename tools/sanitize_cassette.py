#!/usr/bin/env python3
"""Scrub sensitive values from a VCR / pytest-recording cassette.

A cassette captures real HTTP request/response pairs against live NetBox, Meraki,
ThousandEyes, etc. Before such a recording can enter the repository, it must be
sanitized: real IPs, MAC addresses, hostnames, vendor serial numbers, API tokens,
AWS ARNs, and JWTs are replaced with deterministic placeholders that preserve
structure (so graph-correlation tests keep working) but reveal no operational data.

Usage:
    python tools/sanitize_cassette.py <input>.unsanitized.yaml \
        --output tests/cassettes/<name>.yaml

Or, as a pre-commit gate, run against any tracked cassette:
    python tools/sanitize_cassette.py --verify tests/cassettes/<name>.yaml

Design choices
--------------
* All IPv4 addresses (public or private) → deterministic hash within RFC 5737
  test ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). We do NOT try
  to preserve subnet structure for correlator tests — those should use
  in-memory fixtures, not cassettes.
* IPv6 → 2001:db8::<hash> (RFC 3849 documentation range).
* MACs → 02:00:00:XX:XX:XX (locally-administered OUI).
* Hostnames matching configured customer pattern → device-N.
* Meraki serials (QXXX-XXXX-XXXX) → QXXX-XXXX-XXX{hash}.
* Bearer tokens, AWS ARNs, JWTs, private keys → <REDACTED-*>.

Idempotence: sanitized output uses values that the sanitizer itself recognizes
as already-scrubbed (test ranges, REDACTED markers). Re-running on a sanitized
file produces a byte-identical file — this is what makes ``--verify`` reliable
as a CI gate.

This script intentionally has no third-party dependencies beyond PyYAML so it
can run in a minimal CI shell.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - bootstrap error
    sys.stderr.write("PyYAML is required: pip install pyyaml\n")
    sys.exit(2)


# --- Regex sources --------------------------------------------------------------
# Each pattern is anchored conservatively to avoid false positives. Order matters:
# more specific patterns run first.

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"
)
MAC_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b|\b(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}\b"
)
MERAKI_SERIAL_RE = re.compile(r"\bQ[A-Z0-9]{3}-[A-Z0-9]{4}-[A-Z0-9]{4}\b")
# Cisco serials are 11 alnum chars but that pattern is too broad to scrub safely.
# We only scrub when explicitly tagged via context (e.g. "serial_number": "FCH...").
CISCO_SERIAL_KEY_RE = re.compile(
    r'("(?:serial_number|serial|serialNumber)"\s*:\s*")([A-Z0-9]{8,16})(")'
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b")
AWS_ARN_RE = re.compile(r"\barn:aws[a-z0-9\-]*:[a-z0-9\-]*:[a-z0-9\-]*:\d{12}:[^\s\"',]+")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED |)PRIVATE KEY-----.+?-----END (?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED |)PRIVATE KEY-----",
    re.DOTALL,
)
# Authorization header values, NetBox token, Meraki API key.
BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*)(bearer|token|api-key)\s+([^\s\"',]+)")
TOKEN_KV_RE = re.compile(
    r'("(?:token|api_key|apiKey|x-api-key|x_cisco_meraki_api_key|secret|password)"\s*:\s*")([^"]+)(")'
)
# Customer hostnames — configurable via --hostname-pattern. Defaults match
# common patterns seen in this codebase (cpn-*, *.local, *.corp).
DEFAULT_HOSTNAME_PATTERNS = [
    r"\bcpn-[a-z0-9\-]+\b",
    r"\b[a-z0-9\-]+\.(?:corp|local|internal|lan)\b",
]


@dataclass
class SanitizerStats:
    ipv4_pub: int = 0
    ipv4_priv: int = 0
    ipv6: int = 0
    mac: int = 0
    serial_meraki: int = 0
    serial_keyed: int = 0
    jwt: int = 0
    aws_key: int = 0
    aws_arn: int = 0
    private_key: int = 0
    bearer: int = 0
    token_kv: int = 0
    hostname: int = 0
    files: int = 0
    hostnames_seen: set[str] = field(default_factory=set)

    def summary(self) -> str:
        return (
            f"files={self.files} "
            f"ipv4_pub={self.ipv4_pub} ipv4_priv={self.ipv4_priv} ipv6={self.ipv6} "
            f"mac={self.mac} meraki_serial={self.serial_meraki} "
            f"keyed_serial={self.serial_keyed} jwt={self.jwt} "
            f"aws_key={self.aws_key} aws_arn={self.aws_arn} "
            f"private_key={self.private_key} bearer={self.bearer} "
            f"token_kv={self.token_kv} hostname={self.hostname}"
        )


def _stable_index(seed: str, mod: int) -> int:
    """Stable deterministic index for a given seed string within [0, mod)."""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % mod


_TEST_NETS = ("192.0.2", "198.51.100", "203.0.113")


def _is_already_scrubbed_ipv4(addr: ipaddress.IPv4Address) -> bool:
    """Return True if the address is already inside one of our scrub ranges.

    Critical for idempotence: re-running the sanitizer on a sanitized file
    must produce identical output, otherwise ``--verify`` cannot be a CI gate.
    """
    for net_prefix in _TEST_NETS:
        if str(addr).startswith(net_prefix + "."):
            return True
    return False


def _scrub_ipv4(match: re.Match[str], stats: SanitizerStats) -> str:
    raw = match.group(0)
    try:
        addr = ipaddress.IPv4Address(raw)
    except ValueError:
        return raw
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        return raw
    # Skip reserved ranges (which include TEST-NET ranges per ipaddress module).
    # Without this check, an already-sanitized 198.51.100.x address would be
    # re-randomized on the next run and ``--verify`` would falsely fail.
    if _is_already_scrubbed_ipv4(addr):
        return raw
    if addr.is_reserved:
        return raw
    # Map to one of three TEST-NET /24s, deterministically.
    if addr.is_private:
        stats.ipv4_priv += 1
    else:
        stats.ipv4_pub += 1
    net_idx = _stable_index(raw, len(_TEST_NETS))
    host = _stable_index(raw + ":host", 254) + 1
    return f"{_TEST_NETS[net_idx]}.{host}"


def _scrub_ipv6(match: re.Match[str], stats: SanitizerStats) -> str:
    raw = match.group(0)
    try:
        addr = ipaddress.IPv6Address(raw)
    except ValueError:
        return raw
    # Idempotence: skip if already in documentation range (RFC 3849).
    if addr in ipaddress.IPv6Network("2001:db8::/32"):
        return raw
    stats.ipv6 += 1
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"2001:db8::{suffix}"


def _scrub_mac(match: re.Match[str], stats: SanitizerStats) -> str:
    raw = match.group(0)
    # Idempotence: already in the locally-administered scrub prefix.
    normalized = raw.replace("-", ":").replace(".", "").lower()
    if normalized.startswith("02:00:00:") or normalized.startswith("020000"):
        return raw
    stats.mac += 1
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:6].upper()
    return f"02:00:00:{suffix[0:2]}:{suffix[2:4]}:{suffix[4:6]}"


def _scrub_meraki_serial(match: re.Match[str], stats: SanitizerStats) -> str:
    raw = match.group(0)
    # Idempotence: already in the QXXX-XXXX- scrub prefix.
    if raw.startswith("QXXX-XXXX-"):
        return raw
    stats.serial_meraki += 1
    n = _stable_index(raw, 0xFFFF)
    return f"QXXX-XXXX-{n:04X}"


def _scrub_keyed_serial(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.serial_keyed += 1
    return f'{match.group(1)}REDACTEDSERIAL{match.group(3)}'


def _scrub_jwt(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.jwt += 1
    return "<REDACTED-JWT>"


def _scrub_aws_key(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.aws_key += 1
    return "AKIA" + "X" * 16


def _scrub_aws_arn(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.aws_arn += 1
    return "arn:aws:redacted:us-east-1:000000000000:resource/REDACTED"


def _scrub_private_key(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.private_key += 1
    return "-----BEGIN PRIVATE KEY-----\n<REDACTED-PRIVATE-KEY>\n-----END PRIVATE KEY-----"


def _scrub_bearer(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.bearer += 1
    return f"{match.group(1)}{match.group(2)} <REDACTED-TOKEN>"


def _scrub_token_kv(match: re.Match[str], stats: SanitizerStats) -> str:
    stats.token_kv += 1
    return f'{match.group(1)}<REDACTED-TOKEN>{match.group(3)}'


def _scrub_hostname_factory(
    patterns: list[str],
    stats: SanitizerStats,
) -> list[tuple[re.Pattern[str], Any]]:
    compiled: list[tuple[re.Pattern[str], Any]] = []
    for pat in patterns:
        regex = re.compile(pat, re.IGNORECASE)

        def _scrub(match: re.Match[str], _stats: SanitizerStats = stats) -> str:
            raw = match.group(0)
            _stats.hostnames_seen.add(raw)
            _stats.hostname += 1
            n = _stable_index(raw, 9999)
            return f"device-{n}"

        compiled.append((regex, _scrub))
    return compiled


def sanitize_text(
    text: str,
    stats: SanitizerStats,
    hostname_patterns: list[str],
) -> str:
    """Apply all scrub patterns to a single text blob."""
    # Order: most-specific patterns first to avoid over-eager generic matches.
    text = PRIVATE_KEY_RE.sub(lambda m: _scrub_private_key(m, stats), text)
    text = JWT_RE.sub(lambda m: _scrub_jwt(m, stats), text)
    text = AWS_ARN_RE.sub(lambda m: _scrub_aws_arn(m, stats), text)
    text = AWS_ACCESS_KEY_RE.sub(lambda m: _scrub_aws_key(m, stats), text)
    text = BEARER_RE.sub(lambda m: _scrub_bearer(m, stats), text)
    text = TOKEN_KV_RE.sub(lambda m: _scrub_token_kv(m, stats), text)
    text = CISCO_SERIAL_KEY_RE.sub(lambda m: _scrub_keyed_serial(m, stats), text)
    text = MERAKI_SERIAL_RE.sub(lambda m: _scrub_meraki_serial(m, stats), text)
    for regex, fn in _scrub_hostname_factory(hostname_patterns, stats):
        text = regex.sub(lambda m, _fn=fn: _fn(m, stats), text)
    text = MAC_RE.sub(lambda m: _scrub_mac(m, stats), text)
    text = IPV6_RE.sub(lambda m: _scrub_ipv6(m, stats), text)
    text = IPV4_RE.sub(lambda m: _scrub_ipv4(m, stats), text)
    return text


def sanitize_file(
    path: Path,
    output: Path | None,
    stats: SanitizerStats,
    hostname_patterns: list[str],
    verify_only: bool = False,
) -> bool:
    raw = path.read_text(encoding="utf-8")
    sanitized = sanitize_text(raw, stats, hostname_patterns)
    stats.files += 1
    if verify_only:
        return raw == sanitized
    if output is None:
        output = path
    # Round-trip through YAML to validate structure if it parses as YAML;
    # otherwise write text verbatim (cassettes can also be JSON).
    try:
        yaml.safe_load(sanitized)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"warn: sanitized content for {path} no longer parses as YAML: {exc}\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sanitized, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Cassette file(s) to sanitize.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path (single-input only). Defaults to overwriting input in-place.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Do not write; exit 1 if any input contains values that would be scrubbed.",
    )
    parser.add_argument(
        "--hostname-pattern",
        action="append",
        default=None,
        help="Extra hostname regex(es) to scrub. May be passed multiple times.",
    )
    args = parser.parse_args(argv)

    hostname_patterns = list(DEFAULT_HOSTNAME_PATTERNS)
    if args.hostname_pattern:
        hostname_patterns.extend(args.hostname_pattern)

    if args.output and len(args.inputs) != 1:
        parser.error("--output requires exactly one input")

    stats = SanitizerStats()
    failed: list[Path] = []
    for raw_path in args.inputs:
        path = Path(raw_path)
        if not path.is_file():
            sys.stderr.write(f"error: not a file: {path}\n")
            return 2
        ok = sanitize_file(
            path,
            args.output if len(args.inputs) == 1 else None,
            stats,
            hostname_patterns,
            verify_only=args.verify,
        )
        if args.verify and not ok:
            failed.append(path)

    sys.stdout.write(stats.summary() + "\n")
    if args.verify:
        if failed:
            sys.stderr.write(
                "error: the following cassettes contain values that need scrubbing:\n"
            )
            for f in failed:
                sys.stderr.write(f"  {f}\n")
            return 1
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
