# Grafana Dashboards

This directory contains pre-configured Grafana dashboards for monitoring the Revel application.

## Available Dashboards

1. **system-overview.json** - High-level system health overview
   - Service uptime
   - Request rate and error rates
   - Resource utilization
   - Active alerts

2. **application-metrics.json** - Detailed application performance
   - HTTP request metrics
   - Response time percentiles
   - Error breakdown by status code
   - Celery task metrics

3. **infrastructure-metrics.json** - Infrastructure monitoring
   - PostgreSQL metrics
   - Redis performance
   - Resource usage (CPU, memory, disk)
   - Network I/O

## Creating Custom Dashboards

You can create custom dashboards in Grafana UI and export them:

1. Create dashboard in Grafana
2. Click "Dashboard settings" (gear icon)
3. Select "JSON Model" from left sidebar
4. Copy the JSON
5. Save to this directory as `<name>.json`
6. Grafana will auto-load it within 30 seconds

## Dashboard Variables

All dashboards use the following variables:
- `$datasource` - Prometheus datasource
- `$interval` - Time interval for aggregation (auto)
- `$environment` - Deployment environment (from Prometheus labels)

## Notes

- Dashboards are automatically provisioned on Grafana startup
- Changes to JSON files are picked up within 30 seconds
- Set `allowUiUpdates: true` in provisioning config to allow UI edits
