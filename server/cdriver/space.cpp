/* space.cpp - Implementations of Space internals for Franklin {{{
 * vim: foldmethod=marker :
 * Copyright 2014-2016 Michigan Technological University
 * Copyright 2016 Bas Wijnen <wijnen@debian.org>
 * Author: Bas Wijnen <wijnen@debian.org>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 * }}} */

#include "cdriver.h"

//#define DEBUG_PATH

#if 0
#define loaddebug debug
#else
#define loaddebug(...) do {} while(0)
#endif

bool Space::setup_nums(int na, int nm) { // {{{
	if (na == num_axes && nm == num_motors)
		return true;
	loaddebug("new space %d %d %d %d", na, nm, num_axes, num_motors);
	int old_nm = num_motors;
	int old_na = num_axes;
	num_motors = nm;
	num_axes = na;
	if (na != old_na) {
		Axis **new_axes = new Axis *[na];
		loaddebug("new axes: %d", na);
		for (int a = 0; a < min(old_na, na); ++a)
			new_axes[a] = axis[a];
		for (int a = old_na; a < na; ++a) {
			new_axes[a] = new Axis;
			new_axes[a]->park = NAN;
			new_axes[a]->park_order = 0;
			new_axes[a]->min_pos = -INFINITY;
			new_axes[a]->max_pos = INFINITY;
			new_axes[a]->type_data = NULL;
			new_axes[a]->current = NAN;
			new_axes[a]->settings.source = NAN;
			new_axes[a]->settings.endpos = NAN;
			new_axes[a]->history = setup_axis_history();
		}
		for (int a = na; a < old_na; ++a) {
			space_types[type].free_axis(this, a);
			delete[] axis[a]->history;
			delete axis[a];
		}
		delete[] axis;
		axis = new_axes;
		for (int a = old_na; a < na; ++a)
			space_types[type].init_axis(this, a);
	}
	if (nm != old_nm) {
		Motor **new_motors = new Motor *[nm];
		loaddebug("new motors: %d", nm);
		for (int m = 0; m < min(old_nm, nm); ++m)
			new_motors[m] = motor[m];
		for (int m = old_nm; m < nm; ++m) {
			new_motors[m] = new Motor;
			new_motors[m]->step_pin.init();
			new_motors[m]->dir_pin.init();
			new_motors[m]->enable_pin.init();
			new_motors[m]->limit_min_pin.init();
			new_motors[m]->limit_max_pin.init();
			new_motors[m]->steps_per_unit = 100;
			new_motors[m]->limit_v = INFINITY;
			new_motors[m]->limit_a = INFINITY;
			new_motors[m]->home_pos = NAN;
			new_motors[m]->home_order = 0;
			new_motors[m]->limit_v = INFINITY;
			new_motors[m]->limit_a = INFINITY;
			new_motors[m]->active = false;
			new_motors[m]->last_v = NAN;
			new_motors[m]->target_pos = NAN;
			new_motors[m]->settings.current_pos = 0;
			new_motors[m]->history = setup_motor_history();
			new_motors[m]->type_data = NULL;
			ARCH_NEW_MOTOR(id, m, new_motors);
		}
		for (int m = nm; m < old_nm; ++m) {
			DATA_DELETE(id, m);
			space_types[type].free_motor(this, m);
			delete[] motor[m]->history;
			delete motor[m];
		}
		delete[] motor;
		motor = new_motors;
		for (int m = old_nm; m < nm; ++m)
			space_types[type].init_motor(this, m);
		arch_motors_change();
	}
	return true;
} // }}}

void Space::load_info() { // {{{
	loaddebug("loading space %d", id);
	int t = type;
	if (t < 0 || t >= num_space_types) {
		debug("invalid current type?! using default type instead");
		t = id;
	}
	type = shmem->ints[1];
	loaddebug("requested type %d, current is %d", type, t);
	if (type < 0 || type >= num_space_types || (id != 0 && type != id)) {
		debug("request for type %d ignored", type);
		type = t;
		return;	// The rest of the info is not meant for this type, so ignore it.
	}
	if (t != type) {
		loaddebug("setting type to %d", type);
		for (int m = 0; m < num_motors; ++m) {
			space_types[type].free_motor(this, m);
			motor[m]->type_data = NULL;
		}
		for (int a = 0; a < num_axes; ++a) {
			space_types[type].free_axis(this, a);
			axis[a]->type_data = NULL;
		}
		space_types[t].free_space(this);
		type_data = NULL;
		if (!space_types[type].init_space(this)) {
			type = id;
			if (!space_types[type].init_space(this))
				debug("restoring previous space type failed. Hoping for the best.");
			for (int a = 0; a < num_axes; ++a)
				space_types[type].init_axis(this, a);
			for (int m = 0; m < num_motors; ++m)
				space_types[type].init_motor(this, m);
			reset_pos(this);
			for (int a = 0; a < num_axes; ++a)
				axis[a]->settings.source = axis[a]->current;
			return;	// The rest of the info is not meant for this type, so ignore it.
		}
		current_int = 0;
		current_float = 0;
		space_types[type].load_space(this);
		for (int a = 0; a < num_axes; ++a)
			space_types[type].init_axis(this, a);
		for (int m = 0; m < num_motors; ++m)
			space_types[type].init_motor(this, m);
	}
	else {
		current_int = 0;
		current_float = 0;
		space_types[type].load_space(this);
	}
	if (current_int != shmem->ints[100] || current_float != shmem->ints[101]) {
		debug("Warning: load_space (for type %d) did not use correct number of parameters: ints/floats given = %d/%d, used = %d/%d", type, shmem->ints[100], shmem->ints[101], current_int, current_float);
	}
	if (id != 2)
		reset_pos(this);
	loaddebug("done loading space");
} // }}}

void reset_pos(Space *s) { // {{{
	double motors[s->num_motors];
	double xyz[s->num_axes];
	for (int m = 0; m < s->num_motors; ++m)
		motors[m] = s->motor[m]->settings.current_pos;
	space_types[s->type].motors2xyz(s, motors, xyz);
	for (int a = 0; a < s->num_axes; ++a) {
		s->axis[a]->current = xyz[a];
		if (!computing_move) {
			s->axis[a]->settings.source = xyz[a];
			//debug("setting axis %d %d source to %f for reset pos", s->id, a, s->axis[a]->settings.source);
		}
	}
	discard_finals();
} // }}}

void Space::load_axis(int a) { // {{{
	loaddebug("loading axis %d", a);
	axis[a]->park_order = shmem->ints[2];
	axis[a]->park = shmem->floats[0];
	axis[a]->min_pos = shmem->floats[1];
	axis[a]->max_pos = shmem->floats[2];
	current_int = 0;
	current_float = 0;
	space_types[type].load_axis(this, a);
	if (current_int != shmem->ints[100] || current_float != shmem->ints[101]) {
		debug("Warning: load_axis (for type %d, axis %d) did not use correct number of parameters: ints/floats given = %d/%d, used = %d/%d", type, a, shmem->ints[100], shmem->ints[101], current_int, current_float);
	}
} // }}}

void Space::load_motor(int m) { // {{{
	loaddebug("loading motor %d", m);
	uint16_t enable = motor[m]->enable_pin.write();
	double old_home_pos = motor[m]->home_pos;
	double old_steps_per_unit = motor[m]->steps_per_unit;
	bool old_dir_invert = motor[m]->dir_pin.inverted();
	motor[m]->step_pin.read(shmem->ints[2]);
	motor[m]->dir_pin.read(shmem->ints[3]);
	motor[m]->enable_pin.read(shmem->ints[4]);
	motor[m]->limit_min_pin.read(shmem->ints[5]);
	motor[m]->limit_max_pin.read(shmem->ints[6]);
	motor[m]->home_order = shmem->ints[7];
	motor[m]->steps_per_unit = shmem->floats[0];
	if (std::isnan(motor[m]->steps_per_unit)) {
		debug("Trying to set NaN steps per unit for motor %d %d", id, m);
		motor[m]->steps_per_unit = old_steps_per_unit;
	}
	motor[m]->home_pos = shmem->floats[1];
	motor[m]->limit_v = shmem->floats[2];
	motor[m]->limit_a = shmem->floats[3];
	current_int = 0;
	current_float = 0;
	space_types[type].load_motor(this, m);
	if (current_int != shmem->ints[100] || current_float != shmem->ints[101]) {
		debug("Warning: load_motor (for type %d, motor %d) did not use correct number of parameters: ints/floats given = %d/%d, used = %d/%d", type, m, shmem->ints[100], shmem->ints[101], current_int, current_float);
	}
	arch_motors_change();
	SET_OUTPUT(motor[m]->enable_pin);
	if (enable != motor[m]->enable_pin.write()) {
		if (motors_busy)
			SET(motor[m]->enable_pin);
		else {
			RESET(motor[m]->enable_pin);
		}
	}
	RESET(motor[m]->step_pin);
	RESET(motor[m]->dir_pin);
	SET_INPUT(motor[m]->limit_min_pin);
	SET_INPUT(motor[m]->limit_max_pin);
	if (motor[m]->dir_pin.inverted() != old_dir_invert)
		arch_invertpos(id, m);
	if (!std::isnan(motor[m]->home_pos)) {
		// Axes with a limit switch.
		if (motors_busy && (old_home_pos != motor[m]->home_pos || old_steps_per_unit != motor[m]->steps_per_unit) && !std::isnan(old_home_pos)) {
			double diff = motor[m]->home_pos - old_home_pos * old_steps_per_unit / motor[m]->steps_per_unit;
			if (!std::isnan(diff)) {
				motor[m]->settings.current_pos += diff;
				//debug("load motor %d %d new home %f add %f", id, m, motor[m]->home_pos, diff);
				// adjusting the arch pos is not affected by steps/unit.
				arch_addpos(id, m, motor[m]->home_pos - old_home_pos);
			}
		}
		reset_pos(this);
	}
	else if (!std::isnan(motor[m]->settings.current_pos)) {
		// Motors without a limit switch: adjust motor position to match axes.
		for (int a = 0; a < num_axes; ++a)
			axis[a]->target = axis[a]->current;
		space_types[type].xyz2motors(this);
		double diff = motor[m]->target_pos - motor[m]->settings.current_pos;
		if (!std::isnan(diff)) {
			motor[m]->settings.current_pos += diff;
			arch_addpos(id, m, diff);
		}
	}
} // }}}

void Space::save_info() { // {{{
	shmem->ints[1] = type;
	shmem->ints[2] = num_axes;
	shmem->ints[3] = num_motors;
	current_int = 0;
	current_float = 0;
	space_types[type].save_space(this);
	shmem->ints[100] = current_int;
	shmem->ints[101] = current_float;
} // }}}

void Space::save_axis(int a) { // {{{
	shmem->ints[2] = axis[a]->park_order;
	shmem->floats[0] = axis[a]->park;
	shmem->floats[1] = axis[a]->min_pos;
	shmem->floats[2] = axis[a]->max_pos;
	current_int = 0;
	current_float = 0;
	space_types[type].save_axis(this, a);
	shmem->ints[100] = current_int;
	shmem->ints[101] = current_float;
} // }}}

void Space::save_motor(int m) { // {{{
	shmem->ints[2] = motor[m]->step_pin.write();
	shmem->ints[3] = motor[m]->dir_pin.write();
	shmem->ints[4] = motor[m]->enable_pin.write();
	shmem->ints[5] = motor[m]->limit_min_pin.write();
	shmem->ints[6] = motor[m]->limit_max_pin.write();
	shmem->ints[7] = motor[m]->home_order;
	shmem->floats[0] = motor[m]->steps_per_unit;
	shmem->floats[1] = motor[m]->home_pos;
	shmem->floats[2] = motor[m]->limit_v;
	shmem->floats[3] = motor[m]->limit_a;
	current_int = 0;
	current_float = 0;
	space_types[type].save_motor(this, m);
	shmem->ints[100] = current_int;
	shmem->ints[101] = current_float;
} // }}}

void Space::init(int space_id) { // {{{
	type = space_id;
	id = space_id;
	type_data = NULL;
	num_axes = 0;
	num_motors = 0;
	motor = NULL;
	axis = NULL;
	history = NULL;
	space_types[type].init_space(this);
} // }}}

void Space::cancel_update() { // {{{
	// setup_nums failed; restore system to a usable state.
	type = id;
	int n = min(num_axes, num_motors);
	if (!setup_nums(n, n)) {
		debug("Failed to free memory; removing all motors and axes to make sure it works");
		if (!setup_nums(0, 0))
			debug("You're in trouble; this shouldn't be possible");
	}
} // }}}
