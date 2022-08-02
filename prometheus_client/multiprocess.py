from __future__ import unicode_literals

from collections import defaultdict
import glob
import json
import os
import warnings

from .metrics_core import Metric
from .mmap_dict import MmapedDict
from .samples import Sample
from .utils import floatToGoString

try:  # Python3
    FileNotFoundError
except NameError:  # Python >= 2.5
    FileNotFoundError = IOError

MP_METRIC_HELP = 'Multiprocess metric'


class MultiProcessCollector(object):
    """Collector for files for multi-process mode."""

    def __init__(self, registry, path=None):
        if path is None:
            # This deprecation warning can go away in a few releases when removing the compatibility
            if 'prometheus_multiproc_dir' in os.environ and 'PROMETHEUS_MULTIPROC_DIR' not in os.environ:
                os.environ['PROMETHEUS_MULTIPROC_DIR'] = os.environ['prometheus_multiproc_dir']
                warnings.warn("prometheus_multiproc_dir variable has been deprecated in favor of the upper case naming PROMETHEUS_MULTIPROC_DIR", DeprecationWarning)
            path = os.environ.get('PROMETHEUS_MULTIPROC_DIR')
        if not path or not os.path.isdir(path):
            raise ValueError('env PROMETHEUS_MULTIPROC_DIR is not set or not a directory')
        self._path = path
        if registry:
            registry.register(self)

    @staticmethod
    def merge(files, accumulate=True):
        """Merge metrics from given mmap files.

        By default, histograms are accumulated, as per prometheus wire format.
        But if writing the merged data back to mmap files, use
        accumulate=False to avoid compound accumulation.
        """
        metrics = MultiProcessCollector._read_metrics(files)
        return MultiProcessCollector._accumulate_metrics(metrics, accumulate)

    @staticmethod
    def _read_metrics(files):
        metrics = {}
        key_cache = {}

        def _parse_key(key):
            val = key_cache.get(key)
            if not val:
                metric_name, name, labels = json.loads(key)
                labels_key = tuple(sorted(labels.items()))
                val = key_cache[key] = (metric_name, name, labels, labels_key)
            return val

        for f in files:
            parts = os.path.basename(f).split('_')
            typ = parts[0]
            try:
                file_values = MmapedDict.read_all_values_from_file(f)
            except FileNotFoundError:
                if typ == 'gauge' and parts[1] in ('liveall', 'livesum'):
                    # Those files can disappear between the glob of collect
                    # and now (via a mark_process_dead call) so don't fail if
                    # the file is missing
                    continue
                raise
            for key, value, _ in file_values:
                metric_name, name, labels, labels_key = _parse_key(key)

                metric = metrics.get(metric_name)
                if metric is None:
                    metric = Metric(metric_name, MP_METRIC_HELP, typ)
                    metrics[metric_name] = metric

                if typ == 'gauge':
                    pid = parts[2][:-3]
                    metric._multiprocess_mode = parts[1]
                    metric.add_sample(name, labels_key + (('pid', pid),), value)
                else:
                    # The duplicates and labels are fixed in the next for.
                    metric.add_sample(name, labels_key, value)
        return metrics

    @staticmethod
    def _accumulate_metrics(metrics, accumulate):
        for metric in metrics.values():
            samples = defaultdict(float)
            buckets = defaultdict(lambda: defaultdict(float))
            samples_setdefault = samples.setdefault
            for s in metric.samples:
                name, labels, value, timestamp, exemplar = s
                if metric.type == 'gauge':
                    without_pid_key = name, tuple(l for l in labels if l[0] != 'pid')
                    if metric._multiprocess_mode == 'min':
                        current = samples_setdefault(without_pid_key, value)
                        if value < current:
                            samples[without_pid_key] = value
                    elif metric._multiprocess_mode == 'max':
                        current = samples_setdefault(without_pid_key, value)
                        if value > current:
                            samples[without_pid_key] = value
                    elif metric._multiprocess_mode == 'livesum':
                        samples[without_pid_key] += value
                    else:  # all/liveall
                        samples[(name, labels)] = value

                elif metric.type == 'histogram':
                    # A for loop with early exit is faster than a genexpr
                    # or a listcomp that ends up building unnecessary things
                    for l in labels:
                        if l[0] == 'le':
                            bucket_value = float(l[1])
                            # _bucket
                            without_le = tuple(l for l in labels if l[0] != 'le')
                            buckets[without_le][bucket_value] += value
                            break
                    else:  # did not find the `le` key
                        # _sum/_count
                        samples[(name, labels)] += value
                else:
                    # Counter and Summary.
                    samples[(name, labels)] += value

            # Accumulate bucket values.
            if metric.type == 'histogram':
                for labels, values in buckets.items():
                    acc = 0.0
                    for bucket, value in sorted(values.items()):
                        sample_key = f'{metric.name}_bucket', labels + (
                            ('le', floatToGoString(bucket)),
                        )

                        if accumulate:
                            acc += value
                            samples[sample_key] = acc
                        else:
                            samples[sample_key] = value
                    if accumulate:
                        samples[f'{metric.name}_count', labels] = acc

            # Convert to correct sample format.
            metric.samples = [Sample(name_, dict(labels), value) for (name_, labels), value in samples.items()]
        return metrics.values()

    def collect(self):
        files = glob.glob(os.path.join(self._path, '*.db'))
        return self.merge(files, accumulate=True)


def mark_process_dead(pid, path=None):
    """Do bookkeeping for when one process dies in a multi-process setup."""
    if path is None:
        path = os.environ.get('PROMETHEUS_MULTIPROC_DIR', os.environ.get('prometheus_multiproc_dir'))
    for f in glob.glob(os.path.join(path, 'gauge_livesum_{0}.db'.format(pid))):
        os.remove(f)
    for f in glob.glob(os.path.join(path, 'gauge_liveall_{0}.db'.format(pid))):
        os.remove(f)
