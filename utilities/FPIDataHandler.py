"""
FPIDataHandler.py — Load FPI result .npz files into a tidy xarray Dataset.

Primary entry point: load_fpi_data(site, date, emission, ...)

Dataset layout
--------------
Dimension : ``time``
    A sorted, continuous datetime index of all observations across all
    loaded nights.  FPI observations are interleaved sequentially by
    direction (Zenith → North → East → Zenith → …) within each night, so
    the times for different directions are never perfectly aligned.  A
    2-D (time × direction) grid would therefore be extremely sparse; the
    "long/tidy" layout (one row per observation, direction stored as a
    non-dimension coordinate) avoids that waste and lets callers filter
    trivially with ``ds.where(ds.direction == 'North', drop=True)``.

Non-dimension coordinate : ``direction``
    String label for each observation's look direction.

Variables
---------
LOSwind, sigma_LOSwind : raw line-of-sight wind and its uncertainty [m/s]
T, sigma_T             : temperature and uncertainty [K]
skyI, sigma_skyI       : sky intensity and uncertainty [arb]
ze                     : zenith angle [deg]
ref_Dop                : Doppler reference shift computed at load time [m/s];
                         corrected wind = LOSwind - ref_Dop
cloud_mean             : cloud-sensor reading [°C]; NaN when not available

Dataset attributes
------------------
site, emission, date_range, reference
"""

import os
import datetime
import numpy as np
import pandas as pd
import xarray as xr

import airglow.fpiinfo as fpiinfo
import airglow.FPI as FPI


# ── Constants ─────────────────────────────────────────────────────────────────

_EMISSION_SUFFIX = {'green': 'xg', 'red': 'xr'}
_DEFAULT_DIRECTIONS = ['North', 'South', 'East', 'West', 'Zenith']


# ── Private helpers ───────────────────────────────────────────────────────────

def _normalize_date(d):
    """Coerce str / datetime.datetime / datetime.date → datetime.date."""
    if isinstance(d, str):
        return datetime.date.fromisoformat(d)
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    raise TypeError(f"Cannot normalize date: {d!r}")


def _iter_dates(date):
    """Yield datetime.date objects for a single date or (start, end) range."""
    if isinstance(date, tuple):
        start, end = _normalize_date(date[0]), _normalize_date(date[1])
        d = start
        while d <= end:
            yield d
            d += datetime.timedelta(days=1)
    else:
        yield _normalize_date(date)


def _resolve_instrument(site, date, instrument_override):
    """
    Return the instrument name at *site* on *date*.

    Raises ValueError if multiple instruments are simultaneously deployed
    and no override is provided — the caller must specify ``instrument=``.
    Returns None if no instrument is found at the site on that date.
    """
    if instrument_override is not None:
        return instrument_override
    dt = datetime.datetime(date.year, date.month, date.day)
    instrs = fpiinfo.get_instr_at(site, dt)
    if len(instrs) == 0:
        return None
    if len(instrs) > 1:
        raise ValueError(
            f"Multiple instruments found at site '{site}' on {date}: "
            f"{instrs}.  Pass instrument=<name> to select one."
        )
    return instrs[0]


def _strip_tz(dt):
    """
    Return a tz-naive datetime preserving local wall-clock time.

    FPI sky_times may be tz-aware local-time datetimes.  pandas/xarray
    would convert those to UTC when building a DatetimeIndex, changing
    the .hour value that _lt_hour() (in FPIDisplayNew) relies on.
    Stripping the tzinfo here preserves the local-time hour digits.
    """
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


# ── Public API ────────────────────────────────────────────────────────────────

def load_fpi_data(site, date, emission, *,
                  directions=None,
                  instrument=None,
                  cloud_storage=None,
                  temp_dir=None,
                  files=None,
                  reference='zenith',
                  verbose=False):
    """
    Load FPI result .npz files into a tidy xarray Dataset.

    Parameters
    ----------
    site : str
        Site abbreviation, e.g. ``'uao'``.
    date : datetime.date, str, or tuple of two dates
        Single date or ``(start, end)`` inclusive range.  May be ``None``
        when *files* is supplied (date_range attribute is inferred from data).
    emission : {'green', 'red'}
        Emission line; mapped internally to ``'xg'`` or ``'xr'``.
    directions : list of str, optional
        Direction names to retain.  Default: all five standard directions.
    instrument : str, optional
        Instrument name override.  When ``None`` (default), the instrument
        is resolved per date via ``fpiinfo.get_instr_at()``.  Required
        when multiple instruments are simultaneously deployed at a site.
    cloud_storage : CloudStorage instance, optional
        When provided, files are downloaded from cloud object storage.
        Mutually exclusive with *files*.
    temp_dir : str, optional
        Temporary directory for cloud downloads.  Falls back to
        ``cloud_storage.config.temp_dir`` when ``None``.
    files : list of str, optional
        Explicit local file paths.  When supplied, *cloud_storage* is
        ignored and no file-discovery is performed.  Useful when data
        have been mirrored to a local directory.
    reference : {'zenith', 'laser'}
        Doppler reference mode passed to ``FPI.DopplerReference``.  The
        per-observation reference shift is stored as ``ref_Dop`` in the
        Dataset so DataSummary can compute corrected winds without
        repeating this step.
    verbose : bool
        Log skipped files and progress when ``True``.

    Returns
    -------
    xr.Dataset
        See module docstring for full variable and attribute description.
        Returns an empty Dataset (no ``time`` dimension) when no data
        could be loaded.
    """
    # ── Validate emission ──────────────────────────────────────────────────
    if emission not in _EMISSION_SUFFIX:
        raise ValueError(f"emission must be 'green' or 'red', got {emission!r}")
    emission_suffix = _EMISSION_SUFFIX[emission]

    if directions is None:
        directions = _DEFAULT_DIRECTIONS

    # ── Build (path, is_cloud) task list ──────────────────────────────────
    if files is not None:
        # Local file list — load directly with np.load.
        file_tasks = [(f, False, None) for f in files]
        dates_list = list(_iter_dates(date)) if date is not None else []
        date_range_str = (
            f"{dates_list[0]} to {dates_list[-1]}"
            if dates_list else 'inferred from data'
        )

    elif cloud_storage is not None:
        td = temp_dir if temp_dir is not None else cloud_storage.config.temp_dir
        dates_list = list(_iter_dates(date))
        date_range_str = f"{dates_list[0]} to {dates_list[-1]}"
        file_tasks = []
        for d in dates_list:
            instr = _resolve_instrument(site, d, instrument)
            if instr is None:
                if verbose:
                    print(
                        f"[FPIDataHandler] No instrument at '{site}' on {d},"
                        " skipping."
                    )
                continue
            key = (
                f"results/{d.year}/"
                f"{instr}_{site}_{d.strftime('%Y%m%d')}_{emission_suffix}.npz"
            )
            if not cloud_storage.list_objects(key[:-1]):
                if verbose:
                    print(f"[FPIDataHandler] Skipped (not on cloud): {key}")
                continue
            file_tasks.append((key, True, td))

    else:
        raise ValueError(
            "Provide either cloud_storage (for cloud access) "
            "or files (for local access)."
        )

    # ── Accumulate observations ────────────────────────────────────────────
    acc_times         = []
    acc_dirs          = []
    acc_LOSwind       = []
    acc_sigma_LOSwind = []
    acc_T             = []
    acc_sigma_T       = []
    acc_skyI          = []
    acc_sigma_skyI    = []
    acc_ze            = []
    acc_ref_Dop       = []
    acc_cloud_mean    = []

    for path, is_cloud, td in file_tasks:
        local_path = None
        try:
            # ── Obtain local copy of the file ─────────────────────────────
            if is_cloud:
                local_path = os.path.join(td, os.path.basename(path))
                if not cloud_storage.download_file(path, local_path):
                    if verbose:
                        print(f"[FPIDataHandler] Skipped (download failed): {path}")
                    continue
                load_path = local_path
            else:
                load_path = path
                if not os.path.exists(load_path):
                    if verbose:
                        print(f"[FPIDataHandler] Skipped (not found): {load_path}")
                    continue

            # ── Load npz ──────────────────────────────────────────────────
            npz = np.load(load_path, allow_pickle=True, encoding='latin1')
            FPI_Results = npz['FPI_Results'].reshape(-1)[0]
            npz.close()

            sky_times = FPI_Results['sky_times']
            dir_arr   = np.array(FPI_Results['direction'])

            # Direction filter
            dir_mask = np.isin(dir_arr, directions)
            if not dir_mask.any():
                continue

            # Doppler reference shift for every observation in this file
            ref_Dop, _ = FPI.DopplerReference(FPI_Results, reference=reference)

            # Cloud sensor (NaN when instrument has no cloud sensor)
            has_clouds = (
                'Clouds' in FPI_Results
                and FPI_Results['Clouds'] is not None
            )
            if has_clouds:
                cloud_mean_arr = np.asarray(
                    FPI_Results['Clouds']['mean'], dtype=float
                )
            else:
                cloud_mean_arr = np.full(len(sky_times), np.nan)

            # ── Append to accumulators (selected directions only) ──────────
            idx = np.where(dir_mask)[0]

            # Strip timezone so that xarray stores local wall-clock hours;
            # _lt_hour() in FPIDisplayNew relies on .hour being local time.
            acc_times.extend([_strip_tz(sky_times[i]) for i in idx])
            acc_dirs.extend(dir_arr[idx])
            acc_LOSwind.extend(
                np.asarray(FPI_Results['LOSwind'],       dtype=float)[idx])
            acc_sigma_LOSwind.extend(
                np.asarray(FPI_Results['sigma_LOSwind'], dtype=float)[idx])
            acc_T.extend(
                np.asarray(FPI_Results['T'],             dtype=float)[idx])
            acc_sigma_T.extend(
                np.asarray(FPI_Results['sigma_T'],       dtype=float)[idx])
            acc_skyI.extend(
                np.asarray(FPI_Results['skyI'],          dtype=float)[idx])
            acc_sigma_skyI.extend(
                np.asarray(FPI_Results['sigma_skyI'],    dtype=float)[idx])
            acc_ze.extend(
                np.asarray(FPI_Results['ze'],            dtype=float)[idx])
            acc_ref_Dop.extend(
                np.asarray(ref_Dop,                      dtype=float)[idx])
            acc_cloud_mean.extend(cloud_mean_arr[idx])

            if verbose:
                print(
                    f"[FPIDataHandler] Loaded {idx.size} obs from {path}"
                )

        except FileNotFoundError:
            if verbose:
                print(f"[FPIDataHandler] Skipped (not found): {path}")
        except Exception as exc:
            if verbose:
                print(f"[FPIDataHandler] Error loading {path}: {exc}")
        finally:
            # Clean up temporary cloud download
            if is_cloud and local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass

    # ── Build xarray Dataset ───────────────────────────────────────────────
    _empty_attrs = {
        'site':       site,
        'emission':   emission_suffix,
        'date_range': date_range_str,
        'reference':  reference,
    }

    if not acc_times:
        return xr.Dataset(attrs=_empty_attrs)

    # Sort chronologically so the time dimension is a continuous axis.
    # Files are loaded in date order and observations within a file are
    # already time-ordered, so this sort is mostly a sanity check.
    times_pd  = pd.DatetimeIndex(acc_times)
    sort_idx  = times_pd.argsort()

    def _sorted(lst):
        return np.array(lst)[sort_idx]

    ds = xr.Dataset(
        {
            'LOSwind':       ('time', _sorted(acc_LOSwind).astype(float)),
            'sigma_LOSwind': ('time', _sorted(acc_sigma_LOSwind).astype(float)),
            'T':             ('time', _sorted(acc_T).astype(float)),
            'sigma_T':       ('time', _sorted(acc_sigma_T).astype(float)),
            'skyI':          ('time', _sorted(acc_skyI).astype(float)),
            'sigma_skyI':    ('time', _sorted(acc_sigma_skyI).astype(float)),
            'ze':            ('time', _sorted(acc_ze).astype(float)),
            'ref_Dop':       ('time', _sorted(acc_ref_Dop).astype(float)),
            'cloud_mean':    ('time', _sorted(acc_cloud_mean).astype(float)),
        },
        coords={
            'time':      times_pd[sort_idx],
            'direction': ('time', _sorted(acc_dirs)),
        },
        attrs=_empty_attrs,
    )

    # Attach units as variable attributes for documentation
    ds['LOSwind'].attrs['units']       = 'm/s'
    ds['sigma_LOSwind'].attrs['units'] = 'm/s'
    ds['T'].attrs['units']             = 'K'
    ds['sigma_T'].attrs['units']       = 'K'
    ds['ze'].attrs['units']            = 'deg'
    ds['ref_Dop'].attrs['units']       = 'm/s'
    ds['cloud_mean'].attrs['units']    = 'degC'

    return ds


# ── Demo / smoke-test ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    # ── Configuration — edit to match your environment ────────────────────
    SITE      = 'uao'
    EMISSION  = 'red'
    DATE_SINGLE = datetime.date(2023, 3, 15)
    DATE_RANGE  = (datetime.date(2023, 3, 1), datetime.date(2023, 3, 31))

    # ── Try to set up cloud storage (requires .env with AWS credentials) ──
    try:
        from airglow.cloud_storage import CloudStorage, Configuration
        config = Configuration()
        cs     = CloudStorage(config)
        print("Cloud storage initialised.")
        use_cloud = True
    except Exception as exc:
        print(f"Cloud storage not available ({exc}).")
        print("Edit the LOCAL_FILES list below for a local-file demo.")
        use_cloud = False

    # ── Local-file fallback demo ──────────────────────────────────────────
    LOCAL_FILES = [
        # '/rdata/airglow/fpi/results/2023/minime05_uao_20230315_xr.npz',
    ]

    if use_cloud:
        # ── Example 1: single night ───────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"Example 1: single night  {DATE_SINGLE}  site={SITE}  {EMISSION}")
        print('='*60)
        ds1 = load_fpi_data(SITE, DATE_SINGLE, EMISSION,
                            cloud_storage=cs, verbose=True)
        print(ds1)

        # ── Example 2: date range ─────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"Example 2: date range  {DATE_RANGE[0]} – {DATE_RANGE[1]}")
        print('='*60)
        ds2 = load_fpi_data(SITE, DATE_RANGE, EMISSION,
                            cloud_storage=cs, verbose=True)
        print(ds2)

        # ── Example 3: end-to-end with FPIDisplayNew.DataSummary ─────────
        print(f"\n{'='*60}")
        print("Example 3: passing Dataset directly to DataSummary")
        print('='*60)
        if ds2.dims.get('time', 0) > 0:
            import airglow.FPIDisplayNew as FPIDisplayNew
            import matplotlib.pyplot as plt
            results = FPIDisplayNew.DataSummary(ds2, variables=['T', 'U', 'V'])
            print(f"DataSummary returned figures for: {list(results.keys())}")
            for var, (fig, _ax) in results.items():
                fname = f'test_datasummary_{SITE}_{var}.png'
                fig.savefig(fname, dpi=100)
                print(f"  saved {fname}")
            plt.close('all')
        else:
            print("  (no data loaded — skipping plot)")

    elif LOCAL_FILES:
        print(f"\n{'='*60}")
        print("Local-file demo")
        print('='*60)
        ds_local = load_fpi_data(SITE, None, EMISSION,
                                 files=LOCAL_FILES, verbose=True)
        print(ds_local)
    else:
        print("\nNo cloud credentials and no LOCAL_FILES configured.")
        print("Set up a .env file or populate LOCAL_FILES in the __main__ block.")
        sys.exit(0)

    print("\nDone.")
