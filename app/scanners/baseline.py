"""Bounded execution of the packaged ZAP baseline script."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from app.scanners.base import ScanRequest, ScannerOutput


def kill_group(process) -> None:
    """Stop the scanner and any remaining children in its process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


class ZapBaselineAdapter:
    def __init__(self, *, crawl_minutes=1, total_timeout_seconds=900):
        for name, value in (
            ('crawl_minutes', crawl_minutes),
            ('total_timeout_seconds', total_timeout_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        self.crawl_minutes = crawl_minutes
        self.total_timeout_seconds = total_timeout_seconds

    @classmethod
    def from_environment(cls):
        return cls(
            crawl_minutes=int(os.environ.get('ZAP_CRAWL_MINUTES', '1')),
            total_timeout_seconds=int(
                os.environ.get('ZAP_TOTAL_TIMEOUT_SECONDS', '900')
            ),
        )

    def run(self, request: ScanRequest) -> ScannerOutput:
        with tempfile.TemporaryDirectory(prefix='elasticscan-zap-') as directory:
            work = Path(directory)
            (work / 'request.json').write_text(json.dumps({
                'job_id': str(request.job_id),
                'url': request.url,
                'claim_token': str(request.claim_token),
                'scope_regex': request.scope_regex,
            }))
            project = Path(__file__).resolve().parents[2]
            environment = {
                'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'HOME': str(work),
                'LANG': 'C.UTF-8',
                'IS_CONTAINERIZED': 'true',
                'PYTHONPATH': str(project),
                'ELASTICSCAN_BASELINE_DIR': str(work),
            }
            command = [
                sys.executable,
                '/zap/zap-baseline.py',
                '--autooff',
                '-t', request.url,
                '-m', str(self.crawl_minutes),
                '-T', str(max(1, self.total_timeout_seconds // 60)),
                '-J', str(work / 'report.json'),
                '--hook', str(Path(__file__).with_name('baseline_hooks.py')),
                '-z',
                (
                    '-silent -host 127.0.0.1 '
                    '-config api.addrs.addr.name=(127[.]0[.]0[.]1|::1|0:0:0:0:0:0:0:1|zap) '
                    '-config api.addrs.addr.regex=true '
                    f'-dir {work / "zap-home"}'
                ),
            ]
            with (work / 'process.log').open('wb') as log:
                process = subprocess.Popen(
                    command,
                    cwd=work,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    return_code = process.wait(
                        timeout=self.total_timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError('ZAP overall timeout exceeded') from exc
                finally:
                    kill_group(process)

            report_path = work / 'report.json'
            evidence_path = work / 'evidence.json'
            evidence = (
                json.loads(evidence_path.read_text())
                if evidence_path.exists() else {}
            )
            return ScannerOutput(
                return_code=return_code,
                report=(
                    report_path.read_text()
                    if report_path.exists() else None
                ),
                passive_complete=evidence.get('passive_pending') == 0,
                active_scan_count=evidence.get('active_scan_count'),
                visited_urls=tuple(evidence.get('visited_urls', [])),
            )
