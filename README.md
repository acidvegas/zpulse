# ZPulse

Real-time ZFS & disk monitoring for home server racks. Built to watch over multiple nodes with dozens of drives, streaming SMART health, ZFS pool status, I/O rates, and temperatures to a single dashboard over WebSocket. Alerts go to Gotify.


## Why

Every time I looked for a way to keep tabs on disk health across all the nodes in my home rack, the answer was always the same stack: Grafana, Telegraf, Prometheus, maybe throw InfluxDB in there too. That is an absurd amount of infrastructure just to answer "are my drives dying?" I didn't need time-series databases and query languages and dashboarding frameworks. I needed something that tells me if a disk is getting hot, if a ZFS pool is degraded, or if SMART errors are creeping up, across every machine, in one place.

Nothing out there was built for this. Everything either does way too much or only monitors the local machine. So I wrote ZPulse. It is purpose-built for home racks: lightweight agents that stream disk and ZFS telemetry over a single WebSocket connection to one central dashboard. No metric pipelines, no config files longer than the code itself, no containers, no databases. Just a Python agent on each node and a dashboard on whatever box you have lying around.


## Preview

###### Overview

![](./.screens/preview.png)

###### Individual Node Monitoring

![](./.screens/preview2.png)

###### SMART Metadata parsing

![](./.screens/preview3.png)


## Architecture

```
[Server 1] agent.py ──WebSocket──┐
[Server 2] agent.py ──WebSocket──┼──> [Central Box] dashboard.py <──WS──> [Browser]
[Server N] agent.py ──WebSocket──┘
```

- `agent.py` runs on each server as root, collects disk/ZFS/SMART/I/O data, & streams it to the dashboard
- `dashboard.py` runs on a central machine *(Raspberry Pi, NUC, whatever)*, aggregates data from all agents, & serves the web UI
- All data flows over persistent WebSocket connections, no polling


## Dashboard Setup

Installs to `/opt/zpulse-dashboard`. No root required at runtime, just for the setup itself.

```bash
sudo ./dashboard/setup.sh
```

This installs dependencies, creates a venv, fetches Chart.js, and sets up a systemd service. The dashboard listens on port 8888 by default.

To run manually instead:

```bash
cd dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 dashboard.py
```

| Flag            | Default   | Description    |
|-----------------|-----------|----------------|
| `-h`, `--host`  | `0.0.0.0` | Listen address |
| `-p`, `--port`  | `8888`    | Listen port    |
| `-d`, `--debug` | off       | Debug logging  |


## Agent Setup

Installs to `/opt/zpulse-agent`. Must run as root for SMART data & ZFS access.

```bash
sudo ./agent/setup.sh ws://DASHBOARD_IP:8888/ws/agent
```

This installs `smartmontools` and `zfsutils-linux`, creates a venv, and sets up a systemd service that auto-starts and reconnects.

To run manually instead:

```bash
cd agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo ./venv/bin/python agent.py ws://DASHBOARD_IP:8888/ws/agent
```


## Gotify Notifications

Open the dashboard in a browser, click Settings. Enter your Gotify server URL and app token, hit Test, then Save. Alert thresholds for temperature, space usage, SMART failures, and pool health are all configured from the same panel.


## What It Monitors

- Fleet overview with all connected servers, health status, storage usage, alert counts
- Per-server detail view:
  - System info *(kernel, ZFS version, uptime, RAM, CPU)*
  - ZFS pools *(size, allocated, free, fragmentation, dedup, scrub age, vdev tree, errors)*
  - ZFS datasets *(used, available, referenced, compression ratio, quotas)*
  - Snapshots *(name, used, referenced, creation time)*
  - Live I/O charts *(per-disk throughput, IOPS, read/write latency, busy%)*
  - ARC cache stats *(size, hit rate, MRU/MFU, L2ARC)*
  - Temperature charts *(per-disk, live)*
  - Disk details *(model, serial, firmware, capacity, RPM, protocol, health score 0-100, full SMART attributes, SAS error counters, grown defects)*
  - SMART self-test triggering from the UI
  - Alerts pushed to Gotify with configurable cooldowns