"""Explicit idle process for components whose feature implementation is pending."""
import argparse
import signal
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('dispatcher', 'worker'), required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    print(f'{args.role}: bootstrap only; no jobs are consumed, published, or scanned.', flush=True)
    ready = Path('/tmp/bootstrap-ready')
    ready.touch()
    stopped.wait()
    ready.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
