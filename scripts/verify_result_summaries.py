"""Verify actual isolated ZAP counts through persistence and HTTP retrieval."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    scan = subprocess.run(
        ['sh', 'scripts/verify_zap_baseline.sh'],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    prefix = 'ZAP_RESULT_SUMMARY='
    summaries = [
        line[len(prefix):]
        for line in scan.stdout.splitlines()
        if line.startswith(prefix)
    ]
    if len(summaries) != 1:
        raise RuntimeError('Expected one actual ZAP result summary')
    summary = json.loads(summaries[0])
    if (
        not isinstance(summary, dict)
        or set(summary) != {'high', 'medium', 'low'}
        or any(type(value) is not int or value < 0 for value in summary.values())
    ):
        raise RuntimeError('Invalid actual ZAP result summary')

    subprocess.run(
        [
            'docker', 'compose', 'run', '--rm', '--no-deps',
            '-e', 'RUN_POSTGRES_TESTS=1',
            '-e', 'REAL_ZAP_RESULT_JSON=' + json.dumps(summary),
            '-v', f'{root}:/workspace:ro',
            '-w', '/workspace',
            '--entrypoint', 'python',
            'api', '-B', '-m', 'unittest',
            'tests.integration.test_result_summaries',
            'tests.integration.test_job_retrieval.JobRetrievalTests.'
            'test_actual_zap_counts_are_returned_over_http',
            '-v',
        ],
        cwd=root,
        check=True,
    )
    print('Actual ZAP counts persisted and retrieved over HTTP successfully.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
