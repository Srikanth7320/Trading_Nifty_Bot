#!/usr/bin/env bash
# Run from Cloud Shell or your PC (gcloud installed & logged in), NOT on the VM:
#   bash deploy/gcp_static_ip_and_firewall.sh INSTANCE_NAME ZONE [ALLOWED_SOURCE_CIDR]
# 1) Promotes the VM's CURRENT external IP to a reserved static IP (the address does not change).
# 2) Opens tcp:80,443 only for VMs tagged niftybot-web (restrict with a CIDR, e.g. 203.0.113.7/32).
set -euo pipefail
INSTANCE="${1:?usage: $0 INSTANCE ZONE [CIDR]}"; ZONE="${2:?usage: $0 INSTANCE ZONE [CIDR]}"; SRC="${3:-0.0.0.0/0}"
REGION="${ZONE%-*}"
IP="$(gcloud compute instances describe "$INSTANCE" --zone "$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
[ -n "$IP" ] || { echo "The VM has no external IP."; exit 1; }
if gcloud compute addresses describe niftybot-ip --region "$REGION" >/dev/null 2>&1; then
  echo ">> Static address niftybot-ip already exists: $(gcloud compute addresses describe niftybot-ip --region "$REGION" --format='get(address)')"
else
  gcloud compute addresses create niftybot-ip --addresses "$IP" --region "$REGION"
  echo ">> Reserved $IP as static address 'niftybot-ip' (region $REGION)"
fi
gcloud compute instances add-tags "$INSTANCE" --zone "$ZONE" --tags niftybot-web
gcloud compute firewall-rules describe niftybot-web >/dev/null 2>&1 || \
  gcloud compute firewall-rules create niftybot-web --allow tcp:80,tcp:443 --target-tags niftybot-web --source-ranges "$SRC" \
    --description "NiftyBot dashboard"
echo
echo "Dashboard: http://$(gcloud compute addresses describe niftybot-ip --region "$REGION" --format='get(address)')/"
