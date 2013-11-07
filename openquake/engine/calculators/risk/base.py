# -*- coding: utf-8 -*-

# Copyright (c) 2010-2013, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

"""Base RiskCalculator class."""

from functools import wraps
import collections

from openquake.engine import logs, export
from openquake.engine.utils import config, stats, tasks
from openquake.engine.db import models
from openquake.engine.calculators import base
from openquake.engine.calculators.risk import writers, validation, loaders


class RiskCalculator(base.Calculator):
    """
    Abstract base class for risk calculators. Contains a bunch of common
    functionality, including initialization procedures and the core
    distribution/execution logic.

    :attribute dict taxonomies_asset_count:
        A dictionary mapping each taxonomy with the number of assets the
        calculator will work on. Assets are extracted from the exposure input
        and filtered according to the `RiskCalculation.region_constraint`.

    :attribute dict risk_models:
        A nested dict taxonomy -> loss type -> instances of `RiskModel`.
    """

    # a list of :class:`openquake.engine.calculators.risk.validation` classes
    validators = [validation.HazardIMT, validation.EmptyExposure,
                  validation.OrphanTaxonomies, validation.ExposureLossTypes,
                  validation.NoRiskModels]

    def __init__(self, job):
        super(RiskCalculator, self).__init__(job)

        self.taxonomies_asset_count = None
        self.risk_models = None

    def pre_execute(self):
        """
        In this phase, the general workflow is:
            1. Parse the exposure to get the taxonomies
            2. Parse the available risk models
            3. Initialize progress counters
            4. Validate exposure and risk models
        """
        with logs.tracing('get exposure'):
            self.taxonomies_asset_count = (
                (self.rc.preloaded_exposure_model or loaders.exposure(
                    self.job,
                    self.rc.inputs['exposure'])).taxonomies_in(
                        self.rc.region_constraint))

        with logs.tracing('parse risk models'):
            self.risk_models = self.get_risk_models()

            # consider only the taxonomies in the risk models if
            # taxonomies_from_model has been set to True in the
            # job.ini
            if self.rc.taxonomies_from_model:
                self.taxonomies_asset_count = dict(
                    (t, count)
                    for t, count in self.taxonomies_asset_count.items()
                    if t in self.risk_models)

        self._initialize_progress(sum(self.taxonomies_asset_count.values()))

        for validator_class in self.validators:
            validator = validator_class(self)
            error = validator.get_error()
            if error:
                raise ValueError("""Problems in calculator configuration:
                                 %s""" % error)

    def block_size(self):
        """
        Number of assets handled per task.
        """
        return int(config.get('risk', 'block_size'))

    def expected_tasks(self, block_size):
        """
        Number of tasks generated by the task_arg_gen
        """
        num_tasks = 0
        for num_assets in self.taxonomies_asset_count.values():
            n, r = divmod(num_assets, block_size)
            if r:
                n += 1
            num_tasks += n
        return num_tasks

    def concurrent_tasks(self):
        """
        Number of tasks to be in queue at any given time.
        """
        return int(config.get('risk', 'concurrent_tasks'))

    def task_arg_gen(self, block_size):
        """
        Generator function for creating the arguments for each task.

        It is responsible for the distribution strategy. It divides
        the considered exposure into chunks of homogeneous assets
        (i.e. having the same taxonomy). The chunk size is given by
        the `block_size` openquake config parameter

        :param int block_size:
            The number of work items per task (sources, sites, etc.).

        :returns:
            An iterator over a list of arguments. Each contains:

            1. the job id
            2. a getter object needed to get the hazard data
            3. the needed risklib calculators
            4. the output containers to be populated
            5. the specific calculator parameter set
        """
        output_containers = writers.combine_builders(
            [builder(self) for builder in self.output_builders])

        num_tasks = 0
        for taxonomy, assets_nr in self.taxonomies_asset_count.items():
            asset_offsets = range(0, assets_nr, block_size)

            for offset in asset_offsets:
                with logs.tracing("getting assets"):
                    assets = models.ExposureData.objects.get_asset_chunk(
                        self.rc, taxonomy, offset, block_size)

                calculation_units = [
                    self.calculation_unit(loss_type, assets)
                    for loss_type in models.loss_types(self.risk_models)]

                num_tasks += 1
                yield [self.job.id,
                       calculation_units,
                       output_containers,
                       self.calculator_parameters]

        # sanity check to protect against future changes of the distribution
        # logic
        expected_tasks = self.expected_tasks(block_size)
        if num_tasks != expected_tasks:
            raise RuntimeError('Expected %d tasks, generated %d!' % (
                               expected_tasks, num_tasks))

    def _get_outputs_for_export(self):
        """
        Util function for getting :class:`openquake.engine.db.models.Output`
        objects to be exported.
        """
        return export.core.get_outputs(self.job.id)

    def _do_export(self, output_id, export_dir, export_type):
        """
        Risk-specific implementation of
        :meth:`openquake.engine.calculators.base.Calculator._do_export`.

        Calls the risk exporter.
        """
        return export.risk.export(output_id, export_dir, export_type)

    @property
    def rc(self):
        """
        A shorter and more convenient way of accessing the
        :class:`~openquake.engine.db.models.RiskCalculation`.
        """
        return self.job.risk_calculation

    @property
    def hc(self):
        """
        A shorter and more convenient way of accessing the
        :class:`~openquake.engine.db.models.HazardCalculation`.
        """
        return self.rc.get_hazard_calculation()

    @property
    def calculator_parameters(self):
        """
        The specific calculation parameters passed as args to the
        celery task function. A calculator must override this to
        provide custom arguments to its celery task
        """
        return []

    def _initialize_progress(self, total):
        """Record the total/completed number of work items.

        This is needed for the purpose of providing an indication of progress
        to the end user."""
        logs.LOG.debug("Computing risk over %d assets" % total)
        self.progress.update(total=total)
        stats.pk_set(self.job.id, "lvr", 0)
        stats.pk_set(self.job.id, "nrisk_total", total)
        stats.pk_set(self.job.id, "nrisk_done", 0)

    def get_risk_models(self, retrofitted=False):
        """
        Parse vulnerability models for each loss type in
        `openquake.engine.db.models.LOSS_TYPES`,
        then set the `risk_models` attribute.

        :param bool retrofitted:
            True if retrofitted models should be retrieved
        :returns:
            A nested dict taxonomy -> loss type -> instances of `RiskModel`.
        """
        risk_models = collections.defaultdict(dict)

        for v_input, loss_type in self.rc.vulnerability_inputs(retrofitted):
            for taxonomy, model in loaders.vulnerability(v_input):
                risk_models[taxonomy][loss_type] = model

        return risk_models


class count_progress_risk(stats.count_progress):   # pylint: disable=C0103
    """
    Extend :class:`openquake.engine.utils.stats.count_progress` to work with
    celery task where the number of items (i.e. assets) are embedded in
    calculation units.
    """
    def get_task_data(self, job_id, units, *_args):
        num_items = get_num_items(units)

        return job_id, num_items


def get_num_items(units):
    """
    :param units:
        a not empty lists of
        :class:`openquake.engine.calculators.risk.base.CalculationUnit`
        instances
    """
    # Get the getter (an instance of `..hazard_getters.HazardGetter`)
    # from the first unit. A getter keeps a reference to the list of
    # assets we want to consider
    return len(units[0].getter.assets)


def risk_task(task):
    @wraps(task)
    def fn(job_id, units, *args):
        task(job_id, units, *args)
        num_items = get_num_items(units)
        base.signal_task_complete(job_id=job_id, num_items=num_items)
    fn.ignore_result = False

    return tasks.oqtask(count_progress_risk('r')(fn))


#: Calculator parameters are used to compute derived outputs like loss
#: maps, disaggregation plots, quantile/mean curves. See
#: :class:`openquake.engine.db.models.RiskCalculation` for a description

CalcParams = collections.namedtuple(
    'CalcParams', [
        'conditional_loss_poes',
        'poes_disagg',
        'sites_disagg',
        'insured_losses',
        'quantiles',
        'asset_life_expectancy',
        'interest_rate',
        'mag_bin_width',
        'distance_bin_width',
        'coordinate_bin_width',
        'damage_state_ids'
    ])


def make_calc_params(conditional_loss_poes=None,
                     poes_disagg=None,
                     sites_disagg=None,
                     insured_losses=None,
                     quantiles=None,
                     asset_life_expectancy=None,
                     interest_rate=None,
                     mag_bin_width=None,
                     distance_bin_width=None,
                     coordinate_bin_width=None,
                     damage_state_ids=None):
    """
    Constructor of CalculatorParameters
    """
    return CalcParams(conditional_loss_poes,
                      poes_disagg,
                      sites_disagg,
                      insured_losses,
                      quantiles,
                      asset_life_expectancy,
                      interest_rate,
                      mag_bin_width,
                      distance_bin_width,
                      coordinate_bin_width,
                      damage_state_ids)
