"""Scan all reachable Git blobs without emitting or retaining credential values.

Content rules cover JSON fields, assignments, private keys, service token formats
and entropy near credential context. Findings contain locations and rule names
only. Synthetic fixture labels are classified separately, never silently dropped.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import subprocess


RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'bearer-value': re.compile(r'(?i)(?:Bearer|PVEAPIToken=)\s*([A-Za-z0-9._~+/@!=-]{24,})'),
    'service-token': re.compile(r'\b(?:AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{30,}|glpat-[\w-]{20,}|sk-[A-Za-z0-9_-]{30,})\b'),
    'credential-assignment': re.compile(
        r'''(?ix)["']?(?:password|passwd|api_token|access_token|refresh_token|client_secret|aws_secret_access_key|secret_access_key|PROXMOX_VE_API_TOKEN)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']'''),
}
SENSITIVE = re.compile(r'(?i)^(?:password|passwd|api_token|access_token|refresh_token|client_secret|aws_secret_access_key|secret_access_key|PROXMOX_VE_API_TOKEN|authorization)$')
SYNTHETIC = re.compile(r'(?i)fixture|dummy|example|test-token|test-secret|first-secret|second-token|first-token|second-secret|REEMPLAZAR|CHANGE_ME|placeholder|\$\{|^/|^https?://|^\*+$|^<[^>]+>$')


def entropy(value):
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())


def scan(data):
    found = set()
    for name, pattern in RULES.items():
        for match in pattern.finditer(data):
            value = match.group(1) if match.lastindex else match.group()
            classification = 'synthetic-or-reference' if SYNTHETIC.search(value) else 'review-required'
            found.add((name, data.count('\n', 0, match.start()) + 1, classification))
    # JSON semantic detection does not depend on HTTP header spelling.
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if SENSITIVE.match(key) and isinstance(child, str) and len(child) >= 8:
                    classification = 'synthetic-or-reference' if SYNTHETIC.search(child) else 'review-required'
                    found.add(('json-sensitive-field:' + key.lower(), 0, classification))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    try:
        walk(json.loads(data))
    except (ValueError, RecursionError):
        for line in data.splitlines():
            try:
                walk(json.loads(line))
            except (ValueError, RecursionError):
                pass
    for number, line in enumerate(data.splitlines(), 1):
        if re.search(r'(?i)password|secret|token|credential', line):
            for value in re.findall(r'[A-Za-z0-9_+/=-]{32,}', line):
                # Digests, public SSH keys, identifiers and code names are not secrets.
                if re.fullmatch(r'[a-fA-F0-9]{32,128}', value) or SYNTHETIC.search(value):
                    continue
                if entropy(value) >= 4.5 and not any(k in line.lower() for k in ('sha256', 'fingerprint', 'ssh-ed25519', 'ssh-rsa')):
                    found.add(('entropy-in-credential-context', number, 'review-required'))
    return sorted(found)


def audit(repo):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    objects = {}
    for line in git('rev-list', '--objects', '--all').decode().splitlines():
        oid, _, path = line.partition(' ')
        if path:
            objects.setdefault(oid, path)
    findings, count, skipped = [], 0, []
    proc = subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for oid, path in sorted(objects.items()):
            proc.stdin.write((oid + '\n').encode()); proc.stdin.flush()
            header = proc.stdout.readline().decode().split()
            size = int(header[2]); body = proc.stdout.read(size); proc.stdout.read(1)
            if header[1] != 'blob':
                continue
            count += 1
            if size > 16 * 1024 * 1024:
                skipped.append({'blob': oid, 'path': path, 'reason': 'over-16MiB'})
                continue
            if b'\0' in body:
                skipped.append({'blob': oid, 'path': path, 'reason': 'binary-manual-review-required'})
                continue
            for rule, line, classification in scan(body.decode('utf8', errors='replace')):
                findings.append({'blob': oid, 'path': path, 'line': line,
                                 'rule': rule, 'classification': classification})
    finally:
        proc.stdin.close(); proc.wait(timeout=5)
    return {'repo': repo.name, 'head': git('rev-parse', 'HEAD').decode().strip(),
            'reachable_blobs_scanned': count, 'skipped': skipped, 'findings': findings,
            'secret_values_emitted': False, 'scope': 'all locally reachable Git refs; text blobs <=16MiB',
            'remote_unfetched_refs_and_shared_screenshots': 'not-inspected'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repositories', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = [audit(repo) for repo in args.repositories]
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps([{'repo': r['repo'], 'blobs': r['reachable_blobs_scanned'],
                       'review_required': sum(f['classification'] == 'review-required' for f in r['findings']),
                       'skipped': len(r['skipped'])} for r in result]))


if __name__ == '__main__':
    main()
