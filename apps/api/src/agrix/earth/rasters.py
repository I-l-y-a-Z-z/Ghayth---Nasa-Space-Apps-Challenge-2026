"""Native-pixel, polygon-weighted raster extraction without cosmetic upsampling."""

import math
from datetime import date
from pathlib import Path

import numpy as np
from pyproj import CRS, Geod, Transformer
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform

from agrix.earth.records import ProviderError, RasterSummary

MAX_PIXELS = 1_000_000


def _projected_polygon(geometry: dict, crs):
    target = CRS.from_user_input(crs)
    polygon = shape(geometry)
    convert = Transformer.from_crs("EPSG:4326", target, always_xy=True).transform
    projected = transform(convert, polygon)
    center = polygon.representative_point()
    area_crs = CRS.from_proj4(
        f"+proj=laea +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m"
    )
    area_convert = Transformer.from_crs(target, area_crs, always_xy=True).transform
    return projected, area_convert


def _summarize(values, valid, affine, geometry, crs, *, extra_notes=None):
    polygon, area_convert = _projected_polygon(geometry, crs)
    full_area = transform(area_convert, polygon).area
    if full_area <= 0:
        raise ProviderError("INVALID_PLOT", "Plot has no area")
    valid_weight = covered_weight = total = 0.0
    minimum = maximum = None
    count = 0
    height, width = values.shape
    for row in range(height):
        for col in range(width):
            corners = [
                affine * (col, row),
                affine * (col + 1, row),
                affine * (col + 1, row + 1),
                affine * (col, row + 1),
            ]
            intersection = polygon.intersection(Polygon(corners))
            if intersection.is_empty:
                continue
            weight = transform(area_convert, intersection).area
            if weight <= 0:
                continue
            covered_weight += weight
            if not valid[row, col] or not np.isfinite(values[row, col]):
                continue
            value = float(values[row, col])
            count += 1
            valid_weight += weight
            total += value * weight
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
    if CRS.from_user_input(crs).is_geographic:
        lon, lat = affine * (0.5 * width, 0.5 * height)
        geod = Geod(ellps="WGS84")
        _, _, dx = geod.inv(lon, lat, lon + abs(affine.a), lat)
        _, _, dy = geod.inv(lon, lat, lon, lat + abs(affine.e))
        resolution = math.sqrt(abs(dx * dy))
    else:
        unit = CRS.from_user_input(crs).axis_info[0].unit_conversion_factor or 1
        resolution = math.sqrt(abs(affine.a * affine.e)) * unit
    covered = min(1.0, covered_weight / full_area)
    valid_fraction = min(1.0, valid_weight / full_area)
    notes = list(extra_notes or [])
    if covered < 0.999:
        notes.append("Raster does not cover the entire plot.")
    if valid_fraction < 0.999:
        notes.append("Nodata, quality masks or coverage gaps exclude part of the plot.")
    if count == 1:
        notes.append("Only one native pixel contributes; this is regional pixel context.")
    return RasterSummary(
        mean=total / valid_weight if valid_weight else None,
        minimum=minimum,
        maximum=maximum,
        valid_pixels=count,
        valid_fraction=valid_fraction,
        covered_fraction=covered,
        resolution_m=max(resolution, 0.001),
        notes=notes,
    )


def sample_raster(
    path: Path,
    geometry: dict,
    *,
    band: int = 1,
    scale: float | None = None,
    offset: float | None = None,
    calculation: str = "mean",
    source_unit: str | None = None,
    quality_path: Path | None = None,
    quality_valid_values: list[int] | None = None,
    quality_invalid_bits: int | None = None,
) -> RasterSummary:
    """Area-weight pixels intersecting a polygon, preserving nodata and native scale.

    Fractional intersection weights make a small plot within one pixel usable
    without pretending it has finer spatial information. DEM slope needs the
    neighboring native pixels and metre-corrected spacing.
    """
    try:
        import rasterio
        from rasterio.features import geometry_window
        from rasterio.windows import Window
    except ImportError as exc:
        raise ProviderError(
            "RASTER_DEPENDENCY_MISSING", "Install rasterio for raster analysis"
        ) from exc
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ProviderError("RASTER_CRS_MISSING", "Scientific raster requires a CRS")
        polygon, _ = _projected_polygon(geometry, source.crs)
        if not polygon.intersects(box(*source.bounds)):
            raise ProviderError("RASTER_NO_COVERAGE", "Raster does not overlap the plot")
        try:
            window = geometry_window(
                source,
                [polygon.__geo_interface__],
                pad_x=1 if calculation == "slope" else 0,
                pad_y=1 if calculation == "slope" else 0,
            )
        except rasterio.errors.WindowError as exc:
            raise ProviderError("RASTER_NO_COVERAGE", "Raster does not overlap the plot") from exc
        window = window.intersection(Window(0, 0, source.width, source.height))
        if window.width * window.height > MAX_PIXELS:
            raise ProviderError(
                "RASTER_WINDOW_TOO_LARGE", "Native raster window exceeds the processing limit"
            )
        raw = source.read(band, window=window, masked=True)
        values = (
            raw.filled(np.nan).astype(float)
            if np.issubdtype(raw.dtype, np.floating)
            else raw.astype(float).filled(np.nan)
        )
        valid = ~np.ma.getmaskarray(raw) & np.isfinite(values)
        actual_scale = source.scales[band - 1] if scale is None else scale
        actual_offset = source.offsets[band - 1] if offset is None else offset
        values = values * actual_scale + actual_offset
        affine = source.window_transform(window)
        notes = [
            "Native-pixel means weighted by plot/pixel intersection area; no resolution upsampling."
        ]
        if quality_path is not None:
            if quality_valid_values is None and quality_invalid_bits is None:
                raise ProviderError(
                    "QUALITY_RULE_REQUIRED",
                    "A quality raster requires explicit valid values or invalid flag bits",
                )
            with rasterio.open(quality_path) as quality:
                if (source.crs, source.transform, source.width, source.height) != (
                    quality.crs,
                    quality.transform,
                    quality.width,
                    quality.height,
                ):
                    raise ProviderError(
                        "QUALITY_GRID_MISMATCH",
                        "Scientific data and quality mask must share the native grid",
                    )
                flags = quality.read(1, window=window, masked=True)
                valid &= ~np.ma.getmaskarray(flags)
                if quality_valid_values is not None:
                    valid &= np.isin(flags.filled(0), quality_valid_values)
                if quality_invalid_bits is not None:
                    valid &= (flags.filled(0).astype(np.uint64) & quality_invalid_bits) == 0
            values[~valid] = np.nan
            notes.append(
                "Explicit registered native quality flags were applied before aggregation."
            )
        if calculation == "slope":
            if min(values.shape) < 2:
                raise ProviderError(
                    "DEM_NEIGHBORS_MISSING", "Slope requires neighboring elevation pixels"
                )
            unit = (source_unit or source.units[band - 1] or "").lower()
            if unit in {"m", "metre", "metres", "meter", "meters"}:
                pass
            elif unit in {"ft", "feet", "foot"}:
                values *= 0.3048
            elif unit in {"cm"}:
                values *= 0.01
            else:
                raise ProviderError(
                    "DEM_UNIT_MISSING",
                    "DEM elevation units must explicitly be metres, centimetres or feet",
                )
            if source.crs.is_geographic:
                lon, lat = affine * (0.5 * values.shape[1], 0.5 * values.shape[0])
                geod = Geod(ellps="WGS84")
                dx = abs(geod.inv(lon, lat, lon + abs(affine.a), lat)[2])
                dy = abs(geod.inv(lon, lat, lon, lat + abs(affine.e))[2])
            else:
                factor = CRS.from_user_input(source.crs).axis_info[0].unit_conversion_factor or 1
                dx, dy = abs(affine.a) * factor, abs(affine.e) * factor
            gradient_y, gradient_x = np.gradient(values, dy, dx)
            values = np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))
            valid &= np.isfinite(values)
            notes.append(
                "Slope in degrees from native elevation gradients with metric pixel spacing."
            )
        elif calculation != "mean":
            raise ValueError("Unsupported raster calculation")
        result = _summarize(values, valid, affine, geometry, source.crs, extra_notes=notes)
        result.scale, result.offset = float(actual_scale), float(actual_offset)
        return result


def sample_hls_ndvi(
    red_path: Path,
    nir_path: Path,
    quality_path: Path,
    geometry: dict,
    *,
    reflectance_scale: float = 0.0001,
    exclude_bits: int = 63,
) -> RasterSummary:
    """Use co-registered HLS red/NIR bands and Fmask; reject cloudy/snow pixels."""
    try:
        import rasterio
        from rasterio.features import geometry_window
    except ImportError as exc:
        raise ProviderError(
            "RASTER_DEPENDENCY_MISSING", "Install rasterio for HLS analysis"
        ) from exc
    with (
        rasterio.open(red_path) as red,
        rasterio.open(nir_path) as nir,
        rasterio.open(quality_path) as qa,
    ):

        def alignment(source):
            return source.crs, source.transform, source.width, source.height

        if alignment(red) != alignment(nir) or alignment(red) != alignment(qa):
            raise ProviderError(
                "HLS_GRID_MISMATCH", "HLS red, NIR and Fmask must share a native grid"
            )
        polygon, _ = _projected_polygon(geometry, red.crs)
        try:
            window = geometry_window(red, [polygon.__geo_interface__])
        except rasterio.errors.WindowError as exc:
            raise ProviderError(
                "RASTER_NO_COVERAGE", "HLS scene does not overlap the plot"
            ) from exc
        if window.width * window.height > MAX_PIXELS:
            raise ProviderError("RASTER_WINDOW_TOO_LARGE", "HLS window exceeds processing limit")
        r = red.read(1, window=window, masked=True).astype(float)
        n = nir.read(1, window=window, masked=True).astype(float)
        flags = qa.read(1, window=window, masked=True)
        rf, nf = r.filled(np.nan) * reflectance_scale, n.filled(np.nan) * reflectance_scale
        valid = (
            ~np.ma.getmaskarray(r)
            & ~np.ma.getmaskarray(n)
            & ~np.ma.getmaskarray(flags)
            & ((flags.filled(255).astype(np.uint16) & exclude_bits) == 0)
            & (flags.filled(255) != 255)
            & (rf >= 0)
            & (nf >= 0)
            & ((rf + nf) > 0)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            values = (nf - rf) / (nf + rf)
        valid &= np.isfinite(values) & (values >= -1) & (values <= 1)
        return _summarize(
            values,
            valid,
            red.window_transform(window),
            geometry,
            red.crs,
            extra_notes=[
                "HLS Fmask rejects cirrus, clouds, adjacent cloud, shadows, snow and water.",
                "Red/NIR reflectance scale is applied before NDVI; crop identity is not inferred.",
            ],
        )


def sample_netcdf(
    path: Path,
    geometry: dict,
    variable: str,
    *,
    day: date | None = None,
    start: date | None = None,
    end: date | None = None,
    latitude_name: str = "lat",
    longitude_name: str = "lon",
) -> RasterSummary:
    """Read a local CF latitude/longitude grid through xarray and preserve its native cells.

    Projected SMAP EASE-Grid assets should be exported to georeferenced GeoTIFF
    (EPSG:6933) through Harmony, rather than assigning latitude/longitude to x/y.
    """
    try:
        import xarray as xr
        from affine import Affine
    except ImportError as exc:
        raise ProviderError(
            "NETCDF_DEPENDENCY_MISSING", "Install xarray for NetCDF analysis"
        ) from exc
    with xr.open_dataset(path, mask_and_scale=True) as dataset:
        if variable not in dataset:
            raise ProviderError(
                "NETCDF_VARIABLE_MISSING", "Requested scientific variable is absent"
            )
        data = dataset[variable]
        source_scale, source_offset = (
            data.encoding.get("scale_factor", 1),
            data.encoding.get("add_offset", 0),
        )
        if day is not None and "time" in data.dims:
            data = data.sel(time=str(day))
        elif start is not None and end is not None and "time" in data.dims:
            data = data.sel(time=slice(start.isoformat(), end.isoformat() + "T23:59:59"))
        if "time" in data.dims and data.sizes["time"] == 0:
            raise ProviderError(
                "NETCDF_TIME_COVERAGE_MISSING", "No native timestamps in requested period"
            )
        if latitude_name not in data.dims or longitude_name not in data.dims:
            raise ProviderError(
                "NETCDF_GRID_UNSUPPORTED",
                "NetCDF adapter requires native regular latitude/longitude axes",
            )
        if np.any(data[longitude_name].values > 180):
            data = data.assign_coords({longitude_name: ((data[longitude_name] + 180) % 360) - 180})
        data = data.sortby(latitude_name, ascending=False).sortby(longitude_name)
        lat, lon = data[latitude_name].values, data[longitude_name].values
        if min(len(lat), len(lon)) < 2:
            raise ProviderError(
                "NETCDF_GRID_UNSUPPORTED", "At least two coordinates per native axis are required"
            )
        dy, dx = float(abs(lat[1] - lat[0])), float(abs(lon[1] - lon[0]))
        if not np.allclose(np.diff(lat), -dy) or not np.allclose(np.diff(lon), dx):
            raise ProviderError(
                "NETCDF_GRID_UNSUPPORTED", "Irregular grids require a georeferenced provider subset"
            )
        bounds = shape(geometry).bounds
        data = data.sel(
            {
                latitude_name: slice(bounds[3] + dy, bounds[1] - dy),
                longitude_name: slice(bounds[0] - dx, bounds[2] + dx),
            }
        )
        if data.size > MAX_PIXELS or data.size == 0:
            raise ProviderError(
                "NETCDF_NO_USABLE_WINDOW", "No bounded native NetCDF window overlaps the plot"
            )
        missing_temporal_values = bool(data.isnull().any().item())
        temporal_fraction = data.notnull().mean("time") if "time" in data.dims else None
        if "time" in data.dims:
            data = data.mean("time", skipna=True)
        data = data.transpose(latitude_name, longitude_name)
        values = np.asarray(data.values, dtype=float)
        affine = Affine(
            dx,
            0,
            float(data[longitude_name].values[0]) - dx / 2,
            0,
            -dy,
            float(data[latitude_name].values[0]) + dy / 2,
        )
        result = _summarize(
            values,
            np.isfinite(values),
            affine,
            geometry,
            "EPSG:4326",
            extra_notes=[
                "CF scale/fill decoding applied by xarray; native cells are area-weighted."
            ],
        )
        result.scale, result.offset = float(source_scale), float(source_offset)
        if missing_temporal_values:
            # Spatial coverage of a temporal mean does not prove all native
            # timestamps survived. Preserve that limitation as partial coverage.
            if temporal_fraction is not None:
                fractions = np.asarray(
                    temporal_fraction.transpose(latitude_name, longitude_name).values, dtype=float
                )
                fraction_summary = _summarize(
                    fractions, np.isfinite(fractions), affine, geometry, "EPSG:4326"
                )
                result.valid_fraction = min(
                    result.valid_fraction,
                    (fraction_summary.mean or 0) * fraction_summary.covered_fraction,
                )
            result.notes.append(
                "Some requested native time samples are missing; temporal means use surviving values."
            )
        return result
