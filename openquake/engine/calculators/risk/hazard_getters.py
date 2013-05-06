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

"""
Hazard getters for Risk calculators.

A HazardGetter is responsible fo getting hazard outputs needed by a risk
calculation.
"""

import numpy

from openquake.engine import logs
from openquake.hazardlib import geo
from openquake.engine.db import models
from django.db import connections


#: Scaling constant do adapt to the postgis functions (that work with
#: meters)
KILOMETERS_TO_METERS = 1000


# a Django cursor perform some caching which is polluting the
# memory profiler, this is why we are using the underlying cursor
def getcursor(route):
    """Return a psycogp2 cursor from a Django route"""
    return connections[route].connection.cursor()


class HazardGetter(object):
    """
    Base abstract class of an Hazard Getter.

    An Hazard Getter is used to query for the closest hazard data for
    each given asset. An Hazard Getter must be pickable such that it
    should be possible to use different strategies (e.g. distributed
    or not, using postgis or not).

    :attr int hazard_output_id:
        The ID of an Hazard Output object
        :class:`openquake.engine.db.models.Output`

    :attr int hazard_id:
        The ID of an Hazard Output container object
        (e.g. :class:`openquake.engine.db.models.HazardCurve`)

    :attr assets:
        The assets for which we wants to compute.

    :attr max_distance:
        The maximum distance, in kilometers, to use.

    :attr imt:
        The imt (in long form) for which data have to be retrieved

    :attr float weight:
        The weight (if applicable) to be given to the retrieved data
    """
    def __init__(self, hazard_output, assets, max_distance, imt):
        self.hazard_output_id = hazard_output.id
        hazard = self.container(hazard_output)
        self.hazard_id = hazard.id
        self.assets = assets
        self.max_distance = max_distance
        self.imt = imt

        if hasattr(hazard, 'lt_realization') and hazard.lt_realization:
            self.weight = hazard.lt_realization.weight
        else:
            self.weight = None

        # FIXME(lp). It is better to directly store the convex hull
        # instead of the mesh. We are not doing it because
        # hazardlib.Polygon is not (yet) pickeable
        self._assets_mesh = geo.mesh.Mesh.from_points_list([
            geo.point.Point(asset.site.x, asset.site.y)
            for asset in self.assets])
        self.asset_dict = dict((asset.id, asset) for asset in self.assets)
        self.all_asset_ids = set(self.asset_dict)

    def container(self, hazard_output):
        """
        Returns the corresponding output container object from an
        Hazard :class:`openquake.engine.db.models.Output` instance
        """
        raise NotImplementedError

    def __repr__(self):
        return "<%s max_distance=%s assets=%s>" % (
            self.__class__.__name__, self.max_distance,
            [a.id for a in self.assets])

    def get_data(self, imt):
        """
        Subclasses must implement this.

        :param str imt: a string representation of the intensity
        measure type (e.g. SA(0.1)) in which the hazard data should be
        returned

        :returns:
            An OrderedDict mapping ID of
            :class:`openquake.engine.db.models.ExposureData` objects to
            hazard_data (e.g. an array with the poes, or an array with the
            ground motion values). Bear in mind that the returned data could
            lack some assets being filtered out by the ``maximum_distance``
            criteria.
        """
        raise NotImplementedError

    def __call__(self):
        """
        :returns:
            A tuple with three elements. The first is an array of instances of
            :class:`openquake.engine.db.models.ExposureData`, the second is an
            array with the corresponding hazard data, the third is the array of
            IDs of assets that has been filtered out by the getter by the
            ``maximum_distance`` criteria.
        """
        # data is a gmf or a set of hazard curves
        asset_ids, data = self.get_data(self.imt)

        missing_asset_ids = self.all_asset_ids - set(asset_ids)

        if missing_asset_ids:
            logs.LOG.warn(
                "No hazard has been found for %d assets (of %d) "
                "within %s km" % (len(missing_asset_ids), len(self.asset_dict),
                                  self.max_distance))

        ret = ([self.asset_dict[asset_id] for asset_id in asset_ids
                if asset_id in self.asset_dict], data)

        return ret


class HazardCurveGetterPerAsset(HazardGetter):
    """
    Simple HazardCurve Getter that performs a spatial query for each
    asset.

    :attr imls:
        The intensity measure levels of the curves we are going to get.

    :attr dict _cache:
        A cache of the computed hazard curve object on a per-location basis.
    """

    def __init__(self, hazard, assets, max_distance, imt):
        super(HazardCurveGetterPerAsset, self).__init__(
            hazard, assets, max_distance, imt)
        self._cache = {}

    def container(self, hazard_output):
        return hazard_output.hazardcurve

    def get_data(self, imt):
        """
        Calls ``get_by_site`` for each asset and pack the results as
        requested by the :meth:`HazardGetter.get_data` interface.
        """
        imt_type, sa_period, sa_damping = models.parse_imt(imt)

        hc = models.HazardCurve.objects.get(pk=self.hazard_id)

        if hc.output.output_type == 'hazard_curve':
            imls = hc.imls
            hazard_id = self.hazard_id
        elif hc.output.output_type == 'hazard_curve_multi':
            hc = models.HazardCurve.objects.get(
                output__oq_job=hc.output.oq_job,
                output__output_type='hazard_curve',
                statistics=hc.statistics,
                lt_realization=hc.lt_realization,
                imt=imt_type,
                sa_period=sa_period,
                sa_damping=sa_damping)
            imls = hc.imls
            hazard_id = hc.id

        hazard_assets = [(asset.id, self.get_by_site(
            asset.site, hazard_id, imls))
            for asset in self.assets]

        assets = []
        curves = []
        for asset_id, (hazard_curve, distance) in hazard_assets:
            if distance < self.max_distance * KILOMETERS_TO_METERS:
                assets.append(asset_id)
                curves.append(hazard_curve)

        return assets, curves

    def get_by_site(self, site, hazard_id, imls):
        """
        :param site:
            An instance of :class:`django.contrib.gis.geos.point.Point`
            corresponding to the location of an asset.
        """
        if site.wkt in self._cache:
            return self._cache[site.wkt]

        cursor = getcursor('job_init')

        query = """
        SELECT
            hzrdr.hazard_curve_data.poes,
            min(ST_Distance(location::geography,
                            ST_GeographyFromText(%s), false))
                AS min_distance
        FROM hzrdr.hazard_curve_data
        WHERE hazard_curve_id = %s
        GROUP BY id
        ORDER BY min_distance
        LIMIT 1;"""

        args = (site.wkt, hazard_id)

        cursor.execute(query, args)
        poes, distance = cursor.fetchone()

        hazard = zip(imls, poes)

        self._cache[site.wkt] = (hazard, distance)

        return hazard, distance


class GroundMotionValuesGetter(HazardGetter):
    """
    Hazard getter for loading ground motion values.
    """

    def container(self, hazard_output):
        return hazard_output.gmfcollection

    #@profile
    def get_data(self, imt):
        cursor = getcursor('job_init')

        imt_type, sa_period, sa_damping = models.parse_imt(imt)
        spectral_filters = ""
        args = (imt_type, self.hazard_id)

        if imt_type == "SA":
            spectral_filters = "AND sa_period = %s AND sa_damping = %s"
            args += (sa_period, sa_damping)

        # Query explanation. We need to get for each asset the closest
        # ground motion values (and the corresponding rupture ids from
        # which they have been generated) for a given logic tree
        # realization and a given imt.

        # To this aim, we perform a spatial join with the exposure table that
        # is previously filtered by the assets extent, exposure model
        # and taxonomy. We are not filtering with an IN statement on
        # the ids of the assets for perfomance reasons.

        # The ``distinct ON (exposure_data.id)`` combined by the
        # ``ORDER BY ST_Distance`` does the job to select the closest
        # gmvs
        query = """
  SELECT DISTINCT ON (riski.exposure_data.id)
        riski.exposure_data.id, gmf_table.gmvs, gmf_table.rupture_ids
  FROM riski.exposure_data JOIN hzrdr.gmf_agg AS gmf_table
  ON ST_DWithin(riski.exposure_data.site, gmf_table.location, %s)
  WHERE taxonomy = %s AND exposure_model_id = %s AND
        riski.exposure_data.site && %s AND imt = %s AND
        gmf_collection_id = %s {}
  ORDER BY riski.exposure_data.id,
           ST_Distance(riski.exposure_data.site, gmf_table.location, false)
           """.format(spectral_filters)  # this will fill in the {}

        assets_extent = self._assets_mesh.get_convex_hull()
        args = (self.max_distance * KILOMETERS_TO_METERS,
                self.assets[0].taxonomy,
                self.assets[0].exposure_model_id,
                assets_extent.wkt) + args

        cursor.execute(query, args)
        # print cursor.mogrify(query, args)

        data = cursor.fetchall()

        rupture_set = set()
        for _, _, ruptures in data:
            rupture_set.update(ruptures)
        sorted_ruptures = numpy.array(sorted(rupture_set))

        # maps asset_id -> to a 2-tuple (gmvs, ruptures)
        assets, gmf = [], []
        for asset_id, gmvs, ruptures in data:
            gmv = dict(zip(ruptures, gmvs))
            gmvs = numpy.array([gmv.get(r, 0.) for r in sorted_ruptures])
            assets.append(asset_id)
            gmf.append(numpy.array([gmvs, sorted_ruptures]))

        return assets, gmf


# TODO: this calls will disappear soon: see
# https://bugs.launchpad.net/oq-engine/+bug/1170628
class GroundMotionScenarioGetter(HazardGetter):
    """
    Hazard getter for loading ground motion values. It uses the same
    approach used in :class:`GroundMotionValuesGetter`.
    """
    def get_data(self, imt):
        cursor = getcursor('job_init')

        # See the comment in `GroundMotionValuesGetter.get_data` for
        # an explanation of the query
        query = """
  SELECT DISTINCT ON (riski.exposure_data.id) riski.exposure_data.id,
         gmf_table.gmvs
  FROM riski.exposure_data JOIN (
    SELECT location, gmvs
           FROM hzrdr.gmf_scenario
           WHERE hzrdr.gmf_scenario.imt = %s
           AND hzrdr.gmf_scenario.output_id = %s
           AND hzrdr.gmf_scenario.location && %s) gmf_table
  ON ST_DWithin(riski.exposure_data.site, gmf_table.location, %s)
  WHERE taxonomy = %s AND exposure_model_id = %s
  ORDER BY riski.exposure_data.id,
    ST_Distance(riski.exposure_data.site, gmf_table.location, false)
           """

        assets_extent = self._assets_mesh.get_convex_hull()
        args = (imt, self.hazard_id,
                assets_extent.dilate(self.max_distance).wkt,
                self.max_distance * KILOMETERS_TO_METERS,
                self.assets[0].taxonomy,
                self.assets[0].exposure_model_id)
        cursor.execute(query, args)
        # print cursor.mogrify(query, args)
        assets, gmf = [], []
        for asset_id, gmvs in cursor.fetchall():
            assets.append(asset_id)
            gmf.append(gmvs)
        return assets, gmf

    def container(self, hazard_output):
        return hazard_output
