# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Interesting script to use FFPuppet/Sapphire for fast reduction using lithium.
"""
import argparse
import glob
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import threading
import psutil

import ffpuppet
import sapphire
from ..reporter import Report
from ..target import Target


__author__ = "Jesse Schwartzentruber"
__credits__ = ["Tyson Smith", "Jesse Schwartzentruber", "Jason Kratzer"]


log = logging.getLogger("grizzly.reduce.interesting")  # pylint: disable=invalid-name


class Interesting(object):

    def __init__(self, ignore, target, iter_timeout, no_harness, any_crash, skip, min_crashes,
                 repeat, idle_poll, idle_threshold, idle_timeout, testcase_cache=True):
        self.ignore = ignore  # things to ignore
        self.target = target  # a Puppet to run with
        self.server = None  # a server to serve with
        self.wwwdir = None  # testcase root to serve
        self.orig_sig = None  # signature to reduce to (if specified)
        # alt_crash_cb (if set) will be called with args=(temp_prefix) for any crashes which do
        # not match the original signature (assuming --any-crash is not set)
        self.alt_crash_cb = None
        # interesting_cb (if set) is called with args=(temp_prefix) for any interesting crash
        self.interesting_cb = None
        self.iter_timeout = iter_timeout
        self.no_harness = no_harness
        self.skip = skip
        self.skipped = None
        self.min_crashes = min_crashes
        self.repeat = repeat
        self.idle_poll = idle_poll
        self.any_crash = any_crash
        self.idle_threshold = idle_threshold
        self.idle_timeout = idle_timeout
        # testcase cache remembers if we have seen this reduce_file before and if so return the same
        # interesting result
        self.use_result_cache = testcase_cache
        self.result_cache = {}
        # environment if specified in the testcase
        self.env_mod = None

        class _all(object):  # pylint: disable=too-few-public-methods
            @staticmethod
            def __contains__(item):
                """
                use this for sapphire optional_files argument.
                always return True for 'in' except for the testcase itself
                """
                return item != self.landing_page
        self.optional_files = _all()
        self.landing_page = None  # the file to point the target at
        self.reduce_file = None  # the file to reduce

    def config_environ(self, environ):
        self.env_mod = {}
        with open(environ) as env_fp:
            for line in env_fp:
                line = line.rstrip()
                if not line:
                    continue
                key, value = line.split('=', 1)
                if not value:
                    value = None
                self.env_mod[key] = value

    def init(self, _):
        """Lithium initialization entrypoint
        """
        self.wwwdir = os.path.dirname(os.path.realpath(self.landing_page))
        self.landing_page = os.path.basename(self.landing_page)
        self.skipped = None
        self.result_cache = {}

    def monitor_process(self, iteration_done_event, idle_timeout_event):
        # Wait until timeout is hit before polling
        log.debug('Waiting %r before polling', self.idle_timeout)
        exp_time = time.time() + self.idle_timeout
        while exp_time >= time.time() and not iteration_done_event.is_set():
            time.sleep(0.1)

        while not iteration_done_event.is_set():
            if self.target.poll_for_idle(self.idle_threshold, self.idle_poll):
                idle_timeout_event.set()
                break
            else:
                time.sleep(0.1)

    def update_timeout(self, run_time):
        # If run_time is less than poll-time, update it
        log.debug('Run took %r', run_time)
        new_poll_timeout = max(10, min(run_time * 1.5, self.idle_timeout))
        if new_poll_timeout < self.idle_timeout:
            log.info("Updating poll timeout to: %r", new_poll_timeout)
            self.idle_timeout = new_poll_timeout
        # If run_time * 2 is less than iter_timeout, update it
        # in other words, decrease the timeout if this ran in less than half the timeout
        # (floored at 10s)
        new_iter_timeout = max(10, min(run_time * 2, self.iter_timeout))
        if new_iter_timeout < self.iter_timeout:
            log.info("Updating max timeout to: %r", new_iter_timeout)
            self.iter_timeout = new_iter_timeout
            if self.server is not None:
                self.server.close()
                self.server = None
                # trigger relaunch with new timeout

    @property
    def location(self):
        if self.no_harness:
            return "http://127.0.0.1:%d/%s" % (self.server.get_port(), self.landing_page)
        return "http://127.0.0.1:%d/harness#timeout=%d" % (self.server.get_port(),
                                                           self.iter_timeout * 1000)

    def interesting(self, _, temp_prefix):
        """Lithium main iteration entrypoint.

        This should try the reduction and return True or False based on whether the reduction was
        good or bad.  This is subject to a number of options (skip, repeat, cache) and so may
        result in 0 or more actual runs of the target.

        Args:
            _args (unused): Command line arguments from Lithium (N/A)
            temp_prefix (str): A unique prefix for any files written during this iteration.

        Returns:
            bool: True if reduced testcase is still interesting.
        """
        if self.skip:
            if self.skipped is None:
                self.skipped = 0
            elif self.skipped < self.skip:
                self.skipped += 1
                return False
        n_crashes = 0
        n_tries = max(self.repeat, self.min_crashes)
        if self.use_result_cache:
            with open(self.reduce_file, "rb") as test_fp:
                cache_key = hashlib.sha1(test_fp.read()).hexdigest()
            if cache_key in self.result_cache:
                result = self.result_cache[cache_key]['result']
                if result:
                    log.info("Interesting (cached)")
                    cached_prefix = self.result_cache[cache_key]['prefix']
                    for filename in glob.glob(r"%s_*" % cached_prefix):
                        suffix = os.path.basename(filename).split("_", 1)
                        if os.path.isfile(filename):
                            shutil.copy(filename, "%s_%s" % (temp_prefix, suffix[1]))
                        elif os.path.isdir(filename):
                            shutil.copytree(filename, "%s_%s" % (temp_prefix, suffix[1]))
                        else:
                            raise RuntimeError("Cannot copy non-file/non-directory: %s"
                                               % (filename,))
                else:
                    log.info("Uninteresting (cached)")
                return result
        for i in range(n_tries):
            if (n_tries - i) < (self.min_crashes - n_crashes):
                break  # no longer possible to get min_crashes, so stop
            if self._run(temp_prefix):
                n_crashes += 1
                if n_crashes >= self.min_crashes:
                    if self.interesting_cb is not None:
                        self.interesting_cb(temp_prefix)
                    if self.use_result_cache:
                        self.result_cache[cache_key] = {
                            'result': True,
                            'prefix': temp_prefix
                        }
                    return True
        if self.use_result_cache:
            # No need to save the temp_prefix on uninteresting testcases
            # But let's do it anyway to stay consistent
            self.result_cache[cache_key] = {
                'result': False,
                'prefix': temp_prefix
            }
        return False

    def _run(self, temp_prefix):
        """Run a single iteration against the target and determine if it is interesting. This is the
        low-level iteration function used by `interesting`.

        Args:
            temp_prefix (str): A unique prefix for any files written during this iteration.

        Returns:
            bool: True if reduced testcase is still interesting.
        """
        result = False

        # launch sapphire if needed
        if self.server is None:
            if self.no_harness:
                serve_timeout = self.iter_timeout
            else:
                # wait a few extra seconds to avoid races between the harness & sapphire timing out
                serve_timeout = self.iter_timeout + 10
            # have client error pages (code 4XX) call window.close() after a few seconds
            sapphire.Sapphire.CLOSE_CLIENT_ERROR = 2
            self.server = sapphire.Sapphire(timeout=serve_timeout)

            if not self.no_harness:
                harness = os.path.join(os.path.dirname(__file__), '..', 'corpman', 'harness.html')
                with open(harness, 'rb') as harness_fp:
                    harness = harness_fp.read()
                self.server.add_dynamic_response("/harness", lambda: harness, mime_type="text/html")
                self.server.set_redirect("/first_test", self.landing_page, required=True)

        # (re)launch FFPuppet
        if self.target.closed:
            # Try to launch the browser at most, 4 times
            for _ in range(4):
                try:
                    self.target.launch(self.location, env_mod=self.env_mod)
                    break
                except ffpuppet.LaunchError as exc:
                    log.warn(str(exc))
                    time.sleep(15)

        try:
            start_time = time.time()
            idle_timeout_event = threading.Event()
            iteration_done_event = threading.Event()
            poll = threading.Thread(target=self.monitor_process,
                                    args=(iteration_done_event, idle_timeout_event))
            poll.start()

            def keep_waiting():
                return self.target._puppet.is_healthy() and not idle_timeout_event.is_set()

            if self.no_harness:
                # create a tmp file that will never be served
                # this will keep sapphire serving until timeout or ffpuppet exits
                tempfd, tempf = tempfile.mkstemp(prefix=".lithium-garbage-", suffix=".bin",
                                                 dir=self.wwwdir)
                os.close(tempfd)
                try:
                    # serve the testcase
                    server_status, served = self.server.serve_path(self.wwwdir,
                                                                   continue_cb=keep_waiting)
                finally:
                    os.unlink(tempf)

            else:
                self.server.set_redirect("/next_test", self.landing_page, required=True)
                # serve the testcase
                server_status = self.server.serve_path(self.wwwdir,
                                                       continue_cb=keep_waiting,
                                                       optional_files=self.optional_files)[0]

            end_time = time.time()

            # attempt to detect a failure
            failure_detected = self.target.detect_failure(
                self.ignore,
                server_status == sapphire.SERVED_TIMEOUT)

            # handle failure if detected
            if failure_detected == Target.RESULT_FAILURE:
                self.target.close()

                # save logs
                result_logs = temp_prefix + "_logs"
                os.mkdir(result_logs)
                self.target.save_logs(result_logs, meta=True)

                # create a CrashInfo
                crash = Report.from_path(result_logs).create_crash_info(self.target.binary)

                short_sig = crash.createShortSignature()
                if short_sig == "No crash detected":
                    # XXX: need to change this to support reducing timeouts?
                    log.info("Uninteresting: no crash detected")
                elif self.orig_sig is None or self.orig_sig.matches(crash):
                    result = True
                    log.info("Interesting: %s", short_sig)
                    if self.orig_sig is None and not self.any_crash:
                        self.orig_sig = crash.createCrashSignature(maxFrames=5)
                    self.update_timeout(end_time - start_time)
                else:
                    log.info("Uninteresting: different signature: %s", short_sig)
                    if self.alt_crash_cb is not None:
                        self.alt_crash_cb(temp_prefix)

            elif failure_detected == Target.RESULT_IGNORED:
                log.info("Uninteresting: ignored")
                self.target.close()

                # save logs
                result_logs = temp_prefix + "_logs"
                os.mkdir(result_logs)
                self.target.save_logs(result_logs, meta=True)

            else:
                log.info("Uninteresting: no failure detected")

            # trigger relaunch by closing the browser if needed
            self.target.check_relaunch()

        finally:
            iteration_done_event.set()
            poll.join()

        return result

    def cleanup(self, _):
        """Lithium cleanup entrypoint"""
        try:
            if self.server is not None:
                self.server.close()
                self.server = None
        finally:
            if self.target is not None:
                self.target.cleanup()