#!/usr/bin/env bash
# Move the reserved IP 'niftybot-ip' to a different VM in the SAME region:
#   bash deploy/gcp_move_ip_to_new_vm.sh NEW_INSTANCE NEW_ZONE
set -euo pipefail
NEW="${1:?usage: $0 NEW_INSTANCE NEW_ZONE}"; NZONE="${2:?usage: $0 NEW_INSTANCE NEW_ZONE}"
REGION="${NZONE%-*}"
IP="$(gcloud compute addresses describe niftybot-ip --region "$REGION" --format='get(address)')"
USER_URL="$(gcloud compute addresses describe niftybot-ip --region "$REGION" --format='value(users[0])')"
if [ -n "$USER_URL" ]; then
  OLD="${USER_URL##*/}"; OZONE="$(echo "$USER_URL" | sed -E 's#.*/zones/([^/]+)/.*#\1#')"
  if [ "$OLD" != "$NEW" ]; then
    echo ">> Detaching $IP from $OLD"
    gcloud compute instances delete-access-config "$OLD" --zone "$OZONE" --access-config-name "External NAT"
  fi
fi
gcloud compute instances delete-access-config "$NEW" --zone "$NZONE" --access-config-name "External NAT" 2>/dev/null || true
gcloud compute instances add-access-config "$NEW" --zone "$NZONE" --access-config-name "External NAT" --address "$IP"
gcloud compute instances add-tags "$NEW" --zone "$NZONE" --tags niftybot-web
echo "Done: $NEW now answers on http://$IP/  (run deploy/setup_gcp.sh on it and copy the data/ folder + .env)"
