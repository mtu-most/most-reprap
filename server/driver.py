#! /usr/bin/python
# vim: set foldmethod=marker fileencoding=utf8 :

show_own_debug = False
#show_own_debug = True
show_firmware_debug = True

# Constants {{{
C0 = 273.15	# Conversion between K and °C
WAIT = object()	# Sentinel for blocking functions.
# Space types
TYPE_CARTESIAN = 0
TYPE_DELTA = 1
TYPE_EXTRUDER = 2
# }}}

# Imports.  {{{
import fhs
config = fhs.init(packagename = 'franklin', config = {
	'cdriver': None,
	'port': None,
	'uuid': None,
	'run-id': None,
	'allow-system': None
	})
import websockets
from websockets import log
import serial
import time
import math
import struct
import os
import re
import sys
import wave
import sys
import io
import base64
import json
import fcntl
import select
import subprocess
import traceback
import protocol
import mmap
# }}}

# Enable code trace. {{{
if False:
	def trace(frame, why, arg):
		if why == 'call':
			code = frame.f_code
			log('call: %s' % code.co_name)
	sys.settrace(trace)
# }}}

fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)

def dprint(x, data): # {{{
	if show_own_debug:
		log('%s: %s' %(x, ' '.join(['%02x' % ord(c) for c in data])))
# }}}

# Decorator for functions which block.
def delayed(f): # {{{
	def ret(self, *a, **ka):
		#log('delayed called with args %s,%s' % (repr(a), repr(ka)))
		def wrap(id):
			#log('wrap called with id %s' % (repr(id)))
			return f(self, id, *a, **ka)
		return (WAIT, wrap)
	return ret
# }}}

# Call cdriver running on same machine.
class Driver: # {{{
	def __init__(self, port, run_id):
		self.driver = subprocess.Popen((config['cdriver'], port, run_id), stdin = subprocess.PIPE, stdout = subprocess.PIPE, close_fds = True)
		fcntl.fcntl(self.driver.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
		self.buffer = ''
	def available(self):
		return len(self.buffer) > 0
	def write(self, data):
		self.driver.stdin.write(data)
		self.driver.stdin.flush()
	def read(self, length):
		while True:
			try:
				r = self.driver.stdout.read()
			except IOError:
				r = self.buffer[:length]
				self.buffer = self.buffer[length:]
				return r
			if r == '':
				log('EOF!')
				self.close()
			self.buffer += r
			if len(self.buffer) >= length:
				ret = self.buffer[:length]
				self.buffer = self.buffer[length:]
				return ret
	def close(self):
		log('Closing printer driver; exiting.')
		sys.exit(0)
	def fileno(self):
		return self.driver.stdout.fileno()
# }}}

# Reading and writing pins to and from ini files. {{{
def read_pin(pin):
	extra = 0
	if pin.startswith('X'):
		pin = pin[1:]
		if pin == '':
			return 0
	else:
		extra += 256
	if pin.startswith('-'):
		extra += 512
		pin = pin[1:]
	try:
		return int(pin) + extra
	except:
		log('incorrect pin %s' % pin)
		return 0

def write_pin(pin):
	if pin == 0:
		return 'X'
	ret = ''
	if pin >= 512:
		ret += '-'
		pin -= 512
	if pin >= 256:
		pin -= 256
	else:
		ret = 'X' + ret
	return ret + '%d' % pin
# }}}

class Printer: # {{{
	# Internal stuff.  {{{
	def _read_data(self, data): # {{{
		cmd, s, m, e, f = struct.unpack('<BLLLf', data[:17])
		return cmd, s, m, f, e, data[17:]
	# }}}
	def _send(self, *data): # {{{
		#sys.stderr.write(repr(data) + '\n')
		sys.stdout.write(json.dumps(data) + '\n')
		sys.stdout.flush()
	# }}}
	def __init__(self, port, uuid, run_id, allow_system): # {{{
		self.printer = Driver(port, run_id)
		self.uuid = uuid
		self.allow_system = allow_system
		try:
			with fhs.read_data(os.path.join(self.uuid, 'profile')) as pfile:
				self.profile = pfile.readline().strip()
			log('profile is %s' % self.profile)
		except:
			log("No default profile; using 'default'.")
			self.profile = 'default'
		# Fill job queue.
		self.jobqueue = {}
		spool = fhs.read_spool(dir = True, opened = False)
		if spool is not None and os.path.isdir(spool):
			for filename in os.listdir(spool):
				name, ext = os.path.splitext(filename)
				if ext != os.extsep + 'bin':
					log('skipping %s' % filename)
					continue
				try:
					log('opening %s' % filename)
					with open(os.path.join(spool, filename), 'rb') as f:
						f.seek(-6 * 4, os.SEEK_END)
						self.jobqueue[name] = struct.unpack('<' + 'f' * 6, f.read())
				except:
					traceback.print_exc()
					log('failed to open spool file %s' % os.path.join(spool, filename))
		self.job_output = ''
		self.jobs_active = []
		self.jobs_ref = None
		self.jobs_angle = 0
		self.jobs_probemap = None
		self.job_current = 0
		self.job_id = None
		self.confirm_id = 0
		self.confirm_message = None
		self.confirmer = None
		self.position_valid = False
		self.probing = False
		self.home_phase = None
		self.home_target = None
		self.home_space = None
		self.home_spaces = []
		self.home_cb = [False, self._do_home]
		self.probe_cb = [False, None]
		self.gcode_file = False
		self.gcode_map = None
		self.gcode_id = None
		self.gcode_wait = None
		self.gcode_parking = False
		self.gcode_waiting = 0
		self.queue = []
		self.queue_pos = 0
		self.queue_info = None
		self.resuming = False
		self.flushing = False
		self.debug_buffer = None
		self.printer_buffer = ''
		self.command_buffer = ''
		self.audiofile = None
		self.bed_id = 255
		self.fan_id = 255
		self.spindle_id = 255
		self.probe_dist = 1000
		self.probe_safe_dist = 10
		self.unit_name = 'mm'
		# Set up state.
		self.sending = False
		self.paused = False
		self.limits = []
		self.sense = []
		self.wait = False
		self.waitaudio = False
		self.audioid = None
		self.movewait = 0
		self.movecb = []
		self.tempcb = []
		self.alarms = set()
		self.run_id = run_id
		self.zoffset = 0.
		self.multipliers = []
		self.current_extruder = 0
		# Get the printer state.
		self.spaces = []
		self.temps = []
		self.gpios = []
		self._read_globals(False)
		for i, s in enumerate(self.spaces):
			s.read(self._read('SPACE', i))
		for i, t in enumerate(self.temps):
			t.read(self._read('TEMP', i))
			# Disable heater.
			self.settemp(i, float('nan'), update = False)
			# Disable heater alarm.
			self.waittemp(i, None, None)
		for i, g in enumerate(self.gpios):
			g.read(self._read('GPIO', i))
		# The printer may still be doing things.  Pause it and send a move; this will discard the queue.
		self.pause(True, False, update = False)
		self.load(update = False)
		global show_own_debug
		if show_own_debug is None:
			show_own_debug = True
	# }}}
	# Constants.  {{{
	# Single-byte commands.
	single = {'OK': '\xb3', 'WAIT': '\xad' }
	# }}}
	def _broadcast(self, *a): # {{{
		self._send(None, 'broadcast', *a)
	# }}}
	def _close(self): # {{{
		log('disconnecting')
		self._send(None, 'disconnect')
		waiting_commands = ''
		while True:
			select.select([sys.stdin], [], [sys.stdin])
			self.command_buffer += sys.stdin.read()
			while '\n' in self.command_buffer:
				pos = self.command_buffer.index('\n')
				ln = self.command_buffer[:pos]
				self.command_buffer = self.command_buffer[pos + 1:]
				id, func, a, ka = json.loads(ln)
				if func == 'reconnect':
					log('reconnect %s' % a[0])
					self._send_packet(chr(protocol.command['RECONNECT']) + a[0] + '\x00')
					self.command_buffer = waiting_commands + self.command_buffer
					self._send(id, 'return', None)
					return
				elif func in ('export_settings', 'die'):
					ret = getattr(self, func)(*a, **ka)
					if ret == (WAIT, WAIT):
						# Special case: request to die.
						sys.exit(0)
					else:
						self._send(id, 'return', ret)
				else:
					waiting_commands += ln + '\n'
	# }}}
	def _printer_read(self, *a, **ka): # {{{
		while True:
			try:
				return self.printer.read(*a, **ka)
			except:
				log('error reading')
				traceback.print_exc()
				sys.exit(0)
	# }}}
	def _printer_write(self, data): # {{{
		#log('writing %s' % ' '.join(['%02x' % ord(x) for x in data]))
		while True:
			try:
				self.printer.write(data)
				return
			except:
				log('error writing')
				traceback.print_exc()
				sys.exit(0)
	# }}}
	def _command_input(self): # {{{
		data = sys.stdin.read()
		if data == '':
			log('End of file detected on command input; exiting.')
			sys.exit(0)
		self.command_buffer += data
		die = False
		while '\n' in self.command_buffer:
			pos = self.command_buffer.index('\n')
			id, func, a, ka = json.loads(self.command_buffer[:pos])
			#log('command: %s' % repr((id, func, a, ka)))
			self.command_buffer = self.command_buffer[pos + 1:]
			try:
				ret = getattr(self, func)(*a, **ka)
				if isinstance(ret, tuple) and len(ret) == 2 and ret[0] is WAIT:
					# The function blocks; it will send its own reply later.
					if ret[1] is WAIT:
						# Special case: request to die.
						die = True
					else:
						ret[1](id)
						continue
			except SystemExit:
				sys.exit(0)
			except:
				log('error handling command input')
				traceback.print_exc()
				self._send(id, 'error', repr(sys.exc_info()))
				continue
			if not die:
				self._send(id, 'return', ret)
		if die:
			sys.exit(0)
	# }}}
	def _trigger_movewaits(self, num): # {{{
		#traceback.print_stack()
		#log('trigger %s' % repr(self.movecb))
		#log('movecbs: %d/%d' % (num, self.movewait))
		if self.movewait < num:
			log('More cbs received than requested!')
			self.movewait = 0
		else:
			#log('movewait %d/%d' % (num, self.movewait))
			self.movewait -= num
		if self.movewait == 0:
			call_queue.extend([(x[1], [True]) for x in self.movecb])
			self.movecb = []
			if self.flushing and self.queue_pos >= len(self.queue):
				#log('done flushing')
				self.flushing = 'done'
				call_queue.append((self._do_gcode, []))
		#else:
		#	log('cb seen, but waiting for more')
	# }}}
	def _printer_input(self, reply = False): # {{{
		while True:
			if len(self.printer_buffer) == 0:
				r = self._printer_read(1)
				dprint('(1) read', r)
				if r == '':
					return ('no data', None)
				if r == self.single['WAIT']:
					return ('wait', None)
				if r == self.single['OK']:
					return ('ok', None)
				# Regular packet.
				self.printer_buffer = r
			packet_len = ord(self.printer_buffer[0])
			while True:
				r = self._printer_read(packet_len - len(self.printer_buffer))
				dprint('rest of packet read', r)
				if r == '':
					return (None, None)
				self.printer_buffer += r
				if len(self.printer_buffer) >= packet_len:
					break
				if not self.printer.available():
					#log('waiting for more data (%d/%d)' % (len(self.printer_buffer), packet_len))
					ret = select.select([self.printer], [], [self.printer], 1)
					if self.printer not in ret[0]:
						log('broken packet?')
						return (None, None)
			self.printer.write(self.single['OK'])
			cmd, s, m, f, e, data = self._read_data(self.printer_buffer[1:])
			self.printer_buffer = ''
			# Handle the asynchronous events.
			if cmd == protocol.rcommand['MOVECB']:
				#log('movecb %d/%d (%d in queue)' % (s, self.movewait, len(self.movecb)))
				self._trigger_movewaits(s)
				continue
			if cmd == protocol.rcommand['TEMPCB']:
				self.alarms.add(s)
				t = 0
				while t < len(self.tempcb):
					if self.tempcb[t][0] is None or self.tempcb[t][0] in self.alarms:
						call_queue.append((self.tempcb.pop(t)[1], []))
					else:
						t += 1
				call_queue.append((self._do_gcode, []))
				continue
			elif cmd == protocol.rcommand['CONTINUE']:
				if s == 0:
					# Move continue.
					self.wait = False
					#log('resuming queue %d' % len(self.queue))
					call_queue.append((self._do_queue, []))
					if self.flushing is None:
						self.flushing = False
						call_queue.append((self._do_gcode, []))
				else:
					# Audio continue.
					self.waitaudio = False
					self._audio_play()
				continue
			elif cmd == protocol.rcommand['LIMIT']:
				self.limits[s][m] = f
				#log('limit; %d waits' % e)
				self._trigger_movewaits(self.movewait)
				continue
			elif cmd == protocol.rcommand['TIMEOUT']:
				self.position_valid = False
				self._globals_update()
				for i, t in enumerate(self.temps):
					if not math.isnan(t.value):
						t.value = float('nan')
						self._temp_update(i)
				for i, g in enumerate(self.gpios):
					if g.state != g.reset:
						g.state = g.reset
						self._gpio_update(i)
				continue
			elif cmd == protocol.rcommand['SENSE']:
				if m not in self.sense[s]:
					self.sense[s][m] = []
				self.sense[s][m].append((e, f))
				s = 0
				while s < len(self.movecb):
					if self.movecb[s][0] is not False and any(x[1] in self.sense[x[0]] for x in self.movecb[s][0]):
						call_queue.append((self.movecb.pop(s)[1], [self.movewait == 0]))
					else:
						s += 1
				continue
			elif cmd == protocol.rcommand['HOMED']:
				self._do_home(True)
				continue
			elif cmd == protocol.rcommand['DISCONNECT']:
				self._close()
				# _close returns after reconnect.
				continue
			elif cmd == protocol.rcommand['UPDATE_TEMP']:
				if s < len(self.temps):
					self.temps[s].value = f - C0
					self._temp_update(s)
				else:
					log('Ignoring updated invalid temp %d' % s)
				continue
			elif cmd == protocol.rcommand['UPDATE_PIN']:
				gpios[s].state = m
				self._gpio_update(s)
				continue
			elif cmd == protocol.rcommand['CONFIRM']:
				self.request_confirmation(data or 'Continue?')[1](False)
				continue
			elif cmd == protocol.rcommand['FILE_DONE']:
				self._print_done(True, 'completed')
				continue
			if reply:
				return ('packet', (cmd, s, m, f, e, data))
			log('unexpected packet %02x' % cmd)
			raise AssertionError('Received unexpected reply packet')
	# }}}
	def _send_packet(self, data, move = False, audio = False): # {{{
		data = chr(len(data) + 1) + data
		dprint('(1) writing', data);
		self._printer_write(data)
		if not move and not audio:
			return True
		start_time = time.time()
		while True:
			if not self.printer.available():
				ret = select.select([self.printer], [], [self.printer], 1)
				if self.printer not in ret[0] and self.printer not in ret[2]:
					# No response; keep waiting.
					log('no response yet: %s' % repr(ret))
					assert time.time() - start_time < 10
					continue
			ret = self._printer_input()
			if ret[0] == 'wait':
				#log('wait')
				if audio:
					self.waitaudio = True
				else:
					self.wait = True
				return True
			elif ret[0] == 'ok':
				return True
			#log('no response yet')
	# }}}
	def _get_reply(self, cb = False): # {{{
		while True:
			if not self.printer.available():
				ret = select.select([self.printer], [], [self.printer], 3)
				if len(ret[0]) == 0 and len(ret[2]) == 0:
					log('no reply received')
					#traceback.print_stack()
					continue
			ret = self._printer_input(reply = True)
			if ret[0] == 'packet' or (cb and ret[0] == 'no data'):
				return ret[1]
			#log('no response yet waiting for reply')
	# }}}
	def _read(self, cmd, channel, sub = None): # {{{
		if cmd == 'SPACE':
			info = self._read('SPACE_INFO', channel)
			self.spaces[channel].type, self.spaces[channel].max_deviation = struct.unpack('<Bf', info[:5])
			if self.spaces[channel].type == TYPE_CARTESIAN:
				num_axes = struct.unpack('<B', info[5:])[0]
				num_motors = num_axes
			elif self.spaces[channel].type == TYPE_DELTA:
				self.spaces[channel].delta = [{}, {}, {}]
				for a in range(3):
					self.spaces[channel].delta[a]['axis_min'], self.spaces[channel].delta[a]['axis_max'], self.spaces[channel].delta[a]['rodlength'], self.spaces[channel].delta[a]['radius'] = struct.unpack('<ffff', info[5 + 16 * a:5 + 16 * (a + 1)])
				self.spaces[channel].delta_angle = struct.unpack('<f', info[5 + 16 * 3:])[0]
				num_axes = 3
				num_motors = 3
			elif self.spaces[channel].type == TYPE_EXTRUDER:
				num_axes = struct.unpack('<B', info[5:6])[0]
				num_motors = num_axes
				self.spaces[channel].extruder = []
				for a in range(num_axes):
					dx, dy, dz = struct.unpack('<fff', info[6 + 12 * a:6 + 12 * (a + 1)])
					self.spaces[channel].extruder.append({'dx': dx, 'dy': dy, 'dz': dz})
			else:
				log('invalid type')
				raise AssertionError('invalid space type')
			return ([self._read('SPACE_AXIS', channel, axis) for axis in range(num_axes)], [self._read('SPACE_MOTOR', channel, motor) for motor in range(num_motors)])
		if cmd == 'GLOBALS':
			packet = struct.pack('<B', protocol.command['READ_' + cmd])
		elif sub is not None and cmd.startswith('SPACE'):
			packet = struct.pack('<BBB', protocol.command['READ_' + cmd], channel, sub)
		else:
			packet = struct.pack('<BB', protocol.command['READ_' + cmd], channel)
		self._send_packet(packet)
		cmd, s, m, f, e, data = self._get_reply()
		assert cmd == protocol.rcommand['DATA']
		return data
	# }}}
	def _read_globals(self, update = True): # {{{
		data = self._read('GLOBALS', None)
		if data is None:
			return False
		self.queue_length, self.audio_fragments, self.audio_fragment_size, self.num_digital_pins, self.num_analog_pins, num_spaces, num_temps, num_gpios = struct.unpack('<BBBBBBBB', data[:8])
		self.led_pin, self.probe_pin, self.timeout, self.feedrate, self.current_extruder = struct.unpack('<HHHfB', data[8:])
		while len(self.spaces) < num_spaces:
			self.spaces.append(self.Space(self, len(self.spaces)))
			if update:
				data = self._read('SPACE', len(self.spaces) - 1)
				self.spaces[-1].read(data)
		self.spaces = self.spaces[:num_spaces]
		if num_spaces < 2:
			self.multipliers = []
		while len(self.temps) < num_temps:
			self.temps.append(self.Temp(len(self.temps)))
			if update:
				data = self._read('TEMP', len(self.temps) - 1)
				self.temps[-1].read(data)
		self.temps = self.temps[:num_temps]
		while len(self.gpios) < num_gpios:
			self.gpios.append(self.Gpio(len(self.gpios)))
			if update:
				data = self._read('GPIO', len(self.gpios) - 1)
				self.gpios[-1].read(data)
		self.gpios = self.gpios[:num_gpios]
		while len(self.limits) < num_spaces:
			self.limits.append({})
			self.sense.append({})
		while len(self.limits) > num_spaces:
			self.limits.pop()
			self.sense.pop()
		return True
	# }}}
	def _write_globals(self, ns, nt, ng, update = True): # {{{
		if ns is None:
			ns = len(self.spaces)
		if nt is None:
			nt = len(self.temps)
		if ng is None:
			ng = len(self.gpios)
		ds = ns - len(self.spaces)
		dt = nt - len(self.temps)
		dg = ng - len(self.gpios)
		data = struct.pack('<BBBHHHfB', ns, nt, ng, self.led_pin, self.probe_pin, self.timeout, self.feedrate, self.current_extruder)
		self._send_packet(struct.pack('<B', protocol.command['WRITE_GLOBALS']) + data)
		self._read_globals(update = True)
		if update:
			self._globals_update()
			for s in range(ds):
				self._space_update(ns - ds + s)
			for t in range(dt):
				self._temp_update(nt - dt + t)
			for g in range(dg):
				self._gpio_update(ng - dg + g)
		return True
	# }}}
	def _globals_update(self, target = None): # {{{
		self._broadcast(target, 'globals_update', [self.profile, len(self.spaces), len(self.temps), len(self.gpios), self.led_pin, self.probe_pin, self.probe_dist, self.probe_safe_dist, self.bed_id, self.fan_id, self.spindle_id, self.unit_name, self.timeout, self.feedrate, self.zoffset, not self.paused and (None if self.gcode_map is None and not self.gcode_file else True)])
	# }}}
	def _space_update(self, which, target = None): # {{{
		self._broadcast(target, 'space_update', which, self.spaces[which].export())
	# }}}
	def _temp_update(self, which, target = None): # {{{
		self._broadcast(target, 'temp_update', which, self.temps[which].export())
	# }}}
	def _gpio_update(self, which, target = None): # {{{
		self._broadcast(target, 'gpio_update', which, self.gpios[which].export())
	# }}}
	def _use_probemap(self, x, y, z): # {{{
		'''Return corrected z according to self.gcode_probemap.'''
		# Map = [[x0, y0, x1, y1], [nx, ny], [[...], [...], ...]]
		if self.gcode_probemap is None or any(math.isnan(t) for t in (x, y, z)):
			return z
		p = self.gcode_probemap
		x -= p[0][0]
		y -= p[0][1]
		x /= (p[0][2] - p[0][0]) / p[1][0]
		y /= (p[0][3] - p[0][1]) / p[1][1]
		if x < 0:
			x = 0
		ix = int(x)
		if x >= p[1][0]:
			x = p[1][0]
			ix = int(x) - 1
		if y < 0:
			y = 0
		iy = int(y)
		if y >= p[1][1]:
			y = p[1][1]
			iy = int(y) - 1
		fx = x - ix
		fy = y - iy
		l = p[2][iy][ix] * (1 - fy) + p[2][iy + 1][ix] * fy
		r = p[2][iy][ix + 1] * (1 - fy) + p[2][iy + 1][ix + 1] * fy
		return z + l * (1 - fx) + r * fx
	# }}}
	def _do_gcode(self): # {{{
		if self.gcode_map is None:
			#log('end of gcode')
			return
		flushed = False
		if self.flushing == 'done':
			#log('done flushing; continuing gcode')
			self.flushing = False
			flushed = True
		#log('len gcode = %d' % len(self.gcode))
		while self.gcode_current_record < self.gcode_num_records:
			if self.gcode_parking or self.gcode_waiting > 0 or self.confirmer is not None or self.gcode_wait is not None or self.flushing or self.paused:
				#log('gcode waiting: %s' % repr((self.gcode_parking, self.gcode_waiting, self.confirmer, self.gcode_wait)))
				# Wait until done.
				return
			if not self.position_valid:
				self.gcode_parking = True
				self.park(cb = lambda: self._do_gcode(), abort = False)[1](None)
				return
			pos = self.gcode_current_record * (1 + 11 * 4)
			record = struct.unpack('<Bl' + 'f' * 10, self.gcode_map[pos:pos + 1 + 11 * 4])
			cmd = record[0]
			T, x, X, y, Y, z, Z, e, E, f, F = record[1:]
			#log('Running %s %s' % (cmd, args))
			if cmd == protocol.parsed['GOTO']:
				if self.flushing is None:
					#log('not filling; waiting for queue space')
					return
				'''
				# Do only one segment at a time.
				if not flushed:
					self.flush()[1](None)
					self.flushing = True
					#log('flush set')
					return
				flushed = False
				#'''
				#log(repr(args))
				sina, cosa = self.gcode_angle
				target = cosa * X - sina * Y + self.gcode_ref[0], cosa * Y + sina * X + self.gcode_ref[1], Z + self.gcode_ref[2]
				if self._use_probemap and self.gcode_probemap:
					if x is not None:
						source = cosa * x - sina * y + self.gcode_ref[0], cosa * y + sina * x + self.gcode_ref[1]
						#log('gcode %s' % repr([(target[t], source[t], self.gcode_probemap[0][t + 2], self.gcode_probemap[0][t], self.gcode_probemap[1][t]) for t in range(2)]))
						nums = [abs(target[t] - source[t]) / (abs(self.gcode_probemap[0][t + 2] - self.gcode_probemap[0][t]) / self.gcode_probemap[1][t]) for t in range(2) if not math.isnan(target[t]) and self.gcode_probemap[0][t] != self.gcode_probemap[0][t + 2]]
						#log('num %s' % nums)
						if len(nums) == 0 or all(math.isnan(num) for num in nums):
							num = 1
						else:
							num = int(max(n for n in nums if not math.isnan(n))) + 1
						if num == 1:
							#log('debugpart: %.2f %.2f %.2f %.2f' % (target[0], target[1], f, F))
							z = self._use_probemap(*target)
							#log('go to one %f %f %f' % (target[0], target[1], z))
							self.goto([[target[0], target[1], z], {T: E}], f0 = f, f1 = F)[1](None)
						else:
							for t in range(num):
								targetpart = [source[tt] + (target[tt] - source[tt]) * (t + 1.) / num for tt in range(2)]
								#log('debugpart: %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f' % (target[0], target[1], source[0], source[1], targetpart[0], targetpart[1], f, F))
								z = self._use_probemap(targetpart[0], targetpart[1], target[2])
								#log('go to part %f %f %f' % (targetpart[0], targetpart[1], z))
								self.goto([[targetpart[0], targetpart[1], z], {T: E}], f0 = f * num, f1 = F * num)[1](None)
					else:
						self.goto([[target[0], target[1], self._use_probemap(*target)], {T: E}], f0 = f, f1 = F)[1](None)
				else:
					self.goto([target, {T: E}], f0 = f, f1 = F)[1](None)
				if self.wait and self.flushing is False:
					#log('stop filling; wait for queue space')
					self.flushing = None
					self.gcode_current_record += 1
					continue
			else:
				if not flushed:
					self.flush()[1](None)
					self.flushing = True
					#log('flush set')
					return
				flushed = False
				if cmd == protocol.parsed['PARK']:
					self.gcode_current_record += 1
					self.gcode_parking = True
					return self.park(self._do_gcode, abort = False)[1](None)
				elif cmd == protocol.parsed['WAIT']:
					self.gcode_current_record += 1
					self.gcode_wait = time.time() + x / 1000
					return
				elif cmd == protocol.parsed['SETPOS']:
					self.set_axis_pos(1, e, x)
				elif cmd == protocol.parsed['CONFIRM']:
					# Wait.
					self.gcode_current_record += 1
					self.request_confirmation(self.gcode_strings[T] if T >= 0 else 'Continue?')[1](False)
					return
				elif cmd == protocol.parsed['GPIO']:
					if T == -1:
						T = self.fan_id
					elif T == -2:
						T = self.spindle_id
					if 0 <= T < len(self.gpios):
						self.set_gpio(T, state = x)
					elif T != 255:
						log('ignoring GPIO for invalid pin %d' % T)
				elif cmd == protocol.parsed['SETTEMP']:
					if T == -1:
						T = self.bed_id
					if 0 <= T < len(self.temps):
						self.settemp(T, x)
					elif T != 255:
						log('ignoring SETTEMP for invalid temp %d' % T)
				elif cmd == protocol.parsed['WAITTEMP']:
					if T == -1:
						T = self.bed_id
					self.clear_alarm()
					def wait_one(which):
						if not 0 <= which < len(self.temps) or math.isnan(self.temps[which].value) or not self.pin_valid(self.temps[which].thermistor_pin):
							return
						self.waittemp(which, self.temps[which].value)
						self.gcode_waiting += 1
					if T == -2:
						for t in range(len(self.temps)):
							wait_one(t)
					else:
						wait_one(T)
					self.gcode_current_record += 1
					return self.wait_for_temp()[1](None)
				elif cmd == protocol.parsed['SYSTEM']:
					assert 0 <= T < len(self.gcode_strings)
					command = self.gcode_strings[T]
					if not re.match(self.allow_system, command):
						log('Refusing to run forbidden system command: %s' % command)
					else:
						log('Running system command: %s' % command)
						os.system(command)
			if self.gcode_map is not None and self.gcode_current_record < self.gcode_num_records:
				self.gcode_current_record += 1
			else:
				# Printing was aborted; don't call print_done, or we'll abort whatever aborted us.
				return
		if not flushed:
			self.flush()[1](None)
			self.flushing = True
			#log('flush set')
			return
		if len(self.spaces) > 1:
			for e in range(len(self.spaces[1].axis)):
				self.set_axis_pos(1, e, 0)
		self._print_done(True, 'completed')
	# }}}
	def _gcode_close(self): # {{{
		self.gcode_strings = []
		self.gcode_map.close()
		os.close(self.gcode_fd)
		self.gcode_map = None
		self.gcode_fd = -1
	# }}}
	def _print_done(self, complete, reason): # {{{
		if self.gcode_map is not None:
			log(reason)
			self._gcode_close()
		self.gcode_file = False
		#traceback.print_stack()
		if self.queue_info is None and self.gcode_id is not None:
			log('Print done (%d): %s' % (complete, reason))
			self._send(self.gcode_id, 'return', (complete, reason))
		self.gcode_id = None
		self.gcode_wait = None
		#log('job %s' % repr(self.jobs_active))
		if self.queue_info is None and len(self.jobs_active) > 0:
			if complete:
				if self.job_current >= len(self.jobs_active) - 1:
					#log('job queue done')
					if self.job_id is not None:
						self._send(self.job_id, 'return', (True, reason))
					self.job_id = None
					self.jobs_active = []
				else:
					#log('job part done; moving on')
					self._next_job()
			else:
				#log('job aborted')
				if self.job_id is not None:
					self._send(self.job_id, 'return', (False, reason))
				self.job_id = None
				self.jobs_active = []
		while self.queue_pos < len(self.queue):
			id, axes, e, f0, f1, which, cb = self.queue[self.queue_pos]
			self.queue_pos += 1
			if id is not None:
				self._send(id, 'error', 'aborted')
		self.queue = []
		self.queue_pos = 0
		if self.home_phase is not None:
			#log('killing homer')
			self.home_phase = None
			self.set_space(self.home_space, type = self.home_orig_type)
			if self.home_cb in self.movecb:
				self.movecb.remove(self.home_cb)
				if self.home_id is not None:
					self._send(self.home_id, 'return', None)
		if self.probe_cb in self.movecb:
			#log('killing prober')
			self.movecb.remove(self.probe_cb)
			self.probe_cb(False)
		self._globals_update()
	# }}}
	def _unpause(self): # {{{
		if self.gcode_file:
			self._send_packet(chr(protocol.command['RESUME']))	# Just in case.
		if self.queue_info is None:
			return
		log('doing resume to %d/%d' % (self.queue_info[0], len(self.queue_info[2])))
		self.queue = self.queue_info[2]
		self.queue_pos = self.queue_info[0]
		self.movecb = self.queue_info[3]
		self.flushing = self.queue_info[4]
		self.gcode_wait = self.queue_info[5]
		self.resuming = False
		self.queue_info = None
		self.paused = False
		self._globals_update()
		call_queue.append((self._do_gcode, []))
	# }}}
	def _queue_add(self, f, name): # {{{
		origname = name
		i = 0
		while name in self.jobqueue:
			name = '%s-%d' % (origname, i)
			i += 1
		bbox, errors = self._gcode_parse(f, name)
		for e in errors:
			log(e)
		if bbox is None:
			return errors
		self.jobqueue[os.path.splitext(name)[0]] = bbox
		self._broadcast(None, 'queue', [(q, self.jobqueue[q]) for q in self.jobqueue])
		return errors
	# }}}
	def _do_queue(self): # {{{
		#log('queue %s' % repr((self.queue_pos, len(self.queue), self.resuming, self.wait)))
		if self.paused and not self.resuming and len(self.queue) == 0:
			#log('queue is empty')
			return
		while not self.wait and (self.queue_pos < len(self.queue) or self.resuming):
			#log('queue not empty %s' % repr((self.queue_pos, len(self.queue), self.resuming, self.wait)))
			if self.queue_pos >= len(self.queue):
				self._unpause()
				log('unpaused, %d %d' % (self.queue_pos, len(self.queue)))
				if self.queue_pos >= len(self.queue):
					break
			id, axes, f0, f1, cb, probe = self.queue[self.queue_pos]
			#log('queueing %s' % repr((id, axes, f0, f1, cb, probe)))
			self.queue_pos += 1
			# Turn sequences into a dict.
			if isinstance(axes, (list, tuple)):
				adict = {}
				for s, data in enumerate(axes):
					adict[s] = data
				axes = adict
			# Make sure the keys are ints.
			adict = {}
			for k in axes:
				adict[int(k)] = axes[k]
			axes = adict
			a = {}
			a0 = 0
			for i, sp in enumerate(self.spaces):
				# Only handle spaces that are specified.
				if i not in axes or axes[i] is None:
					a0 += len(sp.axis)
					continue
				# Handle sequences.
				if isinstance(axes[i], (list, tuple)):
					for ij, axis in enumerate(axes[i]):
						if ij >= len(sp.axis):
							log('ignoring nonexistent axis %d %d' % (i, ij))
							continue
						if axis is not None and not math.isnan(axis):
							if i == 1 and ij != self.current_extruder:
								log('setting current extruder to %d' % ij)
								self.current_extruder = ij
								self._write_globals()
							# Limit values for axis.
							if axis > sp.axis[ij]['max'] - sp.axis[ij]['offset']:
								log('limiting %d %d to %f because it exceeds max' % (i, ij, axis))
								axis = sp.axis[ij]['max'] - sp.axis[ij]['offset']
							if axis < sp.axis[ij]['min'] - sp.axis[ij]['offset']:
								log('limiting %d %d to %f because it exceeds min' % (i, ij, axis))
								axis = sp.axis[ij]['min'] - sp.axis[ij]['offset']
							a[a0 + ij] = axis
				else:
					for j, axis in tuple(axes[i].items()):
						ij = int(j)
						if ij >= len(sp.axis):
							log('ignoring nonexistent axis %d %d' % (i, ij))
							continue
						if axis is not None and not math.isnan(axis):
							if i == 1 and ij != self.current_extruder:
								log('Setting current extruder to %d' % ij)
								self.current_extruder = ij
								self._write_globals(len(self.spaces), len(self.temps), len(self.gpios))
							# Limit values for axis.
							if axis > sp.axis[ij]['max'] - sp.axis[ij]['offset']:
								log('limiting %d %d to %f because it exceeds max' % (i, ij, axis))
								axis = sp.axis[ij]['max'] - sp.axis[ij]['offset']
							if axis < sp.axis[ij]['min'] - sp.axis[ij]['offset']:
								log('limiting %d %d to %f because it exceeds min' % (i, ij, axis))
								axis = sp.axis[ij]['min'] - sp.axis[ij]['offset']
							a[a0 + ij] = axis
				a0 += len(sp.axis)
			targets = [0] * (((2 + a0 - 1) >> 3) + 1)
			axes = a
			args = ''
			# Set defaults for feedrates.
			if f0 is None:
				f0 = float('inf')
			if f1 is None:
				f1 = f0
			assert f0 >= 0 and f1 >= 0 and (f0 > 0 or f1 > 0)
			# If feedrates are equal to firmware defaults, don't send them.
			if f0 != float('inf'):
				targets[0] |= 1 << 0
				args += struct.pack('<f', f0)
			if f1 != f0:
				targets[0] |= 1 << 1
				args += struct.pack('<f', f1)
			a = list(axes.keys())
			a.sort()
			#log('f0: %f f1: %f' %(f0, f1))
			for axis in a:
				if math.isnan(axes[axis]):
					continue
				targets[(axis + 2) >> 3] |= 1 << ((axis + 2) & 0x7)
				args += struct.pack('<f', axes[axis])
				#log('axis %d: %f' %(axis, axes[axis]))
			if probe:
				assert cb
				self.movewait += 1
				p = chr(protocol.command['PROBE'])
			else:
				self.movewait += 1
				#log('movewait +1 -> %d' % self.movewait)
			#log('queueing %s' % repr((axes, f0, f1, cb, self.flushing)))
			self._send_packet(chr(protocol.command['GOTO']) + ''.join([chr(t) for t in targets]) + args, move = True)
			if id is not None:
				self._send(id, 'return', None)
			if self.flushing is None:
				self.flushing = False
				call_queue.append((self._do_gcode, []))
		#log('queue done %s' % repr((self.queue_pos, len(self.queue), self.resuming, self.wait)))
	# }}}
	def _do_home(self, done = None): # {{{
		#log('do_home: %s %s' % (self.home_phase, done))
		# 0: Prepare for next order.
		# 1: Move to max limit switch or find sense switch.
		# 2: Move to min limit switch or find sense switch.
		# 3: Move slowly away from switch.
		# 4: Repeat until all home orders are done.
		# 5: Set current position; move to center (delta only).
		#log('home %s %s' % (self.home_phase, repr(self.home_target)))
		#traceback.print_stack()
		if self.home_phase is None:
			if done is not None:
				# Continuing call received after homing was aborted; ignore.
				return
			# Initial call; start homing.
			if len(self.home_spaces) == 0:
				return
			self.home_space = self.home_spaces.pop(0)
			#log('homing %s' % repr(self.home_space))
			self.home_phase = 0
			# If it is currently moving, doing the things below without pausing causes stall responses.
			self.pause(True, False)
			self.sleep(False)
			n = [m['home_order'] for m in self.spaces[self.home_space].motor]
			if len(n) == 0:
				self.home_phase = 6
			else:
				self.home_order = min(n)
			# Fall through.
		if self.home_phase == 0:
			# Move up to find limit or sense switch.
			self.home_orig_type = self.spaces[self.home_space].type
			self.set_space(self.home_space, type = TYPE_CARTESIAN)
			self.home_phase = 1
			self.home_motors = [(i, self.spaces[self.home_space].axis[i], m) for i, m in enumerate(self.spaces[self.home_space].motor) if m['home_order'] == self.home_order]
			self.home_target = {}
			self.limits[self.home_space].clear()
			self.sense[self.home_space].clear()
			for i, a, m in self.home_motors:
				if self.pin_valid(m['limit_max_pin']) or (not self.pin_valid(m['limit_min_pin']) and self.pin_valid(m['sense_pin'])):
					dist = m['home_pos'] + 1000	#TODO: use better value.
					self.spaces[self.home_space].set_current_pos(i, a['max'] - dist)
					self.home_target[i] = a['max'] - self.spaces[self.home_space].axis[i]['offset']
			if len(self.home_target) > 0:
				self.home_cb[0] = [(self.home_space, k) for k in self.home_target.keys()]
				if self.home_cb not in self.movecb:
					self.movecb.append(self.home_cb)
				#log("N t %s" % (self.home_target))
				self.goto({self.home_space: self.home_target}, cb = True)[1](None)
				return
			# Fall through.
		if self.home_phase == 1:
			# Continue moving up to find limit or sense switch.
			if len(self.sense[self.home_space]) > 0:
				self.pause(True, False)	# Needed if we hit a sense switch.
			found_limits = False
			for a in self.limits[self.home_space].keys() + self.sense[self.home_space].keys():
				if a in self.home_target:
					self.home_target.pop(a)
					found_limits = True
			# Repeat until move is done, or all limits are hit.
			if (not done or found_limits) and len(self.home_target) > 0:
				self.home_cb[0] = [(self.home_space, k) for k in self.home_target.keys()]
				if self.home_cb not in self.movecb:
					self.movecb.append(self.home_cb)
				#log("0 t %s" % (self.home_target))
				self.goto({self.home_space: self.home_target}, cb = True)[1](None)
				return
			#log('done 1')
			self.home_phase = 2
			# Move down to find limit or sense switch.
			for i, a, m in self.home_motors:
				if (i not in self.limits[self.home_space] and self.pin_valid(m['limit_min_pin'])) or (i not in self.sense[self.home_space] and self.pin_valid(m['sense_pin'])):
					dist = 1000 - m['home_pos']	# TODO: use better value.
					self.spaces[self.home_space].set_current_pos(i, a['min'] + dist)
					self.home_target[i] = a['min'] - self.spaces[self.home_space].axis[i]['offset']
			if len(self.home_target) > 0:
				self.home_cb[0] = [(self.home_space, k) for k in self.home_target.keys()]
				if self.home_cb not in self.movecb:
					self.movecb.append(self.home_cb)
				#log("1 t %s" % (self.home_target))
				self.goto({self.home_space: self.home_target}, cb = True)[1](None)
				return
			# Fall through.
		if self.home_phase == 2:
			# Continue moving down to find limit or sense switch.
			if len(self.sense[self.home_space]) > 0:
				self.pause(True, False)	# Needed if we hit a sense switch.
			found_limits = False
			for a in self.limits[self.home_space].keys() + self.sense[self.home_space].keys():
				if a in self.home_target:
					self.home_target.pop(a)
					found_limits = True
			# Repeat until move is done, or all limits are hit.
			if (not done or found_limits) and len(self.home_target) > 0:
				self.home_cb[0] = [(self.home_space, k) for k in self.home_target.keys()]
				if self.home_cb not in self.movecb:
					self.movecb.append(self.home_cb)
				#log("2 t %s" % (self.home_target))
				self.goto({self.home_space: self.home_target}, cb = True)[1](None)
				return
			if len(self.home_target) > 0:
				log('Warning: not all limits were found during homing')
			self.home_phase = 3
			# Fall through.
		if self.home_phase == 3:
			# Move away slowly.
			self.home_phase = 4
			# self.home_space
			# self.home_motors
			data = ''
			num = 0
			for s, spc in enumerate(self.spaces):
				if s != self.home_space:
					data += '\x03' * len(spc.motor)
				else:
					for m in spc.motor:
						if m['home_order'] != self.home_order:
							data += '\x03'
						elif self.pin_valid(m['limit_max_pin']) or (not self.pin_valid(m['limit_min_pin']) and self.pin_valid(m['sense_pin'])):
							data += '\x01'
							num += 1
						elif self.pin_valid(m['limit_min_pin']):
							data += '\x00'
							num += 1
						else:
							data += '\x03'
			self.home_phase = 4
			if num > 0:
				dprint('homing', data)
				self._send_packet(chr(protocol.command['HOME']) + data)
				return
			# Fall through.
		if self.home_phase == 4:
			self.home_phase = 5
			# Move delta to center.
			if self.home_orig_type == TYPE_DELTA:
				# Goto center.  Since this is only for deltas, assume that switches are not at minimum travel.
				self.home_delta_target = float('nan')
				for i, a, m in self.home_motors:
					if math.isnan(self.home_delta_target) or m['home_pos'] < self.home_delta_target:
						self.home_delta_target = m['home_pos']
				target = {}
				for i, a, m in self.home_motors:
					if i in self.sense[self.home_space]:
						# Correct for possible extra steps that were done between hitting the sensor and pausing.
						self.spaces[self.home_space].set_current_pos(i, a['min'] + m['home_pos'] - self.home_delta_target + self.sense[self.home_space][i][-1][1] - self.spaces[self.home_space].get_current_pos(i))
					else:
						self.spaces[self.home_space].set_current_pos(i, a['min'] + m['home_pos'] - self.home_delta_target)
					target[i] = a['min'] - self.spaces[self.home_space].axis[i]['offset']
				if len(target) > 0:
					self.home_cb[0] = False
					if self.home_cb not in self.movecb:
						self.movecb.append(self.home_cb)
					self.goto({self.home_space: target}, cb = True)[1](None)
					return
			# Fall through.
		if self.home_phase == 5:
			# set current position.
			for i, a, m in self.home_motors:
				if i in self.limits[self.home_space] or i not in self.sense[self.home_space]:
					if self.home_orig_type == TYPE_DELTA:
						if not math.isnan(self.home_delta_target):
							self.spaces[self.home_space].set_current_pos(i, self.home_delta_target)
					else:
						if not math.isnan(m['home_pos']):
							self.spaces[self.home_space].set_current_pos(i, m['home_pos'])
				else:
					# Correct for possible extra steps that were done because pausing happened later than hitting the sensor (only on cartesian; delta was done above).
					if self.home_orig_type == TYPE_DELTA:
						if not math.isnan(self.home_delta_target):
							self.spaces[self.home_space].set_current_pos(i, self.home_delta_target + self.sense[self.home_space][i][-1][1] - self.spaces[self.home_space].get_current_pos(i))
					else:
						if not math.isnan(m['home_pos']):
							self.spaces[self.home_space].set_current_pos(i, m['home_pos'] + self.sense[self.home_space][i][-1][1] - self.spaces[self.home_space].get_current_pos(i))
			n = [m['home_order'] for m in self.spaces[self.home_space].motor if m['home_order'] > self.home_order]
			if len(n) > 0:
				self.home_phase = 0
				self.home_order = min(n)
				return self._do_home()
			self.home_phase = 6
			# Fall through
		if self.home_phase == 6:
			self.set_space(self.home_space, type = self.home_orig_type)
			target = {}
			for i, a in enumerate(self.spaces[self.home_space].axis):
				current = self.spaces[self.home_space].get_current_pos(i)
				if current > a['max'] - a['offset']:
					target[i] = a['max'] - a['offset']
				elif current < a['min'] - a['offset']:
					target[i] = a['min'] - a['offset']
			self.home_phase = 7
			if len(target) > 0:
				self.home_cb[0] = False
				if self.home_cb not in self.movecb:
					self.movecb.append(self.home_cb)
				#log('target: %s' % repr(target))
				self.goto({self.home_space: target}, cb = True)[1](None)
				return
			# Fall through.
		if self.home_phase == 7:
			self.home_phase = None
			if len(self.home_spaces) > 0:
				return self._do_home()
			self.position_valid = True
			if self.home_id is not None:
				self._send(self.home_id, 'return', None)
			if self.home_done_cb is not None:
				call_queue.append((self.home_done_cb, []))
				self.home_done_cb = None
			return
		log('Internal error: invalid home phase')
	# }}}
	def _do_probe(self, id, x, y, z, angle, speed, phase = 0, good = True): # {{{
		log('probe %d' % phase)
		# Map = [[x0, y0, x1, y1], [nx, ny], [[...], [...], ...]]
		if not good:
			# This means the probe has been aborted.
			self.probing = False
			if id is not None:
				self._send(id, 'error', 'aborted')
			return
		self.probing = True
		if not self.position_valid:
			self.home(cb = lambda: self._do_probe(id, x, y, z, angle, speed, phase, good), abort = False)[1](None)
			return
		p = self.jobs_probemap
		if phase == 0:
			if y > p[1][1]:
				# Done.
				self.probing = False
				if id is not None:
					self._send(id, 'return', self.jobs_probemap)
				else:
					for y, c in enumerate(p[2]):
						for x, o in enumerate(c):
							log('map %f %f %f' % (p[0][0] + (p[0][2] - p[0][0]) * x / p[1][0], p[0][1] + (p[0][3] - p[0][1]) * y / p[1][1], o))
					#log('result: %s' % repr(self.jobs_probemap))
					if len(self.jobs_active) == 1:
						def cb():
							self.request_confirmation("Probing done; prepare for job.")[1](False)
							self._next_job()
						self.park(cb = cb, abort = False)[1](None)
					else:
						self._next_job()
				return
			# Goto x,y
			self.probe_cb[1] = lambda good: self._do_probe(id, x, y, z, angle, speed, 1, good)
			self.movecb.append(self.probe_cb)
			px = p[0][0] + (p[0][2] - p[0][0]) * x / p[1][0]
			py = p[0][1] + (p[0][3] - p[0][1]) * y / p[1][1]
			self.goto([[px * self.gcode_angle[1] - py * self.gcode_angle[0], py * self.gcode_angle[1] + px * self.gcode_angle[0]]], cb = True)[1](None)
		elif phase == 1:
			# Probe
			self.probe_cb[1] = lambda good: self._do_probe(id, x, y, z, angle, speed, 2, good)
			if self.pin_valid(self.probe_pin):
				self.movecb.append(self.probe_cb)
				z_low = self.spaces[0].axis[2]['min']
				self.goto([{2: z_low}], f0 = float(speed) / (z - z_low) if z > z_low else float('inf'), cb = True, probe = True)[1](None)
			else:
				self.request_confirmation('Please move the tool to the surface')[1](False)
		else:
			# Record result
			z = self.spaces[0].get_current_pos(2)
			p[2][y][x] = z + self.spaces[0].axis[2]['offset']
			z += self.probe_safe_dist
			if y & 1:
				x -= 1
				if x < 0:
					x = 0
					y += 1
			else:
				x += 1
				if x > p[1][0]:
					x = p[1][0]
					y += 1
			self.probe_cb[1] = lambda good: self._do_probe(id, x, y, z, angle, speed, 0, good)
			self.movecb.append(self.probe_cb)
			# Retract
			self.goto([{2: z}], cb = True)[1](None)
	# }}}
	def _next_job(self): # {{{
		self.job_current += 1
		if self.job_current >= len(self.jobs_active):
			self._print_done(True, 'Queue finished')
			return
		if len(self.jobs_active) > 1:
			def cb():
				self.request_confirmation("Prepare for job '%s'." % self.jobs_active[self.job_current])[1](False)
			self.gcode_parking = True
			self.park(cb = cb, abort = False)[1](None)
		self.gcode_id = None
		self._gcode_run(self.jobs_active[self.job_current], self.jobs_ref, self.jobs_angle, self.jobs_probemap, abort = False)
	# }}}
	def _gcode_run(self, src, ref = (0, 0, 0), angle = 0, probemap = None, abort = True): # {{{
		if not self.position_valid:
			self.park(cb = lambda: self._gcode_run(src, ref, angle, probemap, abort), abort = False)[1](None)
			return
		self.gcode_ref = ref
		angle = math.radians(angle)
		self.gcode_angle = math.sin(angle), math.cos(angle)
		self.gcode_probemap = probemap
		if self.bed_id < len(self.temps):
			self.btemp = self.temps[self.bed_id].value
		else:
			self.btemp = float('nan')
		if abort:
			self._unpause()
			self._print_done(False, 'aborted by starting new print')
		self.queue_info = None
		# Disable all alarms.
		for i in range(len(self.temps)):
			self.waittemp(i, None, None)
		self.paused = False
		self._globals_update()
		self.sleep(False)
		if len(self.spaces) > 1:
			for e in range(len(self.spaces[1].axis)):
				self.set_axis_pos(1, e, 0)
		filename = fhs.read_spool(src + os.extsep + 'bin', opened = False)
		if probemap is not None:
			# Cdriver can't handle this; do it from here.
			self.gcode_fd = os.open(filename, os.O_RDONLY)
			self.gcode_map = mmap.mmap(self.gcode_fd, 0, access = mmap.ACCESS_READ)
			size = self.gcode_map.size()
			numpos = size - 7 * 4
			num_strings = struct.unpack('<L', self.gcode_map[numpos:numpos + 4])[0]
			strlen = [None] * num_strings
			self.gcode_strings = [None] * num_strings
			all_strs = 0
			for i in range(num_strings):
				pos = numpos - (num_strings - i) * 4
				strlen[i] = struct.unpack('<L', self.gcode_map[pos:pos + 4])[0]
				all_strs += strlen[i]
			pos = numpos - num_strings * 4
			self.gcode_num_records = pos / (11 * 4 + 1)
			self.gcode_current_record = 0
			for i in range(num_strings):
				self.gcode_strings[i] = self.gcode_map[pos:pos + strlen[i]]
				pos += strlen[i]
			if self.gcode_num_records <= 0:
				log('nothing to run')
				self._gcode_close()
				if id is not None:
					self._send(id, 'return', False, 'nothing to run')
				self.paused = None
				self._globals_update()
				return
			call_queue.append((self._do_gcode, []))
		else:
			# Let cdriver do the work.
			self.gcode_file = True
			self._globals_update()
			self._send_packet(struct.pack('<Bfffff', protocol.command['RUN_FILE'], ref[0], ref[1], ref[2], self.gcode_angle[0], self.gcode_angle[1]) + filename.encode('utf8'))
	# }}}
	def _audio_play(self): # {{{
		if self.audiofile is None:
			if self.audioid is not None:
				self._send(self.audioid, 'return', True)
			return
		while not self.waitaudio:
			data = self.audiofile.read(self.audio_fragment_size)
			if len(data) < self.audio_fragment_size:
				self.audiofile = None
				if self.audioid is not None:
					self._send(self.audioid, 'return', True)
				return
			self._send_packet(chr(protocol.command['AUDIO_DATA']) + data, audio = True)
	# }}}
	# Subclasses.  {{{
	class Space: # {{{
		def __init__(self, printer, id):
			self.name = 'Space %d' % id
			self.printer = printer
			self.id = id
			self.axis = []
			self.motor = []
			self.delta = [{'axis_min': 0., 'axis_max': 0., 'rodlength': 0., 'radius': 0.} for t in range(3)]
			self.delta_angle = 0
			self.extruder = []
		def read(self, data):
			axes, motors = data
			if self.id == 1:
				self.printer.multipliers = (self.printer.multipliers + [1.] * len(axes))[:len(axes)]
			if len(axes) > len(self.axis):
				self.axis += [{'name': 'Axis %d' % i} for i in range(len(self.axis), len(axes))]
			else:
				self.axis[len(axes):] = []
			for a in range(len(axes)):
				self.axis[a]['offset'], self.axis[a]['park'], self.axis[a]['park_order'], self.axis[a]['max_v'], self.axis[a]['min'], self.axis[a]['max'] = struct.unpack('<ffBfff', axes[a])
				if self.id == 0 and a == 2:
					self.axis[a]['offset'] -= self.printer.zoffset
			if len(motors) > len(self.motor):
				self.motor += [{'name': 'Motor %d' % i} for i in range(len(self.motor), len(motors))]
			else:
				self.motor[len(motors):] = []
			for m in range(len(motors)):
				self.motor[m]['step_pin'], self.motor[m]['dir_pin'], self.motor[m]['enable_pin'], self.motor[m]['limit_min_pin'], self.motor[m]['limit_max_pin'], self.motor[m]['sense_pin'], self.motor[m]['steps_per_unit'], self.motor[m]['max_steps'], self.motor[m]['home_pos'], self.motor[m]['limit_v'], self.motor[m]['limit_a'], self.motor[m]['home_order'] = struct.unpack('<HHHHHHfBfffB', motors[m])
				if self.id == 1 and m < len(self.printer.multipliers):
					self.motor[m]['steps_per_unit'] /= self.printer.multipliers[m]
		def write_info(self, num_axes = None):
			data = struct.pack('<Bf', self.type, self.max_deviation)
			if self.type == TYPE_CARTESIAN:
				data += struct.pack('<B', num_axes if num_axes is not None else len(self.axis))
			elif self.type == TYPE_DELTA:
				for a in range(3):
					data += struct.pack('<ffff', self.delta[a]['axis_min'], self.delta[a]['axis_max'], self.delta[a]['rodlength'], self.delta[a]['radius'])
				data += struct.pack('<f', self.delta_angle)
			elif self.type == TYPE_EXTRUDER:
				num = num_axes if num_axes is not None else len(self.axis)
				data += struct.pack('<B', num)
				for a in range(num):
					if a < len(self.extruder):
						data += struct.pack('<fff', self.extruder[a]['dx'], self.extruder[a]['dy'], self.extruder[a]['dz'])
					else:
						data += struct.pack('<fff', 0, 0, 0)
			else:
				log('invalid type')
				raise AssertionError('invalid space type')
			return data
		def write_axis(self, axis):
			return struct.pack('<ffBfff', self.axis[axis]['offset'] + (self.printer.zoffset if self.id == 0 and axis == 2 else 0), self.axis[axis]['park'], self.axis[axis]['park_order'], self.axis[axis]['max_v'], self.axis[axis]['min'], self.axis[axis]['max'])
		def write_motor(self, motor):
			return struct.pack('<HHHHHHfBfffB', self.motor[motor]['step_pin'], self.motor[motor]['dir_pin'], self.motor[motor]['enable_pin'], self.motor[motor]['limit_min_pin'], self.motor[motor]['limit_max_pin'], self.motor[motor]['sense_pin'], self.motor[motor]['steps_per_unit'] * (1. if self.id != 1 or motor >= len(self.printer.multipliers) else self.printer.multipliers[motor]), self.motor[motor]['max_steps'], self.motor[motor]['home_pos'], self.motor[motor]['limit_v'], self.motor[motor]['limit_a'], self.motor[motor]['home_order'])
		def set_current_pos(self, axis, pos):
			#log('setting pos of %d %d to %f' % (self.id, axis, pos))
			self.printer._send_packet(struct.pack('<BBBf', protocol.command['SETPOS'], self.id, axis, pos))
		def get_current_pos(self, axis):
			self.printer._send_packet(struct.pack('<BBB', protocol.command['GETPOS'], self.id, axis))
			cmd, s, m, f, e, data = self.printer._get_reply()
			assert cmd == protocol.rcommand['POS']
			#log('get current pos %d %d: %f' % (self.id, axis, f))
			return f
		def export(self):
			std = [self.name, self.type, self.max_deviation, [[a['name'], a['offset'], a['park'], a['park_order'], a['max_v'], a['min'], a['max']] for a in self.axis], [[m['name'], m['step_pin'], m['dir_pin'], m['enable_pin'], m['limit_min_pin'], m['limit_max_pin'], m['sense_pin'], m['steps_per_unit'], m['max_steps'], m['home_pos'], m['limit_v'], m['limit_a'], m['home_order']] for m in self.motor], None if self.id != 1 else self.printer.multipliers]
			if self.type == TYPE_CARTESIAN:
				return std
			elif self.type == TYPE_DELTA:
				return std + [[[a['axis_min'], a['axis_max'], a['rodlength'], a['radius']] for a in self.delta] + [self.delta_angle]]
			elif self.type == TYPE_EXTRUDER:
				return std + [[[a['dx'], a['dy'], a['dz']] for a in self.extruder]]
			else:
				log('invalid type')
				raise AssertionError('invalid space type')
		def export_settings(self):
			ret = '[space %d]\r\n' % self.id
			ret += 'name = %s\r\n' % self.name
			ret += 'type = %d\r\n' % self.type
			ret += 'max_deviation = %d\r\n' % self.max_deviation
			if self.type == TYPE_CARTESIAN:
				ret += 'num_axes = %d\r\n' % len(self.axis)
			elif self.type == TYPE_DELTA:
				ret += 'delta_angle = %f\r\n' % self.delta_angle
				for i in range(3):
					ret += '[delta %d %d]\r\n' % (self.id, i)
					ret += ''.join(['%s = %f\r\n' % (x, self.delta[i][x]) for x in ('rodlength', 'radius', 'axis_min', 'axis_max')])
			elif self.type == TYPE_EXTRUDER:
				ret += 'num_axes = %d\r\n' % len(self.axis)
				for i in range(len(self.extruder)):
					ret += '[extruder %d %d]\r\n' % (self.id, i)
					ret += ''.join(['%s = %f\r\n' % (x, self.extruder[i][x]) for x in ('dx', 'dy', 'dz')])
			for i, a in enumerate(self.axis):
				ret += '[axis %d %d]\r\n' % (self.id, i)
				ret += 'name = %s\r\n' % a['name']
				ret += ''.join(['%s = %f\r\n' % (x, a[x]) for x in ('offset', 'park', 'park_order', 'max_v', 'min', 'max')])
			for i, m in enumerate(self.motor):
				ret += '[motor %d %d]\r\n' % (self.id, i)
				ret += 'name = %s\r\n' % m['name']
				ret += ''.join(['%s = %s\r\n' % (x, write_pin(m[x])) for x in ('step_pin', 'dir_pin', 'enable_pin', 'limit_min_pin', 'limit_max_pin', 'sense_pin')])
				ret += ''.join(['%s = %d\r\n' % (x, m[x]) for x in ('max_steps', 'home_order')])
				ret += ''.join(['%s = %f\r\n' % (x, m[x]) for x in ('steps_per_unit', 'home_pos', 'limit_v', 'limit_a')])
			return ret
	# }}}
	class Temp: # {{{
		def __init__(self, id):
			self.name = 'Temp %d' % id
			self.id = id
			self.value = float('nan')
		def read(self, data):
			self.R0, self.R1, logRc, Tc, self.beta, self.heater_pin, self.fan_pin, self.thermistor_pin, fan_temp = struct.unpack('<fffffHHHf', data)
			try:
				self.Rc = math.exp(logRc)
			except:
				self.Rc = float('nan')
			self.Tc = Tc - C0
			self.fan_temp = fan_temp - C0
			self.fan_pin ^= 0x200
		def write(self):
			try:
				logRc = math.log(self.Rc)
			except:
				logRc = float('nan')
			return struct.pack('<fffffHHHf', self.R0, self.R1, logRc, self.Tc + C0, self.beta, self.heater_pin, self.fan_pin ^ 0x200, self.thermistor_pin, self.fan_temp + C0)
		def export(self):
			return [self.name, self.R0, self.R1, self.Rc, self.Tc, self.beta, self.heater_pin, self.fan_pin, self.thermistor_pin, self.fan_temp, self.value]
		def export_settings(self):
			ret = '[temp %d]\r\n' % self.id
			ret += 'name = %s\r\n' % self.name
			ret += ''.join(['%s = %s\r\n' % (x, write_pin(getattr(self, x))) for x in ('heater_pin', 'fan_pin', 'thermistor_pin')])
			ret += ''.join(['%s = %f\r\n' % (x, getattr(self, x)) for x in ('fan_temp', 'R0', 'R1', 'Rc', 'Tc', 'beta')])
			return ret
	# }}}
	class Gpio: # {{{
		def __init__(self, id):
			self.name = 'Gpio %d' % id
			self.id = id
			self.state = 3
			self.reset = 3
		def read(self, data):
			self.pin, state, = struct.unpack('<HB', data)
			self.state = state & 0x3
			self.reset = (state >> 2) & 0x3
		def write(self):
			return struct.pack('<HB', self.pin, self.state | (self.reset << 2))
		def export(self):
			return [self.name, self.pin, self.state, self.reset]
		def export_settings(self):
			ret = '[gpio %d]\r\n' % self.id
			ret += 'name = %s\r\n' % self.name
			ret += 'pin = %s\r\n' % write_pin(self.pin)
			ret += 'reset = %d\r\n' % self.reset
			return ret
	# }}}
	# }}}
	# }}}
	# Useful commands.  {{{
	def reset(self): # {{{
		log('%s resetting and dying.' % self.uuid)
		self._send_packet(struct.pack('<BB', protocol.command['RESET'], 0))
	# }}}
	def die(self, reason): # {{{
		log('%s dying as requested by host (%s).' % (self.uuid, reason))
		return (WAIT, WAIT)
	# }}}
	@delayed
	def flush(self, id): # {{{
		#log('flush start')
		def cb(w):
			#log('flush done')
			if id is not  None:
				self._send(id, 'return', w)
			call_queue.append((self._do_gcode, []))
		self.movecb.append((False, cb))
		if self.flushing is not True:
			self.goto(cb = True)[1](None)
		#log('end flush preparation')
	# }}}
	@delayed
	def probe(self, id, area, angle = 0, speed = 3): # {{{
		if len(self.spaces) < 1 or len(self.spaces[0].axis) < 3 or not self.probe_safe_dist > 0:
			if id is not None:
				self._send(id, 'return', None)
			return
		density = [int(abs(area[t + 2] - area[t]) / self.probe_dist) + 1 for t in range(2)]
		self.jobs_probemap = [area, density, [[None for x in range(density[0] + 1)] for y in range(density[1] + 1)]]
		self.gcode_angle = math.sin(angle), math.cos(angle)
		#log(repr(self.jobs_probemap))
		self._do_probe(id, 0, 0, self.get_axis_pos(0, 2), angle, speed)
	# }}}
	@delayed
	def goto(self, id, moves = (), f0 = None, f1 = None, cb = False, probe = False): # {{{
		#log('goto %s %s %s %d %d' % (repr(moves), f0, f1, cb, probe))
		#log('speed %s' % f0)
		#traceback.print_stack()
		self.queue.append((id, moves, f0, f1, cb, probe))
		if not self.wait:
			self._do_queue()
	# }}}
	@delayed
	def gotocb(self, id, moves = (), f0 = None, f1 = None): # {{{
		self.queue.append((None, moves, f0, f1, True))
		if not self.wait:
			self._do_queue()
		self.wait_for_cb(False)[1](id)
	# }}}
	def sleep(self, sleeping = True, update = True): # {{{
		if sleeping:
			self.position_valid = False
			if update:
				self._globals_update()
		self._send_packet(struct.pack('<BB', protocol.command['SLEEP'], sleeping))
	# }}}
	def settemp(self, channel, temp, update = True): # {{{
		channel = int(channel)
		self.temps[channel].value = temp
		if update:
			self._temp_update(channel)
		self._send_packet(struct.pack('<BBf', protocol.command['SETTEMP'], channel, temp + C0 if not math.isnan(self.temps[channel].beta) else temp))
		if self.gcode_waiting > 0 and any(channel == x[0] for x in self.tempcb):
			self.waittemp(channel, temp)
	# }}}
	def waittemp(self, channel, min, max = None): # {{{
		channel = int(channel)
		if min is None:
			min = float('nan')
		if max is None:
			max = float('nan')
		self._send_packet(struct.pack('<BBff', protocol.command['WAITTEMP'], channel, min + C0 if not math.isnan(self.temps[channel].beta) else min, max + C0 if not math.isnan(self.temps[channel].beta) else max))
	# }}}
	def readtemp(self, channel): # {{{
		channel = int(channel)
		if channel >= len(self.temps):
			log('Trying to read invalid temp %d' % channel)
			return float('nan')
		self._send_packet(struct.pack('<BB', protocol.command['READTEMP'], channel))
		cmd, s, m, f, e, data = self._get_reply()
		assert cmd == protocol.rcommand['TEMP']
		return f - (C0 if not math.isnan(self.temps[channel].beta) else 0)
	# }}}
	def readpower(self, channel): # {{{
		channel = int(channel)
		if channel >= len(self.temps):
			log('Trying to read invalid power %d' % channel)
			return float('nan')
		self._send_packet(struct.pack('<BB', protocol.command['READPOWER'], channel))
		cmd, s, m, f, e, data = self._get_reply()
		assert cmd == protocol.rcommand['POWER']
		return s, m
	# }}}
	def readpin(self, pin): # {{{
		self._send_packet(struct.pack('<BB', protocol.command['READPIN'], pin))
		cmd, s, m, f, e, data = self._get_reply()
		assert cmd == protocol.rcommand['PIN']
		return bool(s)
	# }}}
	def load(self, profile = None, update = True): # {{{
		filenames = fhs.read_data(os.path.join(self.uuid, 'profiles', ((profile and profile.strip()) or self.profile) + os.extsep + 'ini'), opened = False, multiple = True)
		if profile and self.profile != profile.strip():
			#log('setting profile to %s' % profile.strip())
			self.profile = profile.strip()
			if update:
				self._globals_update()
		if len(filenames) > 0:
			with open(filenames[0]) as f:
				self.import_settings(f.read(), update = update)
	# }}}
	def save(self, profile = None): # {{{
		if profile and self.profile != profile.strip():
			log('setting profile to %s' % profile.strip())
			self.profile = profile.strip()
			self._globals_update()
		with fhs.write_data(os.path.join(self.uuid, 'profiles', (profile.strip() or self.profile) + os.extsep + 'ini')) as f:
			f.write(self.export_settings())
	# }}}
	def list_profiles(self): # {{{
		dirnames = fhs.read_data(os.path.join(self.uuid, 'profiles'), dir = True, multiple = True, opened = False)
		ret = []
		for d in dirnames:
			for f in os.listdir(d):
				name = os.path.splitext(f)[0].strip()
				if name not in ret:
					ret.append(name)
		ret.sort()
		return ret
	# }}}
	def remove_profile(self, profile): # {{{
		filename = fhs.write_data(os.path.join(self.uuid, 'profiles', (profile.strip() or self.profile) + os.extsep + 'ini'), opened = False)
		if os.path.exists(filename):
			os.unlink(filename)
			return True
		return False
	# }}}
	def set_default_profile(self, profile): # {{{
		with fhs.write_data(os.path.join(self.uuid, 'profile')) as f:
			f.write(profile.strip() + '\n')
	# }}}
	def abort(self): # {{{
		self.pause();
		for t, temp in enumerate(self.temps):
			self.settemp(t, float('nan'))
		self.sleep();
		for g, gpio in enumerate(self.gpios):
			self.set_gpio(g, state = gpio.reset)
	# }}}
	def pause(self, pausing = True, store = True, update = True): # {{{
		was_paused = self.paused
		if pausing:
			self._send_packet(struct.pack('<BB', protocol.command['QUEUED'], True))
			cmd, s, m, f, e, data = self._get_reply()
			if cmd != protocol.rcommand['QUEUE']:
				log('invalid reply to queued command')
				return
			self.movewait = 0
			self.wait = False
		self.paused = pausing
		if not self.paused:
			if was_paused:
				# Go back to pausing position.
				# First reset all axes that don't have a limit switch.
				for i, sp in enumerate(self.queue_info[1]):
					for a, pos in enumerate(sp):
						# Assume motor[a] corresponds to axis[a] if it exists.
						if len(self.spaces[i].motor) > a and not self.pin_valid(self.spaces[i].motor[a]['limit_max_pin']) and not self.pin_valid(self.spaces[i].motor[a]['limit_min_pin']):
							self.set_axis_pos(i, a, pos)
				self.goto(self.queue_info[1])
				# TODO: adjust extrusion of current segment to shorter path length.
				log('resuming')
				self.resuming = True
			else:
				log('sending resume')
				self._send_packet(chr(protocol.command['RESUME']))
			self._do_queue()
		else:
			#log('pausing')
			if not was_paused:
				#log('pausing %d %d %d %d %d' % (store, self.queue_info is None, len(self.queue), self.queue_pos, s))
				if store and self.queue_info is None and ((len(self.queue) > 0 and self.queue_pos - s >= 0) or self.gcode_file):
					if self.home_phase is not None:
						#log('killing homer')
						self.home_phase = None
						self.set_space(self.home_space, type = self.home_orig_type)
						if self.home_cb in self.movecb:
							self.movecb.remove(self.home_cb)
							if self.home_id is not None:
								self._send(self.home_id, 'return', None)
					if self.probe_cb in self.movecb:
						#log('killing prober')
						self.movecb.remove(self.probe_cb)
						self.probe_cb(False)
					log('pausing gcode %d/%d/%d' % (self.queue_pos, s, len(self.queue)))
					if self.flushing is None:
						self.flushing = False
					self.queue_info = [len(self.queue) if self.gcode_file else self.queue_pos - s, [[s.get_current_pos(a) for a in range(len(s.axis))] for s in self.spaces], self.queue, self.movecb, self.flushing, self.gcode_wait]
				else:
					log('stopping')
					self.paused = False
					if len(self.movecb) > 0:
						call_queue.extend([(x[1], [True]) for x in self.movecb])
				self.queue = []
				self.movecb = []
				self.flushing = False
				self.gcode_wait = None
				self.queue_pos = 0
		if update:
			self._globals_update()
	# }}}
	def queued(self): # {{{
		self._send_packet(struct.pack('<BB', protocol.command['QUEUED'], False))
		cmd, s, m, f, e, data = self._get_reply()
		if cmd != protocol.rcommand['QUEUE']:
			log('invalid reply to queued command')
			return None
		return s
	# }}}
	@delayed
	def home(self, id, spaces = None, speed = 5, cb = None, abort = True): # {{{
		#log('homing')
		if self.home_phase is not None and not self.paused:
			log("ignoring request to home because we're already homing")
			if id is not None:
				self._send(id, 'return', None)
			return
		# Abort only if it is requested, and the job is not paused.
		if abort and self.queue_info is None:
			self._print_done(False, 'aborted by homing')
		self.home_id = id
		self.home_speed = speed
		self.home_done_cb = cb
		if spaces is None:
			self.home_spaces = range(len(self.spaces))
		else:
			self.home_spaces = list(spaces)
		#log('homing %s' % repr(self.home_spaces))
		self._do_home()
	# }}}
	@delayed
	def park(self, id, cb = None, abort = True, order = 0, aborted = False): # {{{
		if aborted:
			if id is not None:
				self._send(id, 'error', 'aborted')
			return
		#log('parking')
		if abort:
			self._print_done(False, 'aborted by parking')
		if not self.position_valid:
			#log('homing')
			self.home(cb = lambda: self.park(cb, abort = False)[1](id), abort = False)[1](None)
			return
		next_order = None
		for s in self.spaces:
			topark = [a['park_order'] for a in s.axis if not math.isnan(a['park']) and a['park_order'] >= order]
			if len(topark) > 0 and (next_order is None or min(topark) > next_order):
				next_order = min(topark)
		if next_order is None:
			self.gcode_parking = False
			if cb:
				call_queue.append((cb, []))
			if id is not None:
				self._send(id, 'return', None)
			return
		self.movecb.append((False, lambda done: self.park(cb, False, next_order + 1, not done)[1](id)))
		self.goto([[a['park'] - a['offset'] if a['park_order'] == next_order else float('nan') for a in s.axis] for s in self.spaces], cb = True)[1](None)
	# }}}
	@delayed
	def audio_play(self, id, name, motors = None): # {{{
		log('motors: %s' % repr(motors))
		assert os.path.basename(name) == name
		filename = os.path.join(self.audiodir, name)
		self.audiofile = open(filename, 'rb')
		channels = [0] *(((sum([len(s.motor) for s in self.spaces]) - 1) >> 3) + 1)
		m0 = 0
		for i, s in enumerate(self.spaces):
			for m in range(len(s.motor)):
				log('checking %s' % repr([i, m]))
				if motors is None or [i, m] in motors:
					log('yes')
					channels[(m0 + m) >> 3] |= 1 <<((m0 + m) & 0x7)
			m0 += len(s.motor)
		us_per_bit = self.audiofile.read(2)
		self._send_packet(chr(protocol.command['AUDIO_SETUP']) + us_per_bit + ''.join([chr(x) for x in channels]))
		self.audio_id = id
		self._audio_play()
	# }}}
	def audio_load(self, name, data): # {{{
		assert os.path.basename(name) == name
		wav = wave.open(io.BytesIO(base64.b64decode(data)))
		assert wav.getnchannels() == 1
		data = [ord(x) for x in wav.readframes(wav.getnframes())]
		# Data is 16 bit signed ints per channel, but it is read as bytes.  First convert it to 16 bit numbers.
		data = [(h << 8) + l if h < 128 else(h << 8) + l -(1 << 16) for l, h in zip(data[::2], data[1::2])]
		# Determine minimum and scale.
		minimum = min(data)
		maximum = max(data)
		scale = maximum - minimum + 1;
		s = ''
		for t in range(0, len(data) - 7, 8):
			c = 0
			for b in range(8):
				c += ((data[t + b] - minimum) * 2 // scale) << b
			s += chr(c)
		filename = os.path.join(self.audiodir, name)
		with open(filename, 'wb') as f:
			f.write(struct.pack('<H', 1000000 // wav.getframerate()) + s)
	# }}}
	def audio_list(self): # {{{
		ret = []
		if not os.path.exists(self.audiodir):
			return ret
		for x in os.listdir(self.audiodir):
			try:
				with open(os.path.join(self.audiodir, x), 'rb') as f:
					us_per_bit = struct.unpack('<H', f.read(2))[0]
					bits = (os.fstat(f.fileno()).st_size - 2) * 8
					t = bits * us_per_bit * 1e-6
					ret.append((x, t))
			except:
				pass
		return ret
	# }}}
	@delayed
	def wait_for_cb(self, id, sense = False): # {{{
		ret = lambda w: id is None or self._send(id, 'return', w)
		if self.movewait == 0 or sense is not False and sense[1] in self.sense[sense[0]]:
			#log('not delaying with wait_for_cb, because there is no cb waiting')
			ret(self.movewait == 0)
		else:
			#log('waiting for cb')
			self.movecb.append((sense, ret))
	# }}}
	def waiting_for_cb(self): # {{{
		return self.movewait > 0
	# }}}
	@delayed
	def wait_for_temp(self, id, which = None): # {{{
		def cb():
			if id is not None:
				self._send(id, 'return', None)
				return
			self.gcode_waiting -= 1
			call_queue.append((self._do_gcode, []))
		if(which is None and len(self.alarms) > 0) or which in self.alarms:
			cb()
		else:
			self.tempcb.append((which, cb))
	# }}}
	def clear_alarm(self, which = None): # {{{
		if which is None:
			self.alarms.clear()
		else:
			self.alarms.discard(which)
	# }}}
	def get_limits(self, space, motor = None):	# {{{
		if motor is None:
			return self.limits[space]
		if motor in self.limits[space]:
			return self.limits[space][motor]
		return None
	# }}}
	def clear_limits(self):	# {{{
		for s in range(len(self.spaces)):
			self.limits[s].clear()
	# }}}
	def get_sense(self, space, motor = None):	# {{{
		if motor is None:
			return self.sense[space]
		if motor in self.sense[space]:
			return self.sense[space][motor]
		return []
	# }}}
	def clear_sense(self):	# {{{
		for s in range(len(self.spaces)):
			self.sense[s].clear()
	# }}}
	def pin_valid(self, pin):	# {{{
		return(pin & 0x100) != 0
	# }}}
	def valid(self):	# {{{
		return self.position_valid
	# }}}
	def export_settings(self): # {{{
		message = '[general]\r\n'
		for t in ('spaces', 'temps', 'gpios'):
			message += 'num_%s = %d\r\n' % (t, len(getattr(self, t)))
		message += 'unit_name=%s\r\n' % self.unit_name
		message += ''.join(['%s = %s\r\n' % (x, write_pin(getattr(self, x))) for x in ('led_pin', 'probe_pin')])
		message += ''.join(['%s = %f\r\n' % (x, getattr(self, x)) for x in ('probe_dist', 'probe_safe_dist', 'bed_id', 'fan_id', 'spindle_id', 'timeout')])
		for i, s in enumerate(self.spaces):
			message += s.export_settings()
		for i, t in enumerate(self.temps):
			message += t.export_settings()
		for i, g in enumerate(self.gpios):
			message += g.export_settings()
		return message
	# }}}
	def import_settings(self, settings, filename = None, update = True): # {{{
		self._broadcast(None, 'blocked', 'importing settings')
		self.sleep(update = update)
		section = 'general'
		index = None
		obj = None
		regexp = re.compile('\s*\[(general|(space|temp|gpio|(extruder|axis|motor|delta)\s+(\d+))\s+(\d+))\]\s*$|\s*(\w+)\s*=\s*(.*?)\s*$|\s*(?:#.*)?$')
		#1: (general|(space|temp|gpio|(axis|motor|delta)\s+(\d+))\s+(\d+))	1 section
		#2: (space|temp|gpio|(extruder|axis|motor|delta)\s+(\d+))		2 section with index
		#3: (extruder|axis|motor|delta)						3 sectionname with two indices
		#4: (\d+)								4 index of space
		#5: (\d+)								5 only or component index
		#6: (\w+)								6 identifier
		#7: (.*?)								7 value
		errors = []
		globals_changed = False
		changed = {'space': set(), 'temp': set(), 'gpio': set(), 'axis': set(), 'motor': set(), 'extruder': set(), 'delta': set()}
		keys = {
				'general': {'num_spaces', 'num_temps', 'num_gpios', 'led_pin', 'probe_pin', 'probe_dist', 'probe_safe_dist', 'bed_id', 'fan_id', 'spindle_id', 'unit_name', 'timeout'},
				'space': {'name', 'type', 'max_deviation', 'num_axes', 'delta_angle'},
				'temp': {'name', 'R0', 'R1', 'Rc', 'Tc', 'beta', 'heater_pin', 'fan_pin', 'thermistor_pin', 'fan_temp'},
				'gpio': {'name', 'pin', 'state', 'reset'},
				'axis': {'name', 'offset', 'park', 'park_order', 'max_v', 'min', 'max'},
				'motor': {'name', 'step_pin', 'dir_pin', 'enable_pin', 'limit_min_pin', 'limit_max_pin', 'sense_pin', 'steps_per_unit', 'max_steps', 'home_pos', 'limit_v', 'limit_a', 'home_order'},
				'extruder': {'dx', 'dy', 'dz'},
				'delta': {'axis_min', 'axis_max', 'rodlength', 'radius'}
			}
		for l in settings.split('\n'):
			r = regexp.match(l)
			if not r:
				errors.append((l, 'syntax error'))
				continue
			if r.group(1) is not None:
				# New section.
				if r.group(2) is not None:
					# At least one index.
					if r.group(3) is not None:
						# Two indices: axis, motor, extruder, delta.
						index = (int(r.group(4)), int(r.group(5)))
						section = r.group(3)
						if index[0] >= len(self.spaces) or index[1] >= len(getattr(self.spaces[index[0]], section)):
							log('index out of range for %s; %s %s' % (index, len(self.spaces), len(getattr(self.spaces[index[0]], section)) if index[0] < len(self.spaces) else 'x'))
							errors.append((l, 'index out of range'))
							obj = None
							continue
						obj = getattr(self.spaces[index[0]], section)[index[1]]
					else:
						# One index: space, temp, gpio.
						index = int(r.group(5))
						section = r.group(2)
						if index >= len(getattr(self, section + 's')):
							errors.append((l, 'index out of range'))
							obj = None
							continue
						obj = getattr(self, section + 's')[index]
					changed[section].add(index)
				else:
					# No indices: general.
					section = r.group(1)
					index = None
					obj = self
					globals_changed = True
				continue
			elif obj is None:
				# Ignore settings for incorrect setion.
				continue
			if not r.group(6):
				# Comment or empty line.
				continue
			key = r.group(6)
			value = r.group(7)
			if key.endswith('name'):
				pass
			elif key.endswith('pin'):
				value = read_pin(value)
			elif key.startswith('num'):
				value = int(value)
			else:
				value = float(value)
			if key not in keys[section]:
				errors.append((l, 'invalid key for section %s' % section))
				continue
			# Ignore performance: always update instantly.
			if index is None:
				#log('setting globals %s:%s' % (key, value))
				self.set_globals(update = update, **{key: value})
			elif section == 'delta':
				#log('setting delta %d %d %s:%s' % (index[0], index[1], key, value))
				self.set_space(index[0], delta = {index[1]: {key: value}}, update = update)
			elif section == 'extruder':
				self.set_space(index[0], extruder = {index[1]: {key: value}}, update = update)
			else:
				#log('setting %s %s %s:%s' % (section, repr(index), key, value))
				getattr(self, 'set_' + section)(index, update = update, **{key: value})
			'''
			# If the number of things is changed, update instantly.
			if key.startswith('num') or key == 'type':
				log('setting now for %s:%s=%s' % (section, key, value))
				if index is None:
					self.set_globals(**{key: value})
					globals_changed = False
				else:
					if section == 'space':
						for i in changed['motor']:
							if i[0] == index:
								self.set_motor(i, readback = False)
						for i in changed['axis']:
							if i[0] == index:
								self.set_axis(i, readback = False)
						for i in changed['delta']:
							if i[0] == index:
								self.set_axis(i, readback = False)
					getattr(self, 'set_' + section)(index, **{key: value})
					changed[section].discard(index)
			else:
				if isinstance(index, tuple):
					log('setting later %s' % repr((section, key, value)))
					obj[key] = value
				else:
					log('setting later other %s' % repr((section, key, value)))
					if section == 'extruder':
						obj[ord[key[1]] - ord['x']] = value
					else:
						setattr(obj, key, value)
		# Update values in the printer by calling the set_* functions with no new settings.
		if globals_changed:
			log('setting globals')
			self.set_globals()
		for section in changed:
			if section == 'extruder':
				changed['space'].add(index)
				continue
			for index in changed[section]:
				if not isinstance(index, tuple):
					continue
				if section != 'delta':
					log('setting non-delta %s' % repr(section))
					getattr(self, 'set_' + section)(index, readback = False)
				changed['space'].add(index[0])
		for section in changed:
			if section == 'extruder':
				continue
			for index in changed[section]:
				if isinstance(index, tuple):
					continue
				log('setting %s' % repr((section, index)))
				getattr(self, 'set_' + section)(index)
				'''
		self._broadcast(None, 'blocked', None)
		return errors
	# }}}
	def _import_file(self, filename, name): # {{{
		return ', '.join('%s (%s)' % (msg, ln) for ln, msg in self.import_settings(open(filename).read(), name))
	# }}}
	@delayed
	def gcode_run(self, id, code, ref = (0, 0, 0), angle = 0, probemap = None): # {{{
		with fhs.write_temp(text = False) as f:
			f.write(code)
			f.seek(0)
			self.gcode_id = id
			return self._gcode_run(f.filename, ref, angle, probemap)
	# }}}
	@delayed
	def request_confirmation(self, id, message): # {{{
		# Abort pending confirmation, if any.
		if self.confirmer not in (False, None):
			self._send(self.confirmer, 'return', False)
		self.confirmer = id
		self.confirm_id += 1
		self.confirm_message = message
		self._broadcast(None, 'confirm', self.confirm_id, self.confirm_message)
	# }}}
	def confirm(self, confirm_id, success = True): # {{{
		if confirm_id != self.confirm_id:
			# Confirmation was already sent, or the request was from a file; send resume just in case.
			#log('no confirm %s' % repr((confirm_id, self.confirm_id)))
			self._send_packet(chr(protocol.command['RESUME']))
			return False
		id = self.confirmer
		self.confirmer = None
		self.confirm_message = None
		self._broadcast(None, 'confirm', None)
		if id not in (False, None):
			self._send(id, 'return', success)
		else:
			if self.probing:
				call_queue.append((self.probe_cb[1], [success]))
			else:
				if not success:
					self._unpause()
					self._print_done(False, 'aborted by failed confirmation')
				else:
					call_queue.append((self._do_gcode, []))
		return True
	# }}}
	def queue_add(self, data, name): # {{{
		with fhs.write_temp() as f:
			f.write(data)
			f.seek(0)
			return self._queue_add(f, name)
	# }}}
	def _queue_add_file(self, filename, name): # {{{
		with open(filename) as f:
			return ', '.join(self._queue_add(f, name))
	# }}}
	def queue_remove(self, name): # {{{
		assert name in self.jobqueue
		#log('removing %s' % name)
		filename = fhs.write_spool(name + os.extsep + 'bin', opened = False)
		try:
			os.unlink(filename)
		except:
			log('unable to unlink %s' % filename)
		del self.jobqueue[name]
		self._broadcast(None, 'queue', [(q, self.jobqueue[q]) for q in self.jobqueue])
	# }}}
	def _gcode_parse(self, src, name): # {{{
		self._broadcast(None, 'blocked', 'parsing g-code')
		errors = []
		mode = None
		message = None
		bbox = [None] * 6
		bbox_last = [None] * 6
		strings = ['']
		unit = 1.
		rel = False
		erel = None
		pos = [[float('nan'), float('nan'), float('nan')], [0.], float('inf')]
		with fhs.write_spool(os.path.splitext(name)[0] + os.path.extsep + 'bin') as dst:
			def add_record(type, nums = None):
				if nums is None:
					nums = []
				if isinstance(nums, dict):
					nums = [nums['T'], nums['x'], nums['X'], nums['y'], nums['Y'], nums['z'], nums['Z'], nums['e'], nums['E'], nums['f'], nums['F']]
				nums += [0] * (11 - len(nums))
				if type != protocol.parsed['PARK']:
					dst.write(struct.pack('<Bl' + 'f' * 10, type, *nums))
				else:
					# TODO
					pass
				if type == protocol.parsed['PARK']:
					if bbox[0] is not None:
						bbox_last[:] = bbox
					bbox[:] = [None] * 6
				elif type == protocol.parsed['GOTO']:
					for i in range(3):
						if nums[2 * i + 1] != nums[2 * i + 2]:
							value = nums[2 * i + 2]
							if math.isnan(value):
								continue
							if bbox[2 * i] is None or value < bbox[2 * i]:
								bbox[2 * i] = value
							if bbox[2 * i + 1] is None or value > bbox[2 * i + 1]:
								bbox[2 * i + 1] = value
			def add_string(string):
				if string is None:
					return 0
				if string not in strings:
					strings.append(string)
				return strings.index(string)
			# Expect to start with a park.
			for a in range(3):
				if len(self.spaces[0].axis) > a and not math.isnan(self.spaces[0].axis[a]['park']):
					pos[0][a] = self.spaces[0].axis[a]['park']
			current_extruder = 0
			for lineno, origline in enumerate(src):
				line = origline.strip()
				#log('parsing %s' % line)
				# Get rid of line numbers and checksums.
				if line.startswith('N'):
					r = re.match(r'N(\d+)\s+(.*?)\*\d+\s*$', line)
					if not r:
						r = re.match(r'N(\d+)\s+(.*?)\s*$', line)
						if not r:
							# Invalid line; ignore it.
							errors.append('%d:ignoring invalid gcode: %s' % (lineno, origline))
							continue
					lineno = int(r.group(1))
					line = r.group(2)
				else:
					lineno += 1
				comment = ''
				while '(' in line:
					b = line.index('(')
					e = line.find(')', b)
					if e < 0:
						errors.append('%d:ignoring line with unterminated comment: %s' % (lineno, origline))
						continue
					comment = line[b + 1:e].strip()
					line = line[:b] + ' ' + line[e + 1:].strip()
				if ';' in line:
					p = line.index(';')
					comment = line[p + 1:].strip()
					line = line[:p].strip()
				if comment.upper().startswith('MSG,'):
					message = comment[4:].strip()
				elif comment.startswith('SYSTEM:'):
					if not re.match(self.allow_system, comment[7:]):
						errors.append('Warning: system command %s is forbidden and will not be run' % comment[7:])
					add_record(protocol.parsed['SYSTEM'], [add_string(comment[7:])])
					continue
				if line == '':
					continue
				line = line.split()
				while len(line) > 0:
					if mode is None or line[0][0] in 'GMTDS':
						if len(line[0]) < 2:
							errors.append('%d:ignoring unparsable line: %s' % (lineno, origline))
							break
						try:
							cmd = line[0][0], int(line[0][1:])
						except:
							errors.append('%d:parse error in line: %s' % (lineno, origline))
							traceback.print_exc()
							break
						line = line[1:]
					else:
						cmd = mode
					args = {}
					success = True
					for i, a in enumerate(line):
						if a[0] in 'GMD':
							line = line[i:]
							break
						try:
							args[a[0]] = float(a[1:])
						except:
							errors.append('%d:ignoring invalid gcode: %s' % (lineno, origline))
							success = False
							break
					else:
						line = []
					if not success:
						break
					if cmd == ('M', 2):
						# Program end.
						break
					elif cmd[0] == 'T':
						target = cmd[1]
						if target >= len(pos[1]):
							pos[1].extend([0.] * (target - len(pos[1]) + 1))
						current_extruder = target
						# Force update of extruder.
						add_record(protocol.parsed['GOTO'], {'x': pos[0][0], 'y': pos[0][1], 'z': pos[0][2], 'X': pos[0][0], 'Y': pos[0][1], 'Z': pos[0][2], 'e': pos[1][current_extruder], 'E': pos[1][current_extruder], 'f': float('inf'), 'F': float('inf'), 'T': current_extruder})
						continue
					elif cmd == ('G', 20):
						unit = 25.4
						continue
					elif cmd == ('G', 21):
						unit = 1.
						continue
					elif cmd == ('G', 90):
						rel = False
						continue
					elif cmd == ('G', 91):
						rel = True
						continue
					elif cmd == ('M', 82):
						erel = False
						continue
					elif cmd == ('M', 83):
						erel = True
						continue
					elif cmd == ('M', 84):
						for e in range(len(pos[1])):
							pos[1][e] = 0.
					elif cmd == ('G', 92):
						if 'E' not in args:
							continue
						args['E'] *= unit
						pos[1][current_extruder] = args['E']
						args['e'] = current_extruder
					elif cmd[0] == 'M' and cmd[1] in (104, 109, 116):
						args['E'] = int(args['T']) if 'T' in args else current_extruder
					if cmd == ('M', 140):
						cmd = ('M', 104)
						args['E'] = -1
					elif cmd == ('M', 190):
						cmd = ('M', 109)
						args['E'] = -1
					elif cmd == ('M', 6):
						# Tool change: park.
						cmd = ('G', 28)
					if cmd == ('G', 28):
						add_record(protocol.parsed['PARK'])
						for a in range(len(pos[0])):
							if len(self.spaces[0].axis) > a and not math.isnan(self.spaces[0].axis[a]['park']):
								pos[0][a] = self.spaces[0].axis[a]['park']
					elif cmd[0] == 'G' and cmd[1] in (0, 1, 81):
						if cmd[1] != 0:
							mode = cmd
						components = {'X': None, 'Y': None, 'Z': None, 'E': None, 'F': None, 'R': None}
						for c in args:
							if c not in components:
								errors.append('%d:invalid component %s' % (lineno, c))
								continue
							assert components[c] is None
							components[c] = args[c]
						f0 = pos[2]
						if components['F'] is not None:
							pos[2] = components['F'] * unit / 60
						oldpos = pos[0][:], pos[1][:]
						if cmd[1] != 81:
							if components['E'] is not None:
								if erel or (erel is None and rel):
									estep = components['E'] * unit
								else:
									estep = components['E'] * unit - pos[1][current_extruder]
								pos[1][current_extruder] += estep
							else:
								estep = 0
						else:
							estep = 0
							if components['R'] is not None:
								if rel:
									r = pos[0][2] + components['R'] * unit
								else:
									r = components['R'] * unit
						for axis in range(3):
							value = components[chr(ord('X') + axis)]
							if value is not None:
								if rel:
									pos[0][axis] += value * unit
								else:
									pos[0][axis] = value * unit
								if axis == 2:
									z = pos[0][2]
						if cmd[1] != 81:
							dist = sum([0] + [(pos[0][x] - oldpos[0][x]) ** 2 for x in range(3) if not math.isnan(pos[0][x] - oldpos[0][x])]) ** .5
							if dist > 0:
								#if f0 is None:
								#	f0 = pos[1][current_extruder]
								f0 = pos[2]	# Always use new value.
								if f0 == 0:
									f0 = float('inf')
							if math.isnan(dist):
								dist = 0
							add_record(protocol.parsed['GOTO'], {'x': oldpos[0][0], 'y': oldpos[0][1], 'z': oldpos[0][2], 'X': pos[0][0], 'Y': pos[0][1], 'Z': pos[0][2], 'e': oldpos[1][current_extruder], 'E': pos[1][current_extruder], 'f': f0 / dist if dist > 0 and cmd[1] == 1 else float('inf'), 'F': pos[2] / dist if dist > 0 and cmd[1] == 1 else float('inf'), 'T': current_extruder})
						else:
							# Drill cycle.
							# Only support OLD_Z (G90) retract mode; don't support repeats(L).
							# goto x,y
							add_record(protocol.parsed['GOTO'], {'x': oldpos[0][0], 'y': oldpos[0][1], 'z': oldpos[0][2], 'X': pos[0][0], 'Y': pos[0][1], 'Z': oldpos[0][2], 'e': 0, 'E': 0, 'f': float('inf'), 'F': float('inf'), 'T': current_extruder})
							# goto r
							add_record(protocol.parsed['GOTO'], {'x': pos[0][0], 'y': pos[0][1], 'z': oldpos[0][2], 'X': pos[0][0], 'Y': pos[0][1], 'Z': r, 'e': 0, 'E': 0, 'f': float('inf'), 'F': float('inf'), 'T': current_extruder})
							# goto z; this is always straight down, because the move before and after it are also vertical.
							if z != r:
								f0 = pos[2] / abs(z - r)
								add_record(protocol.parsed['GOTO'], {'x': pos[0][0], 'y': pos[0][1], 'z': r, 'X': pos[0][0], 'Y': pos[0][1], 'Z': z, 'e': 0, 'E': 0, 'f': f0, 'F': f0, 'T': current_extruder})
							# retract; this is always straight up, because the move before and after it are also non-horizontal.
							add_record(protocol.parsed['GOTO'], {'x': pos[0][0], 'y': pos[0][1], 'z': z, 'X': pos[0][0], 'Y': pos[0][1], 'Z': oldpos[0][2], 'e': 0, 'E': 0, 'f': float('inf'), 'F': float('inf'), 'T': current_extruder})
							# empty move; this makes sure the previous move is entirely vertical.
							add_record(protocol.parsed['GOTO'], {'x': pos[0][0], 'y': pos[0][1], 'z': oldpos[0][2], 'X': pos[0][0], 'Y': pos[0][1], 'Z': oldpos[0][2], 'e': 0, 'E': 0, 'f': float('inf'), 'F': float('inf'), 'T': current_extruder})
							# Set up current z position so next G81 will work.
							pos[0][2] = oldpos[0][2]
					elif cmd == ('G', 4):
						add_record(protocol.parsed['WAIT'], [0, float(args['P']) / 1000 if 'P' in args else 0])
					elif cmd == ('G', 92):
						add_record(protocol.parsed['SETPOS'], [current_extruder, args['E']])
					elif cmd == ('G', 94):
						# Set feedrate to units per minute; this is always used, and it shouldn't raise an error.
						pass
					elif cmd == ('M', 0):
						add_record(protocol.parsed['CONFIRM'], [add_string(message)])
					elif cmd == ('M', 3):
						# Spindle on, clockwise.
						add_record(protocol.parsed['GPIO'], [-2, 1])
					elif cmd == ('M', 4):
						# Spindle on, counterclockwise.
						add_record(protocol.parsed['GPIO'], [-2, 1])
					elif cmd == ('M', 5):
						add_record(protocol.parsed['GPIO'], [-2, 0])
					elif cmd == ('M', 9):
						# Coolant off: ignore.
						pass
					elif cmd == ('M', 42):
						if 'P' in args and 'S' in args:
							add_record(protocol.parsed['GPIO'], [args['P'], args.get('S')])
						else:
							errors.append('%d:invalid M42 request (needs P and S)' % lineno)
					elif cmd == ('M', 84):
						# Don't sleep, but set all extruder positions to 0.
						for e in range(len(pos[1])):
							add_record(protocol.parsed['SETPOS'], [e, 0])
					elif cmd == ('M', 104):
						if args['E'] >= len(self.temps):
							errors.append('ignoring M104 for invalid temp %d' % args['E'])
						elif 'S' not in args:
							errors.append('ignoring M104 without S')
						else:
							add_record(protocol.parsed['SETTEMP'], [args['E'], args['S'] + C0])
					elif cmd == ('M', 106):
						add_record(protocol.parsed['GPIO'], [-1, 1])
					elif cmd == ('M', 107):
						add_record(protocol.parsed['GPIO'], [-1, 0])
					elif cmd == ('M', 109):
						if 'S' in args:
							add_record(protocol.parsed['SETTEMP'], [args['E'], args['S'] + C0])
						add_record(protocol.parsed['WAITTEMP'], [args['E']])
					elif cmd == ('M', 116):
						add_record(protocol.parsed['WAITTEMP'], [-2])
					elif cmd[0] == 'S':
						# Spindle speed; not supported, but shouldn't error.
						pass
					else:
						errors.append('%d:invalid gcode command %s' % (lineno, repr((cmd, args))))
					message = None
			stringmap = []
			size = 0
			for s in strings:
				stringmap.append(len(s))
				dst.write(s)
				size += len(s)
			for s in stringmap:
				dst.write(struct.pack('<L', s))
			ret = bbox
			if bbox[0] is None:
				bbox = bbox_last
				ret = bbox
				if bbox[0] is None:
					bbox = [0] * 6
					ret = None
			dst.write(struct.pack('<L' + 'f' * 6, len(strings), *bbox))
		self._broadcast(None, 'blocked', None)
		return ret, errors
	# }}}
	@delayed
	def queue_print(self, id, names, ref = (0, 0, 0), angle = 0, probemap = None): # {{{
		self.job_output = ''
		self.jobs_active = names
		self.jobs_ref = ref
		self.jobs_angle = angle
		self.jobs_probemap = probemap
		self.job_current = -1	# next_job will make it start at 0.
		self.job_id = id
		if not self.probing:
			self._next_job()
	# }}}
	@delayed
	def queue_probe(self, id, names, ref = (0, 0, 0), angle = 0, speed = 3): # {{{
		bbox = [float('nan'), float('nan'), float('nan'), float('nan')]
		for n in names:
			bb = self.jobqueue[n]
			if not bbox[0] < bb[0]:
				bbox[0] = bb[0]
			if not bbox[1] < bb[2]:
				bbox[1] = bb[2]
			if not bbox[2] > bb[1]:
				bbox[2] = bb[1]
			if not bbox[3] > bb[3]:
				bbox[3] = bb[3]
		self.probe((bbox[0] + ref[0], bbox[1] + ref[1], bbox[2] + ref[0], bbox[3] + ref[1]), angle, speed)[1](None)
		# Pass jobs_probemap to make sure it doesn't get overwritten.
		self.queue_print(names, ref, angle, self.jobs_probemap)[1](id)
	# }}}
	# }}}
	# Accessor functions. {{{
	# Globals. {{{
	def get_globals(self):
		ret = {'num_spaces': len(self.spaces), 'num_temps': len(self.temps), 'num_gpios': len(self.gpios)}
		for key in ('uuid', 'queue_length', 'audio_fragments', 'audio_fragment_size', 'num_analog_pins', 'num_digital_pins', 'led_pin', 'probe_pin', 'probe_dist', 'probe_safe_dist', 'bed_id', 'fan_id', 'spindle_id', 'unit_name', 'timeout', 'feedrate', 'zoffset', 'paused'):
			ret[key] = getattr(self, key)
		return ret
	def set_globals(self, update = True, **ka):
		#log('setting variables with %s' % repr(ka))
		ns = ka.pop('num_spaces') if 'num_spaces' in ka else None
		nt = ka.pop('num_temps') if 'num_temps' in ka else None
		ng = ka.pop('num_gpios') if 'num_gpios' in ka else None
		if 'unit_name' in ka:
			self.unit_name = ka.pop('unit_name')
		for key in ('led_pin', 'probe_pin', 'bed_id', 'fan_id', 'spindle_id'):
			if key in ka:
				setattr(self, key, int(ka.pop(key)))
		oldzoffset = self.zoffset
		for key in ('probe_dist', 'probe_safe_dist', 'timeout', 'feedrate', 'zoffset'):
			if key in ka:
				setattr(self, key, float(ka.pop(key)))
		self._write_globals(ns, nt, ng, update = update)
		if self.zoffset != oldzoffset and len(self.spaces) > 0 and len(self.spaces[0].axis) > 2:
			self.set_axis((0, 2))
		assert len(ka) == 0
	# }}}
	# Space {{{
	def get_axis_pos(self, space, axis):
		if space >= len(self.spaces) or axis >= len(self.spaces[space].axis):
			log('request for invalid axis position %d %d' % (space, axis))
			return float('nan')
		return self.spaces[space].get_current_pos(axis)
	def set_axis_pos(self, space, axis, pos):
		if space >= len(self.spaces) or axis >= len(self.spaces[space].axis):
			log('request to set invalid axis position %d %d' % (space, axis))
			return False
		return self.spaces[space].set_current_pos(axis, pos)
	def get_space(self, space):
		ret = {'name': self.spaces[space].name, 'num_axes': len(self.spaces[space].axis), 'num_motors': len(self.spaces[space].motor), 'type': self.spaces[space].type, 'max_deviation': self.spaces[space].max_deviation}
		if self.spaces[space].type == TYPE_CARTESIAN:
			pass
		elif self.spaces[space].type == TYPE_DELTA:
			delta = []
			for i in range(3):
				d = {}
				for key in ('axis_min', 'axis_max', 'rodlength', 'radius'):
					d[key] = self.spaces[space].delta[i][key]
				delta.append(d)
			delta.append(self.spaces[space].delta_angle)
			ret['delta'] = delta
		elif self.spaces[space].type == TYPE_EXTRUDER:
			ret['extruder'] = []
			for a in range(len(self.spaces[space].axis)):
				ret['extruder'].append({})
				for key in ('dx', 'dy', 'dz'):
					ret['extruder'][-1][key] = self.spaces[space].extruder[a][key]
		else:
			log('invalid type')
		return ret
	def get_axis(self, space, axis):
		ret = {}
		if space == 1:
			ret['multiplier'] = self.multipliers[axis]
		for key in ('name', 'offset', 'park', 'park_order', 'max_v', 'min', 'max'):
			ret[key] = self.spaces[space].axis[axis][key]
		return ret
	def get_motor(self, space, motor):
		ret = {}
		for key in ('name', 'step_pin', 'dir_pin', 'enable_pin', 'limit_min_pin', 'limit_max_pin', 'sense_pin', 'steps_per_unit', 'max_steps', 'home_pos', 'limit_v', 'limit_a', 'home_order'):
			ret[key] = self.spaces[space].motor[motor][key]
		return ret
	def set_space(self, space, readback = True, update = True, **ka):
		if 'name' in ka:
			self.spaces[space].name = ka.pop('name')
		if 'type' in ka:
			self.spaces[space].type = int(ka.pop('type'))
		if 'max_deviation' in ka:
			self.spaces[space].max_deviation = float(ka.pop('max_deviation'))
		if self.spaces[space].type == TYPE_EXTRUDER:
			if 'extruder' in ka:
				e = ka.pop('extruder')
				for ei, ee in e.items():
					i = int(ei)
					for key in ('dx', 'dy', 'dz'):
						if key in ee:
							self.spaces[space].extruder[i][key] = ee.pop(key)
					assert len(ee) == 0
		if self.spaces[space].type in (TYPE_CARTESIAN, TYPE_EXTRUDER):
			if 'num_axes' in ka:
				num_axes = int(ka.pop('num_axes'))
			else:
				num_axes = len(self.spaces[space].axis)
			num_motors = num_axes
		elif self.spaces[space].type == TYPE_DELTA:
			num_axes = 3;
			num_motors = 3;
			if 'delta' in ka:
				d = ka.pop('delta')
				for di, dd in d.items():
					i = int(di)
					assert 0 <= i < 3
					for key in ('axis_min', 'axis_max', 'rodlength', 'radius'):
						if key in dd:
							self.spaces[space].delta[i][key] = dd.pop(key)
					assert len(dd) == 0
			if 'delta_angle' in ka:
				self.spaces[space].delta_angle = ka.pop('delta_angle')
		self._send_packet(struct.pack('<BB', protocol.command['WRITE_SPACE_INFO'], space) + self.spaces[space].write_info(num_axes))
		if readback:
			self.spaces[space].read(self._read('SPACE', space))
			if update:
				self._space_update(space)
		if len(ka) != 0:
			log('invalid input ignored: %s' % repr(ka))
	def set_axis(self, (space, axis), readback = True, update = True, **ka):
		for key in ('name', 'offset', 'park', 'park_order', 'max_v', 'min', 'max'):
			if key in ka:
				self.spaces[space].axis[axis][key] = ka.pop(key)
		if space == 1 and 'multiplier' in ka and axis < len(self.spaces[space].motor):
			assert(ka['multiplier'] > 0)
			self.multipliers[axis] = ka.pop('multiplier')
			self.set_motor((space, axis), readback, update)
		self._send_packet(struct.pack('<BBB', protocol.command['WRITE_SPACE_AXIS'], space, axis) + self.spaces[space].write_axis(axis))
		if readback:
			self.spaces[space].read(self._read('SPACE', space))
			if update:
				self._space_update(space)
		assert len(ka) == 0
	def set_motor(self, (space, motor), readback = True, update = True, **ka):
		for key in ('name', 'step_pin', 'dir_pin', 'enable_pin', 'limit_min_pin', 'limit_max_pin', 'sense_pin', 'steps_per_unit', 'max_steps', 'home_pos', 'limit_v', 'limit_a', 'home_order'):
			if key in ka:
				self.spaces[space].motor[motor][key] = ka.pop(key)
		self._send_packet(struct.pack('<BBB', protocol.command['WRITE_SPACE_MOTOR'], space, motor) + self.spaces[space].write_motor(motor))
		if readback:
			self.spaces[space].read(self._read('SPACE', space))
			if update:
				self._space_update(space)
		assert len(ka) == 0
	# }}}
	# Temp {{{
	def get_temp(self, temp):
		ret = {}
		for key in ('name', 'R0', 'R1', 'Rc', 'Tc', 'beta', 'heater_pin', 'fan_pin', 'thermistor_pin', 'fan_temp'):
			ret[key] = getattr(self.temps[temp], key)
		return ret
	def set_temp(self, temp, update = True, **ka):
		ret = {}
		for key in ('name', 'R0', 'R1', 'Rc', 'Tc', 'beta', 'heater_pin', 'fan_pin', 'thermistor_pin', 'fan_temp'):
			if key in ka:
				setattr(self.temps[temp], key, ka.pop(key))
		self._send_packet(struct.pack('<BB', protocol.command['WRITE_TEMP'], temp) + self.temps[temp].write())
		self.temps[temp].read(self._read('TEMP', temp))
		if update:
			self._temp_update(temp)
		if len(ka) != 0:
			log('problem: %s' % repr(ka))
		assert len(ka) == 0
	# }}}
	# Gpio {{{
	def get_gpio(self, gpio):
		ret = {}
		for key in ('name', 'pin', 'state', 'reset'):
			ret[key] = getattr(self.gpios[gpio], key)
		return ret
	def set_gpio(self, gpio, update = True, **ka):
		for key in ('name', 'pin', 'state', 'reset'):
			if key in ka:
				setattr(self.gpios[gpio], key, ka.pop(key))
		self.gpios[gpio].state = int(self.gpios[gpio].state)
		self.gpios[gpio].reset = int(self.gpios[gpio].reset)
		if (self.gpios[gpio].reset >= 2 and self.gpios[gpio].state < 2) or (self.gpios[gpio].reset < 2 and self.gpios[gpio].state >= 2):
			self.gpios[gpio].state = self.gpios[gpio].reset
		self._send_packet(struct.pack('<BB', protocol.command['WRITE_GPIO'], gpio) + self.gpios[gpio].write())
		self.gpios[gpio].read(self._read('GPIO', gpio))
		if update:
			self._gpio_update(gpio)
		assert len(ka) == 0
	# }}}
	def send_printer(self, target): # {{{
		self._broadcast(target, 'new_printer', [self.uuid, self.queue_length, self.audio_fragments, self.audio_fragment_size, self.num_digital_pins, self.num_analog_pins])
		self._globals_update(target)
		for i, s in enumerate(self.spaces):
			self._space_update(i, target)
		for i, t in enumerate(self.temps):
			self._temp_update(i, target)
		for i, g in enumerate(self.gpios):
			self._gpio_update(i, target)
		self._broadcast(None, 'queue', [(q, self.jobqueue[q]) for q in self.jobqueue])
		if self.confirmer is not None:
			self._broadcast(None, 'confirm', self.confirm_id, self.confirm_message)
	# }}}
	# }}}
# }}}

call_queue = []
printer = Printer(config['port'], config['uuid'], config['run-id'], config['allow-system'])
if printer.printer is None:
	sys.exit(0)

while True: # {{{
	while len(call_queue) > 0:
		f, a = call_queue.pop(0)
		f(*a)
	while printer.printer.available():
		printer._printer_input()
	if len(call_queue) > 0:
		continue	# Handle this first.
	fds = [sys.stdin, printer.printer]
	#log('waiting; movewait = %d' % printer.movewait)
	now = time.time()
	found = select.select(fds, [], fds, printer.gcode_wait and max(0, printer.gcode_wait - now))
	#log(repr(found))
	if len(found[0]) == 0:
		#log('timeout')
		# Timeout.
		printer.gcode_wait = None
		printer._do_gcode()
		continue
	if sys.stdin in found[0] or sys.stdin in found[2]:
		#log('command')
		printer._command_input()
	if printer.printer in found[0] or printer.printer in found[2]:
		#log('printer')
		printer._printer_input()
# }}}
