import datetime
import hashlib
import logging
import os
import pathlib
import re
import shutil
from os.path import exists
from typing import Any, Dict, List
from typing import Callable
from typing import Optional
from typing import Tuple
from typing import Union
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
from dateutil import parser as dtparser

Timestamp = Union[
    str, float, datetime.datetime]  # RFC-3339 string or as a Unix timestamp in seconds
Duration = Union[
    str, float, int, datetime.timedelta]  # Prometheus duration string
Matrix = pd.DataFrame
Vector = pd.Series
Scalar = np.float64
String = str

# Get a logger object
logger = logging.getLogger(__name__)


class Prometheus:

    def __init__(self,
                 api_url: str,
                 headers: Dict = None,
                 cache_path: pathlib.Path = None):
        """
        Create a Prometheus Client.
        :param api_url: The URL to the Prometheus API endpoint.
        :param headers: Required headers for HTTP requests.
        """
        logger.debug(f'Creating prometheus query object for {api_url}')
        self.api_url = api_url
        self.headers = headers
        self._http = requests.Session()
        self._cache_path = cache_path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._http.close()

    def flush_cache(self) -> None:
        shutil.rmtree(self._cache_path, ignore_errors=True)

    def flush_query_cache(self, query: str) -> None:
        # used for generating filenames for cache
        # noinspection InsecureHash
        query_hash = hashlib.sha256(query.encode('utf-8')).hexdigest()
        os.remove(self._cache_path / f'{query_hash}.parquet')

    def query(self,
              query: str,
              labels: Optional[Dict] = None,
              time: Optional[Timestamp] = None,
              timeout: Optional[Duration] = None,
              sort: Optional[Callable[[Dict], Any]] = None) -> Union[
        Matrix, Vector, Scalar, String]:
        """
        Evaluates an instant query at a single point in time.

        :param labels:
        :param sort:
        :param query: Prometheus expression query string.
        :param labels: A dictionary of labels to add to each set of metric labels. Optional.
        :param time: Evaluation timestamp. Optional.
        :param timeout: Evaluation timeout. Optional.
        :param sort: A function passed that generates a sort from a metric dictionary. Optional.
        :return: Pandas DataFrame or Series.
        """
        params = {'query': query}

        if time is not None:
            params['time'] = to_ts(time)

        if timeout is not None:
            params['timeout'] = duration_to_s(timeout)

        results = self._do_query('api/v1/query', params)
        logger.debug(f'Received {len(results)} metrics')

        if sort:
            results = sorted(results, key=lambda r: sort(r['metric']))

        return self._to_pandas(results)

    def query_range(self,
                    query: str,
                    start: Timestamp,
                    end: Timestamp,
                    step: Duration,
                    labels: Optional[Dict] = None,
                    timeout: Optional[Duration] = None,
                    sort: Optional[Callable[[Dict], Any]] = None,
                    flush_cache: Optional[bool] = False) -> Matrix:
        """
        Evaluates an expression query over a range of time.

        :return: Pandas DataFrame.
        :param query: Prometheus expression query string.
        :param start: Start timestamp.
        :param end: End timestamp.
        :param step: Query resolution step width in `duration` format or float number of seconds.
        :param labels: A dictionary of labels to add to each set of metric labels
        :param timeout: Evaluation timeout. Optional.
        :param sort: A function passed that generates a sort from a metric dictionary. Optional.
        :param flush_cache: Flush the query cache before calling.
        """
        epoch_start = to_ts(start)
        epoch_end = to_ts(end)
        step_seconds = duration_to_s(step)
        params = {'query': query, 'start': epoch_start, 'end': epoch_end,
                  'step': step_seconds}

        if timeout is not None:
            params['timeout'] = duration_to_s(timeout)

        # used for generating filenames for cache
        # noinspection InsecureHash
        query_hash = hashlib.sha256(query.encode('utf-8')).hexdigest()

        # don't use a cache if no path is set
        if self._cache_path:
            # if the cache_path was configured, and flush_cache is True
            if flush_cache:
                self.flush_query_cache(query)
            # if the cache_path was configured, look there first
            if exists(self._cache_path):
                if exists(self._cache_path / f'{query_hash}.parquet'):
                    return pd.read_parquet(self._cache_path / f'{query_hash}.parquet')
            else:
                os.makedirs(self._cache_path)

        # get the data
        results = self._do_query('api/v1/query_range', params)
        metric_df = self._to_pandas(
                results,
                epoch_start,
                epoch_end,
                step_seconds,
                sort,
                labels)
        logger.debug(f'Received {len(results)} metrics')

        # make sure to write it if we're caching
        if self._cache_path:
            metric_df.to_parquet(
                    self._cache_path / f'{query_hash}.parquet',
                    use_deprecated_int96_timestamps=True
            )
        return metric_df

    def _do_query(self, path: str, params: Dict) -> Dict:
        resp = self._http.get(urljoin(self.api_url, path), headers=self.headers,
                              params=params)
        if resp.status_code not in [400, 422, 503]:
            resp.raise_for_status()

        response = resp.json()
        if response['status'] != 'success':
            raise RuntimeError('{errorType}: {error}'.format_map(response))

        return response['data']

    @classmethod
    def _to_pandas(cls, results: Dict, start: float = None, end: float = None,
                   step: float = None,
                   sort: Optional[Callable[[Dict], Any]] = None,
                   labels: Dict = None) -> Union[
        Matrix, Vector, Scalar, String]:
        result_type = results['resultType']

        """Convert Prometheus data object to Pandas object."""
        if sort:
            r = sorted(results['result'], key=lambda r: sort(r['metric']))
        else:
            r = results['result']

        if result_type == 'vector':
            return cls._numpy_to_series(
                    *cls._vector_to_numpy(r), labels=labels)
        elif result_type == 'matrix':
            return cls._numpy_to_dataframe(
                    *cls._matrix_to_numpy(r, start, end, step), labels=labels)
        elif result_type == 'scalar':
            return np.float64(r)
        elif result_type == 'string':
            return r
        else:
            raise ValueError('Unknown type: {}'.format(result_type))

    @classmethod
    def _vector_to_numpy(cls, results: dict) -> Tuple[np.ndarray, List]:
        """Take a list of results and turn it into a numpy array."""

        # Create the destination array and NaN fill for missing data
        data = np.zeros((len(results)), dtype=np.float64)
        data[:] = np.nan

        metrics = []

        for ii, t in enumerate(results):
            metric = t['metric']
            value = t['value'][1]

            # Insert the data while converting all the string values data into floating
            # point, simply using `float` works fine as it supports all the string
            # prometheus uses for special values
            data[ii] = np.float64(value)

            metrics.append(metric)

        return data, metrics

    @classmethod
    def _matrix_to_numpy(cls, results: dict, start: float, end: float,
                         step: float) -> Tuple[
        np.ndarray, List, np.ndarray]:
        """Take a list of results and turn it into a numpy array."""

        # Calculate the full range of timestamps we want data at. Add a small constant
        # to the end to make it inclusive if it lies exactly on an interval boundary
        times = np.arange(start, end + 1e-6, step)

        # Create the destination array and NaN fill for missing data
        data = np.zeros((len(results), len(times)), dtype=np.float64)
        data[:] = np.nan

        metrics = []

        for ii, t in enumerate(results):
            metric = t['metric']
            metric_times, values = zip(*t['values'])

            # This identifies which slots to insert the data into. Note that it relies
            # on the fact that Prometheus produces the same grid of samples as we do in
            # here. That should be fine, and we use `np.rint` to mitigate any possible
            # rounding issues, but it's worth noting.
            inds = np.rint((np.array(metric_times) - start) / step).astype(
                    np.int64)

            # Insert the data while converting all the string values data into floating
            # point, simply using `float` works fine as it supports all the string
            # prometheus uses for special values
            data[ii, inds] = [np.float64(v) for v in values]

            metrics.append(metric)

        return data, metrics, times

    @classmethod
    def _numpy_to_series(cls, data: np.ndarray, metrics: List,
                         labels: Dict = None) -> Vector:
        index = cls._metric_index(metrics, labels)
        return pd.Series(data, index=index)

    @classmethod
    def _numpy_to_dataframe(cls, data: np.ndarray, metrics: List,
                            times: np.ndarray, labels: Dict = None) -> Matrix:
        columns = cls._metric_index(metrics, labels)
        index = pd.Index(pd.to_datetime(times, unit='s'), name='timestamp')
        return pd.DataFrame(data.T, columns=columns, index=index)

    @classmethod
    def _metric_index(cls, metrics: List, labels: Dict = None) -> pd.MultiIndex:
        # Merge labels if they exist
        metrics_labels = _merge_metric_labels(metrics, labels)
        # Get the set of all the unique label names
        levels = set()
        for m in metrics_labels:
            if '__name__' in m.keys():
                m['metric_name'] = m.pop('__name__')
            levels |= set(m.keys())
        levels = sorted(list(levels))
        if len(levels) == 0:
            raise RuntimeError(
                    'Queries that are constructed as pandas df need to have at least one label category in the results')

        # Get the set of label values for each metric series and turn into a multilevel index
        mt = [tuple(m.get(level, None) for level in levels) for m in
              metrics_labels]
        index = pd.MultiIndex.from_tuples(mt, names=levels)
        return index


def _merge_metric_labels(metrics: List, labels: Dict) -> List:
    if labels:
        return [{**m, **labels} for m in metrics]

    return metrics


def to_ts(ts: Timestamp):
    """Convert a datetime or float to a UNIX timestamp.

    Parameters
    ----------
    ts
        If float assume it's already a UNIX timestamp, otherwise convert a
        datetime according to the usual Python rules.

    Returns
    -------
    timestamp
        UNIX timestamp.
    """
    if not isinstance(ts, (str, float, datetime.datetime)):
        raise TypeError(f'dt must be a float or datetime. Got {type(ts)}')

    if isinstance(ts, float):
        return ts

    if isinstance(ts, str):
        return dtparser.parse(ts).timestamp()

    return ts.timestamp()


def duration_to_s(duration: Duration) -> float:
    """Convert a Prometheus duration string to an interval in s.

    Parameters
    ----------
    duration
        If float or in it is assumed to be in seconds, and is passed through.
        If a str it is parsed according to prometheus rules.

    Returns
    -------
    seconds
        The number of seconds corresponding to the duration.
    """
    if not isinstance(duration, (str, float, int)):
        raise TypeError(f'Cannot convert {duration}.')

    if isinstance(duration, (float, int)):
        return float(duration)

    if isinstance(duration, datetime.timedelta):
        return duration.total_seconds()

    duration_codes = {
        'ms': 0.001,
        's': 1,
        'm': 60,
        'h': 60 * 60,
        'd': 24 * 60 * 60,
        'w': 7 * 24 * 60 * 60,
        'y': 365 * 24 * 60 * 60,
    }

    # a single time component
    pattern = f'(\\d+)({"|".join(duration_codes.keys())})'

    if not re.fullmatch(f'({pattern})+', duration):
        raise ValueError(f'Invalid format of duration string {duration}.')

    seconds = 0
    for match in re.finditer(pattern, duration):
        num = match.group(1)
        code = match.group(2)

        seconds += int(num) * duration_codes[code]

    return seconds


def ts_to_ms(ts: Timestamp):
    return int(round(to_ts(ts) * 1000))
