from contextlib import suppress
from typing import Optional
import importlib
import logging
import yaml

import qcodes
from qcodes.instrument.base import Instrument
from qcodes.station import Station
import qcodes.utils.validators as validators
from qcodes.instrument.parameter import Parameter
from qcodes.monitor.monitor import Monitor


log = logging.getLogger(__name__)

# config from the qcodesrc.json file (working directory, home or qcodes dir)
auto_reconnect_instrument = qcodes.config["user"]["auto_reconnect_instrument"]

class StationConfigurator:
    """
    The StationConfig class enables the easy creation of intstruments from a
    yaml file.
    Example:
    >>> scfg = StationConfig('exampleConfig.yaml')
    >>> dmm1 = scfg.create_instrument('dmm1')
    """

    PARAMETER_ATTRIBUTES = ['label', 'unit', 'scale', 'inter_delay', 'delay',
                            'step']

    def __init__(self, filename: str, station: Optional[Station] = None) -> None:
        self.monitor_parameters = {}
        # self.station = station if is not None else Station.default if is not None else Station()
        if station is None:
            station = Station.default or Station()
        self.station = station

        with open(filename, 'r') as f:
            self.config = yaml.load(f)
            self._instrument_config = self.config['instruments']

        class ConfigComponent:
            def __init__(self, data):
                self.data = data

            def snapshot(self, update=True):
                return self.data

        # self.station.add_component(ConfigComponent(self.config), 'Config')
        self.station.components['StationConfigurator'] = ConfigComponent(self.config)

    def load_instrument(self, identifier: str,
                          **kwargs) -> Instrument:
        """
        Creates an instrument driver as described by the loaded config file.

        Args:
            identifier: the identfying string that is looked up in the yaml
                configuration file, which identifies the instrument to be added
            **kwargs: additional keyword arguments that get passed on to the
                __init__-method of the instrument to be added.
        """

        # load config
        if identifier not in self._instrument_config.keys():
            raise RuntimeError('Instrument {} not found in config.'
                               .format(identifier))
        instr_cfg = self._instrument_config[identifier]

        # config is not parsed for errors. On errors qcodes should be able to
        # to report them

        # check if instrument is already defined and close connection
        if instr_cfg.get('auto_reconnect', auto_reconnect_instrument):
            # save close instrument and remove from monitor list
            with suppress(KeyError):
                instr = Instrument.find_instrument(identifier)
                # remove parameters related to this instrument from the monitor list
                self.monitor_parameters = {k:v for k,v in self.monitor_parameters.items() if v.get_root_instrument() is not instr}
                instr.close()

        # instantiate instrument
        module = importlib.import_module(instr_cfg['driver'])
        instr_class = getattr(module, instr_cfg['type'])

        init_kwargs = instr_cfg.get('init',{})
        # somebody might have a empty init section in the config
        init_kwargs = {} if init_kwargs is None else init_kwargs
        if 'address' in instr_cfg:
            init_kwargs['address'] = instr_cfg['address']
        instr = instr_class(identifier, **init_kwargs)
        # setup

        # local function to refactor common code from defining new parameter
        # and setting existing one
        def setup_parameter_from_dict(p, options):
            for attr, val in options.items():
                if attr in self.PARAMETER_ATTRIBUTES:
                    # set the attributes of the parameter, that map 1 to 1
                    setattr(p, attr, val)
                    # extra attributes that need parsing
                elif attr == 'limits':
                    lower, upper = [float(x) for x in val.split(',')]
                    p.vals = validators.Numbers(lower, upper)
                elif attr == 'monitor' and val is True:
                    self.monitor_parameters[id(p)] = p
                elif attr == 'alias':
                    setattr(instr, val, p)
                elif attr == 'value':
                    # skip value attribute so that it gets set last
                    # when everything else has been set up
                    pass
                else:
                    log.warning(f'Attribute {attr} no recognized when'
                                f' instatiating parameter \"{p.name}\"')
            if 'value' in options:
                p.set(options['value'])

        # setup existing parameters
        for name, options in instr_cfg.get('parameters', {}).items():
            # get the parameter object from its name:
            p = instr
            for level in name.split('.'):
                p = getattr(p, level)
            setup_parameter_from_dict(p, options)

        # setup new parameters
        for name, options in instr_cfg.get('new_parameters', {}).items():
            # allow only top level paremeters for now
            # pop source only temporarily
            source = options.pop('source', False)
            if source:
                source_p = instr
                for level in source.split('.'):
                    source_p = getattr(source_p, level)
                instr.add_parameter(name,
                                    Parameter,
                                    get_cmd=source_p.get,
                                    set_cmd=source_p.set)
            else:
                instr.add_parameter(name, Parameter)
            p = getattr(instr, name)
            setup_parameter_from_dict(p, options)
            # restore source
            options['source'] = source

        # add the instrument to the station
        self.station.add_component(instr)

        # restart the monitor to apply the changes
        self._restart_monitor()
        return instr

    def _restart_monitor(self):
        if Monitor.running:
            Monitor.running.stop()
        Monitor(*self.monitor_parameters.values())
