# -*- coding: utf-8 -*-
from copy import deepcopy
from aiida.common.extendeddicts import AttributeDict
from aiida.orm import Code
from aiida.orm.data.base import Bool, Float, Int, Str
from aiida.orm.data.folder import FolderData
from aiida.orm.data.parameter import ParameterData
from aiida.orm.data.structure import StructureData
from aiida.orm.data.array.kpoints import KpointsData
from aiida.orm.utils import CalculationFactory
from aiida.work.run import submit
from aiida.work.workchain import WorkChain, ToContext, while_, append_
from aiida_quantumespresso.common.workchain import ErrorHandlerReport
from aiida_quantumespresso.common.workchain import register_error_handler
from aiida_quantumespresso.utils.defaults.calculation import pw as qe_defaults
from aiida_quantumespresso.utils.resources import get_default_options
from aiida_quantumespresso.workflows import BaseRestartWorkChain

PhCalculation = CalculationFactory('quantumespresso.ph')
PwCalculation = CalculationFactory('quantumespresso.pw')

class PhBaseWorkChain(BaseRestartWorkChain):
    """
    Base Workchain to launch a Quantum Espresso phonon ph.x calculation and restart it until
    successfully converged or until the maximum number of restarts is exceeded
    """
    _verbose = True
    _calculation_class = PhCalculation

    def __init__(self, *args, **kwargs):
        super(PhBaseWorkChain, self).__init__(*args, **kwargs)

        self.defaults = AttributeDict({
            'qe': qe_defaults,
            'delta_factor_max_seconds': 0.95,
            'alpha_mix': 0.70,
        })

    @classmethod
    def define(cls, spec):
        super(PhBaseWorkChain, cls).define(spec)
        spec.input('code', valid_type=Code)
        spec.input('qpoints', valid_type=KpointsData)
        spec.input('parameters', valid_type=ParameterData)
        spec.input('parent_calc', valid_type=PwCalculation)
        spec.input('settings', valid_type=ParameterData, required=False)
        spec.input('options', valid_type=ParameterData, required=False)
        spec.outline(
            cls.setup,
            cls.validate_inputs,
            while_(cls.should_run_calculation)(
                cls.prepare_calculation,
                cls.run_calculation,
                cls.inspect_calculation,
            ),
            cls.results,
        )
        spec.output('output_parameters', valid_type=ParameterData)
        spec.output('retrieved', valid_type=FolderData)

    def validate_inputs(self):
        """
        Define context dictionary 'inputs_raw' with the inputs for the PhCalculations as they were at the beginning
        of the workchain. Changes have to be made to a deep copy so this remains unchanged and we can always reset
        the inputs to their initial state. Inputs that are not required by the workchain will be given a default value
        if not specified or be validated otherwise.
        """
        self.ctx.inputs_raw = AttributeDict({
            'code': self.inputs.code,
            'qpoints': self.inputs.qpoints,
            'parameters': self.inputs.parameters.get_dict(),
        })

        if 'INPUTPH' not in self.ctx.inputs_raw.parameters:
            self.ctx.inputs_raw.parameters['INPUTPH'] = {}

        if 'settings' in self.inputs:
            self.ctx.inputs_raw.settings = self.inputs.settings.get_dict()
        else:
            self.ctx.inputs_raw.settings = {}

        if 'options' in self.inputs:
            self.ctx.inputs_raw._options = self.inputs.options.get_dict()
        else:
            self.ctx.inputs_raw._options = get_default_options()

        self.ctx.restart_calc = self.inputs.parent_calc

        # Assign a deepcopy to self.ctx.inputs which will be used by the BaseRestartWorkChain
        self.ctx.inputs = deepcopy(self.ctx.inputs_raw)

    def prepare_calculation(self):
        """
        Prepare the inputs for the next calculation
        """
        if isinstance(self.ctx.restart_calc, PhCalculation):
            self.ctx.inputs.parameters['INPUTPH']['recover'] = True

        self.ctx.inputs.parent_folder = self.ctx.restart_calc.out.remote_folder

    def _prepare_process_inputs(self, inputs):
        """
        The 'max_seconds' setting in the 'INPUTPH' card of the parameters will be set to a fraction of the
        'max_wallclock_seconds' that will be given to the job via the '_options' dictionary. This will prevent the job
        from being prematurely terminated by the scheduler without getting the chance to exit cleanly.
        """
        max_wallclock_seconds = inputs._options['max_wallclock_seconds']
        max_seconds_factor = self.defaults.delta_factor_max_seconds
        max_seconds = max_wallclock_seconds * max_seconds_factor
        inputs.parameters['INPUTPH']['max_seconds'] = max_seconds

        return super(PhBaseWorkChain, self)._prepare_process_inputs(inputs)



@register_error_handler(PhBaseWorkChain, 400)
def _handle_fatal_error_read_namelists(self, calculation):
    """
    The calculation failed because it could not read the generated input file
    """
    if any(['reading inputph namelist' in w for w in calculation.res.warnings]):
        self.abort_nowait('PhCalculation<{}> failed because of an invalid input file'
            .format(calculation.pk))
        return ErrorHandlerReport(True, True)

@register_error_handler(PhBaseWorkChain, 300)
def _handle_error_exceeded_maximum_walltime(self, calculation):
    """
    Calculation ended nominally but ran out of allotted wall time
    """
    if 'Maximum CPU time exceeded' in calculation.res.warnings:
        self.ctx.restart_calc = calculation
        self.report('PhCalculation<{}> terminated because maximum wall time was exceeded, restarting'
            .format(calculation.pk))
        return ErrorHandlerReport(True, True)

@register_error_handler(PhBaseWorkChain, 200)
def _handle_fatal_error_not_converged(self, calculation):
    """
    The calculation failed because it could not read the generated input file
    """
    if ('Phonon did not reach end of self consistency' in calculation.res.warnings):
        alpha_mix_old = calculation.inp.parameters.get_dict()['INPUTPH'].get('alpha_mix(1)', self.defaults.alpha_mix)
        alpha_mix_new = 0.9 * alpha_mix_old
        self.ctx.inputs.parameters['INPUTPH']['alpha_mix(1)'] = alpha_mix_new
        self.ctx.restart_calc = calculation
        self.report('PhCalculation<{}> terminated without reaching convergence, '
            'setting alpha_mix to {} and restarting'.format(calculation.pk, alpha_mix_new))
        return ErrorHandlerReport(True, True)

@register_error_handler(PhBaseWorkChain, 100)
def _handle_error_premature_termination(self, calculation):
    """
    Calculation did not reach the end of execution, probably because it was killed by the scheduler
    for running out of allotted walltime
    """
    if 'QE ph run did not reach the end of the execution.' in calculation.res.parser_warnings:
        inputs = calculation.inp.parameters.get_dict()
        settings = self.ctx.inputs.settings

        factor = self.defaults.delta_factor_max_seconds
        max_seconds = settings.get('max_seconds', inputs['INPUTPH']['max_seconds'])
        max_seconds_reduced = int(max_seconds * factor)
        self.ctx.inputs.parameters['INPUTPH']['max_seconds'] = max_seconds_reduced

        self.report('PwCalculation<{}> was terminated prematurely, reducing "max_seconds" from {} to {}'
            .format(calculation.pk, max_seconds, max_seconds_reduced))
        return ErrorHandlerReport(True, False)