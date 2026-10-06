"""

import os
import json
import math
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# ── Network helpers ──────────────────────────────────────────────────────
USER_AGENT = 'Rain2Flood/3.0'


def _noaa_token():
    """
    The user's own NOAA CDO token, or None.

    QGIS settings first (survives restarts, per-profile), then the
    environment. Never a value baked into this file.
    """
    try:
        from qgis.core import QgsSettings
        v = QgsSettings().value('Rain2Flood/noaa_cdo_token', '')
        if v:
            return str(v).strip()
    except Exception:
        pass  # nosec B110 - best-effort step; failure must not abort the run
    return (os.environ.get('NOAA_CDO_TOKEN') or '').strip() or None


def _urlopen_https(req, timeout):
    """
    urlopen restricted to http/https.

    urllib will happily open file:// and other schemes, so a URL built
    from configuration or an API response could read local files instead
    of fetching. Every URL here is https today; the check keeps that true
    if one ever becomes a variable. (Bandit B310.)
    """
    url = req.full_url if hasattr(req, 'full_url') else str(req)
    if not url.lower().startswith(('http://', 'https://')):
        raise ValueError(
            f"Refusing to open a non-HTTP(S) URL: {url[:60]}")
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - scheme checked above




# ===========================================================================
# 1.  Open-Meteo  (ERA5-Land)  – Daily
# ===========================================================================
def download_openmeteo_data(lat, lon, start_date, end_date, feedback):
    """
    Download daily precipitation from Open-Meteo ERA5 archive.
    Completely free, no API key required.
    Returns DataFrame indexed by Date with column 'Rainfall (mm)'.
    """
    url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=precipitation_sum,rain_sum,snowfall_sum"
        f"&timezone=UTC"
    )
    feedback.pushInfo(f"Open-Meteo daily: {url}")
    try:
        req  = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with _urlopen_https(req, 60) as resp:
            data = json.loads(resp.read())
        if 'daily' not in data:
            raise ValueError(f"Unexpected response: {list(data.keys())}")
        daily = data['daily']
        df = pd.DataFrame({
            'Date':         pd.to_datetime(daily['time']),
            'Rainfall (mm)': pd.to_numeric(
                pd.Series(daily['precipitation_sum']), errors='coerce'
            ).fillna(0),
        })
        df.set_index('Date', inplace=True)
        df['Rainfall (mm)'] = df['Rainfall (mm)'].clip(lower=0)
        feedback.pushInfo(f"Open-Meteo: {len(df)} days, max={df['Rainfall (mm)'].max():.1f} mm")
        return df
    except Exception as e:
        feedback.reportError(f"Open-Meteo daily failed: {e}")
        return None


# ===========================================================================
# 2.  Open-Meteo – Hourly (ERA5-Land or CERRA)
# ===========================================================================
def download_openmeteo_hourly_data(lat, lon, start_date, end_date, feedback,
                                    model='era5'):
    """
    Download hourly precipitation from Open-Meteo.
    model: 'era5' (global) | 'cerra' (Europe ~5 km, 1984-2021)
    Returns DataFrame indexed by DateTime with column 'Rainfall (mm)'.
    """
    # Parse dates
    try:
        start_dt = datetime.strptime(start_date[:10], '%Y-%m-%d')
        end_dt   = datetime.strptime(end_date[:10],   '%Y-%m-%d')
    except Exception:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d %H:%M')
        end_dt   = datetime.strptime(end_date,   '%Y-%m-%d %H:%M')

    base = "https://archive-api.open-meteo.com/v1/archive"
    model_param = ""
    if model == 'cerra':
        base = "https://archive-api.open-meteo.com/v1/cerra"
        model_param = "&models=cerra"

    url = (
        f"{base}?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={start_dt.strftime('%Y-%m-%d')}"
        f"&end_date={end_dt.strftime('%Y-%m-%d')}"
        f"&hourly=precipitation{model_param}&timezone=UTC"
    )
    feedback.pushInfo(f"Open-Meteo hourly ({model}): {start_dt.date()} → {end_dt.date()}")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with _urlopen_https(req, 120) as resp:
            data = json.loads(resp.read())
        hourly = data['hourly']
        df = pd.DataFrame({
            'DateTime':      pd.to_datetime(hourly['time']),
            'Rainfall (mm)': pd.to_numeric(
                pd.Series(hourly['precipitation']), errors='coerce'
            ).fillna(0),
        })
        df.set_index('DateTime', inplace=True)
        df = df.loc[start_dt:end_dt]
        df['Rainfall (mm)'] = df['Rainfall (mm)'].clip(lower=0)
        feedback.pushInfo(f"Open-Meteo hourly: {len(df)} hours, max={df['Rainfall (mm)'].max():.2f} mm")
        return df
    except Exception as e:
        feedback.reportError(f"Open-Meteo hourly failed: {e}")
        return None


# ===========================================================================
# 3.  IMD (India Meteorological Department) via imdlib
# ===========================================================================
def download_imd_data(lat, lon, start_year, end_year, file_dir, feedback):
    """
    Download IMD gridded daily rainfall (0.25° grid) using imdlib.

    Downloads one year at a time, reuses valid local yearly files, and
    retries transient IMD server/network failures before moving on.
    """
    try:
        import imdlib as imd
    except ImportError:
        feedback.reportError("imdlib not installed. Run: pip install imdlib --break-system-packages")
        return None

    if not file_dir:
        file_dir = os.path.join(os.path.expanduser('~'), 'imd_data')
    os.makedirs(file_dir, exist_ok=True)

    try:
        import time

        feedback.pushInfo(
            f"IMD: downloading {start_year}–{end_year} year-by-year with retries …"
        )

        all_dfs = []
        max_attempts = 3

        for year in range(start_year, end_year + 1):
            year_success = False

            for attempt in range(1, max_attempts + 1):
                try:
                    feedback.pushInfo(
                        f"IMD: year {year} (attempt {attempt}/{max_attempts}) …"
                    )

                    # Reuse a valid local yearly IMD file when available.
                    data = None
                    try:
                        data = imd.open_data(
                            'rain', year, year, 'yearwise', file_dir=file_dir
                        )
                        feedback.pushInfo(
                            f"IMD: using existing local data for {year}"
                        )
                    except Exception:
                        feedback.pushInfo(
                            f"IMD: no usable local file for {year}; downloading …"
                        )
                        imd.get_data(
                            'rain', year, year, fn_format='yearwise', file_dir=file_dir
                        )
                        data = imd.open_data(
                            'rain', year, year, 'yearwise', file_dir=file_dir
                        )

                    ds = data.get_xarray()
                    ds = ds.where(ds['rain'] != -999.0)

                    point_data = ds.sel(
                        lat=lat, lon=lon, method='nearest'
                    )

                    df_year = (
                        point_data['rain']
                        .to_dataframe()
                        .reset_index()[['time', 'rain']]
                    )
                    df_year.columns = ['Date', 'Rainfall (mm)']

                    if df_year.empty:
                        raise ValueError(f"No rainfall records returned for {year}")

                    all_dfs.append(df_year)

                    feedback.pushInfo(
                        f"IMD: {year} downloaded successfully ({len(df_year)} records)"
                    )
                    year_success = True
                    break

                except Exception as e:
                    feedback.pushWarning(
                        f"IMD: year {year}, attempt {attempt}/{max_attempts} failed: {e}"
                    )

                    if attempt < max_attempts:
                        wait_seconds = 5 * attempt
                        feedback.pushInfo(
                            f"IMD: waiting {wait_seconds} seconds before retrying {year} …"
                        )
                        time.sleep(wait_seconds)

            if not year_success:
                feedback.pushWarning(
                    f"IMD: year {year} failed after {max_attempts} attempts. Continuing with next year."
                )

        if not all_dfs:
            feedback.reportError(
                "IMD: no yearly rainfall data could be downloaded."
            )
            return None

        df = pd.concat(all_dfs, ignore_index=True)
        df.set_index('Date', inplace=True)
        df.sort_index(inplace=True)
        df['Rainfall (mm)'] = (
            pd.to_numeric(df['Rainfall (mm)'], errors='coerce')
            .fillna(0)
            .clip(lower=0)
        )

        feedback.pushInfo(
            f"IMD: {len(df)} records, {df.index.min().date()} → "
            f"{df.index.max().date()}, max={df['Rainfall (mm)'].max():.1f} mm"
        )
        return df

    except Exception as e:
        feedback.reportError(f"IMD download failed: {e}")
        return None


# ===========================================================================
# 4.  CHIRPS – Climate Hazards Group InfraRed Precipitation
# ===========================================================================
def load_chirps_data(folder, lat, lon, start_year, end_year, feedback):
    """Load CHIRPS NetCDF files for a point location."""
    try:
        import xarray as xr
    except ImportError:
        feedback.reportError("xarray not installed.")
        return None

    if not os.path.exists(folder):
        feedback.reportError(f"CHIRPS folder not found: {folder}")
        return None

    nc_files = sorted([f for f in os.listdir(folder) if f.endswith('.nc')])
    if not nc_files:
        feedback.reportError("No .nc files in CHIRPS folder.")
        return None

    all_dfs = []
    for year in range(start_year, end_year + 1):
        yf = [f for f in nc_files if str(year) in f]
        if not yf:
            feedback.pushWarning(f"CHIRPS: no file for {year}")
            continue
        fpath = os.path.join(folder, yf[0])
        try:
            with xr.open_dataset(fpath) as ds:
                var_name = 'precip' if 'precip' in ds else list(ds.data_vars)[0]
                lat_name = 'latitude' if 'latitude' in ds.coords else 'lat'
                lon_name = 'longitude' if 'longitude' in ds.coords else 'lon'
                point = ds[var_name].sel(
                    {lat_name: lat, lon_name: lon}, method='nearest'
                )
                df_yr = point.to_dataframe().reset_index()[['time', var_name]]
                df_yr.columns = ['Date', 'Rainfall (mm)']
                all_dfs.append(df_yr)
        except Exception as e:
            feedback.pushWarning(f"CHIRPS {year}: {e}")

    if not all_dfs:
        feedback.reportError("No CHIRPS data loaded.")
        return None

    df = pd.concat(all_dfs)
    df.set_index('Date', inplace=True)
    df.sort_index(inplace=True)
    df['Rainfall (mm)'] = pd.to_numeric(df['Rainfall (mm)'], errors='coerce').fillna(0).clip(lower=0)
    feedback.pushInfo(f"CHIRPS: {len(df)} records, max={df['Rainfall (mm)'].max():.1f} mm")
    return df


# ===========================================================================
# 5.  NASA GPM IMERG  (tokenless CSV API, 0.1°, daily)
# ===========================================================================
GPM_BASE = "https://gpm.nasa.gov/api/v1"

def download_gpm_imerg_daily(lat, lon, start_date, end_date, feedback):
    """
    Download GPM IMERG Final Run daily data via NASA Giovanni-equivalent API.
    Uses the EARTHDATA public stats endpoint (no token needed for statistics).
    Falls back to Open-Meteo if unavailable.
    """
    # NASA POWER API (free, covers solar/meteo including precipitation)
    url = (
        f"https://power.larc.nasa.gov/api/temporal/daily/point?"
        f"parameters=PRECTOTCORR&community=AG"
        f"&longitude={lon:.4f}&latitude={lat:.4f}"
        f"&start={start_date.replace('-','')}&end={end_date.replace('-','')}"
        f"&format=JSON"
    )
    feedback.pushInfo(f"NASA POWER (GPM/IMERG proxy): {lat:.3f}°, {lon:.3f}°")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with _urlopen_https(req, 90) as resp:
            data = json.loads(resp.read())
        rain_dict = data['properties']['parameter']['PRECTOTCORR']
        dates  = [datetime.strptime(k, '%Y%m%d') for k in rain_dict.keys()]
        values = [max(0, float(v)) if v != -999 else 0.0 for v in rain_dict.values()]
        df = pd.DataFrame({'Date': dates, 'Rainfall (mm)': values})
        df.set_index('Date', inplace=True)
        feedback.pushInfo(f"NASA POWER: {len(df)} days, max={df['Rainfall (mm)'].max():.1f} mm")
        return df
    except Exception as e:
        feedback.pushWarning(f"NASA POWER failed ({e}). Trying Open-Meteo as fallback …")
        return download_openmeteo_data(lat, lon, start_date, end_date, feedback)


# ===========================================================================
# 6.  NOAA Global Summary of Day (GSD) – nearest station
# ===========================================================================
NOAA_GHCND_BASE = "https://www.ncei.noaa.gov/access/services/data/v1"

def download_noaa_ghcnd_nearest(lat, lon, start_date, end_date,
                                  max_stations=5, feedback=None):
    """
    Download NOAA GHCN-Daily precipitation for the nearest station to a point.
    No API key required for recent data.
    """
    # Step 1: find nearest stations via NCEI
    station_url = (
        f"https://www.ncei.noaa.gov/cdo-web/api/v2/stations?"
        f"datasetid=GHCND&datatypeid=PRCP"
        f"&extent={lat-2},{lon-2},{lat+2},{lon+2}"
        f"&limit=5&startdate={start_date}&enddate={end_date}"
    )
    # NOAA CDO requires a free personal token. One was previously HARDCODED
    # here and shipped in the published plugin — someone's real credential,
    # readable by anyone who downloaded it, rate-limited against them, and
    # revocable out from under every user at once. It has been removed; if
    # it was yours, revoke it at ncdc.noaa.gov/cdo-web/token.
    #
    # The token now comes from the user's own QGIS settings, or the
    # NOAA_CDO_TOKEN environment variable. Without one the request still
    # goes out untokenised and the caller's existing fallback handles the
    # refusal, which is what the original code claimed to do anyway.
    headers = {'User-Agent': USER_AGENT}
    token = _noaa_token()
    if token:
        headers['token'] = token
    elif feedback:
        feedback.pushInfo(
            "  NOAA CDO: no API token configured, so this request will "
            "likely be refused. Get a free token at "
            "ncdc.noaa.gov/cdo-web/token and set it in QGIS under "
            "Settings > Options > Advanced > Rain2Flood > noaa_cdo_token, "
            "or in the NOAA_CDO_TOKEN environment variable.")
    try:
        req = urllib.request.Request(station_url, headers=headers)
        with _urlopen_https(req, 30) as resp:
            st_data = json.loads(resp.read())
        stations = st_data.get('results', [])
        if not stations:
            raise ValueError("No stations found.")
        station_id = stations[0]['id']
        feedback.pushInfo(f"NOAA GHCND: nearest station {station_id}")

        data_url = (
            f"{NOAA_GHCND_BASE}?dataset=daily-summaries"
            f"&stations={station_id}&startDate={start_date}&endDate={end_date}"
            f"&dataTypes=PRCP&format=json&units=metric"
        )
        req2 = urllib.request.Request(data_url, headers={'User-Agent': USER_AGENT})
        with _urlopen_https(req2, 60) as resp2:
            raw = json.loads(resp2.read())
        records = raw if isinstance(raw, list) else raw.get('results', [])
        df = pd.DataFrame(records)
        if df.empty:
            raise ValueError("No data records.")
        df['date'] = pd.to_datetime(df['DATE'])
        df['Rainfall (mm)'] = pd.to_numeric(df['PRCP'], errors='coerce').fillna(0)
        df.set_index('date', inplace=True)
        df = df[['Rainfall (mm)']]
        feedback.pushInfo(f"NOAA GHCND: {len(df)} days, max={df['Rainfall (mm)'].max():.1f} mm")
        return df
    except Exception as e:
        if feedback:
            feedback.pushInfo(
                f"  NOAA GHCND unavailable ({e}) — this is expected from "
                f"time to time: NOAA's station-lookup API needs a personal "
                f"access token, and this plugin can't ship one that works "
                f"reliably for every installation (a single shared token "
                f"gets rate-limited across all users of a public plugin). "
                f"Falling back to Open-Meteo (no token needed) automatically."
            )
        return download_openmeteo_data(lat, lon, start_date, end_date, feedback)


# ===========================================================================
# 7.  Excel / CSV Loader  (generic)
# ===========================================================================
def load_excel_rainfall_data(file_path, date_column, rainfall_column, feedback):
    """
    Load daily or hourly rainfall from Excel (.xlsx/.xls) or CSV.
    Auto-detects format from extension.
    Returns DataFrame indexed by Date/DateTime with column 'Rainfall (mm)'.
    """
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.xlsx', '.xls'):
            df = pd.read_excel(file_path)
        elif ext in ('.csv', '.txt'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_csv(file_path)

        if date_column not in df.columns:
            raise ValueError(f"Column '{date_column}' not found. Available: {list(df.columns)}")
        if rainfall_column not in df.columns:
            raise ValueError(f"Column '{rainfall_column}' not found. Available: {list(df.columns)}")

        result = pd.DataFrame({
            'Date':          pd.to_datetime(df[date_column], infer_datetime_format=True),
            'Rainfall (mm)': pd.to_numeric(df[rainfall_column], errors='coerce'),
        })
        result.set_index('Date', inplace=True)
        result.sort_index(inplace=True)
        result['Rainfall (mm)'] = result['Rainfall (mm)'].fillna(0).clip(lower=0)
        result.replace([np.inf, -np.inf], 0, inplace=True)

        feedback.pushInfo(
            f"Excel/CSV: {len(result)} records, "
            f"{result.index.min().date()} → {result.index.max().date()}, "
            f"max={result['Rainfall (mm)'].max():.1f} mm"
        )
        return result
    except Exception as e:
        feedback.reportError(f"File loading failed: {e}")
        return None


# ===========================================================================
# 8.  Statistical / Synthetic Rainfall Generation
# ===========================================================================
def generate_synthetic_rainfall(mean_annual_mm, cv=0.5, n_years=30,
                                 seed=42, feedback=None):
    """
    Generate synthetic annual maximum daily rainfall series using
    a log-normal distribution (simple planning tool when no data available).

    Parameters
    ----------
    mean_annual_mm : float  – mean annual maximum daily rainfall (mm)
    cv             : float  – coefficient of variation (default 0.5)
    n_years        : int    – number of synthetic years
    seed           : int    – random seed for reproducibility

    Returns
    -------
    DataFrame with 'Date' index and 'Rainfall (mm)' column (daily, with
    synthetic peaks distributed uniformly across years).
    """
    rng = np.random.default_rng(seed)
    sigma_log = np.sqrt(np.log(1 + cv**2))
    mu_log    = np.log(mean_annual_mm) - 0.5 * sigma_log**2
    ann_max   = rng.lognormal(mu_log, sigma_log, n_years)

    # Expand to daily – uniform background with one peak per year
    dates, values = [], []
    base_year = 1990
    for i, peak in enumerate(ann_max):
        yr   = base_year + i
        peak_day = rng.integers(1, 365)
        for day in range(1, 366):
            try:
                date = datetime(yr, 1, 1) + timedelta(days=day - 1)
            except ValueError:
                continue
            rain = peak if day == peak_day else rng.exponential(1.5)
            dates.append(date)
            values.append(max(0, rain))

    df = pd.DataFrame({'Rainfall (mm)': values}, index=pd.DatetimeIndex(dates))
    df.sort_index(inplace=True)
    if feedback:
        feedback.pushInfo(
            f"Synthetic rainfall: {n_years} years, "
            f"mean annual max={ann_max.mean():.1f} mm (CV={cv:.2f})"
        )
    return df


# ===========================================================================
# 9.  Rainfall Data Quality Control
# ===========================================================================
def quality_control_rainfall(df, min_years=5, max_gap_days=30,
                               upper_cap_mm=1500, feedback=None):
    """
    Apply standard quality-control checks to a daily rainfall DataFrame.

    Checks:
      1. Minimum record length
      2. Maximum gap detection
      3. Physical upper cap (global record ~1825 mm/day, Reunion Island)
      4. Negative value removal
      5. Percentage of zero days (flag if > 95%)

    Returns
    -------
    Cleaned DataFrame + QC report dict.
    """
    report = {}
    df = df.copy()
    df['Rainfall (mm)'] = pd.to_numeric(df['Rainfall (mm)'], errors='coerce')

    # 1. Remove negatives
    neg_count = (df['Rainfall (mm)'] < 0).sum()
    df['Rainfall (mm)'] = df['Rainfall (mm)'].clip(lower=0)
    report['negatives_removed'] = int(neg_count)

    # 2. Physical cap
    high_count = (df['Rainfall (mm)'] > upper_cap_mm).sum()
    df.loc[df['Rainfall (mm)'] > upper_cap_mm, 'Rainfall (mm)'] = np.nan
    report['values_capped'] = int(high_count)

    # 3. Fill NaN with 0 (conservative – better than interpolation for extremes)
    nan_count = df['Rainfall (mm)'].isna().sum()
    df['Rainfall (mm)'].fillna(0, inplace=True)
    report['nan_filled'] = int(nan_count)

    # 4. Record length
    n_years = (df.index.max() - df.index.min()).days / 365.25
    report['record_length_years'] = round(n_years, 1)
    if n_years < min_years:
        report['warning'] = f"Record length {n_years:.1f} yr < recommended {min_years} yr."
        if feedback:
            feedback.pushWarning(report['warning'])

    # 5. Zero-day percentage
    zero_pct = (df['Rainfall (mm)'] == 0).mean() * 100
    report['zero_day_pct'] = round(zero_pct, 1)
    if zero_pct > 95:
        report['warning_zeros'] = f"{zero_pct:.1f}% of days have zero rainfall – check data."
        if feedback:
            feedback.pushWarning(report.get('warning_zeros', ''))

    # 6. Maximum gaps
    idx = df.index
    if len(idx) > 1:
        gaps = (idx[1:] - idx[:-1]).days
        max_gap = int(gaps.max())
        report['max_gap_days'] = max_gap
        if max_gap > max_gap_days:
            if feedback:
                feedback.pushWarning(f"Data gap of {max_gap} days detected.")

    if feedback:
        feedback.pushInfo(
            f"QC: {len(df)} records, {n_years:.1f} years, "
            f"zero={zero_pct:.1f}%, neg={neg_count}, capped={high_count}"
        )
    return df, report