#!/usr/bin/env bash
# Remove everything the qualification created and audit for leftovers. Always runs.
set -uo pipefail
OUT=${1:-evidence}; mkdir -p "$OUT"
docker ps -aq --filter label=poofq=1 | xargs -r docker rm -f > /dev/null
docker images --filter label=poofq=1 -q | xargs -r docker rmi -f > /dev/null 2>&1
docker network rm poofq > /dev/null 2>&1
umount /mnt/poofq-data 2>/dev/null; rm -f /opt/poofq-data.img
{
  echo "containers: $(docker ps -aq --filter label=poofq=1 | wc -l)"
  echo "network: $(docker network ls -q --filter name=^poofq$ | wc -l)"
  echo "data mount: $(mount | grep -c /mnt/poofq-data)"
} | tee "$OUT/teardown-audit.txt"
