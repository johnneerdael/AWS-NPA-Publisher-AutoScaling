#!/usr/bin/env bash
# Install on the EC2 HOST, never inside the publisher container.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: sudo bash scripts/install-thread-metrics.sh COLLECTOR_CONFIG.json" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Root is required to read publisher procfs and install the service." >&2
  exit 2
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert isinstance(v,dict) and v.get("executable") and v.get("group")' "$1"
install -d -m 0755 /opt/npa-thread-metrics
python3 -m venv /opt/npa-thread-metrics/venv
/opt/npa-thread-metrics/venv/bin/pip install -r "$script_dir/../requirements.txt"
install -m 0644 "$script_dir/publisher_metrics.py" /opt/npa-thread-metrics/publisher_metrics.py
install -m 0600 "$1" /etc/npa-thread-metrics.json
install -m 0644 "$script_dir/npa-thread-metrics.service" /etc/systemd/system/npa-thread-metrics.service
systemctl daemon-reload
systemctl enable npa-thread-metrics.service
systemctl restart npa-thread-metrics.service
