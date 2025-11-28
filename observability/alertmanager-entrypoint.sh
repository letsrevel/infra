#!/bin/sh
set -e

# Process the config template to replace environment variables
sed -e "s|\${PUSHOVER_USER_KEY}|${PUSHOVER_USER_KEY}|g" \
    -e "s|\${PUSHOVER_APP_TOKEN}|${PUSHOVER_APP_TOKEN}|g" \
    /etc/alertmanager/alertmanager.yml.template > /etc/alertmanager/alertmanager.yml

# Start alertmanager with the processed config
exec /bin/alertmanager "$@"
