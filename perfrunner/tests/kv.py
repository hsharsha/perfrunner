from perfrunner.tests import PerfTest


class KVTest(PerfTest):

    def run(self):
        self._run_load_phase()
        self._compact_bucket()
        self._run_access_phase()
