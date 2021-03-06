# Copyright 2016 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import os
import pytest
import psutil
import shutil
import tempfile
import time

from resource import setrlimit, RLIMIT_CORE, RLIM_INFINITY
from signal import SIGSEGV, SIGKILL
from tests.common.skip import SkipIfBuildType
from subprocess import CalledProcessError

from tests.common.custom_cluster_test_suite import CustomClusterTestSuite

DAEMONS = ['impalad', 'statestored', 'catalogd']
DAEMON_ARGS = ['impalad_args', 'state_store_args', 'catalogd_args']

class TestBreakpad(CustomClusterTestSuite):
  """Check that breakpad integration into the daemons works as expected. This includes
  writing minidump files on unhandled signals and rotating old minidumps on startup. The
  tests kill the daemons by sending a SIGSEGV signal.
  """
  @classmethod
  def get_workload(cls):
    return 'functional-query'

  def setup_method(self, method):
    if self.exploration_strategy() != 'exhaustive':
      pytest.skip()
    # Override parent
    # The temporary directory gets removed in teardown_method() after each test.
    self.tmp_dir = tempfile.mkdtemp()

  def teardown_method(self, method):
    # Override parent
    # Stop the cluster to prevent future accesses to self.tmp_dir.
    self.kill_cluster(SIGKILL)
    assert self.tmp_dir
    shutil.rmtree(self.tmp_dir)

  @classmethod
  def setup_class(cls):
    if cls.exploration_strategy() != 'exhaustive':
      pytest.skip('breakpad tests only run in exhaustive')
    # Disable core dumps for this test
    setrlimit(RLIMIT_CORE, (0, RLIM_INFINITY))

  @classmethod
  def teardown_class(cls):
    # Re-enable core dumps
    setrlimit(RLIMIT_CORE, (RLIM_INFINITY, RLIM_INFINITY))
    # Start default cluster for subsequent tests (verify_metrics).
    cls._start_impala_cluster([])

  def start_cluster_with_args(self, **kwargs):
    cluster_options = []
    for daemon_arg in DAEMON_ARGS:
      daemon_options = " ".join("-%s=%s" % i for i in kwargs.iteritems())
      cluster_options.append("""--%s='%s'""" % (daemon_arg, daemon_options))
    self._start_impala_cluster(cluster_options)

  def start_cluster(self):
    self.start_cluster_with_args(minidump_path=self.tmp_dir, max_minidumps=2)

  def start_cluster_without_minidumps(self):
    self.start_cluster_with_args(minidump_path='', max_minidumps=2)

  def kill_cluster(self, signal):
    self.cluster.refresh()
    processes = self.cluster.impalads + [self.cluster.catalogd, self.cluster.statestored]
    processes = filter(None, processes)
    self.kill_processes(processes, signal)
    self.assert_all_processes_killed()

  def kill_processes(self, processes, signal):
    for process in processes:
      process.kill(signal)
    self.wait_for_all_processes_dead(processes)

  def wait_for_all_processes_dead(self, processes, timeout=300):
    for process in processes:
      try:
        pid = process.get_pid()
        if not pid:
          continue
        psutil_process = psutil.Process(pid)
        psutil_process.wait(timeout)
      except psutil.TimeoutExpired:
        raise RuntimeError("Unable to kill %s (pid %d) after %d seconds." %
            (psutil_process.name, psutil_process.pid, timeout))

  def assert_all_processes_killed(self):
    self.cluster.refresh()
    assert not self.cluster.impalads
    assert not self.cluster.statestored
    assert not self.cluster.catalogd

  def count_minidumps(self, daemon, base_dir=None):
    base_dir = base_dir or self.tmp_dir
    path = os.path.join(base_dir, daemon)
    return len(glob.glob("%s/*.dmp" % path))

  def count_all_minidumps(self, base_dir=None):
    return sum((self.count_minidumps(daemon, base_dir) for daemon in DAEMONS))

  def assert_num_logfile_entries(self, expected_count):
    self.assert_impalad_log_contains('INFO', 'Wrote minidump to ',
        expected_count=expected_count)
    self.assert_impalad_log_contains('ERROR', 'Wrote minidump to ',
        expected_count=expected_count)

  @pytest.mark.execute_serially
  def test_minidump_creation(self):
    """Check that when a daemon crashes it writes a minidump file."""
    assert self.count_all_minidumps() == 0
    self.start_cluster()
    assert self.count_all_minidumps() == 0
    cluster_size = len(self.cluster.impalads)
    self.kill_cluster(SIGSEGV)
    self.assert_num_logfile_entries(1)
    assert self.count_minidumps('impalad') == cluster_size
    assert self.count_minidumps('statestored') == 1
    assert self.count_minidumps('catalogd') == 1

  @pytest.mark.execute_serially
  def test_minidump_relative_path(self):
    """Check that setting 'minidump_path' to a relative value results in minidump files
    written to 'log_dir'.
    """
    minidump_base_dir = os.path.join(os.environ.get('LOG_DIR', '/tmp'), 'minidumps')
    shutil.rmtree(minidump_base_dir)
    # Omitting minidump_path as a parameter to the cluster will choose the default
    # configuration, which is a FLAGS_log_dir/minidumps.
    self.start_cluster_with_args()
    assert self.count_all_minidumps(minidump_base_dir) == 0
    cluster_size = len(self.cluster.impalads)
    self.kill_cluster(SIGSEGV)
    self.assert_num_logfile_entries(1)
    assert self.count_minidumps('impalad', minidump_base_dir) == cluster_size
    assert self.count_minidumps('statestored', minidump_base_dir) == 1
    assert self.count_minidumps('catalogd', minidump_base_dir) == 1
    shutil.rmtree(minidump_base_dir)

  @pytest.mark.execute_serially
  def test_minidump_cleanup(self):
    """Check that a limited number of minidumps is preserved during startup."""
    assert self.count_all_minidumps() == 0
    self.start_cluster()
    self.kill_cluster(SIGSEGV)
    self.assert_num_logfile_entries(1)
    self.start_cluster()
    expected_impalads = min(len(self.cluster.impalads), 2)
    assert self.count_minidumps('impalad') == expected_impalads
    assert self.count_minidumps('statestored') == 1
    assert self.count_minidumps('catalogd') == 1

  @pytest.mark.execute_serially
  def test_disable_minidumps(self):
    """Check that setting the minidump_path to an empty value disables minidump creation.
    """
    assert self.count_all_minidumps() == 0
    self.start_cluster_without_minidumps()
    self.kill_cluster(SIGSEGV)
    self.assert_num_logfile_entries(0)

  def trigger_single_minidump_and_get_size(self):
    """Kill a single impalad with SIGSEGV to make it write a minidump. Kill the rest of
    the cluster. Clean up the single minidump file and return its size.
    """
    self.cluster.refresh()
    assert len(self.cluster.impalads) > 0
    # Make one impalad write a minidump.
    self.kill_processes(self.cluster.impalads[:1], SIGSEGV)
    # Kill the rest of the cluster.
    self.kill_cluster(SIGKILL)
    assert self.count_minidumps('impalad') == 1
    # Get file size of that miniump.
    path = os.path.join(self.tmp_dir, 'impalad')
    minidump_file = glob.glob("%s/*.dmp" % path)[0]
    minidump_size = os.path.getsize(minidump_file)
    os.remove(minidump_file)
    assert self.count_all_minidumps() == 0
    return minidump_size

  @pytest.mark.execute_serially
  def test_limit_minidump_size(self):
    """Check that setting the 'minidump_size_limit_hint_kb' to a small value will reduce
    the minidump file size.
    """
    assert self.count_all_minidumps() == 0
    # Generate minidump with default settings.
    self.start_cluster()
    full_minidump_size = self.trigger_single_minidump_and_get_size()
    # Start cluster with limited minidump file size, we use a very small value, to ensure
    # the resulting minidump will be as small as possible.
    self.start_cluster_with_args(minidump_path=self.tmp_dir,
        minidump_size_limit_hint_kb=1)
    reduced_minidump_size = self.trigger_single_minidump_and_get_size()
    # Check that the minidump file size has been reduced.
    assert reduced_minidump_size < full_minidump_size

  @SkipIfBuildType.not_dev_build
  @pytest.mark.execute_serially
  def test_dcheck_writes_minidump(self):
    """Check that hitting a DCHECK macro writes a minidump."""
    assert self.count_all_minidumps() == 0
    failed_to_start = False
    try:
      self.start_cluster_with_args(minidump_path=self.tmp_dir,
          beeswax_port=1)
    except CalledProcessError:
      failed_to_start = True
    assert failed_to_start
    assert self.count_minidumps('impalad') > 0
