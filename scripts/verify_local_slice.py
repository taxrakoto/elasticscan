"""Run the partial local integration slice without launching a scanner."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            'docker', 'compose', 'run', '--rm', '--no-deps',
            '-e', 'RUN_LOCAL_SLICE_TESTS=1',
            '-e', 'RUN_POSTGRES_TESTS=1',
            '-e', 'RUN_LOCALSTACK_TESTS=1',
            '-e', 'RUN_KEYCLOAK_TESTS=1',
            '-v', f'{root}:/workspace:ro',
            '-w', '/workspace',
            '--entrypoint', 'python',
            'api', '-B', '-m', 'tests.integration.test_local_slice',
        ],
        cwd=root,
        check=False,
    )
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
