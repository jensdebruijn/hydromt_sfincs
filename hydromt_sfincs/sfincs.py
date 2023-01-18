# -*- coding: utf-8 -*-
import os
from os.path import join, isfile, abspath, dirname, basename, isabs
import glob
import numpy as np
import logging
import pyflwdir
import geopandas as gpd
from rasterio.warp import transform_bounds
from shapely.geometry import box
import pandas as pd
import xarray as xr
from pathlib import Path
from typing import Dict, Tuple, List, Union
from scipy import ndimage

import hydromt
from hydromt.models.model_grid import GridModel
from hydromt.models.model_mesh import MeshMixin
from hydromt.vector import GeoDataset, GeoDataArray
from hydromt.raster import RasterDataset, RasterDataArray

from . import workflows, utils, plots, DATADIR
from .regulargrid import RegularGrid
from .sfincs_input import SfincsInput

__all__ = ["SfincsModel"]

logger = logging.getLogger(__name__)


class SfincsModel(MeshMixin, GridModel):
    _NAME = "sfincs"
    _GEOMS = {
        "gauges": "obs",
        "weirs": "weir",
        "thin_dams": "thd",
    }  # parsed to dict of geopandas.GeoDataFrame
    _FORCING_1D = {
        "waterlevel": (
            ["bzs"],
            "bnd",
        ),  #  timeseries (can be multiple), locations tuple
        "discharge": (["dis"], "src"),
        "precip": (["precip"], None),
        "waves": (["bhs", "btp", "bwd", "bds"], "bwv"),  # TODO check names and test
        "wavemaker": (["whi", "wti", "wst"], "wvp"),  # TODO check names and test
    }
    _FORCING_2D = {
        "precip2d": "netampr",  # TODO discuss which 2D forcings exist
    }
    _FORCING_SPW = {"spiderweb": "spw"}  # TODO add read and write functions
    _MAPS = ["msk", "dep", "scs", "manning", "qinf"]
    _STATES = ["rst", "ini"]
    _FOLDERS = []
    _CLI_ARGS = {"region": "setup_grid_from_region", "res": "setup_grid_from_region"}
    _CONF = "sfincs.inp"
    _DATADIR = DATADIR
    _ATTRS = {
        "dep": {"standard_name": "elevation", "unit": "m+ref"},
        "msk": {"standard_name": "mask", "unit": "-"},
        "scs": {
            "standard_name": "potential maximum soil moisture retention",
            "unit": "in",
        },
        "qinf": {"standard_name": "infiltration rate", "unit": "mm.hr-1"},
        "manning": {"standard_name": "manning roughness", "unit": "s.m-1/3"},
        "bzs": {"standard_name": "waterlevel", "unit": "m+ref"},
        "dis": {"standard_name": "discharge", "unit": "m3.s-1"},
        "netampr": {"standard_name": "precipitation", "unit": "mm.hr-1"},
        "precip": {"standard_name": "precipitation", "unit": "mm.hr-1"},
    }

    def __init__(
        self,
        root: str = None,
        mode: str = "w",
        config_fn: str = "sfincs.inp",
        grid_type: str = "regular",
        write_gis: bool = True,
        data_libs: Union[List[str], str] = None,
        logger=logger,
    ):
        """
        The SFINCS model class (SfincsModel) contains methods to read, write, setup and edit
        `SFINCS <https://sfincs.readthedocs.io/en/latest/>`_ models.

        Parameters
        ----------
        root: str, Path, optional
            Path to model folder
        mode: {'w', 'r+', 'r'}
            Open model in write, append or reading mode, by default 'w'
        config_fn: str, Path, optional
            Filename of model config file, by default "sfincs.inp"
        write_gis: bool
            Write model files additionally to geotiff and geojson, by default True
        data_libs: List, str
            List of data catalog yaml files, by default None

        """
        # model folders
        self._write_gis = write_gis
        if write_gis and "gis" not in self._FOLDERS:
            self._FOLDERS.append("gis")

        super().__init__(
            root=root,
            mode=mode,
            config_fn=config_fn,
            data_libs=data_libs,
            logger=logger,
        )

        # placeholder grid classes
        self.grid_type = grid_type
        self.reggrid = None
        self.quadtree = None
        self.subgrid = xr.Dataset()

    @property
    def mask(self):
        """Returns model mask"""
        if self.grid_type == "regular":
            if "msk" in self.grid:
                return self.grid["msk"]
            elif self.reggrid is not None:
                return self.reggrid.empty_mask

    @property
    def region(self) -> gpd.GeoDataFrame:
        """Returns the geometry of the active model cells."""
        # NOTE overwrites propertie in GridModel
        region = gpd.GeoDataFrame()
        if "region" in self.geoms:
            region = self.geoms["region"]
        elif "msk" in self.grid:
            da = xr.where(self.mask > 0, 1, 0).astype(np.int16)
            da.raster.set_nodata(0)
            region = da.raster.vectorize().dissolve()
        elif self.reggrid is not None:
            region = self.reggrid.empty_mask.raster.box
        return region

    def create_grid(
        self,
        x0: float,
        y0: float,
        dx: float,
        dy: float,
        nmax: int,
        mmax: int,
        rotation: float = None,
        crs: int = None,
        grid_type: str = "regular",
        gdf_refinement: gpd.GeoDataFrame = None,
    ):
        self._config.update(
            x0=x0, y0=y0, dx=dx, dy=dy, nmax=nmax, mmax=mmax, rotation=rotation, crs=crs
        )
        self.update_grid_from_config()
        # TODO gdf_refinment for quadtree

    def create_grid_from_region(
        self,
        gdf_region: gpd.GeoDataFrame,
        res: float,
        grid_type: str = "regular",
        gdf_refinment: gpd.GeoDataFrame = None,
    ):
        west, south, east, north = gdf_region.total_bounds
        mmax = int(np.ceil((east - west) / res))
        nmax = int(np.ceil((north - south) / res))
        # TODO gdf_region.minimum_rotated_rectangle for rotated grid
        # https://stackoverflow.com/questions/66108528/angle-in-minimum-rotated-rectangle
        self.create_grid(
            x0=west,
            y0=south,
            dx=res,
            dy=res,
            nmax=nmax,
            mmax=mmax,
            grid_type=grid_type,
            gdf_refinment=gdf_refinment,
        )

    def create_dep(
        self,
        da_list: List[xr.DataArray],
        merge_kwargs: Union[Dict, List[Dict]] = {},
        reproj_kwargs: dict = {},
    ):
        if self.grid_type == "regular":
            da_dep = self.reggrid.create_dep(
                da_list=da_list,
                merge_kwargs=merge_kwargs,
                reproj_kwargs=reproj_kwargs,
            )
            self.set_grid(da_dep, name="dep")
            if "depfile" not in self.config:
                self.config.update({"depfile": "sfincs.dep"})

    def create_mask_active(
        self,
        gdf_include: gpd.GeoDataFrame = None,
        gdf_exclude: gpd.GeoDataFrame = None,
        elv_min: float = None,
        elv_max: float = None,
        fill_area: float = 10,
        drop_area: float = 0,
        connectivity: int = 8,
        all_touched=True,
        reset_mask=False,
    ) -> xr.DataArray:
        """Returns a boolean mask of valid (non nondata) elevation cells, optionally bounded
        by several criteria.

        Parameters
        ----------
        gdf_include, gdf_exclude: geopandas.GeoDataFrame, optional
            Geometries with areas to include/exclude from the active model cells.
            Note that include (second last) and exclude (last) and areas are processed after other critera,
            i.e. `elv_min`, `elv_max` and `drop_area`, and thus overrule these criteria for active model cells.
        elv_min, elv_max : float, optional
            Minimum and maximum elevation thresholds for active model cells.
        fill_area : float, optional
            Maximum area [km2] of contiguous cells below `elv_min` or above `elv_max` but surrounded
            by cells within the valid elevation range to be kept as active cells, by default 10 km2.
        drop_area : float, optional
            Maximum area [km2] of contiguous cells to be set as inactive cells, by default 0 km2.
        connectivity: {4, 8}
            The connectivity used to define contiguous cells, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_mask: bool, optional
            If True, reset existing mask layer. If False (default) updating existing mask.

        Returns
        -------
        xr.DataArray
            model elevation mask
        """
        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_active(
                da_mask=self.grid["msk"] if "msk" in self.grid else None,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                elv_min=elv_min,
                elv_max=elv_max,
                fill_area=fill_area,
                drop_area=drop_area,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_mask=reset_mask,
                # logger=self.logger,
            )
            self.set_grid(da_mask, name="msk")
            # update config
            if "mskfile" not in self.config:
                self.config.update({"mskfile": "sfincs.msk"})
            if "indexfile" not in self.config:
                self.config.update({"indexfile": "sfincs.ind"})
            # update region
            self.logger.info(f"Derive region geometry based on active cells.")
            region = da_mask.where(da_mask <= 1, 1).raster.vectorize()
            self.set_geoms(region, "region")

        return da_mask

    def create_mask_bounds(
        self,
        btype: str = "waterlevel",
        gdf_include: gpd.GeoDataFrame = None,
        gdf_exclude: gpd.GeoDataFrame = None,
        elv_min: float = None,
        elv_max: float = None,
        connectivity: int = 8,
        all_touched: bool = False,
        reset_bounds: bool = False,
    ) -> xr.DataArray:
        """Returns a boolean mask model boundary cells, optionally bounded by several
        criteria. Boundary cells are defined by cells at the edge of active model domain.

        Parameters
        ----------
        btype: {'waterlevel', 'outflow'}
            Boundary type
        gdf_include, gdf_exclude: geopandas.GeoDataFrame
            Geometries with areas to include/exclude from the model boundary.
            Note that exclude (second last) and include (last) areas are processed after other critera,
            i.e. `elv_min`, `elv_max`, and thus overrule these criteria for model boundary cells.
        elv_min, elv_max : float, optional
            Minimum and maximum elevation thresholds for boundary cells.
        connectivity: {4, 8}
            The connectivity used to detect the model edge, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_bounds: bool, optional
            If True, reset existing boundary cells of the selected boundary
            type (`btype`) before setting new boundary cells, by default False.

        Returns
        -------
        bounds: xr.DataArray
            Boolean mask of model boundary cells.
        """

        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_bounds(
                da_mask=self.grid["msk"],
                btype=btype,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                elv_min=elv_min,
                elv_max=elv_max,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_bounds=reset_bounds,
            )
            self.set_grid(da_mask, name="msk")
        return da_mask

    def create_subgrid(
        self,
    ):
        pass

    def setup_grid_from_region(
        self,
        region: dict,
        res: float,
        crs: Union[str, int] = 'utm',
        grid_type: str = "regular",
        refinement_fn: str = None,
        hydrography_fn: str = "merit_hydro",
        basin_index_fn: str = "merit_hydro_index",
    ):
        self.setup_region(
            region = region,
            hydrography_fn = hydrography_fn,
            basin_index_fn = basin_index_fn,
        )
        # get pyproj crs of best UTM zone if crs=utm
        pyproj_crs = hydromt.gis_utils.parse_crs(crs, self.region.to_crs(4326).total_bounds)
        if self.region.crs != pyproj_crs:
            self.geoms['region'] = self.geoms['region'].to_crs(pyproj_crs)

        self.create_grid_from_region(
            region = self.region,
            res = res,
            grid_type=grid_type,
        )

    def setup_grid(
        self,
        x0: float,
        y0: float,
        dx: float,
        dy: float,
        nmax: int,
        mmax: int,
        rotation: float,
        crs: int,
        refinement_fn: str = None,
    ):

        if refinement_fn is not None:
            grid_type = "quadtree"
            # gdf_refinement = gpd.read_file()
        else:
            grid_type = "regular"
            gdf_refinement = None
        self.create_grid(x0=x0, y0=y0, dx=dx, dy=dy, nmax=nmax, mmax=mmax, rotation=rotation, crs=crs, grid_type=grid_type, gdf_refinement=gdf_refinement )

    def setup_dep(
        self,
        dep_fns: List[Union[str, Path]],
        merge_kwargs: Union[Dict, List[Dict]] = {},
    ):
        """Setup model grid and interpolate topobathy (dep) data to this grid.

        NOTE: This method should be called after `setup_region` but before any other model component.

        The input topobathy dataset is reprojected to the model projected `crs` and
        resolution `res` using `reproj_method` interpolation.

        Adds model layers:

        * **dep** map: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        topobathy_fn : str
            Path or data source name for topobathy raster data.
        res : float
            Model resolution [m], by default 100 m.
            If None, the basemaps res is used.
        """

        # read global data (lazy!)
        da_lst = []
        for dep_fn in dep_fns:
            da_elv = self.data_catalog.get_rasterdataset(
                dep_fn, geom=self.region, buffer=20, variables=["elevtn"]
            )
            da_lst.append(da_elv)

        self.create_dep(da_list=da_lst, merge_kwargs=merge_kwargs)

    def setup_merge_topobathy(
        self,
        topobathy_fn,
        elv_min=None,
        elv_max=None,
        mask_fn=None,
        max_width=0,
        offset_fn=None,
        offset_constant=0,
        merge_buffer=0,
        merge_method="first",
        reproj_method="bilinear",
        interp_method="linear",
    ):
        """Updates the existing model topobathy data (dep file) with a new topobathy
        source within the current model extent.

        By default (`merge_method="first"`) invalid (nodata) cells in the current topobathy
        data are replaced with values from the new topobathy source.

        Use `offset_fn` for a spatially varying, or `offset_constant` for a spatially uniform
        offset to convert the vertical datum of the new source before merging.

        Gaps in the data (i.e. areas with nodata cells surrounded areas with valid elevation)
        are interpolated based on `interp_method`, by deafult 'linear'. Gaps are not
        interpolated if `interp_method = None`

        Updates model layer:

        * **dep** map: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        topobathy_fn : str, optional
            Path or data source name for topobathy raster data.

            * Required variables: ['elevtn']
        mask_fn : str, optional
            Path or data source name of polygon with valid new topobathy cells.
        max_width: int, optional
            Maximum width (number of cells) to append to valid dep cells if larger than
            zero. By default 0.
        elv_min, elv_max : float, optional
            Minimum and maximum elevation caps for new topobathy cells, cells outside
            this range are linearly interpolated. Note: applied after offset!
        offset_fn : str, optional
            Path or data source name for Spatially varying map with difference between
            the vertical reference of the current model topobathy and the new data source [m].
            The offset is added to the new source before merging.
        offset_fn : float, optional
            Same as `offset_fn` but spatially uniform value [m].
        merge_buffer : int, optional
            Buffer (number of cells) around the original (`merge_method = 'first'`)
            or new (`merge_method = 'last'`) data source where values are interpolated
            using `interp_method`.
            Not recommended to use in combination with merge_methods 'min' or 'max'
        merge_method: {'first','last','min','max'}, optional
            merge method, by default 'first':

            * first: use valid new where existing invalid
            * last: use valid new
            * min: pixel-wise min of existing and new
            * max: pixel-wise max of existing and new
        reproj_method: {'bilinear', 'cubic', 'nearest'}
            Method used to reproject the offset and second dataset to the grid of the
            new topobathy dataset, by default 'bilinear'
        interp_method, {'linear', 'nearest', 'rio_idw'}, optional
            Method used to interpolate holes of nodata in the merged dataset,
            by default 'linear'. If None holes are not interpolated.
        """
        name = "dep"
        assert name in self.grid
        da_elv = self.grid[name]
        geom = self.grid.raster.box
        da_elv2 = self.data_catalog.get_rasterdataset(
            topobathy_fn, geom=geom, buffer=10, variables=["elevtn"]
        )
        kwargs = dict(
            reproj_method=reproj_method,
            interp_method=interp_method,
            merge_buffer=merge_buffer,
            merge_method=merge_method,
            max_width=max_width,
        )
        # mask
        if mask_fn is not None:
            gdf_mask = self.data_catalog.get_geodataframe(mask_fn)
            da_elv2 = da_elv2.raster.clip_geom(gdf_mask, mask=True)
        # offset
        if offset_fn is not None:
            # variable name not important, but must be single variable
            da_offset = self.data_catalog.get_rasterdataset(
                offset_fn, geom=geom, buffer=10
            )
            assert isinstance(da_offset, xr.DataArray)
            kwargs.update(da_offset=da_offset)
        elif offset_constant > 0:
            kwargs.update(da_offset=offset_constant)
        # merge
        da_dep_merged = workflows.merge_topobathy(
            da_elv,
            da_elv2,
            elv_min=elv_min,
            elv_max=elv_max,
            logger=self.logger,
            **kwargs,
        )
        self.set_grid(data=da_dep_merged.round(2), name=name)

    def setup_mask_active(
        self,
        include_mask_fn=None,
        exclude_mask_fn=None,
        mask_buffer=0,
        elv_min=None,
        elv_max=None,
        fill_area=10,
        drop_area=0,
        connectivity=8,
        all_touched=True,
        reset_mask=False,
    ):
        """Creates mask of active model cells.

        The SFINCS model mask defines 0) Inactive, 1) active, and 2) waterlevel boundary
        and 3) outflow boundary cells. This method sets the active cells set using,
        while boundary cells are set in the `setup_bounds` method.

        Active model cells are based on cells with valid elevation (i.e. not nodata),
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        NOTE: Inactive cells are set to nodata values in all grid which cannot be undone!

        Sets model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        include_mask_fn, exclude_mask_fn: str, optional
            Path or data source name of polygons to include/exclude from the active model domain.
            Note that exclude (second last) and include (last) areas are processed after other critera,
            i.e. `elv_min`, `elv_max` and `drop_area`, and thus overrule these criteria for active model cells.
        mask_buffer: float, optional
            If larger than zero, extend the `include_mask` geometry with a buffer [m],
            by default 0.
        elv_min, elv_max : float, optional
            Minimum and maximum elevation thresholds for active model cells.
        fill_area : float, optional
            Maximum area [km2] of contiguous cells below `elv_min` or above `elv_max` but surrounded
            by cells within the valid elevation range to be kept as active cells, by default 10 km2.
        drop_area : float, optional
            Maximum area [km2] of contiguous cells to be set as inactive cells, by default 0 km2.
        connectivity, {4, 8}:
            The connectivity used to define contiguous cells, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_mask: bool, optional
            If True, reset existing mask layer. If False (default) updating existing mask.
        """

        # read geometries
        gdf1, gdf2 = None, None
        bbox = self.region.to_crs(4326).total_bounds
        if include_mask_fn is not None:
            gdf1 = self.data_catalog.get_geodataframe(include_mask_fn, bbox=bbox)
            if mask_buffer > 0:  # NOTE assumes model in projected CRS!
                gdf1["geometry"] = gdf1.to_crs(self.crs).buffer(mask_buffer)
        if exclude_mask_fn is not None:
            gdf2 = self.data_catalog.get_geodataframe(exclude_mask_fn, bbox=bbox)

        # get mask
        da_mask = self.create_mask_active(
            gdf_include=gdf1,
            gdf_exclude=gdf2,
            elv_min=elv_min,
            elv_max=elv_max,
            fill_area=fill_area,
            drop_area=drop_area,
            connectivity=connectivity,
            all_touched=all_touched,
            reset_mask=reset_mask,
            # logger=self.logger,
        )

        # n = np.count_nonzero(da_mask.values)
        # self.logger.debug(f"Mask with {n:d} active cells set; updating grid ...")
        # # update all grid layers
        # for name in self._grid.raster.vars:
        #     da = self._grid[name]
        #     self._grid[name] = da.where(da_mask, da.raster.nodata)
        # update sfincs mask with boolean mask to conserve mask values
        # da_mask = self.mask.where(da_mask, np.uint8(0))  # unint8 dtype!
        # self.set_grid(da_mask, "msk")

        self.logger.debug(f"Derive region geometry based on active cells.")
        region = da_mask.where(da_mask <= 1, 1).raster.vectorize()
        self.set_geoms(region, "region")

    def setup_mask_bounds(
        self,
        btype="waterlevel",
        include_mask_fn=None,
        exclude_mask_fn=None,
        elv_min=None,
        elv_max=None,
        connectivity=8,
        reset_bounds=False,
    ):
        """Set boundary cells in the model mask.

        The SFINCS model mask defines 0) Inactive, 1) active, and 2) waterlevel boundary
        and 3) outflow boundary cells. Active cells set using the `setup_mask` method,
        while this method sets both types of boundary cells, see `btype` argument.

        Boundary cells at the edge of the active model domain,
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        Updates model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        btype: {'waterlevel', 'outflow'}
            Boundary type
        include_mask_fn, exclude_mask_fn: str, optional
            Path or data source name for geometries with areas to include/exclude from the model boundary.
            Note that exclude (second last) and include (last) areas are processed after other critera,
            i.e. `elv_min`, `elv_max`, and thus overrule these criteria for model boundary cells.
        elv_min, elv_max : float, optional
            Minimum and maximum elevation thresholds for boundary cells.
        reset_bounds: bool, optional
            If True, reset existing boundary cells of the selected boundary
            type (`btype`) before setting new boundary cells, by default False.
        connectivity, {4, 8}:
            The connectivity used to detect the model edge, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        """
        btype = btype.lower()
        bvalues = {"waterlevel": 2, "outflow": 3}
        if btype not in bvalues:
            raise ValueError('btype must be one of "waterlevel", "outflow"')
        bvalue = bvalues[btype]

        # get include / exclude geometries
        gdf_include, gdf_exclude = None, None
        bbox = self.region.to_crs(4326).total_bounds

        if include_mask_fn:
            gdf_include = self.data_catalog.get_geodataframe(include_mask_fn, bbox=bbox)
        if exclude_mask_fn:
            gdf_exclude = self.data_catalog.get_geodataframe(exclude_mask_fn, bbox=bbox)

        # mask values
        da_mask = self.create_mask_bounds(
            btype=btype,
            gdf_include=gdf_include,
            gdf_exclude=gdf_exclude,
            elv_min=elv_min,
            elv_max=elv_max,
            connectivity=connectivity,
            reset_bounds=reset_bounds,
        )

        self.set_grid(da_mask, "msk")

    def setup_river_hydrography(self, hydrography_fn=None, adjust_dem=False, **kwargs):
        """Setup hydrography layers for flow directions ("flwdir") and upstream area
        ("uparea") which are required to setup the setup_river* model components.

        If no hydrography data is provided (`hydrography_fn=None`) flow directions are
        derived from the model elevation data.
        Note that in that case the upstream area map will miss the contribution from area
        upstream of the model domain and incoming rivers in the `setup_river_inflow`
        cannot be detected.

        If the model crs or resolution is different from the input hydrography data,
        it is reprojected to the model grid. Note that this works best if the destination
        resolution is roughly the same or higher (i.e. smaller cells).

        Adds model layers (both not used by SFINCS!):

        * **uparea** map: upstream area [km2]
        * **flwdir** map: local D8 flow directions [-]

        Updates model layer (if `adjust_dem=True`):

        * **dep** map: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        hydrography_fn : str
            Path or data source name for hydrography raster data, by default None
            and derived from model elevation data.

            * Required variable: ['uparea']
            * Optional variable: ['flwdir']
        adjust_dem: bool, optional
            Adjust the model elevation such that each downstream cell is at the
            same or lower elevation. By default True.
        """
        name = "dep"
        assert name in self.grid
        da_elv = self.grid[name]
        if hydrography_fn is not None:
            ds_hydro = self.data_catalog.get_rasterdataset(
                hydrography_fn, geom=self.region, buffer=20, single_var_as_array=False
            )
            assert "uparea" in ds_hydro
            warp = ~da_elv.raster.aligned_grid(ds_hydro)
            if warp or "flwdir" not in ds_hydro:
                self.logger.info("Reprojecting hydrography data to destination grid.")
                ds_out = hydromt.flw.reproject_hydrography_like(
                    ds_hydro, da_elv, logger=self.logger, **kwargs
                )
            else:
                ds_out = ds_hydro[["uparea", "flwdir"]].raster.clip_bbox(
                    da_elv.raster.bounds
                )
            ds_out = ds_out.raster.mask(da_elv != da_elv.raster.nodata)
        else:
            self.logger.info("Getting hydrography data from model grid.")
            da_flw = hydromt.flw.d8_from_dem(da_elv, **kwargs)
            flwdir = hydromt.flw.flwdir_from_da(da_flw, ftype="d8")
            da_upa = xr.DataArray(
                dims=da_elv.raster.dims,
                data=flwdir.upstream_area(unit="km2"),
                name="uparea",
            )
            da_upa.raster.set_nodata(-9999)
            ds_out = xr.merge([da_flw, da_upa.reset_coords(drop=True)])

        self.logger.info("Saving hydrography data to grid.")
        self.set_grid(ds_out["uparea"])
        self.set_grid(ds_out["flwdir"])

        if adjust_dem:
            self.logger.info(f"Hydrologically adjusting {name} map.")
            flwdir = hydromt.flw.flwdir_from_da(ds_out["flwdir"], ftype="d8")
            da_elv.data = flwdir.dem_adjust(da_elv.values)
            self.set_grid(da_elv.round(2), name)

    def setup_river_bathymetry(
        self,
        river_geom_fn=None,
        river_mask_fn=None,
        qbankfull_fn=None,
        rivdph_method="gvf",
        rivwth_method="geom",
        river_upa=25.0,
        river_len=1000,
        min_rivwth=50.0,
        min_rivdph=1.0,
        rivbank=True,
        rivbankq=25,
        segment_length=3e3,
        smooth_length=10e3,
        constrain_rivbed=True,
        constrain_estuary=True,
        dig_river_d4=True,
        plot_riv_profiles=0,
        **kwargs,  # for workflows.get_river_bathymetry method
    ):
        """Burn rivers into the model elevation (dep) file.

        NOTE: this method is experimental and may change in the near future.

        River cells are based on the `river_mask_fn` raster file if `rivwth_method='mask'`,
        or if `rivwth_method='geom'` the rasterized segments buffered with half a river width
        ("rivwth" [m]) if that attribute is found in `river_geom_fn`.

        If a river segment geometry file `river_geom_fn` with bedlevel column ("zb" [m+REF]) or
        a river depth ("rivdph" [m]) in combination with `rivdph_method='geom'` is provided,
        this attribute is used directly.

        Otherwise, a river depth is estimated based on bankfull discharge ("qbankfull" [m3/s])
        attribute taken from the nearest river segment in `river_geom_fn` or `qbankfull_fn`
        upstream river boundary points if provided.

        The river depth is relative to the bankfull elevation profile if `rivbank=True` (default),
        which is estimated as the `rivbankq` elevation percentile [0-100] of cells neighboring river cells.
        This option requires the flow direction ("flwdir") and upstream area ("uparea") maps to be set
        using the "setup_river_hydrography" method. If `rivbank=False` the depth is simply subtracted
        from the elevation of river cells.

        Missing river width and river depth values are filled by propagating valid values downstream and
        using the constant minimum values `min_rivwth` and `min_rivdph` for the remaining missing values.

        Updates model layer:

        * **dep** map: combined elevation/bathymetry [m+ref]

        Adds model layers

        * **rivmsk** map: map of river cells (not used by SFINCS)
        * **rivers** geom: geometry of rivers (not used by SFINCS)

        Parameters
        ----------
        river_geom_fn : str, optional
            Line geometry with river attribute data.

            * Required variable for direct bed level burning: ['zb']
            * Required variable for direct river depth burning: ['rivdph'] (only in combination with rivdph_method='geom')
            * Variables used for river depth estimates: ['qbankfull', 'rivwth']

        river_mask_fn : str, optional
            River mask raster used to define river cells
        qbankfull_fn: str, optional
            Point geometry with bankfull discharge estimates

            * Required variable: ['qbankfull']

        rivdph_method : {'gvf', 'manning', 'powlaw'}
            River depth estimate method, by default 'gvf'
        rivwth_method : {'geom', 'mask'}
            Derive the river with from either the `river_geom_fn` (geom) or
            `river_mask_fn` (mask; default) data.
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1000 m.
        min_rivwth, min_rivdph: float, optional
            Minimum river width [m] (by default 50.0) and depth [m] (by default 1.0)
        rivbank: bool, optional
            If True (default), approximate the reference elevation for the river depth based
            on the river bankfull elevation at cells neighboring river cells. Otherwise
            use the elevation of the local river cell as reference level.
        rivbankq : float, optional
            quantile [1-100] for river bank estimation, by default 25
        segment_length : float, optional
            Approximate river segment length [m], by default 5e3
        smooth_length : float, optional
            Approximate smoothing length [m], by default 10e3
        constrain_estuary : bool, optional
            If True (default) fix the river depth in estuaries based on the upstream river depth.
        constrain_rivbed : bool, optional
            If True (default) correct the river bed level to be hydrologically correct,
            i.e. sloping downward in downstream direction.
        dig_river_d4: bool, optional
            If True (default), dig the river out to be hydrologically connected in D4.
        """
        if river_mask_fn is None and rivwth_method == "mask":
            raise ValueError(
                '"river_mask_fn" should be provided if rivwth_method="mask".'
            )
        # get basemap river flwdir
        self.mask  # make sure msk is grid
        ds = self.grid
        flwdir = None
        if "flwdir" in ds:
            flwdir = hydromt.flw.flwdir_from_da(ds["flwdir"], mask=False)

        # read river line geometry data
        gdf_riv = None
        if river_geom_fn is not None:
            gdf_riv = self.data_catalog.get_geodataframe(
                river_geom_fn, geom=self.region
            ).to_crs(self.crs)
        # read river bankfull point data
        gdf_qbf = None
        if qbankfull_fn is not None:
            gdf_qbf = self.data_catalog.get_geodataframe(
                qbankfull_fn,
                geom=self.region,
            ).to_crs(self.crs)
        # read river mask raster data
        da_rivmask = None
        if river_mask_fn is not None:
            da_rivmask = self.data_catalog.get_rasterdataset(
                river_mask_fn, geom=self.region
            ).raster.reproject_like(ds, "max")
            ds["rivmsk"] = da_rivmask.where(self.mask != 0, 0) != 0
        elif "rivmsk" in ds:
            self.logger.info(
                'River mask based on internal "rivmsk" layer. If this is unwanted '
                "delete the gis/rivmsk.tif file or drop the rivmsk grid variable."
            )

        # estimate elevation bed level based on qbankfull (and other parameters)
        if not (gdf_riv is not None and "zb" in gdf_riv):
            if flwdir is None:
                msg = '"flwdir" staticmap layer missing, run "setup_river_hydrography".'
                raise ValueError(msg)
            gdf_riv, ds["rivmsk"] = workflows.get_river_bathymetry(
                ds,
                flwdir=flwdir,
                gdf_riv=gdf_riv,
                gdf_qbf=gdf_qbf,
                rivdph_method=rivdph_method,
                rivwth_method=rivwth_method,
                river_upa=river_upa,
                river_len=river_len,
                min_rivdph=min_rivdph,
                min_rivwth=min_rivwth,
                rivbank=rivbank,
                rivbankq=rivbankq,
                segment_length=segment_length,
                smooth_length=smooth_length,
                elevtn_name="dep",
                constrain_estuary=constrain_estuary,
                constrain_rivbed=constrain_rivbed,
                logger=self.logger,
                **kwargs,
            )
        elif "rivmsk" not in ds:
            buffer = gdf_riv["rivwth"].values if "rivwth" in gdf_riv else 0
            gdf_riv_buf = gdf_riv.buffer(buffer)
            ds["rivmsk"] = ds.raster.geometry_mask(gdf_riv_buf, all_touched=True)

        # set elevation bed level
        da_elv1, ds["rivmsk"] = workflows.burn_river_zb(
            gdf_riv=gdf_riv,
            da_elv=ds["dep"],
            da_msk=ds["rivmsk"],
            flwdir=flwdir,
            river_d4=dig_river_d4,
            logger=self.logger,
        )

        if plot_riv_profiles > 0:
            # TODO move to plots
            import matplotlib.pyplot as plt

            flw = pyflwdir.from_dataframe(gdf_riv.set_index("idx"))
            upa_pit = gdf_riv.loc[flw.idxs_pit, "uparea"]
            n = int(plot_riv_profiles)
            idxs = flw.idxs_pit[np.argsort(upa_pit).values[::-1]][:n]
            paths, _ = flw.path(idxs=idxs, direction="up")
            _, axes = plt.subplots(n, 1, figsize=(7, n * 4))
            for path, ax in zip(paths, axes):
                g0 = gdf_riv.loc[path, :]
                x = g0["rivdst"].values
                ax.plot(x, g0["zs"], "--k", label="bankfull")
                ax.plot(x, g0["elevtn"], ":k", label="original zb")
                ax.plot(x, g0["zb"], "--g", label=f"{rivdph_method} zb (corrected)")
                mask = da_elv1.raster.geometry_mask(g0).values
                x1 = flwdir.distnc[mask]
                y1 = da_elv1.data[mask]
                s1 = np.argsort(x1)
                ax.plot(x1[s1], y1[s1], ".b", ms=2, label="zb (burned)")
            ax.legend()
            if not os.path.isdir(join(self.root, "figs")):
                os.makedirs(join(self.root, "figs"))
            fn_fig = join(self.root, "figs", "river_bathymetry.png")
            plt.savefig(fn_fig, dpi=225, bbox_inches="tight")

        # update dep
        self.set_grid(da_elv1.round(2), name="dep")
        # keep river geom and rivmsk for postprocessing
        self.set_geoms(gdf_riv, name="rivers")
        # save rivmask as int8 map (geotif does not support bool maps)
        da_rivmask = ds["rivmsk"].astype(np.int8).where(ds["msk"] > 0, 255)
        da_rivmask.raster.set_nodata(255)
        self.set_grid(da_rivmask, name="rivmsk")

    def setup_river_inflow(
        self,
        hydrography_fn=None,
        river_upa=25.0,
        river_len=1e3,
        river_width=2e3,
        keep_rivers_geom=False,
        buffer=10,
        **kwargs,  # catch deprecated args
    ):
        """Setup river inflow (source) points where a river enters the model domain.

        NOTE: this method requires the either `hydrography_fn` or `setup_river_hydrography` to be run first.
        NOTE: best to run after `setup_mask`

        Adds model layers:

        * **src** geoms: discharge boundary point locations
        * **dis** forcing: dummy discharge timeseries
        * **mask** map: SFINCS mask layer (only if `river_width` > 0)
        * **rivers_in** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        hydrography_fn: str, Path, optional
            Path or data source name for hydrography raster data, by default 'merit_hydro'.

            * Required layers: ['uparea', 'flwdir'].
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1 km.
        river_width: float, optional
            Estimated constant width [m] of the inflowing river. Boundary cells within
            half the width are forced to be closed (mask = 1) to avoid instabilities with
            nearby open or waterlevel boundary cells, by default 1 km.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_in" in geoms. By default False.
        buffer: int, optional
            Buffer [no. of cells] around model domain, by default 10.
        """
        if "basemaps_fn" in kwargs:
            self.logger.warning(
                "'basemaps_fn' is deprecated use 'hydrography_fn' instead."
            )
            hydrography_fn = kwargs.pop("basemaps_fn")

        if hydrography_fn is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography_fn,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=buffer,
            )
        else:
            ds = self.grid
            if "uparea" not in ds or "flwdir" not in ds:
                raise ValueError(
                    '"uparea" and/or "flwdir" layers missing. '
                    "Run setup_river_hydrography first or provide hydrography_fn dataset."
                )

        # (re)calculate region to make sure it's accurate
        region = self.mask.where(self.mask <= 1, 1).raster.vectorize()
        gdf_src, gdf_riv = workflows.river_boundary_points(
            da_flwdir=ds["flwdir"],
            da_uparea=ds["uparea"],
            region=region,
            river_len=river_len,
            river_upa=river_upa,
            btype="inflow",
            return_river=keep_rivers_geom,
            logger=self.logger,
        )
        if len(gdf_src.index) == 0:
            return

        # set forcing with dummy timeseries to keep valid sfincs model
        gdf_src = gdf_src.to_crs(self.crs.to_epsg())
        self.set_forcing_1d(xy=gdf_src, name="discharge")
        # set river
        if keep_rivers_geom and gdf_riv is not None:
            gdf_riv = gdf_riv.to_crs(self.crs.to_epsg())
            gdf_riv.index = gdf_riv.index.values + 1  # one based index
            self.set_geoms(gdf_riv, name="rivers_in")

        # update mask if closed_bounds_buffer > 0
        if river_width > 0:
            # apply buffer
            gdf_src_buf = gpd.GeoDataFrame(
                geometry=gdf_src.buffer(river_width / 2), crs=gdf_src.crs
            )
            # find intersect of buffer and model grid
            bounds = utils.mask_bounds(self.mask, gdf_mask=gdf_src_buf)
            # update model mask
            n = np.count_nonzero(bounds.values)
            if n > 0:
                da_mask = self.mask.where(~bounds, np.uint8(1))
                self.set_grid(da_mask, "msk")
                self.logger.debug(
                    f"{n:d} closed (mask=1) boundary cells set around src points."
                )

    def setup_river_outflow(
        self,
        hydrography_fn=None,
        river_upa=25.0,
        river_len=1e3,
        river_width=2e3,
        append_bounds=False,
        keep_rivers_geom=False,
        **kwargs,  # catch deprecated arguments
    ):
        """Setup open boundary cells (mask=3) where a river flows out of the model domain.

        Outflow locations are based on a minimal upstream area threshold. Locations within
        half `river_width` of a discharge source point or waterlevel boundary cells are omitted.

        NOTE: this method requires the either `hydrography_fn` input or `setup_river_hydrography` to be run first.
        NOTE: best to run after `setup_mask`, `setup_bounds` and `setup_river_inflow`

        Adds / edits model layers:

        * **msk** map: edited by adding outflow points (msk=3)
        * **river_out** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        hydrography_fn: str, Path, optional
            Path or data source name for hydrography raster data, by default 'merit_hydro'.
            * Required layers: ['uparea', 'flwdir'].
        river_width: int, optional
            The width [m] of the open boundary cells in the SFINCS msk file.
            By default 2km, i.e.: 1km to each side of the outflow location.
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 25.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1000 m.
        append_bounds: bool, optional
            If True, write new outflow boundary cells on top of existing. If False (default),
            first reset existing outflow boundary cells to normal active cells.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_out" in geoms. By default False.
        """
        if "outflow_width" in kwargs:
            self.logger.warning(
                "'outflow_width' is deprecated use 'river_width' instead."
            )
            river_width = kwargs.pop("outflow_width")
        if "basemaps_fn" in kwargs:
            self.logger.warning(
                "'basemaps_fn' is deprecated use 'hydrography_fn' instead."
            )
            hydrography_fn = kwargs.pop("basemaps_fn")

        if hydrography_fn is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography_fn,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=10,
            )
        else:
            ds = self.grid
            if "uparea" not in ds or "flwdir" not in ds:
                raise ValueError(
                    '"uparea" and/or "flwdir" layers missing. '
                    "Run setup_river_hydrography first or provide hydrography_fn dataset."
                )

        # (re)calculate region to make sure it's accurate
        region = self.mask.where(self.mask <= 1, 1).raster.vectorize()
        gdf_out, gdf_riv = workflows.river_boundary_points(
            da_flwdir=ds["flwdir"],
            da_uparea=ds["uparea"],
            region=region,
            river_len=river_len,
            river_upa=river_upa,
            btype="outflow",
            return_river=keep_rivers_geom,
            logger=self.logger,
        )
        if len(gdf_out.index) == 0:
            return

        # apply buffer
        gdf_out = gdf_out.to_crs(self.crs.to_epsg())  # assumes projected CRS
        gdf_out_buf = gpd.GeoDataFrame(
            geometry=gdf_out.buffer(river_width / 2.0), crs=gdf_out.crs
        )
        # remove points near waterlevel boundary cells
        da_mask = self.mask
        msk2 = (da_mask == 2).astype(np.int8)
        msk_wdw = msk2.raster.zonal_stats(gdf_out_buf, stats="max")
        bool_drop = (msk_wdw[f"{da_mask.name}_max"] == 1).values
        if np.any(bool_drop):
            self.logger.debug(
                f"{int(sum(bool_drop)):d} outflow (mask=3) boundary cells near water level (mask=2) boundary cells dropped."
            )
            gdf_out = gdf_out[~bool_drop]
        if len(gdf_out.index) == 0:
            self.logger.debug(f"0 outflow (mask=3) boundary cells set.")
            return
        # remove outflow points near source points
        fname = self._FORCING_1D["discharge"][0]
        if fname in self.forcing:
            gdf_src = self.forcing[fname].vector.to_gdf()
            idx_drop = gpd.sjoin(gdf_out_buf, gdf_src, how="inner").index.values
            if idx_drop.size > 0:
                gdf_out_buf = gdf_out_buf.drop(idx_drop)
                self.logger.debug(
                    f"{idx_drop.size:d} outflow (mask=3) boundary cells near src points dropped."
                )

        # find intersect of buffer and model grid
        bounds = utils.mask_bounds(da_mask, gdf_mask=gdf_out_buf)
        # update model mask
        if not append_bounds:  # reset existing outflow boundary cells
            da_mask = da_mask.where(da_mask != 3, np.uint8(1))
        bounds = np.logical_and(bounds, da_mask == 1)  # make sure not to overwrite
        n = np.count_nonzero(bounds.values)
        if n > 0:
            da_mask = da_mask.where(~bounds, np.uint8(3))
            self.set_grid(da_mask, "msk")
            self.logger.debug(f"{n:d} outflow (mask=3) boundary cells set.")
        if keep_rivers_geom and gdf_riv is not None:
            gdf_riv = gdf_riv.to_crs(self.crs.to_epsg())
            gdf_riv.index = gdf_riv.index.values + 1  # one based index
            self.set_geoms(gdf_riv, name="rivers_out")

    def setup_cn_infiltration(self, cn_fn="gcn250", antecedent_runoff_conditions="avg"):
        """Setup model potential maximum soil moisture retention map (scsfile)
        from gridded curve number map.

        Adds model layers:

        * **scs** map: potential maximum soil moisture retention [inch]

        Parameters
        ---------
        cn_fn: str, optional
            Name of gridded curve number map.

            * Required layers without antecedent runoff conditions: ['cn']
            * Required layers with antecedent runoff conditions: ['cn_dry', 'cn_avg', 'cn_wet']
        antecedent_runoff_conditions: {'dry', 'avg', 'wet'}, optional
            Antecedent runoff conditions.
            None if data has no antecedent runoff conditions.
            By default `avg`
        """
        # get data
        v = "cn"
        if antecedent_runoff_conditions:
            v = f"cn_{antecedent_runoff_conditions}"
        da_org = self.data_catalog.get_rasterdataset(
            cn_fn, geom=self.region, buffer=10, variables=[v]
        )
        # reproject using median
        da_cn = da_org.raster.reproject_like(self.grid, method="med")
        # CN=100 based on water mask
        if "rivmsk" in self.grid:
            self.logger.info(
                'Updating CN map based on "rivmsk" from setup_river_hydrography method.'
            )
            da_cn = da_cn.where(self.grid["rivmsk"] == 0, 100)
        # convert to potential maximum soil moisture retention S (1000/CN - 10) [inch]
        da_scs = workflows.cn_to_s(da_cn, self.mask > 0).round(3)
        # set grid
        mname = "scs"
        da_scs.attrs.update(**self._ATTRS.get(mname, {}))
        self.set_grid(da_scs, name=mname)
        # update config: remove default infiltration values and set scs map
        self.config.pop("qinf", None)
        self.set_config(f"{mname}file", f"sfincs.{mname}")

    def setup_manning_roughness(
        self,
        lulc_fn=None,
        map_fn=None,
        riv_man=0.03,
        lnd_man=0.1,
        sea_man=None,
    ):
        """Setup model manning roughness map (manningfile) from gridded
        land-use/land-cover map and manning roughness mapping table.

        Adds model layers:

        * **man** map: manning roughness coefficient [s.m-1/3]

        Parameters
        ---------
        lulc_fn: str, optional
            Name of landuse-landcover map.

            * Required layers: ['lulc']
        map_fn: path-like, optional
            CSV mapping file with lulc classes in the index column and manning values
            in another column with 'N' as header.
        lnd_man, riv_man, sea_man: float, optional
            Constant manning roughness values for land (by default 0.1 s.m-1/3)
            river (by default 0.03 s.m-1/3) and sea (by default None and skipped).
            River cells are based on the river mask ('rivmsk') grid layer
            from the `setup_river_hydrography` component. Sea cells are based on elevation
            values smaller than zero.
            Manning roughness for land cells are superseeded by the landuse-landcover
            map based values if `lulc_fn` is not None.
        """
        da_msk = self.mask > 0
        da_man = xr.full_like(da_msk, lnd_man, dtype=np.float32)
        da_man.raster.set_nodata(-9999.0)
        if lulc_fn is not None:
            if map_fn is None:
                map_fn = join(DATADIR, "lulc", f"{lulc_fn}_mapping.csv")
            if not os.path.isfile(map_fn):
                raise IOError(f"Manning roughness mapping file not found: {map_fn}")
            da_org = self.data_catalog.get_rasterdataset(
                lulc_fn, geom=self.region, buffer=10, variables=["lulc"]
            )
            # reproject and reclassify
            # TODO use generic names for parameters
            # FIXME use hydromt general version!!
            da_man = workflows.landuse(
                da_org, da_msk, map_fn, logger=self.logger, params=["N"]
            )["N"]
        if "rivmsk" in self.grid and riv_man is not None:
            self.logger.info("Setting constant manning roughness for river cells.")
            da_man = da_man.where(self.grid["rivmsk"] != 1, riv_man)
        elif lulc_fn is None:
            self.logger.warning(
                'Skipping spatial variable manning roughness map as no river mask ("rivmsk" grid layer)'
                ' or landuse-landcover map ("lulc_fn" argument) was provided. Set constant manning roughness'
                ' using the "manning", "manning_land" and/or "manning_sea" parameters in the sfincs.inp file.'
            )
            return
        if sea_man is not None:
            self.logger.info("Setting constant manning roughness for sea cells.")
            da_man = da_man.where(self.grid["dep"] >= 0, sea_man)
        # mask and set precision
        da_man = da_man.where(da_msk, da_man.raster.nodata).round(3)
        # set grid
        mname = "manning"
        da_man.attrs.update(**self._ATTRS.get(mname, {}))
        self.set_grid(da_man, name=mname)
        # update config: remove default manning values and set maning map
        for v in ["manning_land", "manning_sea", "rgh_lev_land"]:
            self.config.pop(v, None)
        self.set_config(f"{mname}file", f"sfincs.{mname[:3]}")

    def setup_gauges(self, gauges_fn, overwrite=False, **kwargs):
        """Setup model observation point locations.

        Adds model layers:

        * **obs** geom: observation point locations

        Parameters
        ---------
        gauges_fn: str
            Path to observation points geometry file.
            See :py:meth:`hydromt.open_vector`, for accepted files.
        overwrite: bool, optional
            If True, overwrite existing gauges instead of appending the new gauges.
        """
        name = self._GEOMS["gauges"]
        # ensure the catalog is loaded before adding any new entries
        self.data_catalog.sources
        gdf = self.data_catalog.get_geodataframe(
            gauges_fn, geom=self.region, assert_gtype="Point", **kwargs
        ).to_crs(self.crs)
        if not overwrite and name in self.geoms:
            gdf0 = self._geoms.pop(name)
            gdf = gpd.GeoDataFrame(pd.concat([gdf, gdf0], ignore_index=True))
            self.logger.info(f"Adding new gauges to existing gauges.")
        self.set_geoms(gdf, name)
        self.set_config(f"{name}file", f"sfincs.{name}")
        self.logger.info(f"{name} set based on {gauges_fn}")

    def setup_structures(
        self, structures_fn, stype, dz=None, overwrite=False, **kwargs
    ):
        """Setup thin dam or weir structures.

        Adds model layer (depending on `stype`):

        * **thd** geom: thin dam
        * **weir** geom: weir / levee

        Parameters
        ----------
        structures_fn : str, Path
            Path to structure line geometry file.
            The "name" (for thd and weir), "z" and "par1" (for weir only) are optional.
            For weirs: `dz` must be provided if gdf has no "z" column or Z LineString;
            "par1" defaults to 0.6 if gdf has no "par1" column.
        stype : {'thd', 'weir'}
            Structure type.
        overwrite: bool, optional
            If True, overwrite existing 'stype' structures instead of appending the
            new structures.
        dz: float, optional
            If provided, for weir structures the z value is calculated from
            the model elevation (dep) plus dz.
        """
        cols = {
            "thd": ["name", "geometry"],
            "weir": ["name", "z", "par1", "geometry"],
        }
        assert stype in cols
        # read, clip and reproject
        gdf = self.data_catalog.get_geodataframe(
            structures_fn, geom=self.region, **kwargs
        ).to_crs(self.crs)
        gdf = gdf[[c for c in cols[stype] if c in gdf.columns]]  # keep relevant cols
        structs = utils.gdf2structures(gdf)  # check if it parsed correct
        # sample zb values from dep file and set z = zb + dz
        if stype == "weir" and dz is not None:
            elv = self.grid["dep"]
            structs_out = []
            for s in structs:
                pnts = gpd.points_from_xy(x=s["x"], y=s["y"])
                zb = elv.raster.sample(gpd.GeoDataFrame(geometry=pnts, crs=self.crs))
                s["z"] = zb.values + float(dz)
                structs_out.append(s)
            gdf = utils.structures2gdf(structs_out, crs=self.crs)
        elif stype == "weir" and np.any(["z" not in s for s in structs]):
            raise ValueError("Weir structure requires z values.")
        # combine with existing structures if present
        if not overwrite and stype in self.geoms:
            gdf0 = self._geoms.pop(stype)
            gdf = gpd.GeoDataFrame(pd.concat([gdf, gdf0], ignore_index=True))
            self.logger.info(f"Adding {stype} structures to existing structures.")
        # set structures
        self.set_geoms(gdf, stype)
        self.set_config(f"{stype}file", f"sfincs.{stype}")
        self.logger.info(f"{stype} structure set based on {structures_fn}")

    ### FORCING
    def setup_h_forcing(
        self,
        geodataset_fn=None,
        timeseries_fn=None,
        offset_fn=None,
        buffer=5e3,
        **kwargs,
    ):
        """Setup waterlevel boundary point locations (bnd) and time series (bzs).

        Use `geodataset_fn` to set the waterlevel boundary from a dataset of point location
        timeseries. The dataset is clipped to the model region plus `buffer` [m], and
        model time based on the model config tstart and tstop entries.

        Use `timeseries_fn` in combination with `geodataset_fn=None` to set a spatially
        uniform waterlevel for all waterlevel boundary cells (msk==2),

        If `timeseries_fn` and `geodataset_fn` are both not provided a dummy (h=0) waterlevel
        boundary is set.

        The vertical reference of the waterlevel data can be corrected to match
        the vertical reference of the model elevation (dep) layer by adding
        a local offset value derived from the `offset_fn` map to the waterlevels,
        e.g. mean dynamic topography for difference between EGM and MSL levels.

        Adds model layers:

        * **bnd** geom: waterlevel gauge point locations
        * **bzs** forcing: waterlevel time series [m+ref]

        Parameters
        ----------
        geodataset_fn: str, Path
            Path or data source name for geospatial point timeseries file.
            This can either be a netcdf file with geospatial coordinates
            or a combined point location file with a `timeseries_fn` data csv file.

            * Required variables if netcdf: ['waterlevel']
            * Required coordinates if netcdf: ['time', 'index', 'y', 'x']
        timeseries_fn: str, Path
            Path to spatially uniform timeseries csv file with time index in first column
            and waterlevels in the second column. The first row is interpreted as header,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            NOTE: tabulated timeseries files can only in combination with point location
            coordinates be set as a geodataset in the data_catalog yml file.
        offset_fn: str, optional
            Path or data source name for gridded offset between vertical reference of elevation and waterlevel data,
            Adds to the waterlevel data before merging.

            * Required variables: ['mdt']
        buffer: float, optional
            Buffer [m] around model water level boundary cells to select waterlevel gauges,
            by default 5 km.

        """
        name = "waterlevel"
        msk2 = self.mask == 2
        if not np.any(msk2):
            # No waterlevel boundary remove bnd/bzs from sfincs.inp
            self.logger.warning(
                "No waterlevel boundary cells (msk==2) in model mask. "
                "Update the mask layer first before setting waterlevel timeseries."
            )
            return

        tstart, tstop = self.get_model_time()  # model time
        if geodataset_fn is not None:
            if timeseries_fn is not None:
                kwargs.update(fn_data=str(timeseries_fn))
            # read and clip data in time & space
            # buffer around msk==2 values
            region = self.mask.where(self.mask == 2, 0).raster.vectorize()
            da = self.data_catalog.get_geodataset(
                geodataset_fn,
                geom=region,
                buffer=buffer,
                variables=[name],
                time_tuple=(tstart, tstop),
                crs=self.crs.to_epsg(),  # assume model crs if no explicit crs defined
                **kwargs,
            )
        else:
            # create bnd point on single waterlevel boundary cell
            x, y = self.grid.raster.xy(*np.where(msk2))
            gdf = gpd.GeoDataFrame(
                index=[1], geometry=gpd.points_from_xy(x[[0]], y[[0]]), crs=self.crs
            )
            if timeseries_fn is not None:
                da_ts = hydromt.open_timeseries(timeseries_fn, name=name).sel(
                    time=slice(tstart, tstop)
                )
                assert (
                    da_ts["index"].size == 1
                ), "Uniform waterlevel should contain single time series."
                da = GeoDataArray.from_gdf(gdf, da_ts, index_dim="index")
            else:
                self.set_forcing_1d(xy=gdf, name=name)  # dummy timeseries
                return
        # correct for MDT
        if offset_fn is not None and isfile(offset_fn):
            da_mdt = self.data_catalog.get_rasterdataset(
                offset_fn, geom=self.region, buffer=buffer, variables=["mdt"]
            )
            mdt_pnts = da_mdt.raster.sample(da.vector.to_gdf()).fillna(0)
            da = da + mdt_pnts
            mdt_avg = mdt_pnts.mean().values
            self.logger.debug(f"{name} forcing: applied MDT (avg: {mdt_avg:+.2f})")
        self.set_forcing_1d(ts=da, name=name)

    def setup_q_forcing(self, geodataset_fn=None, timeseries_fn=None, **kwargs):
        """Setup discharge boundary point locations (src) and time series (dis).

        Use `geodataset_fn` to set the discharge boundary from a dataset of point location
        timeseries. Only locations within the model domain are selected.

        Use `timeseries_fn` to set discharge boundary conditions to pre-set (src) locations,
        e.g. after the :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow` method.

        The dataset/timeseries are clipped to the model time based on the model config
        tstart and tstop entries.

        Adds model layers:

        * **src** geom: discharge gauge point locations
        * **dis** forcing: discharge time series [m3/s]

        Parameters
        ----------
        geodataset_fn: str, Path
            Path or data source name for geospatial point timeseries file.
            This can either be a netcdf file with geospatial coordinates
            or a combined point location file with a timeseries data csv file
            which can be setup through the data_catalog yml file.

            * Required variables if netcdf: ['discharge']
            * Required coordinates if netcdf: ['time', 'index', 'y', 'x']
        timeseries_fn: str, Path
            Path to tabulated timeseries csv file with time index in first column
            and location IDs in the first row,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            NOTE: tabulated timeseries files can only in combination with point location
            coordinates be set as a geodataset in the data_catalog yml file.

        """
        name = "discharge"
        fname = self._FORCING[name][0]
        tstart, tstop = self.get_model_time()  # time slice
        if geodataset_fn is None and fname not in self.forcing:
            self.logger.warning(
                "No discharge inflow (src) points set: "
                "Run ``setup_river_inflow()`` method first or provide locations."
            )
            return
        elif geodataset_fn is not None:
            if timeseries_fn is not None:
                kwargs.update(fn_data=str(timeseries_fn))
            # read and clip data
            da = (
                self.data_catalog.get_geodataset(
                    geodataset_fn,
                    geom=self.region,
                    variables=[name],
                    time_tuple=(tstart, tstop),
                    crs=self.crs.to_epsg(),  # assume model crs if none defined
                    **kwargs,
                )
                .fillna(0.0)
                .rename(fname)
            )
            self.set_forcing_1d(ts=da, name=name)
        elif timeseries_fn is not None:
            # read timeseries data and match with existing gdf
            gdf = self.forcing[fname].vector.to_gdf()
            da_ts = hydromt.open_timeseries_from_table(timeseries_fn, name=name)
            da_ts = da_ts.sel(time=slice(tstart, tstop)).fillna(0.0)
            self.set_forcing_1d(ts=da_ts, xy=gdf, name=name)
        else:
            raise ValueError(
                'Either "geodataset_fn" or "timeseries_fn" must be provided.'
            )

    def setup_q_forcing_from_grid(
        self,
        discharge_fn,
        locs_fn=None,
        uparea_fn=None,
        wdw=1,
        rel_error=0.05,
        abs_error=50,
        **kwargs,  # catch deprecated args
    ):
        """Setup discharge boundary location (src) and timeseries (dis) based on a
        gridded discharge dataset.

        If `locs_fn` is not provided, the discharge source locations are expected to be
        pre-set, e.g. using the :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow` method.

        If an upstream area grid is provided the discharge boundary condition is
        snapped to the best fitting grid cell within a `wdw` neighboring cells.
        The best fit is dermined based on the minimal relative upstream area error if
        an upstream area value is available for the discharge boundary locations;
        otherwise it is based on maximum upstream area.

        Adds model layers:

        * **dis** forcing: discharge time series [m3/s]
        * **src** geom: discharge gauge point locations

        Adds meta layer (not used by SFINCS):

        * **src_snapped** geom: snapped gauge location on discharge grid

        Parameters
        ----------
        discharge_fn: str, Path, optional
            Path or data source name for gridded discharge timeseries dataset.

            * Required variables: ['discharge' (m3/s)]
            * Required coordinates: ['time', 'y', 'x']
        locs_fn: str, Path, optional
            Path or data source name for point location dataset. Not required if
            point location have previously been set with :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow`
            See :py:meth:`hydromt.open_vector`, for accepted files.

        uparea_fn: str, Path, optional
            Path to upstream area grid in gdal (e.g. geotiff) or netcdf format.

            * Required variables: ['uparea' (km2)]
        wdw: int, optional
            Window size in number of cells around discharge boundary locations
            to snap to, only used if ``uparea_fn`` is provided. By default 1.
        rel_error, abs_error: float, optional
            Maximum relative error (default 0.05) and absolute error (default 50 km2)
            between the discharge boundary location upstream area and the upstream area of
            the best fit grid cell, only used if "discharge" geoms has a "uparea" column.
        """
        if "max_error" in kwargs:
            self.logger.warning(
                "'max_error' is deprecated use 'rel_error' and 'abs_error' instead."
            )
            rel_error = kwargs.pop("max_error")
            abs_error = 0  # mimic old behaviour

        name = "discharge"
        fname = self._FORCING[name][0]
        if locs_fn is not None:
            gdf = self.data_catalog.get_geodataframe(
                locs_fn, geom=self.region, assert_gtype="Point"
            ).to_crs(self.crs)
        elif fname in self.forcing:
            da = self.forcing[fname]
            gdf = da.vector.to_gdf()
        else:
            self.logger.warning(
                'No discharge inflow points in geoms. Provide locations using "locs_fn" or '
                'run "setup_river_inflow()" method first to determine inflow locations.'
            )
            return
        # read data
        ds = self.data_catalog.get_rasterdataset(
            discharge_fn,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),  # model time
            variables=[name],
            single_var_as_array=False,
        )
        if uparea_fn is not None and "uparea" in gdf.columns:
            da_upa = self.data_catalog.get_rasterdataset(
                uparea_fn, geom=self.region, buffer=2, variables=["uparea"]
            )
            # make sure ds and da_upa align
            ds["uparea"] = da_upa.raster.reproject_like(ds, method="nearest")
        elif "uparea" not in gdf.columns:
            self.logger.warning('No "uparea" column found in location data.')

        ds_snapped = workflows.snap_discharge(
            ds=ds,
            gdf=gdf,
            wdw=wdw,
            rel_error=rel_error,
            abs_error=abs_error,
            uparea_name="uparea",
            discharge_name=name,
            logger=self.logger,
        )
        # set zeros for src points without matching discharge
        da_q = ds_snapped[name].reindex(index=gdf.index, fill_value=0).fillna(0)
        # update forcing
        self.set_forcing_1d(name=name, ts=da_q, xy=gdf)
        # keep snapped locations
        self.set_geoms(ds_snapped.vector.to_gdf(), f"{self._FORCING[name][1]}_snapped")

    def setup_p_forcing_from_grid(
        self, precip_fn=None, dst_res=None, aggregate=False, **kwargs
    ):
        """Setup precipitation forcing from a gridded spatially varying data source.

        If aggregate is True, spatially uniform precipitation forcing is added to
        the model based on the mean precipitation over the model domain.
        If aggregate is False, distributed precipitation is added to the model as netcdf file.
        The data is reprojected to the model CRS (and destination resolution `dst_res` if provided).

        Adds one of these model layer:

        * **netamprfile** forcing: distributed precipitation [mm/hr]
        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        precip_fn, str, Path
            Path to precipitation rasterdataset netcdf file.

            * Required variables: ['precip' (mm)]
            * Required coordinates: ['time', 'y', 'x']

        dst_res: float
            output resolution (m), by default None and computed from source data.
            Only used in combination with aggregate=False
        aggregate: bool, {'mean', 'median'}, optional
            Method to aggregate distributed input precipitation data. If True, mean
            aggregation is used, if False (default) the data is not aggregated and
            spatially distributed precipitation is returned.
        """
        variable = "precip"
        # get data for model domain and config time range
        precip = self.data_catalog.get_rasterdataset(
            precip_fn,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),
            variables=[variable],
        )

        # aggregate or reproject in space
        if aggregate:
            stat = aggregate if isinstance(aggregate, str) else "mean"
            self.logger.debug(f"Aggregate {variable} using {stat}.")
            zone = self.region.dissolve()  # make sure we have a single (multi)polygon
            precip_out = precip.raster.zonal_stats(zone, stats=stat)[f"precip_{stat}"]
            precip_out = precip_out.where(precip_out >= 0, 0).fillna(0).squeeze()
        else:
            # reproject to model utm crs
            # NOTE: currently SFINCS errors (stack overflow) on large files,
            # downscaling to model grid is not recommended
            kwargs0 = dict(align=dst_res is not None, method="nearest_index")
            kwargs0.update(kwargs)
            meth = kwargs0["method"]
            self.logger.debug(f"Resample {variable} using {meth}.")
            precip_out = precip.raster.reproject(
                dst_crs=self.crs, dst_res=dst_res, **kwargs
            ).fillna(0)

        # resample in time
        precip_out = hydromt.workflows.resample_time(
            precip_out,
            freq=pd.to_timedelta("1H"),
            conserve_mass=True,
            upsampling="bfill",
            downsampling="sum",
            logger=self.logger,
        )
        precip_out.name = "Precipitation"  # capital is important for netamprfile

        # set correct names and attrs and add forcing
        fname = self._FORCING[variable][0]
        fname2 = self._FORCING[f"{variable}2D"][0]
        if aggregate:
            # remove netamprfile
            self._forcing.pop(fname2, None)
            self._config.pop(f"{fname2}file", None)
            # precipfile = sfincs.precip
            fn_out = f"sfincs.{fname}"
        else:
            # remove precipfile
            self._forcing.pop(fname, None)
            self._config.pop(f"{fname}file", None)
            # netamprfile = precip.nc
            fn_out = f"{variable}.nc"
            fname = fname2
        precip_out.attrs.update(**self._ATTRS.get(fname, {}))
        self.set_config(f"{fname}file", fn_out)
        self.set_forcing(precip_out, name=fname)

    def setup_p_forcing(self, precip_fn=None, **kwargs):
        """Setup spatially uniform precipitation forcing (precip).

        Adds model layers:

        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        precip_fn, str, Path
            Path to tabulated timeseries csv file with time index in first column
            and location IDs in the first row,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            Note: tabulated timeseries files cannot yet be set through the data_catalog yml file.
        """
        ts = hydromt.open_timeseries_from_table(precip_fn, **kwargs)
        self.set_forcing_1d(name="precip", ts=ts.squeeze())
        # remove netamprfile
        fname2 = self._FORCING["precip2D"][0]
        self._forcing.pop(fname2, None)
        self._config.pop(f"{fname2}file", None)

    def plot_forcing(self, fn_out="forcing.png", **kwargs):
        """Plot model timeseries forcing.

        For distributed forcing a spatial avarage is plotted.

        Parameters
        ----------
        fn_out: str
            Path to output figure file.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        forcing : Dict of xr.DataArray
            Model forcing

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        if self.forcing:
            # update missing attributes for plot labels
            for name in self.forcing:
                attrs = self._ATTRS.get(name, {})
                self.forcing[name].attrs.update(**attrs)
            fig, axes = plots.plot_forcing(self.forcing, **kwargs)

            # set xlim to model tstart - tend
            tstart, tstop = self.get_model_time()
            axes[-1].set_xlim(mdates.date2num([tstart, tstop]))

            # save figure
            if fn_out is not None:
                if not os.path.isabs(fn_out):
                    fn_out = join(self.root, "figs", fn_out)
                if not os.path.isdir(dirname(fn_out)):
                    os.makedirs(dirname(fn_out))
                plt.savefig(fn_out, dpi=225, bbox_inches="tight")
            return fig, axes

    def plot_basemap(
        self,
        fn_out: str = "basemap.png",
        variable: str = "dep",
        shaded: bool = True,
        plot_bounds: bool = True,
        plot_region: bool = False,
        plot_geoms: bool = True,
        bmap: str = "sat",
        zoomlevel: int = 11,
        figsize: Tuple[int] = None,
        geom_names: List[str] = None,
        geom_kwargs: Dict = {},
        legend_kwargs: Dict = {},
        **kwargs,
    ):
        """Create basemap plot.

        Parameters
        ----------
        fn_out: str
            Path to output figure file.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        variable : str, optional
            Map of variable in ds to plot, by default 'dep'
        shaded : bool, optional
            Add shade to variable (only for variable = 'dep'), by default True
        plot_bounds : bool, optional
            Add waterlevel (msk=2) and open (msk=3) boundary conditions to plot.
        plot_region : bool, optional
            If True, plot region outline.
        plot_geoms : bool, optional
            If True, plot available geoms.
        bmap : {'sat', ''}
            background map, by default "sat"
        zoomlevel : int, optional
            zoomlevel, by default 11
        figsize : Tuple[int], optional
            figure size, by default None
        geom_names : List[str], optional
            list of model geometries to plot, by default all model geometries.
        geom_kwargs : Dict of Dict, optional
            Model geometry styling per geometry, passed to geopandas.GeoDataFrame.plot method.
            For instance: {'src': {'markersize': 30}}.
        legend_kwargs : Dict, optional
            Legend kwargs, passed to ax.legend method.

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.pyplot as plt

        # combine geoms and forcing locations
        sg = self.geoms.copy()
        for fname, gname in self._FORCING.values():
            if fname in self.forcing and gname is not None:
                sg.update({gname: self._forcing[fname].vector.to_gdf()})

        # make sure grid are set
        if "msk" not in self.grid:
            self.set_grid(self.mask, "msk")

        fig, ax = plots.plot_basemap(
            self.grid,
            sg,
            variable=variable,
            shaded=shaded,
            plot_bounds=plot_bounds,
            plot_region=plot_region,
            plot_geoms=plot_geoms,
            bmap=bmap,
            zoomlevel=zoomlevel,
            figsize=figsize,
            geom_names=geom_names,
            geom_kwargs=geom_kwargs,
            legend_kwargs=legend_kwargs,
            **kwargs,
        )

        if fn_out is not None:
            if not os.path.isabs(fn_out):
                fn_out = join(self.root, "figs", fn_out)
            if not os.path.isdir(dirname(fn_out)):
                os.makedirs(dirname(fn_out))
            plt.savefig(fn_out, dpi=225, bbox_inches="tight")

        return fig, ax

    # I/O
    def read(self):
        """Read the complete model schematization and configuration from file."""
        self.read_config()
        self.read_grid()
        self.read_geoms()
        self.read_forcing()
        self.logger.info("Model read")

    def write(self):
        """Write the complete model schematization and configuration to file."""
        self.logger.info(f"Writing model data to {self.root}")
        self.write_grid()
        self.write_geoms()
        self.write_forcing()
        self.write_states()
        # config last; might be udpated when writing maps, states or forcing
        self.write_config()
        # write data catalog with used data sources
        self.write_data_catalog()  # new in hydromt v0.4.4

    def read_grid(self, data_vars: Union[List, str] = None) -> None:
        """Read SFINCS binary grid and save to `grid` attribute."""
        da_lst = []
        if data_vars is None:
            data_vars = self._MAPS
        elif isinstance(data_vars, str):
            data_vars = list(data_vars)

        # read index file
        ind_fn = self.get_config("indexfile", fallback="sfincs.ind", abs_path=True)
        if not isfile(ind_fn):
            raise IOError(f".ind path {ind_fn} does not exist")

        dtypes = {"msk": "u1"}
        mvs = {"msk": 0}
        if self.reggrid is not None:
            ind = self.reggrid.read_ind(ind_fn=ind_fn)

            for name in data_vars:
                if f"{name}file" in self.config:
                    fn = self.get_config(
                        f"{name}file", fallback=f"sfincs.{name}", abs_path=True
                    )
                    if not isfile(fn):
                        self.logger.warning(f"{name}file not found at {fn}")
                        continue
                    dtype = dtypes.get(name, "f4")
                    mv = mvs.get(name, -9999.0)
                    da = self.reggrid.read_map(fn, ind, dtype, mv, name=name)
                    da_lst.append(da)
            ds = xr.merge(da_lst)
            crs = self.config.get("crs", None)
            if crs is not None:
                ds.raster.set_crs(crs)
            self.set_grid(ds)

            # keep some metadata maps from gis directory
            keep_maps = ["flwdir", "uparea", "rivmsk"]
            fns = glob.glob(join(self.root, "gis", "*.tif"))
            fns = [fn for fn in fns if basename(fn).split(".")[0] in keep_maps]
            if fns:
                ds = hydromt.open_mfraster(fns).load()
                self.set_grid(ds)
                ds.close()

    def write_grid(self, data_vars: Union[List, str] = None):
        """Write SFINCS grid to binary files including map index file.
        Filenames are taken from the `config` attribute (i.e. input file).

        If `write_gis` property is True, all grid are written to geotiff
        files in a "gis" subfolder.
        """
        self._assert_write_mode

        dtypes = {"msk": "u1"}  # default to f4
        if self.reggrid and len(self._grid.data_vars) > 0 and "msk" in self.grid:
            # make sure orientation is S->N
            ds_out = self.grid
            if ds_out.raster.res[1] < 0:
                ds_out = ds_out.raster.flipud()
            mask = ds_out["msk"].values

            self.logger.debug("Write binary map indices based on mask.")
            ind_fn = self.get_config("indexfile", abs_path=True)
            self.reggrid.write_ind(ind_fn=ind_fn, mask=mask)

            if data_vars is None:  # write all maps
                data_vars = [v for v in self._MAPS if v in ds_out]
            elif isinstance(data_vars, str):
                data_vars = list(data_vars)
            self.logger.debug(f"Write binary map files: {data_vars}.")
            for name in data_vars:
                if f"{name}file" not in self.config:
                    self.set_config(f"{name}file", f"sfincs.{name}")
                self.reggrid.write_map(
                    map_fn=self.get_config(f"{name}file", abs_path=True),
                    data=ds_out[name].values,
                    mask=mask,
                    dtype=dtypes.get(name, "f4"),
                )

        if self._write_gis:
            self.write_raster("grid")

    def read_geoms(self):
        """Read geometry files if and save to `geoms` attribute.
        Known geometry files mentioned in the sfincs.inp configuration file are read,
        including: bnd/src/obs xy files and thd/weir structure files.

        If other geojson files are present in a "gis" subfolder folder, those are read as well.
        """
        if not self._write:
            self._geoms = {}  # fresh start in read-only mode
        # read _GEOMS model files
        for gname in self._GEOMS.values():
            if f"{gname}file" in self.config:
                fn = self.get_config(f"{gname}file", abs_path=True)
                if fn is None:
                    continue
                elif not isfile(fn):
                    self.logger.warning(f"{gname}file not found at {fn}")
                    continue
                if gname in ["thd", "weir"]:
                    struct = utils.read_structures(fn)
                    gdf = utils.structures2gdf(struct, crs=self.crs)
                else:
                    gdf = utils.read_xy(fn, crs=self.crs)
                self.set_geoms(gdf, name=gname)
        # read additional geojson files from gis directory
        for fn in glob.glob(join(self.root, "gis", "*.geojson")):
            name = basename(fn).replace(".geojson", "")
            gnames = [f[1] for f in self._FORCING.values() if f[1] is not None]
            skip = gnames + list(self._GEOMS.values())
            if name in skip:
                continue
            gdf = hydromt.open_vector(fn, crs=self.crs)
            self.set_geoms(gdf, name=name)

    def write_geoms(self):
        """Write geoms to bnd/src/obs xy files and thd/weir structure files.
        Filenames are based on the `config` attribute.

        If `write_gis` property is True, all geoms are written to geojson
        files in a "gis" subfolder.
        """
        self._assert_write_mode

        if self._geoms:
            self.logger.info("Write staticgeom files")
            for gname, gdf in self.geoms.items():
                if gname in self._GEOMS.values():
                    if f"{gname}file" not in self.config:
                        self.set_config(f"{gname}file", f"sfincs.{gname}")
                    fn = self.get_config(f"{gname}file", abs_path=True)
                    if gname in ["thd", "weir"]:
                        struct = utils.gdf2structures(gdf)
                        utils.write_structures(fn, struct, stype=gname)
                    else:
                        utils.write_xy(fn, gdf, fmt="%8.2f")
            if self._write_gis:
                self.write_vector(variables=["geoms"])

    def read_forcing(self, data_vars: List = None):
        """Read forcing files and save to `forcing` attribute.
        Known forcing files mentioned in the sfincs.inp configuration file are read,
        including: bzd/dis/precip ascii files and the netampr netcdf file.
        """
        self._assert_read_mode
        if not self._write:
            # start fresh in read-only mode
            self._forcing = {}
        if isinstance(data_vars, str):
            data_vars = list(data_vars)

        # 1D
        dvars_1d = self._FORCING_1D
        if data_vars is not None:
            dvars_1d = [name for name in data_vars if name in dvars_1d]
        tref = utils.parse_datetime(self.config["tref"])
        for name in dvars_1d:
            ts_names, xy_name = self._FORCING_1D[name]
            # read time series
            da_lst = []
            for ts_name in ts_names:
                ts_fn = self.get_config(f"{ts_name}file", abs_path=True)
                if ts_fn is None or not isfile(ts_fn):
                    if ts_fn is not None:
                        self.logger.warning(f"{ts_name}file not found at {ts_fn}")
                    continue
                df = utils.read_timeseries(ts_fn, tref)
                df.index.name = "time"
                if xy_name is not None:
                    df.columns.name = "index"
                    da = xr.DataArray(df, dims=("time", "index"), name=ts_name)
                else:  # spatially uniform forcing
                    da = xr.DataArray(df[df.columns[0]], dims=("time"), name=ts_name)
                da_lst.append(da)
            ds = xr.merge(da_lst)
            # read xy
            if xy_name is not None:
                xy_fn = self.get_config(f"{xy_name}file", abs_path=True)
                if xy_fn is None or not isfile(xy_fn):
                    if xy_fn is not None:
                        self.logger.warning(f"{xy_name}file not found at {xy_fn}")
                else:
                    gdf = utils.read_xy(xy_fn, crs=self.config.get("crs"))
                    # read attribute data from gis files
                    gis_fn = join(self.root, "gis", f"{xy_name}.geojson")
                    if isfile(gis_fn):
                        gdf1 = gpd.read_file(gis_fn)
                        if np.any(gdf1.columns != "geometry"):
                            gdf = gpd.sjoin(gdf, gdf1, how="left")[gdf1.columns]
                    # set locations as coordinates dataset
                    ds = GeoDataset.from_gdf(gdf, ds, index_dim="index")
            # save in self.forcing
            self.set_forcing(ds, name=name)

        # 2D
        dvars_2d = self._FORCING_2D
        if data_vars is not None:
            dvars_2d = [name for name in data_vars if name in dvars_2d]
        for name in dvars_2d:
            fn = self.get_config(f"{name}file", abs_path=True)
            if fn is None or not isfile(fn):
                if fn is not None:
                    self.logger.warning(f"{name}file not found at {fn}")
            else:
                da = xr.open_dataarray(fn, chunks="auto")  # lazy
                self.set_forcing(da, name=name)

    def write_forcing(self):
        """Write forcing to ascii (bzd/dis/precip) and netcdf (netampr) files.
        Filenames are based on the `config` attribute.
        """
        # TODO add data_vars argumetn, split 1d, 2d
        self._assert_write_mode

        if self._forcing:
            self.logger.info("Write forcing files")
            tref = utils.parse_datetime(self.config["tref"])
            # for nc files -> time in minutes since tref
            tref_str = tref.strftime("%Y-%m-%d %H:%M:%S")
            encoding = dict(
                time={"units": f"minutes since {tref_str}", "dtype": "float64"}
            )
            names = {f[0]: f[1] for f in self._FORCING.values()}
            gis_names = []
            for fname in self._forcing:
                if fname not in names:
                    logger.warning(f"{fname} forcing unknown and skipped.")
                    continue
                if f"{fname}file" not in self.config:
                    self.set_config(f"{fname}file", f"sfincs.{fname}")
                fn = self.get_config(f"{fname}file", abs_path=True)
                da = self._forcing[fname]
                if "net" in fname:  # spatially distributed forcing
                    da.to_netcdf(fn, encoding=encoding)
                else:
                    if len(da.dims) == 2:  # forcing at point locations
                        df = da.to_series().unstack(0)
                        gname = names[fname]
                        if gname is None:
                            raise ValueError(f"Locations missing for {fname}")
                        gdf = self._forcing[fname].vector.to_gdf()
                        if f"{gname}file" not in self.config:
                            self.set_config(f"{gname}file", f"sfincs.{gname}")
                        fn_xy = self.get_config(f"{gname}file", abs_path=True)
                        utils.write_xy(fn_xy, gdf, fmt="%8.2f")
                        gis_names.append(fname)
                    else:  # spatially uniform forcing
                        df = da.to_series().to_frame()
                    utils.write_timeseries(fn, df, tref)
            if self._write_gis and len(gis_names) > 0:
                self.write_vector(variables=[f"forcing.{name}" for name in gis_names])

    def read_states(self, crs=None):
        """Read waterlevel state (zsini) from ascii file and save to `states` attribute.
        The inifile if mentioned in the sfincs.inp configuration file is read.

        Parameters
        ----------
        crs: int, CRS
            Coordinate reference system, if provided use instead of epsg code from sfincs.inp
        """
        if not self._write:
            # start fresh in read-only mode
            self._states = {}
        if "inifile" in self.config:
            fn = self.get_config("inifile", abs_path=True)
            if not isfile(fn):
                self.logger.warning("inifile not found at {fn}")
                return
            shape, transform, crs = self.get_spatial_attrs(crs=crs)
            zsini = RasterDataArray.from_numpy(
                data=utils.read_ascii_map(fn),  # orientation S-N
                transform=transform,
                crs=crs,
                nodata=-9999,  # TODO: check what a good nodatavalue is
            )
            if zsini.shape != shape:
                raise ValueError('The shape of "inifile" and maps does not match.')
            if "msk" in self._grid:
                zsini = zsini.where(self.mask != 0, -9999)
            self.set_states(zsini, "zsini")

    def write_states(self, fmt="%8.3f"):
        """Write waterlevel state (zsini)  to ascii map file.
        The filenames is based on the `config` attribute.
        """
        self._assert_write_mode

        assert len(self._states) <= 1
        for name in self._states:
            if f"inifile" not in self.config:
                self.set_config(f"inifile", f"sfincs.{name}")
            fn = self.get_config("inifile", abs_path=True)
            da = self._states[name].fillna(0)  # TODO check proper nodata value
            if da.raster.res[1] < 0:  # orientation is S->N
                da = da.raster.flipud()
            utils.write_ascii_map(fn, da.values, fmt=fmt)
        if self._write_gis:
            self.write_raster("states")

    def read_results(
        self,
        chunksize=100,
        drop=["crs", "sfincsgrid"],
        fn_map="sfincs_map.nc",
        fn_his="sfincs_his.nc",
        **kwargs,
    ):
        """Read results from sfincs_map.nc and sfincs_his.nc and save to the `results` attribute.
        The staggered nc file format is translated into hydromt.RasterDataArray formats.
        Additionally, hmax is computed from zsmax and zb if present.

        Parameters
        ----------
        chunksize: int, optional
            chunk size along time dimension, by default 100
        """
        if not isabs(fn_map):
            fn_map = join(self.root, fn_map)
        if isfile(fn_map):
            ds_face, ds_edge = utils.read_sfincs_map_results(
                fn_map,
                crs=self.crs,
                chunksize=chunksize,
                drop=drop,
                logger=self.logger,
                **kwargs,
            )
            # save as dict of DataArray
            self.set_results(ds_face, split_dataset=True)
            self.set_results(ds_edge, split_dataset=True)

        if not isabs(fn_his):
            fn_his = join(self.root, fn_his)
        if isfile(fn_his):
            ds_his = utils.read_sfincs_his_results(
                fn_his, crs=self.crs, chunksize=chunksize
            )
            # drop double vars (map files has priority)
            drop_vars = [v for v in ds_his.data_vars if v in self._results or v in drop]
            ds_his = ds_his.drop_vars(drop_vars)
            self.set_results(ds_his, split_dataset=True)

    def write_raster(
        self,
        variables=["grid", "states", "results.hmax"],
        root=None,
        driver="GTiff",
        compress="deflate",
        **kwargs,
    ):
        """Write model 2D raster variables to geotiff files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            Model variables are a combination of attribute and layer (optional) using <attribute>.<layer> syntax.
            Known ratster attributes are ["grid", "states", "results"].
            Different variables can be combined in a list.
            By default, variables is ["grid", "states", "results.hmax"]
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to hydromt.RasterDataset.to_raster(driver='GTiff', compress='lzw').
        """

        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write raster file(s) for {var} to 'gis' subfolder")
            layers = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for layer in layers:
                if layer not in obj:
                    self.logger.warning(f"Variable {attr}.{layer} not found: skipping.")
                    continue
                da = obj[layer]
                if len(da.dims) != 2 or "time" in da.dims:
                    continue
                if da.raster.res[1] > 0:  # make sure orientation is N->S
                    da = da.raster.flipud()
                da.raster.to_raster(
                    join(root, f"{layer}.tif"),
                    driver=driver,
                    compress=compress,
                    **kwargs,
                )

    def write_vector(
        self,
        variables=["geoms", "forcing.bzs", "forcing.dis"],
        root=None,
        **kwargs,
    ):
        """Write model vector (geoms) variables to geojson files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            geoms variables. By default all geoms are saved.
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to geopandas.GeoDataFrame.to_file(driver='GeoJSON').
        """
        kwargs.update(driver="GeoJSON")  # fixed
        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write vector file(s) for {var} to 'gis' subfolder")
            names = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for name in names:
                if name not in obj:
                    self.logger.warning(f"Variable {attr}.{name} not found: skipping.")
                    continue
                if isinstance(obj[name], gpd.GeoDataFrame):
                    gdf = obj[name]
                else:
                    try:
                        da = obj[name]
                        gdf = da.vector.to_gdf()
                        name = {f[0]: f[1] for f in self._FORCING.values()}[name]
                    except:
                        self.logger.debug(
                            f"Variable {attr}.{name} could not be written to vector file."
                        )
                        pass
                gdf.to_file(join(root, f"{name}.geojson"), **kwargs)

    def set_forcing_1d(
        self,
        name,
        ts: Union[xr.DataArray, pd.DataFrame, Dict[str, pd.DataFrame]] = None,
        xy: gpd.GeoDataFrame = None,
    ):
        """Set 1D forcing and update staticgoms and config accordingly.

        For waterlevel and discharge forcing point locations are required to set the
        combined src/dis and bnd/bzs files. If only point locations (and no timeseries)
        are given a dummy timeseries with zero values is set.

        If ts and xy are both None, the

        Parameters
        ----------
        name: {'waterlevel', 'discharge', 'precip'}
            Name of forcing type.
        ts: pandas.DataFrame, xarray.DataArray
            Timeseries data. If DataArray it should contain time and index dims; if
            DataFrame the index should be a datetime index and the columns the location
            index.
        xy: geopandas.GeoDataFrame
            Forcing point locations
        """
        ts_name, gname = self._FORCING_1D.get(name, (None, None))
        if ts_name is None:
            names = [f[0] for f in self._FORCING_1D.values() if "net" not in f[0]]
            raise ValueError(f'Unknown forcing "{name}", select from {names}')
        # sort out ts and xy types
        if isinstance(ts, (pd.DataFrame, pd.Series)):
            assert np.dtype(ts.index).type == np.datetime64
            ts.index.name = "time"
            if isinstance(ts, pd.DataFrame):
                ts.columns.name = "index"
                ts = xr.DataArray(ts, dims=("time", "index"), name=ts_name)
            else:  # spatially uniform forcing
                ts = xr.DataArray(ts, dims=("time"), name=ts_name)
        if isinstance(xy, gpd.GeoDataFrame):
            if ts is not None:
                ts = GeoDataArray.from_gdf(xy, ts, index_dim="index")
            else:
                ts = self._dummy_ts(xy, name, fill_value=0)  # dummy timeseries
            for c in xy.columns:
                if c in ["geometry", ts.vector.index_dim]:
                    continue
                ts[c] = xr.IndexVariable("index", xy[c].values)
        if not isinstance(ts, xr.DataArray):
            raise ValueError(
                f"{name} forcing: Unknown type for ts {type(ts)} should be xarray.DataArray."
            )
        # check if locations (bzs / dis)
        if gname is not None:
            assert len(ts.dims) == 2
            # make sure time is on last dim
            ts = ts.transpose(ts.vector.index_dim, ts.vector.time_dim)
            # set crs
            if ts.vector.crs is None:
                ts.vector.set_crs(self.crs.to_epsg())
            elif ts.vector.crs != self.crs:
                ts = ts.vector.to_crs(self.crs.to_epsg())
            # fix order based on x_dim after setting crs (for comparability between OS)
            ts = ts.sortby([ts.vector.x_dim, ts.vector.y_dim], ascending=True)
            # reset index
            dim = ts.vector.index_dim
            ts[dim] = xr.IndexVariable(dim, np.arange(1, ts[dim].size + 1, dtype=int))
            n = ts.vector.index.size
            self.logger.debug(f"{name} forcing: setting {gname} data for {n} points.")
        else:
            if not (len(ts.dims) == 1 and "time" in ts.dims):
                raise ValueError(
                    f"{name} forcing: uniform forcing should have single 'time' dimension."
                )

        # set forcing
        self.logger.debug(f"forcing: setting {name} data.")
        self.set_forcing(ts, name)

    ## model configuration

    def read_config(self, config_fn: str = "sfincs.inp", crs=None):
        if not os.path.isabs(config_fn) and self.root:
            config_fn = os.path.abspath(os.path.join(self.root, config_fn))
        else:
            self.root = os.path.dirname(config_fn)
        inp = SfincsInput()  # initialize with defaults
        if self._read and os.path.isfile(config_fn):
            inp.read(fn_inp=config_fn)
        self._config = inp.to_dict()
        self.update_grid_from_config()
        # update grid properties based on sfincs.inp

    def write_config(self, config_fn: str = "sfincs.inp"):
        """Write config to <root/config_fn>"""
        self._assert_write_mode

        inp = SfincsInput.from_dict(self.config)
        inp.write(fn_inp=os.path.join(self.root, config_fn))

        # for key, value in self.config.items():
        #     if key.endswith("file"):
        #         if not isfile(join(self.root, value)):
        #             value = basename(value)
        #             if isfile(join(self.root, rel_path, value)):
        #                 self.config.update({key: f"{rel_path}/{value}"})
        #             else:
        #                 self.logger.error(f"{key} = {value} not found")

    def update_spatial_attrs(self):
        """Update geospatial `config` (sfincs.inp) attributes based on grid"""
        dx, dy = self.res
        # TODO check self.bounds with rotation!! origin not necessary equal to total_bounds
        west, south, _, _ = self.bounds
        if self.crs is not None:
            self.set_config("crs", self.crs.to_epsg())
        self.set_config("mmax", self.width)
        self.set_config("nmax", self.height)
        self.set_config("dx", dx)
        self.set_config("dy", abs(dy))  # dy is always positive (orientation is S -> N)
        self.set_config("x0", west)
        self.set_config("y0", south)

    def update_grid_from_config(self):
        self.grid_type = (
            "quadtree" if self.config.get("qtrfile") is not None else "regular"
        )
        if self.grid_type == "regular":
            self.reggrid = RegularGrid(
                x0=self.config.get("x0"),
                y0=self.config.get("y0"),
                dx=self.config.get("dx"),
                dy=self.config.get("dy"),
                nmax=self.config.get("nmax"),
                mmax=self.config.get("mmax"),
                rotation=self.config.get("rotation", 0),
                crs=self.config.get("crs"),
            )
        else:
            pass
            # self.quadtree = QuadtreeGrid()

    def get_spatial_attrs(self, crs=None):
        """Get geospatial `config` (sfincs.inp) attributes.

        Parameters
        ----------
        crs: int, CRS
            Coordinate reference system

        Returns
        -------
        shape: tuple of int
            width, height
        transform: Affine.transform
            Geospatial transform
        crs: pyproj.CRS
            Coordinate reference system
        """
        return utils.get_spatial_attrs(self.config, crs=crs, logger=self.logger)

    def get_model_time(self):
        """Return (tstart, tstop) tuple with parsed model start and end time"""
        tstart = utils.parse_datetime(self.config["tstart"])
        tstop = utils.parse_datetime(self.config["tstop"])
        return tstart, tstop

    ## helper method

    def _dummy_ts(self, gdf, name, fill_value=0):
        df = pd.DataFrame(
            index=pd.DatetimeIndex(list(self.get_model_time())),
            columns=gdf.index.values,
            data=np.full((2, gdf.index.size), fill_value, dtype=np.float32),
        )
        ts = GeoDataArray.from_gdf(gdf, df, dims=("time", "index"), name=name)
        return ts
