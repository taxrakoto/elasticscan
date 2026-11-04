"""Create development-only credentials without replacing an existing .env."""
from pathlib import Path
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    content = (ROOT / '.env.example').read_text()
    for name in ('POSTGRES_PASSWORD', 'KC_BOOTSTRAP_ADMIN_PASSWORD', 'LOCAL_USER_PASSWORD'):
        content = content.replace(name + '=\n', name + '=' + secrets.token_urlsafe(24) + '\n')
    try:
        fd = os.open(ROOT / '.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env already exists; left unchanged.')
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    print('Created .env with development-only credentials (mode 0600).')


if __name__ == '__main__':
    main()
