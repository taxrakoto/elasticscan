#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

docker run --rm --init --network none \
  --cap-add NET_ADMIN --user root \
  --mount "type=bind,src=$PWD,dst=/workspace,readonly" \
  --workdir /workspace \
  --entrypoint /bin/sh \
  elasticscan-zap-worker:local -ec '
    python3 -c "
import fcntl, socket, struct
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
    address = struct.pack(\"16sH2s4s8s\", b\"lo:fixture\", socket.AF_INET,
                          b\"\\0\" * 2, socket.inet_aton(\"11.254.254.10\"), b\"\\0\" * 8)
    fcntl.ioctl(control, 0x8916, address)  # Linux SIOCSIFADDR; local alias only.
"
    printf "\n11.254.254.10 approved.example.invalid outside.example.invalid\n" \
      >> /etc/hosts
    exec setpriv --reuid=zap --regid=zap --init-groups \
      env RUN_ZAP_BASELINE_TESTS=1 \
      python3 -B -m unittest tests.integration.test_baseline -v
  '
