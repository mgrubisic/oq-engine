# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2018 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

import os.path
import logging
import operator
import collections
import numpy

from openquake.baselib import hdf5, datastore
from openquake.baselib.python3compat import zip
from openquake.baselib.general import (
    AccumDict, split_in_blocks, split_in_slices, humansize, cached_property)
from openquake.hazardlib.probability_map import ProbabilityMap
from openquake.hazardlib.stats import compute_pmap_stats
from openquake.hazardlib.calc.stochastic import sample_ruptures
from openquake.risklib.riskinput import str2rsi
from openquake.baselib import parallel
from openquake.commonlib import calc, util
from openquake.calculators import base
from openquake.calculators.getters import GmfGetter, RuptureGetter
from openquake.calculators.classical import ClassicalCalculator

U8 = numpy.uint8
U16 = numpy.uint16
U32 = numpy.uint32
U64 = numpy.uint64
F32 = numpy.float32
F64 = numpy.float64
TWO32 = U64(2 ** 32)
rlzs_by_grp_dt = numpy.dtype(
    [('grp_id', U16), ('gsim_id', U16), ('rlzs', hdf5.vuint16)])


def replace_eid(data, eid2idx):
    """
    Convert from event IDs to event indices.

    :param data: an array with a field eid
    :param eid2idx: a dictionary eid -> idx
    """
    uniq, inv = numpy.unique(data['eid'], return_inverse=True)
    data['eid'] = numpy.array([eid2idx[eid] for eid in uniq])[inv]


def store_rlzs_by_grp(dstore):
    """
    Save in the datastore a composite array with fields (grp_id, gsim_id, rlzs)
    """
    lst = []
    assoc = dstore['csm_info'].get_rlzs_assoc()
    logging.info('There are %d realizations', len(assoc.realizations))
    for grp, arr in assoc.by_grp().items():
        for gsim_id, rlzs in enumerate(arr):
            lst.append((int(grp[4:]), gsim_id, rlzs))
    dstore['csm_info/rlzs_by_grp'] = numpy.array(lst, rlzs_by_grp_dt)


def build_ruptures(srcs, srcfilter, param, monitor):
    """
    A small wrapper around :func:
    `openquake.hazardlib.calc.stochastic.sample_ruptures`
    """
    acc = []
    n = 0
    mon = monitor('making contexts', measuremem=False)
    for src in srcs:
        dic = sample_ruptures([src], param, srcfilter, mon)
        vars(src).update(dic)
        acc.append(src)
        n += len(dic['eb_ruptures'])
        if n > param['ruptures_per_block']:
            yield acc
            n = 0
            acc.clear()
    if acc:
        yield acc


def get_events(ebruptures, rlzs_by_gsim):
    ebrs = list(ebruptures)  # iterate on the rupture getter
    if not ebrs:
        return ()
    return numpy.concatenate(
        [ebr.get_events(rlzs_by_gsim) for ebr in ebrs])


def max_gmf_size(ruptures_by_grp, num_rlzs, samples_by_grp, num_imts):
    """
    :param ruptures_by_grp: dictionary grp_id -> EBRuptures
    :param num_rlzs: dictionary grp_id -> number of realizations
    :param samples_by_grp: dictionary grp_id -> samples
    :param num_imts: number of IMTs
    :returns:
        the size of the GMFs generated by the ruptures, by excess, if
        minimum_intensity is set
    """
    # ('rlzi', U16), ('sid', U32),  ('eid', U64), ('gmv', (F32, (len(imtls),)))
    nbytes = 2 + 4 + 8 + 4 * num_imts
    n = 0
    for grp_id, ebruptures in ruptures_by_grp.items():
        for ebr in ebruptures:
            n += len(ebr.rupture.sctx.sids) * ebr.n_occ
    return n * nbytes


# ######################## GMF calculator ############################ #

def update_nbytes(dstore, key, array):
    nbytes = dstore.get_attr(key, 'nbytes', 0)
    dstore.set_attrs(key, nbytes=nbytes + array.nbytes)


def get_mean_curves(dstore):
    """
    Extract the mean hazard curves from the datastore, as a composite
    array of length nsites.
    """
    return dstore['hcurves/mean'].value

# ########################################################################## #


def compute_gmfs(ruptures, src_filter, rlzs_by_gsim, param, monitor):
    """
    Compute GMFs and optionally hazard curves
    """
    res = AccumDict(ruptures={})
    if isinstance(ruptures, RuptureGetter):
        # the ruptures are read from the datastore
        grp_id = ruptures.grp_id
        sitecol = src_filter  # this is actually a site collection
    else:
        # use the ruptures sampled in prefiltering
        grp_id = ruptures[0].grp_id
        sitecol = src_filter.sitecol
    res['ruptures'] = {grp_id: ruptures}
    getter = GmfGetter(
        rlzs_by_gsim, ruptures, sitecol,
        param['oqparam'], param['min_iml'])
    res.update(getter.compute_gmfs_curves(monitor))
    return res


@base.calculators.add('event_based')
class EventBasedCalculator(base.HazardCalculator):
    """
    Event based PSHA calculator generating the ground motion fields and
    the hazard curves from the ruptures, depending on the configuration
    parameters.
    """
    core_task = compute_gmfs
    is_stochastic = True

    @cached_property
    def csm_info(self):
        """
        :returns: a cached CompositionInfo object
        """
        try:
            return self.csm.info
        except AttributeError:
            return self.datastore.parent['csm_info']

    def init(self):
        if hasattr(self, 'csm'):
            self.check_floating_spinning()
        self.rupser = calc.RuptureSerializer(self.datastore)

    def init_logic_tree(self, csm_info):
        self.grp_trt = csm_info.grp_by("trt")
        self.rlzs_assoc = csm_info.get_rlzs_assoc()
        self.rlzs_by_gsim_grp = csm_info.get_rlzs_by_gsim_grp()
        self.samples_by_grp = csm_info.get_samples_by_grp()
        self.num_rlzs_by_grp = {
            grp_id:
            sum(len(rlzs) for rlzs in self.rlzs_by_gsim_grp[grp_id].values())
            for grp_id in self.rlzs_by_gsim_grp}
        self.R = len(self.rlzs_assoc.realizations)
        self.mon_rups = self.monitor('saving ruptures', measuremem=False)
        self.mon_evs = self.monitor('saving events', measuremem=False)

    def from_ruptures(self, param):
        """
        :yields: the arguments for compute_gmfs_and_curves
        """
        oq = self.oqparam
        self.init_logic_tree(self.csm_info)
        concurrent_tasks = oq.concurrent_tasks
        U = len(self.datastore.parent['ruptures'])
        logging.info('Found %d ruptures', U)
        parent = self.can_read_parent() or self.datastore

        def genargs():
            for slc in split_in_slices(U, concurrent_tasks or 1):
                for grp_id in self.rlzs_by_gsim_grp:
                    rlzs_by_gsim = self.rlzs_by_gsim_grp[grp_id]
                    ruptures = RuptureGetter(parent, slc, grp_id)
                    par = param.copy()
                    par['samples'] = self.samples_by_grp[grp_id]
                    yield ruptures, self.sitecol, rlzs_by_gsim, par
        return genargs()

    def zerodict(self):
        """
        Initial accumulator, a dictionary (grp_id, gsim) -> curves
        """
        self.L = len(self.oqparam.imtls.array)
        zd = {r: ProbabilityMap(self.L) for r in range(self.R)}
        return zd

    def _store_ruptures(self, srcs_by_grp):
        gmf_size = 0
        calc_times = AccumDict(accum=numpy.zeros(3, F32))
        for grp, srcs in srcs_by_grp.items():
            for src in srcs:
                self.save_ruptures(src.eb_ruptures)
                gmf_size += max_gmf_size(
                    {src.src_group_id: src.eb_ruptures},
                    self.num_rlzs_by_grp,
                    self.samples_by_grp,
                    len(self.oqparam.imtls))
                calc_times += src.calc_times
        self.rupser.close()
        if gmf_size:
            self.datastore.set_attrs('events', max_gmf_size=gmf_size)
            msg = 'less than ' if self.min_iml.sum() else ''
            logging.info('Estimating %s%s of GMFs', msg, humansize(gmf_size))

        with self.monitor('store source_info', autoflush=True):
            self.store_source_info(calc_times)

    def from_sources(self, par):
        """
        Prefilter the composite source model and store the source_info
        """
        gsims_by_trt = self.csm.gsim_lt.values

        def weight_src(src, factor=numpy.sqrt(len(self.sitecol))):
            return src.num_ruptures * factor

        def weight_rup(ebr):
            return 1

        logging.info('Building ruptures')
        smap = parallel.Starmap(build_ruptures, monitor=self.monitor())
        eff_ruptures = AccumDict(accum=0)  # grp_id => potential ruptures
        srcs_by_grp = AccumDict(accum=[])  # grp_id => srcs
        for sm in self.csm.source_models:
            logging.info('Sending %s', sm)
            for sg in sm.src_groups:
                if not sg.sources:
                    continue
                par['gsims'] = gsims_by_trt[sg.trt]
                eff_ruptures[sg.id] += sum(src.num_ruptures for src in sg)
                for block in self.block_splitter(sg.sources, weight_src):
                    smap.submit(block, self.src_filter, par)
        for srcs in smap:
            srcs_by_grp[srcs[0].src_group_id] += srcs

        # logic tree reduction
        self.store_csm_info(
            {gid: sum(src.num_ruptures for src in srcs_by_grp[gid])
             for gid in srcs_by_grp})
        store_rlzs_by_grp(self.datastore)
        self.init_logic_tree(self.csm.info)
        self._store_ruptures(srcs_by_grp)

        # reorder events
        evs = self.datastore['events'].value
        evs.sort(order='eid')
        self.datastore['events'] = evs
        nr = len(self.datastore['ruptures'])
        ne = len(evs)
        logging.info('Stored {:,d} ruptures and {:,d} events'.format(nr, ne))

        def genargs():
            ruptures = []
            for grp_id, srcs in srcs_by_grp.items():
                for src in srcs:
                    ruptures.extend(src.eb_ruptures)
            ruptures.sort(key=operator.attrgetter('serial'))  # not mandatory
            ct = self.oqparam.concurrent_tasks or 1
            for rups in split_in_blocks(ruptures, ct,
                                        key=operator.attrgetter('grp_id')):
                ebr = rups[0]
                rlzs_by_gsim = self.rlzs_by_gsim_grp[ebr.grp_id]
                par['samples'] = self.samples_by_grp[ebr.grp_id]
                yield rups, self.src_filter, rlzs_by_gsim, par

            if self.oqparam.ground_motion_fields:
                logging.info('Processing the GMFs')
        return genargs()

    def agg_dicts(self, acc, result):
        """
        :param acc: accumulator dictionary
        :param result: an AccumDict with events, ruptures, gmfs and hcurves
        """
        ucerf = self.oqparam.calculation_mode.startswith('ucerf')
        if ucerf:
            [ruptures] = result.ruptures_by_grp.values()
            events = self.save_ruptures(ruptures)
            eid2idx = {}
            if len(events):
                for eid in events['eid']:
                    eid2idx[eid] = self.idx
                    self.idx += 1
        else:
            eid2idx = self.eid2idx
        sav_mon = self.monitor('saving gmfs')
        agg_mon = self.monitor('aggregating hcurves')
        if 'gmdata' in result:
            self.gmdata += result['gmdata']
            with sav_mon:
                data = result.pop('gmfdata')
                replace_eid(data, eid2idx)  # this has to be fast
                self.datastore.extend('gmf_data/data', data)
                # it is important to save the number of bytes while the
                # computation is going, to see the progress
                update_nbytes(self.datastore, 'gmf_data/data', data)
                for sid, start, stop in result['indices']:
                    self.indices[sid, 0].append(start + self.offset)
                    self.indices[sid, 1].append(stop + self.offset)
                self.offset += len(data)
                if self.offset >= TWO32:
                    raise RuntimeError(
                        'The gmf_data table has more than %d rows' % TWO32)
        imtls = self.oqparam.imtls
        with agg_mon:
            for key, poes in result.get('hcurves', {}).items():
                r, sid, imt = str2rsi(key)
                array = acc[r].setdefault(sid, 0).array[imtls(imt), 0]
                array[:] = 1. - (1. - array) * (1. - poes)
        sav_mon.flush()
        agg_mon.flush()
        self.datastore.flush()
        return acc

    def save_ruptures(self, ruptures):
        """
        :param ruptures: a list of EBRuptures
        """
        if len(ruptures):
            with self.mon_rups:
                self.rupser.save(ruptures)
            with self.mon_evs:
                rlzs_by_gsim = self.rlzs_by_gsim_grp[ruptures[0].grp_id]
                events = get_events(ruptures, rlzs_by_gsim)
                num_rlzs = sum(len(rlzs) for rlzs in rlzs_by_gsim.values())
                eids = numpy.concatenate([rup.get_eids(num_rlzs)
                                          for rup in ruptures])
                numpy.testing.assert_equal(eids, events['eid'])
                self.datastore.extend('events', events)
            return events
        return ()

    def check_overflow(self):
        """
        Raise a ValueError if the number of sites is larger than 65,536 or the
        number of IMTs is larger than 256 or the number of ruptures is larger
        than 4,294,967,296. The limits are due to the numpy dtype used to
        store the GMFs (gmv_dt). They could be relaxed in the future.
        """
        max_ = dict(sites=2**16, events=2**32, imts=2**8)
        try:
            events = len(self.datastore['events'])
        except KeyError:
            events = 0
        num_ = dict(sites=len(self.sitecol), events=events,
                    imts=len(self.oqparam.imtls))
        for var in max_:
            if num_[var] > max_[var]:
                raise ValueError(
                    'The event based calculator is restricted to '
                    '%d %s, got %d' % (max_[var], var, num_[var]))

    def execute(self):
        oq = self.oqparam
        self.gmdata = {}
        self.offset = 0
        self.indices = collections.defaultdict(list)  # sid, idx -> indices
        self.min_iml = self.get_min_iml(oq)
        param = self.param.copy()
        param.update(
            oqparam=oq, min_iml=self.min_iml,
            gmf=oq.ground_motion_fields,
            truncation_level=oq.truncation_level,
            ruptures_per_block=oq.ruptures_per_block,
            imtls=oq.imtls, filter_distance=oq.filter_distance,
            ses_per_logic_tree_path=oq.ses_per_logic_tree_path)
        if oq.hazard_calculation_id:  # from ruptures
            assert oq.ground_motion_fields, 'must be True!'
            self.datastore.parent = datastore.read(oq.hazard_calculation_id)
            iterargs = self.from_ruptures(param)
        else:  # from sources
            iterargs = self.from_sources(param)
            if oq.ground_motion_fields is False:
                for args in iterargs:  # store the ruptures/events
                    pass
                return {}
        self.idx = 0  # event ID index, used for UCERF
        # call compute_gmfs in parallel
        acc = parallel.Starmap(
            self.core_task.__func__, iterargs, self.monitor()
        ).reduce(self.agg_dicts, self.zerodict())
        self.check_overflow()  # check the number of events
        base.save_gmdata(self, self.R)
        if self.indices:
            N = len(self.sitecol.complete)
            logging.info('Saving gmf_data/indices')
            with self.monitor('saving gmf_data/indices', measuremem=True,
                              autoflush=True):
                self.datastore['gmf_data/imts'] = ' '.join(oq.imtls)
                dset = self.datastore.create_dset(
                    'gmf_data/indices', hdf5.vuint32,
                    shape=(N, 2), fillvalue=None)
                num_evs = self.datastore.create_dset(
                    'gmf_data/events_by_sid', U32, (N,))
                for sid in self.sitecol.complete.sids:
                    start = numpy.array(self.indices[sid, 0])
                    stop = numpy.array(self.indices[sid, 1])
                    dset[sid, 0] = start
                    dset[sid, 1] = stop
                    num_evs[sid] = (stop - start).sum()
                self.datastore.set_attrs(
                    'gmf_data', avg_events_by_sid=num_evs.value.sum() / N,
                    max_events_by_sid=num_evs.value.max())
        elif (oq.ground_motion_fields and
              'ucerf' not in oq.calculation_mode):
            raise RuntimeError('No GMFs were generated, perhaps they were '
                               'all below the minimum_intensity threshold')
        return acc

    def save_gmf_bytes(self):
        """Save the attribute nbytes in the gmf_data datasets"""
        ds = self.datastore
        for sm_id in ds['gmf_data']:
            ds.set_nbytes('gmf_data/' + sm_id)
        ds.set_nbytes('gmf_data')

    @cached_property
    def eid2idx(self):
        eids = self.datastore['events']['eid']
        eid2idx = dict(zip(eids, numpy.arange(len(eids), dtype=U32)))
        return eid2idx

    def post_execute(self, result):
        oq = self.oqparam
        if 'ucerf' in oq.calculation_mode:
            self.rupser.close()
            self.csm.info.update_eff_ruptures(self.csm.get_num_ruptures())
        N = len(self.sitecol.complete)
        L = len(oq.imtls.array)
        if result and oq.hazard_curves_from_gmfs:
            rlzs = self.rlzs_assoc.realizations
            # compute and save statistics; this is done in process and can
            # be very slow if there are thousands of realizations
            weights = [rlz.weight for rlz in rlzs]
            # NB: in the future we may want to save to individual hazard
            # curves if oq.individual_curves is set; for the moment we
            # save the statistical curves only
            hstats = oq.hazard_stats()
            pmaps = list(result.values())
            if len(hstats):
                logging.info('Computing statistical hazard curves')
                if len(weights) != len(pmaps):
                    # this should never happen, unless I break the
                    # logic tree reduction mechanism during refactoring
                    raise AssertionError('Expected %d pmaps, got %d' %
                                         (len(weights), len(pmaps)))
                for statname, stat in hstats:
                    pmap = compute_pmap_stats(pmaps, [stat], weights)
                    arr = numpy.zeros((N, L), F32)
                    for sid in pmap:
                        arr[sid] = pmap[sid].array[:, 0]
                    self.datastore['hcurves/' + statname] = arr
                    if oq.poes:
                        P = len(oq.poes)
                        I = len(oq.imtls)
                        self.datastore.create_dset(
                            'hmaps/' + statname, F32, (N, P * I))
                        self.datastore.set_attrs(
                            'hmaps/' + statname, nbytes=N * P * I * 4)
                        hmap = calc.make_hmap(pmap, oq.imtls, oq.poes)
                        ds = self.datastore['hmaps/' + statname]
                        for sid in hmap:
                            ds[sid] = hmap[sid].array[:, 0]

        if self.datastore.parent:
            self.datastore.parent.open('r')
        if 'gmf_data' in self.datastore:
            self.save_gmf_bytes()
        if oq.compare_with_classical:  # compute classical curves
            export_dir = os.path.join(oq.export_dir, 'cl')
            if not os.path.exists(export_dir):
                os.makedirs(export_dir)
            oq.export_dir = export_dir
            # one could also set oq.number_of_logic_tree_samples = 0
            self.cl = ClassicalCalculator(oq)
            # TODO: perhaps it is possible to avoid reprocessing the source
            # model, however usually this is quite fast and do not dominate
            # the computation
            self.cl.run(close=False)
            cl_mean_curves = get_mean_curves(self.cl.datastore)
            eb_mean_curves = get_mean_curves(self.datastore)
            rdiff, index = util.max_rel_diff_index(
                cl_mean_curves, eb_mean_curves)
            logging.warn('Relative difference with the classical '
                         'mean curves: %d%% at site index %d',
                         rdiff * 100, index)
