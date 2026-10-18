from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Semaphore
import unittest
from unittest.mock import patch

import requests

import scrape


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []
        self.lock = Lock()

    def monotonic(self):
        with self.lock:
            return self.value

    def sleep(self, delay):
        with self.lock:
            self.sleeps.append(delay)
            self.value += delay


class SourcePacingTests(unittest.TestCase):
    api = "https://api.lever.co/v0/postings/pointclickcare"
    job = "https://jobs.lever.co/pointclickcare/posting"

    def setUp(self):
        self.clock = FakeClock()
        self.starts = []
        self.starts_lock = Lock()
        self.route = lambda request: self.response(request)

        def send(adapter, request, **kwargs):
            with self.starts_lock:
                self.starts.append((request.url, self.clock.monotonic()))
            return self.route(request)

        patches = (
            patch.dict(
                scrape.SOURCE_HOST_NEXT_REQUEST_AT,
                {host: 0.0 for host in scrape.SOURCE_HOST_MIN_INTERVALS},
                clear=True,
            ),
            patch.object(scrape.time, "monotonic", self.clock.monotonic),
            patch.object(scrape.time, "sleep", self.clock.sleep),
            patch.object(requests.adapters.HTTPAdapter, "send", new=send),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def response(request, status=200, location=None):
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response._content = b"open role"
        if location:
            response.headers["Location"] = location
        return response

    def test_listing_and_detail_requests_share_the_host_interval(self):
        scrape.g(self.api)
        scrape.g(self.api + "/posting")

        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_normal_liveness_uses_the_same_pacing_as_detail_collection(self):
        scrape.g(self.job)
        _, ok, reason = scrape.verify({"url": self.job, "src": "lever"})

        self.assertTrue(ok)
        self.assertEqual(reason, "200")
        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_distinct_hosts_do_not_share_a_crawl_delay(self):
        scrape.g(self.api)
        scrape.g(self.job)
        scrape.g("https://example.com/job")

        self.assertEqual([start for _, start in self.starts], [0.0, 0.0, 0.0])
        self.assertEqual(self.clock.sleeps, [])

    def test_concurrent_callers_cannot_reserve_overlapping_dispatches(self):
        barrier = Barrier(4)

        def fetch(index):
            barrier.wait(timeout=5)
            return scrape.g(self.api + f"/{index}").status_code

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(fetch, range(4)))

        self.assertEqual(results, [200] * 4)
        self.assertEqual([start for _, start in self.starts], [0.0, 1.0, 2.0, 3.0])

    def test_early_wakeup_rechecks_the_actual_monotonic_deadline(self):
        scrape.g(self.api)
        wakes = []

        def early_sleep(delay):
            wakes.append(delay)
            self.clock.sleep(delay / 2 if len(wakes) == 1 else delay)

        with patch.object(scrape.time, "sleep", early_sleep):
            scrape.g(self.api)

        self.assertEqual(len(wakes), 2)
        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_failed_attempts_are_also_paced(self):
        def fail_once(request):
            if len(self.starts) == 1:
                raise requests.ConnectionError("fixture connection failure")
            return self.response(request)

        self.route = fail_once
        with self.assertRaises(requests.ConnectionError):
            scrape._source_get(self.api)
        scrape._source_get(self.api)

        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_existing_retry_backoff_and_request_budget_do_not_bypass_pacing(self):
        self.route = lambda request: self.response(request, status=503)
        budget = {"remaining": 3}

        response = scrape.g(self.api, request_budget=budget)
        with self.assertRaises(scrape.SourceRequestBudgetExceeded):
            scrape.g(self.api, request_budget=budget)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual([start for _, start in self.starts], [0.0, 1.0, 2.0])

    def test_each_same_host_redirect_is_paced(self):
        self.route = lambda request: self.response(
            request, status=302, location=self.job + "/final"
        ) if request.url == self.job else self.response(request)

        response = scrape._source_get(self.job)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url, self.job + "/final")
        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_redirect_from_unpaced_host_cannot_bypass_destination_pacing(self):
        initial = "https://example.com/redirect"
        self.route = lambda request: self.response(
            request, status=302, location=self.job
        ) if request.url == initial else self.response(request)
        scrape.g(self.job)

        scrape._source_get(initial)

        self.assertEqual([start for _, start in self.starts], [0.0, 0.0, 1.0])

    def test_disabling_redirects_is_preserved(self):
        self.route = lambda request: self.response(
            request, status=302, location=self.job + "/final"
        )

        response = scrape._source_get(self.job, allow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.starts), 1)

    def test_host_wait_does_not_consume_a_global_request_slot(self):
        gate = Semaphore(1)

        def sleep_without_slot(delay):
            acquired = gate.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                gate.release()
            self.clock.sleep(delay)

        with (
            patch.object(scrape, "SOURCE_REQUESTS", gate),
            patch.object(scrape.time, "sleep", sleep_without_slot),
        ):
            scrape.g(self.api)
            scrape.g(self.api)

        self.assertEqual([start for _, start in self.starts], [0.0, 1.0])

    def test_global_slot_covers_response_body_not_only_headers(self):
        gate = Semaphore(1)
        case = self

        class CheckedBody(requests.Response):
            @property
            def content(self):
                if not self._content_consumed:
                    acquired = gate.acquire(blocking=False)
                    if acquired:
                        gate.release()
                    case.assertFalse(acquired)
                return super().content

        def body(request):
            response = CheckedBody()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content = b"open role"
            return response

        self.route = body
        with patch.object(scrape, "SOURCE_REQUESTS", gate):
            for url in (self.api, "https://example.com/job"):
                with self.subTest(url=url):
                    self.assertEqual(scrape._source_get(url).content, b"open role")

    def test_unpaced_hosts_still_obey_the_existing_global_semaphore(self):
        gate = Semaphore(2)
        release = Event()
        two_started = Event()
        state_lock = Lock()
        state = {"active": 0, "maximum": 0}

        def hold(request):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                if state["active"] == 2:
                    two_started.set()
            try:
                self.assertTrue(release.wait(timeout=5))
                return self.response(request)
            finally:
                with state_lock:
                    state["active"] -= 1

        self.route = hold
        with (
            patch.object(scrape, "SOURCE_REQUESTS", gate),
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            futures = [
                executor.submit(scrape._source_get, f"https://example.com/{index}")
                for index in range(3)
            ]
            try:
                self.assertTrue(two_started.wait(timeout=5))
                self.assertEqual(state["maximum"], 2)
            finally:
                release.set()
            self.assertEqual([future.result().status_code for future in futures], [200] * 3)

        self.assertEqual(state["active"], 0)
        self.assertEqual(state["maximum"], 2)


if __name__ == "__main__":
    unittest.main()
