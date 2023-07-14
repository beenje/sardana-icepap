##############################################################################
##
# This file is part of Sardana
##
# http://www.tango-controls.org/static/sardana/latest/doc/html/index.html
##
# Copyright 2011 CELLS / ALBA Synchrotron, Bellaterra, Spain
##
# Sardana is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
##
# Sardana is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
##
# You should have received a copy of the GNU Lesser General Public License
# along with Sardana.  If not, see <http://www.gnu.org/licenses/>.
##
##############################################################################
import time
import numpy
from sardana import State
from sardana.pool.pooldefs import SynchDomain, SynchParam
from sardana.pool.controller import TriggerGateController, Access, Memorize, \
    Memorized, Type, Description, DataAccess, DefaultValue
import taurus
import tango
import icepap
import json
# [WIP] This controller need the Sardana PR 671 !!!!!

LOW = 'low'
HIGH = 'high'
ECAM = 'ecam'

MAX_ECAM_VALUES = 20477
ECAM_SOURCE_VALUES = ['ENCIN', 'ABSENC', 'INPOS']


class IcePAPTriggerController(TriggerGateController):
    """Basic IcePAPPositionTriggerGateController.
    """

    organization = "ALBA-Cells"
    gender = "TriggerGate"
    model = "Icepap"

    MaxDevice = 1

    ActivePeriod = 50e-6  # 50 micro seconds

    # The properties used to connect to the ICEPAP motor controller
    ctrl_properties = {
        'Host': {
            Type: str,
            Description: 'The host name'
        },
        'Port': {
            Type: int, Description: 'The port number',
            DefaultValue: 5000
        },
        'IcepapCtrlAlias': {
            Type: str,
            Description: 'The host name'
        },
        'AxisInfos': {
            Type: str,
            Description: 'List of InfoX separated by colons, used '
                         'when the trigger is generated by the '
                         'axis (!=0)',
            DefaultValue: 'InfoA'
        },
        'EcamSource': {
            Type: str,
            Description: 'dic "axis, pos_sel, polarity" e.g: '
                         ' {44: "ENCIN"}'
                         'If the axis is not on the this list the syncpos '
                         'will use default value: AXIS,NORMAL',
            DefaultValue: ''
        },
        'Timeout': {
            Type: float,
            Description: 'Timeout used for the IcePAP socket communication',
            DefaultValue: 0.5
        }

    }
    axis_attributes = {
        'StartTriggerOnly': {
            Type: bool,
            Description: 'Launch only the First trigger position',
            Access: DataAccess.ReadWrite,
            Memorize: Memorized
        },
    }

    def __init__(self, inst, props, *args, **kwargs):
        """
        :param inst:
        :param props:
        :param args:
        :param kwargs:
        :return:
        """
        TriggerGateController.__init__(self, inst, props, *args, **kwargs)
        self._log.debug('IcePAPTriggerCtr init....')

        self._time_mode = False
        self._start_trigger_only = False
        self._axis_info_list = list(map(str.strip, self.AxisInfos.split(',')))

        # Calculate the number of retries according to the timeout and the
        # default Tango timeout (3s)
        self._retries_nr = 3 // (self.Timeout + 0.1)
        if self._retries_nr == 0:
            self._retries_nr = 1
        self._retries_nr = int(self._retries_nr)
        self._ipap = icepap.IcePAPController(host=self.Host, port=self.Port,
                                             timeout=self.Timeout)
        self._last_id = None
        self._motor_axis = None
        self._motor_spu = 1
        self._is_tgtenc = False
        self._moveable_on_input = None
        self._ecam_source_dict = None
        self._ecam_source = "AXIS"
        if self.EcamSource:
            self._ecam_source_dict = eval(self.EcamSource)

    def _set_out(self, out=LOW, axis=0):
        motor = self._ipap[self._motor_axis]
        value = [out, 'normal']
        if axis == 0:
            motor.syncaux = value
            if self._ecam_source_dict and \
                    self._motor_axis in self._ecam_source_dict:
                enc = self._ecam_source_dict[self._motor_axis].upper()
                if enc == 'TGTENC':
                    enc = motor.get_cfg('TGTENC')['TGTENC']
                    self._ecam_source_dict[self._motor_axis] = enc
                if enc in ECAM_SOURCE_VALUES:
                    self._ecam_source = self._ecam_source_dict[self._motor_axis]
                else:
                    self._log.error(
                        'Ecam source {} not supported, AXIS will be used'.format(enc))
                    self._ecam_source = "AXIS"
            else:
                self._ecam_source = "AXIS"
        else:
            for info_out in self._axis_info_list:
                setattr(motor, info_out, value)

    def _configureMotor(self, id_, axis):
        # this is a bit hacky, ideally we could define an extra attribute
        # step_per_unit (would require updating it at the same time as the
        # motor's one)
        motor_name = "motor/{}/{}".format(self.IcepapCtrlAlias, id_)
        motor = taurus.Device(motor_name)
        self._motor_axis = id_
        self._motor_spu = motor.read_attribute('step_per_unit').value

        if id_ == self._last_id:
            return
        self._last_id = id_

        if axis == 0:
            # remove previous connection and connect the new motor
            pmux = self._ipap.get_pmux()
            for p in pmux:
                if 'E0' in p:
                    self._ipap.clear_pmux('e0')
                    break
            self._ipap.add_pmux(self._motor_axis, 'e0', pos=False, aux=True,
                                hard=True)

            pmux = self._ipap.get_pmux()
            self._log.debug('_connectMotor PMUX={0}'.format(pmux))

    def AddDevice(self, axis):
        if axis == 0:
            moveable_on_input_raw = {}
            encoder = json.JSONEncoder()
            for i in self.ipap.find_axes():
                # this is a bit hacky, ideally we could define an extra
                # attribute step_per_unit (would require updating it at the
                # same time as the motor's one)
                motor_name = "motor/{}/{}".format(self.IcepapCtrlAlias, i)
                try:
                    motor = tango.DeviceProxy(motor_name)
                    alias = motor.alias()
                    moveable_on_input_raw[alias] = i
                except Exception:
                    pass

            self._moveable_on_input = encoder(moveable_on_input_raw)

    def StateOne(self, axis):
        """Get the trigger/gate state"""
        # self._log.debug('StateOne(%d): entering...' % axis)
        hw_state = None
        state = State.On
        status = 'No synchronization in progress'
        if self._motor_axis is not None:
            for i in range(self._retries_nr):
                try:
                    hw_state = self._ipap[self._motor_axis].state
                    break
                except Exception:
                    self._log.error('State reading error retry: {0}'.format(i))

            if hw_state is None or not hw_state.is_poweron():
                state = State.Alarm
                status = 'The motor is power off or not possible to read State'
            elif hw_state.is_moving() or hw_state.is_settling():
                state = State.Moving
                status = 'Moving'
            else:
                status = 'Motor is not generating triggers.'

        return state, status

    def PreStartOne(self, axis, value=None):
        """PreStart the specified trigger"""
        # self._log.debug('PreStartOne(%d): entering...' % axis)
        if self._time_mode:
            self._set_out(LOW, axis)
        else:
            self._set_out(ECAM, axis)
        return True

    def StartOne(self, axis):
        """Overwrite the StartOne method"""
        if self._time_mode:
            self._set_out(HIGH, axis)
            time.sleep(0.01)
            self._set_out(LOW, axis)
            return

        if self._is_tgtenc:
            self._log.info('Send ESYNC to motor: %s',
                           self._ipap[self._motor_axis].name)
            self._ipap[self._motor_axis].esync()

    def AbortOne(self, axis):
        """Start the specified trigger"""
        self._log.debug('AbortOne(%d): entering...' % axis)

        self._set_out(LOW, axis)

    def SynchOne(self, axis, configuration):
        # TODO: implement the configuration for multiples configuration
        synch_group = configuration[0]
        nr_points = synch_group[SynchParam.Repeats]

        if SynchParam.Initial not in synch_group:
            # Synchronization by time (step scan and ct)
            if nr_points > 1:
                msg = 'The IcePAP Trigger Controller is not allowed to ' \
                      'generate multiple trigger synchronized by time'
                raise ValueError(msg)
            else:
                self._time_mode = True
            return

        self._time_mode = False

        # Check target encoder configuration to send ESYNC on StartOne
        try:
            tgtenc_cfg = \
                self._ipap[self._motor_axis].get_cfg('TGTENC')['TGTENC']
            self._is_tgtenc = tgtenc_cfg == 'NONE'
        except Exception as e:
            self._log.error('SynchOne(%d).\nException:\n%s' % (axis, str(e)))
            return False

        start_user = synch_group[SynchParam.Initial][SynchDomain.Position]
        delta_user = synch_group[SynchParam.Total][SynchDomain.Position]

        start = start_user * self._motor_spu
        delta = delta_user * self._motor_spu

        end = start + delta * nr_points

        # Calculation of the syncpos according to the selected encoder
        motor = self._ipap[self._motor_axis]
        enc = self._ecam_source.upper()
        if enc != 'AXIS':
            if enc == 'ENCIN':
                cfgstep = 'EINNSTEP'
                cfgturn = 'EINNTURN'
            elif enc == 'ABSENC':
                cfgstep = 'ABSNSTEP'
                cfgturn = 'ABSNTURN'
            elif enc == 'INPOS':
                cfgstep = 'INPNSTEP'
                cfgturn = 'INPNTURN'
            else:
                cfgstep = 'ANSTEP'
                cfgturn = 'ANTURN'

            enc_resol = int(motor.get_cfg(cfgstep)[
                            cfgstep])/int(motor.get_cfg(cfgturn)[cfgturn])
            motor_resol = int(motor.get_cfg('ANSTEP')[
                              'ANSTEP'])/int(motor.get_cfg('ANTURN')['ANTURN'])
            start = (start / motor_resol) * enc_resol
            delta = (delta / motor_resol) * enc_resol
            end = (end / motor_resol) * enc_resol

        self._log.debug('IcepapTriggerCtr configuration: %f %f %d %d' %
                        (start, end, nr_points, delta))

        # There is a limitation of numbers of point on the icepap (20477)
        # ecamdat = motor.getAttribute('ecamdatinterval')
        # ecamdat.write([initial, final, nr_points], with_read=False)

        # The ecamdattable attribute is protected against non increasing
        # list at the icepap library level. HOWEVER, is not protected
        # agains list with repeated elements

        if self._start_trigger_only:
            trigger_table = numpy.array([start])
            self._log.debug('Start trigger only flag is active.')
        elif nr_points > MAX_ECAM_VALUES:
            msg = 'The Trigger by position not accept more than {0} ' \
                  'positions (points)'.format(MAX_ECAM_VALUES)
            raise RuntimeError(msg)
        else:
            trigger_table = numpy.linspace(start, end - delta,
                                           int(nr_points))
            self._log.debug('Table generated by numpy.linspace({0},{1},'
                            '{2})'.format(start, end-delta, nr_points))

        table_loaded = False
        for i in range(self._retries_nr):
            try:
                self._ipap[self._motor_axis].set_ecam_table(
                    trigger_table, source=self._ecam_source)
                table_loaded = True
                break
            except Exception:
                self._log.warning('Send trigger table error retry: '
                                  '{0}'.format(i))
        if not table_loaded:
            raise RuntimeError('Can not send trigger table.')

    def GetAxisPar(self, axis, parameter):
        if axis == 0 and parameter == "MoveableOnInput":
            return self._moveable_on_input

    def SetAxisPar(self, axis, par, value):
        if axis == 0 and par == "active_input":
            self._configureMotor(value, axis)
        else:
            raise ValueError(
                "unsupported axis par {} for axis {}".format(par, axis))

    # -------------------------------------------------------------------------
    #               Axis Extra Parameters
    # -------------------------------------------------------------------------

    def setStartTriggerOnly(self, axis, value):
        self._start_trigger_only = value

    def getStartTriggerOnly(self, axis):
        return self._start_trigger_only
