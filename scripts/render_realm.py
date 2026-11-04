"""Inject runtime-only fixture passwords into the reproducible realm definition."""
import json
import os
from pathlib import Path


def main():
    realm = json.loads(Path('/app/docker/keycloak/realm.json').read_text())
    password = os.environ['LOCAL_USER_PASSWORD']
    if not password:
        raise SystemExit('LOCAL_USER_PASSWORD must be set')
    for user in realm['users']:
        user['credentials'] = [{'type': 'password', 'value': password, 'temporary': False}]
    target = Path('/realm/elasticscan-realm.json')
    target.write_text(json.dumps(realm))
    target.chmod(0o644)  # Shared only with Keycloak on a Docker-managed volume.


if __name__ == '__main__':
    main()
