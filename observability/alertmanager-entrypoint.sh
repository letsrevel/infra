#!/bin/sh
set -e

# Process the config template to replace environment variables
envsubst < /etc/alertmanager/alertmanager.yml.template > /etc/alertmanager/alertmanager.yml

# Start alertmanager with the processed config
exec /bin/alertmanager "$@"
