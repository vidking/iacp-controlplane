"""Export only non-secret Locust request statistics for a reviewed Phase C run."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--duration', required=True, type=int)
    parser.add_argument('--configured-rps', required=True, type=int)
    parser.add_argument('--accepted', action='store_true')
    args = parser.parse_args()
    raw = args.csv.read_bytes()
    rows = []
    with args.csv.open() as stream:
        for row in csv.DictReader(stream):
            rows.append({'endpoint': row['Name'], 'requests': int(row['Request Count']),
                         'failures': int(row['Failure Count']), 'rps': float(row['Requests/s']),
                         'p50_ms': float(row['50%']), 'p95_ms': float(row['95%']),
                         'p99_ms': float(row['99%']), 'max_ms': float(row['Max Response Time'])})
    result = {'source_file': args.csv.name, 'source_sha256': hashlib.sha256(raw).hexdigest(),
              'controlplane_revision': '4f153e89826f8e8decc25bcd083e093db37668fd',
              'client_revision': '927a5c56b9d7b7a4d16e53e1e97f2bc161eeb09b',
              'duration_seconds': args.duration, 'configured_rps': args.configured_rps,
              'accepted_as_phase_c_sample': args.accepted, 'users': 10,
              'scenario': 'readonly', 'vm_mutations': 0, 'rows': rows,
              'percentile_method': 'Locust 2.42.6 approximate histogram percentiles; aggregate is not an average of route percentiles'}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps(next(r for r in rows if r['endpoint'] == 'Aggregated')))


if __name__ == '__main__':
    main()
