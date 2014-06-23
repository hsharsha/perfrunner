from time import time, sleep

import numpy as np
from couchbase import Couchbase
from couchbase.user_constants import OBS_NOTFOUND
from logger import logger
from mc_bin_client.mc_bin_client import MemcachedClient, MemcachedError
from tap import TAP
from upr import UprClient
from upr.constants import CMD_STREAM_REQ, SUCCESS

from perfrunner.helpers.cbmonitor import with_stats
from perfrunner.helpers.misc import pretty_dict, uhex
from perfrunner.tests import PerfTest
from perfrunner.workloads.tcmalloc import WorkloadGen


class KVTest(PerfTest):

    """
    The most basic KV workflow:
        Initial data load ->
            Persistence and intra-cluster replication (for consistency) ->
                Data compaction (for consistency) ->
                    "Hot" load or working set warm up ->
                        "access" phase or active workload
    """

    @with_stats
    def access(self):
        super(KVTest, self).timer()

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.compact_bucket()

        self.hot_load()

        self.workload = self.test_config.access_settings
        self.access_bg()
        self.access()


class PersistLatencyTest(KVTest):

    """
    The same as base test but persistence latency is measured.
    """

    COLLECTORS = {'persist_latency': True}

    ALL_BUCKETS = True

    def run(self):
        super(PersistLatencyTest, self).run()

        if self.test_config.stats_settings.enabled:
            latency = self.reporter.post_to_sf(
                *self.metric_helper.calc_observe_latency(percentile=95)
            )
            if hasattr(self, 'experiment'):
                self.experiment.post_results(latency)


class ReplicateLatencyTest(PersistLatencyTest):

    """
    The same as base test but intra-cluster replication latency is measured.
    """

    COLLECTORS = {'replicate_latency': True}


class MixedLatencyTest(KVTest):

    """
    Enables reporting of GET and SET latency.
    """

    COLLECTORS = {'latency': True}

    def run(self):
        super(MixedLatencyTest, self).run()
        if self.test_config.stats_settings.enabled:
            for operation in ('get', 'set'):
                self.reporter.post_to_sf(
                    *self.metric_helper.calc_kv_latency(operation=operation,
                                                        percentile=95)
                )


class ReadLatencyTest(MixedLatencyTest):

    """
    Enables reporting of GET latency.
    """

    COLLECTORS = {'latency': True}

    def run(self):
        super(MixedLatencyTest, self).run()
        if self.test_config.stats_settings.enabled:
            latency_get = self.reporter.post_to_sf(
                *self.metric_helper.calc_kv_latency(operation='get',
                                                    percentile=95)
            )
            if hasattr(self, 'experiment'):
                self.experiment.post_results(latency_get)


class BgFetcherTest(KVTest):

    """
    Enables reporting of average BgFetcher wait time (disk fetches).
    """

    COLLECTORS = {'latency': True}

    ALL_BUCKETS = True

    def run(self):
        super(BgFetcherTest, self).run()
        if self.test_config.stats_settings.enabled:
            self.reporter.post_to_sf(self.metric_helper.calc_avg_bg_wait_time())


class DrainTest(KVTest):

    """
    Enables reporting of average disk write queue size.
    """

    ALL_BUCKETS = True

    def run(self):
        super(DrainTest, self).run()
        if self.test_config.stats_settings.enabled:
            drain_rate = self.reporter.post_to_sf(
                self.metric_helper.calc_avg_disk_write_queue()
            )
            if hasattr(self, 'experiment'):
                self.experiment.post_results(drain_rate)


class FlusherTest(KVTest):

    """
    The maximum drain rate benchmark. Data set is loaded while persistence is
    disabled. After that persistence is enabled and ep-engine commits data with
    maximum possible speed (using very large batches).

    Please notice that measured drain rate has nothing to do with typical
    production characteristics. It only used as baseline / reference.
    """

    def mc_iterator(self):
        password = self.test_config.bucket.password
        for hostname in self.cluster_spec.yield_hostnames():
            for bucket in self.test_config.buckets:
                mc = MemcachedClient(host=hostname, port=11210)
                try:
                    mc.sasl_auth_plain(user=bucket, password=password)
                    yield mc
                except MemcachedError:
                    logger.warn('Auth failure')

    def stop_persistence(self):
        for mc in self.mc_iterator():
            mc.stop_persistence()

    def start_persistence(self):
        for mc in self.mc_iterator():
            mc.start_persistence()

    @with_stats
    def drain(self):
        for master in self.cluster_spec.yield_masters():
            for bucket in self.test_config.buckets:
                self.monitor.monitor_disk_queue(master, bucket)

    def run(self):
        self.stop_persistence()
        self.load()

        self.access_bg()
        self.start_persistence()
        from_ts, to_ts = self.drain()
        time_elapsed = (to_ts - from_ts) / 1000.0

        self.reporter.finish('Drain', time_elapsed)
        if self.test_config.stats_settings.enabled:
            self.reporter.post_to_sf(
                self.metric_helper.calc_max_drain_rate(time_elapsed)
            )


class BeamRssTest(KVTest):

    """
    Enables reporting of Erlang (beam.smp process) memory usage.
    """

    def run(self):
        super(BeamRssTest, self).run()
        if self.test_config.stats_settings.enabled:
            if self.remote.os != 'Cygwin':
                self.reporter.post_to_sf(
                    *self.metric_helper.calc_max_beam_rss()
                )


class WarmupTest(PerfTest):

    """
    After typical workload we restart all nodes and measure time it takes
    to perform cluster warm up (internal technology, not to be confused with
    "hot" load phase).
    """

    def access(self):
        super(WarmupTest, self).timer()

    def warmup(self):
        self.remote.stop_server()
        self.remote.drop_caches()
        self.remote.start_server()
        warmup_time = 0
        for master in self.cluster_spec.yield_masters():
            for bucket in self.test_config.buckets:
                host = master.split(':')[0]
                warmup_time += self.monitor.monitor_warmup(self.memcached,
                                                           host, bucket)
        return round(warmup_time / 10 ** 6 / 60, 1)  # min

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.compact_bucket()

        self.hot_load()

        self.workload = self.test_config.access_settings
        self.access_bg()
        self.access()

        self.wait_for_persistence()
        self.compact_bucket()

        warmup_time = self.warmup()

        self.reporter.post_to_sf(warmup_time)


class TapTest(PerfTest):

    """
    Load data and then read entire data set via TAP protocol (single-threaded
    consumer).
    """

    def consume(self):
        logger.info('Reading data via TAP')
        password = self.test_config.bucket.password
        for master in self.cluster_spec.yield_masters():
            host = master.split(':')[0]
            for bucket in self.test_config.buckets:
                tap = TAP(host=host, bucket=bucket, password=password)
                while True:
                    status, batch = tap.provide_batch()
                    if not batch or status:
                        break

    def run(self):
        self.load()
        self.wait_for_persistence()

        self.reporter.start()
        self.consume()
        self.reporter.finish('Backfilling')


class UprTest(TapTest):

    """
    Load data and then read entire data set via UPR protocol (single-threaded
    consumer).
    """

    def consume(self):
        password = self.test_config.bucket.password
        for master in self.cluster_spec.yield_masters():
            host = master.split(':')[0]
            for bucket in self.test_config.buckets:
                logger.info(
                    'Reading data via UPR from {}/{}'.format(host, bucket)
                )
                upr_client = UprClient(host=host, port=11210)
                upr_client.sasl_auth_plain(username=bucket, password=password)
                mcd_client = MemcachedClient(host=host, port=11210)
                mcd_client.sasl_auth_plain(user=bucket, password=password)

                op = upr_client.open_producer('stream')
                response = op.next_response()
                if response['status'] != SUCCESS:
                    logger.interrupt('Failed to open producer')

                for vb in range(1024):
                    vb_stats = mcd_client.stats('vbucket-seqno {}'.format(vb))
                    uuid = long(vb_stats['vb_{}:uuid'.format(vb)])
                    high_seqno = long(vb_stats['vb_{}:high_seqno'.format(vb)])

                    op = upr_client.stream_req(vb=vb,
                                               flags=0,
                                               start_seqno=0,
                                               end_seqno=high_seqno,
                                               vb_uuid=uuid,
                                               high_seqno=high_seqno)
                    while op.has_response():
                        response = op.next_response()
                        if response['opcode'] != CMD_STREAM_REQ:
                            break
                    upr_client.close_stream(vbucket=vb)
                upr_client.shutdown()


class FragmentationTest(PerfTest):

    """
    This test implements the append-only scenario:
    1. Single node.
    2. Load X items, 700-1400 bytes, average 1KB (11-22 fields).
    3. Append data
        3.1. Mark first 80% of items as working set.
        3.2. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
        3.3. Mark first 40% of items as working set.
        3.4. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
        3.5. Mark first 20% of items as working set.
        3.6. Randomly update 75% of items in working set by adding 1 field at a time (62 bytes).
    4. Repeat step #3 5 times.

    See workloads/tcmalloc.py for details.

    Scenario described above allows to spot issues with memory/allocator
    fragmentation.
    """

    @with_stats
    def load_and_append(self):
        password = self.test_config.bucket.password
        WorkloadGen(self.test_config.load_settings.items,
                    self.master_node, self.test_config.buckets[0],
                    password).run()

    def calc_fragmentation_ratio(self):
        ratios = []
        for target in self.target_iterator:
            host = target.node.split(':')[0]
            stats = self.memcached.get_stats(host, target.bucket,
                                             stats='memory')
            ratio = float(stats['mem_used']) / float(stats['total_heap_bytes'])
            ratios.append(ratio)
        ratio = 100 * (1 - sum(ratios) / len(ratios))
        ratio = round(ratio, 1)
        logger.info('Fragmentation: {}'.format(ratio))
        return ratio

    def run(self):
        self.load_and_append()
        fragmentation_ratio = self.calc_fragmentation_ratio()
        self.reporter.post_to_sf(fragmentation_ratio)


class FragmentationLargeTest(FragmentationTest):

    """
    The same as base test but large documents are used.
    """

    @with_stats
    def load_and_append(self):
        password = self.test_config.bucket.password
        WorkloadGen(self.test_config.load_settings.items,
                    self.master_node, self.test_config.buckets[0], password,
                    small=False).run()


class ReplicationTest(PerfTest):

    """
    Quick replication test. Single documents are sequentially inserted and
    replication latency is measured after each insert.
    """

    NUM_SAMPLES = 5000

    def measure_latency(self):
        logger.info('Measuring replication latency')
        timings = []
        found = lambda cb: [
            v for v in cb.observe(item).value if v.flags != OBS_NOTFOUND
        ]
        password = self.test_config.bucket.password
        for master in self.cluster_spec.yield_masters():
            for bucket in self.test_config.buckets:
                host = master.split(':')[0]
                cb = Couchbase.connect(host=host, port=8091,
                                       bucket=bucket, password=password)
                for _ in range(self.NUM_SAMPLES):
                    item = uhex()
                    cb.set(item, item)
                    t0 = time()
                    while len(found(cb)) != 2:
                        sleep(0.001)
                    latency = 1000 * (time() - t0)  # s -> ms
                    logger.info(latency)
                    timings.append(latency)

        summary = {
            'min': round(min(timings), 1),
            'max': round(max(timings), 1),
            'mean': round(np.mean(timings), 1),
            '80th': round(np.percentile(timings, 80), 1),
            '90th': round(np.percentile(timings, 90), 1),
            '95th': round(np.percentile(timings, 95), 1),
            '99th': round(np.percentile(timings, 99), 1),
        }
        logger.info(pretty_dict(summary))

        if hasattr(self, 'experiment'):
            self.experiment.post_results(summary['95th'])

    def run(self):
        self.measure_latency()
