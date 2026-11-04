"""Print a development fixture access token; requires the local stack."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username', choices=('alice', 'alice-peer', 'bob'))
    args = parser.parse_args()
    values = dict(line.split('=', 1) for line in (Path(__file__).resolve().parents[1] / '.env').read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    data = urlencode({'grant_type': 'password', 'client_id': 'elasticscan-dev',
                      'username': args.username, 'password': values['LOCAL_USER_PASSWORD']}).encode()
    with urlopen(Request('http://localhost:8080/realms/elasticscan/protocol/openid-connect/token', data=data), timeout=10) as response:
        print(json.load(response)['access_token'])


if __name__ == '__main__':
    main()
