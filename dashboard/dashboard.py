#!/usr/bin/env python3
# ZPulse - Developed by acidvegas in Python (https://github.com/acidvegas/rackwatch)
# zpulse/dashboard/dashboard.py

import argparse
import asyncio
import json
import logging
import os
import tempfile
import time

from collections import deque
from pathlib     import Path

try:
	import aiohttp.web
except ImportError:
	raise ImportError('missing aiohttp module (pip install aiohttp)')

try:
	import apv
except ImportError:
	raise ImportError('missing apv module (pip install apv)')

# ── Configuration ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / 'settings.json'
HISTORY_SIZE  = 720

DEFAULT_SETTINGS = {
	'gotify_url'           : '',
	'gotify_token'         : '',
	'alert_temp_warning'   : 45,
	'alert_temp_critical'  : 55,
	'alert_space_warning'  : 80,
	'alert_space_critical' : 90,
	'alert_smart_enabled'  : True,
	'alert_pool_enabled'   : True,
	'alert_cooldown'       : 3600,
}

CRITICAL_SMART_ATTRS = {5, 10, 187, 188, 196, 197, 198, 199}

settings = dict(DEFAULT_SETTINGS)

# ── Per-Agent State ──────────────────────────────────────────────────────────

class AgentState:
	__slots__ = ('hostname', 'ws', 'online', 'last_seen', 'capabilities', 'current', 'history', 'alerts_active', 'alert_log', 'alert_cooldowns')

	def __init__(self, hostname: str, ws):
		'''Initialize state for a newly connected agent.

		:param hostname: Agent's hostname
		:param ws: WebSocket connection to the agent
		'''

		self.hostname        = hostname
		self.ws              = ws
		self.online          = True
		self.last_seen       = time.time()
		self.capabilities    = {}
		self.current         = {}
		self.history         = {
			'timestamps'   : deque(maxlen=HISTORY_SIZE),
			'io'           : {},
		}
		self.alerts_active   = []
		self.alert_log       = deque(maxlen=200)
		self.alert_cooldowns = {}


agents      = {}   # hostname -> AgentState
browser_subs = {}  # ws -> subscribed hostname (or None)
gotify_session = None


# ── Settings ─────────────────────────────────────────────────────────────────

def load_settings():
	'''Load settings from disk, merging with defaults for any missing keys.'''

	global settings
	if SETTINGS_FILE.exists():
		try:
			with open(SETTINGS_FILE) as f:
				saved = json.load(f)
			merged = dict(DEFAULT_SETTINGS)
			merged.update(saved)
			settings = merged
		except Exception as e:
			logging.warning('Failed to load settings: %s', e)


def save_settings():
	'''Atomically write current settings to disk using a temp file and rename.'''

	try:
		fd, tmp = tempfile.mkstemp(dir=str(BASE_DIR), suffix='.json.tmp')
		with os.fdopen(fd, 'w') as f:
			json.dump(settings, f, indent=2)
		os.replace(tmp, str(SETTINGS_FILE))
	except Exception as e:
		logging.error('Failed to save settings: %s', e)


# ── Gotify ───────────────────────────────────────────────────────────────────

async def send_gotify(title: str, message: str, priority: int = 5):
	'''Send a push notification via the configured Gotify server.

	:param title: Notification title
	:param message: Notification body
	:param priority: Gotify priority level (default 5)
	'''

	global gotify_session
	url   = settings.get('gotify_url', '').rstrip('/')
	token = settings.get('gotify_token', '')
	if not url or not token:
		return False
	try:
		if gotify_session is None:
			gotify_session = aiohttp.ClientSession()
		async with gotify_session.post(
			f'{url}/message?token={token}',
			json={'title': title, 'message': message, 'priority': priority},
			timeout=aiohttp.ClientTimeout(total=10),
		) as resp:
			return resp.status == 200
	except Exception as e:
		logging.warning('Gotify failed: %s', e)
		return False


# ── Alerts ───────────────────────────────────────────────────────────────────

def _should_alert(agent: 'AgentState', alert_type: str, target: str):
	'''Check if an alert should fire based on cooldown period.

	:param agent: Agent state to check cooldowns against
	:param alert_type: Category of alert (e.g. disk_temp_crit, pool_health)
	:param target: Specific target name (e.g. sda, tank)
	'''

	key = f'{alert_type}:{target}'
	now = time.time()
	cooldown = settings.get('alert_cooldown', 3600)
	if now - agent.alert_cooldowns.get(key, 0) < cooldown:
		return False
	agent.alert_cooldowns[key] = now
	return True


async def _emit_alert(agent: 'AgentState', alert_type: str, severity: str, target: str, message: str):
	'''Log an alert and send a Gotify notification.

	:param agent: Agent state to append the alert to
	:param alert_type: Category of alert
	:param severity: One of info, warning, critical
	:param target: Specific target name (e.g. sda, tank)
	:param message: Human-readable alert message
	'''

	entry = {'type': alert_type, 'severity': severity, 'target': target,
			 'message': message, 'timestamp': time.time()}
	agent.alert_log.appendleft(entry)
	priority = {'info': 3, 'warning': 5, 'critical': 8}.get(severity, 5)
	asyncio.create_task(send_gotify(f'[{severity.upper()}] {agent.hostname}/{target}', message, priority))


async def check_alerts(agent: 'AgentState'):
	'''Evaluate disk and pool data against alert thresholds and emit notifications.

	:param agent: Agent state containing current disk and pool data
	'''

	active = []
	disks_msg = agent.current.get('disks', {})
	disks = disks_msg.get('disks', []) if isinstance(disks_msg, dict) else []
	pools_msg = agent.current.get('pools', {})
	pools = pools_msg.get('pools', []) if isinstance(pools_msg, dict) else []

	if settings.get('alert_smart_enabled', True):
		for d in disks:
			name = d.get('name', '')
			temp = d.get('temperature')
			if temp is not None:
				if temp >= settings.get('alert_temp_critical', 55):
					a = {'type': 'disk_temp', 'severity': 'critical', 'target': name, 'message': f'Disk {name} temperature is {temp}°C (critical)'}
					active.append(a)
					if _should_alert(agent, 'disk_temp_crit', name):
						await _emit_alert(agent, 'disk_temp', 'critical', name, a['message'])
				elif temp >= settings.get('alert_temp_warning', 45):
					a = {'type': 'disk_temp', 'severity': 'warning', 'target': name, 'message': f'Disk {name} temperature is {temp}°C (warning)'}
					active.append(a)
					if _should_alert(agent, 'disk_temp_warn', name):
						await _emit_alert(agent, 'disk_temp', 'warning', name, a['message'])
			if d.get('health') is False:
				a = {'type': 'disk_health', 'severity': 'critical', 'target': name, 'message': f'Disk {name} SMART health check FAILED'}
				active.append(a)
				if _should_alert(agent, 'disk_health', name):
					await _emit_alert(agent, 'disk_health', 'critical', name, a['message'])
			for attr in d.get('smart_attributes', []):
				if attr.get('id') in CRITICAL_SMART_ATTRS and attr.get('when_failed'):
					a = {'type': 'smart_attr', 'severity': 'warning', 'target': name, 'message': f'Disk {name}: {attr["name"]} failing'}
					active.append(a)
					if _should_alert(agent, f'smart_attr_{attr["id"]}', name):
						await _emit_alert(agent, 'smart_attr', 'warning', name, a['message'])

	if settings.get('alert_pool_enabled', True):
		for p in pools:
			pname = p.get('name', '')
			if p.get('health') not in ('ONLINE', ''):
				sev = 'critical' if p['health'] == 'FAULTED' else 'warning'
				a = {'type': 'pool_health', 'severity': sev, 'target': pname, 'message': f'Pool {pname} is {p["health"]}'}
				active.append(a)
				if _should_alert(agent, 'pool_health', pname):
					await _emit_alert(agent, 'pool_health', sev, pname, a['message'])
			cap = p.get('capacity_pct', 0)
			if cap >= settings.get('alert_space_critical', 90):
				a = {'type': 'pool_space', 'severity': 'critical', 'target': pname, 'message': f'Pool {pname} is {cap}% full'}
				active.append(a)
				if _should_alert(agent, 'pool_space_crit', pname):
					await _emit_alert(agent, 'pool_space', 'critical', pname, a['message'])
			elif cap >= settings.get('alert_space_warning', 80):
				a = {'type': 'pool_space', 'severity': 'warning', 'target': pname, 'message': f'Pool {pname} is {cap}% full'}
				active.append(a)
				if _should_alert(agent, 'pool_space_warn', pname):
					await _emit_alert(agent, 'pool_space', 'warning', pname, a['message'])

	agent.alerts_active = active


# ── History ──────────────────────────────────────────────────────────────────

def update_history(agent: 'AgentState', data: dict):
	'''Append incoming I/O, temperature, and ARC data to the agent's rolling history.

	:param agent: Agent state containing history deques
	:param data: Incoming message dict from the agent
	'''

	msg_type = data['type']

	if msg_type == 'io':
		ts = data.get('ts', time.time())
		agent.history['timestamps'].append(ts)

		for dname, rates in data.get('rates', {}).items():
			if dname not in agent.history['io']:
				agent.history['io'][dname] = {
					'read_bps'  : deque(maxlen=HISTORY_SIZE),
					'write_bps' : deque(maxlen=HISTORY_SIZE),
					'read_iops' : deque(maxlen=HISTORY_SIZE),
					'write_iops': deque(maxlen=HISTORY_SIZE),
				}
			h = agent.history['io'][dname]
			h['read_bps'].append(rates.get('read_bps', 0))
			h['write_bps'].append(rates.get('write_bps', 0))
			h['read_iops'].append(rates.get('read_iops', 0))
			h['write_iops'].append(rates.get('write_iops', 0))


def serialize_history(h: dict):
	'''Convert history deques to plain lists for JSON serialization.

	:param h: History dict containing deques
	'''

	return {
		'timestamps'   : list(h['timestamps']),
		'io'           : {dn: {k: list(v) for k, v in s.items()} for dn, s in h['io'].items()},
	}


# ── Server List ──────────────────────────────────────────────────────────────

def get_server_list():
	'''Build a summary list of all known agents for the fleet overview.'''

	out = []
	for hn, a in agents.items():
		disks_msg = a.current.get('disks', {})
		disks = disks_msg.get('disks', []) if isinstance(disks_msg, dict) else []
		pools_msg = a.current.get('pools', {})
		pools = pools_msg.get('pools', []) if isinstance(pools_msg, dict) else []
		sys_msg = a.current.get('system', {})
		sys_info = sys_msg.get('info', {}) if isinstance(sys_msg, dict) else {}
		logs_msg = a.current.get('logs', {})
		log_errors = logs_msg.get('errors', []) if isinstance(logs_msg, dict) else []

		bad_pools    = [p for p in pools if p.get('health') not in ('ONLINE', '', None)]
		failed_disks = sum(1 for d in disks if d.get('health') is False)
		crit_alerts  = sum(1 for al in a.alerts_active if al.get('severity') == 'critical')
		pool_states  = {p.get('health') for p in pools}
		worst_pool   = ('FAULTED' if 'FAULTED' in pool_states else 'DEGRADED' if 'DEGRADED' in pool_states
		                else 'ONLINE' if pools else '')

		out.append({
			'hostname'        : hn,
			'online'          : a.online,
			'last_seen'       : a.last_seen,
			'disk_count'      : len(disks),
			'pool_count'      : len(pools),
			'alert_count'     : len(a.alerts_active),
			'crit_alert_count': crit_alerts,
			'degraded'        : bool(bad_pools) or failed_disks > 0,
			'failed_disks'    : failed_disks,
			'pool_status'     : worst_pool,
			'log_error_count' : len(log_errors),
			'total_raw'       : sum(d.get('size', 0) for d in disks),
			'total_usable'    : sum(p.get('size', 0) for p in pools),
			'total_used'      : sum(p.get('allocated', 0) for p in pools),
			'cpu_model'       : sys_info.get('cpu_model', ''),
			'ram_total'       : sys_info.get('ram_total', 0),
			'ram_available'   : sys_info.get('ram_available', 0),
			'uptime_seconds'  : sys_info.get('uptime_seconds', 0),
		})
	return out


# ── WebSocket: Agents ────────────────────────────────────────────────────────

async def agent_ws_handler(request: aiohttp.web.Request):
	'''Handle WebSocket connections from monitoring agents.

	:param request: Incoming aiohttp request
	'''

	ws = aiohttp.web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
	await ws.prepare(request)

	hostname = None
	try:
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.TEXT:
				try:
					data = json.loads(msg.data)
				except json.JSONDecodeError:
					continue

				if data.get('type') == 'hello':
					hostname = data.get('hostname', 'unknown')
					if hostname in agents:
						agents[hostname].ws = ws
						agents[hostname].online = True
						agents[hostname].last_seen = time.time()
						agents[hostname].capabilities = data.get('capabilities', {})
					else:
						agents[hostname] = AgentState(hostname, ws)
						agents[hostname].capabilities = data.get('capabilities', {})
					logging.info('Agent connected: %s', hostname)
					await broadcast_server_list()
					continue

				if data.get('type') == 'smarttest_result' and hostname:
					await forward_to_browsers(hostname, data)
					continue

				if hostname and hostname in agents:
					a = agents[hostname]
					a.last_seen = time.time()
					a.current[data['type']] = data
					update_history(a, data)

					if data['type'] in ('disks', 'pools'):
						await check_alerts(a)

					await forward_to_browsers(hostname, data)

			elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
				break
	finally:
		if hostname and hostname in agents:
			agents[hostname].online = False
			agents[hostname].ws = None
			logging.info('Agent disconnected: %s', hostname)
			await broadcast_server_list()

	return ws


# ── WebSocket: Browsers ─────────────────────────────────────────────────────

async def browser_ws_handler(request: aiohttp.web.Request):
	'''Handle WebSocket connections from browser clients.

	:param request: Incoming aiohttp request
	'''

	ws = aiohttp.web.WebSocketResponse(heartbeat=30)
	await ws.prepare(request)
	browser_subs[ws] = None

	try:
		await ws.send_json({'type': 'servers', 'servers': get_server_list()})
		await ws.send_json({'type': 'settings', 'settings': settings})

		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.TEXT:
				try:
					data = json.loads(msg.data)
				except json.JSONDecodeError:
					continue

				cmd = data.get('type')

				if cmd == 'subscribe':
					hn = data.get('hostname')
					browser_subs[ws] = hn
					if hn and hn in agents:
						a = agents[hn]
						await ws.send_json({
							'type'    : 'full_state',
							'hostname': hn,
							'online'  : a.online,
							'current' : a.current,
							'history' : serialize_history(a.history),
							'alerts'  : {'active': a.alerts_active, 'log': list(a.alert_log)},
						})
					else:
						await ws.send_json({
							'type': 'full_state', 'hostname': hn or '',
							'online': False, 'current': {}, 'history': {},
							'alerts': {'active': [], 'log': []},
						})

				elif cmd == 'smarttest':
					hn = data.get('hostname')
					if hn and hn in agents and agents[hn].online and agents[hn].ws:
						try:
							await agents[hn].ws.send_str(json.dumps({
								'type': 'smarttest',
								'device': data.get('device', ''),
								'test_type': data.get('test_type', 'short'),
							}))
						except Exception:
							pass

				elif cmd == 'save_settings':
					new = data.get('settings', {})
					for key in DEFAULT_SETTINGS:
						if key in new:
							expected = type(DEFAULT_SETTINGS[key])
							try:
								settings[key] = expected(new[key])
							except (ValueError, TypeError):
								pass
					await asyncio.to_thread(save_settings)
					for bws in list(browser_subs.keys()):
						try:
							await bws.send_json({'type': 'settings', 'settings': settings})
						except Exception:
							pass

				elif cmd == 'test_notification':
					ok = await send_gotify('ZPulse Test', 'Test notification from ZPulse.', 5)
					await ws.send_json({'type': 'test_notification_result', 'success': ok})

			elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
				break
	finally:
		browser_subs.pop(ws, None)

	return ws


# ── Broadcast / Forward ─────────────────────────────────────────────────────

async def forward_to_browsers(hostname: str, data: dict):
	'''Forward an agent message to all browsers subscribed to that agent.

	:param hostname: Agent hostname the data came from
	:param data: Message dict to forward
	'''

	data_out = dict(data)
	data_out['hostname'] = hostname

	if data['type'] in ('disks', 'pools'):
		a = agents.get(hostname)
		if a:
			alert_msg = json.dumps({
				'type': 'alerts', 'hostname': hostname,
				'active': a.alerts_active, 'log': list(a.alert_log),
			})
			for bws, sub_hn in list(browser_subs.items()):
				if sub_hn == hostname:
					try:
						await bws.send_str(alert_msg)
					except Exception:
						pass

	msg = json.dumps(data_out)
	for bws, sub_hn in list(browser_subs.items()):
		if sub_hn == hostname:
			try:
				await bws.send_str(msg)
			except Exception:
				pass


async def broadcast_server_list():
	'''Send the current server list to all connected browsers.'''

	servers = get_server_list()
	msg = json.dumps({'type': 'servers', 'servers': servers})
	for bws in list(browser_subs.keys()):
		try:
			await bws.send_str(msg)
		except Exception:
			pass


async def periodic_broadcast(_app=None):
	'''Refresh the server list for all browsers every 30 seconds.'''

	while True:
		await asyncio.sleep(30)
		await broadcast_server_list()


# ── HTTP Routes ──────────────────────────────────────────────────────────────

async def index_handler(request: aiohttp.web.Request):
	'''Serve the main dashboard HTML page.

	:param request: Incoming aiohttp request
	'''

	return aiohttp.web.FileResponse(BASE_DIR / 'templates' / 'index.html')


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def on_startup(app: aiohttp.web.Application):
	'''Start background tasks when the server starts.

	:param app: aiohttp application instance
	'''

	app['periodic_task'] = asyncio.create_task(periodic_broadcast())


async def on_shutdown(app: aiohttp.web.Application):
	'''Cancel background tasks and close HTTP sessions on shutdown.

	:param app: aiohttp application instance
	'''

	global gotify_session
	app['periodic_task'].cancel()
	if gotify_session:
		await gotify_session.close()



if __name__ == '__main__':
	# Parse command line arguments
	parser = argparse.ArgumentParser(description='ZPulse Dashboard')
	parser.add_argument('--host', default='0.0.0.0', help='Listen address (default: 0.0.0.0)')
	parser.add_argument('--port', type=int, default=8888, help='Listen port (default: 8888)')
	parser.add_argument('-d', '--debug', action='store_true', help='Enable debug logging')
	args = parser.parse_args()

	# Setup logging
	if args.debug:
		apv.setup_logging(level='DEBUG', log_to_disk=True, max_log_size=5*1024*1024, max_backups=5, compress_backups=True, log_file_name='zpulse-dashboard', show_details=True)
		logging.debug('Debug logging enabled')
	else:
		apv.setup_logging(level='INFO')

	load_settings()

	logging.info('ZPulse Dashboard starting on http://%s:%d', args.host, args.port)

	app = aiohttp.web.Application()
	app.router.add_get('/', index_handler)
	app.router.add_get('/ws/agent', agent_ws_handler)
	app.router.add_get('/ws', browser_ws_handler)
	app.router.add_static('/static', str(BASE_DIR / 'static'))
	app.on_startup.append(on_startup)
	app.on_shutdown.append(on_shutdown)

	aiohttp.web.run_app(app, host=args.host, port=args.port, print=lambda s: logging.info(s))
