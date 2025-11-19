# Alerting & Dashboard Setup Guide

This guide explains how to configure Prometheus Alertmanager with Pushover notifications and manage Grafana dashboards.

## Table of Contents

- [Pushover Setup](#pushover-setup)
- [Alert Configuration](#alert-configuration)
- [Available Alerts](#available-alerts)
- [Dashboard Management](#dashboard-management)
- [Testing Alerts](#testing-alerts)
- [Troubleshooting](#troubleshooting)

---

## Pushover Setup

Pushover provides instant push notifications to your phone/desktop for critical alerts.

### 1. Create Pushover Account

1. Go to [pushover.net](https://pushover.net/) and sign up
2. Download the Pushover app on your phone (iOS/Android)
3. Note your **User Key** (shown on the dashboard)

### 2. Create Application Token

1. In Pushover dashboard, click "Create an Application/API Token"
2. Fill in:
   - **Name**: Revel Alerts
   - **Type**: Application
   - **Description**: Production alerts for Revel application
3. Submit and note the **API Token/Key**

### 3. Configure Environment Variables

Add to your `.env` file:

```bash
# Pushover credentials
PUSHOVER_USER_KEY=your_user_key_from_step_1
PUSHOVER_APP_TOKEN=your_app_token_from_step_2
```

### 4. Restart Alertmanager

```bash
docker-compose restart alertmanager
```

---

## Alert Configuration

### Alert Severities

Alerts are categorized into three severity levels:

| Severity | Priority | Behavior | Use Case |
|----------|----------|----------|----------|
| **critical** | Emergency (2) | Bypasses quiet hours, requires acknowledgment, retries every 60s | Service down, database down, disk full |
| **warning** | High (1) | Bypasses quiet hours, normal notification | High error rates, resource pressure, slow performance |
| **info** | Normal (0) | Respects quiet hours | Informational events, high traffic |

### Alert Grouping

Alerts are grouped by:
- `alertname` - Type of alert
- `cluster` - Deployment cluster
- `service` - Affected service

This reduces notification spam by bundling related alerts.

### Notification Timing

- **Critical**: Sent after 10s, repeats every 30m
- **Warning**: Sent after 30s, repeats every 2h
- **Info**: Sent after 30s, repeats every 12h

---

## Available Alerts

### Infrastructure Alerts (`alerts/infrastructure.yml`)

#### Critical
- **ServiceDown** - Any monitored service is unreachable
- **PostgresDown** - Database is down
- **DiskSpaceCritical** - Less than 5% disk space remaining

#### Warning
- **HighCPUUsage** - CPU above 90% for 5 minutes
- **HighMemoryUsage** - Memory above 90% for 5 minutes
- **DiskSpaceLow** - Less than 10% disk space remaining
- **HighDatabaseConnections** - Above 80% of max_connections
- **SlowQueries** - Database queries averaging >1 second
- **RedisDown** - Redis cache is down
- **RedisMemoryHigh** - Redis memory above 90%
- **RedisConnectionsHigh** - More than 100 Redis connections
- **RedisHitRateLow** - Cache hit rate below 50%
- **LokiDown** - Log aggregation service down
- **TempoDown** - Tracing service down
- **PrometheusStorageLow** - Prometheus storage above 85%

### Application Alerts (`alerts/application.yml`)

#### Critical
- **ApplicationDown** - Health check endpoint failing
- **HighHTTPErrorRate** - 5xx errors above 5%
- **CeleryWorkerDown** - Background task worker down

#### Warning
- **HighHTTP4xxRate** - 4xx errors above 20%
- **SlowHTTPRequests** - 95th percentile latency above 2s
- **HighCeleryQueueLength** - More than 100 pending tasks
- **HighCeleryTaskFailureRate** - Task failure rate above 10%
- **SlowCeleryTasks** - Average task runtime above 60s
- **ClamAVDown** - Antivirus scanning disabled
- **HighAuthenticationFailureRate** - Possible brute force attack
- **HighExternalAPIErrorRate** - External API integration issues
- **TelegramBotDown** - Telegram bot service down

#### Info
- **LowActiveUsers** - Less than 100 requests in past hour
- **HighUserRegistrationRate** - Unusual signup activity
- **HighRequestRate** - Traffic above 1000 req/s

---

## Alert Customization

### Modifying Alert Thresholds

Edit alert files in `observability/alerts/`:

```yaml
- alert: HighHTTPErrorRate
  expr: (sum(rate(django_http_responses_total_by_status_total{status=~"5.."}[5m])) / sum(rate(django_http_responses_total_by_status_total[5m]))) * 100 > 5  # Change threshold here
  for: 5m  # Change duration here
  labels:
    severity: critical  # Change severity here
```

### Adding New Alerts

Create a new alert rule:

```yaml
- alert: YourCustomAlert
  expr: your_prometheus_query > threshold
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Brief description"
    description: "Detailed description with {{ $value }}"
```

Then reload Prometheus:

```bash
docker-compose exec prometheus kill -HUP 1
```

### Inhibition Rules

Alertmanager includes inhibition rules to prevent alert fatigue:

- When **ServiceDown** fires, suppress **high latency** warnings
- When **PostgresDown** fires, suppress **connection** warnings

Add more in `observability/alertmanager-config.yml`:

```yaml
inhibit_rules:
  - source_match:
      severity: 'critical'
      alertname: 'YourCriticalAlert'
    target_match:
      severity: 'warning'
    equal: ['service']
```

---

## Dashboard Management

### Accessing Dashboards

Grafana is available at: `https://grafana.letsrevel.io`

Default credentials (change in `.env`):
- Username: `${GRAFANA_ADMIN_USER}`
- Password: `${GRAFANA_ADMIN_PASSWORD}`

### Pre-configured Dashboards

1. **System Overview** (`system-overview.json`)
   - Service health status (Web, PostgreSQL, Redis)
   - Active alerts count
   - HTTP request rate
   - Response status distribution

More dashboards can be added to `observability/dashboards/` directory.

### Creating Custom Dashboards

#### Method 1: Grafana UI

1. Create dashboard in Grafana UI
2. Click "Dashboard settings" (gear icon)
3. Select "JSON Model"
4. Copy JSON
5. Save to `observability/dashboards/<name>.json`
6. Auto-loaded within 30 seconds

#### Method 2: Import from Grafana.com

1. Browse [grafana.com/grafana/dashboards](https://grafana.com/grafana/dashboards/)
2. Find dashboard (e.g., "Django" or "PostgreSQL")
3. Note the dashboard ID
4. In Grafana UI: Dashboards → Import → Enter ID
5. Configure datasources (use "Prometheus")
6. Export as JSON and save to `observability/dashboards/`

### Recommended Community Dashboards

- **PostgreSQL Database**: Dashboard ID `9628`
- **Redis**: Dashboard ID `763`
- **Node Exporter Full**: Dashboard ID `1860` (if you add node exporter)
- **Django Application**: Dashboard ID `12544`

### Dashboard Provisioning

Dashboards are automatically provisioned on startup via:
- `grafana-dashboard-provisioning.yaml` - Provisioning config
- `dashboards/` directory - Dashboard JSON files

Settings:
- `allowUiUpdates: true` - Can edit dashboards in UI
- `updateIntervalSeconds: 30` - Checks for changes every 30s
- `foldersFromFilesStructure: true` - Organizes by directory structure

---

## Testing Alerts

### Test Alert Configuration

Check Alertmanager config is valid:

```bash
docker-compose exec alertmanager amtool check-config /etc/alertmanager/alertmanager.yml
```

### Send Test Alert

```bash
# Send test alert to Alertmanager
docker-compose exec prometheus promtool test rules /etc/prometheus/alerts/*.yml
```

### Verify Pushover Integration

Trigger a test notification:

```bash
curl -X POST https://api.pushover.net/1/messages.json \
  -d "token=YOUR_APP_TOKEN" \
  -d "user=YOUR_USER_KEY" \
  -d "message=Test alert from Revel" \
  -d "title=Test Alert"
```

### View Active Alerts

- **Prometheus**: `http://localhost:9090/alerts`
- **Alertmanager**: `http://localhost:9093/#/alerts`
- **Grafana**: Built-in alerting page

### Silence Alerts

During maintenance windows:

```bash
# Silence alerts for 2 hours
docker-compose exec alertmanager amtool silence add alertname="HighCPUUsage" --duration=2h --comment="Planned maintenance"
```

---

## Troubleshooting

### Alerts Not Firing

1. **Check Prometheus is evaluating rules**:
   ```bash
   docker-compose logs prometheus | grep -i "rule evaluation"
   ```

2. **Verify alert rules loaded**:
   ```bash
   curl http://localhost:9090/api/v1/rules
   ```

3. **Check alert is firing**:
   ```bash
   curl http://localhost:9090/api/v1/alerts
   ```

### Pushover Notifications Not Received

1. **Check Alertmanager logs**:
   ```bash
   docker-compose logs alertmanager | grep -i pushover
   ```

2. **Verify environment variables**:
   ```bash
   docker-compose exec alertmanager env | grep PUSHOVER
   ```

3. **Test Pushover credentials**:
   ```bash
   curl -X POST https://api.pushover.net/1/users/validate.json \
     -d "token=YOUR_APP_TOKEN" \
     -d "user=YOUR_USER_KEY"
   ```

4. **Check Pushover app settings**: Ensure notifications are enabled

### Alert Grouping Issues

View current alert groups in Alertmanager:

```bash
curl http://localhost:9093/api/v2/alerts/groups
```

Adjust grouping in `alertmanager-config.yml`:

```yaml
route:
  group_by: ['alertname', 'cluster']  # Modify as needed
  group_wait: 30s
  group_interval: 5m
```

### Dashboards Not Loading

1. **Check dashboard provisioning logs**:
   ```bash
   docker-compose logs grafana | grep -i dashboard
   ```

2. **Verify JSON syntax**:
   ```bash
   cat observability/dashboards/system-overview.json | jq .
   ```

3. **Check file permissions**:
   ```bash
   ls -la observability/dashboards/
   ```

### Datasource Connection Issues

Test datasource connectivity:

```bash
# From Grafana container
docker-compose exec grafana curl http://prometheus:9090/-/healthy
docker-compose exec grafana curl http://loki:3100/ready
docker-compose exec grafana curl http://tempo:3200/ready
```

---

## Resource Usage

Additional resources for alerting and dashboards:

| Service | Memory | CPU | Purpose |
|---------|--------|-----|---------|
| alertmanager | 256MB | 0.5 | Alert routing and Pushover integration |
| blackbox-exporter | 128MB | 0.25 | Health check probing |

Total additional: **384MB RAM, 0.75 CPUs** (~1.2% RAM, 9.4% CPU on CCX33)

---

## Security Notes

- Pushover tokens in `.env` are mounted as environment variables
- Alertmanager is only accessible within Docker network
- Consider using secrets management for production (Docker secrets, Vault)
- Grafana admin credentials should be rotated regularly

---

## Next Steps

1. ✅ Configure Pushover credentials in `.env`
2. ✅ Test alert notifications
3. ✅ Customize alert thresholds for your traffic patterns
4. ✅ Create custom Grafana dashboards
5. ✅ Set up silence rules for maintenance windows
6. ✅ Document on-call procedures

For more information, see:
- [Prometheus Alerting](https://prometheus.io/docs/alerting/latest/overview/)
- [Pushover API](https://pushover.net/api)
- [Grafana Dashboards](https://grafana.com/docs/grafana/latest/dashboards/)
