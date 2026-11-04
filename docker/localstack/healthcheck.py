"""Wait for this boot's successful ready hook, including after a restart."""
import json
from urllib.request import urlopen


try:
    with urlopen('http://localhost:4566/_localstack/init/ready', timeout=2) as response:
        state = json.load(response)
    successful = state['completed'] and any(
        script['name'] == 'init.sh' and script['state'] == 'SUCCESSFUL'
        for script in state['scripts']
    )
except (OSError, ValueError, KeyError):
    successful = False

raise SystemExit(0 if successful else 1)
