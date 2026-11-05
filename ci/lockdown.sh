#!/usr/bin/env bash
# Runner lockdown. Runs once, as root, BEFORE any untrusted image is pulled or started.
# - containers live on an internal Docker network: no route to the internet, the runner, or cloud metadata
# - every packet from that network to the runner itself is dropped (INPUT)
# - forwarding out of the network is dropped except between containers on it (DOCKER-USER)
# - app data goes on a dedicated, size-capped loopback ext4 filesystem (nosuid,nodev)
set -euo pipefail
NET=poofq; SUBNET=172.31.250.0/24
docker network create --internal --subnet "$SUBNET" "$NET" > /dev/null
BR="br-$(docker network inspect -f '{{.Id}}' "$NET" | cut -c1-12)"
iptables -I INPUT 1 -i "$BR" -j DROP
iptables -I DOCKER-USER 1 -i "$BR" -j DROP
iptables -I DOCKER-USER 1 -i "$BR" -o "$BR" -j ACCEPT
iptables -I DOCKER-USER 1 -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER 1 -d 168.63.129.16 -j DROP
DATA_MB=${POOFQ_DATA_MB:-2048}
fallocate -l "${DATA_MB}M" /opt/poofq-data.img
mkfs.ext4 -q -F /opt/poofq-data.img
mkdir -p /mnt/poofq-data
mount -o loop,nosuid,nodev /opt/poofq-data.img /mnt/poofq-data
chmod 0777 /mnt/poofq-data
echo "lockdown: network $NET ($SUBNET) on $BR; data cap ${DATA_MB} MiB at /mnt/poofq-data"
